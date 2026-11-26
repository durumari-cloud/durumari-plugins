"""Fixed public reason codes shared by the worker and stdlib watchdog."""

import re
from typing import Any

PROFILE_INITIALIZING = "DURUMARI_GUARD_PROFILE_INITIALIZING"
PROFILE_READY = "DURUMARI_GUARD_PROFILE_READY"

SAFE_REASONS = frozenset({
    "search_required", "read_required", "current_read_required", "server_unavailable",
    "unsupported_guard_host", "unsupported_guard_event", "host_identity_required",
    "invalid_guard_state", "session_start_required", "sensitive_prompt_requires_safe_query",
    "guard_unavailable", "guard_check_failed", "guard_worker_timeout",
    "guard_worker_unavailable", "guard_worker_failed", "guard_output_invalid",
    "guard_input_invalid", "guard_profile_timeout", "protected_project_store_read_failed",
    "workspace_out_of_scope", "project_credential_missing", "project_removed",
    "project_profile_changed", "project_profile_busy", "project_reconnect_required",
    "agent_upgrade_required",
})


def safe_reason(value: Any, fallback: str = "guard_unavailable") -> str:
    if isinstance(value, str) and value in SAFE_REASONS:
        return value
    return fallback if fallback in SAFE_REASONS else "guard_unavailable"


def worker_reason(value: Any) -> str:
    """Never forward worker prose, paths, server errors or arbitrary parenthesized values."""
    if not isinstance(value, str):
        return "guard_check_failed"
    matches = re.findall(r"\(([a-z_]{1,64})\)", value)
    if len(matches) != 1:
        return "guard_check_failed"
    return safe_reason(matches[0], "guard_check_failed")


def timeout_reason(stderr: Any) -> str:
    """Identify only the fixed local-init phase; never forward worker diagnostics."""
    if isinstance(stderr, bytes):
        stderr = stderr.decode("ascii", errors="replace")
    if not isinstance(stderr, str) or len(stderr) > 4096:
        return "guard_worker_timeout"
    stages = [line for line in stderr.splitlines()
              if line in {PROFILE_INITIALIZING, PROFILE_READY}]
    return ("guard_profile_timeout" if stages and stages[-1] == PROFILE_INITIALIZING
            else "guard_worker_timeout")
