"""Compatibility boundaries for user-owned installations made before the rename.

Old credentials never change server or project scope. Pending encrypted records
keep using their existing lock and directory; they are never silently copied.
"""

from pathlib import Path
from typing import Any

PLUGIN_SERVICE_V1 = "MALMOA.MCP.PluginCredential.v1"
PROJECT_SERVICE_V1 = "MALMOA.MCP.ProjectConnection.v1"
MANIFEST_V1 = "malmoa.yaml"
DIRECTORY_V1 = ".malmoa"
MCP_KEY_V1 = "malmoa_project"
MANAGED_START_V1 = "<!-- MALMOA MANAGED BEGIN -->"
MANAGED_END_V1 = "<!-- MALMOA MANAGED END -->"


# Subdirectories of ``~/.durumari`` that other Durumari components create there
# (the backend meeting STT relay's default spill directory). They are not plugin
# state, so they must not make an earlier ``~/.malmoa`` installation ambiguous.
FOREIGN_DATA_ENTRIES = frozenset({"meeting-stt-relay"})


def _holds_only_foreign_entries(directory: Path, foreign: frozenset[str]) -> bool:
    if not foreign or not directory.is_dir():
        return False
    try:
        names = {entry.name for entry in directory.iterdir()}
    except OSError:
        return False
    return bool(names) and names <= foreign


def compatible_path(
    root: Path, current: str, previous: str, foreign: frozenset[str] = frozenset()
) -> Path:
    from durumari_mcp.credentials import AdapterError

    latest, old = root / current, root / previous
    if latest.is_symlink() or old.is_symlink():
        raise AdapterError("unsafe_legacy_path")
    if old.exists() and _holds_only_foreign_entries(latest, foreign):
        return old
    if latest.exists() and old.exists():
        raise AdapterError("ambiguous_legacy_state_requires_review")
    return old if old.exists() else latest


def manifest_path(root: Path) -> Path:
    return compatible_path(root, "durumari.yaml", MANIFEST_V1)


def data_directory(root: Path) -> Path:
    return compatible_path(root, ".durumari", DIRECTORY_V1, FOREIGN_DATA_ENTRIES)


def binding(servers: dict[str, Any]) -> dict[str, Any] | None:
    from durumari_mcp.credentials import AdapterError

    if "durumari_project" in servers and MCP_KEY_V1 in servers:
        raise AdapterError("ambiguous_legacy_binding_requires_review")
    return servers.get("durumari_project", servers.get(MCP_KEY_V1))


def tool_pattern(pattern: str) -> str:
    """Keep earlier transcript tool names inside the same privacy checks."""
    return pattern.replace("durumari_", "(?:durumari|malmoa)_")
