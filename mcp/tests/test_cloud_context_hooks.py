"""Hooks must never turn ordinary tool use into remote Context requests."""

import io
import json
import sys
from datetime import UTC
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from durumari_mcp import hook_recovery, project_cli
from durumari_mcp.context_outbox import ContextOutbox, PendingStatus
from durumari_mcp.credentials import AdapterError


def test_pre_tool_hook_does_no_profile_capture_or_network_work(monkeypatch, capsys):
    monkeypatch.setattr(
        project_cli,
        "read_hook_payload",
        lambda: {
            "hook_event_name": "PreToolUse",
            "session_id": "concurrent-chat",
        },
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("tool use must not inspect credentials, capture, or contact Durumari")

    monkeypatch.setattr(project_cli, "ProjectGateway", forbidden)
    monkeypatch.setattr(project_cli, "HookRecovery", forbidden)
    monkeypatch.setattr(project_cli, "capture_hook", forbidden, raising=False)
    project_cli.project_hook(SimpleNamespace(profile="unavailable", client="codex"))
    output = capsys.readouterr()
    assert output.out == ""
    assert output.err == ""


def test_start_hook_is_local_and_bounded(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "hook-health")
    monkeypatch.setattr(
        project_cli,
        "read_hook_payload",
        lambda: {
            "hook_event_name": "SessionStart",
            "cwd": str(tmp_path),
            "session_id": "chat",
        },
    )

    class Gateway:
        def __init__(self, *args):
            self.profile = SimpleNamespace(
                workspace=str(tmp_path),
                project_id="project",
                source_id="source",
                gateway_origin="https://example.com",
                check_workspace=lambda cwd: None,
            )

        def request(self, *args, **kwargs):
            raise AssertionError("startup must not request server Context")

        def handoff(self, *args, **kwargs):
            raise AssertionError("startup must not retrieve handoff")

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    monkeypatch.setattr(
        project_cli,
        "capture_hook",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("startup must not capture a conversation")
        ),
        raising=False,
    )
    project_cli.project_hook(SimpleNamespace(profile="test", client="codex"))
    output = capsys.readouterr()
    assert len(output.out.encode()) <= 1200
    assert "project" in output.out and "search" in output.out.lower()
    assert not output.err


def test_notice_state_is_separate_for_two_chats(monkeypatch, tmp_path):
    from durumari_mcp import hook_recovery

    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path)
    a = hook_recovery.HookRecovery("team", "codex", str(tmp_path), "chat-a")
    b = hook_recovery.HookRecovery("team", "codex", str(tmp_path), "chat-b")
    assert a.defer("project_login_required", "chat-a")
    assert b.status() is None
    assert b.defer("project_login_required", "chat-b")
    assert not a.defer("project_login_required", "chat-a", cached=True)


@pytest.mark.parametrize("encoding", ["cp949", "ascii", "utf-8"])
@pytest.mark.parametrize("scenario", ["startup", "failure", "cached", "stop", "stop_quarantined"])
def test_host_decodes_unicode_hook_frames_in_any_locale(monkeypatch, tmp_path, encoding, scenario):
    """The real hook path must not turn Unicode output into a false network failure."""
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "hook-health")
    payload = {
        "hook_event_name": "Stop" if scenario.startswith("stop") else "SessionStart",
        "cwd": str(tmp_path),
        "session_id": "locale-chat",
    }
    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: payload)
    message = "두루마리 정책 — 한글과 이모지 🧶"

    class Gateway:
        def __init__(self, *args):
            if scenario == "failure":
                raise AdapterError("project_login_required")
            if scenario == "cached":
                raise AssertionError("cached notice must not inspect credentials")
            self.root = tmp_path
            self.profile = SimpleNamespace(
                check_workspace=lambda cwd: None,
                project_id="locale-project",
                gateway_origin="https://example.com",
                connection_id="connection",
                source_id="source",
                actor_id="actor",
                client="mcp",
                workspace=str(tmp_path),
                outbox_key=SecretStr(Fernet.generate_key().decode("ascii")),
            )

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    monkeypatch.setattr(
        project_cli,
        "_hook_snapshot",
        lambda *args: {
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message}
        },
    )
    monkeypatch.setattr(ContextOutbox, "pending_status", lambda *args: PendingStatus(
        0 if scenario == "stop_quarantined" else 1, scenario == "stop_quarantined"
    ))
    if scenario == "cached":
        recovery = hook_recovery.HookRecovery("locale", "claude", str(tmp_path), "locale-chat")
        assert recovery.defer("project_login_required", "previous-chat")

    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding=encoding, errors="strict")
    with monkeypatch.context() as output:
        output.setattr(sys, "stdout", stream)
        project_cli.project_hook(SimpleNamespace(profile="locale", client="claude"))
        stream.flush()
    raw = buffer.getvalue()
    stream.detach()
    frame = json.loads(raw.decode("utf-8", errors="strict"))
    if scenario == "startup":
        assert frame["hookSpecificOutput"]["additionalContext"] == message
        assert "systemMessage" not in frame
        recovery = hook_recovery.HookRecovery("locale", "claude", str(tmp_path), "locale-chat")
        assert recovery.status() is None
    elif scenario.startswith("stop"):
        assert frame["systemMessage"].startswith("두루마리 저장 확인")
        if scenario == "stop_quarantined":
            assert "격리된 저장 기록은 별도 복구 확인이 필요" in frame["systemMessage"]
    else:
        # A disconnected start is never silent and says whether cached rules apply.
        assert frame["systemMessage"] == (
            "두루마리 연결 끊김, 캐시 없음. 작업 전에 연결을 확인해 주세요. "
            "두루마리 로그인 또는 프로젝트 접근 권한을 확인해 주세요."
        )
        context = json.loads(frame["hookSpecificOutput"]["additionalContext"])
        assert context["reason"] == "project_login_required"


