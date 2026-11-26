"""Install/remove only the approved project's settings, preserving other tools."""

import json
import tomllib
from pathlib import Path

import pytest
from test_context_v2_adapter import context_profile

from durumari_mcp.credentials import AdapterError


@pytest.mark.parametrize("connections", [1, 2, 3])
@pytest.mark.parametrize("reconnect_after_comment", [False, True])
def test_codex_user_comment_survives_repeated_connect_and_remove(
    tmp_path, connections, reconnect_after_comment
):
    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    for _ in range(connections):
        install_binding(scope, "shared", ["codex"])
    path = tmp_path / ".codex/config.toml"
    comment = "# 사용자 메모: 이 주석은 연결 제거 후에도 남아야 합니다.\n"
    path.write_text(path.read_text(encoding="utf-8") + "\n" + comment, encoding="utf-8")
    if reconnect_after_comment:
        install_binding(scope, "shared", ["codex"])
        assert path.read_text(encoding="utf-8").count(comment) == 1

    result = remove_binding(scope, "shared")

    assert path.exists()
    assert path.read_text(encoding="utf-8").count(comment) == 1
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    assert "durumari_project" not in config.get("mcp_servers", {})
    assert "hooks" not in config.get("features", {})
    assert ".codex/config.toml" in result["restored"]
    assert ".codex/config.toml" not in result["removed"]


def test_codex_table_removal_preserves_comments_but_not_hashes_in_values(tmp_path):
    from durumari_mcp.context_binding import install_binding, remove_codex_block

    install_binding(context_profile(tmp_path), "shared", ["codex"])
    path = tmp_path / ".codex/config.toml"
    text = path.read_text(encoding="utf-8").replace(
        "[mcp_servers.durumari_project]\n",
        '[mcp_servers.durumari_project] # header note\n# inside note\n'
        'description = "hash # is data, not a comment" # inline note\n'
        "literal = 'literal # data'\n"
        'multiline = """first\n# multiline data\nlast""" # multiline note\n',
    )
    text += '\n[mcp_servers.other]\ncommand = "keep-me" # other note\n'

    result = remove_codex_block(text, profile="shared")

    for comment in ("# header note", "# inside note", "# inline note", "# multiline note"):
        assert result.count(comment) == 1
    for value in ("# is data", "# data", "# multiline data"):
        assert value not in result
    assert '[mcp_servers.other]\ncommand = "keep-me" # other note\n' in result
    assert tomllib.loads(result)["mcp_servers"] == {"other": {"command": "keep-me"}}


@pytest.mark.parametrize(
    "value",
    [
        r'"escaped \" # string data"',
        r'"backslash \\ # string data"',
        "'''first\n# string data\nlast'''",
        '"""first\n# string data\nlast"""',
        '"""# string data with final quote""""',
        '"""# string data with two final quotes"""""',
    ],
)
def test_codex_comments_do_not_retain_removed_string_values(value):
    from durumari_mcp.context_binding import toml_comments

    text = f"description = {value} # keep this comment"
    tomllib.loads(text)  # Each fixture is valid TOML, including multiline quote endings.
    assert toml_comments(text) == "# keep this comment\n"


def test_packaged_hooks_use_utf8_under_a_non_utf8_system_locale(tmp_path, monkeypatch):
    from durumari_mcp.context_binding import install_binding

    original = Path.read_text
    templates = []

    def legacy_locale_read(path, *args, **kwargs):
        if path.parent.name == "examples" and path.name.endswith("-hooks.json"):
            templates.append(path.name)
            if kwargs.get("encoding") is None:
                kwargs["encoding"] = "cp1252"
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", legacy_locale_read)
    install_binding(context_profile(tmp_path), "shared", ["codex", "claude"])
    assert set(templates) == {"project-codex-hooks.json", "project-claude-hooks.json"}
    hooks = json.loads((tmp_path / ".codex/hooks.json").read_text(encoding="utf-8"))
    assert hooks["hooks"]


