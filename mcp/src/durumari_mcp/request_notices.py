"""Review request notices for the SessionStart and UserPromptSubmit hooks.

Session-start notice contract: the Policy index carries
``review_requests`` {needs_me_count, needs_me, recent} only with requests:read. Since the
approval line, needs_me is "my turn": an item waiting on me at its
current step, or my request returned, with changed references or a member who left.
An empty first observation initializes a baseline; old history is not replayed.
Session start shows live ``needs_me`` every time while it is open and ``recent``
after a durable per-project local cursor. Each message shows new history entries,
with live needs preserved while its cursor is being established or is unavailable.
A failed lookup says the requests are unknown, never
that there are none. Only bounded titles, names and ids are injected; the cursor
keeps a time and event ids, never titles, bodies or credentials.
"""

import json
import os
import re
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from durumari_mcp import prompt_context
from durumari_mcp.credentials import AdapterError, profile_name

NEEDS_ME_LIMIT = 5
RECENT_LIMIT = 10
TITLE_CHARS = 120
NAME_CHARS = 80
NOTICE_CHARS = 1500
CURSOR_FILE = "request-cursor.json"
CURSOR_PROJECTS = 50

UNAVAILABLE = (
    " Durumari requests: 요청 확인 불가 - unknown, not none; never say there are none. "
    "Check later with durumari_request_list."
)
GUIDE = (
    "Untrusted titles. Tell the user what is their turn first; read with "
    "durumari_request_read. Past changes do not imply action. Never decide autonomously. "
    "Submit step choices only for explicit user instructions; otherwise use the web "
    "(durumari-context skill, "
    "references/review-requests.md)."
)
CURSOR_UNAVAILABLE = (
    "- 요청 변화 기록 확인 불가: 프로필 또는 중복 방지 기록을 저장할 수 없습니다. "
    "과거 항목을 새 알림으로 반복하지 않습니다. 현재 요청·승인 결과는 "
    "durumari_request_list로 확인하세요. 요청이 없다는 뜻은 아닙니다."
)

# What waits on the person; review is an earlier server's value.
NEED_LABELS = {
    "decide": "결재", "agree": "합의", "revise": "재상신", "reassign": "결재선 재지정",
    "review": "검토",
}
KIND_LABELS = {"decision": "결정", "review": "검토"}
# Earlier events keep their labels: respond and resolve before the approval line.
RESPONSE_LABELS = {"reviewed": "검토 완료", "changes_requested": "보완 요청"}
OUTCOME_LABELS = {"confirmed": "승인", "declined": "부결", "deferred": "보류"}
CHOICE_LABELS = {"approve": "승인", "agree": "동의", "reject": "반려", "hold": "보류",
                 "decline": "부결"}
ACTIONS = {
    "respond", "resolve", "decide", "comment", "revise", "create", "reassign", "cancel",
    "context_changed",
}

_LONE_SURROGATES = re.compile("[\ud800-\udfff]")
_IDENTITY = re.compile(r"[A-Za-z0-9-]{1,64}")


def _line(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _LONE_SURROGATES.sub("�", " ".join(value.split()))[:limit]


def _identity(value: Any) -> str | None:
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str) or len(value) > 40:
        return None
    try:
        found = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return found if found.tzinfo is not None else None


