import json

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from test_cloud_protocol import SyntheticOperations

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.exploration import explore


class Source(SyntheticOperations):
    def __init__(self):
        super().__init__()
        self.reads = []

    def read(self, suffix="", **query):
        self.reads.append((suffix, query))
        if suffix == "/search":
            return {"items": [{"id": "seed"}], "truncated": True}
        return {"id": suffix[1:], "project_id": "project", "revision": 2,
                "title": "Original", "sections": [{"text": "original body"}],
                "neighbors": [{"id": "neighbour", "revision": 3, "title": "Evidence",
                               "relation": "supports", "direction": "in"}]}


def test_exploration_records_actual_reads_and_relations_without_reading_neighbours():
    source = Source()
    result = explore(source, ["seed", "seed"], "")
    assert [event["type"] for event in result["events"]] == ["read", "discovered", "relation"]
    assert result["events"][0]["document"]["sections"][0]["text"] == "original body"
    assert result["events"][1]["document"]["sections"] == []
    assert result["events"][2]["edge"] == {
        "from": "neighbour", "to": "seed", "kind": "document", "label": "supports"}
    assert len(source.reads) == 1
    assert all(event["scope"] == result["scope"] for event in result["events"])
    assert explore(source, ["seed"], "")["scope"] != result["scope"]


def test_query_discovery_does_not_manufacture_links_between_search_hits():
    result = explore(Source(), [], "연결")
    assert result["partial"] is True
    assert result["events"][0]["type"] == "read"
    with pytest.raises(ToolError):
        explore(Source(), [], "")


def test_project_change_is_rejected_instead_of_returning_partial_other_project_data():
    class Changed(Source):
        def read(self, suffix="", **query):
            result = super().read(suffix, **query)
            if suffix == "/other":
                result["project_id"] = "other-project"
            return result

    with pytest.raises(ToolError, match="scope_changed"):
        explore(Changed(), ["seed", "other"], "")


def test_mixed_project_search_keeps_only_first_project_and_marks_partial():
    class Changed(Source):
        def read(self, suffix="", **query):
            if suffix == "/search":
                return {"items": [{"id": "seed"}, {"id": "other"}]}
            result = super().read(suffix, **query)
            if suffix == "/other":
                result["project_id"] = "other-project"
            return result

    result = explore(Changed(), [], "연결")
    assert result["partial"] is True
    assert all(event.get("document", {}).get("project_id", "project") == "project"
               for event in result["events"])


@pytest.mark.anyio
async def test_actual_sdk_exposes_graph_entry_and_original_payload_once():
    async with Client(create_context_server(Source())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert tools["durumari_context_explore"].meta["ui"]["resourceUri"]
        result = await client.call_tool("durumari_context_explore", {"query": "연결"})
        assert not result.is_error
        source = json.loads(result.content[0].text)
        assert source["scope"]["project_id"] == "project"
        assert result.model_dump_json().count("original body") == 1


def test_repeated_search_hits_of_one_document_read_and_emit_it_once():
    class Sections(Source):
        def read(self, suffix="", **query):
            if suffix == "/search":
                self.reads.append((suffix, query))
                # Several matching sections of the same document are one document.
                return {"items": [{"id": "seed"}, {"id": "seed"}, {"id": "seed"}]}
            return super().read(suffix, **query)

    source = Sections()
    result = explore(source, [], "연결")
    reads = [event for event in result["events"] if event["type"] == "read"]
    assert [event["document"]["id"] for event in reads] == ["seed"]
    assert [read for read in source.reads if read[0] != "/search"] == [
        ("/seed", {"budget_tokens": 1500, "view": "document"})
    ]
    assert len(result["events"]) == 3  # one read, one discovery, one actual relation
    assert result["partial"] is False


def test_discovered_documents_are_never_linked_to_each_other():
    class Crowd(Source):
        def read(self, suffix="", **query):
            if suffix == "/search":
                return {"items": [{"id": "seed"}]}
            value = super().read(suffix, **query)
            value["neighbors"] = [
                {"id": f"n{index}", "revision": 1, "title": f"관련 {index}"} for index in range(3)
            ]
            return value

    result = explore(Crowd(), [], "연결")
    assert [event["type"] for event in result["events"]] == [
        "read", "discovered", "discovered", "discovered"]
