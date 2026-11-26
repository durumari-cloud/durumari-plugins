"""Human browser tasks with content-free, recoverable results for every MCP host."""

from collections.abc import Callable
from typing import Annotated, Any

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import Field


def register_resource_tools(
    resources: Callable[[str, dict[str, Any]], dict[str, Any]],
    register: Any,
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

    def durumari_context_browser(
        project_ref: Reference,
        request_key: Key,
        context_id: Reference | None = None,
        revision: Annotated[int | None, Field(ge=1, strict=True)] = None,
    ) -> CallToolResult:
        """Browse/search Context and read a whole document in a human browser.

        Open browser_url, then use durumari_resource_status with task_ref to recover
        the verified selected document ID/revision/hash. Keep this conversation's
        original project_ref. The reference is not a grant or a host conversation ID.
        """
        return begin(project_ref, request_key, "contexts", "browse", context_id, revision)

    def durumari_resource_status(
        task_ref: Reference,
        wait_seconds: Annotated[int, Field(ge=0, le=15, strict=True)] = 0,
    ) -> CallToolResult:
        """Recover a resource browser task's safe result with a bounded wait.

        A completed result contains verified IDs/revisions/counts, never source
        bodies, file bytes or secret values. Pending, canceled and expired are
        distinct states. After switching space, recover the original task's
        receipt; do not retry a mutation against the new target.
        """
        return safe(
            lambda: resources(
                "status",
                {
                    "task_ref": task_ref,
                    "wait_seconds": wait_seconds,
                },
            )
        )

    for fn, title, required in [
        (durumari_context_browser, "Browse Context in the browser", ["context:read"]),
        (durumari_resource_status, "Check a resource browser result", ["context:read"]),
    ]:
        register(fn, title=title, read=fn is durumari_resource_status, required_scopes=required)
