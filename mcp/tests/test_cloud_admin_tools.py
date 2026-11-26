"""The web's cloud management tools for account connections."""

from typing import Any

import pytest
from mcp.types import CallToolResult, TextContent

from durumari_mcp.cloud_admin_tools import register_cloud_admin_tools

CLOUD = "11111111-1111-4111-8111-111111111111"
TOOLS = {
    "durumari_notifications",
    "durumari_chat_channels",
    "durumari_cloud_membership",
    "durumari_cloud_settings",
    "durumari_member_manage",
    "durumari_access_requests",
    "durumari_action_request_list",
    "durumari_agent_connections",
    "durumari_agent_runs",
    "durumari_billing",
}


def tools() -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    found: dict[str, Any] = {}
    options: dict[str, dict[str, Any]] = {}

    def cloud_admin(tool: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool, body))
        return {"ok": True}

    def register(fn: Any, **kwargs: Any) -> None:
        found[fn.__name__], options[fn.__name__] = fn, kwargs

    def safe(call: Any) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=str(call()))])

    register_cloud_admin_tools(cloud_admin, register, safe)
    return found, options, calls


def test_each_feature_is_one_tool_and_only_given_arguments_are_sent():
    found, options, calls = tools()
    assert set(found) == TOOLS
    assert options["durumari_action_request_list"]["read"] is True
    found["durumari_member_manage"](
        action="invite", cloud_id=CLOUD, request_key="k-1",
        invitation={"email": "new@example.invalid"},
    )
    found["durumari_notifications"]()
    found["durumari_action_request_list"]()
    assert calls == [
        ("durumari_member_manage", {"action": "invite", "cloud_id": CLOUD, "request_key": "k-1",
                                    "invitation": {"email": "new@example.invalid"}}),
        ("durumari_notifications", {"action": "list"}),
        ("durumari_action_request_list", {"action": "list"}),
    ]


@pytest.mark.anyio
async def test_an_account_client_with_cloud_admin_lists_the_tools():
    from mcp import Client as McpClient
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    sent: list[tuple[str, dict[str, Any]]] = []

    class WithCloudAdmin(SyntheticOperations):
        def cloud_admin(self, tool: str, body: dict[str, Any]) -> dict[str, Any]:
            sent.append((tool, body))
            return {"items": []}

    async with McpClient(create_context_server(WithCloudAdmin())) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert TOOLS <= listed
        result = await client.call_tool(
            "durumari_agent_runs", {"action": "list", "space_id": CLOUD}
        )
        assert not result.is_error
    assert sent == [("durumari_agent_runs", {"action": "list", "space_id": CLOUD})]
    async with McpClient(create_context_server(SyntheticOperations())) as client:
        assert not TOOLS & {tool.name for tool in (await client.list_tools()).tools}


def test_billing_names_the_proposed_change_apart_from_its_step():
    found, options, calls = tools()
    assert options["durumari_billing"]["required_scopes"] == ["billing:read", "billing:write"]
    found["durumari_billing"](
        action="propose", change="seats", target="cloud", cloud_id=CLOUD, seats=7,
        request_key="k-1",
    )
    assert calls == [
        ("durumari_billing", {"action": "propose", "change": "seats", "target": "cloud",
                              "cloud_id": CLOUD, "seats": 7, "request_key": "k-1"}),
    ]
