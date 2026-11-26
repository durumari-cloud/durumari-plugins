"""Small, non-secret retry/notice state; never changes hook trust or capture queues."""

import hashlib
import json
import math
import os
import re
import time
import traceback
from pathlib import Path
from typing import Any
from uuid import uuid4

from durumari_mcp.credentials import AdapterError, profile_name
from durumari_mcp.project_outbox import process_lock, state_root

RETRY_SECONDS = 30
NOTICE_SECONDS = 300
# This Agent's settings name a removed or unusable connection, typically after another
# Agent reconnected the folder; reconnecting this Agent fixes it.
RECONNECT_AGENT_ERRORS = {
    "project_removed",
    "project_client_mismatch",
    "invalid_project_credential",
}
# Any other local check failure: the hook makes no server request before it, so it is
# never a server outage.
LOCAL_CHECK_FAILED = "project_local_check_failed"
HUMAN_ACTION_ERRORS = {
    "project_repository_mismatch_reconnect",
    "project_connection_scope_changed",
    "project_manifest_profile_mismatch",
    "workspace_out_of_scope",
    LOCAL_CHECK_FAILED,
} | RECONNECT_AGENT_ERRORS
RECOVERABLE = {
    "project_credential_missing",
    "project_login_required",
    "project_access_denied",
    "project_connection_unavailable",
    "project_action_denied",
    "project_delivery_unknown",
    "project_unavailable",
    "project_rate_limited",
    "project_profile_busy",
    "protected_project_store_read_failed",
    "protected_project_store_write_failed",
} | HUMAN_ACTION_ERRORS
# The server (or a newer local format) refused this plugin version; updating keeps
# the same connection.
UPDATE_ERRORS = {"server_requires_client_update", "client_update_required"}
RECOVERABLE |= UPDATE_ERRORS
NETWORK_ERRORS = {
    "project_delivery_unknown",
    "project_unavailable",
    "project_rate_limited",
    "project_profile_busy",
}


def recovery_root() -> Path:
    # Public health metadata is separate from the encrypted project-sync store.
    return state_root().parent / "hook-health"


class HookRecovery:
    def __init__(self, name: str, client: str, cwd: str, session: str = "unknown") -> None:
        self.directory = recovery_root() / profile_name(name)
        scope = client + "\0" + str(Path(cwd).resolve()) + "\0" + session
        self.path = self.directory / (hashlib.sha256(scope.encode()).hexdigest() + ".json")

    def once(self, marker: str) -> bool:
        path = self.path.with_suffix("." + marker + ".json")
        try:
            with process_lock(self.directory, timeout=0.05):
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as stream:
                    stream.write('{"version":1}')
                return True
        except (AdapterError, OSError):
            return False

    def _read(self) -> dict[str, Any] | None:
        if not self.path.exists():
            return None
        if self.path.is_symlink() or self.path.stat().st_size > 1024:
            return None
        value = json.loads(self.path.read_text())
        if not isinstance(value, dict) or set(value) != {
            "reason",
            "failed_at",
            "notice_at",
            "notice_session",
        }:
            return None
        if value["reason"] not in RECOVERABLE or not isinstance(value["notice_session"], str):
            return None
        if any(
            type(value[key]) not in {int, float} or not math.isfinite(value[key])
            for key in ("failed_at", "notice_at")
        ):
            return None
        return value

    def status(self) -> dict[str, Any] | None:
        try:
            with process_lock(self.directory, timeout=0.05):
                return self._read()
        except (AdapterError, OSError, ValueError, TypeError):
            return None

    def cooling_down(self, status: dict[str, Any]) -> bool:
        return bool(0 <= time.time() - status["failed_at"] < RETRY_SECONDS)

    def defer(self, reason: str, session: str, *, cached: bool = False) -> bool:
        """Return whether to show a notice. Cached checks do not extend backoff."""
        now = time.time()
        session_key = hashlib.sha256(session.encode()).hexdigest()
        try:
            with process_lock(self.directory, timeout=0.05):
                previous = self._read()
                notify = not (
                    previous
                    and previous["reason"] == reason
                    and previous["notice_session"] == session_key
                    and (
                        reason in HUMAN_ACTION_ERRORS
                        or 0 <= now - previous["notice_at"] < NOTICE_SECONDS
                    )
                )
                value = {
                    "reason": reason,
                    "failed_at": previous["failed_at"] if cached and previous else now,
                    "notice_at": previous["notice_at"] if previous and not notify else now,
                    "notice_session": session_key,
                }
                temporary = self.directory / (uuid4().hex + ".tmp")
                try:
                    fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    with os.fdopen(fd, "w") as stream:
                        json.dump(value, stream)
                    os.replace(temporary, self.path)
                finally:
                    temporary.unlink(missing_ok=True)
                return notify
        except (AdapterError, OSError, ValueError, TypeError):
            return True  # Failure to keep advisory state must not prevent recovery.

    def clear(self) -> None:
        try:
            with process_lock(self.directory, timeout=0.05):
                self.path.unlink(missing_ok=True)
        except (AdapterError, OSError):
            pass


def clear_hook_recovery(name: str) -> None:
    """New credentials permit an immediate retry; existing queues stay untouched."""
    directory = recovery_root() / profile_name(name)
    if not directory.exists():
        return
    try:
        with process_lock(directory, timeout=0.05):
            for path in directory.glob("*.json"):
                if not path.is_symlink():
                    path.unlink(missing_ok=True)
    except (AdapterError, OSError):
        pass


FAILURE_FILE = "last-failure.json"
_PACKAGE = "durumari_mcp"


def failure_label(error: BaseException) -> str:
    """Content-free label: exception class and innermost adapter frame, never its message."""
    try:
        return _failure_label(error)
    except Exception:
        return "unlabelled"


def _failure_label(error: BaseException) -> str:
    kind = type(error)
    name = (
        kind.__qualname__
        if kind.__module__ == "builtins"
        else (kind.__module__ + "." + kind.__qualname__)
    )
    name = re.sub(r"[^A-Za-z0-9_.]", "_", name)[:120]
    location = ""
    for frame in reversed(traceback.extract_tb(error.__traceback__)):
        parts = Path(frame.filename).parts
        if _PACKAGE in parts:
            location = Path(frame.filename).name + ":" + str(frame.lineno)
            break
    return name + (" at " + location if location else "")


def record_hook_failure(name: str, event: str, error: BaseException) -> None:
    """Keep only the latest unexpected hook failure for local diagnosis."""
    try:
        from importlib.metadata import version

        adapter = version("durumari-mcp")
    except Exception:
        adapter = "unknown"
    value = {
        "version": 1,
        "event": event if event in {"SessionStart", "Stop"} else "other",
        "error": failure_label(error),
        "failed_at": time.time(),
        "adapter_version": re.sub(r"[^A-Za-z0-9_.+-]", "_", adapter)[:40],
    }
    try:
        directory = recovery_root() / profile_name(name)
        with process_lock(directory, timeout=0.05):
            temporary = directory / (uuid4().hex + ".tmp")
            try:
                fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as stream:
                    json.dump(value, stream)
                os.replace(temporary, directory / FAILURE_FILE)
            finally:
                temporary.unlink(missing_ok=True)
    except Exception:
        pass  # Diagnostics must never change hook behavior.
