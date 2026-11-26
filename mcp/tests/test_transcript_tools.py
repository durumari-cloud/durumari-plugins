"""Meeting transcript tools: one read-only definition for server and stdio."""

import json
import re
from types import SimpleNamespace

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.context_client import ContextClient
from durumari_mcp.credentials import AdapterError

SEARCH, READ = "durumari_meeting_transcript_search", "durumari_meeting_transcript_read"


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Operations:
    def __init__(self):
        self.calls = []

    def read(self, suffix="", **query):
        return {}

    def transcripts(self, action, body):
        self.calls.append((action, body))
        return {"mode": action, "items": [], "has_more": False}


@pytest.mark.anyio
async def test_tools_are_read_only_with_context_read_and_described_states():
    async with Client(create_context_server(Operations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert {name for name in tools if "transcript" in name} == {SEARCH, READ}
    for name in (SEARCH, READ):
        tool = tools[name]
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.title
        assert tool.meta["securitySchemes"] == [{"type": "oauth2", "scopes": ["context:read"]}]
        assert "untrusted" in tool.description
    assert set(tools[SEARCH].input_schema["properties"]) == {
        "query",
        "session_id",
        "from_us",
        "to_us",
        "speaker_id",
        "after",
        "limit",
        "budget_tokens",
    }
    read = tools[READ]
    assert read.input_schema["required"] == ["session_id"]
    assert set(read.input_schema["properties"]) == {
        "session_id",
        "from_us",
        "to_us",
        "since_cursor",
        "segment_id",
        "revision",
        "after",
        "limit",
        "budget_tokens",
    }
    text = read.description
    for word in (
        "no_speech",
        "all_deleted",
        "not_yet_processed",
        "deleted_count",
        "coverage",
        "gaps",
        "processing_state",
        "failed",
        "latest revision",
        "next_since_cursor",
    ):
        assert word in text, word
    assert "processing_state" in tools[SEARCH].description
    assert "latest revision" in tools[SEARCH].description


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "action", "body"),
    [
        (SEARCH, {}, "search", {"limit": 20, "budget_tokens": 2000}),
        (
            SEARCH,
            {"query": "합성 안건", "session_id": "s-1", "from_us": 0, "to_us": 10, "after": "a_b"},
            "search",
            {
                "query": "합성 안건",
                "session_id": "s-1",
                "from_us": 0,
                "to_us": 10,
                "after": "a_b",
                "limit": 20,
                "budget_tokens": 2000,
            },
        ),
        (
            READ,
            {"session_id": "s-1", "segment_id": "w0-n0", "revision": 2, "budget_tokens": 4000},
            "read",
            {
                "session_id": "s-1",
                "segment_id": "w0-n0",
                "revision": 2,
                "limit": 100,
                "budget_tokens": 4000,
            },
        ),
        (
            READ,
            {"session_id": "s-1", "since_cursor": 7, "limit": 50},
            "read",
            {"session_id": "s-1", "since_cursor": 7, "limit": 50, "budget_tokens": 2000},
        ),
    ],
)
async def test_tools_forward_exact_bodies_without_unset_values(name, arguments, action, body):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert json.loads(result.content[0].text)["mode"] == action
    assert operations.calls == [(action, body)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        (READ, {}),
        (READ, {"session_id": ".."}),
        (READ, {"session_id": "a/b"}),
        (READ, {"session_id": "s", "from_us": -1}),
        (READ, {"session_id": "s", "revision": 0}),
        (READ, {"session_id": "s", "budget_tokens": 999}),
        (READ, {"session_id": "s", "limit": 201}),
        (READ, {"session_id": "s", "after": "not base64!"}),
        (READ, {"session_id": "s", "text": "never forwarded"}),
        (SEARCH, {"query": "x" * 201}),
        (SEARCH, {"limit": 51}),
        (SEARCH, {"budget_tokens": 16001}),
        (SEARCH, {"from_us": "0"}),
    ],
)
async def test_invalid_arguments_never_reach_operations(name, arguments):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error and not operations.calls
    assert "never forwarded" not in result.model_dump_json()


@pytest.mark.anyio
async def test_clients_without_transcripts_list_no_transcript_tools():
    class ContextOnly:
        def read(self, suffix="", **query):
            return {}

    async with Client(create_context_server(ContextOnly())) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert not any("transcript" in name for name in names)


def test_stdio_client_reads_the_bound_project_transcript_routes():
    calls = []

    def context_request(path, body=None, *, method="GET", idempotency_key=None):
        calls.append((path, body, method))
        return {"ok": True}

    client = ContextClient.__new__(ContextClient)
    client.prefix = "/api/v2/projects/p-1/contexts"
    client.gateway = SimpleNamespace(context_request=context_request)
    client.transcripts("search", {"query": "합성", "limit": 5})
    client.transcripts("read", {"session_id": "s-1", "since_cursor": 0, "limit": 10})
    assert calls == [
        (
            "/api/v2/projects/p-1/contexts/meeting-transcripts/search?q=%ED%95%A9%EC%84%B1&limit=5",
            None,
            "GET",
        ),
        (
            "/api/v2/projects/p-1/contexts/meeting-transcripts/sessions/s-1?since_cursor=0&limit=10",
            None,
            "GET",
        ),
    ]
    for action, body in (
        ("write", {}),
        ("read", {}),
        ("read", {"session_id": ".."}),
        ("read", {"session_id": "s", "text": "x"}),
        ("search", {"session": "s"}),
    ):
        with pytest.raises(AdapterError, match="invalid_transcript_operation"):
            client.transcripts(action, body)
    assert len(calls) == 2


def test_gateway_accepts_only_the_transcript_read_routes():
    from durumari_mcp import project_gateway

    source = open(project_gateway.__file__, encoding="utf-8").read()
    found = re.search(r'r"(meeting-transcripts/[^"]+)"', source)
    pattern = "/api/v2/projects/p/contexts/" + found.group(1).rstrip("|")
    for route in ("search", "sessions/s-1", "sessions/0d1d8f6d-88d2-4f3d-ba1d-f69ce2f4470c"):
        assert re.fullmatch(pattern, "/api/v2/projects/p/contexts/meeting-transcripts/" + route)
    for route in ("sessions/..", "sessions/.x", "sessions/s/windows", "commit", "sessions/"):
        assert not re.fullmatch(pattern, "/api/v2/projects/p/contexts/meeting-transcripts/" + route)
