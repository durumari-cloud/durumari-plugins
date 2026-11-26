"""Discover authorized Context without selecting a conversation's project."""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field


def register(search: Callable[[dict[str, Any]], dict[str, Any]], register_tool: Any) -> None:
    from durumari_mcp.cloud_protocol import Budget, Reference, safe

    def durumari_account_context_search(
        query: Annotated[str, Field(min_length=2, max_length=200)],
        after_project: Reference | None = None,
        after: Annotated[int, Field(ge=0, le=10000, strict=True)] = 0,
        budget_tokens: Budget = 1500,
    ) -> CallToolResult:
        """Search current Context across this account's authorized spaces.

        No Git, folder or conversation selection is required. Each hit includes its
        project_id, project_name and cloud. Use target_project_id=project_id on Context
        CRUD tools to name that immutable target. A space ID grants no permission.
        Follow next_after within the same after_project page, then next_project_after
        with after=0. Coverage is one bounded space page, not an exhaustive account
        search until both cursors are exhausted. Read the current sections before use.
        """
        return safe(
            lambda: search(
                {
                    "query": query,
                    "after_project": after_project,
                    "after": after,
                    "budget_tokens": budget_tokens,
                }
            )
        )

    register_tool(
        durumari_account_context_search,
        read=True,
        title="Search authorized Context across projects",
        required_scopes=["context:read"],
    )


def register_unified(
    search: Callable[[dict[str, Any]], dict[str, Any]], register_tool: Any
) -> None:
    from durumari_mcp.cloud_protocol import Reference, safe

    def durumari_account_search(
        query: Annotated[str, Field(min_length=2, max_length=100)],
        types: Annotated[
            list[Literal["context", "work_log", "request", "file", "secret", "board", "glossary"]]
            | None,
            Field(max_length=7),
        ] = None,
        cloud_id: Reference | None = None,
        limit: Annotated[int, Field(ge=1, le=10, strict=True)] = 5,
        after: Annotated[int, Field(ge=0, le=100, strict=True)] = 0,
    ) -> CallToolResult:
        """Search every resource kind this connection may read, as the web's global search.

        Kinds: context, work_log, request, file, secret, board, glossary. Omit types to
        search all of them; a kind this connection has no permission or feature for is
        listed in omitted_types and never searched. Each group returns up to limit rows
        and has_more; continue a group with after (rows already seen, up to 100). Hits
        carry type, id, title, excerpt, cloud_id and project_id; use
        target_project_id=project_id on Context tools, then read the current sections
        before use. Secrets match by name and return metadata only, never a value.
        spaces_truncated=true means more spaces exist than one call covers: narrow with
        cloud_id. A space ID grants no permission.
        """
        return safe(
            lambda: search(
                {
                    "query": query,
                    "types": types,
                    "cloud_id": cloud_id,
                    "limit": limit,
                    "after": after,
                }
            )
        )

    register_tool(
        durumari_account_search,
        read=True,
        title="Search all resources across projects",
        required_scopes=["context:read"],
    )


def register_my_work(
    my_work: Callable[[dict[str, Any]], dict[str, Any]], register_tool: Any
) -> None:
    from durumari_mcp.cloud_protocol import Reference, safe

    def durumari_my_work(
        after_project: Reference | None = None,
        updated_days: Annotated[int, Field(ge=1, le=365, strict=True)] = 30,
        limit_per_space: Annotated[int, Field(ge=1, le=10, strict=True)] = 5,
    ) -> CallToolResult:
        """List this person's open work across every space the account may read.

        Use for "what do I have to do (today)?" without choosing a space. Per space:
        open work logs (in_progress, waiting, unknown) assigned to the person or saved
        by them with no assignee, changed within updated_days, and review requests
        needing their action. Spaces with nothing open are left out. Follow
        next_project_after for more spaces; omitted lists kinds this connection cannot
        read. Read a work log with target_project_id=project_id before acting on it.
        """
        body: dict[str, Any] = {
            "updated_days": updated_days,
            "limit_per_space": limit_per_space,
        }
        if after_project is not None:
            body["after_project"] = after_project
        return safe(lambda: my_work(body))

    register_tool(
        durumari_my_work,
        read=True,
        title="List my open work across projects",
        required_scopes=["context:read"],
    )
