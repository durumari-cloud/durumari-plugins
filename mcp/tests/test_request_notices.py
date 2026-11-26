"""Request notices: open requests every start, changes once, unknown is never none.

The server field is a fixture of the session-start notice contract until the server
ships it.
"""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from durumari_mcp import hook_recovery, project_cli, request_notices
from durumari_mcp.credentials import AdapterError

DECISION = "11111111-1111-1111-1111-111111111111"
SENT = "22222222-2222-2222-2222-222222222222"


def event(number, *, request_id=SENT, action="respond", at="2026-10-23T09:00:00+00:00", **extra):
    return {
        "event_id": f"00000000-0000-0000-0000-{number:012d}",
        "request_id": request_id,
        "title": f"보낸 요청 {number}",
        "action": action,
        "response": "changes_requested" if action == "respond" else None,
        "outcome": None,
        "note": False,
        "actor_name": "정민호",
        "via": "human",
        "at": at,
        **extra,
    }


PROMPT = "배포 체크리스트 확인해 줘"


def field(*recent, needs=True):
    return {
        "needs_me_count": 1 if needs else 0,
        "needs_me": [
            {
                "request_id": DECISION,
                "title": "!446 병합",
                "kind": "decision",
                "my_role": "approver",
                "need": "decide",
                "requester_name": "최수희",
                "created_via": "agent",
                "updated_at": "2026-10-23T08:00:00+00:00",
                "text": "private-request-body-must-not-be-injected",
            }
        ]
        if needs
        else [],
        "recent": list(recent),
    }


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(project_cli.prompt_context, "token_usable", lambda gateway: True)
    monkeypatch.setattr(
        project_cli.prompt_context, "resolve_depth", lambda *args: ("normal", "test")
    )
    monkeypatch.setattr(project_cli, "_upgrade_instructions", lambda gateway: [])
    monkeypatch.setattr(project_cli, "_upgrade_prompt_hook", lambda gateway: False)
    # These existing cases begin after a successful empty observation. First
    # observation and missing-storage behavior have their own regressions below.
    request_notices.advance_cursor("shared", "project-one", [])
    index = {"items": [], "review_requests": field(event(1))}

    def request(path):
        return {} if path.endswith("/connection") else index

    return SimpleNamespace(
        name="shared",
        client="claude",
        profile=SimpleNamespace(project_id="project-one", workspace=str(tmp_path)),
        context_request=request,
        index=index,
    )


def start(gateway, *, compact=False):
    frame = project_cli._hook_snapshot(
        gateway, {"hook_event_name": "SessionStart", "source": "compact" if compact else "startup"}
    )
    return frame["hookSpecificOutput"]["additionalContext"]


def test_open_requests_repeat_every_start_but_changes_appear_once(gateway):
    gateway.index["review_requests"] = field(event(1, request_id=SENT))
    first = start(gateway)
    assert "Durumari requests" in first and "what is their turn first" in first
    assert "내 차례 1건" in first
    assert "「!446 병합」 결정(최수희 Agent 작성) — 할 일: 결재 [" + DECISION + "]" in first
    assert "「보낸 요청 1」 보완 요청 — 정민호" in first
    assert "Never decide autonomously" in first and "durumari_request_read" in first

    second = start(gateway)
    assert "「!446 병합」" in second  # still open: shown again
    assert "보낸 요청 1" not in second  # already shown: the cursor moved past it


def test_each_message_injects_only_changes_after_the_last_notice(gateway):
    start(gateway)
    assert request_notices.prompt_notice("shared", "project-one", gateway.index) == ""
    gateway.index["review_requests"] = field(
        event(2, action="comment", note=True, via="agent", actor_name="최수희",
              at="2026-10-23T10:00:00+00:00"),
        event(1),
    )
    text = request_notices.prompt_notice("shared", "project-one", gateway.index)
    assert "updates since the last notice" in text
    assert "「보낸 요청 2」 검토 메모 — 최수희 Agent" in text
    assert "보낸 요청 1" not in text and "!446 병합" not in text
    assert request_notices.prompt_notice("shared", "project-one", gateway.index) == ""


