"""A folder connection must not advertise account-only project selection."""

import json

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.credentials import AdapterError

REF = "00000000-0000-4000-8000-000000000001"
OTHER_REF = "00000000-0000-4000-8000-000000000002"


class BoundOperations:
    def __init__(self):
        self.calls = []

    def policies(self, after=None):
        self.calls.append("policies")
        return {"items": [], "project_id": "bound"}

    def read(self, suffix="", **query):
        self.calls.append((suffix, query))
        return {"items": []}

    def mutate(self, *args):
        self.calls.append(args)
        return {"status": "stored"}


@pytest.mark.anyio
async def test_bound_tools_hide_account_reference_and_policy_still_reads():
    operations = BoundOperations()
    async with Client(create_context_server(operations)) as client:
        tools = (await client.list_tools()).tools
        assert all("project_ref" not in tool.input_schema["properties"] for tool in tools)
        policy = next(tool for tool in tools if tool.name == "durumari_policy_index")
        assert "bound" in policy.description
        assert "omit project_ref on Context and Policy calls" in client.instructions
        assert "selected immutable project_ref on every Context call" not in client.instructions
        assert "original project_ref and request_key" not in client.instructions
        result = await client.call_tool("durumari_policy_index", {})
        assert not result.is_error
        assert json.loads(result.content[0].text)["project_id"] == "bound"
    assert operations.calls == ["policies"]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("durumari_policy_index", {}),
        ("durumari_context_search", {"query": "policy"}),
        ("durumari_context_save", {"request_key": "original", "title": "Result", "body": "Safe"}),
    ],
)
async def test_cached_bound_schema_never_silently_ignores_an_explicit_reference(name, payload):
    operations = BoundOperations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, {**payload, "project_ref": REF})
    assert result.is_error
    error = json.loads(result.content[0].text)["error"]
    assert error["code"] == "project_ref_not_supported"
    assert error["details"]["next_action"] == "check_bound_project_and_omit_project_ref"
    assert "durumari_project_status" in error["details"]["message"]
    assert error["details"]["retryable"] is False
    assert "account_upgrade_required" not in json.dumps(error)
    assert REF not in json.dumps(error)
    assert not operations.calls


class AccountOperations(BoundOperations):
    def policies(self, after=None):
        raise AdapterError("project_selection_required")

    def targeted(self, ref):
        if ref != REF:
            raise AdapterError("project_reference_stale")
        self.calls.append(("targeted", ref))
        view = BoundOperations()
        view.calls = self.calls
        return view


@pytest.mark.anyio
async def test_account_tools_keep_explicit_reference_and_stale_reference_rejection():
    operations = AccountOperations()
    async with Client(create_context_server(operations)) as client:
        tools = (await client.list_tools()).tools
        policy = next(tool for tool in tools if tool.name == "durumari_policy_index")
        assert "project_ref" in policy.input_schema["properties"]
        assert "On account connections" in client.instructions
        unselected = await client.call_tool("durumari_policy_index", {})
        assert unselected.is_error
        assert (
            json.loads(unselected.content[0].text)["error"]["code"] == "project_selection_required"
        )
        result = await client.call_tool("durumari_policy_index", {"project_ref": REF})
        assert not result.is_error
        rejected = await client.call_tool("durumari_policy_index", {"project_ref": OTHER_REF})
        assert rejected.is_error
        assert json.loads(rejected.content[0].text)["error"]["code"] == "project_reference_stale"
    assert operations.calls == [("targeted", REF), "policies"]
