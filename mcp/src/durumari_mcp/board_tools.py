"""Board tools for account connections.

The web's boards of a cloud, offered over MCP as on the official REST API
(`/clouds/{cloud_id}/boards/...`). A client exposes them by implementing
`boards(action, body)`. The server runs the web's own board functions, so access follows
the board's space, edits conflict on expected_version, and an operation_id or request_key
replays. Live cursors (presence) stay on the web screen.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field

Id = Annotated[str, Field(min_length=36, max_length=36, pattern=r"^[0-9a-f-]{36}$")]
BoardId = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9-]+$")]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
ObjectId = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")]


def register_board_tools(
    boards: Callable[[str, dict[str, Any]], dict[str, Any]],
    register: Any,
    safe: Callable[[Callable[[], dict[str, Any]]], CallToolResult],
) -> None:
    def call(action: str, body: dict[str, Any]) -> CallToolResult:
        return safe(lambda: boards(action, {k: v for k, v in body.items() if v is not None}))

    def durumari_board_read(
        cloud_id: Id,
        action: Literal["list", "scopes", "read", "events", "image", "evidence"] = "list",
        space_id: Id | None = None,
        board_id: BoardId | None = None,
        after: Annotated[int, Field(ge=0)] | None = None,
        image_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None,
        evidence_id: BoardId | None = None,
    ) -> CallToolResult:
        """Read a cloud's boards. list: boards of space_id (default the common space).
        scopes: spaces you can read, and where you may create. read: one board_id with its
        objects, revision and writable. events: changes after revision `after`. image: an
        uploaded image_id as base64. evidence: a saved board evidence_id and its snapshot."""
        return call(
            action,
            {
                "cloud_id": cloud_id,
                "space_id": space_id,
                "board_id": board_id,
                "after": after,
                "image_id": image_id,
                "evidence_id": evidence_id,
            },
        )

    def durumari_board_edit(
        cloud_id: Id,
        action: Literal["create", "operate", "batch", "upload_image", "save_context"],
        board_id: BoardId | None = None,
        request_key: Key | None = None,
        space_id: Id | None = None,
        title: Annotated[str, Field(min_length=1, max_length=120)] | None = None,
        operation: dict[str, Any] | None = None,
        operations: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=100)]
        | None = None,
        data_base64: Annotated[str, Field(min_length=1, max_length=7_000_000)] | None = None,
        revision: Annotated[int, Field(ge=0)] | None = None,
        object_ids: Annotated[list[ObjectId], Field(min_length=1, max_length=50)] | None = None,
        body: Annotated[str, Field(min_length=1, max_length=10000)] | None = None,
    ) -> CallToolResult:
        """Change a board when the person asked for it.
        create: request_key, title, space_id; the same request_key answers with the first
        board (replayed=true).
        operate: board_id, operation={operation_id, object_id, expected_version, value}
        where value is {id, kind: stroke|text|image, x, y, width, height, points, text,
        image_id} or null to remove; batch: operations=[...] (one per object, up to 100).
        A reused operation_id replays; a stale expected_version is board_object_conflict.
        upload_image: board_id, data_base64 (PNG or JPEG, up to 5 MB) -> image id.
        save_context: board_id, request_key, space_id, revision, object_ids, title, body
        saves the selected objects as a Context with the board as evidence."""
        return call(
            action,
            {
                "cloud_id": cloud_id,
                "board_id": board_id,
                "request_key": request_key,
                "space_id": space_id,
                "title": title,
                "operation": operation,
                "operations": operations,
                "data_base64": data_base64,
                "revision": revision,
                "object_ids": object_ids,
                "body": body,
            },
        )

    def durumari_board_delete(cloud_id: Id, board_id: BoardId) -> CallToolResult:
        """Delete one board when the person asked for it (the space's delete permission, or
        edit for a board you made). The board leaves every list."""
        return call("delete", {"cloud_id": cloud_id, "board_id": board_id})

    register(durumari_board_read, read=True, title="Read boards")
    register(
        durumari_board_edit,
        title="Create or change a board",
        required_scopes=["context:read", "context:write"],
    )
    register(
        durumari_board_delete,
        destructive=True,
        title="Delete a board",
        required_scopes=["context:read", "context:delete"],
    )