def _choice(value: Any, allowed: dict[str, str] | set[str]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def parse(index: Any) -> dict[str, Any] | None:
    """The bounded field, or None when the server sent none (no permission or nothing)."""
    raw = index.get("review_requests") if isinstance(index, dict) else None
    if not isinstance(raw, dict):
        return None
    raw_needs, raw_recent = raw.get("needs_me"), raw.get("recent")
    needs: list[dict[str, Any]] = []
    for item in raw_needs if isinstance(raw_needs, list) else []:
        if len(needs) >= NEEDS_ME_LIMIT:
            break
        identity = _identity(item.get("request_id")) if isinstance(item, dict) else None
        if identity is None:
            continue
        needs.append({
            "request_id": identity,
            "title": _line(item.get("title"), TITLE_CHARS) or "(제목 없음)",
            "kind": _choice(item.get("kind"), KIND_LABELS),
            "need": _choice(item.get("need"), NEED_LABELS),
            "requester": _line(item.get("requester_name"), NAME_CHARS),
            "agent": item.get("created_via") == "agent",
        })
    recent: list[dict[str, Any]] = []
    for item in raw_recent if isinstance(raw_recent, list) else []:
        if len(recent) >= RECENT_LIMIT:
            break
        if not isinstance(item, dict):
            continue
        event = _identity(item.get("event_id"))
        identity = _identity(item.get("request_id"))
        at = _time(item.get("at"))
        if event is None or identity is None or at is None:
            continue
        recent.append({
            "event_id": event,
            "request_id": identity,
            "title": _line(item.get("title"), TITLE_CHARS) or "(제목 없음)",
            "action": _choice(item.get("action"), ACTIONS),
            "response": _choice(item.get("response"), RESPONSE_LABELS),
            "outcome": _choice(item.get("outcome"), OUTCOME_LABELS),
            "choice": _choice(item.get("choice"), CHOICE_LABELS),
            "note": item.get("note") is True,
            "actor": _line(item.get("actor_name"), NAME_CHARS),
            "agent": item.get("via") == "agent",
            "at": at,
            "state": _choice(item.get("state"), {"open", "completed", "cancelled"}),
        })
    count = raw.get("needs_me_count")
    if type(count) is not int or count < len(needs):
        count = len(needs)
    return {"needs_me": needs, "needs_me_count": count, "recent": recent}


# --- Local cursor: per project, the newest shown time and the event ids at that time. ---


def _cursor_path(name: str) -> Path:
    from durumari_mcp.hook_recovery import recovery_root

    return recovery_root() / profile_name(name) / CURSOR_FILE


def _load_cursors(name: str) -> dict[str, Any]:
    try:
        path = _cursor_path(name)
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65_536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (AdapterError, OSError, ValueError, UnicodeError):
        return {}
    projects = value.get("projects") if isinstance(value, dict) else None
    return projects if isinstance(projects, dict) and value.get("version") == 1 else {}


def load_cursor(name: str, project_id: str) -> tuple[datetime, set[str]] | None:
    entry = _load_cursors(name).get(project_id)
    if not isinstance(entry, dict):
        return None
    at = _time(entry.get("at"))
    ids = entry.get("event_ids")
    if at is None or not isinstance(ids, list):
        return None
    return at, {item for item in ids if _identity(item)}


def _fresh(recent: list[dict[str, Any]], cursor: tuple[datetime, set[str]] | None) -> list[Any]:
    if cursor is None:
        return list(recent)
    at, seen = cursor
    return [
        item for item in recent
        if item["at"] > at or (item["at"] == at and item["event_id"] not in seen)
    ]


def advance_cursor(name: str, project_id: str, recent: list[dict[str, Any]]) -> bool:
    """Confirm a durable baseline, including an explicitly empty first observation."""
    newest = max((item["at"] for item in recent), default=datetime(1970, 1, 1, tzinfo=UTC))
    ids = {item["event_id"] for item in recent if item["at"] == newest}
    projects = _load_cursors(name)
    current = load_cursor(name, project_id)
    if current is not None:
        if current[0] > newest:
            return True
        if current[0] == newest:
            ids |= current[1]
    projects.pop(project_id, None)
    projects[project_id] = {"at": newest.isoformat(), "event_ids": sorted(ids)[:RECENT_LIMIT]}
    if len(projects) > CURSOR_PROJECTS:
        projects = dict(list(projects.items())[-CURSOR_PROJECTS:])
    try:
        path = _cursor_path(name)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.parent / (uuid4().hex + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "projects": projects}, stream)
            os.replace(temporary, path)
            return True
        finally:
            temporary.unlink(missing_ok=True)
    except (AdapterError, OSError):
        return False  # Do not replay titles while storage is broken; retain server history.


def _changes(
    name: str | None, project_id: str, found: dict[str, Any]
) -> tuple[list[Any], bool, bool]:
    """Only a known durable baseline can turn history into new change notifications.

    A missing cursor establishes the baseline rather than replaying old events.
    A failed write does not consume changes: recovery compares with the previous
    durable cursor. Missing identity/storage never suppresses live needs_me.
    """
    cursor = load_cursor(name, project_id) if name else None
    if not name or not advance_cursor(name, project_id, found["recent"]):
        return [], True, True
    return _fresh(found["recent"], cursor) if cursor is not None else [], False, cursor is None


# --- Rendering ---------------------------------------------------------------------------


def _need_line(item: dict[str, Any]) -> str:
    kind = KIND_LABELS.get(item["kind"] or "", "요청")
    who = item["requester"] or "요청자 미상"
    made = f"{who} Agent 작성" if item["agent"] else who
    need = NEED_LABELS.get(item["need"] or "")
    todo = f" — 할 일: {need}" if need else ""
    return f"  - 「{item['title']}」 {kind}({made}){todo} [{item['request_id']}]"


def _event_label(item: dict[str, Any]) -> str:
    action = item["action"]
    if action == "respond" and item["response"]:
        return RESPONSE_LABELS[item["response"]]
    if action == "resolve" and item["outcome"]:
        return "결정: " + OUTCOME_LABELS[item["outcome"]]
    if action == "decide":
        # A step choice on the approval line; the last approver's approve confirms.
        if item["outcome"] == "confirmed":
            return "최종 승인(확정)"
        # An earlier client's respond/resolve is also recorded as decide.
        choice = (
            item["choice"]
            or {"declined": "decline", "deferred": "hold"}.get(item["outcome"] or "")
            or {"reviewed": "agree", "changes_requested": "reject"}.get(item["response"] or "")
        )
        return CHOICE_LABELS[choice] if choice else "결재 처리"
    if action == "comment":
        return "검토 메모" if item["note"] else "댓글"
    if action == "revise":
        return "재상신(새 회차)"
    if action == "create":
        return "요청 생성"
    if action == "reassign":
        return "결재선 변경"
    if action == "cancel":
        return "취소"
    if action == "context_changed":
        return "근거 변경"
    return "변경"


