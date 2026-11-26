"""File, review and secret tools only open a bound browser task; no content crosses."""

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server

REF = "11111111-1111-1111-1111-111111111111"
ID = "22222222-2222-2222-2222-222222222222"


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
    ("name", "arguments", "kind", "action", "target"),
    [
        ("durumari_files_browser", {}, "files", "browse", None),
        ("durumari_files_browser", {"action": "manage", "file_id": ID}, "files", "manage", ID),
        ("durumari_reviews_browser", {"action": "manage"}, "reviews", "manage", None),
        ("durumari_reviews_browser", {"review_id": ID}, "reviews", "browse", ID),
        ("durumari_secret_browser", {}, "secrets", "manage", None),
        ("durumari_secret_browser", {"secret_id": ID}, "secrets", "manage", ID),
    ],
)
async def test_intents_bind_exact_kind_action_and_target(name, arguments, kind, action, target):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(
            name, {"project_ref": REF, "request_key": "same-retry", **arguments}
        )
    assert not result.is_error
    body = {"project_ref": REF, "request_key": "same-retry", "kind": kind, "action": action}
    if target:
        body["resource_id"] = target
    assert operations.calls == [("begin", body)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("durumari_files_browser", {"data": "unused-sentinel"}),
        ("durumari_files_browser", {"revision": 1}),
        ("durumari_reviews_browser", {"action": "resolve"}),
        ("durumari_secret_browser", {"value": "unused-sentinel"}),
        ("durumari_secret_browser", {"current_password": "unused-sentinel"}),
    ],
)
async def test_content_or_unbound_revisions_never_reach_operations(name, arguments):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, {"project_ref": REF, "request_key": "k", **arguments})
    assert result.is_error and "unused-sentinel" not in result.model_dump_json()
    assert not operations.calls


@pytest.mark.anyio
async def test_tools_declare_their_read_scopes_and_secret_tool_follows_vault_access():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name, scopes in {
            "durumari_files_browser": ["files:read"],
            "durumari_reviews_browser": ["requests:read"],
            "durumari_secret_browser": ["secrets:list"],
        }.items():
            assert tools[name].meta["securitySchemes"] == [{"type": "oauth2", "scopes": scopes}]
            assert not {"value", "password", "data", "body"} & set(
                tools[name].input_schema["properties"]
            )
    closed = Operations()
    server = create_context_server(closed, secret_tools=lambda: False)
    async with Client(server) as client:
        result = await client.call_tool(
            "durumari_secret_browser", {"project_ref": REF, "request_key": "k"}
        )
    assert result.is_error and not closed.calls
