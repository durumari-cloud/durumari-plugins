"""Pure freshness decisions for a future host-enforced Context gate.

Inputs must come from the authenticated transport and host event identity. These
values are not signed receipts and must never be accepted from model tool arguments
as proof. This module alone does not install hooks or protect a host. The caller
must refresh current metadata before every dependent action, and fail closed when
that request fails. No prompt, Context body or credential belongs in these values.
"""

import re
from collections.abc import Sequence, Set
from dataclasses import astuple, dataclass
from typing import Literal


@dataclass(frozen=True)
class GuardScope:
    project: str
    connection: str
    client: str
    session: str
    turn: str
    epoch: str  # Changes after resume/compaction, even within the same turn.


@dataclass(frozen=True)
class SectionRead:
    scope: GuardScope
    context: str
    section: str
    revision: int
    sha256: str
    reference_mode: str
    complete: bool


@dataclass(frozen=True)
class CurrentSection:
    context: str
    section: str
    revision: int
    sha256: str
    state: str


BlockReason = Literal["search_required", "read_required", "current_read_required"]


def check_context(
    scope: GuardScope,
    search_scope: GuardScope | None,
    searched_contexts: Set[str],
    reads: Sequence[SectionRead],
    current_sections: Sequence[CurrentSection],
) -> BlockReason | None:
    """Return a safe block reason; None permits only this freshness boundary.

    Semantic relevance and whether a section contains historical decisions are
    separate obligations. A matching digest cannot establish either by itself.
    The server's current section must also remain effective (active).
    """
    if not all(astuple(scope)) or search_scope != scope or not searched_contexts:
        return "search_required"
    if not reads:
        return "read_required"
    current = {(item.context, item.section): item for item in current_sections}
    if len(current) != len(current_sections):
        return "current_read_required"
    for read in reads:
        if (
            read.scope != scope
            or read.context not in searched_contexts
            or not read.section
            or type(read.revision) is not int
            or read.revision < 1
            or re.fullmatch(r"[a-f0-9]{64}", read.sha256) is None
            or read.reference_mode != "current"
            or read.complete is not True
        ):
            return "read_required"
        head = current.get((read.context, read.section))
        if (
            head is None
            or head.state != "active"
            or type(head.revision) is not int
            or head.revision != read.revision
            or head.sha256 != read.sha256
        ):
            return "current_read_required"
    return None