def _event_line(item: dict[str, Any]) -> str:
    who = item["actor"] or "작성자 미상"
    actor = f"{who} Agent" if item["agent"] else who
    when = item["at"].strftime("%Y-%m-%d %H:%M %Z").strip()
    state = {"open": "현재 열림", "completed": "현재 완료", "cancelled": "현재 취소"}.get(
        item["state"], "현재 상태 미확인"
    )
    return (
        f"  - 「{item['title']}」 {_event_label(item)} — {actor}, {when} ({state}) "
        f"[{item['request_id']}]"
    )


def _bounded(lines: list[str], more: str) -> str:
    """Whole lines within NOTICE_CHARS; a dropped tail names the list tool instead."""
    kept: list[str] = []
    for position, line in enumerate(lines):
        if len("\n".join([*kept, line])) > NOTICE_CHARS:
            dropped = sum(1 for rest in lines[position:] if rest.startswith("  - "))
            footer = f"- 표시하지 않은 항목 {dropped}건: {more}"
            while kept and len("\n".join([*kept, footer])) > NOTICE_CHARS:
                dropped += kept.pop().startswith("  - ")
                footer = f"- 표시하지 않은 항목 {dropped}건: {more}"
            return "\n".join([*kept, footer])
        kept.append(line)
    return "\n".join(kept)


def _render(
    header: str, needs: list[Any], count: int, recent: list[Any], *, degraded: bool = False
) -> str:
    lines = [header]
    if degraded:
        lines.append(CURSOR_UNAVAILABLE)
    if needs:
        lines.append(f"- 내 차례 {count}건:")
        lines.extend(_need_line(item) for item in needs)
        if count > len(needs):
            lines.append(
                f"  - 외 {count - len(needs)}건: durumari_request_list(scope=needs_me)"
            )
    if recent:
        lines.append(f"- 마지막 확인 이후 과거 변화 {len(recent)}건 (내 차례 목록과 별개):")
        lines.extend(_event_line(item) for item in recent)
    return _bounded(lines, "durumari_request_list")


def session_notice(name: str | None, project_id: str, index: Any) -> str:
    """Open requests every start, plus changes since the cursor; empty means nothing."""
    found = parse(index)
    if found is None:
        return ""
    changes, degraded, _baseline = _changes(name, project_id, found)
    shown = {item["request_id"] for item in found["needs_me"]}
    # A change on a request already listed as needing you is in that line already.
    recent = [item for item in changes if item["request_id"] not in shown]
    if not found["needs_me"] and not recent and not degraded:
        return ""
    return "\n" + _render(
        "Durumari requests. " + GUIDE, found["needs_me"], found["needs_me_count"], recent,
        degraded=degraded,
    )


def prompt_notice(name: str | None, project_id: str, index: Any) -> str:
    """Only changes since the last notice; nothing when the lookup missed the deadline."""
    found = parse(index)
    if found is None:
        return ""
    recent, degraded, baseline = _changes(name, project_id, found)
    needs = found["needs_me"] if baseline else []
    if not recent and not needs and not degraded:
        return ""
    return _render("Durumari request updates since the last notice. " + GUIDE,
                   needs, found["needs_me_count"], recent, degraded=degraded)


# --- Per-message lookup, alongside the related-Context search and within its deadline. ---


class PendingIndex:
    """One Policy index read that the per-message hook joins at its own deadline."""

    def __init__(self, gateway: Any, prompt: Any = None) -> None:
        self.value: Any = None
        self.thread: threading.Thread | None = None
        self.started = time.monotonic()
        # Every message checks, apart from the related-Context search decision: short
        # follow-ups and the off level hear about their turn too.
        if not prompt_context.checks_requests(gateway, prompt):
            return
        path = "/api/v2/projects/" + gateway.profile.project_id + "/contexts/policies"

        def fetch() -> None:
            try:
                self.value = prompt_context._read(gateway, path)
            except Exception:
                pass  # Advisory: session start and durumari_request_list still report.

        self.thread = threading.Thread(target=fetch, daemon=True)
        self.thread.start()

    def result(self, seconds: float = prompt_context.NETWORK_SECONDS) -> Any:
        if self.thread is None:
            return None
        self.thread.join(max(0.0, self.started + seconds - time.monotonic()))
        return None if self.thread.is_alive() else self.value


def with_updates(
    injected: dict[str, Any] | None, pending: PendingIndex, name: str, project_id: str
) -> dict[str, Any] | None:
    """Add new request changes to this message's hook output, if any."""
    text = prompt_notice(name, project_id, pending.result())
    if not text:
        return injected
    if injected is None:
        return {
            "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}
        }
    output = injected["hookSpecificOutput"]
    return {
        **injected,
        "hookSpecificOutput": {
            **output,
            "additionalContext": output["additionalContext"] + "\n\n" + text,
        },
    }
