"""One browser approval, project-local Agent setup, and the common Context CLI."""

import json
import os
import re
import secrets
import sys
import time
import webbrowser
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import SecretStr

from durumari_mcp import plugin_env
from durumari_mcp.browser_login import browser_origin
from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.context_binding import (
    AGENTS,
    DEFAULT_AGENTS,
    bound_agents,
    bound_profile,
    install_binding,
    shared_profile,
    workspace_identity,
)
from durumari_mcp.context_client import ContextClient
from durumari_mcp.context_removal import disable, local_profile, reconcile, remove
from durumari_mcp.credentials import AdapterError, os_store
from durumari_mcp.feature_tools import HiddenFeatures, hide_unavailable
from durumari_mcp.json_output import emit_json as _emit_json
from durumari_mcp.legacy import manifest_path
from durumari_mcp.local_upgrade import migrate_manifest
from durumari_mcp.onboarding import CHANGES, change_preflight, read_manifest, repository_identity
from durumari_mcp.opencode_config import FORMATS
from durumari_mcp.project_credentials import (
    ProjectProfile,
    load_project_profile,
    save_project_profile,
)
from durumari_mcp.project_gateway import ContextFailure, ProjectGateway, ProjectTransport
from durumari_mcp.server_aliases import same_server
from durumari_mcp.stdio_serve import serve_stdio
from durumari_mcp.workspace_binding import binding_root, folder_identity, git_remote

ENROLLMENT_SERVICE = "DURUMARI.ContextEnrollment.v2"


def add_commands(subcommands: Any) -> None:
    connect = subcommands.add_parser(
        "context-connect", help="Connect this work folder once for all Agents"
    )
    connect.add_argument("--workspace", default=os.getcwd())
    connect.add_argument("--server")
    connect.add_argument("--project")
    connect.add_argument(
        "--folder-source", help="Public folder source UUID; always requires this device approval"
    )
    connect.add_argument("--agents", nargs="+", choices=AGENTS, default=DEFAULT_AGENTS)
    connect.add_argument(
        "--opencode-config",
        choices=FORMATS,
        default="auto",
        help="OpenCode settings shape; auto checks the installed opencode version.",
    )
    for command in (
        "context-serve", "context-execution", "context-status", "context-remove", "context",
    ):
        parser = subcommands.add_parser(command, help="Use the bound Cloud Context")
        parser.add_argument("--workspace", default=os.getcwd())
        parser.add_argument("--profile")
        parser.add_argument("--client", default="mcp")
        if command == "context-status":
            parser.add_argument(
                "--change",
                choices=CHANGES,
                help=(
                    "Check locally, before a git init, folder move or detach, "
                    "whether the connection must be approved again."
                ),
            )
        if command == "context":
            parser.add_argument(
                "action",
                choices=[
                    "list",
                    "search",
                    "read",
                    "save",
                    "revise",
                    "delete",
                    "restore",
                    "history",
                    "operation",
                    "sprint-close",
                ],
            )
            parser.add_argument("--id")
            parser.add_argument(
                "--query",
                help=(
                    "Search with 1-3 distinctive terms or a short phrase in the user's language. "
                    "Try synonyms separately; shorten queries with no results. "
                    "Use list for catalog requests."
                ),
            )
            parser.add_argument(
                "--include-archived",
                action="store_true",
                help="search: also return cold (archived) Context the default search hides.",
            )
            parser.add_argument("--section")
            parser.add_argument(
                "--view",
                choices=["document", "outline", "provenance", "attachments", "activity"],
                default="document",
                help=(
                    "Use outline first for section ids, titles and sha256 without text. "
                    "attachments lists attachment metadata only; "
                    "activity lists who changed which column value, newest first "
                    "(--after is the number of entries already read)."
                ),
            )
            parser.add_argument("--revision", type=int)
            parser.add_argument("--after", default="")
            parser.add_argument("--order", choices=["id", "created_desc"], default="id")
            parser.add_argument("--request-key")
            parser.add_argument(
                "--input",
                help=(
                    'UTF-8 JSON file; use - for stdin. delete: {"base_revision": N}. '
                    'restore: {"base_revision": N, "restore_revision": M} makes a stale '
                    "version whose sections show only with --revision; add "
                    '"activate": true only when the user restores it as current.'
                    ' revise: {"base_revision": N, "sections": [{"id", "before_sha256", '
                    '"text"}]} replaces whole sections, so read them to the end first '
                    "(truncated: follow next_after); a cut of a 1,000+ character section to "
                    'half or less needs "allow_shrink": ["<section id>"]. To add a line, send '
                    '{"section_appends": [{"id": "<section id>", "text": "<line>"}]} alone: '
                    "no read, no base_revision, no sha256 needed."
                ),
            )


