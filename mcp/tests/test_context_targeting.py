"""Project references select a fresh operation view for each MCP invocation."""

import json

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.project_gateway import ContextFailure

PROJECT_A = "11111111-1111-1111-1111-111111111111"
PROJECT_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class RecordingView:
    def __init__(self, project, calls):
        self.project = project
        self.calls = calls

    def record(self, method, *args, **kwargs):
        self.calls.append((self.project, method, args, kwargs))
        return {"project_id": self.project, "method": method}

    def read(self, suffix="", **query):
        return self.record("read", suffix, **query)

    def mutate(self, method, suffix, body, key):
        return self.record("mutate", method, suffix, body, key)

    def operation(self, key):
        return self.record("operation", key)

    def policies(self, after=None):
        return self.record("policies")

    def status(self):
        return self.record("status")

    def document_types(self):
        return self.record("document_types")


class TargetedOperations(RecordingView):
    def __init__(self):
        super().__init__("legacy", [])
        self.selected = []

    def targeted(self, ref):
        self.selected.append(ref)
        return RecordingView(ref, self.calls)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "method"),
    [
        ("durumari_context_search", {"query": "needle"}, "read"),
        ("durumari_context_list", {}, "read"),
        ("durumari_context_read", {"context_id": "context"}, "read"),
        (
            "durumari_context_read",
            {"context_id": "context", "view": "attachments", "revision": 2},
            "read",
        ),
        (
            "durumari_context_save",
            {"request_key": "save", "title": "Result", "body": "Selected project"},
            "mutate",
        ),
        (
            "durumari_context_revise",
            {
                "request_key": "revise",
                "context_id": "context",
                "base_revision": 1,
                "changes": {"summary": "Changed"},
            },
            "mutate",
        ),
        (
            "durumari_context_version",
            {"action": "history", "context_id": "context"},
            "read",
        ),
        (
            "durumari_context_version",
            {
                "action": "delete",
                "context_id": "context",
                "base_revision": 1,
                "request_key": "delete",
            },
            "mutate",
        ),
        (
            "durumari_context_version",
            {
                "action": "restore",
                "context_id": "context",
                "base_revision": 2,
                "restore_revision": 1,
                "request_key": "restore",
            },
            "mutate",
        ),
        ("durumari_context_operation", {"request_key": "receipt"}, "operation"),
        ("durumari_policy_index", {}, "policies"),
        ("durumari_project_status", {}, "status"),
        ("durumari_context_related", {"context_id": "context"}, "read"),
        ("durumari_document_types", {}, "document_types"),
    ],
)
async def test_every_context_branch_uses_only_the_explicit_target(name, arguments, method):
    operations = TargetedOperations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, {**arguments, "project_ref": PROJECT_A})
    assert not result.is_error
    assert json.loads(result.content[0].text) == {"project_id": PROJECT_A, "method": method}
    assert operations.selected == [PROJECT_A]
    assert len(operations.calls) == 1
    assert operations.calls[0][0:2] == (PROJECT_A, method)


@pytest.mark.anyio
async def test_document_types_asks_hosts_for_exactly_the_context_read_unit():
    # registered like the Context reads, with the backend's unit
    # (scopes.TOOL_SCOPES); the server refuses a token without it on every call.
    async with Client(create_context_server(TargetedOperations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    tool = tools["durumari_document_types"]
    assert tool.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["context:read"]}]
    assert tool.annotations.read_only_hint is True


@pytest.mark.anyio
async def test_interleaved_targeted_writes_and_legacy_calls_do_not_change_default_target():
    operations = TargetedOperations()
    payload = {"request_key": "same-key", "title": "Result", "body": "Reported result"}
    async with Client(create_context_server(operations)) as client:
        for project in [PROJECT_A, PROJECT_B, PROJECT_A]:
            result = await client.call_tool(
                "durumari_context_save", {**payload, "project_ref": project}
            )
            assert not result.is_error
        legacy = await client.call_tool("durumari_project_status", {})
    assert json.loads(legacy.content[0].text)["project_id"] == "legacy"
    assert [call[0] for call in operations.calls] == [PROJECT_A, PROJECT_B, PROJECT_A, "legacy"]
    assert [call[2][-1] for call in operations.calls[:3]] == ["same-key"] * 3
    assert operations.project == "legacy"


@pytest.mark.anyio
async def test_legacy_adapter_rejects_supplied_reference_instead_of_silently_using_default():
    operations = RecordingView("legacy", [])
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_project_status", {"project_ref": PROJECT_A})
    assert result.is_error
    value = json.loads(result.content[0].text)
    assert value["status"] == 422
    assert value["error"]["code"] == "project_ref_not_supported"
    assert value["error"]["details"]["next_action"] == "check_bound_project_and_omit_project_ref"
    assert not operations.calls


@pytest.mark.anyio
async def test_invalid_target_is_rejected_without_echo_or_adapter_call():
    operations = TargetedOperations()
    marker = "PRIVATE_INVALID_REFERENCE"
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_project_status", {"project_ref": marker})
    assert result.is_error and marker not in result.model_dump_json()
    assert not operations.selected and not operations.calls


@pytest.mark.anyio
async def test_null_reference_keeps_legacy_default_and_stale_reference_never_falls_back():
    class StaleOperations(TargetedOperations):
        def targeted(self, ref):
            raise ContextFailure(409, {"code": "project_selection_changed"})

    operations = StaleOperations()
    async with Client(create_context_server(operations)) as client:
        legacy = await client.call_tool("durumari_project_status", {"project_ref": None})
        stale = await client.call_tool("durumari_project_status", {"project_ref": PROJECT_A})
    assert not legacy.is_error
    assert json.loads(legacy.content[0].text)["project_id"] == "legacy"
    assert stale.is_error
    assert json.loads(stale.content[0].text) == {
        "error": {"code": "project_selection_changed"},
        "status": 409,
    }
    assert len(operations.calls) == 1


@pytest.mark.anyio
async def test_recent_report_list_keeps_order_and_cursor_in_the_selected_project():
    operations = TargetedOperations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(
            "durumari_context_list",
            {
                "project_ref": PROJECT_A,
                "order": "created_desc",
                "after": PROJECT_B,
            },
        )
        assert not result.is_error
    assert operations.calls == [
        (
            PROJECT_A,
            "read",
            ("",),
            {
                "after": PROJECT_B,
                "include_deleted": "false",
                "order": "created_desc",
            },
        )
    ]
