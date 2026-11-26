"""HTTP MCP and native adapters expose one bounded, non-approving proposal contract."""

import json
from types import SimpleNamespace

import pytest
from mcp import Client

from durumari_mcp.account_client import AccountContextClient
from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.context_client import ContextClient
from durumari_mcp.credentials import AdapterError

ID = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Operations:
    def __init__(self):
        self.calls = []

    def proposals(self, action, body):
        self.calls.append((action, body))
        return {"action": action, "state": "needs_decision"}


@pytest.mark.anyio
async def test_proposal_tools_expose_no_approval_and_keep_recovery_key():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        names = {name for name in tools if name.startswith("durumari_proposal_")}
        assert names == {
            "durumari_proposal_" + a for a in ("create", "list", "read", "apply", "status")
        }
        assert tools["durumari_proposal_status"].annotations.read_only_hint
        # Applying rewrites the target Context; creating only proposes (OpenAI scan, 1135).
        assert tools["durumari_proposal_apply"].annotations.destructive_hint is True
        assert tools["durumari_proposal_create"].annotations.destructive_hint is False
        result = await client.call_tool(
            "durumari_proposal_apply", {"proposal_id": ID, "request_key": "fixed-apply"}
        )
        assert not result.is_error
        assert json.loads(result.content[0].text)["state"] == "needs_decision"
        assert operations.calls == [("apply", {"proposal_id": ID, "request_key": "fixed-apply"})]
        bad = await client.call_tool(
            "durumari_proposal_apply",
            {"proposal_id": ID, "request_key": "fixed-apply", "approved": True},
        )
        assert bad.is_error
        assert len(operations.calls) == 1


@pytest.mark.parametrize("account", [False, True])
def test_native_adapters_preserve_idempotency_headers_and_project_reference(account):
    calls = []
    if account:
        client = object.__new__(AccountContextClient)
        client.request = lambda path, **kwargs: calls.append((path, kwargs)) or {}
    else:
        client = object.__new__(ContextClient)
        client.gateway = SimpleNamespace(
            context_request=lambda path, body, **kwargs: (
                calls.append((path, {"body": body, **kwargs})) or {}
            )
        )
    body = {"project_ref": ID, "proposal_id": ID, "request_key": "fixed-key"}
    client.proposals("apply", body)
    path, kwargs = calls[0]
    assert path == "/api/v2/agent/proposals/apply"
    assert kwargs["body"] == body
    assert kwargs["idempotency_key"] == "fixed-key"
    with pytest.raises(AdapterError, match="invalid_proposal_operation"):
        client.proposals("approve", body)
    assert len(calls) == 1


@pytest.mark.anyio
@pytest.mark.parametrize(
    "reviewers", [None, [{"user_id": "22222222-2222-2222-2222-222222222222", "required": True}]]
)
async def test_proposal_reviewers_are_optional_on_an_approval_line(reviewers):
    """Omitted reviewers leave the approver alone on a one-step line."""
    operations = Operations()
    arguments = {
        "context_id": ID,
        "base_revision": 2,
        "sections": [{"id": "s1", "before_sha256": "a" * 64, "text": "새 문장"}],
        "evidence": [{"context_id": ID, "revision": 2}],
        "reason": "근거가 바뀌었습니다.",
        "approver_id": ID,
        "request_key": "proposal-1",
    }
    if reviewers is not None:
        arguments["reviewers"] = reviewers
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_proposal_create", arguments)
    assert not result.is_error
    ((action, body),) = operations.calls
    assert action == "create" and body["request_key"] == "proposal-1"
    if reviewers is None:
        assert "reviewers" not in body["proposal"]
    else:
        assert body["proposal"]["reviewers"] == reviewers