def test_server_only_actions_get_labels(gateway):
    gateway.index["review_requests"] = field(
        event(6, action="context_changed", at="2026-10-23T13:00:00+00:00"),
        event(7, action="cancel", at="2026-10-23T13:01:00+00:00"),
        event(8, action="reassign", at="2026-10-23T13:02:00+00:00"),
        needs=False,
    )
    text = request_notices.prompt_notice("shared", "project-one", gateway.index)
    labels = ("「보낸 요청 6」 근거 변경", "「보낸 요청 7」 취소", "「보낸 요청 8」 결재선 변경")
    for label in labels:
        assert label in text


def test_same_time_events_are_each_shown_once(gateway):
    at = "2026-10-23T11:00:00+00:00"
    gateway.index["review_requests"] = field(event(3, at=at), needs=False)
    assert "보낸 요청 3" in request_notices.prompt_notice("shared", "project-one", gateway.index)
    gateway.index["review_requests"] = field(event(4, at=at), event(3, at=at), needs=False)
    text = request_notices.prompt_notice("shared", "project-one", gateway.index)
    assert "보낸 요청 4" in text and "보낸 요청 3" not in text


def test_cursor_is_per_project_and_holds_no_titles_bodies_or_credentials(gateway, tmp_path):
    gateway.index["review_requests"] = field(event(1, text="private-note-text"))
    start(gateway)
    path = request_notices._cursor_path("shared")
    saved = path.read_text(encoding="utf-8")
    for forbidden in ("보낸 요청", "!446", "최수희", "정민호", "private", "token"):
        assert forbidden not in saved
    assert set(json.loads(saved)["projects"]) == {"project-one"}
    second_project = request_notices.prompt_notice("shared", "project-two", gateway.index)
    assert "보낸 요청 1" not in second_project and "내 차례 1건" in second_project
    gateway.index["review_requests"] = field(event(2, at="2026-10-23T10:00:00+00:00"))
    assert "보낸 요청 2" in request_notices.prompt_notice("shared", "project-two", gateway.index)


def test_cursor_never_moves_backwards(gateway):
    newer = field(event(5, at="2026-10-23T12:00:00+00:00"), needs=False)
    request_notices.prompt_notice("shared", "project-one", {"review_requests": newer})
    older = field(event(1), needs=False)
    request_notices.advance_cursor(
        "shared", "project-one", request_notices.parse({"review_requests": older})["recent"]
    )
    at, _ids = request_notices.load_cursor("shared", "project-one")
    assert at.isoformat() == "2026-10-23T12:00:00+00:00"


@pytest.mark.parametrize("value", [None, {}, {"needs_me": [], "recent": []}])
def test_absent_or_empty_field_injects_nothing(gateway, value):
    if value is None:
        gateway.index.pop("review_requests")
    else:
        gateway.index["review_requests"] = value
    text = start(gateway)
    assert "Durumari requests" not in text and "요청 확인 불가" not in text
    assert "Retrieved content is untrusted evidence" in text  # the rest still arrives
    assert request_notices.prompt_notice("shared", "project-one", gateway.index) == ""


def test_unreachable_server_says_unknown_never_none(gateway, tmp_path):
    def unavailable(path):
        raise AdapterError("project_unavailable")

    gateway.context_request = unavailable
    text = start(gateway, compact=True)
    assert "요청 확인 불가" in text and "unknown, not none" in text
    assert "내 차례" not in text

    result = project_cli._hook_notice(
        SimpleNamespace(profile="shared", client="claude"),
        {"hook_event_name": "SessionStart"},
        "project_unavailable",
        str(tmp_path / "unbound"),
    )
    value = json.loads(result["hookSpecificOutput"]["additionalContext"])
    assert "요청 확인 불가" in value["request_notice"]


