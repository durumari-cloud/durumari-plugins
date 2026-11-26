"""Connection-contract decisions recorded in docs/connection-contract.md section 12."""

import json
import sys

import pytest
from test_context_v2_adapter import context_profile
from test_continuity import project_store as store_fixture

from durumari_mcp import cli
from durumari_mcp.context_binding import install_binding, remove_binding
from durumari_mcp.project_credentials import save_project_profile
from durumari_mcp.project_gateway import ProjectGateway

project_store = store_fixture


@pytest.mark.parametrize(
    ("agents", "expected"),
    [
        # AGENTS.md is the source CLAUDE.md and GEMINI.md import.
        (["claude"], {"AGENTS.md", "CLAUDE.md"}),
        (["codex"], {"AGENTS.md"}),
        (["gemini"], {"AGENTS.md", "GEMINI.md"}),
        (["cursor", "opencode"], {"AGENTS.md"}),
        (["claude", "codex", "gemini"], {"AGENTS.md", "CLAUDE.md", "GEMINI.md"}),
    ],
)
def test_d3_only_selected_agents_get_instruction_files(tmp_path, agents, expected):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", agents)
    created = {
        name for name in ("AGENTS.md", "CLAUDE.md", "GEMINI.md") if (tmp_path / name).exists()
    }
    assert created == expected


def test_d3_an_existing_managed_block_is_still_kept_current(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["codex"])
    agents_md = tmp_path / "AGENTS.md"
    agents_md.write_text(agents_md.read_text(encoding="utf-8").replace("Policy", "OLD"), "utf-8")
    install_binding(scope, "shared", ["claude"])
    assert "OLD" not in agents_md.read_text(encoding="utf-8")


def test_d2_remove_deletes_files_it_left_empty_and_keeps_user_content(tmp_path):
    scope = context_profile(tmp_path)
    (tmp_path / "CLAUDE.md").write_text("# Team notes\n", encoding="utf-8")
    install_binding(scope, "shared", ["claude", "gemini"])
    remove_binding(scope, "shared")
    assert (tmp_path / "CLAUDE.md").read_text(encoding="utf-8").strip() == "# Team notes"
    assert not (tmp_path / "GEMINI.md").exists()
    assert not (tmp_path / ".mcp.json").exists()
    assert not (tmp_path / ".gemini" / "settings.json").exists()
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_d2_remove_keeps_settings_that_still_hold_other_entries(tmp_path):
    scope = context_profile(tmp_path)
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "other"}}}), encoding="utf-8"
    )
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir()
    settings.write_text(json.dumps({"permissions": {"allow": ["Read"]}}), encoding="utf-8")
    install_binding(scope, "shared", ["claude"])
    remove_binding(scope, "shared")
    servers = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]
    assert servers == {"other": {"command": "other"}}
    kept = json.loads(settings.read_text(encoding="utf-8"))
    assert kept["permissions"] == {"allow": ["Read"]}


@pytest.mark.parametrize("event", ["SessionStart", "UserPromptSubmit", "Stop"])
@pytest.mark.parametrize("owner", ["user", "other-profile"])
def test_removal_keeps_unowned_hooks_and_permissions(tmp_path, event, owner):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    path = tmp_path / ".claude/settings.local.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    hook = {"type": "command", "command": "user-command"}
    if owner == "other-profile":
        hook = {
            "type": "command",
            "command": sys.executable,
            "args": [
                "-m", "durumari_mcp", "project-hook", "--profile", "other", "--client", "claude"
            ],
        }
    group = {"matcher": "user-matcher", "hooks": [hook]}
    values["hooks"][event].append(group)
    values["permissions"] = {"allow": ["Read"]}
    path.write_text(json.dumps(values), encoding="utf-8")

    result = remove_binding(scope, "shared")

    assert ".claude/settings.local.json" in result["restored"]
    kept = json.loads(path.read_text(encoding="utf-8"))
    expected_hooks = {name: [] for name in values["hooks"]}
    expected_hooks[event] = [group]
    assert kept == {"hooks": expected_hooks, "permissions": {"allow": ["Read"]}}


