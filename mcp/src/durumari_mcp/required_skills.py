"""Verify and safely synchronize project Skills without gating other MCP work.

The server selects immutable releases. Local markers identify managed directories;
they are never the authority for the bytes that a previous release should contain.
"""

import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any
from urllib.parse import quote

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_skills import (
    INSTALL_CLIENTS,
    MARKER,
    SKILL_ROOTS,
    git_install_paths,
    read_marker,
    safe_parents,
    skill_root,
    validate_bundle,
    verified_copy,
)
from durumari_mcp.skill_files import file_bytes

ROOT = "/api/v2/agent/skills"
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9-]+(?:\.[a-z0-9-]+)*)?"


def _code(error: Exception) -> str:
    value = str(error)
    return value if re.fullmatch(r"[a-z_]{1,80}", value) else "skill_sync_unavailable"


def _snapshot(gateway: Any) -> list[dict[str, Any]]:
    response = gateway.request(ROOT + "/required")
    rows = response.get("items")
    if not isinstance(rows, list) or len(rows) > 1000 or response.get("has_more"):
        raise AdapterError("invalid_required_skills")
    ids: set[str] = set()
    names: set[str] = set()
    for row in rows:
        if (
            not isinstance(row, dict)
            or row.get("project_id") != gateway.profile.project_id
            or row.get("required") is not True
            or not isinstance(row.get("id"), str)
            or re.fullmatch(r"[a-f0-9-]{36}", row["id"]) is None
            or not isinstance(row.get("name"), str)
            or re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row["name"]) is None
            or len(row["name"]) > 64
            or not isinstance(row.get("selected_version"), str)
            or len(row["selected_version"]) > 40
            or re.fullmatch(VERSION, row["selected_version"]) is None
            or not isinstance(row.get("sha256"), str)
            or re.fullmatch(r"[a-f0-9]{64}", row["sha256"]) is None
            or row["id"] in ids
            or row["name"] in names
        ):
            raise AdapterError("invalid_required_skills")
        ids.add(row["id"])
        names.add(row["name"])
    return rows


def _canonical(gateway: Any, row: dict[str, Any], version: str, digest: str) -> dict[str, Any]:
    if (
        len(version) > 40
        or re.fullmatch(VERSION, version) is None
        or re.fullmatch(r"[a-f0-9]{64}", digest) is None
    ):
        raise AdapterError("skill_local_changes")
    bundle: dict[str, Any] = gateway.request(
        ROOT + "/" + row["id"] + "/versions/" + quote(version, safe="")
    )
    if (
        bundle.get("id"),
        bundle.get("project_id"),
        bundle.get("name"),
        bundle.get("version"),
        bundle.get("sha256"),
    ) != (row["id"], row["project_id"], row["name"], version, digest):
        raise AdapterError("skill_scope_mismatch")
    validate_bundle(bundle)
    return bundle


def _expected(bundle: dict[str, Any]) -> dict[str, str]:
    return {
        name: hashlib.sha256(file_bytes(name, value)).hexdigest()
        for name, value in bundle["files"].items()
    }


def _read(path: Path, maximum: int) -> bytes:
    # Nonblocking/no-follow flags prevent a replaced FIFO or symlink from hanging
    # a status request. Windows junction checks are also performed on every entry.
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise AdapterError("skill_local_changes")
        value = stream.read(maximum + 1)
    if len(value) > maximum:
        raise AdapterError("skill_local_changes")
    return value