def _sync_connected_skills(configured: dict[str, Any], agents: list[str]) -> dict[str, Any]:
    from durumari_mcp import required_skills
    from durumari_mcp.skill_gateway import SkillGateway

    try:
        gateway = SkillGateway(configured["profile"], "mcp")
        configured_agents: list[str] = configured.get("configured_agents", agents)
        report: dict[str, Any] = {
            "clients": [required_skills.sync(gateway, agent) for agent in configured_agents]
        }
    except Exception as error:
        # The approval and binding have already succeeded. A synchronization failure
        # must retain that connection so the Agent can inspect or retry later.
        code = str(error)
        report = {
            "status": "unavailable",
            "error": code if re.fullmatch(r"[a-z_]{1,80}", code) else "skill_sync_unavailable",
            "recovery_tool": "durumari_skill_sync",
        }
    return {**configured, "required_skills": report}


def selected_workspace(value: str) -> Path:
    try:
        return Path(value).resolve(strict=True)
    except FileNotFoundError:
        # A mistyped or not-yet-created folder; no path is echoed and nothing changes.
        raise AdapterError("workspace_not_found") from None


# Required --input fields; the server still validates the whole request.
REQUIRED_FIELDS = {
    "delete": ("base_revision",),
    "restore": ("base_revision", "restore_revision"),
}


def _carried_agents(root: Path, replaced: str | None, requested: list[str]) -> list[str]:
    """Agents that move from a disabled grant to the new one, after those requested.

    A reconnect from one Agent must not leave the others naming the disabled grant:
    their hooks would report a failure and their MCP tools would not start.
    A live profile keeps its own agents.
    """
    agents = list(requested)
    if replaced:
        try:
            if load_project_profile(replaced).disabled:
                agents = list(dict.fromkeys([*agents, *bound_agents(root, replaced)]))
        except AdapterError:
            pass
    return agents


