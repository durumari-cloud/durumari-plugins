"""Asking a space's managers for view or edit.

One definition for Chat and Code; each client supplies ``access_requests(body)``. The
request grants nothing: the space's managers (else the cloud owners) approve it on the
web, and the first decision counts. A pending request for the same person, space and
level is reused, so calling again does not send a second one.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field

TOOLS = ("durumari_access_request_create",)


def register_access_tools(
    access_requests: Callable[[dict[str, Any]], dict[str, Any]],
    register: Any,
) -> None:
    from durumari_mcp.cloud_protocol import safe

    def durumari_access_request_create(
        level: Literal["view", "edit"],
        project_id: Annotated[str, Field(pattern=r"^[a-f0-9-]{36}$")] | None = None,
        note: Annotated[str, Field(max_length=500)] | None = None,
    ) -> CallToolResult:
        """Ask for view or edit on a space after a refusal; approval happens on the web.

        Use it only when the user wants access and a tool error said
        details.next_action = request_edit (level=edit) or request_access (level=view);
        pass that error's details.project_id. A fixed-space connection may omit
        project_id. The result names who was asked; nothing is granted until one of
        them approves, so tell the user and do not retry the refused write meanwhile.
        cause=connection is not this: reconnect_with_write means the user reconnects
        the app with editing allowed; ask_owner_to_approve_connection means the cloud
        owner allows the connection.
        """
        body: dict[str, Any] = {"level": level}
        if project_id is not None:
            body["project_id"] = project_id
        if note is not None:
            body["note"] = note
        return safe(lambda: access_requests(body))

    register(
        durumari_access_request_create,
        title="Request view or edit access",
        required_scopes=["context:read"],
    )
