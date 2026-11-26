"""Candidate hook protocol: no host installation or trust is implied by these tests."""

import json
import sys
from copy import deepcopy

import pytest
from test_context_guard_read import Reader

from durumari_mcp.context_guard_hook import ContextGuardHook, main
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def candidate(tmp_path):
    reader = Reader()
    hook = ContextGuardHook(tmp_path / "state", reader, "project", "connection", "codex")
    hook.handle({"hook_event_name": "SessionStart", "session_id": "session"})
    return hook, reader


def prompt(session="session", turn="turn"):
    return {"hook_event_name": "UserPromptSubmit", "session_id": session,
            "turn_id": turn, "prompt": "자원"}


def tool(session="session", turn="turn"):
    return {"hook_event_name": "PreToolUse", "session_id": session,
            "turn_id": turn, "tool_name": "apply_patch"}


def test_current_read_is_injected_and_only_metadata_is_saved(candidate):
    hook, reader = candidate
    output = hook.handle(prompt())
    assert "Tab S6 Lite" in output["hookSpecificOutput"]["additionalContext"]
    assert hook.handle(tool()) == {}
    saved = hook.path.read_text()
    assert "Tab S6 Lite" not in saved
    assert '"prompt"' not in saved
    assert '"texts"' not in saved
    assert "section" in saved
    assert reader.calls[-1][1]["view"] == "outline"


def test_missing_proof_denies_tool(candidate):
    hook, _ = candidate
    result = hook.handle(tool())["hookSpecificOutput"]
    assert result["permissionDecision"] == "deny"


def test_no_result_blocks_prompt_and_cannot_reuse_prior_proof(candidate):
    hook, reader = candidate
    hook.handle(prompt())
    reader.search = {"items": []}
    assert hook.handle(prompt(turn="second"))["decision"] == "block"
    assert hook.handle(tool())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_plain_file_instructions_cannot_displace_the_leading_question(candidate):
    hook, reader = candidate
    original = reader.read

    def topic_only(suffix="", **query):
        if suffix == "/search":
            if query["q"] != "실기기":
                return {"items": []}
            return {"items": [{**reader.search["items"][0], "title": "실기기 자원"}]}
        return original(suffix, **query)

    reader.read = topic_only
    payload = prompt()
    payload["prompt"] = (
        "지금 실기기 STT QA는 어떤 기기와 자원으로 준비해야 해?\n"
        "historical-stt-qa.md를 읽고 durumari_context_read를 호출한다. "
        "historical-stt-qa.md는 현재 근거가 아니다."
    )
    result = hook.handle(payload)
    assert "Tab S6 Lite" in result["hookSpecificOutput"]["additionalContext"]
    assert hook.handle(tool()) == {}


