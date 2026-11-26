"""GitHub Copilot CLI project binding through the shared project .mcp.json."""

import json

import pytest
from test_context_v2_adapter import context_profile

from durumari_mcp.context_binding import AGENTS, DEFAULT_AGENTS, install_binding, remove_binding
from durumari_mcp.credentials import AdapterError


def entry(root):
    return json.loads((root / ".mcp.json").read_text())["mcpServers"]["durumari_project"]


def test_copilot_is_explicit_and_uses_the_common_context_server(tmp_path):
    assert "copilot" in AGENTS and "copilot" not in DEFAULT_AGENTS
    scope = context_profile(tmp_path)
    original = {"mcpServers": {"other": {"type": "local", "command": "o", "tools": ["*"]}}}
    (tmp_path / ".mcp.json").write_text(json.dumps(original))
    (tmp_path / "AGENTS.md").write_text("Team rules\n")
    result = install_binding(scope, "shared", ["copilot"])
    assert result["configured_agents"] == ["copilot"]
    value = entry(tmp_path)
    assert value["type"] == "stdio"
    assert value["args"] == [
        "-m", "durumari_mcp", "context-serve", "--profile", "shared", "--client", "copilot"
    ]
    assert not any("token" in str(part) for part in value["args"])
    assert json.loads((tmp_path / ".mcp.json").read_text())["mcpServers"]["other"] == (
        original["mcpServers"]["other"]
    )
    # Copilot CLI reads AGENTS.md; the managed block carries the Context contract.
    assert "Durumari Cloud Context" in (tmp_path / "AGENTS.md").read_text()
    assert not (tmp_path / ".claude/settings.local.json").exists()
    scope.check_client("copilot")
    remove_binding(scope, "shared")
    assert json.loads((tmp_path / ".mcp.json").read_text()) == original
    assert (tmp_path / "AGENTS.md").read_text().strip() == "Team rules"


def test_claude_and_copilot_share_one_entry_and_keep_claude_hooks(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude", "copilot"])
    value = entry(tmp_path)
    assert value["type"] == "stdio" and value["args"][-2:] == ["--client", "mcp"]
    scope.check_client("mcp")
    hooks = json.loads((tmp_path / ".claude/settings.local.json").read_text())["hooks"]
    assert hooks
    assert install_binding(scope, "shared", ["claude", "copilot"])["configured_agents"] == [
        "claude",
        "copilot",
    ]
    remove_binding(scope, "shared")
    # Only our shared entry was there, so removal deletes the file (contract decision 2).
    assert not (tmp_path / ".mcp.json").exists()


def test_claude_only_entry_is_unchanged_by_the_copilot_adapter(tmp_path):
    install_binding(context_profile(tmp_path), "shared", ["claude"])
    assert "type" not in entry(tmp_path)


def test_reconnecting_claude_does_not_record_copilot_from_the_shared_file(tmp_path):
    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    install_binding(scope, "shared", ["claude"])
    record = json.loads((tmp_path / ".durumari/context-binding.json").read_text())
    assert record["agents"] == ["claude"]
    assert entry(tmp_path)["args"][-2:] == ["--client", "claude"]


def test_foreign_entry_blocks_copilot_without_changes(tmp_path):
    scope = context_profile(tmp_path)
    foreign = json.dumps({"mcpServers": {"durumari_project": {"command": "someone-else"}}})
    (tmp_path / ".mcp.json").write_text(foreign)
    with pytest.raises(AdapterError, match="existing_mcp_binding_conflict"):
        install_binding(scope, "shared", ["codex", "copilot"])
    assert (tmp_path / ".mcp.json").read_text() == foreign
    assert not (tmp_path / ".codex/config.toml").exists()


def test_copilot_label_cannot_cross_into_a_nested_repository(tmp_path):
    import subprocess

    scope = context_profile(tmp_path)
    child = tmp_path / "vendor"
    child.mkdir()
    subprocess.run(["git", "init", "-q", str(child)], check=True)
    scope.check_client("copilot")
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(child))


def test_context_connect_defaults_exclude_experimental_copilot():
    import argparse

    from durumari_mcp.context_cli import add_commands

    parser = argparse.ArgumentParser()
    add_commands(parser.add_subparsers(dest="command"))
    assert "copilot" not in parser.parse_args(["context-connect"]).agents
    assert parser.parse_args(["context-connect", "--agents", "copilot"]).agents == ["copilot"]
