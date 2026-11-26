"""Graph neighbors over MCP: the related tool and relates_to writes."""

import json

import pytest
from mcp import Client
from test_cloud_protocol import RecordingOperations

from durumari_mcp.cloud_protocol import create_context_server

TARGET = "0d1d8f6d-88d2-4f3d-ba1d-f69ce2f4470c"


@pytest.mark.anyio
async def test_related_tool_is_read_only_titled_and_reads_the_related_route():
    operations = RecordingOperations()
    async with Client(create_context_server(operations)) as client:
        tool = next(
            tool
            for tool in (await client.list_tools()).tools
            if tool.name == "durumari_context_related"
        )
        assert tool.title and tool.annotations.read_only_hint
        assert not tool.annotations.destructive_hint
        assert tool.input_schema["properties"]["depth"]["maximum"] == 2
        result = await client.call_tool(
            "durumari_context_related", {"context_id": "context", "depth": 2, "limit": 3}
        )
        assert not result.is_error
        assert operations.reads[-1] == ("/context/related", {"depth": 2, "limit": 3})
        for invalid in ({"depth": 3}, {"limit": 11}, {"depth": "2"}):
            rejected = await client.call_tool(
                "durumari_context_related", {"context_id": "context", **invalid}
            )
            assert rejected.is_error


@pytest.mark.anyio
async def test_save_and_revise_pass_relates_to_and_omit_it_when_empty():
    operations = RecordingOperations()
    async with Client(create_context_server(operations)) as client:
        await client.call_tool(
            "durumari_context_save",
            {"request_key": "plain", "title": "제목", "body": "# 본문\n내용"},
        )
        assert "relates_to" not in operations.writes[-1][2]
        saved = await client.call_tool(
            "durumari_context_save",
            {
                "request_key": "linked",
                "title": "결정",
                "body": "# 결정\n채택",
                "relates_to": [{"context_id": TARGET, "relation": "supports"}],
            },
        )
        assert not saved.is_error, saved.content[0].text
        assert operations.writes[-1][2]["relates_to"] == [
            {"context_id": TARGET, "relation": "supports"}
        ]
        for invalid in (
            [{"context_id": "not-a-context"}],
            [{"context_id": TARGET, "relation": "likes"}],
            [{"context_id": TARGET}] * 9,
        ):
            rejected = await client.call_tool(
                "durumari_context_save",
                {"request_key": "bad", "title": "t", "body": "b", "relates_to": invalid},
            )
            assert rejected.is_error
        revised = await client.call_tool(
            "durumari_context_revise",
            {
                "request_key": "revise",
                "context_id": "context",
                "base_revision": 1,
                "changes": {"relates_to": [{"context_id": TARGET}]},
            },
        )
        assert not revised.is_error
        assert json.loads(revised.content[0].text)["status"] == "stored"
        assert operations.writes[-1][2]["relates_to"] == [{"context_id": TARGET}]
