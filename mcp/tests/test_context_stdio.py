"""Run the official SDK against the current Context-only stdio contract."""

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client, StdioServerParameters, stdio_client


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_official_stdio_sdk_exposes_only_bounded_context_tools(mode):
    script = """
import logging
from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.project_gateway import ContextFailure
logging.disable(logging.CRITICAL)
class Fixture:
    def read(self, suffix="", **query):
        if suffix == "/conflict":
            raise ContextFailure(409, {"code":"context_conflict", "details":{"latest_revision":3}})
        return {"project_id":"synthetic", "items":[], "next_after":None}
    def status(self):
        return {"project_id":"synthetic", "ready":True}
    def operation(self, key):
        return {"request_key":key, "status":"stored", "project_id":"synthetic"}
    def mutate(self, method, suffix, body, key):
        return {"request_key":key, "status":"stored", "project_id":"synthetic"}
create_context_server(Fixture()).run(transport="stdio")
"""
    marker = "INVALID_ARGUMENT_MUST_NOT_BE_ECHOED!"
    with tempfile.TemporaryFile(mode="w+") as stderr:
        transport = stdio_client(
            # The SDK intentionally filters inherited environment variables.
            # Pass this checked-out source explicitly to the subprocess, which
            # may run in a dependency-only venv without an editable install.
            StdioServerParameters(
                command=sys.executable,
                args=["-c", script],
                env={"PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
            ),
            errlog=stderr,
        )
        async with Client(transport, mode=mode) as client:
            listed_tools = (await client.list_tools()).tools
            tools_by_name = {tool.name: tool for tool in listed_tools}
            names = set(tools_by_name)
            assert names == {
                "durumari_open_context_workspace",
                "durumari_context_explore",
                "durumari_file_upload",
                "durumari_file_upload_status",
                "durumari_context_list",
                "durumari_context_search",
                "durumari_context_read",
                "durumari_context_save",
                "durumari_context_revise",
                "durumari_context_version",
                "durumari_context_operation",
                "durumari_policy_index",
                "durumari_project_status",
                "durumari_context_related",
                "durumari_glossary_read",
                "durumari_glossary_submit",
            }
            assert "Browse metadata for catalogs or new reports" not in tools_by_name[
                "durumari_context_list"
            ].description
            assert "before Jira" not in tools_by_name["durumari_context_list"].description
            assert "external tool, plugin or skill" not in tools_by_name[
                "durumari_policy_index"
            ].description
            assert "applicability conditions" in tools_by_name["durumari_policy_index"].description
            status = await client.call_tool("durumari_project_status", {})
            assert status.structured_content is None
            assert json.loads(status.content[0].text) == {"project_id": "synthetic", "ready": True}
            invalid = await client.call_tool("durumari_context_read", {"context_id": marker})
            assert invalid.is_error
            assert marker not in invalid.model_dump_json()
            conflict = await client.call_tool("durumari_context_read", {"context_id": "conflict"})
            assert conflict.is_error
            assert json.loads(conflict.content[0].text) == {
                "error": {"code": "context_conflict", "details": {"latest_revision": 3}},
                "status": 409,
            }
        stderr.seek(0)
        assert marker not in stderr.read()


def test_server_instructions_do_not_reference_jira():
    from durumari_mcp.cloud_protocol import create_context_server

    server = create_context_server(SimpleNamespace())
    assert "jira" not in server.instructions.casefold()
