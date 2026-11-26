"""The web's document extras register only for a client that implements them and send
just the arguments given."""

import json

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server

PROJECT = "11111111-1111-4111-8111-111111111111"
TOOLS = {
    "durumari_context_view": (True, ["context:read"]),
    "durumari_context_mark": (False, ["context:read"]),
    "durumari_context_unarchive": (False, ["context:read", "context:write"]),
    "durumari_context_archive": (True, ["context:read"]),
    "durumari_file_list": (True, ["files:read"]),
    "durumari_file_manage": (False, ["files:read", "files:write"]),
    "durumari_graph_read": (True, ["context:read"]),
    "durumari_graph_submit": (False, ["context:read", "context:write"]),
    "durumari_document_type_read": (True, ["context:read"]),
    "durumari_document_type_submit": (False, ["context:read", "context:write"]),
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Plain:
    def read(self, suffix="", **query):
        return {}

    def mutate(self, method, suffix, body, key):
        return {}

    def operation(self, key):
        return {}

    def policies(self, after=None):
        return {}

    def status(self):
        return {}


class WithExtras(Plain):
    def __init__(self, target="bound", calls=None):
        self.target = target
        self.calls = [] if calls is None else calls

    def direct_target(self, project_id):
        return WithExtras(project_id, self.calls)

    def web_documents(self, tool, action, body):
        self.calls.append((self.target, tool, action, body))
        return {"tool": tool, "action": action}


@pytest.mark.anyio
async def test_a_client_without_the_method_lists_none_of_the_tools():
    async with Client(create_context_server(Plain())) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert not names & set(TOOLS)


@pytest.mark.anyio
async def test_tools_declare_read_only_hints_and_oauth_units():
    async with Client(create_context_server(WithExtras())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for name, (read, units) in TOOLS.items():
        tool = tools[name]
        assert tool.annotations.read_only_hint is read, name
        assert tool.meta["securitySchemes"] == [{"type": "oauth2", "scopes": units}], name
    assert tools["durumari_document_type_submit"].annotations.destructive_hint is True
    assert tools["durumari_graph_submit"].annotations.destructive_hint is True


@pytest.mark.anyio
async def test_calls_send_only_the_arguments_given_to_the_named_target():
    operations = WithExtras()
    async with Client(create_context_server(operations)) as client:
        answer = await client.call_tool(
            "durumari_context_view",
            {
                "action": "compare",
                "context_id": "c1",
                "to_revision": 3,
                "target_project_id": PROJECT,
            },
        )
        await client.call_tool(
            "durumari_document_type_submit",
            {"action": "update", "type_id": "minutes", "body": {"icon": "x"}},
        )
        await client.call_tool(
            "durumari_context_mark",
            {"action": "feed_check", "items": [{"context_id": "c1", "revision": 2}]},
        )
    assert json.loads(answer.content[0].text) == {
        "tool": "durumari_context_view",
        "action": "compare",
    }
    assert operations.calls == [
        (PROJECT, "durumari_context_view", "compare", {"context_id": "c1", "to_revision": 3}),
        (
            "bound",
            "durumari_document_type_submit",
            "update",
            {"type_id": "minutes", "body": {"icon": "x"}},
        ),
        (
            "bound",
            "durumari_context_mark",
            "feed_check",
            {"items": [{"context_id": "c1", "revision": 2}]},
        ),
    ]


@pytest.mark.anyio
async def test_sprint_close_and_progress_reach_the_client_as_given():
    operations = WithExtras()
    async with Client(create_context_server(operations)) as client:
        await client.call_tool(
            "durumari_document_type_submit",
            {
                "action": "sprint_close",
                "context_id": "ACME-3",
                "body": {"next_sprint_id": "ACME-4", "report": False},
                "request_key": "close-1",
            },
        )
        await client.call_tool(
            "durumari_document_type_read",
            {"action": "properties", "context_id": "ACME-1", "include_progress": True},
        )
    assert operations.calls == [
        (
            "bound",
            "durumari_document_type_submit",
            "sprint_close",
            {
                "context_id": "ACME-3",
                "body": {"next_sprint_id": "ACME-4", "report": False},
                "request_key": "close-1",
            },
        ),
        (
            "bound",
            "durumari_document_type_read",
            "properties",
            {"context_id": "ACME-1", "include_progress": True},
        ),
    ]


@pytest.mark.anyio
async def test_an_unknown_action_is_refused_before_the_client():
    operations = WithExtras()
    async with Client(create_context_server(operations)) as client:
        answer = await client.call_tool("durumari_graph_read", {"action": "delete"})
    assert answer.is_error
    assert operations.calls == []
