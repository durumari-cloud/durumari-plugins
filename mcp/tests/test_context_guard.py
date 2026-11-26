"""Freshness decisions consume transport observations, never model declarations."""

from dataclasses import replace

import pytest

from durumari_mcp.context_guard import CurrentSection, GuardScope, SectionRead, check_context


@pytest.fixture
def scope():
    return GuardScope("project", "connection", "codex", "session", "turn", "epoch")


@pytest.fixture
def read(scope):
    return SectionRead(scope, "context", "section", 3, "a" * 64, "current", True)


@pytest.fixture
def current():
    return CurrentSection("context", "section", 3, "a" * 64, "active")


def test_matching_current_read_allows(scope, read, current):
    assert check_context(scope, scope, {"context"}, [read], [current]) is None


@pytest.mark.parametrize("field", ["project", "connection", "client", "session", "turn", "epoch"])
def test_other_scope_cannot_satisfy_search_or_read(scope, read, current, field):
    other = replace(scope, **{field: "other"})
    assert check_context(scope, other, {"context"}, [read], [current]) == "search_required"
    assert check_context(scope, scope, {"context"}, [replace(read, scope=other)], [current]) \
        == "read_required"


def test_no_result_and_snippet_only_block(scope, read, current):
    assert check_context(scope, scope, set(), [read], [current]) == "search_required"
    assert check_context(scope, scope, {"context"}, [], [current]) == "read_required"


@pytest.mark.parametrize("change", [{"reference_mode": "pinned"}, {"complete": False}])
def test_historical_and_partial_reads_block(scope, read, current, change):
    assert check_context(scope, scope, {"context"}, [replace(read, **change)], [current]) \
        == "read_required"


def test_read_must_come_from_this_search(scope, read, current):
    assert check_context(scope, scope, {"unrelated"}, [read], [current]) == "read_required"


@pytest.mark.parametrize("change", [{"revision": 4}, {"sha256": "b" * 64},
                                    {"state": "superseded"}, {"state": "stale"}])
def test_current_server_change_blocks(scope, read, current, change):
    assert check_context(scope, scope, {"context"}, [read], [replace(current, **change)]) \
        == "current_read_required"


def test_unavailable_current_metadata_never_uses_local_read(scope, read):
    assert check_context(scope, scope, {"context"}, [read], []) == "current_read_required"


def test_one_good_read_does_not_hide_another_changed_read(scope, read, current):
    other_read = replace(read, section="other")
    other_current = replace(current, section="other", revision=4)
    assert check_context(scope, scope, {"context"}, [read, other_read],
                         [current, other_current]) == "current_read_required"


@pytest.mark.parametrize("digest", ["", "x" * 64, "a" * 63])
def test_invalid_digest_does_not_become_a_receipt(scope, read, current, digest):
    assert check_context(scope, scope, {"context"}, [replace(read, sha256=digest)],
                         [replace(current, sha256=digest)]) == "read_required"


def test_missing_host_identity_blocks(scope, read, current):
    missing = replace(scope, session="")
    assert check_context(missing, missing, {"context"},
                         [replace(read, scope=missing)], [current]) == "search_required"


def test_ambiguous_current_metadata_blocks(scope, read, current):
    assert check_context(scope, scope, {"context"}, [read],
                         [current, replace(current, revision=4)]) == "current_read_required"


@pytest.mark.parametrize("revision", [0, -1, True])
def test_invalid_revision_blocks(scope, read, current, revision):
    assert check_context(scope, scope, {"context"}, [replace(read, revision=revision)],
                         [replace(current, revision=revision)]) == "read_required"