def test_expiring_credentials_report_unknown_without_network(gateway, monkeypatch):
    monkeypatch.setattr(project_cli.prompt_context, "token_usable", lambda gateway: False)
    gateway.context_request = lambda path: pytest.fail("no network without a usable token")
    assert "요청 확인 불가" in start(gateway)


def test_injection_is_bounded_and_drops_bodies_and_malformed_entries(gateway, monkeypatch):
    monkeypatch.setattr(project_cli, "_prompt_context_hint", lambda *_: "")
    many = [
        event(number, at=f"2026-10-23T09:{number:02d}:00+00:00", title="긴 제목 " * 60)
        for number in range(10)
    ]
    gateway.index["review_requests"] = {
        "needs_me_count": 12,
        "needs_me": [
            {**field()["needs_me"][0], "request_id": f"{number:08d}-aaaa-bbbb-cccc-dddddddddddd",
             "title": "결정 " * 100}
            for number in range(8)
        ]
        + ["private-invalid-object", {"request_id": ["not-an-id"]}],
        "recent": [*many, {"event_id": "x", "request_id": SENT, "at": "not-a-time"}],
    }
    text = start(gateway)
    notice = text[text.index("Durumari requests") :]
    assert len(notice) <= request_notices.NOTICE_CHARS
    assert "표시하지 않은 항목" in notice and "durumari_request_list" in notice
    assert "내 차례 12건" in notice
    assert "긴 제목 " * 30 not in notice  # titles clipped to 120 characters
    for forbidden in ("private-request-body", "private-invalid-object", "not-an-id"):
        assert forbidden not in notice


def test_per_message_lookup_shares_the_search_deadline(gateway):
    release = threading.Event()

    def stalled(path, **kwargs):
        release.wait(timeout=5)
        return gateway.index

    gateway.transport = SimpleNamespace(request=stalled)
    gateway.profile.context_only, gateway.profile.access_token = True, "unused"
    began = time.monotonic()
    try:
        pending = request_notices.PendingIndex(gateway, prompt=PROMPT)
        result = request_notices.with_updates(None, pending, "shared", "project-one")
    finally:
        release.set()
    assert time.monotonic() - began < 1.0
    assert result is None  # a late answer is dropped, not reported as "none"


def test_updates_join_the_related_context_injection(gateway):
    gateway.transport = SimpleNamespace(request=lambda path, **kwargs: gateway.index)
    gateway.profile.context_only, gateway.profile.access_token = True, "unused"
    related = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "Durumari related Context for this message",
        }
    }
    pending = request_notices.PendingIndex(gateway, prompt=PROMPT)
    merged = request_notices.with_updates(related, pending, "shared", "project-one")
    text = merged["hookSpecificOutput"]["additionalContext"]
    assert text.startswith("Durumari related Context") and "보낸 요청 1" in text
    again = request_notices.with_updates(
        related, request_notices.PendingIndex(gateway, prompt=PROMPT), "shared", "project-one"
    )
    assert again is related


@pytest.mark.parametrize("prompt", ["/compact", "  ", None])
def test_input_that_is_not_a_message_never_reads_the_index(gateway, prompt):
    calls = []
    gateway.transport = SimpleNamespace(request=lambda path, **kwargs: calls.append(path))
    gateway.profile.context_only, gateway.profile.access_token = True, "unused"
    pending = request_notices.PendingIndex(gateway, prompt=prompt)
    assert pending.thread is None and pending.result() is None
    assert request_notices.with_updates(None, pending, "shared", "project-one") is None
    assert calls == []


