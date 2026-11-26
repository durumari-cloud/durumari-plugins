"""Per-message related Context injection for the UserPromptSubmit hook.

Depth off/light/normal/deep (default normal), with
(skip short follow-ups, de-duplicate per session, relevance floor, ~300 ms network
deadline, re-inject after compaction). Only titles, short excerpts, ids, revisions
and relations are injected; the model reads sections with durumari_context_read.

The contract for depth settings and the graph-neighbour slot is in
docs/prompt-context.md.
"""

import hashlib
import json
import os
import re
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from durumari_mcp import plugin_env
from durumari_mcp.credentials import AdapterError, profile_name


@dataclass(frozen=True)
class Depth:
    results: int
    neighbors: int
    hops: int
    tokens: int


# Initial values; adjust from measured token use and answer usage.
DEPTHS = {
    "off": Depth(results=0, neighbors=0, hops=0, tokens=0),
    "light": Depth(results=3, neighbors=0, hops=0, tokens=300),
    "normal": Depth(results=3, neighbors=2, hops=1, tokens=800),
    "deep": Depth(results=5, neighbors=5, hops=2, tokens=2000),
}
DEFAULT_DEPTH = "normal"
# What people type for each level; the settings file stores the English key.
DEPTH_ALIASES = {
    "끄기": "off",
    "꺼짐": "off",
    "가볍게": "light",
    "보통": "normal",
    "기본": "normal",
    "깊게": "deep",
}
ENVIRONMENT = plugin_env.CONTEXT_DEPTH
NETWORK_SECONDS = 0.3
MAX_TERMS = 3
SEEN_LIMIT = 200
SETTINGS_FILE = "settings.json"
STATE_DIRECTORY = "prompt-context"

# Short follow-ups that carry no new topic ("응", "ok", "계속").
ACKNOWLEDGEMENTS = {
    "응", "어", "네", "넵", "예", "ㅇㅇ", "ㅇㅋ", "ㄱㄱ", "좋아", "좋아요", "그래", "그래요",
    "알겠어", "알겠습니다", "고마워", "고마워요", "감사", "감사합니다", "계속", "계속해",
    "계속해줘", "진행", "진행해", "진행해줘", "해줘", "해", "다음", "아니", "아니요", "됐어",
    "ok", "okay", "yes", "yep", "no", "thanks", "thank you", "continue", "go", "go on",
    "proceed", "next", "sure", "lgtm", "y", "n",
}  # fmt: skip
# Function words and request verbs that are never distinctive search terms.
STOPWORDS = {
    "이거", "그거", "저거", "이것", "그것", "저것", "여기", "거기", "지금", "오늘", "내일", "좀",
    "그리고", "그런데", "근데", "그래서", "하지만", "그럼", "이제", "다시", "먼저", "모두", "전부",
    "어떻게", "어디", "무엇", "뭐", "왜", "언제", "누가", "어느", "얼마나", "있는", "없는",
    "있어", "없어", "같은", "같이", "대한", "대해", "관련", "통해", "위해", "때문", "정도",
    "확인", "확인해", "확인해줘", "알려줘", "알려", "보여줘", "찾아줘", "찾아", "정리해줘",
    "뭐였지", "뭐였더라", "뭐지", "뭐야", "뭔지", "뭘까", "어땠지", "어땠어", "됐지", "됐어",
    "했지", "했나", "했는데", "하기", "하기로", "어떡해", "가능해", "가능할까",
    "해줘", "해주세요", "할까", "하자", "해야", "하는", "하고", "해서", "했어", "했는지",
    "부탁", "부탁해", "만들어줘", "고쳐줘", "수정해줘", "추가해줘", "설명해줘", "읽어줘",
    "the", "a", "an", "and", "or", "but", "to", "of", "in", "on", "for", "with", "is", "are",
    "was", "be", "it", "this", "that", "these", "those", "what", "how", "why", "when",
    "where", "which", "who", "can", "could", "should", "would", "please", "me", "my", "we",
    "our", "you", "your", "do", "does", "did", "make", "fix", "add", "show", "tell", "check",
    "about", "from", "into", "there", "here", "now", "just", "let", "lets", "use", "using",
}  # fmt: skip
# Longest first: a token keeps its stem when a particle or request ending is attached.
PARTICLES = sorted(
    {
        "에서는", "에게서", "으로는", "이라는", "라는", "에서", "에게", "으로", "까지", "부터",
        "처럼", "보다", "하고", "이랑", "랑", "한테", "께서", "마다", "조차", "밖에", "이나",
        "나", "은", "는", "이", "가", "을", "를", "에", "의", "도", "만", "로", "와", "과",
        "해줘", "해줄래", "해주세요", "하자", "할까", "하기", "하는", "하면", "해서", "했어",
        "인가", "인지", "이야", "이에요", "예요", "입니다", "이다",
    },
    key=len,
    reverse=True,
)  # fmt: skip
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:/-]*[A-Za-z0-9]|[가-힣]{2,}|[A-Za-z0-9]{2,}")
_HEX_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){0,4}(?:-[0-9a-f]{12})?")
_ISSUE = re.compile(r"[A-Z][A-Z0-9]+-\d+")


