"""One version source for the runtime, CLI and MCP server identifier."""

import re
from importlib.metadata import PackageNotFoundError, version
from typing import Any


def runtime_version() -> str:
    """Installed ``durumari-mcp`` package version (the plugin bundle pins the same value)."""
    try:
        return version("durumari-mcp")
    except PackageNotFoundError:
        return "0+unknown"


def server_release(value: dict[str, Any]) -> dict[str, Any] | None:
    """Keep only the public, bounded fields of ``/api/v2/release``."""
    release, api = value.get("release_id"), value.get("context_api")
    if (
        isinstance(release, str)
        and re.fullmatch(r"[A-Za-z0-9._-]{1,128}", release)
        and type(api) is int
    ):
        return {"release_id": release, "context_api": api}
    return None


UPDATE_STATES = {"current", "update_available", "update_required"}


def parse_version(value: Any) -> tuple[int, int, int] | None:
    match = (
        re.fullmatch(r"([0-9]{1,5})\.([0-9]{1,5})\.([0-9]{1,6})", value)
        if isinstance(value, str)
        else None
    )
    return (int(match[1]), int(match[2]), int(match[3])) if match else None


def client_update(value: dict[str, Any] | None, installed: str) -> dict[str, Any]:
    """Whether this plugin should be updated, from the server's ``client`` announcement.

    A session start hook asks "update?" when ``state`` is not ``current``. Older servers
    announce nothing; that is ``unknown``, never an error.
    """
    client = value.get("client") if isinstance(value, dict) else None
    latest = client.get("latest_version") if isinstance(client, dict) else None
    minimum = client.get("minimum_version") if isinstance(client, dict) else None
    result: dict[str, Any] = {"state": "unknown", "installed_version": installed}
    if not isinstance(client, dict) or not parse_version(latest) or not parse_version(minimum):
        return result
    result.update(latest_version=latest, minimum_version=minimum)
    state = client.get("status")
    if state not in UPDATE_STATES:
        # The server could not read our version; compare locally with the same rule.
        current = parse_version(installed)
        if current is None:
            return result
        state = (
            "update_required"
            if current < (parse_version(minimum) or current)
            else "update_available"
            if current < (parse_version(latest) or current)
            else "current"
        )
    native = value.get("native") if isinstance(value, dict) else None
    required = native.get("required_native_version") if isinstance(native, dict) else None
    current = parse_version(installed)
    if parse_version(required) and (current is None or current < (parse_version(required) or ())):
        # The server refuses this plugin's Agent requests until it is updated.
        state = "update_required"
        result.update(required_version=required, blocked=True)
    result["state"] = state
    if state != "current":
        result["action"] = "durumari-mcp update"
    return result
