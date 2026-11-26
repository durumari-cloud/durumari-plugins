"""Chat inbox Agent tools: writes only on a person's request."""

from typing import Any

import pytest

from durumari_mcp.chat_inbox_tools import payload, register_chat_inbox_tools
from durumari_mcp.credentials import AdapterError


class Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def chat_inbox(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((action, body))
        return {"ok": True}


def tools() -> tuple[dict[str, Any], dict[str, dict[str, Any]], Client]:
    client = Client()
    found: dict[str, Any] = {}
    options: dict[str, dict[str, Any]] = {}

    def register(fn: Any, **kwargs: Any) -> None:
        found[fn.__name__], options[fn.__name__] = fn, kwargs

    register_chat_inbox_tools(lambda _ref, _target=None: client, register, lambda call: call())
    return found, options, client


def test_four_tools_with_reads_marked_and_writes_needing_context_write():
    found, options, _client = tools()
    assert set(found) == {
        "durumari_chat_inbox_list",
        "durumari_chat_inbox_read",
        "durumari_chat_inbox_organize",
        "durumari_chat_inbox_discard",
    }
    assert options["durumari_chat_inbox_list"]["read"] is True
    assert options["durumari_chat_inbox_organize"]["required_scopes"] == [
        "context:read",
        "context:write",
    ]
    assert options["durumari_chat_inbox_discard"]["destructive"] is True


def test_writes_are_refused_before_sending_unless_a_person_asked():
    found, _options, client = tools()
    with pytest.raises(AdapterError, match="person_request_required"):
        found["durumari_chat_inbox_organize"](
            item_id="item-1", base_revision=1, person_requested=False
        )
    with pytest.raises(AdapterError, match="person_request_required"):
        found["durumari_chat_inbox_discard"](
            item_id="item-1", base_revision=1, person_requested=False
        )
    assert client.calls == []
    found["durumari_chat_inbox_organize"](
        item_id="item-1", base_revision=2, person_requested=True, title="정리한 제목"
    )
    ((action, body),) = client.calls
    assert action == "organize" and body["person_requested"] is True
    assert body["item_id"] == "item-1" and body["body"]["base_revision"] == 2


def test_reads_send_no_write_flag_and_secret_shaped_text_never_leaves():
    found, _options, client = tools()
    found["durumari_chat_inbox_list"](state="open", limit=5)
    assert client.calls == [("list", {"body": {"state": "open", "after": "", "limit": 5}})]
    with pytest.raises(AdapterError, match="sensitive_context_rejected"):
        payload(
            "organize",
            item_id="item-1",
            body={"body": "Authorization: Bearer sk-" + "SyntheticCredentialOnly123456789"},
            person_requested=True,
        )
    with pytest.raises(AdapterError, match="invalid_chat_inbox_item"):
        payload("read")


@pytest.mark.anyio
async def test_a_server_whose_client_has_an_inbox_lists_the_four_tools():
    # the tools were written but never registered by any server.
    from mcp import Client as McpClient
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    class WithInbox(SyntheticOperations):
        def chat_inbox(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
            return {"action": action, "items": []}

    names = {
        "durumari_chat_inbox_list",
        "durumari_chat_inbox_read",
        "durumari_chat_inbox_organize",
        "durumari_chat_inbox_discard",
    }
    async with McpClient(create_context_server(WithInbox())) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert names <= listed
        listed_items = await client.call_tool("durumari_chat_inbox_list", {})
        assert not listed_items.is_error
    async with McpClient(create_context_server(SyntheticOperations())) as client:
        assert not names & {tool.name for tool in (await client.list_tools()).tools}


@pytest.mark.anyio
async def test_an_account_client_with_places_lists_cloud_and_space_tools():
    # MCP gets the REST API's cloud/space create, rename, restore and reads.
    from mcp import Client as McpClient
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    calls: list[tuple[str, dict[str, Any]]] = []

    class WithPlaces(SyntheticOperations):
        def places(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
            calls.append((action, body))
            return {"id": "c" * 36, "name": body.get("name", "")}

    async with McpClient(create_context_server(WithPlaces())) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert {
            "durumari_cloud_create",
            "durumari_cloud_rename",
            "durumari_cloud_restore",
            "durumari_space_create",
            "durumari_space_rename",
            "durumari_space_restore",
            "durumari_cloud_members",
            "durumari_cloud_permissions",
            "durumari_action_request",
            "durumari_action_status",
        } <= listed
        assert not any("delete" in name and "cloud" in name for name in listed)
        made = await client.call_tool(
            "durumari_cloud_create", {"request_key": "k-1", "name": "Synthetic"}
        )
        assert not made.is_error
    assert calls == [
        ("create_cloud", {"request_key": "k-1", "name": "Synthetic", "default_language": "ko"})
    ]


@pytest.mark.anyio
async def test_transcript_corrections_need_a_person_request_and_never_confirm():
    # fixing a stored transcript does not need the recording device.
    from mcp import Client as McpClient
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    sent: list[tuple[str, dict[str, Any]]] = []

    class WithCorrections(SyntheticOperations):
        def transcript_corrections(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
            sent.append((action, body))
            return {"revision": 2}

    async with McpClient(create_context_server(WithCorrections())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        name = tools["durumari_meeting_transcript_name_speaker"]
        assert name.input_schema["properties"]["name_status"]["enum"] == [
            "suggested",
            "unidentified",
        ]
        refused = await client.call_tool(
            "durumari_meeting_transcript_correct",
            {
                "session_id": "s-1",
                "segment_id": "g-1",
                "expected_revision": 1,
                "request_key": "k-1",
                "person_requested": False,
                "text": "x",
            },
        )
        assert refused.is_error and sent == []
        fixed = await client.call_tool(
            "durumari_meeting_transcript_correct",
            {
                "session_id": "s-1",
                "segment_id": "g-1",
                "expected_revision": 1,
                "request_key": "k-1",
                "person_requested": True,
                "text": "x",
            },
        )
        assert not fixed.is_error
    assert sent == [
        (
            "correct_segment",
            {
                "session_id": "s-1",
                "item_id": "g-1",
                "request_key": "k-1",
                "person_requested": True,
                "body": {"expected_revision": 1, "text": "x"},
            },
        )
    ]
