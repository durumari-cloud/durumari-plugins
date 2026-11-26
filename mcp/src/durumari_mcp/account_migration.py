"""Explicit, encrypted and resumable retirement of one owned folder MCP entry.

An ordinary runtime update never calls this module. Plans touch no folder files.
Only an account MCP tool's successful, authorized read unlocks apply. Credentials,
outboxes and other hosts remain in their original scope. Shared instructions use
the common generator, never a personal account-specific replacement.
"""

import json
import re
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from cryptography.fernet import Fernet, InvalidToken
from mcp.types import ToolAnnotations

from durumari_mcp import account_setup, context_binding, instruction_files
from durumari_mcp.account_client import AccountContextClient
from durumari_mcp.account_credentials import load_account_profile
from durumari_mcp.account_setup import atomic, checked, digest
from durumari_mcp.credentials import AdapterError
from durumari_mcp.local_upgrade import user_data_directory
from durumari_mcp.private_files import protect_file
from durumari_mcp.project_credentials import load_project_profile
from durumari_mcp.project_outbox import process_lock, state_root
from durumari_mcp.server_aliases import same_server

CLIENTS = {"codex": "codex", "claude-code": "claude", "cursor": "cursor"}
RECORD = ".durumari/context-binding.json"
ALLOWED = {
    RECORD,
    ".codex/config.toml",
    ".codex/hooks.json",
    ".mcp.json",
    ".claude/settings.local.json",
    ".cursor/mcp.json",
    "AGENTS.md",
    "CLAUDE.md",
    "GEMINI.md",
}


def _directory(client: str, profile: str) -> Path:
    if client not in CLIENTS:
        raise AdapterError("account_setup_client_unsupported")
    # Reuse profile validation and secure ancestor checks.
    from durumari_mcp.credentials import profile_name

    return checked(user_data_directory() / "account-migrations" / client / profile_name(profile))


