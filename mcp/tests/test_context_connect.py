"""A durable approval survives interrupted setup and temporary outages."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from test_context_v2_adapter import context_profile
from test_continuity import project_store as store_fixture

from durumari_mcp import context_cli, context_removal, required_skills
from durumari_mcp.context_binding import install_binding, shared_profile
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import load_project_profile, save_project_profile
from durumari_mcp.project_gateway import ContextFailure, ProjectTransport

project_store = store_fixture


@pytest.fixture
def setup(project_store, tmp_path, monkeypatch):
    scope = context_profile(tmp_path)
    monkeypatch.setattr(context_cli, "os_store", lambda: project_store)
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(
        required_skills, "sync", lambda gateway, client: {"status": "current", "items": []}
    )
    browsers = []
    monkeypatch.setattr(context_cli.webbrowser, "open", browsers.append)
    args = SimpleNamespace(
        workspace=str(tmp_path),
        server=scope.gateway_origin,
        project=scope.project_id,
        agents=["claude"],
    )
    return scope, args, browsers


def approval(scope):
    return {
        "status": "approved",
        "repository_id": scope.repository_id,
        "workspace_identity": scope.workspace_identity,
        "tokens": {
            "connection": {
                "id": scope.connection_id,
                "project_id": scope.project_id,
                "source_id": scope.source_id,
                "actor_id": scope.actor_id,
                "host": "mcp",
                "actions": ["context:read", "context:write"],
            },
            "access_token": scope.access_token.get_secret_value(),
            "refresh_token": scope.refresh_token.get_secret_value(),
            "access_expires_at": scope.access_expires_at.isoformat(),
        },
    }


def connection(scope):
    return {
        "id": scope.connection_id,
        "project_id": scope.project_id,
        "repository_id": scope.repository_id,
        "workspace_identity": scope.workspace_identity,
    }


def enrollment(scope):
    request_id = "12345678-1234-1234-1234-123456789012"
    return {
        "request_id": request_id,
        "approval_path": "/agent/approve?request=" + request_id,
    }


def test_lost_finish_reply_reuses_stored_approval_without_another_browser(
    setup, project_store, monkeypatch
):
    scope, args, browsers = setup
    paths = []
    finishes = 0

    def request(self, path, **kwargs):
        nonlocal finishes
        paths.append(path)
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            return enrollment(scope)
        if path.endswith("/poll"):
            return approval(scope)
        if path.endswith("/finish"):
            finishes += 1
            if finishes == 1:
                raise AdapterError("project_delivery_unknown")
            return {}
        if path.endswith("/connection"):
            return connection(scope)
        pytest.fail("Unexpected request")

    monkeypatch.setattr(ProjectTransport, "request", request)
    with pytest.raises(AdapterError, match="project_delivery_unknown"):
        context_cli.connect(args)
    assert len(browsers) == 1
    result = context_cli.connect(args)
    assert len(browsers) == 1
    assert paths.count("/api/v2/agent/enrollments") == 1
    assert not load_project_profile(result["profile"]).disabled
    key = shared_profile(scope.gateway_origin, "enrollment", Path(args.workspace))
    assert project_store.get_password(context_cli.ENROLLMENT_SERVICE, key) is None


def test_temporary_outage_keeps_the_grant_and_does_not_reopen_approval(
    setup, project_store, monkeypatch
):
    scope, args, browsers = setup
    save_project_profile("shared", scope)
    install_binding(scope, "shared", args.agents)
    monkeypatch.setattr(
        ProjectTransport,
        "request",
        lambda *a, **kw: (_ for _ in ()).throw(AdapterError("project_delivery_unknown")),
    )
    with pytest.raises(AdapterError, match="project_delivery_unknown"):
        context_cli.connect(args)
    assert not load_project_profile("shared").disabled
    assert browsers == []
    monkeypatch.setattr(ProjectTransport, "request", lambda *a, **kw: connection(scope))
    assert context_cli.connect(args)["profile"] == "shared"
    assert browsers == []


def test_revoked_grant_is_disabled_and_new_approval_keeps_old_encrypted_key(
    setup, project_store, monkeypatch
):
    scope, args, browsers = setup
    save_project_profile("shared", scope)
    install_binding(scope, "shared", args.agents)
    renewed = scope.model_copy(update={"connection_id": "connection-new"})

    def request(self, path, **kwargs):
        if path.endswith("/connection"):
            raise ContextFailure(410, {"code": "connection_revoked"})
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            return enrollment(renewed)
        if path.endswith("/poll"):
            return approval(renewed)
        if path.endswith("/finish"):
            return {}
        pytest.fail("Unexpected request")

    monkeypatch.setattr(ProjectTransport, "request", request)
    result = context_cli.connect(args)
    assert len(browsers) == 1
    assert load_project_profile("shared").disabled
    assert load_project_profile("shared").outbox_key == scope.outbox_key
    assert result["profile"] != "shared"
    assert load_project_profile(result["profile"]).connection_id == renewed.connection_id


def test_refused_token_refresh_is_disabled_and_reopens_approval(
    setup, project_store, monkeypatch
):
    # The v1 refresh reports a revoked or expired grant as project_access_denied,
    # not as the v2 ContextFailure the connection lookup checks.
    scope, args, browsers = setup
    save_project_profile("shared", scope)
    install_binding(scope, "shared", args.agents)
    renewed = scope.model_copy(update={"connection_id": "connection-new"})

    def request(self, path, **kwargs):
        if path.endswith("/connection"):
            raise AdapterError("project_access_denied")
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            return enrollment(renewed)
        if path.endswith("/poll"):
            return approval(renewed)
        if path.endswith("/finish"):
            return {}
        pytest.fail("Unexpected request")

    monkeypatch.setattr(ProjectTransport, "request", request)
    result = context_cli.connect(args)
    assert len(browsers) == 1
    assert load_project_profile("shared").disabled
    assert load_project_profile("shared").outbox_key == scope.outbox_key
    assert load_project_profile(result["profile"]).connection_id == renewed.connection_id


def test_expired_enrollment_gets_one_fresh_device_secret(setup, project_store, monkeypatch):
    scope, args, browsers = setup
    device_tokens = []

    def request(self, path, **kwargs):
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            device_tokens.append(kwargs["body"]["device_token"])
            if len(device_tokens) == 1:
                raise ContextFailure(410, {"code": "enrollment_expired"})
            return enrollment(scope)
        if path.endswith("/poll"):
            return approval(scope)
        if path.endswith("/finish"):
            return {}
        pytest.fail("Unexpected request")

    monkeypatch.setattr(ProjectTransport, "request", request)
    assert context_cli.connect(args)["project_id"] == scope.project_id
    assert len(device_tokens) == 2 and device_tokens[0] != device_tokens[1]
    assert len(browsers) == 1


def test_request_left_for_another_project_restarts_with_a_fresh_secret(
    setup, project_store, monkeypatch
):
    scope, args, browsers = setup
    device_tokens = []

    def request(self, path, **kwargs):
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            device_tokens.append(kwargs["body"]["device_token"])
            if len(device_tokens) == 1:
                raise ContextFailure(409, {"code": "enrollment_scope_changed"})
            return enrollment(scope)
        if path.endswith("/poll"):
            return approval(scope)
        if path.endswith("/finish"):
            return {}
        pytest.fail("Unexpected request")

    monkeypatch.setattr(ProjectTransport, "request", request)
    assert context_cli.connect(args)["project_id"] == scope.project_id
    assert len(device_tokens) == 2 and device_tokens[0] != device_tokens[1]


def test_denied_request_is_forgotten_so_the_next_run_asks_again(setup, project_store, monkeypatch):
    scope, args, browsers = setup
    device_tokens = []

    def request(self, path, **kwargs):
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            device_tokens.append(kwargs["body"]["device_token"])
            return enrollment(scope)
        if path.endswith("/poll"):
            return {"status": "denied"}
        pytest.fail("Unexpected request")

    monkeypatch.setattr(ProjectTransport, "request", request)
    for _ in range(2):
        with pytest.raises(AdapterError, match="project_connection_cancelled"):
            context_cli.connect(args)
    assert len(device_tokens) == 2 and device_tokens[0] != device_tokens[1]
    assert len(browsers) == 2


def test_reconnect_from_one_agent_moves_every_agent_off_the_revoked_grant(
    setup, project_store, monkeypatch
):
    # Claude reconnected with --agents claude; Codex kept naming the
    # disabled profile, so its hooks reported a failure and its MCP tools vanished.
    scope, args, browsers = setup
    save_project_profile("shared", scope)
    install_binding(scope, "shared", ["codex", "claude"])
    renewed = scope.model_copy(update={"connection_id": "connection-new"})

    def request(self, path, **kwargs):
        if path.endswith("/connection"):
            raise ContextFailure(410, {"code": "connection_revoked"})
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            return enrollment(renewed)
        if path.endswith("/poll"):
            return approval(renewed)
        if path.endswith("/finish"):
            return {}
        pytest.fail("Unexpected request")

    monkeypatch.setattr(ProjectTransport, "request", request)
    result = context_cli.connect(args)
    assert len(browsers) == 1
    assert load_project_profile("shared").disabled
    assert set(result["configured_agents"]) == {"claude", "codex"}
    root = Path(args.workspace)
    for name in (".codex/config.toml", ".codex/hooks.json"):
        text = (root / name).read_text(encoding="utf-8")
        assert result["profile"] in text
        assert '"shared"' not in text and "--profile shared " not in text
