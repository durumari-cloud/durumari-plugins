"""Repository changes pause sharing, keep host chat usable, and require approval."""

import json
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_continuity import profile
from test_continuity import project_store as project_store_fixture

from durumari_mcp import cli, context_cli, hook_recovery, onboarding, project_cli
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import save_project_profile

project_store = project_store_fixture


def bind(root, client="claude"):
    """A repository-bound manifest as an approved connection leaves it."""
    import yaml

    (root / "durumari.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "server": "https://durumari.example.com",
                "project_id": "project-1",
                "source_id": "source-1",
                "context_source": "server",
                "repository_id": onboarding.repository_identity(root),
            },
            sort_keys=False,
        )
    )
    return "team-" + client


def mismatched(root, client="claude"):
    name = bind(root, client)
    path = root / "durumari.yaml"
    path.write_text(path.read_text().replace(onboarding.repository_identity(root), "a" * 64))
    return name


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_every_hook_event_is_advisory_without_capture_or_network(
    project_store, monkeypatch, tmp_path, capsys, client
):
    name = mismatched(tmp_path, client)
    save_project_profile(name, profile(tmp_path).model_copy(update={"client": client}))
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "health")
    payload = {"cwd": str(tmp_path), "session_id": "session"}
    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: payload)
    args = SimpleNamespace(profile=name, client=client)
    for index, event in enumerate(
        [
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "Stop",
            "Stop",
            "SessionEnd",
            "PostToolUse",
        ]
    ):
        payload["hook_event_name"] = event
        project_cli.project_hook(args)
        output = capsys.readouterr()
        if index == 0:
            value = json.loads(output.out)
            assert value["continue"] is True
            context = json.loads(value["hookSpecificOutput"]["additionalContext"])
            assert context["reason"] == "project_repository_mismatch_reconnect"
            assert context["pending_records"] == "preserved"
        else:
            assert not output.out
        assert not output.err
    # A human-action failure does not repeat its notice after the retry timer.
    monkeypatch.setattr(hook_recovery.time, "time", lambda: 10**11)
    project_cli.project_hook(args)
    assert not capsys.readouterr().out


def status_change(root, change):
    return SimpleNamespace(
        command="context-status", workspace=str(root), profile=None, client="mcp", change=change
    )


def test_status_change_on_mismatch_is_local_read_only_and_never_opens_credentials(
    monkeypatch, tmp_path, capsys
):
    mismatched(tmp_path)
    before = (tmp_path / "durumari.yaml").read_bytes()
    monkeypatch.setattr(context_cli, "load_project_profile", lambda *a: pytest.fail("profile"))
    monkeypatch.setattr(context_cli, "ProjectGateway", lambda *a: pytest.fail("gateway"))
    context_cli.execute(status_change(tmp_path, "move"))
    value = json.loads(capsys.readouterr().out)
    assert value["ready"] is False
    assert value["reason"] == "project_repository_mismatch_reconnect"
    assert value["scope"] == "local_diagnostics_only"
    assert value["reconnect_required"] is True
    assert (tmp_path / "durumari.yaml").read_bytes() == before


def test_status_change_reports_git_initialization_without_mutation(monkeypatch, tmp_path, capsys):
    bind(tmp_path)
    before = (tmp_path / "durumari.yaml").read_bytes()
    monkeypatch.setattr(context_cli, "ProjectGateway", lambda *a: pytest.fail("gateway"))
    context_cli.execute(status_change(tmp_path, "git-init"))
    value = json.loads(capsys.readouterr().out)
    assert value["change"] == "git-init"
    assert value["reconnect_required"] is True
    assert value["scope"] == "local_diagnostics_only"
    assert (tmp_path / "durumari.yaml").read_bytes() == before


@pytest.mark.parametrize(
    "command",
    [
        ["project-init", "--apply"],
        ["project-doctor", "--client", "claude"],
        ["project-preflight", "--client", "claude", "--change", "git-init"],
    ],
)
def test_retired_onboarding_commands_point_to_current_contract(
    monkeypatch, tmp_path, capsys, command
):
    bind(tmp_path)
    # Current connections write context-serve, which the retired checks rejected as
    # invalid_configured_profile instead of pointing to the current commands.
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "durumari_project": {
                        "command": "python",
                        "args": [
                            "-m",
                            "durumari_mcp",
                            "context-serve",
                            "--profile",
                            "durumari-0123456789abcdef-mcp-01234567",
                            "--client",
                            "claude",
                        ],
                    }
                }
            }
        )
    )
    before = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    monkeypatch.setattr(
        sys, "argv", ["durumari-mcp", command[0], "--workspace", str(tmp_path), *command[1:]]
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    error = capsys.readouterr().err
    assert "agent_upgrade_required_use_context_connect" in error
    assert "invalid_configured_profile" not in error
    assert "context-status" in error
    after = {path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()}
    assert after == before


def test_binding_and_context_still_reject_mismatch(project_store, tmp_path):
    name = mismatched(tmp_path)
    scope = profile(tmp_path)
    save_project_profile(name, scope)
    with pytest.raises(AdapterError, match="project_repository_mismatch_reconnect"):
        onboarding.load_manifest(tmp_path)
    from durumari_mcp.context_binding import install_binding

    with pytest.raises(AdapterError, match="project_repository_mismatch_reconnect"):
        install_binding(scope, name, ["claude"])
    from durumari_mcp.project_gateway import ProjectGateway

    with pytest.raises(AdapterError, match="project_repository_mismatch_reconnect"):
        ProjectGateway(name, "claude")


def test_manifest_hash_edit_cannot_override_protected_repository_binding(project_store, tmp_path):
    name = mismatched(tmp_path)
    scope = profile(tmp_path).model_copy(update={"repository_id": "a" * 64})
    save_project_profile(name, scope)
    path = tmp_path / "durumari.yaml"
    path.write_text(path.read_text().replace("a" * 64, onboarding.repository_identity(tmp_path)))
    assert onboarding.load_manifest(tmp_path)["project_id"] == scope.project_id
    from durumari_mcp.project_gateway import ProjectGateway

    with pytest.raises(AdapterError, match="project_repository_mismatch_reconnect"):
        ProjectGateway(name, scope.client)


def test_real_git_initialization_and_remote_change_require_reconnect(tmp_path):
    bind(tmp_path)
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True)
    with pytest.raises(AdapterError, match="project_repository_mismatch_reconnect"):
        onboarding.load_manifest(tmp_path)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://example.com/team/repo.git",
        ],
        check=True,
    )
    with pytest.raises(AdapterError, match="project_repository_mismatch_reconnect"):
        onboarding.load_manifest(tmp_path)
