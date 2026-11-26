"""Context browser intents retain their target and expose no completion payload."""

import json

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server

REF = "11111111-1111-1111-1111-111111111111"
ID = "22222222-2222-2222-2222-222222222222"
RESOURCE_TOOLS = {
    "durumari_context_browser",
    "durumari_resource_status",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Operations:
    def __init__(self):
        self.calls = []

    def resources(self, action, body):
        self.calls.append((action, body))
        return {"task_ref": ID, "state": "pending"}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "kind", "action"),
    [
        ("durumari_context_browser", {}, "contexts", "browse"),
        ("durumari_context_browser", {"context_id": ID, "revision": 2}, "contexts", "browse"),
    ],
)
async def test_browser_intents_bind_exact_target_without_raw_payload(name, arguments, kind, action):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(
            name, {"project_ref": REF, "request_key": "same-retry", **arguments}
        )
    assert not result.is_error
    body = {"project_ref": REF, "request_key": "same-retry", "kind": kind, "action": action}
    if "context_id" in arguments:
        body.update(resource_id=ID, revision=2)
    assert operations.calls == [("begin", body)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"project_ref": "not-a-reference", "request_key": "key"},
        {"project_ref": REF, "request_key": "invalid key"},
        {"project_ref": REF, "request_key": "key", "revision": True},
        {"project_ref": REF, "request_key": "key", "revision": 1},
        {"project_ref": REF, "request_key": "key", "body": "unused-sentinel"},
        {"project_ref": REF, "request_key": "key", "result": {"state": "completed"}},
    ],
)
async def test_invalid_browser_intent_never_reaches_operations(arguments):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_context_browser", arguments)
    assert result.is_error and "unused-sentinel" not in result.model_dump_json()
    assert not operations.calls


@pytest.mark.anyio
async def test_resource_tools_have_exact_arguments_and_scopes_and_no_skills_browser():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        expected = {
            "durumari_context_browser": (
                {"project_ref", "request_key", "context_id", "revision"},
                ["context:read"],
            ),
            "durumari_resource_status": ({"task_ref", "wait_seconds"}, ["context:read"]),
        }
        for name, (arguments, scopes) in expected.items():
            assert set(tools[name].input_schema["properties"]) == arguments
            assert tools[name].meta["securitySchemes"] == [{"type": "oauth2", "scopes": scopes}]
            assert tools[name].annotations.read_only_hint is (name == "durumari_resource_status")
        # Import, files, reviews and secret tasks come from their own modules;
        # a skills browser has no handler and is never registered.
        for name in ("durumari_skills_browser",):
            assert name not in tools
            result = await client.call_tool(name, {"project_ref": REF, "request_key": "key"})
            assert result.is_error
    assert not operations.calls


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["pending", "canceled", "expired", "completed"])
async def test_resource_status_preserves_distinct_states_and_safe_selected_metadata(state):
    class Selected(Operations):
        def resources(self, action, body):
            super().resources(action, body)
            value = {"task_ref": ID, "state": state}
            if state == "completed":
                value["result"] = {
                    "kind": "contexts",
                    "action": "browse",
                    "project_id": REF,
                    "resource_count": 1,
                    "resources": [{"id": ID, "revision": 2, "sha256": "a" * 64}],
                }
            return value

    operations = Selected()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_resource_status", {"task_ref": ID})
    assert not result.is_error
    value = json.loads(result.content[0].text)
    assert value["state"] == state
    if state == "completed":
        assert value["result"]["resources"] == [{"id": ID, "revision": 2, "sha256": "a" * 64}]
    else:
        assert "result" not in value
    assert operations.calls == [("status", {"task_ref": ID, "wait_seconds": 0})]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {"task_ref": ID, "wait_seconds": True},
        {"task_ref": ID, "wait_seconds": -1},
        {"task_ref": ID, "wait_seconds": 16},
        {"task_ref": ID, "project_ref": REF},
        {"task_ref": ID, "body": "unused-sentinel"},
        {"task_ref": ID, "result": {"state": "completed"}},
    ],
)
async def test_invalid_status_never_reaches_operations(arguments):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_resource_status", arguments)
    assert result.is_error and "unused-sentinel" not in result.model_dump_json()
    assert not operations.calls


@pytest.mark.anyio
async def test_resource_capability_is_hidden_for_legacy_operations_and_stale_calls():
    async with Client(create_context_server(object())) as client:
        assert RESOURCE_TOOLS.isdisjoint(tool.name for tool in (await client.list_tools()).tools)
        for name in RESOURCE_TOOLS:
            assert (await client.call_tool(name, {})).is_error
