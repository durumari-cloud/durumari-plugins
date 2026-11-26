"""The web's cloud, member, notification and connection management for account connections.

Each tool is one web feature and its `action` names the step; the server runs the web's own
route function for it, as the official REST API does. A client exposes these tools by
implementing `cloud_admin(tool, body)`. Inviting, resending an invitation, approving an
access request, switching to explicit permissions, replacing a member's groups, opening view
to everyone and changing the connection policy are only asked: the answer is
human_approval_required with the web page where a person approves.
Withdrawing the account is done by the person on the web after signing in again.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field

Id = Annotated[str, Field(min_length=36, max_length=36, pattern=r"^[0-9a-f-]{36}$")]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
READ = ["context:read"]
WRITE = ["context:read", "context:write"]
# billing:read is part of READ; billing:write only when the OAuth request names it (1090).
BILLING = ["billing:read", "billing:write"]


def register_cloud_admin_tools(
    cloud_admin: Callable[[str, dict[str, Any]], dict[str, Any]],
    register: Any,
    safe: Callable[[Callable[[], dict[str, Any]]], CallToolResult],
) -> None:
    def call(tool: str, body: dict[str, Any]) -> CallToolResult:
        # Each tool passes locals(), which also holds this helper (a free variable).
        values = {k: v for k, v in body.items() if v is not None and k != "call"}
        return safe(lambda: cloud_admin(tool, values))

    def durumari_notifications(
        action: Literal["list", "summary", "read", "read_all"] = "list",
        request_key: Key | None = None,
        notification_id: Id | None = None,
        read: bool | None = None,
        before: str | None = None,
        after: Annotated[str, Field(max_length=36)] | None = None,
        limit: Annotated[int, Field(ge=1, le=100)] | None = None,
        filter: Literal["all", "unread"] | None = None,
    ) -> CallToolResult:
        """Your notification inbox: list (after, limit, filter), summary (unread counts),
        read (notification_id, read=false marks unread) or read_all (before, ISO time).
        Marking needs request_key."""
        return call("durumari_notifications", locals())

    def durumari_chat_channels(
        action: Literal[
            "list",
            "connect",
            "control",
            "mattermost_list",
            "mattermost_register",
            "mattermost_revoke",
        ],
        space_id: Id,
        channel_id: Annotated[str, Field(max_length=200)] | None = None,
        binding_id: Id | None = None,
        control: Literal["pause", "resume", "remove"] | None = None,
        revision: Annotated[int, Field(ge=0)] | None = None,
        token: Annotated[str, Field(min_length=1, max_length=200)] | None = None,
        server_origin: Annotated[str, Field(max_length=300)] | None = None,
        command_id: Id | None = None,
    ) -> CallToolResult:
        """A space's chat inbox channels and Mattermost slash commands: list, connect
        (channel_id), control (binding_id, control, revision), mattermost_list,
        mattermost_register (the command's token and server_origin, only when the person
        gave them) or mattermost_revoke (command_id)."""
        return call("durumari_chat_channels", locals())

    def durumari_cloud_membership(
        action: Literal["deleted_clouds", "deleted_spaces", "leave", "withdrawal_plan", "withdraw"],
        cloud_id: Id | None = None,
    ) -> CallToolResult:
        """Deleted clouds you can restore, a cloud's deleted spaces (cloud_id), leaving a
        cloud (cloud_id; only when the person asked), what withdrawing would remove, or
        withdraw: that answers with the web page where the person withdraws after signing
        in again."""
        return call("durumari_cloud_membership", locals())

    def durumari_cloud_settings(
        action: Literal[
            "icon_upload",
            "icon_read",
            "usage",
            "usage_refresh",
            "cloud_language",
            "cloud_language_set",
            "account_language",
            "account_language_set",
            "navigation",
            "navigation_set",
            "dashboard",
            "dashboard_set",
            "profile_update",
        ],
        cloud_id: Id | None = None,
        image_base64: Annotated[str, Field(max_length=1_400_000)] | None = None,
        days: Annotated[int, Field(ge=1, le=366)] | None = None,
        default_language: Literal["ko", "en"] | None = None,
        preferred_language: Literal["ko", "en"] | None = None,
        kind: Literal["tabs", "groups", "collapsed", "clouds", "spaces"] | None = None,
        scope: Annotated[str, Field(max_length=128)] | None = None,
        order: list[str] | None = None,
        expected_revision: Annotated[int, Field(ge=0)] | None = None,
        widgets: list[dict[str, Any]] | None = None,
        display_name: Annotated[str, Field(min_length=1, max_length=80)] | None = None,
    ) -> CallToolResult:
        """Cloud icon (icon_upload with image_base64 → upload_id; icon_read), usage and
        usage_refresh (cloud owner; days), the cloud's language, your account language,
        sidebar order (navigation: kind, scope, order, expected_revision), the cloud
        dashboard layout (dashboard: cloud_id, widgets, expected_revision) and your display
        name (profile_update)."""
        return call("durumari_cloud_settings", locals())

    def durumari_member_manage(
        action: Literal[
            "invitations",
            "invite",
            "invitation_resend",
            "invitation_revoke",
            "space_invitations",
            "space_invite",
            "space_invitation_revoke",
            "space_invitation_retry",
            "aliases",
            "groups",
            "audit",
            "activate_permissions",
            "everyone_view_all",
        ],
        cloud_id: Id | None = None,
        space_id: Id | None = None,
        request_key: Key | None = None,
        invitation_id: Id | None = None,
        invitation: dict[str, Any] | None = None,
        send_email: bool | None = None,
        user_id: Id | None = None,
        aliases: list[str] | None = None,
        group_ids: list[Id] | None = None,
        space_ids: list[Id] | None = None,
    ) -> CallToolResult:
        """Invitations, member aliases and permission changes. Reads: invitations (cloud),
        space_invitations, audit. Direct: invitation_revoke, space_invitation_revoke,
        space_invitation_retry, aliases (user_id, aliases). Asked for a person's web
        approval (request_key; answers human_approval_required with approval_url): invite
        (cloud_id, invitation={email, role, group_ids, grants}), space_invite (space_id,
        invitation={email, role, access}), invitation_resend, groups (user_id, group_ids),
        activate_permissions, everyone_view_all (space_ids). Roles, removal, groups and
        grants: durumari_action_request."""
        return call("durumari_member_manage", locals())

    def durumari_access_requests(
        action: Literal["list", "approve", "deny"] = "list",
        scope: Literal["incoming", "sent"] | None = None,
        request_id: Id | None = None,
        request_key: Key | None = None,
    ) -> CallToolResult:
        """Requests for view or edit access in spaces: list (incoming to decide, or sent),
        deny (request_id), or approve, which is asked for the cloud owner's web approval
        (request_key; answers human_approval_required)."""
        return call("durumari_access_requests", locals())

    def durumari_action_request_list() -> CallToolResult:
        """Open execution requests waiting for approval in clouds you own."""
        return call("durumari_action_request_list", {"action": "list"})

    def durumari_agent_connections(
        action: Literal[
            "list", "injection", "injection_set", "policy", "policy_set", "requests", "apps",
            "app",
        ],
        space_id: Id | None = None,
        cloud_id: Id | None = None,
        after: Annotated[str, Field(max_length=128)] | None = None,
        depth: Literal["off", "light", "normal", "deep"] | None = None,
        request_key: Key | None = None,
        mode: Literal["auto", "approval", "blocked"] | None = None,
        approvers: list[dict[str, Any]] | None = None,
        state: Literal["pending", "approved", "denied"] | None = None,
        family: Annotated[str, Field(max_length=40)] | None = None,
    ) -> CallToolResult:
        """Agent connections: a space's connections (list) and Context injection depth
        (injection, injection_set), a cloud's connection policy (policy; policy_set with
        mode and approvers is asked for the owner's web approval), connection requests
        (requests: state, space_id), and your connected apps (apps, app with family)."""
        return call("durumari_agent_connections", locals())

    def durumari_agent_runs(
        action: Literal["list", "read", "stop", "remove"],
        space_id: Id | None = None,
        run_id: Id | None = None,
        limit: Annotated[int, Field(ge=1, le=50)] | None = None,
    ) -> CallToolResult:
        """Your Agent runs: list a space's runs, read, stop or remove one (run_id)."""
        return call("durumari_agent_runs", locals())

    def durumari_billing(
        action: Literal["plan", "cloud_plan", "vapor", "propose", "proposal", "settings"],
        cloud_id: Id | None = None,
        change: Literal["checkout", "cancel", "resume", "seats", "change_plan"] | None = None,
        target: Literal["account", "cloud"] | None = None,
        plan: Literal["plus", "pro", "team"] | None = None,
        interval: Literal["month", "year"] | None = None,
        seats: Annotated[int, Field(ge=5, le=10_000)] | None = None,
        trial: bool | None = None,
        proposal_id: Id | None = None,
        request_key: Key | None = None,
    ) -> CallToolResult:
        """Plans and billing. plan (your account), cloud_plan or vapor (cloud_id) read.
        propose (change, target, cloud_id for a cloud, plan/interval/seats/trial, request_key)
        never charges or changes anything: it returns the quote and confirm_url, the web page
        where the person confirms; tell them to open it. proposal (proposal_id) reads one.
        settings (target, cloud_id) gives the web page for the payment method and invoices."""
        return call("durumari_billing", locals())

    register(durumari_notifications, title="Notification inbox", required_scopes=WRITE)
    register(durumari_chat_channels, title="Chat channels and Mattermost", required_scopes=WRITE)
    register(
        durumari_cloud_membership,
        title="Deleted lists, leaving and withdrawal",
        required_scopes=WRITE,
        destructive=True,
    )
    register(durumari_cloud_settings, title="Cloud and account settings", required_scopes=WRITE)
    register(
        durumari_member_manage, title="Invitations and permission changes", required_scopes=WRITE
    )
    register(durumari_access_requests, title="Review access requests", required_scopes=WRITE)
    register(
        durumari_action_request_list, read=True, title="List open execution requests"
    )
    register(durumari_agent_connections, title="Agent connection settings", required_scopes=WRITE)
    register(
        durumari_agent_runs, title="Agent runs", required_scopes=WRITE, destructive=True
    )
    register(durumari_billing, title="Plans and billing", required_scopes=BILLING)
