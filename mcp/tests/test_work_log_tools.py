"""Unknown delivery keeps the request identity; a receipt cannot invent success."""

import pytest
from test_context_v2_adapter import context_profile
from test_continuity import project_store as store_fixture

from durumari_mcp.credentials import AdapterError
from durumari_mcp.work_log_tools import deliver, migration_batch, migration_preview

project_store = store_fixture


def test_unknown_delivery_retains_original_key():
    def send(_body):
        raise AdapterError("project_delivery_unknown")

    result = deliver(send, "create", {"request_key": "one", "body": {"title": "작업"}})
    assert result == {
        "status": "unknown",
        "request_key": "one",
        "next_action": "read_operation_or_retry_same_key_and_payload",
    }


def test_rejects_receipt_for_another_project():
    with pytest.raises(AdapterError, match="invalid_work_log_receipt"):
        deliver(
            lambda _: {
                "status": "stored",
                "request_key": "one",
                "project_id": "other",
                "work_log_id": "work",
            },
            "create",
            {"request_key": "one", "body": {"title": "작업"}},
            project_id="expected",
        )


def test_migration_unknown_delivery_reuses_per_item_key_and_validates_batch():
    class Client:
        def __init__(self):
            self.keys = []

        def work_logs(self, action, payload):
            self.keys.append(payload["request_key"])
            raise AdapterError("project_delivery_unknown")

    client = Client()
    candidates = [{"context_id": "00000000-0000-0000-0000-000000000001", "source_revision": 1}]
    first = migration_batch(client, "batch", candidates)
    second = migration_batch(client, "batch", candidates)
    assert first["results"][0]["status"] == second["results"][0]["status"] == "unknown"
    assert client.keys[0] == client.keys[1] == first["results"][0]["request_key"]
    with pytest.raises(AdapterError, match="invalid_migration_batch"):
        migration_batch(client, "batch", candidates + candidates)


def test_preview_unavailable_does_not_claim_fields_or_expose_untrusted_data():
    class Client:
        def work_logs(self, action, payload):
            if action == "list":
                return {"items": [{"origin": "legacy_context", "id": "one", "title": "작업",
                                  "inventory_status": "ready", "source_revision": 1,
                                  "identity_basis": "tag"}],
                        "next_after": None, "scope": "active_project_visible",
                        "consistency": "live_rescan_required", "excluded": []}
            return {"preview_status": "ready", "copied_fields": ["raw body"],
                    "unknown_fields": [], "body": "must stay hidden"}

        def read(self, suffix, **query):
            return {"revision": 1, "title": "작업"}

    result = migration_preview(Client())
    candidate = result["candidates"][0]
    assert result["unavailable"] == 1
    assert candidate["preview_status"] == "unavailable"
    assert "copied_fields" not in candidate
    assert "must stay hidden" not in str(candidate)


def test_gateway_allows_only_its_project_session_index(project_store, tmp_path, monkeypatch):
    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.project_gateway import ProjectGateway

    monkeypatch.chdir(tmp_path)
    profile = context_profile(tmp_path)
    save_project_profile("synthetic", profile)
    gateway = ProjectGateway("synthetic", "mcp", root=tmp_path / "state")
    seen = []
    gateway.transport.request = lambda route, **kwargs: seen.append(route) or {"ok": True}
    route = f"/api/v2/projects/{profile.project_id}/contexts/work-logs/session"
    assert gateway.context_request(route) == {"ok": True} and seen == [route]
    with pytest.raises(AdapterError, match="invalid_project_operation"):
        gateway.context_request(route, {}, method="POST")
    with pytest.raises(AdapterError, match="invalid_project_operation"):
        gateway.context_request(route.replace(profile.project_id, "another-project"))


def test_gateway_allows_only_supported_work_log_agent_posts(project_store, tmp_path, monkeypatch):
    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.project_gateway import ProjectGateway

    monkeypatch.chdir(tmp_path)
    profile = context_profile(tmp_path)
    save_project_profile("synthetic", profile)
    gateway = ProjectGateway("synthetic", "mcp", root=tmp_path / "state")
    gateway.transport.request = lambda route, **kwargs: {"route": route}
    prefix = "/api/v2/agent/work-logs/"
    for action in ("list", "read", "revision", "create", "update", "import", "operation"):
        result = gateway.context_request(prefix + action, {}, method="POST")
        assert result["route"] == prefix + action
    with pytest.raises(AdapterError, match="invalid_project_operation"):
        gateway.context_request(prefix + "read", method="GET")
    with pytest.raises(AdapterError, match="invalid_project_operation"):
        gateway.context_request(prefix + "delete", {}, method="POST")


def test_project_client_work_log_calls_pass_gateway_and_validate_receipts(
    project_store, tmp_path, monkeypatch
):
    from durumari_mcp.context_client import ContextClient
    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.project_gateway import ProjectGateway

    monkeypatch.chdir(tmp_path)
    profile = context_profile(tmp_path)
    save_project_profile("synthetic", profile)
    gateway = ProjectGateway("synthetic", "mcp", root=tmp_path / "state")
    calls = []

    def request(route, **kwargs):
        calls.append((route, kwargs))
        if route.endswith("/create"):
            return {"status": "stored", "request_key": kwargs["idempotency_key"],
                    "project_id": profile.project_id, "work_log_id": "one"}
        return {"id": "one", "title": "작업"}

    gateway.transport.request = request
    client = ContextClient(gateway, "synthetic")
    assert client.work_logs("create", {"request_key": "one", "body": {"title": "작업"}})[
        "status"
    ] == "stored"
    assert client.work_logs("read", {"work_log_id": "one"})["id"] == "one"
    assert [call[0] for call in calls] == [
        "/api/v2/agent/work-logs/create", "/api/v2/agent/work-logs/read"
    ]
    assert calls[0][1]["idempotency_key"] == "one"


def test_native_hook_rejects_malformed_untrusted_metadata():
    from durumari_mcp.project_cli import _native_work_log_index

    items = [{"id": "one", "title": "test", "status": {"nested": "bad"}, "revision": 1},
             {"id": "two", "title": "test", "status": "in_progress", "revision": True}]
    assert _native_work_log_index({"native_work_logs": {"items": items}})["items"] == []
