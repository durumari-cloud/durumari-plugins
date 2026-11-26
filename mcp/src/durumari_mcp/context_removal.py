"""Durable, value-free removal metadata; credentials stay in the OS store."""

import json
from pathlib import Path
from typing import Any

from durumari_mcp.context_binding import json_file, remove_binding, workspace_identity, write_files
from durumari_mcp.context_outbox import ContextOutbox
from durumari_mcp.credentials import AdapterError, profile_name
from durumari_mcp.legacy import manifest_path
from durumari_mcp.onboarding import read_manifest
from durumari_mcp.project_credentials import (
    ProjectProfile,
    load_project_profile,
    save_project_profile,
)
from durumari_mcp.project_gateway import ContextFailure, ProjectTransport
from durumari_mcp.project_outbox import process_lock, protected_directory, state_root
from durumari_mcp.server_aliases import same_server


def marker(root: Path) -> Path:
    directory = state_root() / "removals"
    protected_directory(directory)
    return directory / (workspace_identity(root) + ".json")


def pending_profile(root: Path) -> str | None:
    path = marker(root)
    if path.exists() and path.stat().st_size > 4096:
        raise AdapterError("project_connection_scope_changed")
    value = json_file(path.parent, path.name)
    if not value:
        return None
    if value.get("workspace_identity") != workspace_identity(root):
        raise AdapterError("project_connection_scope_changed")
    name = value.get("profile")
    if not isinstance(name, str):
        raise AdapterError("project_connection_scope_changed")
    return profile_name(name)


def local_profile(root: Path) -> str | None:
    pending = pending_profile(root)
    if pending:
        return pending
    record = json_file(root, ".durumari/context-binding.json")
    if not record:
        return None
    name = record.get("profile")
    if not isinstance(name, str):
        raise AdapterError("project_connection_scope_changed")
    return profile_name(name)


def removal_scope(profile: ProjectProfile, root: Path) -> None:
    if Path(profile.workspace).resolve() != root:
        raise AdapterError("workspace_out_of_scope")
    if profile.context_only and profile.workspace_identity != workspace_identity(root):
        raise AdapterError("workspace_out_of_scope")
    # Revoking our own grant remains possible after a Git remote rename. Local
    # metadata and each managed entry still fence removal to this exact grant.
    if manifest_path(root).exists():
        value = read_manifest(root)
        # A teammate's committed source_id must not block removing our own grant.
        if (
            not same_server(value["server"], profile.gateway_origin)
            or value["project_id"] != profile.project_id
        ):
            raise AdapterError("project_connection_scope_changed")


def disable(name: str, root: Path) -> ProjectProfile:
    # Serialize with credential rotation only. No ordinary Context request waits here.
    with process_lock(state_root() / (name + ".credentials"), timeout=25):
        profile = load_project_profile(name)
        removal_scope(profile, root)
        profile = profile.model_copy(update={"disabled": True})
        save_project_profile(name, profile)
    ContextOutbox(state_root(), name, profile).stop()
    return profile


def finish_revocation(name: str, profile: ProjectProfile, root: Path) -> bool:
    if not profile.disabled or Path(profile.workspace).resolve() != root:
        raise AdapterError("project_connection_scope_changed")
    removal_scope(profile, root)
    try:
        ProjectTransport(profile.gateway_origin).request(
            "/api/v2/agent/revoke",
            body={"refresh_token": profile.refresh_token.get_secret_value()},
        )
    except (AdapterError, ContextFailure):
        return False
    path = marker(root)
    if pending_profile(root) == name:
        path.unlink(missing_ok=True)
    return True


# Removal ends only this folder's grant; the user is told what stays (contract section 8).
KEPT = [
    "server_project_and_context",
    "plugin",
    "shared_runtime",
    "other_folder_connections",
    "encrypted_queue_and_quarantine",
]


def remove(name: str, root: Path) -> dict[str, Any]:
    prior = load_project_profile(name)
    removal_scope(prior, root)
    path = marker(root)
    write_files(
        path.parent,
        {
            path.name: json.dumps(
                {
                    "profile": profile_name(name),
                    "workspace_identity": workspace_identity(root),
                }
            )
            + "\n"
        },
    )
    profile = disable(name, root)
    # A retry after local removal uses the marker instead of the deleted manifest.
    record = json_file(root, ".durumari/context-binding.json")
    local = remove_binding(profile, name) if record else {"removed": [], "restored": []}
    confirmed = finish_revocation(name, profile, root)
    return {
        "status": "removed" if confirmed else "removed_locally",
        "server_revocation": "confirmed" if confirmed else "pending",
        "local_files": local,
        # The team's durumari.yaml is shared and may be committed; removal keeps it and says so.
        "kept": KEPT + (["shared_project_manifest"] if manifest_path(root).exists() else []),
    }


def reconcile(root: Path) -> None:
    name = pending_profile(root)
    if name and remove(name, root)["server_revocation"] != "confirmed":
        raise AdapterError("project_removal_pending_retry")
