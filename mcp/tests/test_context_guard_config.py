"""Review/apply/restore a temporary candidate without replacing other hooks."""

import json
import subprocess

import pytest

from durumari_mcp import context_binding
from durumari_mcp.context_guard_config import apply_plan, prepare_plan, restore_plan
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(context_binding, "save_backup", lambda *args: None)
    executable = tmp_path / "runtime with spaces" / "python"
    executable.parent.mkdir()
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    return tmp_path / "project", executable


def test_new_candidate_round_trip_removes_only_its_file(setup):
    root, executable = setup
    root.mkdir()
    plan = prepare_plan(root, executable, "profile")
    assert not (root / ".codex/hooks.json").exists()
    apply_plan(plan)
    value = json.loads((root / ".codex/hooks.json").read_text())
    assert set(value["hooks"]) == {"SessionStart", "UserPromptSubmit", "PreToolUse", "Stop",
                                   "PostCompact"}
    assert value["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] == 40
    assert "additionalContextLimit" not in value["hooks"]["Stop"][0]["hooks"][0]
    assert "additionalContextLimit" not in value["hooks"]["PostCompact"][0]["hooks"][0]
    assert "exit 2" in value["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    restore_plan(plan)
    assert not (root / ".codex/hooks.json").exists()


def test_existing_comments_and_other_hooks_are_restored_byte_for_byte(setup):
    root, executable = setup
    (root / ".codex").mkdir(parents=True)
    path = root / ".codex/hooks.json"
    original = '// user comment\n{"hooks":{"Stop":[{"hooks":[{"type":"command",' \
               '"command":"user-hook"}]}]},"custom":true}\n'
    path.write_text(original)
    plan = prepare_plan(root, executable, "profile")
    apply_plan(plan)
    assert "user-hook" in path.read_text()
    restore_plan(plan)
    assert path.read_text() == original


def test_changed_file_aborts_apply_without_overwriting(setup):
    root, executable = setup
    root.mkdir()
    plan = prepare_plan(root, executable, "profile")
    (root / ".codex").mkdir()
    path = root / ".codex/hooks.json"
    path.write_text('{"new_user_setting":true}')
    with pytest.raises(AdapterError, match="guard_configuration_changed"):
        apply_plan(plan)
    assert path.read_text() == '{"new_user_setting":true}'


def test_restore_preserves_new_unrelated_settings_and_hooks(setup):
    root, executable = setup
    root.mkdir()
    plan = prepare_plan(root, executable, "profile")
    apply_plan(plan)
    path = root / ".codex/hooks.json"
    current = json.loads(path.read_text())
    current["new_user_setting"] = True
    current["hooks"]["UserAddedEmptyEvent"] = []
    current["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "user-hook"}]})
    path.write_text(json.dumps(current))
    restore_plan(plan)
    result = json.loads(path.read_text())
    assert result == {"new_user_setting": True,
                      "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "user-hook"}]}],
                                "UserAddedEmptyEvent": []}}


@pytest.mark.parametrize("change", ["command", "timeout"])
def test_changed_candidate_definition_is_preserved_for_review(setup, change):
    root, executable = setup
    root.mkdir()
    plan = prepare_plan(root, executable, "profile")
    apply_plan(plan)
    path = root / ".codex/hooks.json"
    value = json.loads(path.read_text())
    hook = value["hooks"]["Stop"][0]["hooks"][0]
    if change == "command":
        hook["command"] += " --changed-by-person"
    else:
        hook["timeout"] = 99
    path.write_text(json.dumps(value))
    before = path.read_bytes()
    with pytest.raises(AdapterError, match="guard_configuration_changed"):
        restore_plan(plan)
    assert path.read_bytes() == before


def test_symlink_destination_is_never_changed(setup, tmp_path):
    root, executable = setup
    (root / ".codex").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("{}")
    (root / ".codex/hooks.json").symlink_to(outside)
    with pytest.raises(AdapterError):
        prepare_plan(root, executable, "profile")
    assert outside.read_text() == "{}"


def test_missing_runtime_returns_native_block_exit_instead_of_shell_failure(setup):
    root, executable = setup
    root.mkdir()
    plan = prepare_plan(root, executable, "profile")
    executable.unlink()
    result = subprocess.run(["/bin/sh", "-c", plan.command], capture_output=True, text=True)
    assert result.returncode == 2
    assert "의존 작업을 보류" in result.stderr


def test_profile_cannot_inject_shell_code(setup):
    root, executable = setup
    root.mkdir()
    with pytest.raises(AdapterError):
        prepare_plan(root, executable, "profile; echo injected")


def test_explicit_block_exit_is_not_mislabeled_as_runtime_launch_failure(setup):
    root, executable = setup
    root.mkdir()
    executable.write_text("#!/bin/sh\nprintf '%s\\n' 'fixed block (read_required)' >&2\nexit 2\n")
    plan = prepare_plan(root, executable, "profile")
    result = subprocess.run(["/bin/sh", "-c", plan.command], capture_output=True, text=True)
    assert result.returncode == 2
    assert result.stderr == "fixed block (read_required)\n"


@pytest.mark.parametrize("event", ["SessionStart", "PostCompact"])
@pytest.mark.parametrize("failure", ["missing", "exit2", "exit1"])
def test_lifecycle_launch_failure_uses_native_stop_json(setup, event, failure):
    root, executable = setup
    root.mkdir()
    if failure != "missing":
        executable.write_text(f"#!/bin/sh\nexit {2 if failure == 'exit2' else 1}\n")
    plan = prepare_plan(root, executable, "profile")
    if failure == "missing":
        executable.unlink()
    command = json.loads(plan.rendered)["hooks"][event][0]["hooks"][0]["command"]
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["continue"] is False
    assert "의존 작업을 보류" in output["stopReason"]


def test_restore_removes_distinct_lifecycle_commands_around_user_edits(setup):
    root, executable = setup
    root.mkdir()
    plan = prepare_plan(root, executable, "profile")
    apply_plan(plan)
    path = root / ".codex/hooks.json"
    value = json.loads(path.read_text())
    value["user_setting"] = True
    path.write_text(json.dumps(value))
    restore_plan(plan)
    assert json.loads(path.read_text()) == {"user_setting": True}


@pytest.mark.parametrize("event", ["SessionStart", "PostCompact"])
def test_lifecycle_success_preserves_runner_stop_reason(setup, event):
    root, executable = setup
    root.mkdir()
    payload = {"continue": False, "stopReason": "read_required"}
    executable.write_text("#!/bin/sh\nprintf '%s\\n' '" + json.dumps(payload) + "'\n")
    plan = prepare_plan(root, executable, "profile")
    command = json.loads(plan.rendered)["hooks"][event][0]["hooks"][0]["command"]
    result = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True)
    assert result.returncode == 0
    assert json.loads(result.stdout) == payload
    assert not result.stderr
