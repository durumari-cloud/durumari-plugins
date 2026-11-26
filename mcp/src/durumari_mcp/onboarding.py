"""Repository bootstrap and portable, reviewed document migration."""

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

from durumari_mcp.credentials import AdapterError, profile_name
from durumari_mcp.legacy import (
    MANAGED_END_V1,
    MANAGED_START_V1,
    manifest_path,
)
from durumari_mcp.local_upgrade import MANIFEST_SCHEMA_VERSION
from durumari_mcp.project_credentials import ProjectProfile

START, END = "<!-- DURUMARI MANAGED BEGIN -->", "<!-- DURUMARI MANAGED END -->"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def inside(root: Path, path: Path) -> Path:
    if path.is_symlink() or any(
        parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents
    ):
        raise AdapterError("symlink_not_imported")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        raise AdapterError("path_out_of_workspace") from None
    return path


def read_manifest(root: Path) -> dict[str, Any]:
    """Validate public binding metadata without granting repository access.

    Only local diagnostics and human-approved connect may use this reader.
    All Context and capture entrypoints must use load_manifest/check_binding.
    """
    try:
        path = inside(root, manifest_path(root))
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if (
            isinstance(value, dict)
            and type(value.get("schema_version")) is int
            and value["schema_version"] > MANIFEST_SCHEMA_VERSION
        ):
            # A teammate's newer runtime wrote this layout; this one must update first.
            raise AdapterError("client_update_required")
        if (
            not isinstance(value, dict)
            or set(value)
            - {"repository_id", "context_mode", "workspace_identity", "profile", "folder_source_id"}
            != {"schema_version", "server", "project_id", "source_id", "context_source"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != MANIFEST_SCHEMA_VERSION
            or value["context_source"] != "server"
        ):
            raise ValueError
        ProjectProfile.origin(value["server"])
        for key in ("project_id", "source_id"):
            if not isinstance(value[key], str) or not re.fullmatch(
                r"[A-Za-z0-9-]{1,128}", value[key]
            ):
                raise ValueError
        if "repository_id" in value and (
            not isinstance(value["repository_id"], str)
            or not re.fullmatch(r"[0-9a-f]{64}", value["repository_id"])
        ):
            raise ValueError
        if "context_mode" in value and value["context_mode"] != "cloud-context":
            raise ValueError
        # Legacy manifests also carry both per-user keys; the shared form has neither.
        legacy = [key for key in ("workspace_identity", "profile") if key in value]
        if legacy:
            if len(legacy) != 2 or "context_mode" not in value:
                raise ValueError
            if not isinstance(value["workspace_identity"], str) or not re.fullmatch(
                r"[a-f0-9]{64}", value["workspace_identity"]
            ):
                raise ValueError
            profile_name(value["profile"])
        if "folder_source_id" in value:
            from durumari_mcp.workspace_binding import folder_identity

            if value.get("context_mode") != "cloud-context" or value.get(
                "repository_id"
            ) != folder_identity(value["folder_source_id"]):
                raise ValueError
        return value
    except (OSError, ValueError, TypeError, yaml.YAMLError):
        raise AdapterError("invalid_project_manifest") from None


def load_manifest(root: Path) -> dict[str, Any]:
    value = read_manifest(root)
    from durumari_mcp.workspace_binding import folder_identity

    identity = (
        folder_identity(value["folder_source_id"])
        if value.get("folder_source_id")
        else repository_identity(root)
    )
    if "repository_id" in value and value["repository_id"] != identity:
        raise AdapterError("project_repository_mismatch_reconnect")
    if value.get("workspace_identity"):  # Legacy form; the OS profile checks it otherwise.
        from durumari_mcp.context_binding import workspace_identity

        if value["workspace_identity"] != workspace_identity(root):
            raise AdapterError("workspace_out_of_scope")
    return value


def repository_diagnostics(root: Path) -> dict[str, Any]:
    manifest = read_manifest(root)
    from durumari_mcp.workspace_binding import folder_identity

    current = (
        folder_identity(manifest["folder_source_id"])
        if manifest.get("folder_source_id")
        else repository_identity(root)
    )
    matches = manifest.get("repository_id", current) == current
    return {
        "project_id": manifest["project_id"],
        "server": manifest["server"],
        "repository_matches": matches,
        "bound_repository_id": manifest.get("repository_id"),
        "current_repository_id": current,
        "ready": False,
        "reason": (
            "live_access_not_verified" if matches else "project_repository_mismatch_reconnect"
        ),
        "scope": "local_diagnostics_only",
        "context_available": False,
        "capture_paused": not matches,
        "pending_records": "preserved_not_inspected",
        "next_action": "verify_live_access" if matches else "browser_reconnect_then_reload_host",
    }


CHANGES = ("git-init", "move", "detach")


def change_preflight(root: Path, change: str) -> dict[str, Any]:
    """Predict whether a planned folder or Git change needs browser reconnection."""
    if change not in CHANGES:
        raise AdapterError("invalid_command_arguments")
    result = repository_diagnostics(root)
    if change == "git-init":
        predicted = (
            result["current_repository_id"]
            if (root / ".git").exists()
            else digest(("local:" + str(root / ".git")).encode())
        )
        reconnect = predicted != result["bound_repository_id"]
    else:
        predicted = None
        reconnect = True  # Workspace scope changes even if a remote stays the same.
    result.update(
        change=change,
        predicted_repository_id=predicted,
        reconnect_required=reconnect or not result["repository_matches"],
        recovery_plan=[
            "preserve_connection_files_and_pending_records",
            "prepare_final_folder_and_git_structure",
            "browser_approve_existing_project_connection",
            "verify_status_reload_host_then_resume",
        ],
    )
    return result


def repository_identity(root: Path) -> str:
    """Bind copied manifests to a repository, while allowing clones/worktrees.

    Store only a hash of a credential-free remote or a local repository path.
    Multiple repositories can still explicitly enroll in the same server project.
    """

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
        ).strip()

    try:
        remote = git("config", "--get", "remote.origin.url")
        if "://" in remote:
            parsed = urlsplit(remote)
            identity = (
                (parsed.hostname or "")
                + (":" + str(parsed.port) if parsed.port else "")
                + parsed.path
            )
        else:
            identity = remote.split("@", 1)[-1].replace(":", "/", 1)
        identity = "remote:" + identity.rstrip("/").removesuffix(".git")
    except (subprocess.CalledProcessError, OSError, ValueError):
        try:
            identity = "local:" + str((root / git("rev-parse", "--git-common-dir")).resolve())
        except (subprocess.CalledProcessError, OSError, ValueError):
            identity = "local:" + str(root.resolve())
    return digest(identity.encode())


