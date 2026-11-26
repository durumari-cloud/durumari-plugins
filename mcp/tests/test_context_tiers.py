"""Context lifecycle tiers in the plugin (PROJ-554)."""

import pytest
from mcp import Client
from test_context_targeting import RecordingView

from durumari_mcp import prompt_context
from durumari_mcp.cloud_protocol import create_context_server


@pytest.fixture
def anyio_backend():
    return "asyncio"


def item(identity, tier=None):
    value = {"id": identity, "revision": 1, "title": "플러그인 규칙 " + identity}
    if tier is not None:
        value["tier"] = tier
    return value


def test_injection_skips_warm_and_cold_but_keeps_items_from_older_servers():
    found = prompt_context.rank(
        {"플러그인": [item("hot", "hot"), item("warm", "warm"), item("cold", "cold"), item("old")]},
        5,
    )
    assert [entry["id"] for entry in found] == ["hot", "old"]


def test_an_issue_key_term_keeps_that_issues_own_dormant_record():
    own = {**item("own", "warm"), "title": "작업 기록 — PROJ-9 정리"}
    other = {**item("other", "warm"), "title": "PROJ-10 다른 일"}
    prefix = {**item("prefix", "warm"), "title": "PROJ-95 다른 이슈"}
    found = prompt_context.rank({"PROJ-9": [own, other, prefix]}, 5)
    assert [entry["id"] for entry in found] == ["own"]


@pytest.mark.anyio
@pytest.mark.parametrize(("arguments", "sent"), [({}, None), ({"include_archived": True}, "true")])
async def test_search_tool_asks_for_archived_context_only_when_requested(arguments, sent):
    calls = []
    async with Client(create_context_server(RecordingView("project", calls))) as client:
        result = await client.call_tool("durumari_context_search", {"query": "결정", **arguments})
    assert not result.is_error
    assert calls == [
        (
            "project",
            "read",
            ("/search",),
            {"q": "결정", "after": 0, "budget_tokens": 1500, "include_archived": sent},
        )
    ]
