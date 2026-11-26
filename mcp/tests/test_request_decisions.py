"""Explicit-user delegated submissions remain distinct from autonomous notes."""

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server

REQUEST = "11111111-1111-1111-1111-111111111111"
ITEM = "22222222-2222-2222-2222-222222222222"
INSTRUCTION = {"source": "explicit_user_instruction", "message_ref": "message-17"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Operations:
    def __init__(self):
        self.calls = []

    def requests(self, action, body):
        self.calls.append((action, body))
        return {"status": "stored", "applied_revision": 4}


@pytest.mark.anyio
@pytest.mark.parametrize("action", ["respond", "decide"])
async def test_formal_actions_require_and_forward_exact_explicit_instruction(action):
    operations = Operations()
    body = {
        "request_key": "instruction-17",
        "request_id": REQUEST,
        "item_id": ITEM,
        "base_revision": 3,
        "expected_round": 2,
        "user_instruction": INSTRUCTION,
        "text": "요청한 범위로 처리합니다.",
    }
    body.update({"response": "reviewed"} if action == "respond" else
                {"outcome": "deferred", "reason": "기기 검증이 남았습니다."})
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_request_" + action, body)
    assert not result.is_error
    assert operations.calls == [(action, body)]


@pytest.mark.anyio
@pytest.mark.parametrize("change", [
    {"user_instruction": None},
    {"user_instruction": {"source": "agent_recommendation", "message_ref": "x"}},
    {"user_instruction": {"source": "explicit_user_instruction", "message_ref": " "}},
    {"expected_round": 0},
    {"expected_round": True},
])
async def test_missing_or_autonomous_instruction_never_reaches_submission(change):
    operations = Operations()
    body = {
        "request_key": "instruction-17", "request_id": REQUEST, "item_id": ITEM,
        "base_revision": 3, "expected_round": 2, "user_instruction": INSTRUCTION,
        "response": "reviewed", "text": "검토했습니다.",
        **change,
    }
    if body["user_instruction"] is None:
        body.pop("user_instruction")
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_request_respond", body)
    assert result.is_error
    assert operations.calls == []


@pytest.mark.anyio
async def test_formal_tools_are_writes_and_disclose_instruction_trust_limit():
    async with Client(create_context_server(Operations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    for action in ("respond", "decide"):
        tool = tools["durumari_request_" + action]
        assert not tool.annotations.read_only_hint
        assert tool.meta["securitySchemes"] == [
            {"type": "oauth2", "scopes": ["requests:write"]}
        ]
        assert "user_instruction" in tool.input_schema["required"]
        assert "expected_round" in tool.input_schema["required"]
        assert "not cryptographic proof" in tool.description