@pytest.mark.parametrize(
    ("path_name", "content", "agent"),
    [
        ("CLAUDE.md", " \n\t\n", "claude"),
        (".claude/settings.local.json", '{"permissions":{"allow":[]}}\n', "claude"),
        ("opencode.jsonc", '{\n// Keep this team setting\n"mcp":{}\n}\n', "opencode"),
    ],
)
def test_removal_keeps_existing_empty_user_files(tmp_path, path_name, content, agent):
    path = tmp_path / path_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", [agent])
    install_binding(scope, "shared", [agent])
    result = remove_binding(scope, "shared")
    assert path.exists()
    assert path_name in result["restored"]
    if path_name.endswith(".jsonc"):
        assert "// Keep this team setting" in path.read_text(encoding="utf-8")
    if path_name.endswith(".json"):
        assert json.loads(path.read_text(encoding="utf-8"))["permissions"] == {"allow": []}


def test_removal_keeps_user_values_added_to_generated_settings(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    path = tmp_path / ".claude/settings.local.json"
    values = json.loads(path.read_text(encoding="utf-8"))
    values["permissions"] = {"allow": []}
    path.write_text(json.dumps(values), encoding="utf-8")
    remove_binding(scope, "shared")
    assert json.loads(path.read_text(encoding="utf-8"))["permissions"] == {"allow": []}


@pytest.mark.parametrize("command", ["project-import", "project-export"])
def test_d4_import_and_export_are_retired_without_touching_files(
    monkeypatch, tmp_path, capsys, command
):
    before = sorted(path.name for path in tmp_path.iterdir())
    argv = ["durumari-mcp", command, "--workspace", str(tmp_path)]
    if command == "project-export":
        argv += ["--output", str(tmp_path / "export.json")]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    assert "agent_upgrade_required_use_context_connect" in capsys.readouterr().err
    assert sorted(path.name for path in tmp_path.iterdir()) == before


@pytest.mark.parametrize("quarantined", [False, True])
def test_d6_status_counts_pending_writes_for_the_whole_connection(
    project_store, tmp_path, monkeypatch, quarantined
):
    from durumari_mcp.context_client import ContextClient

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "claude", root=tmp_path / "state")
    client = ContextClient(gateway, "this-session")
    if quarantined:
        client.queue.enqueue("old", "old-session", "POST", client.queue.prefix, {})
        assert client.queue.quarantine()
    client.queue.enqueue("key-a", "this-session", "POST", client.queue.prefix, {"title": "a"})
    client.queue.enqueue("key-b", "other-session", "POST", client.queue.prefix, {"title": "b"})
    monkeypatch.setattr(
        gateway,
        "context_request",
        lambda path, **kw: {
            "id": scope.connection_id,
            "project_id": scope.project_id,
            "repository_id": scope.repository_id,
            "workspace_identity": scope.workspace_identity,
            "state": "active",
        },
    )
    monkeypatch.setattr(client, "release", lambda: None)
    status = client.status()
    assert status["pending_writes"] == 2
    assert status["pending_writes_this_session"] == 1
    assert status["quarantined_writes_recovery_required"] is quarantined


def test_d1_shared_manifest_holds_no_per_user_keys_and_a_clone_has_no_profile(tmp_path):
    import yaml

    from durumari_mcp.context_binding import bound_profile
    from durumari_mcp.onboarding import read_manifest

    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    manifest = read_manifest(tmp_path)
    assert not {"profile", "workspace_identity"} & set(manifest)
    # 0.4.8 requires private keys whenever context_mode is present; the shared
    # Git form stays readable to those already approved runtimes by omitting it.
    assert "context_mode" not in manifest
    assert bound_profile(tmp_path, manifest) == "shared"
    # Another teammate's clone has the committed manifest but not the ignored record.
    clone = tmp_path / "clone"
    clone.mkdir()
    (clone / "durumari.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    assert bound_profile(clone, read_manifest(clone)) is None


def test_d1_legacy_manifest_is_still_read_and_removal_keeps_its_shared_keys(tmp_path):
    import yaml

    from durumari_mcp.context_binding import bound_profile, workspace_identity
    from durumari_mcp.onboarding import read_manifest

    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    path = tmp_path / "durumari.yaml"
    legacy = {
        **yaml.safe_load(path.read_text()),
        "context_mode": "cloud-context",
        "workspace_identity": workspace_identity(tmp_path),
        "profile": "shared",
    }
    path.write_text(yaml.safe_dump(legacy, sort_keys=False))
    (tmp_path / ".durumari/context-binding.json").write_text(
        json.dumps({"profile": "shared", "agents": ["claude"]})
    )
    assert bound_profile(tmp_path, read_manifest(tmp_path)) == "shared"
    result = remove_binding(scope, "shared")
    assert "durumari.yaml" in result["restored"]
    kept = yaml.safe_load(path.read_text())
    assert kept == {
        key: value for key, value in legacy.items() if key not in {"profile", "workspace_identity"}
    }


def test_d1_a_partial_legacy_manifest_is_rejected(tmp_path):
    import yaml

    from durumari_mcp.credentials import AdapterError
    from durumari_mcp.onboarding import read_manifest

    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    path = tmp_path / "durumari.yaml"
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), "profile": "shared"}))
    with pytest.raises(AdapterError, match="invalid_project_manifest"):
        read_manifest(tmp_path)


