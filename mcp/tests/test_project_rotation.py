"""Native refresh faults recover automatically from a durable encrypted pair."""

import json
from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet
from test_continuity import connection, profile
from test_continuity import project_store as store_fixture

from durumari_mcp import project_gateway
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import load_project_profile, save_project_profile

project_store = store_fixture


class Protocol:
    def __init__(self, scope, path, fault=None):
        self.scope, self.path, self.fault = scope, path, fault
        self.calls = []
        self.prepared = None
        self.active = scope.access_token.get_secret_value()
        self.committed = False
        self.failures_left = 2 if fault in {"prepare", "commit"} else 1

    def request(self, path, **kwargs):
        self.calls.append(path)
        if path.endswith("/prepare"):
            number = str(len(self.calls))
            self.prepared = {
                "connection": connection(self.scope),
                "access_token": "mpa_" + number * 43,
                "refresh_token": "mpr_" + number * 43,
                "access_expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            }
            if self.fault == "prepare":
                self.failures_left -= 1
                if not self.failures_left:
                    self.fault = None
                raise AdapterError("project_delivery_unknown")
            return self.prepared
        if path.endswith("/commit"):
            contents = self.path.read_bytes()
            assert self.prepared["refresh_token"].encode() not in contents
            plaintext = Fernet(self.scope.outbox_key.get_secret_value().encode()).decrypt(contents)
            assert self.prepared["refresh_token"] in plaintext.decode()
            assert kwargs["body"]["prepared_refresh_token"] == self.prepared["refresh_token"]
            self.committed = True
            self.active = self.prepared["access_token"]
            if self.fault in {"commit", "interrupt"}:
                fault = self.fault
                self.failures_left -= 1
                if not self.failures_left:
                    self.fault = None
                if fault == "interrupt":
                    raise KeyboardInterrupt
                raise AdapterError("project_delivery_unknown")
            return {
                "status": "committed",
                "connection": connection(self.scope),
                "access_expires_at": self.prepared["access_expires_at"],
            }
        if path.endswith("/connection"):
            assert kwargs["token"].get_secret_value() == self.active
            return connection(self.scope)
        raise AssertionError("Unexpected protocol route")


def setup(project_store, tmp_path, fault=None):
    scope = profile(tmp_path).model_copy(
        update={"access_expires_at": datetime.now(UTC) - timedelta(seconds=1)}
    )
    save_project_profile("team", scope)
    root = tmp_path / "queue"
    protocol = Protocol(scope, root / "team.credentials/refresh.enc", fault)
    gateway = project_gateway.ProjectGateway("team", "claude", root=root)
    gateway.transport.request = protocol.request
    return scope, root, protocol, gateway


@pytest.mark.parametrize("fault", ["prepare", "commit", "interrupt"])
def test_lost_response_or_interruption_recovers_after_restart_without_login(
    project_store, tmp_path, fault
):
    scope, root, protocol, gateway = setup(project_store, tmp_path, fault)
    expected = KeyboardInterrupt if fault == "interrupt" else AdapterError
    with pytest.raises(expected):
        gateway.request("/api/v1/project-agent/connection")
    assert load_project_profile("team").protected_json() == scope.protected_json()
    assert protocol.path.is_file()
    restarted = project_gateway.ProjectGateway("team", "claude", root=root)
    restarted.transport.request = protocol.request
    assert restarted.request("/api/v1/project-agent/connection")["id"] == scope.connection_id
    current = load_project_profile("team")
    assert current.connection_id == scope.connection_id
    assert current.outbox_key == scope.outbox_key
    assert not current.refresh_uncertain and not protocol.path.exists()
    assert current.refresh_token.get_secret_value() == protocol.prepared["refresh_token"]
    assert sum(path.endswith("/prepare") for path in protocol.calls) == (
        3 if fault == "prepare" else 1
    )


@pytest.mark.parametrize("fault", ["prepare", "commit"])
def test_one_lost_response_is_retried_within_the_same_request(project_store, tmp_path, fault):
    scope, _, protocol, gateway = setup(project_store, tmp_path, fault)
    protocol.failures_left = 1
    gateway.request("/api/v1/project-agent/connection")
    assert protocol.committed and not protocol.path.exists()
    assert load_project_profile("team").outbox_key == scope.outbox_key


def test_keychain_write_failure_retains_committed_pair_and_recovers(
    project_store, tmp_path, monkeypatch
):
    scope, root, protocol, gateway = setup(project_store, tmp_path)
    real_save = project_gateway.save_project_profile
    monkeypatch.setattr(
        project_gateway,
        "save_project_profile",
        lambda *args: (_ for _ in ()).throw(AdapterError("protected_project_store_write_failed")),
    )
    with pytest.raises(AdapterError, match="protected_project_store_write_failed"):
        gateway.request("/api/v1/project-agent/connection")
    assert protocol.committed and protocol.path.exists()
    assert load_project_profile("team").protected_json() == scope.protected_json()
    monkeypatch.setattr(project_gateway, "save_project_profile", real_save)
    restarted = project_gateway.ProjectGateway("team", "claude", root=root)
    restarted.transport.request = protocol.request
    restarted.request("/api/v1/project-agent/connection")
    assert not protocol.path.exists()


def test_pair_is_durable_before_commit_and_failed_journal_write_never_consumes(
    project_store, tmp_path, monkeypatch
):
    from durumari_mcp import project_rotation

    scope, _, protocol, gateway = setup(project_store, tmp_path)
    real_write = project_rotation.write_journal

    def fail_pair(path, profile, value):
        if value.prepared_json is not None:
            raise AdapterError("project_refresh_journal_write_failed")
        return real_write(path, profile, value)

    monkeypatch.setattr(project_rotation, "write_journal", fail_pair)
    with pytest.raises(AdapterError, match="project_refresh_journal_write_failed"):
        gateway.request("/api/v1/project-agent/connection")
    assert not protocol.committed
    assert load_project_profile("team").protected_json() == scope.protected_json()
    monkeypatch.setattr(project_rotation, "write_journal", real_write)
    gateway.request("/api/v1/project-agent/connection")
    assert protocol.committed


def test_tampered_journal_never_contacts_server_or_writes_credentials(project_store, tmp_path):
    scope, _, protocol, gateway = setup(project_store, tmp_path, "commit")
    with pytest.raises(AdapterError):
        gateway.request("/api/v1/project-agent/connection")
    ciphertext = bytearray(urlsafe_b64decode(protocol.path.read_bytes()))
    ciphertext[-1] ^= 1
    protocol.path.write_bytes(urlsafe_b64encode(ciphertext))
    calls = len(protocol.calls)
    with pytest.raises(AdapterError, match="project_refresh_journal_invalid"):
        gateway.request("/api/v1/project-agent/connection")
    assert len(protocol.calls) == calls
    assert load_project_profile("team").protected_json() == scope.protected_json()


def test_invalid_prepared_scope_is_never_committed_or_saved(project_store, tmp_path):
    scope, _, protocol, gateway = setup(project_store, tmp_path)

    def changed(path, **kwargs):
        response = protocol.request(path, **kwargs)
        if path.endswith("/prepare"):
            response["connection"]["source_id"] = "unapproved-source"
        return response

    gateway.transport.request = changed
    with pytest.raises(AdapterError, match="project_connection_scope_changed"):
        gateway.request("/api/v1/project-agent/connection")
    assert not protocol.committed
    assert load_project_profile("team").protected_json() == scope.protected_json()


def test_legacy_uncertain_journal_is_not_reinterpreted_as_recoverable(project_store, tmp_path):
    import hashlib

    scope, _, protocol, gateway = setup(project_store, tmp_path)
    protocol.path.parent.mkdir(parents=True)
    protocol.path.write_bytes(
        Fernet(scope.outbox_key.get_secret_value().encode()).encrypt(
            json.dumps(
                {
                    "connection_id": scope.connection_id,
                    "refresh_fingerprint": hashlib.sha256(
                        scope.refresh_token.get_secret_value().encode()
                    ).hexdigest(),
                }
            ).encode()
        )
    )
    with pytest.raises(AdapterError, match="project_login_required"):
        gateway.request("/api/v1/project-agent/connection")
    assert not protocol.calls


def test_recovery_checks_the_fresh_receipt_instead_of_historical_expiry(
    project_store, tmp_path, monkeypatch
):
    scope, _, protocol, gateway = setup(project_store, tmp_path, "interrupt")
    with pytest.raises(KeyboardInterrupt):
        gateway.request("/api/v1/project-agent/connection")
    point = datetime.now(UTC) + timedelta(days=35)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return point

    monkeypatch.setattr(project_gateway, "datetime", Later)
    real_request = protocol.request

    def current(path, **kwargs):
        result = real_request(path, **kwargs)
        if path.endswith("/prepare"):
            result["access_expires_at"] = (point + timedelta(minutes=15)).isoformat()
        row = result.get("connection", result)
        row["expires_at"] = (point + timedelta(days=90)).isoformat()
        return result

    protocol.request = current
    gateway.transport.request = current
    gateway.request("/api/v1/project-agent/connection")
    assert load_project_profile("team").access_expires_at > point
    assert load_project_profile("team").outbox_key == scope.outbox_key
