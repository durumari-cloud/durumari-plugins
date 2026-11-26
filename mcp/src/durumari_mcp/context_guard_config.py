"""Reviewable, reversible configuration for a temporary POSIX Codex guard trial.

Preparing a plan never writes settings or grants hook trust. Applying one requires
the caller's explicit human authorization. This is not the normal connection or
update path, and does not claim full host protection.
"""

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from durumari_mcp.config_json import load as load_json
from durumari_mcp.context_binding import json_file, json_output, write_files
from durumari_mcp.credentials import AdapterError, profile_name
from durumari_mcp.onboarding import inside, merge_hooks

NAME = ".codex/hooks.json"
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "PostCompact")


@dataclass(frozen=True)
class GuardConfigPlan:
    root: Path
    profile: str
    command: str
    original: bytes | None = field(repr=False)
    rendered: str = field(repr=False)


def _bytes(root: Path) -> bytes | None:
    path = inside(root, root / NAME)
    return path.read_bytes() if path.exists() else None


def _hooks(value: dict[str, Any]) -> dict[str, Any]:
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict):
        raise AdapterError("invalid_existing_hooks")
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise AdapterError("invalid_existing_hooks")
        for group in groups:
            if (not isinstance(group, dict) or not isinstance(group.get("hooks"), list)
                    or any(not isinstance(hook, dict) for hook in group["hooks"])):
                raise AdapterError("invalid_existing_hooks")
    return hooks


def _owned(command: Any, profile: str) -> bool:
    if not isinstance(command, str):
        return False
    try:
        args = shlex.split(command)
    except ValueError:
        return False
    return "durumari_mcp.context_guard_runner" in args and ["--profile", profile] in [
        args[i:i + 2] for i in range(len(args))
    ]


def prepare_plan(root: Path, executable: Path, profile: str) -> GuardConfigPlan:
    """Merge a candidate into an in-memory preview; preserve unrelated settings."""
    if os.name != "posix":
        raise AdapterError("unsupported_guard_host")
    root = root.resolve(strict=True)
    profile = profile_name(profile)
    if (not executable.is_absolute() or not executable.is_file()
            or not os.access(executable, os.X_OK)):
        raise AdapterError("guard_runtime_unavailable")
    original = _bytes(root)
    current = json_file(root, NAME)
    for groups in _hooks(current).values():
        if any(_owned(hook.get("command"), profile) for group in groups for hook in group["hooks"]):
            raise AdapterError("guard_already_configured")
    command = shlex.join([str(executable), "-m", "durumari_mcp.context_guard_runner",
                          "--profile", profile, "--client", "codex"])
    # Prompt/tool events need exit 2 rather than a normal shell launch error.
    # The watchdog handles worker failures before the outer host timeout.
    invocation = command
    command += "; durumari_guard_exit_status=$?; "
    command += 'case "$durumari_guard_exit_status" in 0) ;; 2) exit 2 ;; *) '
    command += "printf '%s\\n' " + shlex.quote(
        "두루마리 검사기를 실행할 수 없어 의존 작업을 보류합니다."
    ) + " >&2; exit 2 ;; esac"
    # Lifecycle events do not document exit 2 as a native stop. Even if Python
    # cannot start, the shell must return their supported stop JSON with exit 0.
    stop_output = json.dumps({
        "continue": False,
        "stopReason": "두루마리 검사기를 실행할 수 없어 의존 작업을 보류합니다.",
    }, ensure_ascii=False)
    lifecycle_command = (invocation + "; durumari_guard_exit_status=$?; "
                         'case "$durumari_guard_exit_status" in 0) ;; *) '
                         + "printf '%s\\n' " + shlex.quote(stop_output)
                         + "; exit 0 ;; esac")
    template = {"hooks": {event: [{"hooks": [{
        "type": "command", "command": (lifecycle_command
            if event in {"SessionStart", "PostCompact"} else command), "timeout": 40,
        **({"additionalContextLimit": 65536} if event not in {"Stop", "PostCompact"} else {}),
        "statusMessage": "두루마리 · 필수 Context 검사 시험 후보",
    }]}] for event in EVENTS}}
    rendered = json_output(root, NAME, merge_hooks(current, template))
    return GuardConfigPlan(root, profile, command, original, rendered)


def apply_plan(plan: GuardConfigPlan) -> None:
    """After approval, abort if the reviewed file changed. Never grant trust."""
    current = _bytes(plan.root)
    if current == plan.rendered.encode("utf-8"):
        return
    if current != plan.original:
        raise AdapterError("guard_configuration_changed")
    write_files(plan.root, {NAME: plan.rendered})


def restore_plan(plan: GuardConfigPlan) -> None:
    """Restore exactly, or remove only this candidate around unrelated new edits."""
    current = _bytes(plan.root)
    if current is None:
        return
    if current == plan.rendered.encode("utf-8"):
        write_files(plan.root, {
            NAME: plan.original.decode("utf-8") if plan.original is not None else None
        })
        return
    value = json_file(plan.root, NAME)
    hooks = _hooks(value)
    planned = _hooks(load_json(plan.rendered))
    expected = {
        event: [hook for group in groups for hook in group["hooks"]
                if _owned(hook.get("command"), plan.profile)]
        for event, groups in planned.items()
    }
    # Do not reinterpret a person's edit to this candidate as our owned definition.
    for event, groups in hooks.items():
        for group in groups:
            for hook in group["hooks"]:
                if (_owned(hook.get("command"), plan.profile)
                        and hook not in expected.get(event, [])):
                    raise AdapterError("guard_configuration_changed")
    previous = load_json(plan.original.decode("utf-8")) if plan.original else {}
    previous_hooks = _hooks(previous)
    removed = False
    for event, groups in list(hooks.items()):
        retained = []
        event_removed = False
        for group in groups:
            kept = [hook for hook in group["hooks"] if hook not in expected.get(event, [])]
            if len(kept) == len(group["hooks"]):
                retained.append(group)
                continue
            removed = True
            event_removed = True
            if kept:
                retained.append({**group, "hooks": kept})
        if not event_removed:
            continue
        if retained or event in previous_hooks:
            hooks[event] = retained
        else:
            hooks.pop(event, None)
    if not removed:
        return
    if not hooks and "hooks" not in previous:
        value.pop("hooks", None)
    content = json_output(plan.root, NAME, value) if value or plan.original else None
    write_files(plan.root, {NAME: content})