def _path(mid: str, client: str, profile: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{32}", mid):
        raise AdapterError("invalid_account_migration_id")
    return checked(_directory(client, profile) / (mid + ".journal"))


def _save(row: dict[str, Any]) -> None:
    path = _path(row["migration_id"], row["client"], row["account_profile"])
    account = load_account_profile(row["account_profile"])
    encrypted = Fernet(account.outbox_key.get_secret_value().encode()).encrypt(
        json.dumps(row, ensure_ascii=False).encode()
    )
    atomic(path, encrypted)
    protect_file(path)


def _load(mid: str, client: str, profile: str) -> dict[str, Any]:
    path = _path(mid, client, profile)
    try:
        account = load_account_profile(profile)
        row = json.loads(
            Fernet(account.outbox_key.get_secret_value().encode()).decrypt(path.read_bytes())
        )
        if (row["migration_id"], row["client"], row["account_profile"]) != (mid, client, profile):
            raise ValueError
        root = Path(row["workspace"])
        checked(root)
        if not root.is_absolute() or not root.is_dir():
            raise ValueError
        for item in row["files"]:
            if item["path"] not in ALLOWED:
                raise ValueError
            checked(root / item["path"])
        return dict(row)
    except (OSError, ValueError, KeyError, TypeError, InvalidToken):
        raise AdapterError("account_migration_recovery_required") from None


def _identity(row: dict[str, Any]) -> Any:
    account = load_account_profile(row["account_profile"])
    legacy = load_project_profile(row["legacy_profile"])
    if (
        (account.connection_id, account.actor_id, account.client, account.gateway_origin)
        != (row["account_connection"], row["actor_id"], row["client"], row["server"])
        or (legacy.connection_id, legacy.actor_id, legacy.project_id, legacy.workspace)
        != (row["legacy_connection"], row["actor_id"], row["project_id"], row["workspace"])
        # A folder bound before the move and an account made after it name one server.
        or not same_server(legacy.gateway_origin, row["server"])
    ):
        raise AdapterError("account_migration_identity_changed")
    return account


def pending_writes(name: str) -> Any:
    from durumari_mcp.context_outbox import ContextOutbox

    return ContextOutbox(state_root(), name, load_project_profile(name)).pending_status(None)


def _no_pending_writes(name: str) -> None:
    """Retiring the folder MCP must not strand its unconfirmed Context writes.

    They stay in the folder's own scope (never resent to the account); the folder
    connection delivers or recovers them before the transition is planned or applied.
    """
    pending = pending_writes(name)
    if pending.count or pending.recovery_required:
        raise AdapterError("account_migration_pending_writes")


def _host_hash(client: str, profile: str, origin: str) -> str:
    path, directory = account_setup.paths(client, Path.home(), user_data_directory())
    _, _, servers = account_setup.read_configuration(path, client)
    installation = account_setup.record(directory / "installed.json")
    if not account_setup.owned(
        servers.get(account_setup.SERVER), installation, client, profile, origin
    ):
        raise AdapterError("account_migration_account_setup_required")
    # Hosts update session counters and trust state independently. Pin only the
    # owned MCP entry; owned() still rechecks installation/client/profile/origin.
    return digest(json.dumps(servers[account_setup.SERVER], sort_keys=True).encode())


def _view(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row[key] for key in ("migration_id", "state", "client", "workspace", "project_id")
    } | {
        "files": [
            {
                "path": f["path"],
                "before_sha256": digest(f["before"].encode()),
                "after_sha256": digest(f["after"].encode()),
            }
            for f in row["files"]
        ],
        "credentials": "retained_in_original_scope",
        "pending_requests": "retained_in_original_scope",
        "shared_instructions": "generic_managed_blocks_only_review_tracked_changes",
        "state_observation": "journal_only",
        "next_action": (
            "restart_host_and_verify_single_account_tools"
            if row["state"] == "completed"
            else "call_durumari_account_migration_verify_in_account_host"
        ),
    }


def plan(root: Path, client: str, account_profile: str) -> dict[str, Any]:
    """Plan only the selected known folder, never scan a disk or infer ownership."""
    if client not in CLIENTS:
        raise AdapterError("account_setup_client_unsupported")
    if not root.is_absolute():
        raise AdapterError("explicit_absolute_workspace_required")
    checked(root)
    if not root.is_dir():
        raise AdapterError("workspace_directory_required")
    root = root.resolve()
    record = context_binding.json_file(root, RECORD)
    name = record.get("profile")
    if not isinstance(name, str):
        raise AdapterError("account_migration_binding_ownership_required")
    account = load_account_profile(account_profile)
    legacy = load_project_profile(name)
    row: dict[str, Any] = {
        "migration_id": uuid4().hex,
        "state": "planned",
        "client": client,
        "account_profile": account_profile,
        "account_connection": account.connection_id,
        "actor_id": account.actor_id,
        "legacy_profile": name,
        "legacy_connection": legacy.connection_id,
        "project_id": legacy.project_id,
        "server": account.gateway_origin,
        "workspace": str(root),
        "files": [],
        "host_sha256": _host_hash(client, account_profile, account.gateway_origin),
    }
    _identity(row)
    _no_pending_writes(name)
    agent = CLIENTS[client]
    path_name = ".codex/config.toml" if agent == "codex" else context_binding.JSON_FILES[agent][0]
    path = checked(root / path_name)
    if not path.is_file():
        raise AdapterError("account_migration_binding_ownership_required")
    before = path.read_bytes().decode("utf-8")
    if agent == "codex":
        after = context_binding.remove_codex_block(before, profile=name)
    else:
        current = context_binding.json_file(root, path_name)
        entry = current.get("mcpServers", {}).get(context_binding.SERVER)
        if not context_binding.owned(entry, name):
            raise AdapterError("account_migration_binding_ownership_required")
        # Claude and Copilot share this file. Do not remove another host's entry.
        if agent == "claude" and "copilot" in record.get("agents", []):
            raise AdapterError("account_migration_shared_host_configuration")
        current["mcpServers"].pop(context_binding.SERVER)
        after = context_binding.json_output(root, path_name, current)
    if before == after:
        raise AdapterError("account_migration_binding_ownership_required")
    changes = {path_name: after}
    if agent in {"codex", "claude"}:
        hook_path = ".codex/hooks.json" if agent == "codex" else ".claude/settings.local.json"
        if checked(root / hook_path).exists():
            hook_name, text = context_binding.hook_settings(root, agent, name, remove=True)
            changes[hook_name] = text
    record.setdefault("account_clients", {})[client] = {
        "profile": account_profile,
        "migration_id": row["migration_id"],
    }
    changes[RECORD] = json.dumps(record, ensure_ascii=False) + "\n"
    instructions = {}
    for instruction in instruction_files.INSTRUCTION_FILES:
        source = checked(root / instruction)
        instructions[instruction] = source.read_bytes().decode("utf-8") if source.exists() else None
    try:
        _, block, _ = instruction_files.split(instructions.get("AGENTS.md") or "")
        if block is not None:
            owner = re.search(r"(?m)^Project: ([^\r\n]+)$", block)
            if owner is None or owner[1] != legacy.project_id:
                raise AdapterError("account_migration_instruction_ownership_required")
        generated = instruction_files.upgrade(instructions, legacy.project_id)
    except instruction_files.ManagedTextError:
        raise AdapterError("account_migration_instruction_repair_required") from None
    if any(instructions.get(name) is None for name in generated):
        # A missing shared source may also be a user import target. Ask the normal
        # generator/repository workflow to create and review it before retirement.
        raise AdapterError("account_migration_shared_instruction_generation_required")
    changes.update(generated)
    for name, after in changes.items():
        before = checked(root / name).read_bytes().decode("utf-8")
        # Preserve a uniform CRLF style, including newly rendered comments.
        if "\r\n" in before and "\n" not in before.replace("\r\n", ""):
            after = after.replace("\r\n", "\n").replace("\n", "\r\n")
        if before != after:
            row["files"].append({"path": name, "before": before, "after": after})
    with process_lock(_directory(client, account_profile)):
        _save(row)
    return _view(row)


def verify(mid: str, client: Any) -> dict[str, Any]:
    """Called only by the native account MCP registration, never by CLI apply."""
    with process_lock(_directory(client.client, client.name)):
        row = _load(mid, client.client, client.name)
        _identity(row)
        if row["state"] in {"completed", "rolled_back", "rolling_back"}:
            raise AdapterError("account_migration_verification_not_pending")
        client.status()  # Revalidate the current grant and actor with the server.
        if row["host_sha256"] != _host_hash(client.client, client.name, row["server"]):
            raise AdapterError("account_migration_configuration_changed")
        navigation = client.projects("start", {"request_key": "migration-" + mid})
        selected = client.projects(
            "select",
            {
                "conversation_ref": navigation["conversation_ref"],
                "expected_revision": navigation["revision"],
                "request_key": "migration-select-" + mid,
                "project_id": row["project_id"],
            },
        )
        client.targeted(selected["project_ref"]).read(limit=1)
        row["verified_at"] = time.time()
        if row["state"] != "applying":
            row["state"] = "verified"
        _save(row)
        return _view(row) | {"host_tool_invocation": "observed", "authorized_read": "verified"}


def _change(mid: str, client: str, profile: str, *, reverse: bool) -> dict[str, Any]:
    with process_lock(_directory(client, profile)):
        row = _load(mid, client, profile)
        _identity(row)
        final = "rolled_back" if reverse else "completed"
        if row["state"] == final:
            expected = "before" if reverse else "after"
            if any(
                checked(Path(row["workspace"]) / item["path"]).read_bytes()
                != item[expected].encode()
                for item in row["files"]
            ):
                raise AdapterError("account_migration_configuration_changed")
            return _view(row)
        if not reverse:
            if row["state"] not in {"verified", "applying"}:
                raise AdapterError("account_migration_host_verification_required")
            if row.get("verified_at", 0) < time.time() - 900:
                raise AdapterError("account_migration_host_verification_required")
            if row["host_sha256"] != _host_hash(client, profile, row["server"]):
                raise AdapterError("account_migration_configuration_changed")
            # Host discovery evidence is not a continuing grant. Recheck current
            # account/project access immediately before retiring any folder entry.
            active_account = AccountContextClient(profile, client)
            active_account.status()
            active_account.direct_target(row["project_id"]).read(limit=1)
            _identity(row)
            _no_pending_writes(row["legacy_profile"])
        elif row["state"] not in {"applying", "completed", "rolling_back"}:
            raise AdapterError("account_migration_not_applied")
        root = Path(row["workspace"])
        # Preflight every file first. Interrupted replacements may already match
        # the desired bytes; any third value is a user edit and stops the batch.
        for item in row["files"]:
            current = checked(root / item["path"]).read_bytes()
            if current not in (item["before"].encode(), item["after"].encode()):
                raise AdapterError("account_migration_configuration_changed")
        row["state"] = "rolling_back" if reverse else "applying"
        _save(row)
        for item in reversed(row["files"]) if reverse else row["files"]:
            old, new = (
                (item["after"], item["before"]) if reverse else (item["before"], item["after"])
            )
            path = checked(root / item["path"])
            if path.read_bytes() != new.encode():
                atomic(path, new.encode(), expected=digest(old.encode()))
            # Persist a resume point after each replacement; a crash before this
            # checkpoint is recovered by comparing actual bytes on the next run.
            row["last_file"] = item["path"]
            _save(row)
        row["state"] = final
        if reverse:
            row.pop("verified_at", None)
        _save(row)
        return _view(row)


def apply(mid: str, client: str, profile: str) -> dict[str, Any]:
    return _change(mid, client, profile, reverse=False)


def rollback(mid: str, client: str, profile: str) -> dict[str, Any]:
    return _change(mid, client, profile, reverse=True)


def status(mid: str, client: str, profile: str) -> dict[str, Any]:
    return _view(_load(mid, client, profile))


def register(server: Any, client: Any) -> None:
    """Expose verification solely through the configured native account server."""
    from durumari_mcp.cloud_protocol import safe

    def durumari_account_migration_verify(migration_id: str) -> Any:
        """Verify a planned folder transition with this account's current allowed read.

        Use only the migration_id from the person's explicit account-migrate plan.
        This does not change folder files or grant permissions. After this succeeds,
        apply the reviewed plan and restart the host to remove duplicate tools.
        """
        return safe(lambda: verify(migration_id, client))

    server.add_tool(
        durumari_account_migration_verify,
        annotations=ToolAnnotations(
            read_only_hint=False,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
    )
