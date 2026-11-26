"""Human space wording keeps the installed MCP wire contract."""

import json

import pytest
from mcp import Client
from test_cloud_protocol import SyntheticOperations

from durumari_mcp.cloud_protocol import create_context_server


@pytest.mark.anyio
@pytest.mark.parametrize("account", [False, True])
async def test_space_wording_keeps_tool_names_target_arguments_and_user_content(account):
    operations = SyntheticOperations()
    operations.projects = lambda action, body: {"action": action, **body}
    if account:
        operations.targeted = lambda ref: operations
    async with Client(create_context_server(operations)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert "space" in tools["durumari_project_status"].description
        assert "spaces" in tools["durumari_project_catalog"].description
        assert "project_id" in tools["durumari_project_select"].input_schema["properties"]
        assert "project_url" in tools["durumari_project_select"].input_schema["properties"]
        assert "space_id" not in tools["durumari_project_select"].input_schema["properties"]
        properties = tools["durumari_context_save"].input_schema["properties"]
        assert ("project_ref" in properties) is account
        assert ("target_project_id" in properties) is account
        assert "current space facts" in client.instructions
        assert "durumari_project_status" in client.instructions
        assert ("retain an explicit target" in client.instructions) is account
        assert ("no account target selection is needed" in client.instructions) is not account
        result = await client.call_tool("durumari_context_save", {
            "title": "내 프로젝트", "body": "project_id /api/v2/projects/ are immutable",
            "request_key": "same-wire-contract",
        })
        assert not result.is_error
        assert json.loads(result.content[0].text)["project_id"] == "project"
        assert operations.writes[0][2]["title"] == "내 프로젝트"
        assert operations.writes[0][2]["body"] == "project_id /api/v2/projects/ are immutable"