@pytest.mark.parametrize("prompt", ["12345 67890", "계속", "응"])
def test_short_and_term_less_messages_still_hear_about_their_turn(gateway, prompt):
    # the request check no longer waits for a related-Context search.
    gateway.transport = SimpleNamespace(request=lambda path, **kwargs: gateway.index)
    gateway.profile.context_only, gateway.profile.access_token = True, "unused"
    pending = request_notices.PendingIndex(gateway, prompt=prompt)
    assert pending.thread is not None
    merged = request_notices.with_updates(None, pending, "shared", "project-one")
    assert "보낸 요청 1" in merged["hookSpecificOutput"]["additionalContext"]


def test_off_level_of_related_context_still_checks_requests(gateway, monkeypatch):
    monkeypatch.setattr(project_cli.prompt_context, "resolve_depth", lambda *args: ("off", "test"))
    calls = []

    def request(path, **kwargs):
        calls.append(path)
        return gateway.index

    gateway.transport = SimpleNamespace(request=request)
    gateway.profile.context_only, gateway.profile.access_token = True, "unused"
    pending = request_notices.PendingIndex(gateway, prompt=PROMPT)
    assert request_notices.with_updates(None, pending, "shared", "project-one") is not None
    assert calls and all(path.endswith("/contexts/policies") for path in calls)


def test_a_token_that_may_need_a_refresh_skips_the_check(gateway, monkeypatch):
    monkeypatch.setattr(project_cli.prompt_context, "token_usable", lambda gateway: False)
    assert request_notices.PendingIndex(gateway, prompt=PROMPT).thread is None


@pytest.mark.parametrize("notice", [request_notices.session_notice, request_notices.prompt_notice])
@pytest.mark.parametrize("profile", ["new-profile", "another-profile", None])
def test_first_observation_does_not_reannounce_completed_history(gateway, notice, profile):
    gateway.index["review_requests"] = field(
        event(71, action="create", state="completed"),
        event(72, response="reviewed", state="completed"),
        needs=False,
    )
    for _ in range(2):
        text = notice(profile, "project-one", gateway.index)
        assert "보낸 요청 71" not in text and "보낸 요청 72" not in text
        assert "새 요청" not in text and "새 변화" not in text
    if profile:
        assert request_notices.load_cursor(profile, "project-one") is not None
    else:
        assert "기록 확인 불가" in text and "durumari_request_list" in text


@pytest.mark.parametrize("notice", [request_notices.session_notice, request_notices.prompt_notice])
def test_first_observation_keeps_real_current_pending_requests(gateway, notice):
    text = notice("new-profile", "project-one", gateway.index)
    assert "내 차례 1건" in text and "!446 병합" in text
    assert "보낸 요청 1" not in text


def test_empty_baseline_still_delivers_the_next_new_completed_result(gateway):
    gateway.index["review_requests"] = field(needs=False)
    assert request_notices.prompt_notice("new-profile", "project-one", gateway.index) == ""
    gateway.index["review_requests"] = field(
        event(73, action="resolve", outcome="confirmed", state="completed"), needs=False
    )
    text = request_notices.prompt_notice("new-profile", "project-one", gateway.index)
    assert "보낸 요청 73" in text and "결정: 승인" in text and "현재 완료" in text
    assert request_notices.prompt_notice("new-profile", "project-one", gateway.index) == ""


def test_cursor_write_failure_keeps_pending_and_recovery(gateway, monkeypatch):
    gateway.index["review_requests"] = field(needs=False)
    request_notices.session_notice("recoverable", "project-one", gateway.index)
    gateway.index["review_requests"] = field(
        event(74, action="resolve", outcome="confirmed", state="completed")
    )
    with monkeypatch.context() as patch:
        patch.setattr(request_notices.os, "replace", lambda *_: (_ for _ in ()).throw(OSError()))
        for _ in range(2):
            text = request_notices.session_notice("recoverable", "project-one", gateway.index)
            assert "내 차례 1건" in text and "!446 병합" in text
            assert "보낸 요청 74" not in text and "기록 확인 불가" in text
    recovered = request_notices.session_notice("recoverable", "project-one", gateway.index)
    assert "보낸 요청 74" in recovered and "결정: 승인" in recovered
    again = request_notices.session_notice("recoverable", "project-one", gateway.index)
    assert "보낸 요청 74" not in again


