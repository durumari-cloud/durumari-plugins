"""Mandatory transport reads for the Context gate; no cache or snippet fallback.

This is not a relevance oracle or a host installation. Search selects bounded
candidate sections; their text stays untrusted evidence. Host integration must
bind the scope to actual hook events and enforce failures before dependent work.
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from durumari_mcp.context_guard import CurrentSection, GuardScope, SectionRead, check_context
from durumari_mcp.prompt_context import rank

MAX_SECTIONS = 3
MAX_PAGES = 12
MAX_CHARS = 12000
IDENTIFIER = re.compile(r"[A-Za-z0-9-]{1,64}")


class Reader(Protocol):
    def read(self, suffix: str = "", **query: Any) -> dict[str, Any]: ...


class GuardBlocked(Exception):
    """Only fixed, safe reason codes cross the hook boundary."""


@dataclass(frozen=True)
class RequiredContext:
    scope: GuardScope
    searched_contexts: frozenset[str]
    reads: tuple[SectionRead, ...]
    texts: tuple[str, ...]


def _active(value: dict[str, Any], scope: GuardScope, revision: int) -> bool:
    efficacy = value.get("efficacy")
    return (
        value.get("project_id") == scope.project
        and value.get("reference_mode") == "current"
        and value.get("state") == "active"
        and type(value.get("revision")) is int
        and value["revision"] == revision
        and isinstance(efficacy, dict)
        and efficacy.get("status") == "active"
        and type(efficacy.get("current_revision")) is int
        and efficacy["current_revision"] == revision
    )


def _section_text(
    reader: Reader, scope: GuardScope, context: str, section: str, revision: int,
) -> tuple[SectionRead, str]:
    parts: list[str] = []
    cursor = ""
    seen: set[str] = set()
    digest = ""
    total = None
    offset = 0
    for _ in range(MAX_PAGES):
        page = reader.read("/" + context, section_id=section, after=cursor, budget_tokens=2000)
        if page.get("id") != context or not _active(page, scope, revision):
            raise GuardBlocked("current_read_required")
        sections = page.get("sections")
        if not isinstance(sections, list) or len(sections) != 1:
            raise GuardBlocked("read_required")
        item = sections[0]
        if not isinstance(item, dict) or item.get("id") != section:
            raise GuardBlocked("read_required")
        text = item.get("text")
        if not isinstance(text, str) or item.get("start") != offset:
            raise GuardBlocked("read_required")
        if not digest:
            digest = item.get("sha256", "")
            total = item.get("total")
        if (
            not isinstance(digest, str)
            or re.fullmatch(r"[a-f0-9]{64}", digest) is None
            or item.get("sha256") != digest
            or type(total) is not int
            or not 0 < total <= MAX_CHARS
            or item.get("total") != total
            or item.get("end") != offset + len(text)
        ):
            raise GuardBlocked("read_required")
        offset += len(text)
        if offset > total:
            raise GuardBlocked("read_required")
        parts.append(text)
        next_cursor = page.get("next_after")
        if not next_cursor:
            body = "".join(parts)
            if (page.get("truncated") is not False or offset != total
                    or hashlib.sha256(body.encode("utf-8")).hexdigest() != digest):
                raise GuardBlocked("read_required")
            return SectionRead(scope, context, section, revision, digest, "current", True), body
        if not isinstance(next_cursor, str) or next_cursor in seen or len(next_cursor) > 500:
            raise GuardBlocked("read_required")
        seen.add(next_cursor)
        cursor = next_cursor
    raise GuardBlocked("read_required")


def refresh_required_context(reader: Reader, scope: GuardScope, proof: RequiredContext) -> None:
    """Recheck effective current metadata immediately before a dependent action."""
    try:
        if proof.scope != scope:
            raise GuardBlocked("search_required")
        heads: list[CurrentSection] = []
        for read in proof.reads:
            value = reader.read("/" + read.context, view="outline", budget_tokens=2000)
            if value.get("id") != read.context or not _active(value, scope, read.revision):
                raise GuardBlocked("current_read_required")
            sections = value.get("sections")
            if not isinstance(sections, list):
                raise GuardBlocked("current_read_required")
            matches = [s for s in sections if isinstance(s, dict) and s.get("id") == read.section]
            if len(matches) != 1:
                raise GuardBlocked("current_read_required")
            heads.append(CurrentSection(read.context, read.section, value["revision"],
                                        matches[0].get("sha256", ""), value["state"]))
        reason = check_context(scope, proof.scope, proof.searched_contexts, proof.reads, heads)
        if reason:
            raise GuardBlocked(reason)
    except GuardBlocked:
        raise
    except Exception:
        raise GuardBlocked("server_unavailable") from None


def read_required_context(
    reader: Reader, scope: GuardScope, queries: Sequence[str],
) -> RequiredContext:
    """Search, fully read selected current sections, then revalidate their heads.

    Only metadata and full selected text are returned in memory. Callers must not
    persist the text as a freshness receipt. A transport failure or uncertain
    response blocks instead of silently consulting local history.
    """
    try:
        if not 1 <= len(queries) <= 3 or any(not 2 <= len(q) <= 200 for q in queries):
            raise GuardBlocked("search_required")
        found: dict[str, list[dict[str, Any]]] = {}
        revisions: dict[tuple[str, str], int] = {}
        for query in queries:
            value = reader.read("/search", q=query, budget_tokens=1500)
            items = value.get("items")
            if not isinstance(items, list):
                raise GuardBlocked("search_required")
            if len(items) > 25:
                raise GuardBlocked("search_required")
            found[query] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                context, section, revision = (item.get(k) for k in ("id", "section_id", "revision"))
                if (not isinstance(context, str) or IDENTIFIER.fullmatch(context) is None
                        or not isinstance(section, str) or IDENTIFIER.fullmatch(section) is None
                        or type(revision) is not int or revision < 1):
                    continue
                key = context, section
                if key in revisions and revisions[key] != revision:
                    raise GuardBlocked("current_read_required")
                revisions[key] = revision
                # Rank section matches across every query before applying the
                # read bound. Filling the bound from the first query discards
                # stronger evidence returned by subsequent queries.
                found[query].append({**item, "context_id": context,
                                     "id": context + ":" + section})
        selected = {
            (item["context_id"], item["section_id"]): item["revision"]
            for item in rank(found, MAX_SECTIONS)
        }
        if not selected:
            raise GuardBlocked("search_required")
        reads, texts = [], []
        for (context, section), revision in selected.items():
            read, body = _section_text(reader, scope, context, section, revision)
            reads.append(read)
            texts.append(body)
        if sum(map(len, texts)) > MAX_CHARS:
            raise GuardBlocked("read_required")
        proof = RequiredContext(
            scope, frozenset(c for c, _ in selected), tuple(reads), tuple(texts)
        )
        refresh_required_context(reader, scope, proof)
        return proof
    except GuardBlocked:
        raise
    except Exception:
        raise GuardBlocked("server_unavailable") from None