def test_install_all_agents_and_remove_preserves_other_settings(tmp_path):
    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex/config.toml").write_text(
        'model = "existing"\n[mcp_servers.other]\ncommand = "other"\n'
    )
    (tmp_path / "AGENTS.md").write_text("User instructions\n")
    result = install_binding(scope, "shared", ["codex", "claude", "cursor", "gemini", "opencode"])
    assert result["configured_agents"] == ["codex", "claude", "cursor", "gemini", "opencode"]
    assert (
        json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["other"]["command"]
        == "other"
    )
    assert tomllib.loads((tmp_path / ".codex/config.toml").read_text())["model"] == "existing"
    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert config["mcp_servers"]["durumari_project"]["tools"] == {
        "durumari_context_version": {"approval_mode": "approve"}
    }
    assert "approval_policy" not in config and "sandbox_mode" not in config
    for path, key in [
        (".mcp.json", "mcpServers"),
        (".cursor/mcp.json", "mcpServers"),
        (".gemini/settings.json", "mcpServers"),
        ("opencode.json", "mcp"),
    ]:
        entry = json.loads((tmp_path / path).read_text())[key]["durumari_project"]
        assert not any("token" in value for value in entry if isinstance(value, str))
    assert (
        install_binding(scope, "shared", ["codex", "claude", "cursor", "gemini", "opencode"])
        == result
    )
    remove_binding(scope, "shared")
    assert json.loads((tmp_path / ".mcp.json").read_text()) == {
        "mcpServers": {"other": {"command": "other"}}
    }
    assert tomllib.loads((tmp_path / ".codex/config.toml").read_text())["mcp_servers"] == {
        "other": {"command": "other"}
    }
    assert (tmp_path / "AGENTS.md").read_text().strip() == "User instructions"
    # Decision 1: the team's shared manifest stays and never held per-user keys.
    import yaml

    manifest = yaml.safe_load((tmp_path / "durumari.yaml").read_text())
    assert manifest["project_id"] == scope.project_id
    assert not {"profile", "workspace_identity"} & set(manifest)


def test_conflicting_binding_is_detected_before_any_files_change(tmp_path):
    from durumari_mcp.context_binding import install_binding

    scope = context_profile(tmp_path)
    old = json.dumps({"mcpServers": {"durumari_project": {"command": "user-custom-tool"}}})
    (tmp_path / ".cursor").mkdir()
    (tmp_path / ".cursor/mcp.json").write_text(old)
    with pytest.raises(AdapterError, match="existing_mcp_binding_conflict"):
        install_binding(scope, "shared", ["codex", "cursor"])
    assert (tmp_path / ".cursor/mcp.json").read_text() == old
    assert not (tmp_path / ".codex/config.toml").exists()


def test_symlinked_config_cannot_modify_an_outside_project(tmp_path):
    from durumari_mcp.context_binding import install_binding

    scope = context_profile(tmp_path)
    outside = tmp_path.parent / "outside-config"
    outside.write_text("preserve")
    (tmp_path / ".mcp.json").symlink_to(outside)
    with pytest.raises(AdapterError):
        install_binding(scope, "shared", ["claude"])
    assert outside.read_text() == "preserve"


def test_managed_block_points_every_agent_to_the_policy_index(tmp_path):
    from durumari_mcp.context_binding import install_binding

    install_binding(context_profile(tmp_path), "shared", ["codex", "claude", "gemini"])
    block = (tmp_path / "AGENTS.md").read_text(encoding="utf-8")
    assert "check the project Policy index" in block
    assert "durumari_policy_index" in block
    assert "ask the user before acting" in block
    # Claude Code and Gemini CLI read AGENTS.md through one import line.
    for name in ("CLAUDE.md", "GEMINI.md"):
        text = (tmp_path / name).read_text(encoding="utf-8")
        assert "@AGENTS.md" in text.splitlines()
        assert "Policy index" not in text


def owned_by(path: Path, key: str) -> list[str]:
    if not path.exists():
        return []
    entry = json.loads(path.read_text()).get(key, {}).get("durumari_project")
    return entry.get("args", []) if entry else []


