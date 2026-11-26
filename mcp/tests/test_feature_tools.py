"""A feature the operator turned off leaves the local MCP servers' tool lists."""

import pytest
from mcp import Client
from test_cloud_protocol import SyntheticOperations

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.feature_tools import (
    FEATURE_TOOLS,
    HiddenFeatures,
    feature_for_tool,
    hidden_features,
    hide_unavailable,
    tool_hidden,
)

CORE = (
    "durumari_context_search",
    "durumari_context_list",
    "durumari_context_read",
    "durumari_context_save",
    "durumari_context_revise",
    "durumari_context_version",
    "durumari_context_operation",
    "durumari_context_explore",
    "durumari_context_related",
    "durumari_policy_index",
    "durumari_project_status",
)
# Core too, but listed only by clients that serve it (SyntheticOperations does not).
OPTIONAL_CORE = ("durumari_document_types",)


def test_the_release_lookup_names_only_well_formed_feature_keys():
    assert hidden_features({"features": {"hidden": ["glossary.enabled", "vault.enabled"]}}) == {
        "glossary.enabled",
        "vault.enabled",
    }
    for odd in (None, {}, {"features": []}, {"features": {"hidden": "skills"}}, [], "x"):
        assert hidden_features(odd) == frozenset()
    assert hidden_features(
        {"features": {"hidden": ["../x", 3, "UPPER.enabled", "ok.enabled"]}}
    ) == {"ok.enabled"}
    assert hidden_features({"features": {"hidden": ["a.enabled"] * 65}}) == frozenset()


def test_core_context_tools_belong_to_no_feature():
    for name in CORE + OPTIONAL_CORE:
        assert feature_for_tool(name) is None, name
        assert not tool_hidden(name, {key for _, key in FEATURE_TOOLS}), name
    assert tool_hidden("durumari_glossary_read", {"glossary.enabled"})
    assert tool_hidden("durumari_skill_list", {"skills.enabled"})
    assert tool_hidden("durumari_skill_link", {"vault.enabled"})
    assert not tool_hidden("durumari_skill_link", {"skills.enabled"})


def test_the_hidden_list_is_read_once_per_ttl_and_fails_open():
    now = [0.0]
    answers = [{"features": {"hidden": ["glossary.enabled"]}}, RuntimeError("down"), None, {}]
    calls = []

    def release():
        calls.append(now[0])
        answer = answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    hidden = HiddenFeatures(release, ttl=60, clock=lambda: now[0])
    assert hidden.current() == {"glossary.enabled"}
    assert hidden.current() == {"glossary.enabled"}
    assert calls == [0.0]
    now[0] = 61.0
    assert hidden.current() == {"glossary.enabled"}  # the read failed: keep what was known
    now[0] = 122.0
    assert hidden.current() == {"glossary.enabled"}  # no answer: keep what was known
    now[0] = 183.0
    assert hidden.current() == frozenset()  # a real answer with nothing hidden
    assert len(calls) == 4


@pytest.mark.anyio
async def test_a_hidden_feature_leaves_the_tool_list_and_a_call_gets_a_stable_code():
    server = create_context_server(SyntheticOperations())
    async with Client(server) as client:
        before = {tool.name for tool in (await client.list_tools()).tools}
    assert any(name.startswith("durumari_glossary_") for name in before)
    assert set(CORE) <= before

    state = {"hidden": ["glossary.enabled"]}
    server = create_context_server(SyntheticOperations())
    hide_unavailable(server, HiddenFeatures(lambda: {"features": state}, ttl=0, wait=5))
    async with Client(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
        assert not any(name.startswith("durumari_glossary_") for name in listed)
        assert set(CORE) <= listed
        assert listed == {name for name in before if feature_for_tool(name) != "glossary.enabled"}
        refused = await client.call_tool("durumari_glossary_read", {})
        assert refused.is_error
        assert "feature_unavailable" in refused.content[0].text
        # The operator turns it back on: no restart, the next list shows it again.
        state["hidden"] = []
        assert {tool.name for tool in (await client.list_tools()).tools} == before


@pytest.mark.anyio
async def test_a_failing_lookup_hides_nothing():
    def broken():
        raise RuntimeError("server unreachable")

    server = create_context_server(SyntheticOperations())
    hide_unavailable(server, HiddenFeatures(broken, ttl=0, wait=5))
    async with Client(server) as client:
        listed = {tool.name for tool in (await client.list_tools()).tools}
    assert any(name.startswith("durumari_glossary_") for name in listed)
