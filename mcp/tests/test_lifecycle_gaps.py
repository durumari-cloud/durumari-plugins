"""Lifecycle transitions of the connection contract that earlier tests did not cover."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_context_connect import approval, connection, enrollment
from test_context_connect import setup as setup_fixture
from test_context_v2_adapter import context_profile
from test_continuity import project_store as store_fixture

from durumari_mcp import cli, context_cli
from durumari_mcp.context_binding import install_binding, shared_profile
from durumari_mcp.credentials import AdapterError
from durumari_mcp.onboarding import repository_identity
from durumari_mcp.project_credentials import load_project_profile, save_project_profile
from durumari_mcp.project_gateway import ProjectTransport

project_store = store_fixture
setup = setup_fixture


def fresh_approval_server(scope, calls):
    def request(self, path, **kwargs):
        calls.append(path)
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            return enrollment(scope)
        if path.endswith("/poll"):
            return approval(scope)
        if path.endswith("/finish"):
            return {}
        if path.endswith("/connection"):
            return connection(scope)
        pytest.fail("Unexpected request " + path)

    return request


def test_t4c_approval_timeout_keeps_the_device_secret_for_the_next_run(
    setup, project_store, monkeypatch
):
    scope, args, browsers = setup
    clock = iter([0.0, 0.0, 601.0])
    monkeypatch.setattr(context_cli.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(context_cli.time, "sleep", lambda seconds: None)
    tokens = []

    def request(self, path, **kwargs):
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            tokens.append(kwargs["body"]["device_token"])
            return enrollment(scope)
        if path.endswith("/poll"):
            return {"status": "pending"}
        pytest.fail("Unexpected request " + path)

    monkeypatch.setattr(ProjectTransport, "request", request)
    with pytest.raises(AdapterError, match="browser_approval_pending"):
        context_cli.connect(args)
    assert len(browsers) == 1
    key = shared_profile(scope.gateway_origin, "enrollment", Path(args.workspace))
    assert project_store.get_password(context_cli.ENROLLMENT_SERVICE, key) == tokens[0]


def test_t7_remote_change_starts_browser_reapproval_and_keeps_the_old_profile(
    setup, project_store, monkeypatch
):
    scope, args, browsers = setup
    save_project_profile("shared", scope)
    install_binding(scope, "shared", args.agents)
    root = Path(args.workspace)
    subprocess.run(
        ["git", "-C", str(root), "remote", "set-url", "origin", "git@gitlab.com:team/repo.git"],
        check=True,
    )
    moved = scope.model_copy(
        update={"connection_id": "connection-moved", "repository_id": repository_identity(root)}
    )
    calls = []
    monkeypatch.setattr(ProjectTransport, "request", fresh_approval_server(moved, calls))
    result = context_cli.connect(args)
    assert len(browsers) == 1
    assert result["profile"] != "shared"
    assert load_project_profile(result["profile"]).repository_id == moved.repository_id
    # The old grant's encrypted key and queue identity are kept for later reconciliation.
    assert load_project_profile("shared").outbox_key == scope.outbox_key


@pytest.mark.parametrize("change", ["copy", "worktree"])
def test_copied_folder_or_worktree_asks_for_its_own_approval(
    setup, project_store, monkeypatch, tmp_path, change
):
    scope, args, browsers = setup
    save_project_profile("shared", scope)
    install_binding(scope, "shared", args.agents)
    root = Path(args.workspace)
    if change == "copy":
        target = tmp_path.parent / (tmp_path.name + "-copy")
        shutil.copytree(root, target)
    else:
        subprocess.run(["git", "-C", str(root), "add", "durumari.yaml"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "-c",
                "user.name=t",
                "-c",
                "user.email=t@example.invalid",
                "commit",
                "-q",
                "-m",
                "binding",
            ],
            check=True,
        )
        target = tmp_path.parent / (tmp_path.name + "-worktree")
        subprocess.run(["git", "-C", str(root), "worktree", "add", "-q", str(target)], check=True)
    from durumari_mcp.context_binding import workspace_identity

    other = scope.model_copy(
        update={
            "connection_id": "connection-" + change,
            "workspace": str(target),
            "workspace_identity": workspace_identity(target),
            "repository_id": repository_identity(target),
        }
    )
    calls = []
    monkeypatch.setattr(ProjectTransport, "request", fresh_approval_server(other, calls))
    args.workspace = str(target)
    result = context_cli.connect(args)
    assert len(browsers) == 1
    assert load_project_profile(result["profile"]).workspace == str(target.resolve())
    assert load_project_profile("shared").workspace == scope.workspace


def test_local_settings_deleted_alone_are_restored_without_another_approval(
    setup, project_store, monkeypatch
):
    scope, args, browsers = setup
    save_project_profile("shared", scope)
    install_binding(scope, "shared", args.agents)
    root = Path(args.workspace)
    (root / ".mcp.json").unlink()
    (root / ".claude" / "settings.local.json").unlink()
    monkeypatch.setattr(ProjectTransport, "request", lambda *a, **kw: connection(scope))
    assert context_cli.connect(args)["profile"] == "shared"
    assert browsers == []
    servers = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert "context-serve" in servers["durumari_project"]["args"]
    assert (root / ".claude" / "settings.local.json").exists()


def test_c5_disabled_hook_settings_survive_install_and_remove(tmp_path):
    scope = context_profile(tmp_path)
    claude = tmp_path / ".claude" / "settings.local.json"
    claude.parent.mkdir()
    claude.write_text(json.dumps({"disableAllHooks": True, "hooks": {}}), encoding="utf-8")
    codex = tmp_path / ".codex" / "config.toml"
    codex.parent.mkdir()
    codex.write_text("[features]\nhooks = false\n", encoding="utf-8")
    install_binding(scope, "shared", ["claude", "codex"])
    assert json.loads(claude.read_text(encoding="utf-8"))["disableAllHooks"] is True
    assert "hooks = false" in codex.read_text(encoding="utf-8")
    assert "hooks = true" not in codex.read_text(encoding="utf-8")


def test_c6_project_serve_forwards_a_context_profile_and_refuses_others(
    project_store, monkeypatch, tmp_path
):
    scope = context_profile(tmp_path)
    save_project_profile("current", scope)
    save_project_profile(
        "legacy", scope.model_copy(update={"context_only": False, "client": "claude"})
    )
    served = []
    monkeypatch.setattr(context_cli, "execute", lambda args: served.append(args))
    monkeypatch.chdir(tmp_path)
    for name in ("current", "legacy"):
        monkeypatch.setattr(
            sys,
            "argv",
            ["durumari-mcp", "project-serve", "--profile", name, "--client", "claude"],
        )
        if name == "current":
            cli.main()
        else:
            with pytest.raises(SystemExit):
                cli.main()
    assert len(served) == 1
    assert served[0].command == "context-serve"
    assert served[0].profile == "current"
    assert Path(served[0].workspace).resolve() == tmp_path.resolve()
