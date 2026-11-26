"""Account credentials stay private while each operation retains its explicit target."""

import json
import sys
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from durumari_mcp import account_cli, account_client, account_credentials
from durumari_mcp.account_client import AccountContextClient
from durumari_mcp.account_credentials import (
    AccountProfile,
    load_account_profile,
    save_account_profile,
)
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_gateway import ContextFailure, ProjectTransport

CONNECTION = "11111111-1111-1111-1111-111111111111"
ACTOR = "22222222-2222-2222-2222-222222222222"
REF_A = "33333333-3333-3333-3333-333333333333"
REF_B = "44444444-4444-4444-4444-444444444444"
PROJECT_A = "55555555-5555-5555-5555-555555555555"
PROJECT_B = "66666666-6666-6666-6666-666666666666"
CONTEXT = "77777777-7777-7777-7777-777777777777"
REQUEST = "88888888-8888-8888-8888-888888888888"
ACCESS = "mpa_" + "A" * 43
REFRESH = "mpr_" + "B" * 43


class MemoryStore:
    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[service, name] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


@pytest.fixture
def store(monkeypatch, tmp_path_factory):
    result = MemoryStore()
    monkeypatch.setattr(account_credentials, "os_store", lambda: result)
    state = tmp_path_factory.mktemp("account-state")
    monkeypatch.setattr(account_cli, "state_root", lambda: state)
    monkeypatch.setattr(account_client, "state_root", lambda: state)
    return result


def connection():
    return {
        "id": CONNECTION,
        "actor_id": ACTOR,
        "client": "codex",
        "provider_family": "openai",
        "scope_kind": "account",
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
    }


