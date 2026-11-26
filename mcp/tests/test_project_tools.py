"""Conversation navigation is explicit and does not depend on host conversation IDs."""

import json

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server

CONVERSATION = "11111111-1111-1111-1111-111111111111"
PROJECT = "22222222-2222-2222-2222-222222222222"
TASK = "33333333-3333-3333-3333-333333333333"
NAVIGATION = {
    "durumari_conversation_start",
    "durumari_project_catalog",
    "durumari_project_select",
    "durumari_project_browser",
    "durumari_project_disconnect",
    "durumari_interaction_status",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class LegacyOperations:
    def status(self):
        return {"state": "active", "project_id": "legacy"}


class NavigationOperations(LegacyOperations):
    def __init__(self):
        self.calls = []

    def projects(self, action, body):
        self.calls.append((action, body))
        return {"action": action, "state": "pending" if action == "status" else "ready"}


@pytest.mark.anyio
async def test_navigation_capability_is_hidden_for_legacy_including_stale_calls():
    async with Client(create_context_server(LegacyOperations())) as client:
        assert not NAVIGATION.intersection(tool.name for tool in (await client.list_tools()).tools)
        stale = await client.call_tool("durumari_conversation_start", {"request_key": "start"})
    assert stale.is_error


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "action", "body"),
    [
        (
            "durumari_conversation_start",
            {"request_key": "start"},
            "start",
            {"request_key": "start"},
        ),
        (
            "durumari_project_catalog",
            {"conversation_ref": CONVERSATION},
            "catalog",
            {"conversation_ref": CONVERSATION},
        ),
        (
            "durumari_project_catalog",
            {
                "conversation_ref": CONVERSATION,
                "cloud_id": PROJECT,
                "query": "",
                "after": "opaque-cursor",
            },
            "catalog",
            {
                "conversation_ref": CONVERSATION,
                "cloud_id": PROJECT,
                "query": "",
                "after": "opaque-cursor",
            },
        ),
        (
            "durumari_project_select",
            {
                "conversation_ref": CONVERSATION,
                "expected_revision": 0,
                "request_key": "select",
                "project_id": PROJECT,
            },
            "select",
            {
                "conversation_ref": CONVERSATION,
                "expected_revision": 0,
                "request_key": "select",
                "project_id": PROJECT,
            },
        ),
        (
            "durumari_project_select",
            {
                "conversation_ref": CONVERSATION,
                "expected_revision": 1,
                "request_key": "link",
                "project_url": "https://durumari.kr/projects/" + PROJECT,
            },
            "select",
            {
                "conversation_ref": CONVERSATION,
                "expected_revision": 1,
                "request_key": "link",
                "project_url": "https://durumari.kr/projects/" + PROJECT,
            },
        ),
        (
            "durumari_project_browser",
            {"conversation_ref": CONVERSATION, "expected_revision": 1, "request_key": "browser"},
            "browser",
            {"conversation_ref": CONVERSATION, "expected_revision": 1, "request_key": "browser"},
        ),
        (
            "durumari_project_disconnect",
            {"conversation_ref": CONVERSATION, "expected_revision": 1, "request_key": "detach"},
            "disconnect",
            {"conversation_ref": CONVERSATION, "expected_revision": 1, "request_key": "detach"},
        ),
        (
            "durumari_interaction_status",
            {"task_ref": TASK},
            "status",
            {"task_ref": TASK, "wait_seconds": 0},
        ),
        (
            "durumari_interaction_status",
            {"task_ref": TASK, "wait_seconds": 15},
            "status",
            {"task_ref": TASK, "wait_seconds": 15},
        ),
    ],
)
async def test_navigation_dispatches_exact_opaque_references(name, arguments, action, body):
    operations = NavigationOperations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert json.loads(result.content[0].text)["action"] == action
    assert operations.calls == [(action, body)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("durumari_conversation_start", {"request_key": "invalid request key"}),
        ("durumari_conversation_start", {"request_key": "start", "host_session_id": "private"}),
        ("durumari_project_catalog", {"conversation_ref": "private-invalid-reference"}),
        ("durumari_project_catalog", {"conversation_ref": CONVERSATION, "query": "x" * 101}),
        ("durumari_project_catalog", {"conversation_ref": CONVERSATION, "after": "x" * 257}),
        ("durumari_project_catalog", {"conversation_ref": CONVERSATION, "cloud_id": "private"}),
        (
            "durumari_project_select",
            {"conversation_ref": CONVERSATION, "expected_revision": 0, "request_key": "select"},
        ),
        (
            "durumari_project_select",
            {
                "conversation_ref": CONVERSATION,
                "expected_revision": 0,
                "request_key": "select",
                "project_id": PROJECT,
                "project_url": "https://durumari.kr/projects/" + PROJECT,
            },
        ),
        (
            "durumari_project_browser",
            {"conversation_ref": CONVERSATION, "expected_revision": True, "request_key": "browser"},
        ),
        (
            "durumari_project_disconnect",
            {"conversation_ref": CONVERSATION, "expected_revision": -1, "request_key": "detach"},
        ),
        ("durumari_interaction_status", {"task_ref": TASK, "wait_seconds": 16}),
        ("durumari_interaction_status", {"task_ref": TASK, "wait_seconds": "1"}),
        ("durumari_interaction_status", {"task_ref": TASK, "wait_seconds": True}),
    ],
)
async def test_invalid_navigation_never_reaches_operations_or_echoes_private_input(name, arguments):
    operations = NavigationOperations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert "private" not in result.model_dump_json()
    assert not operations.calls


