"""Explicit conversation navigation with recoverable browser interactions."""

from collections.abc import Callable
from typing import Annotated, Any
from uuid import uuid4

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import Field


def register_project_tools(
    projects: Callable[[str, dict[str, Any]], dict[str, Any]], register: Any
) -> None:
    # Imported when the already-initialized protocol registers this optional capability.
    from durumari_mcp.cloud_protocol import Key, Reference, safe

    def durumari_conversation_start(request_key: Key | None = None) -> CallToolResult:
        """Start a new conversation's independent cloud/space selection.

        Optional: target_project_id works without a selection. For a browser selection,
        open browser_url and recover it with durumari_interaction_status.

        For a new conversation omit request_key or use a new unique key. Only reuse an
        explicit key after unknown delivery for that same conversation. Retain the
        returned request_key and conversation_ref;
        it is an opaque reference, not a host conversation ID or permission grant.
        Never reuse another conversation's key/reference or change folder settings.
        Isolation requires the caller to pass the right reference; the host's real
        conversation identity is not available or enforced by this protocol.
        """
        key = request_key if request_key is not None else str(uuid4())

        def start() -> dict[str, Any]:
            return {**projects("start", {"request_key": key}), "request_key": key}

        return safe(start)

    def durumari_project_catalog(
        conversation_ref: Reference | None = None,
        cloud_id: Reference | None = None,
        query: Annotated[str | None, Field(max_length=100)] = None,
        after: Annotated[str | None, Field(max_length=256)] = None,
    ) -> CallToolResult:
        """Browse accessible clouds/spaces; follow the returned cursor.

        Never ask the person to pick a space you can name yourself.
        Read-only. Omit conversation_ref to list the spaces this account may name with
        target_project_id; pass it to also see this conversation's selection.
        Listing grants no access. Each space carries role (owner|member), content
        {read, write, manage, limited_by} and content_summary, a sentence to show the
        user; owner_sees_all means an owner's connection sees every space. Rights are
        the account's, evaluated now; permissions are checked again on every operation.
        """
        body: dict[str, Any] = {}
        for key, value in (
            ("conversation_ref", conversation_ref),
            ("cloud_id", cloud_id),
            ("query", query),
            ("after", after),
        ):
            if value is not None:
                body[key] = value
        return safe(lambda: projects("catalog", body))

    def durumari_project_select(
        conversation_ref: Reference,
        expected_revision: Annotated[int, Field(ge=0, strict=True)],
        request_key: Key,
        project_id: Reference | None = None,
        project_url: Annotated[str | None, Field(min_length=1, max_length=2048)] = None,
    ) -> CallToolResult:
        """Select/switch this conversation using exactly one project_id or project_url.

        Supply the current conversation revision and retain the returned project_ref.
        Pass that immutable project_ref to every Context call. Server validation of a
        link and current access is authoritative; a link or reference grants no access.
        Reuse request_key only for the same selection after unknown delivery. The
        result adds the space's current content and content_summary (never cached).
        """
        if (project_id is None) == (project_url is None):
            raise ToolError("invalid_project_selection")
        body: dict[str, Any] = {
            "conversation_ref": conversation_ref,
            "expected_revision": expected_revision,
            "request_key": request_key,
        }
        if project_id is not None:
            body["project_id"] = project_id
        else:
            body["project_url"] = project_url
        return safe(lambda: projects("select", body))

    def durumari_project_browser(
        conversation_ref: Reference,
        expected_revision: Annotated[int, Field(ge=0, strict=True)],
        request_key: Key,
    ) -> CallToolResult:
        """Open a cloud/space chooser when a browser is more convenient.

        Open the returned browser_url, then call durumari_interaction_status with
        task_ref to recover the selection into this conversation. Keep waiting bounded;
        pending is not completion. Login, cancellation and expiry belong to the same
        task. Creating a cloud/space requires the person's explicit browser action.
        """
        return safe(
            lambda: projects(
                "browser",
                {
                    "conversation_ref": conversation_ref,
                    "expected_revision": expected_revision,
                    "request_key": request_key,
                },
            )
        )

    def durumari_project_disconnect(
        conversation_ref: Reference,
        expected_revision: Annotated[int, Field(ge=0, strict=True)],
        request_key: Key,
    ) -> CallToolResult:
        """Clear only this conversation's space selection using its current revision.

        This does not remove space data, folder configuration or another conversation's
        selection. Retain the new revision; select or open the chooser to connect again.
        """
        return safe(
            lambda: projects(
                "disconnect",
                {
                    "conversation_ref": conversation_ref,
                    "expected_revision": expected_revision,
                    "request_key": request_key,
                },
            )
        )

    def durumari_interaction_status(
        task_ref: Reference,
        wait_seconds: Annotated[int, Field(ge=0, le=15, strict=True)] = 0,
    ) -> CallToolResult:
        """Recover a browser task's safe result with a short, bounded wait.

        Pending/unknown is not success. On completion use the returned project_ref and
        conversation revision for subsequent calls. Report cancelled/expired/denied
        states accurately; do not substitute another conversation or historical data.
        """
        return safe(
            lambda: projects("status", {"task_ref": task_ref, "wait_seconds": wait_seconds})
        )

    for fn, title, read in [
        (durumari_conversation_start, "Start an independent conversation", False),
        (durumari_project_catalog, "Browse accessible clouds and projects", True),
        (durumari_project_select, "Select this conversation's project", False),
        (durumari_project_browser, "Choose a project in the browser", False),
        (durumari_project_disconnect, "Disconnect this conversation's project", False),
        (durumari_interaction_status, "Check a browser task result", True),
    ]:
        register(
            fn,
            read=read,
            title=title,
            required_scopes=["context:read"],
            idempotent=fn is not durumari_conversation_start,
        )
