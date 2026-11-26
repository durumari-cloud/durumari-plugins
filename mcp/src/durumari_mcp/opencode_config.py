"""OpenCode v1/v2 project MCP settings and the discoverable Context skill.

Sources (checked 2026-10-10):
- v1 MCP: https://opencode.ai/docs/mcp-servers/ and https://opencode.ai/config.json
  (``mcp.<name>`` entries with ``enabled``; the schema rejects ``mcp.servers``).
- v2 MCP: https://opencode.ai/v2/docs/mcp-servers (``mcp.servers.<name>``, ``disabled``).
- v2 migration: https://opencode.ai/v2/docs/migrate-v1/ (v2 reads the same files and
  normalizes v1 fields in memory; v1 must not read native v2-only shapes).
- Skills: https://opencode.ai/docs/skills/ and https://opencode.ai/v2/docs/skills
  (``.opencode/skills/<name>/SKILL.md`` is discovered by both versions).

Version policy: ``1.x`` (and earlier ``0.x`` releases) use v1, ``2.x`` uses v2.
Channel builds such as ``0.0.0-beta-*``/``0.0.0-dev-*`` exist for both lines, so
they and a missing executable use the v1 shape, which both versions accept.
Any other version is rejected unless the format is chosen explicitly.
"""

import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from durumari_mcp.credentials import AdapterError

FORMATS = ("auto", "v1", "v2")
SKILL_PATH = ".opencode/skills/durumari-context/SKILL.md"
SKILL_MARKER = "<!-- durumari-managed: opencode-context-skill -->"
VERSION = re.compile(r"\b(\d+)\.(\d+)\.(\d+)(-[0-9A-Za-z.-]+)?\b")


def installed_version() -> str | None:
    """Return the installed ``opencode`` version string, or None when unavailable."""
    executable = shutil.which("opencode")
    if not executable:
        return None
    try:
        output = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
            stdin=subprocess.DEVNULL,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    match = VERSION.search(output or "")
    return match.group(0) if match else ""


def detected_format(version: str | None) -> tuple[str | None, str]:
    if version is None:
        return None, "not_installed"
    match = VERSION.fullmatch(version)
    if not match:
        return None, "unknown_version"
    major, prerelease = int(match.group(1)), match.group(4)
    if version.startswith("0.0.0") and prerelease:
        return None, "channel_build:" + version
    if major in (0, 1):
        return "v1", "installed:" + version
    if major == 2:
        return "v2", "installed:" + version
    raise AdapterError("unsupported_opencode_version_choose_opencode_config")


def choose_format(
    current: dict[str, Any], requested: str, server: str, owned: Any
) -> tuple[str, str]:
    if requested not in FORMATS:
        raise AdapterError("invalid_opencode_config_format")
    mcp = current.get("mcp", {})
    if not isinstance(mcp, dict):
        raise AdapterError("invalid_agent_configuration")
    servers = mcp.get("servers")
    if servers is not None and not isinstance(servers, dict):
        raise AdapterError("invalid_agent_configuration")
    native_v2 = isinstance(servers, dict) and any(
        name != server or not owned(entry) for name, entry in servers.items()
    )
    # Other servers in the v1 shape (names directly under ``mcp``).
    other_v1 = any(
        name not in {"servers", "timeout", server} and isinstance(entry, dict)
        for name, entry in mcp.items()
    )
    if native_v2 and other_v1:
        # Never rewrite a user's mixed file; let them convert it first.
        raise AdapterError("opencode_configuration_mixed_formats")
    if requested == "auto":
        wanted, basis = detected_format(installed_version())
    else:
        wanted, basis = requested, "explicit"
    if native_v2:
        if wanted == "v1":
            raise AdapterError("opencode_v1_cannot_read_v2_configuration")
        return "v2", basis if wanted == "v2" else "existing_v2_configuration"
    if other_v1:
        if wanted == "v2" and requested == "v2":
            raise AdapterError("opencode_configuration_mixed_formats")
        # v2 reads v1-shaped servers; keep the user's file in one format.
        return "v1", basis if wanted == "v1" else "existing_v1_configuration"
    if wanted is None:
        return "v1", basis + "_compatible_v1"
    return wanted, basis


def take_entry(current: dict[str, Any], server: str, owned: Any) -> dict[str, Any] | None:
    """Remove this server from either shape; refuse entries owned by someone else."""
    mcp = current.get("mcp", {})
    if not isinstance(mcp, dict):
        raise AdapterError("invalid_agent_configuration")
    found = None
    servers = mcp.get("servers")
    for container in (mcp, servers if isinstance(servers, dict) else {}):
        if server in container:
            if not owned(container[server]):
                raise AdapterError("existing_mcp_binding_conflict")
            found = container.pop(server)
    if isinstance(servers, dict) and not servers:
        mcp.pop("servers")
    return found


def place_entry(
    current: dict[str, Any],
    server: str,
    command: list[str],
    fmt: str,
    previous: dict[str, Any] | None,
) -> None:
    # Keep a user's explicit choice to disable the server across reconnects and migration.
    off = previous is not None and (
        previous.get("enabled") is False or previous.get("disabled") is True
    )
    mcp = current.setdefault("mcp", {})
    if fmt == "v2":
        entry: dict[str, Any] = {"type": "local", "command": command}
        if off:
            entry["disabled"] = True
        mcp.setdefault("servers", {})[server] = entry
    else:
        mcp[server] = {"type": "local", "command": command, "enabled": not off}


def skill_text(source: str) -> str:
    """Rename the shared Context skill to an OpenCode-valid, collision-safe ID."""
    match = re.fullmatch(r"---\n(.*?)\n---\n(.*)", source, flags=re.S)
    if not match:
        raise AdapterError("invalid_packaged_skill")
    front = re.sub(r"(?m)^name: .*$", "name: durumari-context", match.group(1))
    return "---\n" + front + "\n---\n" + SKILL_MARKER + "\n" + match.group(2)


def packaged_skill() -> str:
    path = Path(__file__).parent / "examples" / "skills" / "durumari-context" / "SKILL.md"
    return skill_text(path.read_text(encoding="utf-8"))


def skill_removal(root: Path) -> str | None:
    """Return the managed skill path for removal; a user's own skill has no marker."""
    path = root / SKILL_PATH
    if path.exists() and not path.is_symlink():
        if SKILL_MARKER in path.read_text(encoding="utf-8"):
            return SKILL_PATH
    return None