def test_remove_undoes_everything_connect_created_in_a_fresh_folder(tmp_path):
    # Windows 11 regression 2026-10-21: the .gitignore block, the Codex hooks feature
    # and empty .claude/.durumari folders were left behind after context-remove.
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["codex", "claude", "cursor", "gemini", "opencode"])
    result = remove_binding(scope, "shared")
    # The shared manifest stays for the team (decision 1); nothing else remains.
    assert sorted(path.name for path in tmp_path.iterdir()) == [".git", "durumari.yaml"]
    assert {".gitignore", ".codex/config.toml", ".claude/", ".durumari/"} <= set(result["removed"])


def test_remove_restores_existing_user_files_exactly(tmp_path):
    scope = context_profile(tmp_path)
    ignore = "node_modules/\n.env\n"
    codex = 'model = "existing"\n\n[features]\nweb_search = true\n'
    (tmp_path / ".gitignore").write_text(ignore, encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(codex, encoding="utf-8")
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude/notes.md").write_text("mine\n", encoding="utf-8")
    install_binding(scope, "shared", ["codex", "claude"])
    assert "hooks = true" in (tmp_path / ".codex/config.toml").read_text(encoding="utf-8")
    remove_binding(scope, "shared")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == ignore
    assert (tmp_path / ".codex/config.toml").read_text(encoding="utf-8") == codex
    assert (tmp_path / ".claude/notes.md").exists()
    assert not (tmp_path / ".durumari").exists()


def test_remove_keeps_a_hooks_feature_and_ignore_lines_the_user_already_had(tmp_path):
    scope = context_profile(tmp_path)
    (tmp_path / ".gitignore").write_text("/.mcp.json\n", encoding="utf-8")
    (tmp_path / ".codex").mkdir()
    codex = "[features]\nhooks = true\n"
    (tmp_path / ".codex/config.toml").write_text(codex, encoding="utf-8")
    install_binding(scope, "shared", ["codex", "claude"])
    remove_binding(scope, "shared")
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == "/.mcp.json\n"
    assert (tmp_path / ".codex/config.toml").read_text(encoding="utf-8") == codex


def test_remove_keeps_user_lines_added_inside_the_durumari_ignore_block(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    path = tmp_path / ".gitignore"
    path.write_text(path.read_text(encoding="utf-8") + "dist/\n", encoding="utf-8")
    remove_binding(scope, "shared")
    assert path.read_text(encoding="utf-8") == "# Durumari local Agent settings\ndist/\n"


def test_remove_with_an_older_record_cleans_only_files_it_created(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["codex", "claude"])
    record_path = tmp_path / ".durumari/context-binding.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    for key in ("created_directories", "gitignore_added", "codex_hooks_feature"):
        record.pop(key)  # The 0.4.31 record layout.
    record_path.write_text(json.dumps(record), encoding="utf-8")
    remove_binding(scope, "shared")
    assert sorted(path.name for path in tmp_path.iterdir()) == [".git", "durumari.yaml"]


def test_remove_keeps_a_committed_durumari_ignore_block(tmp_path):
    import subprocess

    scope = context_profile(tmp_path)  # A Git repository with an origin remote.
    install_binding(scope, "shared", ["claude"])
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@example.com"]
    subprocess.run([*git, "add", ".gitignore"], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "ignore"], check=True)
    before = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    remove_binding(scope, "shared")
    # Teammates rely on the committed block; one user's removal leaves shared files alone.
    assert (tmp_path / ".gitignore").read_text(encoding="utf-8") == before
