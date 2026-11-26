"""An explicitly enabled guard survives reconnect and leaves with its binding."""

import json
import sys
import tomllib
from pathlib import Path

from test_context_v2_adapter import context_profile

from durumari_mcp.context_binding import install_binding, remove_binding
from durumari_mcp.context_guard_config import apply_plan, prepare_plan
from durumari_mcp.prompt_context import ensure_prompt_hook


def guard_hooks(root):
    path = root / ".codex/hooks.json"
    if not path.exists():
        return []
    return [hook for groups in json.loads(path.read_text())["hooks"].values()
            for group in groups for hook in group["hooks"]
            if "durumari_mcp.context_guard_runner" in hook.get("command", "")]


def test_reconnect_preserves_exact_guard_definitions_and_remove_cleans_them(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["codex"])
    apply_plan(prepare_plan(tmp_path, Path(sys.executable), "shared"))
    before = guard_hooks(tmp_path)
    assert len(before) == 5
    install_binding(scope, "shared", ["codex"])
    assert guard_hooks(tmp_path) == before
    # The normal upgrade path may add its advisory prompt hook, but must not
    # replace or widen an explicitly reviewed mandatory guard definition.
    path = tmp_path / ".codex/hooks.json"
    value = json.loads(path.read_text())
    value["hooks"]["UserPromptSubmit"] = [
        group for group in value["hooks"]["UserPromptSubmit"]
        if any("context_guard_runner" in h.get("command", "") for h in group["hooks"])
    ]
    path.write_text(json.dumps(value))
    assert ensure_prompt_hook(tmp_path, "codex", "shared") is True
    assert guard_hooks(tmp_path) == before
    remove_binding(scope, "shared")
    assert guard_hooks(tmp_path) == []
    assert not (tmp_path / ".codex/hooks.json").exists()


def test_remove_preserves_other_profile_and_user_commands(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["codex"])
    apply_plan(prepare_plan(tmp_path, Path(sys.executable), "shared"))
    apply_plan(prepare_plan(tmp_path, Path(sys.executable), "other-profile"))
    path = tmp_path / ".codex/hooks.json"
    value = json.loads(path.read_text())
    user = {"type": "command", "command": (
        "echo python -m durumari_mcp.context_guard_runner --profile shared"
    )}
    value["hooks"]["PreToolUse"].append({"hooks": [user]})
    path.write_text(json.dumps(value))
    before = guard_hooks(tmp_path)
    remove_binding(scope, "shared")
    retained = guard_hooks(tmp_path)
    assert len(retained) == 6
    assert user in retained
    assert [h for h in retained if h != user] == [
        h for h in before if "--profile other-profile " in h["command"]
    ]
    assert tomllib.loads((tmp_path / ".codex/config.toml").read_text())["features"]["hooks"]