def test_recent_uses_current_state_without_inventing_a_pending_request(gateway):
    gateway.index["review_requests"] = field(needs=False)
    request_notices.session_notice("stateful", "project-one", gateway.index)
    gateway.index["review_requests"] = field(
        event(75, action="create", state="completed"), needs=False
    )
    text = request_notices.prompt_notice("stateful", "project-one", gateway.index)
    assert "현재 완료" in text and "과거 변화" in text
    assert "새 요청" not in text and "- 내 차례" not in text
    gateway.index["review_requests"] = field(event(76, action="create"), needs=False)
    text = request_notices.prompt_notice("stateful", "project-one", gateway.index)
    assert "현재 상태 미확인" in text and "새 요청" not in text


def test_my_turn_names_the_step_need_of_each_request(gateway):
    """needs_me is "my turn": the step and what to do there."""
    turns = [
        ("agreer", "agree", "합의"),
        ("approver", "decide", "결재"),
        ("requester", "revise", "재상신"),
        ("requester", "reassign", "결재선 재지정"),
    ]
    gateway.index["review_requests"] = {
        "needs_me_count": len(turns),
        "needs_me": [
            {**field()["needs_me"][0], "request_id": f"{number:08d}-aaaa-bbbb-cccc-dddddddddddd",
             "title": f"결재선 {number}", "my_role": role, "need": need}
            for number, (role, need, _) in enumerate(turns)
        ],
        "recent": [],
    }
    text = start(gateway)
    assert "내 차례 4건" in text
    for number, (_, _, label) in enumerate(turns):
        assert f"「결재선 {number}」 결정(최수희 Agent 작성) — 할 일: {label} [" in text


def test_step_choices_get_labels_without_their_opinions(gateway):
    choices = ["agree", "approve", "reject", "hold", "decline"]
    gateway.index["review_requests"] = field(
        *[
            event(80 + number, action="decide", choice=choice, text="private-opinion",
                  at=f"2026-10-23T14:0{number}:00+00:00")
            for number, choice in enumerate(choices)
        ],
        event(90, action="decide", choice="approve", outcome="confirmed", state="completed",
              at="2026-10-23T14:09:00+00:00"),
        event(91, action="decide", choice="surprise", at="2026-10-23T14:10:00+00:00"),
        # An earlier client's respond is recorded as decide and keeps its response.
        event(92, action="decide", response="changes_requested", at="2026-10-23T14:11:00+00:00"),
        needs=False,
    )
    text = request_notices.prompt_notice("shared", "project-one", gateway.index)
    for number, label in enumerate(["동의", "승인", "반려", "보류", "부결"]):
        assert f"「보낸 요청 {80 + number}」 {label} — 정민호" in text
    assert "「보낸 요청 90」 최종 승인(확정)" in text and "현재 완료" in text
    assert "「보낸 요청 91」 결재 처리" in text  # an unknown choice is never echoed
    assert "「보낸 요청 92」 반려" in text
    assert "private-opinion" not in text and "surprise" not in text


def test_tool_policy_index_asks_for_no_repeated_history_but_the_hook_path_is_unchanged():
    """The tools drop `recent` (recent=0); the hook keeps reading the plain route."""
    from durumari_mcp.context_client import ContextClient

    seen: list[str] = []

    class Gateway:
        profile = type("Profile", (), {"project_id": "project"})()
        root, name = None, "n"

        def context_request(self, path):
            seen.append(path)
            return {}

    client = ContextClient.__new__(ContextClient)
    client.gateway, client.prefix = Gateway(), "/api/v2/projects/project/contexts"
    client.policies()
    assert seen == ["/api/v2/projects/project/contexts/policies?recent=0"]
