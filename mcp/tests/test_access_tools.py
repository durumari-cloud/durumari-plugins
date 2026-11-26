"""Asking for view/edit: one tool for Chat and Code, approved on the web."""

import json

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server, recovery
from durumari_mcp.project_gateway import ContextFailure

NAME = "durumari_access_request_create"
SPACE = "0d1d8f6d-88d2-4f3d-ba1d-f69ce2f4470c"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Operations:
    def __init__(self, failure=None):
        self.calls, self.failure = [], failure

    def read(self, suffix="", **query):
        return {}

    def access_requests(self, body):
        self.calls.append(body)
        if self.failure is not None:
            raise self.failure
        return {"request_id": "r-1", "recipients": [], "status": "pending_human_decision"}


@pytest.mark.anyio
async def test_tool_is_listed_with_read_scope_and_forwards_only_given_fields():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        result = await client.call_tool(NAME, {"level": "edit", "project_id": SPACE})
    tool = tools[NAME]
    assert tool.annotations.read_only_hint is False
    assert tool.annotations.destructive_hint is False
    assert tool.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["context:read"]}]
    assert "approval happens on the web" in tool.description
    assert not result.is_error
    assert json.loads(result.content[0].text)["status"] == "pending_human_decision"
    assert operations.calls == [{"level": "edit", "project_id": SPACE}]


@pytest.mark.anyio
async def test_invalid_level_never_reaches_the_server():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(NAME, {"level": "manage"})
    assert result.is_error and not operations.calls


@pytest.mark.anyio
async def test_clients_without_access_requests_list_no_tool():
    class ContextOnly:
        def read(self, suffix="", **query):
            return {}

    async with Client(create_context_server(ContextOnly())) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert NAME not in names


@pytest.mark.parametrize(
    ("code", "action", "expected"),
    [
        ("access_denied", "request_edit", "level=edit"),
        ("access_denied", "request_access", "level=view"),
        ("insufficient_scope", "reconnect_with_write", "editing allowed"),
        ("access_denied", "ask_owner_to_approve_connection", "cloud owner"),
        ("human_approval_required", "approve_in_web", "Only a person decides"),
        ("project_selection_required", None, "target_project_id"),
    ],
)
def test_refusals_spell_out_the_step_the_instructions_no_longer_carry(code, action, expected):
    details = {"next_action": action} if action else {}
    shown = recovery({"code": code, "details": details})["details"]
    assert expected in shown["next_step"]
    assert shown.get("next_action") == action


def test_server_403_details_reach_the_tool_error_unchanged():
    details = {
        "cause": "connection",
        "next_action": "reconnect_with_write",
        "project_id": SPACE,
        "request_path": f"/api/v1/projects/{SPACE}/access-requests",
    }
    shown = recovery(ContextFailure(403, {"code": "insufficient_scope", "details": details}).error)
    assert {key: shown["details"][key] for key in details} == details
