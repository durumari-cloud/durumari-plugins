"""A mandatory read cannot silently degrade into search snippets or local state."""

import hashlib
from copy import deepcopy
from dataclasses import replace

import pytest

from durumari_mcp.context_guard import GuardScope
from durumari_mcp.context_guard_read import (
    GuardBlocked,
    read_required_context,
    refresh_required_context,
)

SCOPE = GuardScope("project", "connection", "codex", "session", "turn", "epoch")
TEXT = "Fold6 → Tab S6 Lite. SSD·A23 제외. 지급 대기. 기존 Apple 팀 활용."
SHA = hashlib.sha256(TEXT.encode()).hexdigest()


def document():
    return {
        "id": "context", "project_id": "project", "revision": 3,
        "reference_mode": "current", "state": "active",
        "efficacy": {"current_revision": 3, "status": "active"},
        "sections": [{"id": "section", "sha256": SHA, "text": TEXT,
                      "start": 0, "end": len(TEXT), "total": len(TEXT)}],
        "next_after": None, "truncated": False,
    }


class Reader:
    def __init__(self):
        self.search = {"items": [{"id": "context", "section_id": "section", "revision": 3,
                                  "title": "자원", "snippet": "자원"}]}
        self.document = document()
        self.head = document()
        self.calls = []
        self.error = None

    def read(self, suffix="", **query):
        self.calls.append((suffix, query))
        if self.error:
            raise self.error
        if suffix == "/search":
            return deepcopy(self.search)
        return deepcopy(self.head if query.get("view") == "outline" else self.document)


def test_actual_current_text_not_stale_local_is_returned():
    reader = Reader()
    result = read_required_context(reader, SCOPE, ["자원"])
    assert result.texts == (TEXT,)
    assert result.reads[0].scope == SCOPE
    assert result.reads[0].sha256 == SHA
    assert [call[0] for call in reader.calls] == ["/search", "/context", "/context"]
    assert all("revision" not in query for _, query in reader.calls)


def test_no_result_blocks():
    reader = Reader()
    reader.search = {"items": []}
    with pytest.raises(GuardBlocked, match="search_required"):
        read_required_context(reader, SCOPE, ["자원"])


@pytest.mark.parametrize("error", [OSError("private network detail"), RuntimeError("auth detail")])
def test_transport_error_is_sanitized(error):
    reader = Reader()
    reader.error = error
    with pytest.raises(GuardBlocked, match="server_unavailable") as caught:
        read_required_context(reader, SCOPE, ["자원"])
    assert "detail" not in str(caught.value)
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("field,value", [("reference_mode", "pinned"),
                                         ("project_id", "other"), ("state", "superseded")])
def test_noncurrent_or_other_project_read_blocks(field, value):
    reader = Reader()
    reader.document[field] = value
    with pytest.raises(GuardBlocked):
        read_required_context(reader, SCOPE, ["자원"])


def test_search_read_race_blocks():
    reader = Reader()
    reader.document["revision"] = 4
    reader.document["efficacy"]["current_revision"] = 4
    with pytest.raises(GuardBlocked, match="current_read_required"):
        read_required_context(reader, SCOPE, ["자원"])


def test_read_action_race_blocks():
    reader = Reader()
    reader.head["revision"] = 4
    reader.head["efficacy"]["current_revision"] = 4
    with pytest.raises(GuardBlocked, match="current_read_required"):
        read_required_context(reader, SCOPE, ["자원"])


@pytest.mark.parametrize("change", [{"sha256": "a" * 64}, {"start": 1}, {"total": 9999}])
def test_incomplete_or_inconsistent_text_blocks(change):
    reader = Reader()
    reader.document["sections"][0].update(change)
    with pytest.raises(GuardBlocked, match="read_required"):
        read_required_context(reader, SCOPE, ["자원"])


def test_continuation_is_read_and_verified_before_return():
    reader = Reader()
    original = reader.read

    def paged(suffix="", **query):
        result = original(suffix, **query)
        if suffix == "/context" and query.get("view") != "outline":
            section = result["sections"][0]
            if not query.get("after"):
                section.update(text=TEXT[:10], end=10)
                result.update(next_after="cursor", truncated=True)
            else:
                section.update(text=TEXT[10:], start=10)
        return result

    reader.read = paged
    assert read_required_context(reader, SCOPE, ["자원"]).texts == (TEXT,)


def test_repeated_cursor_cannot_loop_forever():
    reader = Reader()
    reader.document.update(next_after="same", truncated=True)
    with pytest.raises(GuardBlocked, match="read_required"):
        read_required_context(reader, SCOPE, ["자원"])


def test_missing_matching_section_is_not_an_outline_read():
    reader = Reader()
    reader.search["items"][0].pop("section_id")
    with pytest.raises(GuardBlocked, match="search_required"):
        read_required_context(reader, SCOPE, ["자원"])


def test_another_chat_cannot_even_revalidate_this_proof():
    reader = Reader()
    proof = read_required_context(reader, SCOPE, ["자원"])
    reader.calls.clear()
    with pytest.raises(GuardBlocked, match="search_required"):
        refresh_required_context(reader, replace(SCOPE, session="another"), proof)
    assert reader.calls == []


def test_all_queries_are_ranked_before_the_read_limit():
    reader = Reader()
    original = reader.read

    def multi(suffix="", **query):
        if suffix == "/search":
            item = reader.search["items"][0]
            primary = {**item, "id": "primary", "title": "맥락 기기"}
            noise = [{**item, "id": f"noise-{n}", "title": "맥락"} for n in range(3)]
            return {"items": [*noise, primary] if query["q"] == "맥락" else [primary]}
        result = original(suffix, **query)
        result["id"] = suffix[1:]
        return result

    reader.read = multi
    proof = read_required_context(reader, SCOPE, ["맥락", "기기"])
    assert proof.reads[0].context == "primary"
    assert len(proof.reads) == 3


def test_unconfirmed_relevance_does_not_pass_on_matching_hash_alone():
    reader = Reader()
    reader.search["items"][0].update(title="과거 자료", snippet="관련 없는 문장")
    with pytest.raises(GuardBlocked, match="search_required"):
        read_required_context(reader, SCOPE, ["자원"])