def approved():
    return {
        "status": "approved",
        "connection": connection(),
        "tokens": {
            "access_token": ACCESS,
            "refresh_token": REFRESH,
            "access_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
    }


def profile(**changes):
    data = {
        "gateway_origin": "https://durumari.kr",
        "connection_id": CONNECTION,
        "actor_id": ACTOR,
        "client": "codex",
        "provider_family": "openai",
        "expires_at": datetime.now(UTC) + timedelta(days=1),
        "access_token": ACCESS,
        "refresh_token": REFRESH,
        "access_expires_at": datetime.now(UTC) + timedelta(hours=1),
        "outbox_key": Fernet.generate_key().decode(),
    }
    return AccountProfile.model_validate({**data, **changes})


def receipt(project=PROJECT_A, key="same-key"):
    return {
        "status": "stored",
        "project_id": project,
        "context_id": CONTEXT,
        "revision": 1,
        "request_key": key,
        "replayed": False,
    }


class Transport:
    def __init__(self):
        self.calls = []
        self.fail_write = False

    def request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        if path == "/api/v2/agent/projects/resolve":
            ref = kwargs["body"]["project_ref"]
            return {"project_id": PROJECT_A if ref == REF_A else PROJECT_B}
        if path == "/api/v2/agent/context/mutations":
            if self.fail_write:
                raise AdapterError("project_delivery_unknown")
            body = kwargs["body"]
            return receipt(
                PROJECT_A if body["project_ref"] == REF_A else PROJECT_B, body["request_key"]
            )
        if path == "/api/v2/agent/projects/operation":
            return receipt(key=kwargs["body"]["request_key"])
        if path == "/api/v2/agent/account/connection":
            return connection()
        return {"items": []}


def client(tmp_path, store, transport=None, **changes):
    save_account_profile("personal", profile(**changes))
    return AccountContextClient(
        "personal", "codex", root=tmp_path, transport=transport or Transport()
    )


def test_account_profile_uses_separate_store_and_rejects_client_or_origin_substitution(store):
    saved = profile()
    save_account_profile("personal", saved)
    assert load_account_profile("personal") == saved
    assert ACCESS not in repr(saved) and REFRESH not in repr(saved)
    assert all("Account" in service for service, _ in store.values)
    with pytest.raises(AdapterError, match="account_client_mismatch"):
        saved.check_client("cursor")
    with pytest.raises(ValueError):
        profile(gateway_origin="https://durumari.kr@attacker.invalid")


def test_fresh_read_resolves_immutable_target_and_never_changes_other_view(tmp_path, store):
    transport = Transport()
    account = client(tmp_path, store, transport)
    first, second = account.targeted(REF_A), account.targeted(REF_B)
    assert not transport.calls  # Construction must not reject an old receipt reference.
    first.read("/search", q="selected")
    second.read()
    first.read()
    reads = [(path, args) for path, args in transport.calls if "/contexts" in path]
    assert [path.split("/contexts")[0] for path, _ in reads] == [
        "/api/v2/projects/" + PROJECT_A,
        "/api/v2/projects/" + PROJECT_B,
        "/api/v2/projects/" + PROJECT_A,
    ]
    assert [args["project_ref"] for _, args in reads] == [REF_A, REF_B, REF_A]
    with pytest.raises(AdapterError, match="project_selection_required"):
        account.read()


@pytest.mark.parametrize(
    ("method", "suffix", "action"),
    [
        ("POST", "", "create"),
        ("PATCH", "/" + CONTEXT, "patch"),
        ("DELETE", "/" + CONTEXT, "delete"),
        ("POST", "/" + CONTEXT + "/restore", "restore"),
    ],
)
def test_mutations_use_account_route_and_original_target_key(
    tmp_path, store, method, suffix, action
):
    transport = Transport()
    account = client(tmp_path, store, transport)
    body = {"summary": "Reported"}
    assert account.targeted(REF_A).mutate(method, suffix, body, "same-key") == receipt()
    path, args = transport.calls[-1]
    assert path == "/api/v2/agent/context/mutations"
    assert args["body"] == {
        "project_ref": REF_A,
        "request_key": "same-key",
        "action": action,
        **({"context_id": CONTEXT} if suffix else {}),
        "body": body,
    }
    assert args["idempotency_key"] == "same-key"


def test_unknown_write_is_encrypted_ref_scoped_and_receipt_recovers_after_restart(tmp_path, store):
    transport = Transport()
    account = client(tmp_path, store, transport)
    transport.fail_write = True
    body = {"title": "PRIVATE_CONTEXT_TEXT"}
    assert account.targeted(REF_A).mutate("POST", "", body, "same-key")["status"] == "unknown"
    # The same key in another target must not return or consume A's request.
    assert account.targeted(REF_B).mutate("POST", "", body, "same-key")["status"] == "unknown"
    assert all(
        b"PRIVATE_CONTEXT_TEXT" not in path.read_bytes() for path in tmp_path.rglob("*.sqlite3")
    )
    restarted = AccountContextClient("personal", "codex", root=tmp_path, transport=transport)
    transport.calls.clear()
    assert restarted.targeted(REF_A).operation("same-key") == receipt()
    assert [path for path, _ in transport.calls] == ["/api/v2/agent/projects/operation"]


def test_wrong_project_receipt_cannot_confirm_a_write(tmp_path, store):
    class WrongTransport(Transport):
        def request(self, path, **kwargs):
            result = super().request(path, **kwargs)
            return receipt(PROJECT_B) if path.endswith("/mutations") else result

    account = client(tmp_path, store, WrongTransport())
    with pytest.raises(AdapterError, match="invalid_context_receipt"):
        account.targeted(REF_A).mutate("POST", "", {"summary": "Safe"}, "same-key")


def test_unknown_refresh_is_not_replayed_and_requires_safe_reconnection(tmp_path, store):
    class UnknownRefresh(Transport):
        def request(self, path, **kwargs):
            self.calls.append((path, kwargs))
            raise AdapterError("project_delivery_unknown")

    transport = UnknownRefresh()
    account = client(
        tmp_path, store, transport, access_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    for _ in range(2):
        with pytest.raises(AdapterError, match="account_reconnect_required"):
            account.status()
    assert [path for path, _ in transport.calls] == ["/api/v2/agent/account/token"]
    assert load_account_profile("personal").refresh_uncertain


def test_enrollment_unknown_start_reuses_protected_proof_and_no_folder_files(
    tmp_path, store, monkeypatch, capsys
):
    class EnrollmentTransport:
        calls = []
        fail = True

        def __init__(self, origin):
            self.origin = origin

        def request(self, path, **kwargs):
            self.calls.append((path, kwargs))
            if path.endswith("/poll"):
                return approved()
            if self.fail:
                self.fail = False
                raise AdapterError("project_delivery_unknown")
            return {
                "request_id": REQUEST,
                "approval_url": "/connect/account?request=" + REQUEST,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "interval_seconds": 2,
            }

    transport = EnrollmentTransport("https://durumari.kr")
    monkeypatch.setattr(account_cli, "ProjectTransport", lambda origin: transport)
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(
        client="codex", server="https://durumari.kr", profile="personal", no_browser=True
    )
    with pytest.raises(AdapterError, match="project_delivery_unknown"):
        account_cli.connect(args)
    result = account_cli.connect(args)
    assert result["state"] == "active"
    starts = [body["body"] for path, body in transport.calls if not path.endswith("/poll")]
    assert len(starts) == 2 and starts[0] == starts[1]
    assert starts[0]["device_code"].startswith("mcd_")
    assert load_account_profile("personal").access_token.get_secret_value() == ACCESS
    assert not list(tmp_path.iterdir())
    output = json.dumps(result) + capsys.readouterr().out + capsys.readouterr().err
    assert all(value not in output for value in [ACCESS, REFRESH, starts[0]["device_code"]])


def test_credentials_or_proof_never_follow_an_untrusted_approval_url(store, monkeypatch):
    class MaliciousTransport:
        def request(self, path, **kwargs):
            return {
                "request_id": REQUEST,
                "approval_url": "https://attacker.invalid/collect",
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "interval_seconds": 2,
            }

    monkeypatch.setattr(account_cli, "ProjectTransport", lambda origin: MaliciousTransport())
    monkeypatch.setattr(account_cli.webbrowser, "open", lambda url: pytest.fail("must not open"))
    args = SimpleNamespace(
        client="codex", server="https://durumari.kr", profile="personal", no_browser=False
    )
    with pytest.raises(AdapterError, match="invalid_account_enrollment"):
        account_cli.connect(args)


def test_account_commands_are_exposed_without_project_or_workspace_arguments():
    from durumari_mcp.cli import build_parser

    parser = build_parser()
    connect = parser.parse_args(["account-connect", "--client", "cursor", "--no-browser"])
    assert connect.client == "cursor" and not hasattr(connect, "workspace")
    serve = parser.parse_args(["account-serve", "--client", "codex", "--profile", "personal"])
    assert serve.profile == "personal"


def test_targeted_secret_reads_use_metadata_only_account_route(tmp_path, store):
    transport = Transport()
    account = client(tmp_path, store, transport)
    account.targeted(REF_A).secrets()
    assert transport.calls[-1][0] == "/api/v2/agent/context/secrets"
    assert transport.calls[-1][1]["project_ref"] == REF_A
    with pytest.raises(AdapterError, match="invalid_project_operation"):
        account.targeted(REF_A).secrets("/values")


def test_refreshed_credentials_are_durably_reloaded_by_sibling_clients(tmp_path, store):
    class RefreshTransport(Transport):
        def request(self, path, **kwargs):
            if path.endswith("/account/token"):
                self.calls.append((path, kwargs))
                result = approved()
                result["tokens"]["refresh_token"] = "mpr_" + "C" * 43
                result["tokens"]["access_token"] = "mpa_" + "D" * 43
                result.pop("status")
                return result
            return super().request(path, **kwargs)

    transport = RefreshTransport()
    first = client(
        tmp_path, store, transport, access_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    second = AccountContextClient("personal", "codex", root=tmp_path, transport=transport)
    assert first.status()["state"] == "active"
    assert second.status()["state"] == "active"
    assert sum(path.endswith("/account/token") for path, _ in transport.calls) == 1
    metadata = [args for path, args in transport.calls if path.endswith("/account/connection")]
    assert all(args["token"].get_secret_value() == "mpa_" + "D" * 43 for args in metadata)


def test_invalid_refresh_identity_never_replaces_original_profile(tmp_path, store):
    class WrongRefresh(Transport):
        def request(self, path, **kwargs):
            value = approved()
            value["connection"]["actor_id"] = PROJECT_B
            return value

    account = client(
        tmp_path, store, WrongRefresh(), access_expires_at=datetime.now(UTC) - timedelta(seconds=1)
    )
    with pytest.raises(AdapterError, match="account_reconnect_required"):
        account.status()
    saved = load_account_profile("personal")
    assert saved.actor_id == ACTOR and saved.refresh_uncertain


def test_unknown_payload_is_not_replaced_by_same_key_on_retry(tmp_path, store):
    transport = Transport()
    account = client(tmp_path, store, transport)
    transport.fail_write = True
    target = account.targeted(REF_A)
    assert target.mutate("POST", "", {"title": "Original"}, "same-key")["status"] == "unknown"
    with pytest.raises(AdapterError, match="context_idempotency_conflict"):
        target.mutate("POST", "", {"title": "Changed"}, "same-key")
    assert sum(path.endswith("/mutations") for path, _ in transport.calls) == 1


def test_saved_receipt_from_wrong_target_cannot_reconcile_existing_unknown(tmp_path, store):
    class WrongOperation(Transport):
        def request(self, path, **kwargs):
            if path.endswith("/projects/operation"):
                return receipt(PROJECT_B)
            return super().request(path, **kwargs)

    transport = WrongOperation()
    account = client(tmp_path, store, transport)
    transport.fail_write = True
    target = account.targeted(REF_A)
    assert target.mutate("POST", "", {"title": "Original"}, "same-key")["status"] == "unknown"
    with pytest.raises(AdapterError, match="context_outbox_scope_mismatch"):
        target.operation("same-key")


def test_pending_proof_survives_failure_to_store_approved_tokens(store, monkeypatch):
    class EnrollmentTransport:
        def request(self, path, **kwargs):
            if path.endswith("/poll"):
                return approved()
            return {
                "request_id": REQUEST,
                "approval_url": "/connect/account?request=" + REQUEST,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "interval_seconds": 2,
            }

    original = store.set_password

    def fail_profile(service, name, value):
        if service == account_credentials.ACCOUNT_SERVICE:
            raise RuntimeError("private OS store failure")
        original(service, name, value)

    monkeypatch.setattr(store, "set_password", fail_profile)
    monkeypatch.setattr(account_cli, "ProjectTransport", lambda origin: EnrollmentTransport())
    args = SimpleNamespace(
        client="codex", server="https://durumari.kr", profile="personal", no_browser=True
    )
    with pytest.raises(AdapterError, match="protected_account_store_write_failed"):
        account_cli.connect(args)
    assert account_credentials.load_pending("personal") is not None


def test_transport_sets_only_fixed_reference_header_and_rejects_header_injection():
    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return b'{"items":[]}'

    calls = []

    def open_request(request, timeout):
        calls.append((request, timeout))
        return Response()

    transport = ProjectTransport("https://durumari.kr")
    transport.opener = SimpleNamespace(open=open_request)
    transport.request(
        "/api/v2/projects/" + PROJECT_A + "/contexts", token=SecretStr(ACCESS), project_ref=REF_A
    )
    request, timeout = calls[0]
    headers = dict((key.lower(), value) for key, value in request.header_items())
    assert headers["x-durumari-project-ref"] == REF_A
    assert headers["authorization"] == "ProjectPlugin " + ACCESS
    assert headers["x-durumari-client-version"] == account_client.runtime_version()
    assert "cookie" not in headers and timeout == 10
    with pytest.raises(AdapterError, match="invalid_project_reference"):
        transport.request(
            "/api/v2/projects/" + PROJECT_A + "/contexts", project_ref=REF_A + "\r\nCookie: private"
        )
    assert len(calls) == 1


def test_account_serve_uses_new_client_without_touching_context_binding(
    store, monkeypatch, tmp_path
):
    from durumari_mcp.cli import build_parser, execute

    save_account_profile("personal", profile())
    created = []
    served = []
    registered = []
    monkeypatch.setattr(
        account_cli,
        "create_context_server",
        lambda client: created.append(client) or SimpleNamespace(
            add_tool=lambda fn, **kwargs: registered.append(fn.__name__),
            list_tools=None,
            call_tool=None,
        ),
    )
    # The stdio loop itself is `serve_stdio`.
    monkeypatch.setattr(account_cli, "serve_stdio", served.append)
    monkeypatch.chdir(tmp_path)
    execute(
        build_parser().parse_args(["account-serve", "--profile", "personal", "--client", "codex"])
    )
    assert len(created) == 1 and isinstance(created[0], AccountContextClient)
    assert len(served) == 1 and hasattr(served[0], "add_tool")
    assert registered == ["durumari_account_migration_verify"]
    assert not list(tmp_path.iterdir())


def test_browser_status_strips_local_wait_control_from_strict_http_contract(tmp_path, store):
    class StrictStatus(Transport):
        def request(self, path, **kwargs):
            assert path == "/api/v2/agent/projects/status"
            assert kwargs["body"] == {"task_ref": REQUEST}
            return {"state": "pending"}

    account = client(tmp_path, store, StrictStatus())
    assert account.projects("status", {"task_ref": REQUEST, "wait_seconds": 0}) == {
        "state": "pending"
    }


def test_approval_wait_and_restarts_poll_without_repeating_admitted_start(store, monkeypatch):
    class PendingTransport:
        starts = 0
        polls = 0

        def request(self, path, **kwargs):
            if path.endswith("/poll"):
                self.polls += 1
                return approved() if self.polls == 32 else {"status": "pending"}
            self.starts += 1
            return {
                "request_id": REQUEST,
                "approval_url": "/connect/account?request=" + REQUEST,
                "expires_at": (datetime.now(UTC) + timedelta(minutes=10)).isoformat(),
                "interval_seconds": 2,
            }

    transport = PendingTransport()
    monkeypatch.setattr(account_cli, "ProjectTransport", lambda origin: transport)
    args = SimpleNamespace(
        client="codex", server="https://durumari.kr", profile="personal", no_browser=True
    )
    for _ in range(31):
        assert account_cli.connect(args)["state"] == "pending"
    assert account_cli.connect(args)["state"] == "active"
    assert transport.starts == 1 and transport.polls == 32


def test_unknown_refresh_cli_guidance_names_safe_account_reconnect(store, monkeypatch, capsys):
    from durumari_mcp import cli

    save_account_profile("personal", profile())

    def fail(args):
        raise AdapterError("account_reconnect_required")

    monkeypatch.setattr(cli, "execute", fail)
    monkeypatch.setattr(
        sys, "argv", ["durumari-mcp", "account-serve", "--client", "codex", "--profile", "personal"]
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    output = capsys.readouterr().err
    assert exited.value.code == 2
    assert (
        "account-connect --client codex --profile personal --server https://durumari.kr" in output
    )
    assert "기존 기록과 미확인 저장은 보존" in output
    assert ACCESS not in output and REFRESH not in output


def test_browser_status_wait_is_local_bounded_and_keeps_exact_task(tmp_path, store, monkeypatch):
    class StatusTransport(Transport):
        def request(self, path, **kwargs):
            self.calls.append((path, kwargs))
            assert kwargs["body"] == {"task_ref": REQUEST}
            return {"state": "pending" if len(self.calls) == 1 else "completed"}

    transport = StatusTransport()
    account = client(tmp_path, store, transport)
    monkeypatch.setattr(account, "request", transport.request)
    clock = iter([0.0, 14.0, 16.0])
    slept = []
    monkeypatch.setattr(account_client.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(account_client.time, "sleep", slept.append)
    assert account.projects("status", {"task_ref": REQUEST, "wait_seconds": 15}) == {
        "state": "completed"
    }
    assert slept == [1.0] and len(transport.calls) == 2


def test_account_requests_and_transcripts_name_the_conversation_project(tmp_path, store):
    transport = Transport()
    account = client(tmp_path, store, transport)
    account.requests("list", {"scope": "assigned", "project_ref": REF_A})
    assert transport.calls[-1][0] == "/api/v2/agent/requests/list"
    assert transport.calls[-1][1]["body"]["project_ref"] == REF_A
    account.requests("note", {"request_key": "note-1", "project_ref": REF_A})
    assert transport.calls[-1][1]["idempotency_key"] == "note-1"
    with pytest.raises(AdapterError, match="invalid_request_operation"):
        account.requests("resolve", {"project_ref": REF_A})
    with pytest.raises(AdapterError, match="project_selection_required"):
        account.transcripts("search", {"query": "예산"})
    account.targeted(REF_A).transcripts("search", {"query": "예산", "limit": 5})
    path, args = transport.calls[-1]
    prefix = "/api/v2/projects/" + PROJECT_A + "/contexts/meeting-transcripts/"
    assert path.startswith(prefix + "search?")
    assert "q=" in path and args["project_ref"] == REF_A
    account.targeted(REF_A).transcripts("read", {"session_id": "s-1", "limit": 10})
    assert transport.calls[-1][0].startswith(prefix + "sessions/s-1?")
    with pytest.raises(AdapterError, match="invalid_transcript_operation"):
        account.targeted(REF_A).transcripts("read", {"session_id": "../x"})


def test_account_server_lists_request_proposal_and_transcript_tools(tmp_path, store):
    import asyncio

    from durumari_mcp.cloud_protocol import create_context_server

    server = create_context_server(client(tmp_path, store, Transport()))
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    for name in (
        "durumari_request_list",
        "durumari_proposal_list",
        "durumari_meeting_transcript_search",
        "durumari_meeting_transcript_read",
    ):
        assert "project_ref" in tools[name].input_schema["properties"]


class NoticeTransport(Transport):
    def request(self, path, **kwargs):
        if path.startswith("/api/v2/release"):
            self.calls.append((path, kwargs))
            return {"client": {"latest_version": "99.0.0", "minimum_version": "0.0.1"}}
        if path == "/api/v2/projects/" + PROJECT_A + "/contexts/policies?recent=0":
            self.calls.append((path, kwargs))
            return {"items": [{"id": "p-1", "when": "always"}], "review_requests": {"open": 1}}
        return super().request(path, **kwargs)


def test_account_status_carries_the_session_start_notices_without_hooks(tmp_path, store):
    transport = NoticeTransport()
    account = client(tmp_path, store, transport)
    unselected = account.status()
    assert unselected["update"]["state"] == "update_available"
    selected = account.targeted(REF_A).status()
    assert selected["project_id"] == PROJECT_A
    assert selected["policy_index"]["items"][0]["id"] == "p-1"
    assert selected["policy_index"]["review_requests"] == {"open": 1}
    assert selected["update"]["latest_version"] == "99.0.0"
    assert selected["pending_writes"] == 0


def test_account_status_survives_unavailable_notices(tmp_path, store):
    class Failing(Transport):
        def request(self, path, **kwargs):
            if path.startswith("/api/v2/release") or path.endswith("/policies?recent=0"):
                raise AdapterError("project_delivery_unknown")
            return super().request(path, **kwargs)

    selected = client(tmp_path, store, Failing()).targeted(REF_A).status()
    assert selected["policy_index"] == "unavailable"
    assert selected["update"]["state"] == "unknown"


def test_account_instructions_name_status_as_the_hook_free_session_start():
    from durumari_mcp.cloud_protocol import create_context_server

    account = AccountContextClient.__new__(AccountContextClient)
    server = create_context_server(account)
    assert "after resume or compaction, call durumari_project_status" in server.instructions


def test_account_status_points_to_the_folder_connection_it_has_not_replaced(
    tmp_path, store, monkeypatch
):
    folder = tmp_path / "bound"
    (folder / ".durumari").mkdir(parents=True)
    (folder / "durumari.yaml").write_text("schema_version: 1\n")
    record = folder / ".durumari/context-binding.json"
    record.write_text(json.dumps({"profile": "legacy", "agents": ["codex"]}))
    monkeypatch.chdir(folder)
    from durumari_mcp import project_credentials

    stored = {
        "legacy": SimpleNamespace(gateway_origin="https://app.durumari.cloud", actor_id=ACTOR)
    }

    def load(name):
        if name not in stored:
            raise AdapterError("project_credential_missing")
        return stored[name]

    monkeypatch.setattr(project_credentials, "load_project_profile", load)
    account = client(tmp_path, store)
    legacy = account.status()["legacy_folder"]
    assert legacy["state"] == "folder_connection_active"
    assert legacy["workspace"] == str(folder.resolve())
    assert "account-migrate plan --client codex --profile personal" in legacy["next_action"]
    assert record.read_text() == json.dumps({"profile": "legacy", "agents": ["codex"]})
    # Another server, another user or a folder without credentials is not this account's.
    for other in (
        SimpleNamespace(gateway_origin="https://durumari.example", actor_id=ACTOR),
        SimpleNamespace(gateway_origin="https://durumari.kr", actor_id="someone-else"),
    ):
        stored["legacy"] = other
        assert "legacy_folder" not in account.status()
    del stored["legacy"]
    assert "legacy_folder" not in account.status()
    record.write_text(
        json.dumps({"profile": "legacy", "account_clients": {"codex": {"migration_id": "m"}}})
    )
    assert "legacy_folder" not in account.status()
    monkeypatch.chdir(tmp_path)
    assert "legacy_folder" not in account.status()


@pytest.mark.parametrize("status", [429, 503])
def test_a_busy_server_keeps_the_save_queued_and_delivers_it_once_later(tmp_path, store, status):
    # Retries spent on a refusal the server never processed: nothing was applied, so the
    # exact request stays queued for its key instead of becoming a rejection.
    class Busy(Transport):
        busy = True

        def request(self, path, **kwargs):
            if path == "/api/v2/agent/context/mutations" and self.busy:
                self.busy = False
                self.calls.append((path, kwargs))
                raise ContextFailure(
                    status,
                    {"code": "project_rate_limited", "details": {"retry_after_seconds": 3}},
                )
            return super().request(path, **kwargs)

    transport = Busy()
    target = client(tmp_path, store, transport).targeted(REF_A)
    body = {"title": "Saved while busy"}
    first = target.mutate("POST", "", body, "same-key")
    assert first["status"] == "pending"
    assert first["error"]["details"]["retry_after_seconds"] == 3
    assert target.mutate("POST", "", body, "same-key") == receipt()
    sent = [kwargs["body"] for path, kwargs in transport.calls if path.endswith("/mutations")]
    assert len(sent) == 2 and sent[0] == sent[1]
