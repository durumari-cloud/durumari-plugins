"""Board tools for account clients: the web's boards over MCP."""

from typing import Any

import pytest

from durumari_mcp.feature_tools import feature_for_tool

CLOUD = "11111111-1111-4111-8111-111111111111"
BOARDS = {"durumari_board_read", "durumari_board_edit", "durumari_board_delete"}


@pytest.mark.anyio
async def test_a_client_with_boards_lists_three_tools_and_forwards_actions():
    from mcp import Client as McpClient
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    calls: list[tuple[str, dict[str, Any]]] = []

    class WithBoards(SyntheticOperations):
        def boards(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
            calls.append((action, body))
            return {"items": []}

    async with McpClient(create_context_server(WithBoards())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert BOARDS <= set(tools)
        assert tools["durumari_board_read"].annotations.read_only_hint is True
        assert tools["durumari_board_delete"].annotations.destructive_hint is True
        assert tools["durumari_board_edit"].annotations.destructive_hint is False
        edit = tools["durumari_board_edit"].input_schema["properties"]["action"]
        assert set(edit["enum"]) == {"create", "operate", "batch", "upload_image", "save_context"}
        # Live cursors stay on the web screen.
        assert not any("presence" in name for name in tools)

        listed = await client.call_tool("durumari_board_read", {"cloud_id": CLOUD})
        assert not listed.is_error
        operation = {"operation_id": "op-1", "object_id": "o-1", "expected_version": 0}
        edited = await client.call_tool(
            "durumari_board_edit",
            {"cloud_id": CLOUD, "action": "operate", "board_id": "b-1", "operation": operation},
        )
        assert not edited.is_error
        removed = await client.call_tool(
            "durumari_board_delete", {"cloud_id": CLOUD, "board_id": "b-1"}
        )
        assert not removed.is_error
        refused = await client.call_tool(
            "durumari_board_read", {"cloud_id": CLOUD, "action": "presence"}
        )
        assert refused.is_error
    assert calls == [
        ("list", {"cloud_id": CLOUD}),
        ("operate", {"cloud_id": CLOUD, "board_id": "b-1", "operation": operation}),
        ("delete", {"cloud_id": CLOUD, "board_id": "b-1"}),
    ]


@pytest.mark.anyio
async def test_a_client_without_boards_lists_none():
    from mcp import Client as McpClient
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    async with McpClient(create_context_server(SyntheticOperations())) as client:
        assert not BOARDS & {tool.name for tool in (await client.list_tools()).tools}


def test_board_tools_follow_the_whiteboard_switch():
    assert {feature_for_tool(name) for name in BOARDS} == {"boards.enabled"}
