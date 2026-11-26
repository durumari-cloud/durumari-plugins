"""The shared plugin is a valid GitHub Copilot CLI Agent Plugins 1.0 plugin.

Rules from https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference
"""

import importlib.util
import json
import re

from agent_layout import PLUGIN, ROOT, SKILLS

SCRIPT = PLUGIN / "scripts/durumari_plugin.py"
spec = importlib.util.spec_from_file_location("durumari_plugin_copilot", SCRIPT)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def test_root_manifest_follows_agent_plugins_1_0():
    manifest = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    assert manifest["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    name = manifest["name"]
    assert re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?", name)
    assert "--" not in name and ".." not in name
    # Component path fields are not valid in Agent Plugins manifests.
    assert not {"skills", "mcpServers", "agents", "hooks", "commands"} & set(manifest)
    assert set(manifest) <= {
        "$schema", "name", "version", "description", "author", "homepage",
        "repository", "license", "keywords", "extensions",
    }
    # Portable MCP follows the matching standard, separate from Claude's format.
    # This validates package shape, not a live Copilot OAuth connection.
    mcp = json.loads((PLUGIN / "mcp.json").read_text(encoding="utf-8"))
    assert mcp["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert mcp["mcpServers"]["durumari"]["type"] == "streamable-http"


def test_skills_are_immediate_subdirectories_with_skill_md():
    skills = sorted(p.parent.name for p in SKILLS.glob("*/SKILL.md"))
    assert skills == [
        "durumari-connect",
        "durumari-context",
        "durumari-migrate",
        "durumari-secrets",
        "durumari-share-skill",
        "durumari-status",
        "durumari-update",
        "durumari-work-logs",
    ]
    assert not (PLUGIN / "SKILL.md").exists()


def test_catalog_marketplace_is_readable_by_copilot():
    item = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text(encoding="utf-8"))
    assert re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", item["name"]) and len(item["name"]) <= 64
    assert item["owner"]["name"]
    assert [(p["name"], p["source"]) for p in item["plugins"]] == [
        ("durumari", "./plugins/durumari-agent")
    ]


def test_wrapper_connects_only_copilot_when_selected(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/runtime/python")
    monkeypatch.setattr(plugin, "require_guided_runtime", lambda command: None)
    monkeypatch.setattr(plugin, "bundled_server", lambda: "https://durumari.kr")
    monkeypatch.setattr(plugin, "run", lambda cmd, **kw: calls.append(cmd) or 0)
    args = plugin.parser().parse_args(
        ["connect", "--workspace", str(tmp_path), "--client", "copilot"]
    )
    assert plugin.execute(args) == 0
    assert calls[-1][-2:] == ["--agents", "copilot"]