def connect(args: Any) -> dict[str, Any]:
    # Connect binds exactly the folder the human selected. Upward manifest discovery is
    # only for using an existing binding (serve/status/context); it never makes connect
    # re-enroll or overwrite an ancestor's binding. A selected subfolder gets its own
    # manifest and source, which then stops inheritance from the parent.
    root = selected_workspace(args.workspace)
    opencode_format = getattr(args, "opencode_config", "auto")
    if not root.is_dir():
        raise AdapterError("workspace_directory_required")
    manifest = read_manifest(root) if manifest_path(root).exists() else {}
    server = args.server or manifest.get("server") or "https://app.durumari.cloud"
    project = args.project or manifest.get("project_id")
    name = shared_profile(server, "enrollment", root)
    store = os_store()  # Confirm an approved OS credential store before opening a browser.
    reconcile(root)
    folder_id = getattr(args, "folder_source", None) or manifest.get("folder_source_id")
    # An explicit or bound folder source never needs (or waits on) a Git lookup.
    remote = None if folder_id is not None else git_remote(root)
    if folder_id is not None:
        folder_identity(folder_id)
    elif remote is None:
        if manifest.get("repository_id") or bound_profile(root, manifest):
            # An existing Git binding keeps the Git contract: a failed Git lookup (no git,
            # timeout, origin removed) must not silently switch it to a new folder source.
            # Switching is only allowed explicitly with --folder-source.
            raise AdapterError("repository_root_and_remote_required")
        folder_id = store.get_password(ENROLLMENT_SERVICE, name + "-folder")
        if folder_id is None:
            folder_id = str(uuid4())
            store.set_password(ENROLLMENT_SERVICE, name + "-folder", folder_id)
    source_identity = folder_identity(folder_id) if folder_id else repository_identity(root)
    existing = bound_profile(root, manifest)
    if existing:
        try:
            profile = load_project_profile(existing)
            if (
                profile.context_only
                and not profile.disabled
                and same_server(profile.gateway_origin, server)
                and profile.project_id == project
                and profile.folder_source_id == folder_id
            ):
                profile.check_workspace(str(root))
                # Complete a lost finish reply using the protected enrollment journal.
                secret = store.get_password(ENROLLMENT_SERVICE, name)
                if secret:
                    try:
                        ProjectTransport(server).request(
                            "/api/v2/agent/enrollments/finish", body={"device_token": secret}
                        )
                    except ContextFailure as error:
                        if error.status != 410:
                            raise
                    store.delete_password(ENROLLMENT_SERVICE, name)
                os.chdir(root)
                try:
                    gateway = ProjectGateway(existing, "mcp")
                    current = gateway.context_request("/api/v2/agent/connection")
                    if (
                        current.get("id"),
                        current.get("project_id"),
                        current.get("repository_id"),
                        current.get("workspace_identity"),
                    ) != (
                        profile.connection_id,
                        profile.project_id,
                        profile.repository_id,
                        profile.workspace_identity,
                    ):
                        raise AdapterError("project_connection_scope_changed")
                except ContextFailure as error:
                    if error.status not in {401, 403, 410}:
                        raise
                    disable(existing, root)
                except AdapterError as error:
                    # The v1 token refresh reports a revoked or expired grant this way;
                    # an outage raises a different code and keeps the grant.
                    if str(error) != "project_access_denied":
                        raise
                    disable(existing, root)
                else:
                    return _sync_connected_skills(
                        install_binding(
                            profile, existing, args.agents, opencode_format=opencode_format
                        ),
                        args.agents,
                    )
        except AdapterError as error:
            # A changed remote, a moved or copied folder, or a worktree keeps the old grant
            # and asks for a new browser approval; nothing is inherited from the old scope.
            if str(error) not in {
                "project_credential_missing",
                "project_repository_mismatch_reconnect",
                "workspace_out_of_scope",
            }:
                raise
    transport = ProjectTransport(server)
    config = transport.request("/api/v2/agent/config")
    if folder_id and config.get("folder_sources") is not True:
        raise AdapterError("server_upgrade_required_for_folder_sources")
    origin = browser_origin(config.get("browser_origin"), server)
    secret = store.get_password(ENROLLMENT_SERVICE, name)
    if secret is None:
        secret = secrets.token_urlsafe(32)
        store.set_password(ENROLLMENT_SERVICE, name, secret)
    body = {
        "device_token": secret,
        "client": "mcp",
        **({"folder_source_id": folder_id} if folder_id else {"repository_remote": remote}),
        "workspace_identity": workspace_identity(root),
        "workspace_label": root.name[:100],
        "project_id": project,
    }
    try:
        request = transport.request("/api/v2/agent/enrollments", body=body)
    except ContextFailure as error:
        # An expired request, or one left by an earlier run for another project or
        # remote, cannot be resumed: start a fresh request with a new device secret.
        if (error.status, error.error.get("code")) not in {
            (410, "enrollment_expired"),
            (409, "enrollment_scope_changed"),
        }:
            raise
        secret = secrets.token_urlsafe(32)
        store.set_password(ENROLLMENT_SERVICE, name, secret)
        request = transport.request(
            "/api/v2/agent/enrollments", body={**body, "device_token": secret}
        )
    request_id = request.get("request_id", "")
    if (
        not re.fullmatch(r"[a-f0-9-]{36}", request_id)
        or request.get("approval_path") != "/agent/approve?request=" + request_id
    ):
        raise AdapterError("invalid_browser_login_request")
    url = origin + request["approval_path"]
    print("브라우저에서 구름·프로젝트·작업 소스를 확인하고 연결을 승인해 주세요.", file=sys.stderr)
    print(url, file=sys.stderr, flush=True)
    webbrowser.open(url)
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        result = transport.request("/api/v2/agent/enrollments/poll", body={"device_token": secret})
        if result.get("status") == "approved":
            tokens = result["tokens"]
            connection = tokens["connection"]
            selected_folder = result.get("folder_source_id", folder_id)
            if folder_id:
                if not isinstance(selected_folder, str):
                    raise AdapterError("enrollment_scope_changed")
                selected_identity = folder_identity(selected_folder)
            else:
                if selected_folder is not None:
                    raise AdapterError("project_connection_scope_changed")
                selected_identity = source_identity
            if (
                connection["host"] != "mcp"
                or set(connection["actions"]) != {"context:read", "context:write"}
                or result.get("repository_id") != selected_identity
                or result.get("workspace_identity") != workspace_identity(root)
            ):
                raise AdapterError("project_connection_scope_changed")
            name = (
                shared_profile(server, connection["project_id"], root) + "-" + connection["id"][:8]
            )
            prior = None
            try:
                prior = load_project_profile(name)
            except AdapterError as error:
                if str(error) != "project_credential_missing":
                    raise
            profile = ProjectProfile(
                gateway_origin=server,
                project_id=connection["project_id"],
                source_id=connection["source_id"],
                actor_id=connection["actor_id"],
                connection_id=connection["id"],
                client="mcp",
                workspace=str(root),
                repository_id=result["repository_id"],
                folder_source_id=selected_folder,
                workspace_identity=result["workspace_identity"],
                context_only=True,
                capture_enabled=False,
                access_token=SecretStr(tokens["access_token"]),
                refresh_token=SecretStr(tokens["refresh_token"]),
                access_expires_at=datetime.fromisoformat(tokens["access_expires_at"]),
                outbox_key=prior.outbox_key
                if prior is not None
                else SecretStr(Fernet.generate_key().decode()),
            )
            agents = _carried_agents(root, existing, args.agents)
            # Validate settings before storing credentials, then apply; a lost finish reply
            # never discards a durably stored profile or requires another approval.
            install_binding(
                profile,
                name,
                agents,
                apply=False,
                approved_manifest=manifest or None,
                opencode_format=opencode_format,
            )
            save_project_profile(name, profile)
            configured = install_binding(
                profile,
                name,
                agents,
                approved_manifest=manifest or None,
                opencode_format=opencode_format,
            )
            if selected_folder:
                store.set_password(
                    ENROLLMENT_SERVICE,
                    shared_profile(server, "enrollment", root) + "-folder",
                    selected_folder,
                )
            transport.request("/api/v2/agent/enrollments/finish", body={"device_token": secret})
            store.delete_password(ENROLLMENT_SERVICE, shared_profile(server, "enrollment", root))
            return _sync_connected_skills(configured, agents)
        if result.get("status") != "pending":
            # A denied request is final; the next run must ask for a new approval.
            store.delete_password(ENROLLMENT_SERVICE, shared_profile(server, "enrollment", root))
            raise AdapterError("project_connection_cancelled")
        time.sleep(2)
    raise AdapterError("browser_approval_pending")


