"""Account Skill writes retain their explicit target and immutable recovery identity."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from durumari_mcp.account_skill_gateway import AccountSkillGateway
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_gateway import ContextFailure

REF_A = "11111111-1111-1111-1111-111111111111"
REF_B = "22222222-2222-2222-2222-222222222222"
PROJECT = "33333333-3333-3333-3333-333333333333"
SKILL = "44444444-4444-4444-4444-444444444444"
OPERATION = "55555555-5555-5555-5555-555555555555"
ROOT = "/api/v2/agent/skills"


class Account:
    def __init__(self, root):
        self.name, self.client, self.session = "account-test", "codex", "test-process"
        self.root, self.current = root, {REF_A, REF_B}
        self.profile = SimpleNamespace(
            gateway_origin="https://durumari.kr",
            connection_id="connection",
            actor_id="actor",
            client="codex",
            outbox_key=SecretStr(Fernet.generate_key().decode()),
        )
        self.calls, self.failure = [], None

    def targeted(self, ref):
        def resolve():
            self.calls.append(("resolve", ref))
            if ref not in self.current:
                raise AdapterError("project_reference_stale")
            return PROJECT

        return SimpleNamespace(_resolve=resolve)

    def request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        body = kwargs["body"]
        if path.endswith("/read"):
            return {"id": SKILL, "project_id": PROJECT, "revision": 1}
        if path.endswith("/installations"):
            return {"status": "device_reported"}
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        result = {
            "operation_id": OPERATION,
            "project_ref": body["project_ref"],
            "request_key": body["request_key"],
            "project_id": PROJECT,
            "action": body.get("action", "metadata"),
            "state": "completed",
            "skill_id": SKILL,
            "revision": 2,
            **({"secret_id": body["secret_id"]} if "secret_id" in body else {}),
        }
        if "version" in body.get("body", {}):
            result["version"] = body["body"]["version"]
        if body.get("action") == "publish":
            from durumari_mcp.project_skills import bundle_digest

            result["sha256"] = bundle_digest(body["body"])
        return result


def test_account_selection_is_required_and_read_wire_is_strict(tmp_path):
    account = Account(tmp_path)
    base = AccountSkillGateway(account)
    with pytest.raises(AdapterError, match="project_selection_required"):
        base.request(ROOT)
    selected = base.targeted(REF_A)
    assert selected.request(ROOT + "?q=team&limit=3")["project_id"] == PROJECT
    assert account.calls[-1] == (
        "/api/v2/agent/account/skills/read",
        {"body": {"project_ref": REF_A, "path": "", "query": {"q": "team", "limit": 3}}},
    )
    assert base.project_ref is None and selected.project_ref == REF_A
    selected.request(ROOT + "?requires_secret=true&limit=5")
    assert account.calls[-1][1]["body"]["query"] == {"requires_secret": True, "limit": 5}


def test_same_project_same_key_has_separate_encrypted_conversation_queues(tmp_path):
    account = Account(tmp_path)
    base = AccountSkillGateway(account)
    body = {"base_revision": 1, "label": "Private instructions", "tags": []}
    for ref in (REF_A, REF_B):
        receipt = base.targeted(ref).request(
            ROOT + "/" + SKILL + "/metadata", body, method="PATCH", request_key="same-key"
        )
        assert receipt["state"] == "completed" and receipt["project_ref"] == ref
    assert len(list(tmp_path.rglob("context.sqlite3"))) == 2
    assert all(
        b"Private instructions" not in path.read_bytes() for path in tmp_path.rglob("*sqlite3*")
    )
    with pytest.raises(AdapterError, match="context_idempotency_conflict"):
        base.targeted(REF_A).request(
            ROOT + "/" + SKILL + "/metadata",
            {**body, "label": "Changed"},
            method="PATCH",
            request_key="same-key",
        )


def test_unknown_delivery_survives_restart_and_historical_receipt_never_resolves(tmp_path):
    account = Account(tmp_path)
    selected = AccountSkillGateway(account, REF_A)
    account.failure = AdapterError("project_delivery_unknown")
    body = {"base_revision": 1, "label": "Original", "tags": []}
    result = selected.request(
        ROOT + "/" + SKILL + "/metadata", body, method="PATCH", request_key="lost"
    )
    assert result["state"] == "unknown"
    account.current.remove(REF_A)
    account.calls.clear()
    recovered = AccountSkillGateway(account, REF_A).operation("lost")
    assert recovered["state"] == "completed"
    assert [call[0] for call in account.calls] == ["/api/v2/agent/account/skills/operations"]
    with pytest.raises(AdapterError, match="project_reference_stale"):
        selected.request(ROOT + "/" + SKILL + "/metadata", body, method="PATCH", request_key="new")


def test_missing_key_and_device_routes_do_not_send_requests(tmp_path):
    account = Account(tmp_path)
    selected = AccountSkillGateway(account, REF_A)
    for path, key in [(ROOT, None), (ROOT + "/devices", "key")]:
        with pytest.raises(AdapterError):
            selected.request(path, {}, method="POST", request_key=key)
    assert not account.calls


def test_link_receipt_preserves_the_exact_secret_reference_and_current_revisions(tmp_path):
    account = Account(tmp_path)
    body = {
        "base_revision": 1,
        "secret_revision": 2,
        "secret_policy_revision": 3,
        "slot": "api_token",
    }
    result = AccountSkillGateway(account, REF_A).request(
        ROOT + "/" + SKILL + "/secrets/" + REF_B, body, method="PATCH", request_key="link"
    )
    assert result["action"] == "link" and result["secret_id"] == REF_B
    assert account.calls[-1][1]["body"] == {
        "project_ref": REF_A,
        "request_key": "link",
        "action": "link",
        "skill_id": SKILL,
        "secret_id": REF_B,
        "body": body,
    }


def test_upgrade_required_preserves_the_original_request_for_retry(tmp_path):
    account = Account(tmp_path)
    selected = AccountSkillGateway(account, REF_A)
    body = {"base_revision": 1, "label": "Original", "tags": []}
    account.failure = ContextFailure(426, {"code": "client_update_required"})
    with pytest.raises(ContextFailure):
        selected.request(
            ROOT + "/" + SKILL + "/metadata", body, method="PATCH", request_key="upgrade"
        )
    assert (
        selected.request(
            ROOT + "/" + SKILL + "/metadata", body, method="PATCH", request_key="upgrade"
        )["state"]
        == "completed"
    )
    sent = [call for call in account.calls if call[0].endswith("/mutations")]
    assert sent[0] == sent[1]


def test_full_size_bundle_fits_bounded_skill_queue_without_widening_context_queue(tmp_path):
    from durumari_mcp.context_outbox import ContextOutbox

    account = Account(tmp_path)
    header = "---\nname: team\ndescription: Bounded fixture\n---\n"
    body = {
        "name": "team",
        "version": "1.0.0",
        "files": {
            "SKILL.md": header + "x" * (128000 - len(header.encode())),
            **{f"references/{i}.md": "x" * 128000 for i in range(3)},
        },
        "requirements": [],
    }
    result = AccountSkillGateway(account, REF_A).request(
        ROOT, body, method="POST", request_key="large"
    )
    assert result["state"] == "completed"
    assert ContextOutbox.MAX_PAYLOAD_BYTES == 262144


@pytest.mark.parametrize(
    "changed", ["project_ref", "project_id", "action", "skill_id", "files", "source"]
)
def test_mutation_receipt_cannot_change_target_action_or_include_bundle_content(tmp_path, changed):
    account = Account(tmp_path)
    original = account.request

    def response(path, **kwargs):
        result = original(path, **kwargs)
        if changed in {"project_ref", "project_id", "skill_id"}:
            result[changed] = REF_B
        elif changed == "action":
            result[changed] = "delete"
        elif changed == "files":
            result[changed] = {"SKILL.md": "not an allowed receipt"}
        else:
            result[changed] = {"files": "not provenance"}
        return result

    account.request = response
    with pytest.raises(AdapterError, match="invalid_skill_receipt"):
        AccountSkillGateway(account, REF_A).request(
            ROOT + "/" + SKILL + "/metadata",
            {"base_revision": 1, "label": "Team", "tags": []},
            method="PATCH",
            request_key="edit",
        )


def test_historical_operation_must_match_the_persisted_action_before_confirming(tmp_path):
    account = Account(tmp_path)
    selected = AccountSkillGateway(account, REF_A)
    account.failure = AdapterError("project_delivery_unknown")
    selected.request(
        ROOT + "/" + SKILL + "/metadata",
        {"base_revision": 1, "label": "Team", "tags": []},
        method="PATCH",
        request_key="lost",
    )
    original = account.request

    def changed(path, **kwargs):
        return {**original(path, **kwargs), "action": "delete"}

    account.request = changed
    account.current.remove(REF_A)
    with pytest.raises(AdapterError, match="invalid_skill_receipt"):
        selected.operation("lost")
    assert selected._queue(PROJECT).pending(None) == 1


@pytest.mark.parametrize("changed", ["version", "sha256"])
def test_publish_receipt_proves_the_actual_requested_release(tmp_path, changed):
    account = Account(tmp_path)
    original = account.request

    def response(path, **kwargs):
        result = original(path, **kwargs)
        result[changed] = "2.0.0" if changed == "version" else "0" * 64
        return result

    account.request = response
    with pytest.raises(AdapterError, match="invalid_skill_receipt"):
        AccountSkillGateway(account, REF_A).request(
            ROOT,
            {
                "name": "team",
                "version": "1.0.0",
                "files": {"SKILL.md": "---\nname: team\ndescription: Fixture\n---\n"},
                "requirements": [],
            },
            method="POST",
            request_key="publish",
        )


def test_prepared_receipt_keeps_unknown_queue_until_verified_terminal_operation(tmp_path):
    account = Account(tmp_path)
    original = account.request
    state = "prepared"

    def response(path, **kwargs):
        return {**original(path, **kwargs), "state": state}

    account.request = response
    selected = AccountSkillGateway(account, REF_A)
    receipt = selected.request(
        ROOT + "/" + SKILL + "/metadata",
        {"base_revision": 1, "label": "Team", "tags": []},
        method="PATCH",
        request_key="prepared",
    )
    assert receipt["state"] == "prepared" and selected._queue(PROJECT).pending(None) == 1
    assert selected.operation("prepared")["state"] == "prepared"
    assert selected._queue(PROJECT).pending(None) == 1
    state = "completed"
    assert selected.operation("prepared")["state"] == "completed"
    assert selected._queue(PROJECT).pending(None) == 0


def test_account_client_skill_factories_are_immutable_and_have_no_default_project(tmp_path):
    from durumari_mcp.account_client import AccountContextClient

    values = Account(tmp_path)
    client = AccountContextClient.__new__(AccountContextClient)
    client.__dict__.update(values.__dict__)
    assert client.native_skills is True
    base = client.skill_gateway
    first = client.targeted(REF_A).skill_gateway
    second = client.targeted(REF_B).skill_gateway
    assert (base.project_ref, first.project_ref, second.project_ref) == (None, REF_A, REF_B)
    assert base._account is first._account is second._account is client


@pytest.fixture
def folder(tmp_path, monkeypatch):
    import json

    import yaml
    from test_continuity import profile

    from durumari_mcp import account_skill_install, context_removal, project_gateway

    workspace = tmp_path / "folder"
    workspace.mkdir()
    account = Account(tmp_path / "state")
    native = profile(workspace).model_copy(
        update={
            "project_id": PROJECT,
            "client": "codex",
            "gateway_origin": account.profile.gateway_origin,
        }
    )
    account.profile.actor_id = native.actor_id
    (workspace / "durumari.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "server": native.gateway_origin,
                "project_id": PROJECT,
                "source_id": native.source_id,
                "context_source": "server",
            }
        )
    )
    (workspace / ".durumari").mkdir()
    (workspace / ".durumari/context-binding.json").write_text(
        json.dumps({"profile": "actual-folder"})
    )
    monkeypatch.setattr(account_skill_install, "load_project_profile", lambda name: native)
    monkeypatch.setattr(project_gateway, "load_project_profile", lambda name: native)
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "removals")
    monkeypatch.chdir(workspace)
    return account, workspace


def test_local_install_reuses_real_folder_validation_and_never_enrolls(folder, monkeypatch):
    from durumari_mcp import skill_cli
    from durumari_mcp.project_skills import bundle_digest

    account, workspace = folder
    bundle = {
        "id": SKILL,
        "project_id": PROJECT,
        "name": "team",
        "version": "1.0.0",
        "selected_version": "1.0.0",
        "requirements": [],
        "files": {"SKILL.md": "---\nname: team\ndescription: Safe fixture\n---\n"},
    }
    bundle["sha256"] = bundle_digest(bundle)
    original = account.request

    def request(path, **kwargs):
        return bundle if path.endswith("/read") else original(path, **kwargs)

    account.request = request
    monkeypatch.setattr(
        skill_cli, "enroll_device", lambda *args: pytest.fail("account install enrolled a device")
    )
    before = (workspace / "durumari.yaml").read_bytes()
    local = AccountSkillGateway(account, REF_A).local()
    assert local.profile.connection_id == "conn-1" and local.name == "actual-folder"
    result = skill_cli.install_skill(local, bundle, "codex", enroll=False)
    assert result["status"] == "installed"
    assert (workspace / ".durumari/skills/team/SKILL.md").read_text() == bundle["files"]["SKILL.md"]
    assert (workspace / "durumari.yaml").read_bytes() == before


def test_local_install_accepts_an_account_connected_through_the_moved_host(folder):
    account, _ = folder
    account.profile.gateway_origin = "https://app.durumari.cloud"
    assert AccountSkillGateway(account, REF_A).local().name == "actual-folder"


@pytest.mark.parametrize("change", ["server", "actor", "project"])
def test_local_target_mismatch_stops_before_modifying_folder_binding(folder, change):
    account, workspace = folder
    before = (workspace / "durumari.yaml").read_bytes()
    if change == "server":
        account.profile.gateway_origin = "https://preview.durumari.kr"
    elif change == "actor":
        account.profile.actor_id = "different-actor"
    else:
        account.targeted = lambda ref: SimpleNamespace(_resolve=lambda: REF_B)
    with pytest.raises(AdapterError, match="account_skill_folder_target_mismatch"):
        AccountSkillGateway(account, REF_A).local()
    assert (workspace / "durumari.yaml").read_bytes() == before
    assert not (workspace / ".durumari/skills").exists()


def test_account_installs_into_the_user_folder_without_a_folder_binding(
    tmp_path, monkeypatch, isolated_user_data
):
    from durumari_mcp import skill_cli
    from durumari_mcp.project_skills import bundle_digest

    unbound = tmp_path / "plain"
    unbound.mkdir()
    monkeypatch.chdir(unbound)
    account = Account(tmp_path / "state")
    bundle = {
        "id": SKILL,
        "project_id": PROJECT,
        "name": "team",
        "version": "1.0.0",
        "selected_version": "1.0.0",
        "requirements": [],
        "files": {"SKILL.md": "---\nname: team\ndescription: Safe fixture\n---\n"},
    }
    bundle["sha256"] = bundle_digest(bundle)
    sent = []
    original = account.request

    def request(path, **kwargs):
        sent.append(path)
        return bundle if path.endswith("/read") else original(path, **kwargs)

    account.request = request
    monkeypatch.setattr(
        skill_cli, "enroll_device", lambda *args: pytest.fail("account install enrolled a device")
    )
    local = AccountSkillGateway(account, REF_A).local()
    root = Path(local.profile.workspace)
    assert root.is_relative_to(isolated_user_data / "account-skills")
    assert root.name == PROJECT and local.profile.project_id == PROJECT
    assert skill_cli.install_skill(local, bundle, "codex", enroll=False)["status"] == "installed"
    assert (root / ".durumari/skills/team/SKILL.md").read_text() == bundle["files"]["SKILL.md"]
    assert not (unbound / ".durumari").exists() and not (unbound / "durumari.yaml").exists()
    assert any(path.endswith("/installations") for path in sent)


def test_a_busy_server_keeps_the_skill_request_queued_for_the_same_key(tmp_path):
    # Retries spent on a refusal the server never processed.
    account = Account(tmp_path)
    selected = AccountSkillGateway(account, REF_A)
    body = {"base_revision": 1, "label": "Original", "tags": []}
    busy = {"code": "project_rate_limited", "details": {"retry_after_seconds": 2}}
    account.failure = ContextFailure(429, busy)
    first = selected.request(
        ROOT + "/" + SKILL + "/metadata", body, method="PATCH", request_key="busy"
    )
    assert first["state"] == "pending"
    assert first["error"]["details"]["retry_after_seconds"] == 2
    assert first["next_action"] == "retry_same_request_after_wait"
    again = selected.request(
        ROOT + "/" + SKILL + "/metadata", body, method="PATCH", request_key="busy"
    )
    assert again["state"] == "completed"
    sent = [call for call in account.calls if call[0].endswith("/mutations")]
    assert len(sent) == 2 and sent[0] == sent[1]
