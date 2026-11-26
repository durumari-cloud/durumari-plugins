"""Integration wiring: the shared MCP server registers durumari_context_import."""

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server

REF = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Operations:
    def __init__(self):
        self.calls = []

    def resources(self, action, body):
        self.calls.append((action, body))
        return {"task_ref": REF, "state": "pending"}


@pytest.mark.anyio
async def test_server_lists_import_with_write_scopes_and_binds_import_manage():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        tool = tools["durumari_context_import"]
        assert tool.meta["securitySchemes"] == [
            {"type": "oauth2", "scopes": ["context:read", "context:write"]}
        ]
        assert set(tool.input_schema["properties"]) == {"project_ref", "request_key"}
        result = await client.call_tool(
            "durumari_context_import",
            {"project_ref": REF, "request_key": "k", "body": "unused-sentinel"},
        )
        assert result.is_error and not operations.calls
        result = await client.call_tool(
            "durumari_context_import", {"project_ref": REF, "request_key": "k"}
        )
    assert not result.is_error
    assert operations.calls == [("begin", {
        "project_ref": REF, "request_key": "k", "kind": "import", "action": "manage",
    })]
