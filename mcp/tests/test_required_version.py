"""A server-required plugin version blocks work but keeps connections and queued writes."""

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest
from test_context_v2_adapter import context_profile
from test_continuity import project_store as store_fixture
from test_hook_recovery import hook as hook_fixture

from durumari_mcp import project_cli
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import save_project_profile
from durumari_mcp.project_gateway import ContextFailure, ProjectGateway, ProjectTransport
from durumari_mcp.version_info import client_update

project_store, hook = store_fixture, hook_fixture
REFUSAL = {"error": {"code": "client_update_required", "details": {
    "required_version": "0.4.18", "latest_version": "0.4.18", "update_url": "/downloads",
}}}


class Opener:
    def __init__(self, status=426):
        self.status, self.headers = status, []

    def open(self, request, timeout):
        self.headers.append(dict(request.header_items()))
        raise HTTPError(
            request.full_url, self.status, "upgrade", {}, io.BytesIO(json.dumps(REFUSAL).encode())
        )


def test_every_native_request_names_its_version_and_426_needs_an_update(monkeypatch):
    monkeypatch.setattr("durumari_mcp.project_gateway.runtime_version", lambda: "0.4.17")
    transport = ProjectTransport("https://example.com")
    transport.opener = Opener()
    with pytest.raises(ContextFailure) as refused:
        transport.request("/api/v2/projects/p/contexts")
    assert refused.value.status == 426
    assert refused.value.error == REFUSAL["error"]
    with pytest.raises(AdapterError, match="^server_requires_client_update$"):
        transport.request("/api/v1/project-agent/handoff")
    assert [h["X-durumari-client-version"] for h in transport.opener.headers] == ["0.4.17"] * 2


def test_unknown_local_version_is_sent_as_unknown_not_as_free_text(monkeypatch):
    monkeypatch.setattr("durumari_mcp.project_gateway.runtime_version", lambda: "0+unknown")
    transport = ProjectTransport("https://example.com")
    transport.opener = Opener(status=404)
    with pytest.raises(ContextFailure):
        transport.request("/api/v2/projects/p/contexts")
    assert transport.opener.headers[0]["X-durumari-client-version"] == "unknown"


def test_refused_write_stays_queued_and_the_same_key_delivers_after_update(
    project_store, tmp_path, monkeypatch
):
    from durumari_mcp.context_client import ContextClient

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "codex", root=tmp_path / "state")
    calls = []

    def refused(path, **kwargs):
        calls.append((path, kwargs["idempotency_key"]))
        raise ContextFailure(426, REFUSAL["error"])

    gateway.transport.request = refused
    body = {"title": "checkpoint", "body": "selected result"}
    first = ContextClient(gateway, "old-plugin").mutate("POST", "", body, "kept-write")
    assert first == {
        "status": "pending",
        "request_key": "kept-write",
        "error": REFUSAL["error"],
        "next_action": "update_durumari_then_retry_same_request",
    }
    # The updated plugin restarts as a new process with the same profile and queue.
    gateway.transport.request = lambda path, **kwargs: (
        calls.append((path, kwargs["idempotency_key"])) or {
            "status": "stored", "project_id": scope.project_id, "context_id": "doc",
            "revision": 1, "request_key": kwargs["idempotency_key"], "replayed": False,
        }
    )
    updated = ContextClient(ProjectGateway("team", "codex", root=tmp_path / "state"), "updated")
    updated.gateway.transport.request = gateway.transport.request
    assert updated.mutate("POST", "", body, "kept-write")["status"] == "stored"
    assert [key for _, key in calls] == ["kept-write", "kept-write"]
    assert calls[0][0] == calls[1][0]
    assert updated.queue.pending("old-plugin") == 0


@pytest.mark.parametrize(
    ("installed", "required", "state", "blocked"),
    [
        ("0.4.17", "0.4.18", "update_required", True),
        ("0+unknown", "0.4.18", "update_required", True),
        ("0.4.18", "0.4.18", "current", False),
        ("0.4.18", None, "current", False),
    ],
)
def test_status_reports_a_server_enforced_minimum(installed, required, state, blocked):
    release = {
        "client": {"latest_version": "0.4.18", "minimum_version": "0.4.17", "status": "current"},
        "native": {"latest_version": "0.4.18", "supported_min_version": "0.4.17",
                   "required_native_version": required, "update_url": "/downloads"},
    }
    result = client_update(release, installed)
    assert result["state"] == state
    assert result.get("blocked", False) is blocked
    if blocked:
        assert result["required_version"] == required
        assert result["action"] == "durumari-mcp update"


@pytest.mark.parametrize("reason", ["server_requires_client_update", "client_update_required"])
def test_session_start_asks_for_an_update_not_a_reconnect(hook, reason):
    output = project_cli._hook_notice(hook.args, hook.payload, reason, str(hook.root))
    context = json.loads(output["hookSpecificOutput"]["additionalContext"])
    assert context["status"] == "update_required"
    assert context["pending_records"] == "preserved"
    assert "durumari-mcp update" in context["recovery_command"]
    assert "context-connect" not in context["recovery_command"]
    assert "업데이트" in output["systemMessage"]


def test_hook_failure_with_refused_version_keeps_the_update_reason(hook, capsys):
    hook.failure[0] = "server_requires_client_update"
    project_cli.project_hook(SimpleNamespace(**vars(hook.args)))
    context = json.loads(
        json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    )
    assert context["reason"] == "server_requires_client_update"
    assert context["status"] == "update_required"