def _actual(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    pending = [root]
    entries, total = 0, 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for entry in children:
                entries += 1
                path = Path(entry.path)
                if entries > 10_000 or path.is_symlink() or path.is_junction():
                    raise AdapterError("skill_local_changes")
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(info.st_mode):
                    raise AdapterError("skill_local_changes")
                if path == root / MARKER:
                    continue
                if len(found) >= 100:
                    raise AdapterError("skill_local_changes")
                value = _read(path, 128_000)
                total += len(value)
                if total > 512_000:
                    raise AdapterError("skill_local_changes")
                found[path.relative_to(root).as_posix()] = hashlib.sha256(value).hexdigest()
    return found


def _inspect(gateway: Any, client: str, row: dict[str, Any]) -> dict[str, Any]:
    item: dict[str, Any] = {
        "skill_id": row["id"],
        "name": row["name"],
        "version": row["selected_version"],
        "sha256": row["sha256"],
        "state": "missing",
    }
    canonical = _canonical(gateway, row, row["selected_version"], row["sha256"])
    target = skill_root(Path(gateway.profile.workspace), client) / row["name"]
    try:
        safe_parents(target)
        if not target.exists():
            return item
        marker_path = target / MARKER
        if marker_path.is_symlink() or marker_path.is_junction():
            raise AdapterError("skill_local_changes")
        marker = json.loads(_read(marker_path, 64_000))
        if (
            not isinstance(marker, dict)
            or (marker.get("skill_id"), marker.get("project_id")) != (row["id"], row["project_id"])
            or not isinstance(marker.get("version"), str)
            or not isinstance(marker.get("sha256"), str)
        ):
            raise AdapterError("skill_local_changes")
        previous = canonical
        current = (marker["version"], marker["sha256"]) == (row["selected_version"], row["sha256"])
        if not current:
            # Prove the old installation's bytes against its immutable server release
            # before using the installer's marker to authorize an automatic update.
            previous = _canonical(gateway, row, marker["version"], marker["sha256"])
        expected = _expected(previous)
        if marker.get("files") != expected or _actual(target) != expected:
            raise AdapterError("skill_local_changes")
        item["state"] = "current" if current else "outdated"
    except Exception as error:
        item.update(state="conflict", reason=_code(error))
    return item


def _result(gateway: Any, client: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    states = {item["state"] for item in items}
    state = (
        "conflict" if "conflict" in states else "needs_sync" if states - {"current"} else "current"
    )
    return {
        "project_id": gateway.profile.project_id,
        "client": client,
        "status": state,
        "items": items,
        "recovery_tool": "durumari_skill_sync",
        "local_changes": "preserved",
        "storage": "project_shared",
        "access": "mcp",
        "mcp_ready": state == "current",
    }


def status(gateway: Any, client: str) -> dict[str, Any]:
    """Read fresh selected releases and compare local bytes; never install or block work."""
    try:
        rows = _snapshot(gateway)
        if client not in INSTALL_CLIENTS:
            return {
                **_result(gateway, client, []),
                "status": "unsupported_client",
                "supported_clients": list(INSTALL_CLIENTS),
                "mcp_ready": False,
            }
        result = _result(gateway, client, [_inspect(gateway, client, row) for row in rows])
        legacy = [copy for row in rows for copy in _legacy_inventory(gateway, row)]
        _attach_legacy(result, legacy)
        return result
    except Exception as error:
        return {
            **_result(gateway, client, []),
            "status": "unavailable",
            "error": _code(error),
            "mcp_ready": False,
        }


def _legacy_inventory(gateway: Any, row: dict[str, Any]) -> list[dict[str, str]]:
    """Observe discovery copies without moving files, reporting or changing Git."""
    workspace = Path(gateway.profile.workspace)
    reports = []
    for client, directory in SKILL_ROOTS.items():
        target = workspace / directory / "skills" / row["name"]
        report = {
            "skill_id": row["id"],
            "client": client,
            "path": target.relative_to(workspace).as_posix(),
        }
        try:
            safe_parents(target)
            if not target.exists():
                continue
            marker = read_marker(target)
            if (
                (marker.get("project_id"), marker.get("skill_id")) != (row["project_id"], row["id"])
                or not isinstance(marker.get("version"), str)
                or not isinstance(marker.get("sha256"), str)
            ):
                raise AdapterError("skill_name_collision")
            previous = _canonical(gateway, row, marker["version"], marker["sha256"])
            verified_copy(target, previous)
            git_install_paths(workspace, [target])
            report.update(status="pending", reason="skill_legacy_sync_required")
        except Exception as error:
            report.update(status="preserved", reason=_code(error))
        reports.append(report)
    return reports


def _attach_legacy(result: dict[str, Any], copies: list[dict[str, str]]) -> None:
    # Preserve backup receipts while preferring fresh observations of a path that
    # still exists (for example, migration failed after the central install).
    combined: dict[tuple[str | None, ...], dict[str, str]] = {}
    for copy in [*copies, *result.get("legacy_copies", [])]:
        key = (copy.get("skill_id"), copy.get("client"), copy.get("path"))
        previous = combined.get(key, {})
        observed = {**previous, **copy}
        if previous.get("status") == "recovery_required":
            # A fresh path observation cannot prove that a failed restore's backup
            # was recovered. Keep its location and required recovery state visible.
            observed["status"] = "recovery_required"
            if "reason" in previous:
                observed["reason"] = previous["reason"]
        combined[key] = observed
    if not combined:
        return
    legacy = list(combined.values())
    conflicts = [copy for copy in legacy if copy["status"] in {"preserved", "recovery_required"}]
    result.update(legacy_copies=legacy, legacy_conflicts=conflicts)
    warnings = result.setdefault("warnings", [])
    for copy in legacy:
        if copy["status"] == "backed_up":
            continue
        warning = {
            "skill_id": copy["skill_id"],
            "code": (
                "legacy_skill_copy_pending"
                if copy["status"] == "pending"
                else "legacy_skill_copy_conflict"
            ),
        }
        if warning not in warnings:
            warnings.append(warning)
    if not warnings:
        result.pop("warnings", None)


def _legacy_metadata(value: Any, skill_id: str) -> list[dict[str, str]]:
    if not isinstance(value, dict) or not isinstance(value.get("legacy_copies"), list):
        return []
    result = []
    for copy in value["legacy_copies"][:100]:
        if not isinstance(copy, dict):
            continue
        state = copy.get("status", copy.get("state"))
        if not isinstance(state, str) or state not in {
            "backed_up",
            "preserved",
            "recovery_required",
        }:
            continue
        item = {"skill_id": skill_id, "status": state}
        for key in ("client", "path", "backup_path", "reason"):
            text = copy.get(key)
            if (
                isinstance(text, str)
                and len(text) <= 4096
                and not any(ord(char) < 32 for char in text)
            ):
                item[key] = text
        result.append(item)
    return result


def sync(gateway: Any, client: str) -> dict[str, Any]:
    """Install missing or unchanged older releases, preserving every local conflict.

    Device enrollment, credentials and downloaded script execution are intentionally
    absent. Failure is a synchronization report; unrelated MCP work remains available.
    """
    from durumari_mcp.skill_cli import install_skill

    updated: list[str] = []
    warnings: list[dict[str, str]] = []
    legacy: list[dict[str, str]] = []
    try:
        rows = _snapshot(gateway)
        if client not in INSTALL_CLIENTS:
            return {
                **_result(gateway, client, []),
                "status": "unsupported_client",
                "supported_clients": list(INSTALL_CLIENTS),
                "mcp_ready": False,
                "updated": [],
            }
        for row in rows:
            item = _inspect(gateway, client, row)
            if item["state"] not in {"missing", "outdated", "current"}:
                continue
            try:
                installed = install_skill(gateway, row, client, enroll=False)
                copies = _legacy_metadata(installed, row["id"])
                legacy.extend(copies)
                if any(copy["status"] != "backed_up" for copy in copies):
                    warnings.append({"skill_id": row["id"], "code": "legacy_skill_copy_conflict"})
                if item["state"] != "current":
                    updated.append(row["id"])
            except Exception as error:
                warnings.append({"skill_id": row["id"], "code": _code(error)})
        # Selection may change while files are installed. A fresh status reports the
        # new target instead of claiming that an old snapshot is synchronized.
        result = status(gateway, client)
        result["updated"] = updated
        _attach_legacy(result, legacy)
        if warnings:
            combined_warnings = result.setdefault("warnings", [])
            combined_warnings.extend(
                warning for warning in warnings if warning not in combined_warnings
            )
        return result
    except Exception as error:
        return {
            **_result(gateway, client, []),
            "status": "unavailable",
            "error": _code(error),
            "updated": updated,
            "mcp_ready": False,
            "legacy_copies": legacy,
            "legacy_conflicts": [copy for copy in legacy if copy["status"] != "backed_up"],
        }
