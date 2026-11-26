"""Worker errors must become native deny output before the host hook timeout."""

import io
import json
import subprocess
import sys
from types import SimpleNamespace

import pytest

from durumari_mcp.context_guard_runner import main, run_guard


@pytest.mark.parametrize("failure,reason", [
    (OSError("private path"), "guard_worker_unavailable"),
    (subprocess.TimeoutExpired("private command", 30), "guard_worker_timeout"),
])
def test_worker_failure_denies_tool_without_raw_error(monkeypatch, failure, reason):
    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(subprocess, "run", fail)
    result = run_guard({"hook_event_name": "PreToolUse"}, "profile", "codex")
    assert result["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "private" not in json.dumps(result)
    assert result["hookSpecificOutput"]["permissionDecisionReason"].endswith(f"({reason})")


@pytest.mark.parametrize("stdout,code", [("broken", 0), ("{}", 1), ("[]", 0),
                                        ("{}", 0), ('{"continue":true}', 0)])
def test_bad_or_missing_prompt_decision_is_blocked(monkeypatch, stdout, code):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout=stdout, stderr="private", returncode=code))
    assert run_guard({"hook_event_name": "UserPromptSubmit"}, "profile", "codex") \
        ["decision"] == "block"


def test_valid_prompt_context_passes_and_child_budget_is_bounded(monkeypatch):
    expected = {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                       "additionalContext": "current selected text"}}

    def child(command, **kwargs):
        assert kwargs["timeout"] == 30
        assert "--profile" in command
        assert "shell" not in kwargs
        return SimpleNamespace(stdout=json.dumps(expected), stderr="", returncode=0)

    monkeypatch.setattr(subprocess, "run", child)
    assert run_guard({"hook_event_name": "UserPromptSubmit"}, "profile", "codex") == expected


def test_worker_cannot_allow_a_different_event_shape(monkeypatch):
    wrong = {"hookSpecificOutput": {"hookEventName": "Stop", "additionalContext": "text"}}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout=json.dumps(wrong), stderr="", returncode=0))
    assert run_guard({"hook_event_name": "PreToolUse"}, "profile", "codex") \
        ["hookSpecificOutput"]["permissionDecision"] == "deny"


@pytest.mark.parametrize("event,key", [("UserPromptSubmit", "reason"),
                                       ("PreToolUse", "permissionDecisionReason")])
def test_only_fixed_worker_reason_code_is_preserved(monkeypatch, event, key):
    if event == "UserPromptSubmit":
        output = {"decision": "block", "reason": "private endpoint (read_required)"}
    else:
        output = {"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny",
                                         "permissionDecisionReason": "private (read_required)"}}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout=json.dumps(output), stderr="", returncode=0))
    result = run_guard({"hook_event_name": event}, "profile", "codex")
    message = result[key] if event == "UserPromptSubmit" else result["hookSpecificOutput"][key]
    assert message.endswith("(read_required)")
    assert "private" not in message


def test_unknown_worker_reason_is_not_echoed(monkeypatch):
    output = {"decision": "block", "reason": "private (credential-value-123)"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout=json.dumps(output), stderr="", returncode=0))
    result = run_guard({"hook_event_name": "UserPromptSubmit"}, "profile", "codex")
    assert result["reason"].endswith("(guard_check_failed)")
    assert "credential" not in result["reason"]


def test_blocking_main_uses_native_exit_two_with_safe_stderr(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["guard", "--profile", "profile", "--client", "codex"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        json.dumps({"hook_event_name": "UserPromptSubmit"}).encode()
    )))
    output = {"decision": "block", "reason": "private (read_required)"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout=json.dumps(output), stderr="", returncode=0))
    assert main() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.endswith("(read_required)\n")
    assert "private" not in captured.err


@pytest.mark.parametrize("event", ["SessionStart", "PostCompact"])
def test_session_start_stop_is_preserved_as_safe_json_not_exit_two(monkeypatch, capsys, event):
    monkeypatch.setattr(sys, "argv", ["guard", "--profile", "profile", "--client", "codex"])
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        json.dumps({"hook_event_name": event, "source": "compact"}).encode()
    )))
    output = {"continue": False, "stopReason": "private (read_required)",
              "systemMessage": "private"}
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout=json.dumps(output), stderr="", returncode=0))
    assert main() == 0
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["continue"] is False
    assert result["stopReason"].endswith("(read_required)")
    assert "private" not in captured.out
    assert captured.err == ""


@pytest.mark.parametrize("event", ["SessionStart", "PostCompact"])
def test_session_start_worker_failure_stops_continuation(monkeypatch, event):
    def fail(*args, **kwargs):
        raise OSError("private")
    monkeypatch.setattr(subprocess, "run", fail)
    result = run_guard({"hook_event_name": event}, "profile", "codex")
    assert result["continue"] is False
    assert result["stopReason"].endswith("(guard_worker_unavailable)")


def test_empty_compaction_worker_result_cannot_resume_without_reads(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw:
                        SimpleNamespace(stdout="{}", stderr="", returncode=0))
    result = run_guard({"hook_event_name": "SessionStart", "source": "compact"},
                       "profile", "codex")
    assert result["continue"] is False
    assert result["stopReason"].endswith("(guard_output_invalid)")
    assert run_guard({"hook_event_name": "SessionStart", "source": "startup"},
                     "profile", "codex") == {}


@pytest.mark.parametrize("stderr,reason", [
    (b"DURUMARI_GUARD_PROFILE_INITIALIZING\n", "guard_profile_timeout"),
    ("DURUMARI_GUARD_PROFILE_INITIALIZING\nDURUMARI_GUARD_PROFILE_READY\n",
     "guard_worker_timeout"),
    ("private endpoint (guard_profile_timeout)", "guard_worker_timeout"),
    ("x" * 4097 + "\nDURUMARI_GUARD_PROFILE_INITIALIZING\n", "guard_worker_timeout"),
])
def test_timeout_phase_uses_only_fixed_worker_markers(monkeypatch, stderr, reason):
    def fail(command, **kwargs):
        assert "--stage-events" in command
        raise subprocess.TimeoutExpired(command, 30, stderr=stderr)

    monkeypatch.setattr(subprocess, "run", fail)
    result = run_guard({"hook_event_name": "UserPromptSubmit"}, "profile", "codex")
    assert result["decision"] == "block"
    assert result["reason"].endswith(f"({reason})")
    assert "private" not in json.dumps(result)


def test_real_worker_timeout_keeps_profile_stage_and_hides_stderr(tmp_path, monkeypatch):
    from durumari_mcp import context_guard_runner

    package = tmp_path / "durumari_mcp"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "context_guard_hook.py").write_text(
        "import sys,time\n"
        "sys.stderr.write('private detail\\nDURUMARI_GUARD_PROFILE_INITIALIZING\\n')\n"
        "sys.stderr.flush()\n"
        "time.sleep(60)\n"
    )
    monkeypatch.setenv("PYTHONPATH", str(tmp_path))
    monkeypatch.setattr(context_guard_runner, "WORKER_SECONDS", 3)
    result = run_guard({"hook_event_name": "UserPromptSubmit"}, "profile", "codex")
    assert result["decision"] == "block"
    assert result["reason"].endswith("(guard_profile_timeout)")
    assert "로컬 연결 초기화" in result["reason"]
    assert "private detail" not in json.dumps(result)
