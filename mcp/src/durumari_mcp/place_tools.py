"""Cloud and space management tools for account connections.

The official REST API lets an account connection create, rename and restore clouds and
spaces and read members and permissions; these tools give MCP the same operations. A client
exposes them by implementing `places(action, body)`. Deleting a cloud or a space and
changing or removing a member are only asked (durumari_action_request): a person approves
on the web, as on the REST API.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field

Id = Annotated[str, Field(min_length=36, max_length=36, pattern=r"^[0-9a-f-]{36}$")]
Name = Annotated[str, Field(min_length=1, max_length=80)]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]


def register_place_tools(
    places: Callable[[str, dict[str, Any]], dict[str, Any]],
    register: Any,
    safe: Callable[[Callable[[], dict[str, Any]]], CallToolResult],
) -> None:
    def call(action: str, body: dict[str, Any]) -> CallToolResult:
        return safe(lambda: places(action, {k: v for k, v in body.items() if v is not None}))

    def durumari_cloud_create(
        request_key: Key,
        name: Name,
        default_language: Literal["ko", "en"] = "ko",
        default_space_name: Name | None = None,
    ) -> CallToolResult:
        """Create a cloud you own, with its default space, when the person asked for it.

        Reuse the same request_key after an unclear reply: it answers with the cloud the
        first attempt made (replayed=true) instead of making a second one.
        """
        return call(
            "create_cloud",
            {
                "request_key": request_key,
                "name": name,
                "default_language": default_language,
                "default_space_name": default_space_name,
            },
        )

    def durumari_cloud_rename(cloud_id: Id, name: Name, expected_name: Name) -> CallToolResult:
        """Rename a cloud you own. expected_name must be the name you last read; if it was
        changed meanwhile the call is refused (workspace_name_conflict)."""
        return call(
            "rename_cloud", {"cloud_id": cloud_id, "name": name, "expected_name": expected_name}
        )

    def durumari_cloud_restore(cloud_id: Id) -> CallToolResult:
        """Bring back a deleted cloud you own, with the spaces deleted with it."""
        return call("restore_cloud", {"cloud_id": cloud_id})

    def durumari_space_create(
        request_key: Key, cloud_id: Id, name: Name | None = None
    ) -> CallToolResult:
        """Create a space in a cloud you own; without a name the cloud's next default name
        is used. Reuse the request_key after an unclear reply."""
        return call(
            "create_space", {"request_key": request_key, "cloud_id": cloud_id, "name": name}
        )

    def durumari_space_rename(space_id: Id, name: Name, expected_name: Name) -> CallToolResult:
        """Rename a space (cloud owner only); expected_name must be the current name."""
        return call(
            "rename_space", {"space_id": space_id, "name": name, "expected_name": expected_name}
        )

    def durumari_space_restore(space_id: Id) -> CallToolResult:
        """Bring back a deleted space (cloud owner only, while its cloud is not deleted)."""
        return call("restore_space", {"space_id": space_id})

    def durumari_cloud_members(
        cloud_id: Id | None = None, space_id: Id | None = None
    ) -> CallToolResult:
        """List the members of a cloud (cloud_id) or of a space (space_id). Read only:
        inviting, removing or changing roles stays on the web."""
        return call("members", {"cloud_id": cloud_id, "space_id": space_id})

    def durumari_cloud_permissions(
        cloud_id: Id,
        view: Literal["grants", "groups", "effective"] = "grants",
        target_type: Literal["cloud", "space"] | None = None,
        target_id: Id | None = None,
        user_id: Id | None = None,
    ) -> CallToolResult:
        """Read a cloud's grants (permission managers), groups, or effective permissions on
        target_type/target_id (default the cloud) for user_id (default you)."""
        return call(
            "permissions",
            {
                "cloud_id": cloud_id,
                "view": None if view == "grants" else view,
                "target_type": target_type,
                "target_id": target_id,
                "user_id": user_id,
            },
        )

    def durumari_action_request(
        request_key: Key,
        action: Literal[
            "cloud.delete",
            "space.delete",
            "member.role",
            "member.remove",
            "group.create",
            "group.update",
            "group.delete",
            "group.member_add",
            "group.member_remove",
            "grant.create",
            "grant.remove",
            "member.invite",
            "space.invite",
            "invitation.resend",
            "access_request.approve",
            "access.activate",
            "member.groups",
            "everyone.view_all",
            "connection_policy.update",
        ],
        target_id: Id,
        params: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Ask a person to approve a member, group, grant or deletion change on the web;
        nothing runs here. target_id: the cloud (create actions), space, member user,
        group or grant. params: cloud_id (+role; group_id; name, parent_id, description) or for
        grant.create user_id|group_id, on (cloud|space), on_id, permission, include_spaces.
        Answers human_approval_required with approval_url and request_id."""
        return call(
            "request_action",
            {"request_key": request_key, "action": action, "target_id": target_id,
             "params": params},
        )

    def durumari_action_status(request_id: Id) -> CallToolResult:
        """Read an execution request's state (pending, executed, denied, expired, stale)."""
        return call("action_status", {"request_id": request_id})

    write = ["context:read", "context:write"]
    register(durumari_cloud_create, title="Create a cloud", required_scopes=write)
    register(durumari_cloud_rename, title="Rename a cloud", required_scopes=write)
    register(durumari_cloud_restore, title="Restore a deleted cloud", required_scopes=write)
    register(durumari_space_create, title="Create a space", required_scopes=write)
    register(durumari_space_rename, title="Rename a space", required_scopes=write)
    register(durumari_space_restore, title="Restore a deleted space", required_scopes=write)
    register(durumari_cloud_members, read=True, title="List cloud or space members")
    register(durumari_cloud_permissions, read=True, title="Read cloud permissions and groups")
    register(durumari_action_request, title="Ask for a web-approved action", required_scopes=write)
    register(durumari_action_status, read=True, title="Read an action request")