def execute(args: Any) -> None:
    if args.command == "context-connect":
        _emit_json(connect(args))
        return
    cwd = selected_workspace(args.workspace)
    if args.command == "context-remove":
        # Destructive: never resolve upward to an ancestor's binding. Remove only the
        # binding whose root is exactly the selected folder.
        if binding_root(cwd) != cwd:
            raise AdapterError("remove_requires_binding_root")
        root = cwd
    else:
        # Read/use only: the nearest approved binding; check_workspace below still
        # requires cwd to stay inside it without crossing a manifest or Git boundary.
        root = binding_root(cwd)
    if args.command == "context-status" and getattr(args, "change", None):
        # A planned change is predicted from local metadata only; no credential is opened.
        _emit_json(change_preflight(root, args.change))
        return
    name = args.profile or (
        bound_profile(root, read_manifest(root)) if manifest_path(root).exists() else None
    )
    if args.command == "context-remove" and not name:
        name = local_profile(root)
        if not name:
            _emit_json({"status": "not_connected", "server_revocation": "not_pending"})
            return
    if not name:
        raise AdapterError("project_credential_missing")
    profile = load_project_profile(name)
    if args.command == "context-remove":
        _emit_json(remove(name, root))
        return
    profile.check_workspace(str(cwd))
    # An older durumari.yaml layout is converted once this grant is verified. A failed
    # write leaves the older layout, which this runtime still reads.
    try:
        migrate_manifest(root, name, profile.workspace_identity)
    except OSError:
        pass
    # The caller selects an explicit approved workspace; MCP additionally verifies
    # the launch directory at every operation, including nested-repository checks.
    if args.command != "context-serve":
        os.chdir(cwd)
    gateway = ProjectGateway(name, args.client)
    transport = getattr(gateway, "transport", None)
    if transport is not None:
        transport.via = "mcp" if args.command == "context-serve" else "cli"
    session = plugin_env.get(plugin_env.SESSION_ID) or f"process-{os.getpid()}"
    client = ContextClient(gateway, session)
    if args.command == "context-execution":
        from durumari_mcp.context_execution import run_worker

        if run_worker(client, gateway.profile.project_id, gateway.profile.connection_id,
                      args.client, sys.stdin, sys.stdout):
            raise AdapterError("required_current_context_unavailable")
        return
    if args.command == "context-serve":
        gateway.profile.check_workspace(os.getcwd())
        before_tool: Callable[[], None] | None = None
        # The launcher policy holds private queries and Context ids, so it arrives in the
        # owner-only environment (never argv, which any local user can read via ps).
        execution_policy = plugin_env.pop(plugin_env.EXECUTION_POLICY)
        if execution_policy is not None:
            from durumari_mcp.context_execution import ExecutionGuard
            from durumari_mcp.context_guard_read import GuardBlocked

            try:
                if len(execution_policy) > 16384:
                    raise ValueError
                gate = ExecutionGuard.from_launcher_policy(
                    client, gateway.profile.project_id, gateway.profile.connection_id,
                    args.client, json.loads(execution_policy),
                )
            except (ValueError, TypeError, GuardBlocked):
                raise AdapterError("required_current_context_unavailable") from None
            def check_execution() -> None:
                gate.check(gate.scope)

            before_tool = check_execution
        server = create_context_server(client, before_tool=before_tool)
        hide_unavailable(server, HiddenFeatures(client.release))
        serve_stdio(server)
        return
    if args.command == "context-status":
        _emit_json(client.status())
        return
    action = args.action
    identifier = args.id
    if identifier is not None and not re.fullmatch(r"[A-Za-z0-9-]{1,64}", identifier):
        raise AdapterError("invalid_context_identifier")
    # Name the missing argument instead of a generic invalid_context_request.
    if not identifier and action in {
        "read", "history", "revise", "delete", "restore", "sprint-close",
    }:
        raise AdapterError("context_id_required")
    if not args.query and action == "search":
        raise AdapterError("context_query_required")
    if not args.request_key and action in {
        "operation", "save", "revise", "delete", "restore", "sprint-close",
    }:
        raise AdapterError("context_request_key_required")
    if not args.input and action in {"save", "revise", "delete", "restore"}:
        raise AdapterError("context_input_required")
    if action == "list":
        result = client.read(after=args.after, order=args.order)
    elif action == "search":
        result = client.read(
            "/search",
            q=args.query,
            after=int(args.after or 0),
            include_archived="true" if getattr(args, "include_archived", False) else None,
        )
    elif action == "read" and identifier and args.view == "attachments":
        if args.section or args.after:
            raise AdapterError("invalid_reference_selection")
        result = client.read("/" + identifier + "/attachments", revision=args.revision)
    elif action == "read" and identifier and args.view == "activity":
        if args.section or args.revision:
            raise AdapterError("invalid_reference_selection")
        result = client.read("/" + identifier + "/activity", after=int(args.after or 0))
    elif action == "read" and identifier:
        result = client.read(
            "/" + identifier,
            section_id=args.section,
            revision=args.revision,
            after=args.after,
            view=args.view,
        )
    elif action == "history" and identifier:
        result = client.read("/" + identifier + "/history", after=int(args.after or 0))
    elif action == "sprint-close" and identifier and args.request_key:
        # Moves the sprint's unfinished documents on, marks it closed and drafts a closing
        # report; --input is optional JSON {"next_sprint_id": id-or-key, "report": bool}.
        body: Any = {}
        if args.input:
            if args.input == "-":
                raw = sys.stdin.buffer.read(262145)
            else:
                raw = Path(args.input).read_bytes()
            body = json.loads(raw) if len(raw) <= 262144 else None
        if not isinstance(body, dict):
            raise AdapterError("invalid_context_request")
        result = gateway.context_request(
            client.prefix + "/" + identifier + "/sprint-close",
            body,
            method="POST",
            idempotency_key=args.request_key,
        )
    elif action == "operation" and args.request_key:
        result = client.operation(args.request_key)
    elif action in {"save", "revise", "delete", "restore"} and args.request_key and args.input:
        raw = sys.stdin.buffer.read(262145) if args.input == "-" else Path(args.input).read_bytes()
        if len(raw) > 262144:
            raise AdapterError("context_request_too_large")
        body = json.loads(raw)
        if not isinstance(body, dict) or (action != "save" and not identifier):
            raise AdapterError("invalid_context_request")
        if any(
            type(body.get(field)) is not int or body[field] < 1
            for field in REQUIRED_FIELDS.get(action, ())
        ):
            raise AdapterError("context_input_fields_required")
        method = {"save": "POST", "revise": "PATCH", "delete": "DELETE", "restore": "POST"}[action]
        suffix = (
            ""
            if action == "save"
            else "/" + identifier + ("/restore" if action == "restore" else "")
        )
        result = client.mutate(method, suffix, body, args.request_key)
        if (
            action == "restore"
            and result.get("status") == "stored"
            and body.get("activate") is not True
        ):
            # Intended: a restore without activate is a new stale version with no current
            # effect, so the default document view shows no sections (contract section 7).
            result = {
                **result,
                "state": "stale",
                "next_action": "read_with_revision_or_restore_with_activate",
            }
    else:
        raise AdapterError("invalid_context_request")
    _emit_json(result)