def test_another_turn_or_session_does_not_inherit_read(candidate):
    hook, _ = candidate
    hook.handle(prompt())
    for payload in [tool(turn="other"), tool(session="other")]:
        assert hook.handle(payload)["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_resume_or_compaction_invalidates_proof(candidate):
    hook, _ = candidate
    hook.handle(prompt())
    hook.handle({"hook_event_name": "SessionStart", "session_id": "session", "source": "compact"})
    assert hook.handle(tool())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_compaction_stops_immediate_model_continuation_until_new_read(candidate):
    hook, _ = candidate
    hook.handle(prompt())
    before = json.loads(hook.path.read_text())["epoch"]
    output = hook.handle({"hook_event_name": "SessionStart", "session_id": "session",
                          "source": "compact"})
    assert output["continue"] is False
    assert output["stopReason"].endswith("(read_required)")
    state = json.loads(hook.path.read_text())
    assert state["epoch"] != before
    assert state["proof"] is None
    assert "additionalContext" in hook.handle(prompt(turn="after"))["hookSpecificOutput"]
    assert hook.handle(tool(turn="after")) == {}


def test_session_start_failure_uses_supported_stop_shape(candidate):
    hook, _ = candidate
    output = hook.handle({"hook_event_name": "SessionStart"})
    assert output["continue"] is False
    assert output["stopReason"].endswith("(host_identity_required)")
    assert "decision" not in output


@pytest.mark.parametrize("trigger", ["manual", "auto"])
def test_post_compact_invalidates_proof_and_stops_before_new_model_request(candidate, trigger):
    hook, _ = candidate
    hook.handle(prompt())
    before = json.loads(hook.path.read_text())["epoch"]
    output = hook.handle({"hook_event_name": "PostCompact", "session_id": "session",
                          "turn_id": "turn", "trigger": trigger})
    assert output["continue"] is False
    assert output["stopReason"].endswith("(read_required)")
    state = json.loads(hook.path.read_text())
    assert state["epoch"] != before
    assert state["proof"] is None
    assert hook.handle(tool())["hookSpecificOutput"]["permissionDecision"] == "deny"
    hook.handle(prompt(turn="fresh"))
    assert hook.handle(tool(turn="fresh")) == {}


def test_server_update_denies_tool(candidate):
    hook, reader = candidate
    hook.handle(prompt())
    reader.head["revision"] = 4
    reader.head["efficacy"]["current_revision"] = 4
    assert hook.handle(tool())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_offline_error_does_not_leak_details(candidate):
    hook, reader = candidate
    reader.error = RuntimeError("private network endpoint")
    result = hook.handle(prompt())
    assert result["decision"] == "block"
    assert "private" not in json.dumps(result)


def test_missing_turn_identity_blocks(candidate):
    hook, _ = candidate
    payload = prompt()
    payload.pop("turn_id")
    assert hook.handle(payload)["decision"] == "block"


def test_new_connection_cannot_use_previous_connection_proof(candidate):
    hook, _ = candidate
    hook.handle(prompt())
    other = ContextGuardHook(hook.path.parent, Reader(), "project", "other", "codex")
    assert other.handle(tool())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_corrupt_or_symlinked_state_blocks(candidate, tmp_path):
    hook, _ = candidate
    hook.path.write_text("broken")
    assert hook.handle(prompt())["decision"] == "block"
    hook.path.unlink()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    hook.path.symlink_to(outside)
    assert hook.handle(prompt())["decision"] == "block"
    assert outside.read_text() == "untouched"


def test_stop_warning_does_not_claim_to_hide_already_streamed_text(candidate):
    hook, reader = candidate
    hook.handle(prompt())
    reader.error = RuntimeError("offline")
    payload = {**tool(), "hook_event_name": "Stop"}
    result = hook.handle(payload)
    assert "이미 표시된" in result["systemMessage"]
    assert result.get("decision") != "block"  # Avoid pretending Stop rejects streamed output.


def test_unknown_host_is_not_reported_as_protected(candidate):
    hook, _ = candidate
    unsupported = ContextGuardHook(hook.path.parent, Reader(), "project", "connection", "claude")
    assert unsupported.handle(deepcopy(prompt()))["decision"] == "block"


def test_secret_like_prompt_is_not_sent_to_search_or_persisted(candidate):
    hook, reader = candidate
    payload = {**prompt(), "prompt": "password=synthetic-only-do-not-send"}
    assert hook.handle(payload)["decision"] == "block"
    assert reader.calls == []
    assert "synthetic-only" not in hook.path.read_text()


def test_trusting_hooks_mid_session_can_bootstrap_only_by_reading_current_context(tmp_path):
    reader = Reader()
    hook = ContextGuardHook(tmp_path / "state", reader, "project", "connection", "codex")
    assert hook.handle(tool())["hookSpecificOutput"]["permissionDecisionReason"].endswith(
        "(session_start_required)"
    )
    assert not hook.path.exists()
    assert "Tab S6 Lite" in hook.handle(prompt())["hookSpecificOutput"]["additionalContext"]
    assert reader.calls
    state = json.loads(hook.path.read_text())
    assert state["session"] == "session"
    assert state["proof"]["scope"]["turn"] == "turn"
    assert hook.handle(tool()) == {}


@pytest.mark.parametrize("failure", ["no_result", "offline"])
def test_mid_session_bootstrap_cannot_allow_a_prompt_when_fresh_read_fails(tmp_path, failure):
    reader = Reader()
    if failure == "no_result":
        reader.search = {"items": []}
    else:
        reader.error = RuntimeError("private endpoint")
    hook = ContextGuardHook(tmp_path / "state", reader, "project", "connection", "codex")
    assert hook.handle(prompt())["decision"] == "block"
    assert json.loads(hook.path.read_text())["proof"] is None
    assert hook.handle(tool())["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_mid_session_bootstrap_does_not_reset_corrupt_or_other_session_state(tmp_path):
    hook = ContextGuardHook(tmp_path / "state", Reader(), "project", "connection", "codex")
    hook.handle(prompt())
    before = hook.path.read_text()
    assert hook.handle(prompt(session="other"))["decision"] == "block"
    assert hook.path.read_text() == before
    hook.path.write_text("corrupt")
    assert hook.handle(prompt())["decision"] == "block"
    assert hook.path.read_text() == "corrupt"


@pytest.mark.parametrize("reason", ["project_profile_busy", "protected_project_store_read_failed"])
def test_profile_failure_before_state_creation_has_a_safe_code(monkeypatch, capsys, reason):
    from durumari_mcp import project_cli, project_gateway

    monkeypatch.setattr(sys, "argv", ["guard", "--profile", "profile", "--client", "codex"])
    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: {
        **prompt(), "cwd": "/synthetic/approved"
    })

    def busy(*args):
        raise AdapterError(reason)

    monkeypatch.setattr(project_gateway, "ProjectGateway", busy)
    assert main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["reason"].endswith(f"({reason})")


def test_worker_stage_events_expose_no_profile_or_prompt_values(monkeypatch, capsys):
    from durumari_mcp import project_cli, project_gateway

    monkeypatch.setattr(sys, "argv", ["guard", "--profile", "private-profile", "--client",
                                     "codex", "--stage-events"])
    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: {
        **prompt(), "cwd": "/synthetic/approved"
    })

    def fail(*args):
        raise AdapterError("project_profile_busy")

    monkeypatch.setattr(project_gateway, "ProjectGateway", fail)
    assert main() == 0
    captured = capsys.readouterr()
    assert captured.err == "DURUMARI_GUARD_PROFILE_INITIALIZING\n"
    assert "private-profile" not in captured.out + captured.err


def test_profile_ready_marker_requires_both_workspace_checks(monkeypatch, capsys, tmp_path):
    import os
    from types import SimpleNamespace

    from durumari_mcp import context_client, hook_recovery, project_cli, project_gateway

    checked = []
    profile = SimpleNamespace(project_id="project", connection_id="connection",
                              check_workspace=checked.append)
    monkeypatch.setattr(sys, "argv", ["guard", "--profile", "profile", "--client",
                                     "codex", "--stage-events"])
    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: {
        "hook_event_name": "SessionStart", "session_id": "session", "cwd": str(tmp_path)
    })
    monkeypatch.setattr(project_gateway, "ProjectGateway", lambda *args:
                        SimpleNamespace(profile=profile))
    monkeypatch.setattr(context_client, "ContextClient", lambda *args: Reader())
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "health")
    assert main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {}
    assert captured.err == ("DURUMARI_GUARD_PROFILE_INITIALIZING\n"
                            "DURUMARI_GUARD_PROFILE_READY\n")
    assert checked == [str(tmp_path), os.getcwd()]
