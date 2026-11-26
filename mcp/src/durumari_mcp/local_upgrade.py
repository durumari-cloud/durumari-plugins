"""Local settings follow the plugin version without leaving files in the user's repository.

User device layer: ``durumari.yaml`` carries a schema version and the
runtime converts an older layout when it runs; a newer layout asks for an update instead
of failing. Backups of files the binding changes live in the user data folder, one per
file, never next to the user's files.
"""

import hashlib
import json
import os
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from durumari_mcp.credentials import AdapterError
from durumari_mcp.legacy import manifest_path
from durumari_mcp.private_files import protect_directory, protect_file

# Shared durumari.yaml layout this runtime writes. Runtimes since 0.4.0 accept exactly 1,
# so a later layout must wait until the previous runtime also reads it (expand first).
MANIFEST_SCHEMA_VERSION = 1
# Older manifests also held these per-user keys; the current layout keeps them in the
# ignored .durumari/context-binding.json so the shared file is the same for the team.
USER_KEYS = ("profile", "workspace_identity")
LEGACY_BACKUP = ".durumari-backup-"
# Folders whose files the binding writes; earlier runtimes left backups beside them.
BACKUP_FOLDERS = (
    ".",
    ".codex",
    ".claude",
    ".cursor",
    ".gemini",
    ".durumari",
    ".opencode/skills/durumari-context",
)
LISTED_BACKUPS = 20


def user_data_directory(
    platform: str | None = None,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Per-user data folder, next to the runtime's release state."""
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = home or Path.home()
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData/Local") / "DURUMARI"
    return home / ".local/share/durumari"


def backup_path(root: Path, name: str, *, data: Path | None = None) -> Path:
    """One backup per workspace file: a new backup replaces the previous one."""
    from durumari_mcp.context_binding import workspace_identity

    folder = (data or user_data_directory()) / "backups" / workspace_identity(root)[:16]
    relative = Path(name).as_posix()
    digest = hashlib.sha256(relative.encode()).hexdigest()[:12]
    return folder / (digest + "-" + Path(relative).name)


def save_backup(root: Path, name: str, previous: bytes, *, data: Path | None = None) -> Path:
    path = backup_path(root, name, data=data)
    protect_directory(path.parent.parent)
    protect_directory(path.parent)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".durumari-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(previous)
            stream.flush()
            os.fsync(stream.fileno())
        protect_file(temporary)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def repository_backups(root: Path) -> list[str]:
    """Backups earlier runtimes left in the repository; reported, never deleted."""
    found: list[str] = []
    for folder in BACKUP_FOLDERS:
        directory = root / folder
        if directory.is_symlink() or not directory.is_dir():
            continue
        try:
            found += [
                path.relative_to(root).as_posix()
                for path in directory.iterdir()
                if LEGACY_BACKUP in path.name and path.is_file() and not path.is_symlink()
            ]
        except OSError:
            continue
    return sorted(found)


def manifest_schema(root: Path) -> dict[str, Any]:
    """Schema state of the shared manifest without changing it."""
    try:
        path = manifest_path(root)
        if not path.exists():
            return {"state": "missing"}
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError, AdapterError):
        return {"state": "invalid"}
    version = value.get("schema_version") if isinstance(value, dict) else None
    if type(version) is not int:
        return {"state": "invalid"}
    if version > MANIFEST_SCHEMA_VERSION:
        return {"state": "update_required", "schema_version": version}
    if version < MANIFEST_SCHEMA_VERSION:
        return {"state": "invalid", "schema_version": version}
    legacy = any(key in value for key in USER_KEYS)
    return {"state": "convert_on_run" if legacy else "current", "schema_version": version}


def local_config_status(root: Path) -> dict[str, Any]:
    """Status block for the local settings; a session start hook can act on it."""
    result: dict[str, Any] = {
        "manifest": manifest_schema(root),
        "supported_schema_version": MANIFEST_SCHEMA_VERSION,
        "backup_folder": str(user_data_directory() / "backups"),
    }
    backups = repository_backups(root)
    if backups:
        result["repository_backups"] = {
            "count": len(backups),
            "files": backups[:LISTED_BACKUPS],
            "cleanup": (
                "Earlier versions left these backups in the repository. Durumari no longer "
                "creates or reads them and never deletes them. Review each one, then delete "
                "it yourself; the '*.durumari-backup-*' .gitignore line keeps them out of Git."
            ),
        }
    return result


def migrate_manifest(root: Path, name: str, workspace: str | None) -> str:
    """Convert this grant's legacy manifest to the shared layout; repeated runs do nothing.

    Only the per-user keys of *this* profile move; a teammate's legacy key is theirs.
    """
    from durumari_mcp.context_binding import (
        bound_agents,
        created_files,
        json_file,
        write_files,
    )

    state: str = manifest_schema(root)["state"]
    if state == "update_required":
        raise AdapterError("client_update_required")
    if state != "convert_on_run":
        return state
    path = manifest_path(root)
    manifest = yaml.safe_load(path.read_text(encoding="utf-8"))
    if manifest.get("profile") != name:
        return "current"
    try:
        record = json_file(root, ".durumari/context-binding.json")
    except AdapterError:
        record = {}
    if record.get("profile") not in {None, name}:
        return "current"  # Another grant owns this folder's record; leave both alone.
    identity = workspace or manifest.get("workspace_identity")
    shared = {key: item for key, item in manifest.items() if key not in USER_KEYS}
    updated = {
        "profile": name,
        "agents": bound_agents(root, name),
        "workspace_identity": identity,
        "created_files": sorted(created_files(record)),
    }
    write_files(
        root,
        {
            ".durumari/context-binding.json": json.dumps(updated) + "\n",
            path.name: yaml.safe_dump(shared, sort_keys=False),
        },
    )
    return "migrated"
