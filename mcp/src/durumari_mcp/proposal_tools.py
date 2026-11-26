"""Submit immutable patches, inspect human decisions, and apply only confirmed proposals."""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import BaseModel, ConfigDict, Field

from durumari_mcp.request_tools import ItemReference, Reviewer

ACTIONS = frozenset({"create", "list", "read", "apply", "status"})


class ProposalSection(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")
    before_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    text: str = Field(max_length=5000)


def register_proposal_tools(
    proposals: Callable[[str, dict[str, Any]], dict[str, Any]], register: Any
) -> None:
    from durumari_mcp.cloud_protocol import Key, Reference, safe

    def target(project_ref: str | None, **values: Any) -> dict[str, Any]:
        return {**values, **({"project_ref": project_ref} if project_ref else {})}

    def durumari_proposal_create(
        context_id: Reference,
        base_revision: Annotated[int, Field(ge=1, strict=True)],
        sections: Annotated[list[ProposalSection], Field(min_length=1, max_length=8)],
        evidence: Annotated[list[ItemReference], Field(min_length=1, max_length=6)],
        reason: Annotated[str, Field(min_length=1, max_length=1000)],
        approver_id: Reference,
        request_key: Key,
        reviewers: Annotated[list[Reviewer] | None, Field(max_length=20)] = None,
        project_ref: Reference | None = None,
    ) -> CallToolResult:
        """Propose exact section edits with evidence; creates a human decision card.

        The card is a decision request: approver_id makes the final approval step,
        required reviewers agree first, optional ones are cc; omit reviewers to let
        the approver alone decide. This never approves or applies the patch. Read the
        current section hashes first. Keep the entire proposal small enough for one
        decision card. On an unknown result retry the same request_key or use
        proposal_status. Do not treat Agent reports as human approval; use the
        returned web_url for the person's decision.
        """
        proposal = {
            "context_id": context_id,
            "base_revision": base_revision,
            "sections": [s.model_dump() for s in sections],
            "evidence": [r.model_dump(exclude_none=True) for r in evidence],
            "reason": reason,
            "approver_id": approver_id,
        }
        if reviewers is not None:
            proposal["reviewers"] = [r.model_dump() for r in reviewers]
        return safe(
            lambda: proposals(
                "create", target(project_ref, request_key=request_key, proposal=proposal)
            )
        )

    def durumari_proposal_list(
        after: Annotated[str, Field(max_length=36)] = "",
        limit: Annotated[int, Field(ge=1, le=20, strict=True)] = 10,
        context_id: Reference | None = None,
        state: Literal["needs_decision", "approved", "applied", "rejected", "stale"] | None = None,
        project_ref: Reference | None = None,
    ) -> CallToolResult:
        """List this space's bounded proposals and current decision/application state.

        Filter by context_id or state before proposing: do not re-propose a change that
        is already waiting or was declined for the same evidence (the server refuses
        it with proposal_evidence_declined, and with proposal_limit_reached when too
        many of your proposals await a decision).
        """
        extra = {k: v for k, v in (("context_id", context_id), ("state", state)) if v}
        return safe(
            lambda: proposals("list", target(project_ref, after=after, limit=limit, **extra))
        )

    def durumari_proposal_read(
        proposal_id: Reference, project_ref: Reference | None = None
    ) -> CallToolResult:
        """Read the immutable patch, evidence, human decision state and application receipt."""
        return safe(lambda: proposals("read", target(project_ref, proposal_id=proposal_id)))

    def durumari_proposal_apply(
        proposal_id: Reference, request_key: Key, project_ref: Reference | None = None
    ) -> CallToolResult:
        """Apply an unchanged, human-confirmed proposal under current authority.

        Approval and application are separate. Changed targets, evidence or decision
        questions are refused. Retry unknown results with the same request_key.
        """
        return safe(
            lambda: proposals(
                "apply", target(project_ref, proposal_id=proposal_id, request_key=request_key)
            )
        )

    def durumari_proposal_status(
        request_key: Key, project_ref: Reference | None = None
    ) -> CallToolResult:
        """Recover this actor's proposal write by its original request key; no new action."""
        return safe(lambda: proposals("status", target(project_ref, request_key=request_key)))

    register(durumari_proposal_create, title="Propose a Context patch for human decision")
    register(durumari_proposal_list, read=True, title="List Context proposals")
    register(durumari_proposal_read, read=True, title="Read a Context proposal")
    # Applying rewrites the target Context, like revise: hosts must treat it as destructive.
    register(
        durumari_proposal_apply,
        destructive=True,
        title="Apply a human-confirmed Context proposal",
    )
    register(durumari_proposal_status, read=True, title="Recover a Context proposal receipt")