def _scope_miss_hook(monkeypatch, tmp_path, connected):
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "hook-health")
    monkeypatch.setattr(
        project_cli,
        "read_hook_payload",
        lambda: {"hook_event_name": "SessionStart", "session_id": "s", "cwd": str(tmp_path)},
    )

    class Gateway:
        def __init__(self, *args):
            raise AdapterError("workspace_out_of_scope")

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    monkeypatch.setattr(project_cli, "_account_covers", lambda client, reason: connected)
    project_cli.project_hook(SimpleNamespace(profile="legacy", client="claude"))


def test_folder_scope_miss_is_silent_when_the_account_is_connected(monkeypatch, capsys, tmp_path):
    _scope_miss_hook(monkeypatch, tmp_path, connected=True)
    assert capsys.readouterr().out == ""


def test_folder_scope_miss_still_warns_without_an_account(monkeypatch, capsys, tmp_path):
    _scope_miss_hook(monkeypatch, tmp_path, connected=False)
    assert "연결 끊김" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_account_covers_only_folder_scope_reasons(monkeypatch):
    monkeypatch.setattr(
        "durumari_mcp.account_credentials.load_account_profile",
        lambda name: {"access_token": "t"},
    )
    assert project_cli._account_covers("claude", "workspace_out_of_scope")
    assert not project_cli._account_covers("claude", "project_unavailable")
    assert not project_cli._account_covers("unknown", "workspace_out_of_scope")


def test_account_covers_requires_a_usable_token(monkeypatch):
    for profile in ({}, {"access_token": ""}, None):
        monkeypatch.setattr(
            "durumari_mcp.account_credentials.load_account_profile",
            lambda name, profile=profile: profile,
        )
        assert not project_cli._account_covers("claude", "workspace_out_of_scope")


def test_account_covers_accepts_a_real_account_profile(monkeypatch):
    from datetime import datetime, timedelta

    from durumari_mcp.account_credentials import AccountProfile

    now = datetime.now(UTC)
    profile = AccountProfile(
        gateway_origin="https://durumari.kr",
        connection_id="11111111-1111-4111-8111-111111111111",
        actor_id="22222222-2222-4222-8222-222222222222",
        client="claude-code",
        provider_family="anthropic",
        expires_at=now + timedelta(days=30),
        access_token="mpa_" + "a" * 43,
        refresh_token="mpr_" + "b" * 43,
        access_expires_at=now + timedelta(hours=1),
        outbox_key=Fernet.generate_key().decode(),
    )
    monkeypatch.setattr(
        "durumari_mcp.account_credentials.load_account_profile", lambda name: profile
    )
    assert project_cli._account_covers("claude", "workspace_out_of_scope")
    expired = profile.model_copy(update={"expires_at": now - timedelta(days=1)})
    monkeypatch.setattr(
        "durumari_mcp.account_credentials.load_account_profile", lambda name: expired
    )
    assert not project_cli._account_covers("claude", "workspace_out_of_scope")
