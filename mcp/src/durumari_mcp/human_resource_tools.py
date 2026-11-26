"""File, review and secret work through a human browser task, the same for every host.

Each tool only opens a task bound to the conversation's project_ref. Writes happen
in the browser, where the person sees and confirms them; the model then recovers
IDs and revisions with durumari_resource_status. No tool takes file bytes, review
text, secret values or passwords.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import Field

TOOLS = ("durumari_files_browser", "durumari_reviews_browser", "durumari_secret_browser")


def register_human_resource_tools(
    resources: Callable[[str, dict[str, Any]], dict[str, Any]],
    register: Any,
    secret_tools: Callable[[], bool],
) -> None:
    from durumari_mcp.cloud_protocol import Key, Reference, safe

    def begin(
        project_ref: str,
        request_key: str,
        kind: str,
        action: str,
        resource_id: str | None = None,
        revision: int | None = None,
    ) -> CallToolResult:
        body: dict[str, Any] = {
            "project_ref": project_ref,
            "request_key": request_key,
            "kind": kind,
            "action": action,
        }
        if resource_id is not None:
            body["resource_id"] = resource_id
        if revision is not None:
            if resource_id is None:
                raise ToolError("invalid_tool_arguments")
            body["revision"] = revision
        return safe(lambda: resources("begin", body))

    def durumari_files_browser(
        project_ref: Reference,
        request_key: Key,
        action: Literal["browse", "manage"] = "browse",
        file_id: Reference | None = None,
        revision: Annotated[int | None, Field(ge=1, strict=True)] = None,
    ) -> CallToolResult:
        """Browse space files, or upload/replace/remove/restore them in a human browser.

        File bytes stay in the browser. "manage" needs the connection's files:write
        and the person's own permission; every change keeps the earlier version.
        Open browser_url, then recover verified IDs/revisions/sha256 with
        durumari_resource_status. Pending is not completion.
        """
        return begin(project_ref, request_key, "files", action, file_id, revision)

    def durumari_reviews_browser(
        project_ref: Reference,
        request_key: Key,
        action: Literal["browse", "manage"] = "browse",
        review_id: Reference | None = None,
        revision: Annotated[int | None, Field(ge=1, strict=True)] = None,
    ) -> CallToolResult:
        """Browse review requests, or create/respond to one in a human browser.

        The person's real role decides what they may answer or resolve. Open
        browser_url, then recover the request ID and applied revision with
        durumari_resource_status. Never report pending work as completed.
        """
        return begin(project_ref, request_key, "reviews", action, review_id, revision)

    def durumari_secret_browser(
        project_ref: Reference,
        request_key: Key,
        secret_id: Reference | None = None,
        revision: Annotated[int | None, Field(ge=1, strict=True)] = None,
    ) -> CallToolResult:
        """Open the secure vault screen to create, replace or delete secret values.

        Never send a value or password to this tool, and never ask the person to
        paste one into chat. They change values in the vault, then confirm with
        their password; durumari_resource_status returns only IDs and revisions.
        This grants the model no create, read or use permission.
        """
        if not secret_tools():
            raise ToolError("tool_unavailable")
        return begin(project_ref, request_key, "secrets", "manage", secret_id, revision)

    for fn, title, required in [
        (durumari_files_browser, "Browse or manage files", ["files:read"]),
        (durumari_reviews_browser, "Browse or manage reviews", ["requests:read"]),
        (durumari_secret_browser, "Manage secrets on the secure screen", ["secrets:list"]),
    ]:
        register(fn, title=title, read=False, required_scopes=required)