@pytest.mark.anyio
async def test_navigation_describes_explicit_conversation_and_bounded_browser_completion():
    async with Client(create_context_server(NavigationOperations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert NAVIGATION <= tools.keys()
    assert "new conversation" in tools["durumari_conversation_start"].description
    assert "browser_url" in tools["durumari_project_browser"].description
    assert "durumari_interaction_status" in tools["durumari_project_browser"].description
    for name in NAVIGATION:
        assert tools[name].annotations.read_only_hint is (
            name in {"durumari_project_catalog", "durumari_interaction_status"}
        )
        assert tools[name].annotations.idempotent_hint is (name != "durumari_conversation_start")
        assert tools[name].title
        assert tools[name].meta["securitySchemes"] == [
            {"type": "oauth2", "scopes": ["context:read"]}
        ]
    assert tools["durumari_project_disconnect"].annotations.destructive_hint is False


@pytest.mark.anyio
async def test_browser_completion_preserves_the_returned_target_and_revision():
    class CompletedOperations(NavigationOperations):
        def projects(self, action, body):
            super().projects(action, body)
            return {
                "state": "completed",
                "result": {"project_ref": PROJECT, "revision": 2, "project_id": PROJECT},
            }

    operations = CompletedOperations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_interaction_status", {"task_ref": TASK})
    assert not result.is_error
    assert json.loads(result.content[0].text) == {
        "state": "completed",
        "result": {"project_ref": PROJECT, "revision": 2, "project_id": PROJECT},
    }
    assert operations.calls == [("status", {"task_ref": TASK, "wait_seconds": 0})]


@pytest.mark.anyio
async def test_new_conversations_generate_unique_keys_and_explicit_retry_key_is_preserved():
    operations = NavigationOperations()
    async with Client(create_context_server(operations)) as client:
        first = await client.call_tool("durumari_conversation_start", {})
        second = await client.call_tool("durumari_conversation_start", {})
        retry = await client.call_tool(
            "durumari_conversation_start", {"request_key": "chat:retry-A"}
        )
    assert not first.is_error and not second.is_error and not retry.is_error
    keys = [json.loads(result.content[0].text)["request_key"] for result in [first, second, retry]]
    assert keys[0] != keys[1] and all(keys[:2])
    assert keys[2] == "chat:retry-A"
    assert operations.calls == [("start", {"request_key": key}) for key in keys]