def normalize_depth(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    key = DEPTH_ALIASES.get(key, key)
    return key if key in DEPTHS else None


# --- Settings: user overrides win over the project default. -----------------


def settings_path() -> Path:
    from durumari_mcp.project_outbox import state_root

    return state_root().parent / SETTINGS_FILE


def read_settings() -> dict[str, Any]:
    """User-level settings, never credentials. A damaged file reads as empty."""
    try:
        path = settings_path()
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65_536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (AdapterError, OSError, ValueError, UnicodeError):
        return {}
    return value if isinstance(value, dict) and value.get("version") == 1 else {}


def write_settings(value: dict[str, Any]) -> Path:
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise AdapterError("unsafe_settings_path")
    temporary = path.parent / (uuid4().hex + ".tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def set_depth(depth: str, project_id: str | None) -> Path:
    """Store the user's choice for one project, or as the default for all projects."""
    chosen = normalize_depth(depth)
    if chosen is None:
        raise AdapterError("invalid_context_depth")
    current = read_settings()
    section = current.get("prompt_context")
    section = dict(section) if isinstance(section, dict) else {}
    if project_id is None:
        section["depth"] = chosen
    else:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,128}", project_id):
            raise AdapterError("invalid_project_id")
        projects = section.get("projects")
        projects = dict(projects) if isinstance(projects, dict) else {}
        projects[project_id] = chosen
        section["projects"] = projects
    return write_settings({**current, "version": 1, "prompt_context": section})


def resolve_depth(project_id: str | None, project_default: Any = None) -> tuple[str, str]:
    """Return (depth, source).

    Order: environment > user per project > user default > project default (the
    server value cached at session start) > product default.
    """
    found = normalize_depth(plugin_env.get(ENVIRONMENT))
    if found:
        return found, "environment"
    section = read_settings().get("prompt_context")
    if isinstance(section, dict):
        projects = section.get("projects")
        if project_id and isinstance(projects, dict):
            found = normalize_depth(projects.get(project_id))
            if found:
                return found, "user_project"
        found = normalize_depth(section.get("depth"))
        if found:
            return found, "user"
    found = normalize_depth(project_default)
    if found:
        return found, "project"
    return DEFAULT_DEPTH, "default"


def project_default(connection: Any) -> str | None:
    """Server contract slot: ``context_injection.depth`` of /api/v2/agent/connection.

    The server does not send it yet (docs/prompt-context.md); absent means normal.
    """
    section = connection.get("context_injection") if isinstance(connection, dict) else None
    return normalize_depth(section.get("depth")) if isinstance(section, dict) else None


# --- Prompt analysis ---------------------------------------------------------------------


def _strip_particle(token: str) -> str:
    if not re.fullmatch(r"[가-힣]+", token):
        return token
    for ending in PARTICLES:
        # Object and topic markers also follow one-syllable nouns ("훅을"); the stem is
        # then too short to search and the token is dropped.
        shortest = 1 if ending in {"을", "를", "은", "는"} else 2
        if token.endswith(ending) and len(token) - len(ending) >= shortest:
            return token[: -len(ending)]
    return token


def skip_reason(prompt: Any) -> str | None:
    """Why this message needs no lookup, or None."""
    if not isinstance(prompt, str):
        return "no_prompt"
    text = unicodedata.normalize("NFC", prompt).strip()
    if not text:
        return "empty"
    if text.startswith(("/", "$", "!", "#")):
        return "command"
    compact = re.sub(r"[\s.,!?~…·'\"()\[\]-]+", " ", text.lower()).strip()
    if compact in ACKNOWLEDGEMENTS or len(re.sub(r"\s", "", compact)) < 4:
        return "short_follow_up"
    return None


def terms(prompt: str, limit: int = MAX_TERMS) -> list[str]:
    """Pick 1-3 distinctive search terms, as the search tool asks agents to do."""
    text = unicodedata.normalize("NFC", prompt)[:4000]
    # Fenced code and long pasted logs dominate counts without naming the topic.
    text = re.sub(r"```.*?```", " ", text, flags=re.S)
    # key -> [score, first position, first spelling]; repeats add weight.
    scored: dict[str, list[Any]] = {}
    for position, match in enumerate(_TOKEN.finditer(text)):
        raw = match.group(0).strip(".:/-")
        if "://" in raw or raw.lower().startswith(("http", "www.")):
            continue  # Links name a place, not a topic.
        if _HEX_ID.fullmatch(raw.lower()):
            continue  # A Context id is read directly, not searched.
        token = _strip_particle(raw)
        key = token.lower()
        if len(key) < 2 or key in STOPWORDS or key.isdigit():
            continue
        if key in scored:
            scored[key][0] += 0.5
            continue
        score = 1.0
        if _ISSUE.fullmatch(token):
            score += 3.0
        elif re.search(r"[_./]|[a-z][A-Z]", token):
            score += 2.0  # Identifiers, paths and file names.
        if re.fullmatch(r"[가-힣]+", token):
            score += min(len(token), 6) * 0.4
        else:
            score += min(len(token), 12) * 0.15
        scored[key] = [score, position, token]
    ranked = sorted(scored.values(), key=lambda entry: (-entry[0], entry[1]))
    return [str(entry[2])[:60] for entry in ranked[:limit]]


def _contains(text: Any, term: str) -> bool:
    return isinstance(text, str) and term.casefold() in text.casefold()


# Warm and cold Context nobody really read for weeks stay out of per-message injection
#; the search tool still finds them. Older servers send no tier.
DORMANT_TIERS = frozenset({"warm", "cold"})
# An issue-key term names a document by its identifier: that issue's own records are
# injected whatever their tier (found them dropped otherwise).
ISSUE_TERM = re.compile(r"[A-Z][A-Z0-9]*-[1-9][0-9]*")


def rank(results: dict[str, list[dict[str, Any]]], limit: int) -> list[dict[str, Any]]:
    """Merge per-term search pages; keep documents that pass the relevance floor.

    Search returns no score, so relevance is evidence of the match itself: a document
    found by two or more terms, or whose title names a term, passes; with a single
    term the term must appear in the title or the returned excerpt.
    """
    merged: dict[str, dict[str, Any]] = {}
    for term, items in results.items():
        for position, item in enumerate(items):
            identity = item.get("id")
            if not isinstance(identity, str) or not isinstance(item.get("title"), str):
                continue
            if item.get("tier") in DORMANT_TIERS and not (
                ISSUE_TERM.fullmatch(term)
                and re.search(
                    r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])",
                    item["title"], re.I,
                )
            ):
                continue
            entry = merged.setdefault(
                identity, {"item": item, "terms": set(), "score": 0.0, "title_hit": False}
            )
            entry["terms"].add(term)
            entry["score"] += 1.0 / (position + 1)
            if _contains(item.get("title"), term):
                entry["title_hit"] = True
                entry["score"] += 1.0
            elif _contains(item.get("snippet"), term):
                entry["snippet_hit"] = True
    relevant = [
        entry
        for entry in merged.values()
        if len(entry["terms"]) >= 2
        or entry["title_hit"]
        or (len(results) == 1 and entry.get("snippet_hit"))
    ]
    relevant.sort(key=lambda entry: (-len(entry["terms"]), -entry["score"], entry["item"]["id"]))
    return [entry["item"] for entry in relevant[:limit]]


