"""Error guidance names only current commands and does not misstate recovery."""

import sys

import pytest

from durumari_mcp import cli, context_cli
from durumari_mcp.credentials import AdapterError

RETIRED_GUIDANCE = ("project-connect", "--create-workspace")


def run_failure(monkeypatch, capsys, tmp_path, code):
    def fail(args):
        raise AdapterError(code)

    monkeypatch.setattr(cli, "execute", fail)
    monkeypatch.setattr(
        sys, "argv", ["durumari-mcp", "context-status", "--workspace", str(tmp_path)]
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    return capsys.readouterr().err


def test_project_connect_is_retired_without_connecting(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(context_cli, "connect", lambda *a: pytest.fail("connect"))
    monkeypatch.setattr(
        sys,
        "argv",
        ["durumari-mcp", "project-connect", "--workspace", str(tmp_path), "--client", "claude"],
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    assert "agent_upgrade_required_use_context_connect" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_workspace_directory_failure_has_current_guidance(monkeypatch, capsys, tmp_path):
    message = run_failure(monkeypatch, capsys, tmp_path, "workspace_directory_required")
    assert "작업 폴더" in message
    assert "context-connect" in message
    assert "--create-workspace" not in message


def test_git_lookup_guidance_distinguishes_new_folders_from_existing_git_bindings(
    monkeypatch, capsys, tmp_path
):
    message = run_failure(monkeypatch, capsys, tmp_path, "repository_root_and_remote_required")
    assert "origin" in message
    assert "새 비Git·origin 없는 폴더" in message
    assert "UUID 입력 없이" in message
    assert "context-connect --workspace <폴더>" in message
    assert "기존 Git 연결은 폴더 소스로 자동 전환하지 않습니다" in message
    assert "origin remote가 설정된 상태로 연결해 주세요" not in message


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        ("project_login_required", "context-connect"),
        ("interactive_project_login_required", "context-connect"),
        ("project_credential_missing", "context-connect"),
        ("repository_root_and_remote_required", "origin"),
        ("project_repository_mismatch_reconnect", "context-connect"),
        ("project_removal_pending_retry", "context-remove"),
        ("existing_mcp_binding_conflict", "durumari_project"),
        ("project_access_denied", "context-connect"),
    ],
)
def test_guidance_points_to_current_commands(monkeypatch, tmp_path, capsys, code, expected):
    error = run_failure(monkeypatch, capsys, tmp_path, code)
    assert error.startswith("두루마리: " + code)
    assert expected in error
    assert not any(retired in error for retired in RETIRED_GUIDANCE)


def test_access_denied_does_not_claim_the_connection_is_unrecoverable(
    monkeypatch, tmp_path, capsys
):
    # Retired v1 endpoints answer 410 with the same code; the cause is not always a revocation.
    error = run_failure(monkeypatch, capsys, tmp_path, "project_access_denied")
    assert "복구할 수 없습니다" not in error
    assert "기존 기록은 보존됩니다" in error