def managed(previous: str, block: str) -> str:
    previous = previous.replace(MANAGED_START_V1, START).replace(MANAGED_END_V1, END)
    if START in previous or END in previous:
        if (
            previous.count(START) != 1
            or previous.count(END) != 1
            or previous.index(START) > previous.index(END)
        ):
            raise AdapterError("invalid_managed_instructions")
        return (
            previous[: previous.index(START)] + block + previous[previous.index(END) + len(END) :]
        )
    return previous.rstrip() + "\n\n" + block + "\n"


def _codex_interrupt_timeout(hook: dict[str, Any], event: str) -> bool:
    return (
        event == "Interrupt"
        and "commandWindows" in hook
        and "-m durumari_mcp project-hook " in str(hook.get("command", ""))
        and type(hook.get("timeout")) in {int, float}
        and hook["timeout"] > 3
    )


def _hook_execution(group: Any, event: str) -> Any:
    """Compare execution definitions without labels or host enablement flags."""
    if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
        return group
    result = {key: value for key, value in group.items() if key not in {"enabled", "disabled"}}
    result["hooks"] = [
        {
            key: value
            for key, value in hook.items()
            if key not in {"statusMessage", "enabled", "disabled"}
        }
        if isinstance(hook, dict)
        else hook
        for hook in group["hooks"]
    ]
    for hook in result["hooks"]:
        if isinstance(hook, dict) and _codex_interrupt_timeout(hook, event):
            hook["timeout"] = 3  # Codex already applies this deadline to older definitions.
    return result


def merge_hooks(current: dict[str, Any], hooks: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = json.loads(json.dumps(current))
    existing = result.setdefault("hooks", {})
    if not isinstance(existing, dict):
        raise AdapterError("invalid_existing_hooks")
    for event, groups in hooks["hooks"].items():
        old = existing.setdefault(event, [])
        if not isinstance(old, list):
            raise AdapterError("invalid_existing_hooks")
        for group in groups:
            match = next(
                (
                    item
                    for item in old
                    if _hook_execution(item, event) == _hook_execution(group, event)
                ),
                None,
            )
            if match is None:
                old.append(json.loads(json.dumps(group)))
                continue
            for previous, incoming in zip(match["hooks"], group["hooks"], strict=True):
                if incoming.get("timeout") == 3 and _codex_interrupt_timeout(previous, event):
                    previous["timeout"] = 3
                label = previous.get("statusMessage")
                if (label is None or isinstance(label, str) and not label.strip()) and incoming.get(
                    "statusMessage"
                ):
                    previous["statusMessage"] = incoming["statusMessage"]
    return result