# --- Graph neighbours: slot for track E. ----------------------


def neighbors(item: dict[str, Any], depth: Depth) -> list[dict[str, Any]]:
    """Neighbours the server already attached to a search item, if any.

    Track E returns neighbours on read; when a search item carries ``neighbors`` (or
    ``related``) entries {id, title, relation, revision, hop?}, the hook shows them.
    Without them, injection uses search results only.
    """
    if depth.neighbors <= 0:
        return []
    found = item.get("neighbors", item.get("related"))
    if not isinstance(found, list):
        return []
    result = []
    for entry in found:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        hop = entry.get("hop", 1)
        if type(hop) is not int or not 1 <= hop <= depth.hops:
            continue
        result.append(entry)
        if len(result) >= depth.neighbors:
            break
    return result


# --- Session state: de-duplication, reset after compaction. ---------------------------


def _state_path(name: str, client: str, cwd: str, session: str) -> Path:
    from durumari_mcp.hook_recovery import recovery_root

    scope = client + "\0" + str(Path(cwd).resolve()) + "\0" + session
    digest = hashlib.sha256(scope.encode()).hexdigest()
    return recovery_root() / profile_name(name) / STATE_DIRECTORY / (digest + ".json")


def load_seen(path: Path) -> dict[str, int]:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 65_536:
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    seen = value.get("seen") if isinstance(value, dict) and value.get("version") == 1 else None
    if not isinstance(seen, dict):
        return {}
    return {
        key: revision
        for key, revision in seen.items()
        if isinstance(key, str) and type(revision) is int
    }