def test_split_connection_is_recorded_and_removed_as_a_whole(tmp_path):
    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude", "codex"])
    result = install_binding(scope, "shared", ["cursor"])
    assert result["configured_agents"] == ["cursor"]
    record = json.loads((tmp_path / ".durumari/context-binding.json").read_text())
    assert {"CLAUDE.md", "AGENTS.md", ".mcp.json", ".cursor/mcp.json"} <= set(
        record.pop("created_files")
    )
    assert record.pop("gitignore_added")[0] == "/.codex/config.toml"
    assert record == {
        "profile": "shared",
        "agents": ["claude", "codex", "cursor"],
        "workspace_identity": scope.workspace_identity,
        "created_directories": [".claude", ".codex", ".cursor", ".durumari"],
        "codex_hooks_feature": True,
    }
    remove_binding(scope, "shared")
    assert owned_by(tmp_path / ".mcp.json", "mcpServers") == []
    assert owned_by(tmp_path / ".cursor/mcp.json", "mcpServers") == []
    # The created Codex settings held only our server and hooks feature.
    assert not (tmp_path / ".codex").exists()
    for hooks in (".claude/settings.local.json", ".codex/hooks.json"):
        # Removal deletes a settings file that only held our entries (contract decision 2).
        path = tmp_path / hooks
        assert not path.exists() or "project-hook" not in path.read_text()
    assert not (tmp_path / ".durumari/context-binding.json").exists()


@pytest.mark.parametrize("record", [None, "{broken", '{"profile": "shared", "agents": "x"}'])
def test_removal_sweeps_this_profiles_settings_missing_from_the_record(tmp_path, record):
    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude", "codex", "gemini"])
    # A record written by an older plugin, or damaged, lists only the last call.
    path = tmp_path / ".durumari/context-binding.json"
    if record is None:
        path.write_text(json.dumps({"profile": "shared", "agents": ["gemini", "retired"]}))
        remove_binding(scope, "shared")
    else:
        path.write_text(record)
        install_binding(scope, "shared", ["gemini"])
        assert json.loads(path.read_text())["agents"] == ["codex", "claude", "gemini"]
        remove_binding(scope, "shared")
    assert owned_by(tmp_path / ".mcp.json", "mcpServers") == []
    assert owned_by(tmp_path / ".gemini/settings.json", "mcpServers") == []
    assert "durumari_project" not in (tmp_path / ".codex/config.toml").read_text()
    hooks = tmp_path / ".claude/settings.local.json"
    assert not hooks.exists() or "project-hook" not in hooks.read_text()


def test_split_removal_preserves_other_profile_and_user_entries(tmp_path):
    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    other = {
        "command": "python",
        "args": ["-m", "durumari_mcp", "context-serve", "--profile", "other"],
    }
    user = {"command": "user-custom-tool"}
    (tmp_path / ".gemini").mkdir()
    (tmp_path / ".gemini/settings.json").write_text(
        json.dumps({"mcpServers": {"durumari_project": other}})
    )
    (tmp_path / "opencode.json").write_text(json.dumps({"mcp": {"durumari_project": user}}))
    install_binding(scope, "shared", ["claude"])
    install_binding(scope, "shared", ["cursor"])
    record = json.loads((tmp_path / ".durumari/context-binding.json").read_text())
    assert record["agents"] == ["claude", "cursor"]
    remove_binding(scope, "shared")
    assert owned_by(tmp_path / ".mcp.json", "mcpServers") == []
    assert owned_by(tmp_path / ".cursor/mcp.json", "mcpServers") == []
    gemini = json.loads((tmp_path / ".gemini/settings.json").read_text())
    assert gemini["mcpServers"]["durumari_project"] == other
    opencode = json.loads((tmp_path / "opencode.json").read_text())
    assert opencode["mcp"]["durumari_project"] == user


def test_recorded_agent_replaced_by_another_profile_still_conflicts(tmp_path):
    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    install_binding(scope, "shared", ["cursor"])
    other = {"command": "python", "args": ["-m", "durumari_mcp", "context-serve", "--profile", "x"]}
    replaced = json.dumps({"mcpServers": {"durumari_project": other}})
    (tmp_path / ".mcp.json").write_text(replaced)
    with pytest.raises(AdapterError, match="existing_mcp_binding_conflict"):
        remove_binding(scope, "shared")
    assert (tmp_path / ".mcp.json").read_text() == replaced
    assert owned_by(tmp_path / ".cursor/mcp.json", "mcpServers")


def test_removal_reports_deleted_and_restored_files(tmp_path):
    # Contract section 8: the user is told what removal changed, file by file.
    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}}))
    install_binding(scope, "shared", ["claude"])
    result = remove_binding(scope, "shared")
    assert ".mcp.json" in result["restored"]
    assert ".durumari/context-binding.json" in result["removed"]
    assert "CLAUDE.md" in result["removed"]  # It held only our generated block.
    assert not set(result["removed"]) & set(result["restored"])
    for path in result["removed"]:
        assert not (tmp_path / path).exists()
