"""Merge identifiers and execution checks are data-only, never a GitLab client."""

import pytest
from pydantic import ValidationError

from durumari_mcp.merge_request import MergeRequest, require_confirmed_merge

MR = {
    "url": "https://lab.example/team/project/-/merge_requests/446",
    "iid": 446,
    "head_sha": "a" * 40,
    "target_branch": "integrate/feature",
    "action": "merge",
}


def item():
    return {
        "round": 2,
        "merge_request": MR,
        "decision": {
            "outcome": "confirmed",
            "round": 2,
            "context_id": "11111111-1111-1111-1111-111111111111",
            "context_revision": 1,
        },
    }


def live():
    return {
        "web_url": MR["url"],
        "iid": 446,
        "sha": MR["head_sha"],
        "target_branch": MR["target_branch"],
        "state": "opened",
        "draft": False,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"url": "javascript:alert(1)"},
        {"url": "https://user:password@lab.example/team/project/-/merge_requests/446"},
        {"url": MR["url"] + "?token=secret"},
        {"url": MR["url"] + "#note"},
        {"url": MR["url"].replace("446", "445")},
        {"iid": True},
        {"iid": 0},
        {"head_sha": "abc1234"},
        {"target_branch": "-main"},
        {"target_branch": "a..b"},
        {"target_branch": "main\n"},
        {"target_branch": "foo.lock/bar"},
        {"action": "deploy"},
    ],
)
def test_rejects_invalid_identifiers(change):
    with pytest.raises(ValidationError):
        MergeRequest.model_validate({**MR, **change})


def test_current_confirmed_target_is_accepted():
    assert require_confirmed_merge(item(), live()).model_dump() == MR


@pytest.mark.parametrize("source", [
    {"via": "agent"},
    {"user_instruction": {"source": "explicit_user_instruction", "human_input_verified": False}},
])
def test_delegated_record_does_not_satisfy_existing_human_merge_gate(source):
    value = item()
    value["decision"].update(source)
    with pytest.raises(ValueError, match="human_decision_required"):
        require_confirmed_merge(value, live())


@pytest.mark.parametrize(
    "change",
    [
        {"sha": "b" * 40},
        {"iid": 445},
        {"target_branch": "main"},
        {"web_url": MR["url"].replace("team", "other")},
        {"state": "closed"},
        {"draft": True},
    ],
)
def test_changed_live_target_is_refused(change):
    with pytest.raises(ValueError, match="merge_target_changed"):
        require_confirmed_merge(item(), {**live(), **change})


@pytest.mark.parametrize(
    "decision",
    [
        None,
        {"outcome": "declined", "round": 2},
        {"outcome": "deferred", "round": 2},
        {"outcome": "confirmed", "round": 1},
        {"outcome": "confirmed", "round": 2},
    ],
)
def test_unapproved_or_old_round_is_refused(decision):
    with pytest.raises(ValueError, match="human_decision_required"):
        require_confirmed_merge({**item(), "decision": decision}, live())