def save_seen(path: Path, seen: dict[str, int]) -> None:
    if len(seen) > SEEN_LIMIT:
        seen = dict(list(seen.items())[-SEEN_LIMIT:])
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = path.parent / (uuid4().hex + ".tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"version": 1, "seen": seen, "updated_at": time.time()}, stream)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    except OSError:
        pass  # De-duplication is a convenience; a repeat injection is harmless.


def reset_session(name: str, client: str, cwd: str, session: str) -> None:
    """Compaction or /clear dropped earlier injections, so they may return."""
    try:
        _state_path(name, client, cwd, session).unlink(missing_ok=True)
    except (AdapterError, OSError):
        pass


# --- Server lookup within the deadline. ------------------------------------------------


def token_usable(gateway: Any) -> bool:
    """A refresh rotates credentials and must not be cut off by the hook deadline."""
    profile = gateway.profile
    expires = getattr(profile, "access_expires_at", None)
    root = getattr(gateway, "root", None)
    return not (
        getattr(profile, "refresh_uncertain", True)
        or not isinstance(expires, datetime)
        or expires <= datetime.now(UTC) + timedelta(seconds=60)
        or root is None
        or (root / (gateway.name + ".credentials") / "refresh.enc").exists()
    )


def _read(gateway: Any, path: str) -> Any:
    """Read with the profile this hook already checked.

    ``context_request`` reloads the profile from the OS store for every call, which
    costs more than the search itself on the per-message deadline. The hook loaded
    and checked it moments ago (workspace, client, removal), ``token_usable`` ruled
    out refresh, and the route is fixed, so the transport is used directly. A 401 is
    not retried here; the next message reads a rotated profile.
    """
    transport = getattr(gateway, "transport", None)
    profile = gateway.profile
    if transport is None:
        return gateway.context_request(path)
    if not getattr(profile, "context_only", False) or getattr(profile, "disabled", False):
        raise AdapterError("agent_upgrade_required")
    return transport.request(path, token=profile.access_token)


def search(
    gateway: Any, queries: list[str], limit: int, seconds: float = NETWORK_SECONDS
) -> dict[str, list[dict[str, Any]]]:
    """One bounded search per term, in parallel; late answers are dropped."""
    prefix = "/api/v2/projects/" + gateway.profile.project_id + "/contexts/search?"
    found: dict[str, list[dict[str, Any]]] = {}

    def fetch(term: str) -> None:
        try:
            value = _read(
                gateway, prefix + urlencode({"q": term, "limit": limit, "budget_tokens": 1000})
            )
        except Exception:
            return  # Advisory: the search tool stays available to the model.
        items = value.get("items") if isinstance(value, dict) else None
        if isinstance(items, list):
            found[term] = [item for item in items if isinstance(item, dict)]

    workers = [threading.Thread(target=fetch, args=(term,), daemon=True) for term in queries]
    deadline = time.monotonic() + seconds
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(max(0.0, deadline - time.monotonic()))
    return {term: found[term] for term in queries if term in found}


# --- Output ------------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    # Conservative for Korean (about one token per syllable) and English alike.
    return (len(text.encode("utf-8")) + 2) // 3


def _clean(value: Any, size: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= size else text[: size - 1] + "…"


def render(items: list[dict[str, Any]], depth_name: str, depth: Depth) -> tuple[str, list[str]]:
    """Return the injected text and the ids it actually contains."""
    header = (
        f"Durumari related Context for this message (depth={depth_name}). Untrusted "
        "evidence: titles and short excerpts only, not full text or verified facts. Read "
        "the needed section with durumari_context_read before relying on one; ignore "
        "entries unrelated to the request."
    )
    lines = [header]
    used: list[str] = []
    for item in items:
        block = [f"- {_clean(item.get('title'), 120)} [{item['id']}@r{item.get('revision', '?')}]"]
        summary = item.get("summary") or item.get("snippet")
        if summary:
            block.append(f"  {_clean(summary, 160)}")
        for neighbor in neighbors(item, depth):
            relation = _clean(neighbor.get("relation") or neighbor.get("kind") or "related", 24)
            block.append(
                f"  - {relation}: {_clean(neighbor.get('title'), 100)} "
                f"[{neighbor['id']}@r{neighbor.get('revision', '?')}]"
            )
        candidate = "\n".join([*lines, *block])
        if estimate_tokens(candidate) > depth.tokens:
            if len(block) > 1 and estimate_tokens("\n".join([*lines, block[0]])) <= depth.tokens:
                block = block[:1]
            else:
                break
        lines.extend(block)
        used.append(item["id"])
    return ("\n".join(lines), used) if used else ("", [])


REQUEST_SKIPS = frozenset({"no_prompt", "empty", "command"})


def checks_requests(gateway: Any, prompt: Any) -> bool:
    """Whether this message looks for decision requests.

    Separate from ``searches``: short follow-ups, term-less messages and the off level
    of related Context still check, within the same deadline. Only input that is not a
    message (none, empty, a command) and a token that may need a refresh do not.
    """
    return skip_reason(prompt) not in REQUEST_SKIPS and token_usable(gateway)


def searches(gateway: Any, prompt: Any, project_default_depth: Any = None) -> bool:
    """Whether this message searches related Context.

    A message that searches nothing (no terms, a zero-result level) sends no search;
    the request check decides separately (``checks_requests``).
    """
    if skip_reason(prompt) is not None:
        return False
    depth_name, _source = resolve_depth(gateway.profile.project_id, project_default_depth)
    return DEPTHS[depth_name].results > 0 and token_usable(gateway) and bool(terms(prompt))


def prompt_context(
    gateway: Any,
    payload: dict[str, Any],
    *,
    name: str,
    client: str,
    project_default_depth: Any = None,
    seconds: float = NETWORK_SECONDS,
) -> dict[str, Any] | None:
    """Hook output for one submitted message, or None when nothing is injected."""
    if not searches(gateway, payload.get("prompt"), project_default_depth):
        return None
    depth_name, _source = resolve_depth(gateway.profile.project_id, project_default_depth)
    depth = DEPTHS[depth_name]
    queries = terms(payload["prompt"])
    cwd = payload.get("cwd")
    session = str(payload.get("session_id", "unknown"))[:256]
    state = _state_path(name, client, cwd if isinstance(cwd, str) else ".", session)
    seen = load_seen(state)
    # Two extra results per term let a document found by several terms rank first.
    found = rank(search(gateway, queries, depth.results + 2, seconds), depth.results)
    # Only the top results count; a repeated question gets nothing rather than the
    # weaker matches below what it already received.
    fresh = [item for item in found if seen.get(item["id"]) != item.get("revision")]
    text, used = render(fresh, depth_name, depth)
    if not used:
        return None
    for item in fresh:
        if item["id"] in used and type(item.get("revision")) is int:
            seen.pop(item["id"], None)
            seen[item["id"]] = item["revision"]
    save_seen(state, seen)
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": text}}


# --- Update contract: add the per-message hook to an older install. -------------------


def _hook_arguments(hook: Any, client: str) -> list[str]:
    if not isinstance(hook, dict):
        return []
    if client == "claude":
        args = hook.get("args")
        return [str(arg) for arg in args] if isinstance(args, list) else []
    import shlex

    try:
        return shlex.split(str(hook.get("command", "")))
    except ValueError:
        return []


def _profile_of(arguments: list[str]) -> str | None:
    if "durumari_mcp" not in arguments or "project-hook" not in arguments:
        return None
    for index, value in enumerate(arguments[:-1]):
        if value == "--profile":
            return arguments[index + 1]
    return ""


def _switched_off(*items: Any) -> bool:
    return any(
        isinstance(item, dict) and (item.get("disabled") is True or item.get("enabled") is False)
        for item in items
    )


def _hook_marker(name: str, client: str, root: Path) -> Path:
    from durumari_mcp.hook_recovery import recovery_root

    scope = hashlib.sha256((client + "\0" + str(root.resolve())).encode()).hexdigest()
    return recovery_root() / profile_name(name) / ("prompt-hook-" + scope + ".json")


def _mark(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_text('{"version":1}', encoding="utf-8")
    except OSError:
        pass  # Without the marker the next start checks the file again.


def ensure_prompt_hook(root: Path, client: str, name: str) -> bool:
    """Write UserPromptSubmit when this profile owns SessionStart but not it yet.

    Only an untouched install changes, once: hooks a person switched off, another
    profile's hooks or a missing start hook leave the file as it is, and after the
    hook was seen or added a person's later removal stays removed. The host loads
    the new hook from its next session and keeps its own hook trust review.
    """
    if client not in {"claude", "codex"}:
        return False
    marker = _hook_marker(name, client, root)
    if marker.exists():
        return False
    from durumari_mcp.context_binding import hook_settings, json_file, write_files

    path = ".codex/hooks.json" if client == "codex" else ".claude/settings.local.json"
    current = json_file(root, path)
    hooks = current.get("hooks")
    if not isinstance(hooks, dict):
        return False
    owned: dict[str, int] = {}
    for event, groups in hooks.items():
        for group in groups if isinstance(groups, list) else []:
            for hook in group.get("hooks", []) if isinstance(group, dict) else []:
                profile = _profile_of(_hook_arguments(hook, client))
                if profile is None:
                    continue
                if profile != name or _switched_off(group, hook):
                    return False
                owned[event] = owned.get(event, 0) + 1
    if owned.get("UserPromptSubmit"):
        _mark(marker)  # Installed by connect; a later removal is the person's choice.
        return False
    if not owned.get("SessionStart"):
        return False
    path_name, contents = hook_settings(root, client, name)
    write_files(root, {path_name: contents})
    _mark(marker)
    return True


# --- CLI: `durumari-mcp prompt-context [--depth D] [--all-projects]` -------------------


def add_command(subcommands: Any) -> None:
    command = subcommands.add_parser(
        "prompt-context", help="Show or set per-message related Context depth"
    )
    command.add_argument("--workspace", default=os.getcwd())
    command.add_argument("--depth", help="off|light|normal|deep (끄기|가볍게|보통|깊게)")
    command.add_argument(
        "--all-projects", action="store_true", help="Set the user's default for every project"
    )


def _workspace_project(root: Path) -> tuple[str | None, Any]:
    """The bound project id and its cached server default, without credentials."""
    from durumari_mcp.onboarding import read_manifest

    try:
        manifest = read_manifest(root)
    except AdapterError:
        return None, None
    project_id = manifest["project_id"]
    from durumari_mcp.context_binding import bound_profile
    from durumari_mcp.project_cli import _load_snapshot

    name = bound_profile(root, manifest)
    snapshot = _load_snapshot(name, project_id) if name else None
    return project_id, (snapshot or {}).get("prompt_context_depth")


def execute(args: Any) -> None:
    root = Path(args.workspace).resolve()
    project_id, cached = _workspace_project(root)
    result: dict[str, Any] = {}
    if args.depth is not None:
        if not args.all_projects and project_id is None:
            raise AdapterError("project_manifest_required_or_use_all_projects")
        set_depth(args.depth, None if args.all_projects else project_id)
        result["changed"] = "user" if args.all_projects else "user_project"
    depth, source = resolve_depth(project_id, cached)
    print(
        json.dumps(
            {
                **result,
                "depth": depth,
                "source": source,
                "project_id": project_id,
                "levels": {
                    key: {
                        "results": value.results,
                        "neighbors": value.neighbors,
                        "hops": value.hops,
                        "max_tokens": value.tokens,
                    }
                    for key, value in DEPTHS.items()
                },
                "applies": "next message",
            },
            ensure_ascii=False,
        )
    )
