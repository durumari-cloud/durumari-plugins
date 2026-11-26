"""The Claude Code plugin declares the remote account MCP; the local launcher stays opt-in."""

import json

from agent_layout import CLAUDE_PUBLIC, PLUGIN


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_claude_plugin_declares_remote_mcp_in_the_public_format():
    manifest = load(PLUGIN / ".claude-plugin/plugin.json")
    assert manifest["mcpServers"] == "./mcp-claude.json"
    declared = load(PLUGIN / "mcp-claude.json")
    reference = load(CLAUDE_PUBLIC / ".mcp.json")
    assert set(declared) == {"mcpServers"}
    [(name, entry)] = declared["mcpServers"].items()
    assert name == "durumari"
    assert entry == reference["mcpServers"]["durumari"]
    assert entry == {"type": "http", "url": "https://mcp.durumari.cloud/mcp"}


def test_portable_root_and_cursor_discover_one_shared_remote_mcp_config():
    cursor = load(PLUGIN / ".cursor-plugin/plugin.json")
    portable = load(PLUGIN / "plugin.json")
    assert "mcpServers" not in cursor
    assert "mcpServers" not in portable
    assert portable["$schema"] == "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
    config = load(PLUGIN / "mcp.json")
    assert config["$schema"] == "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
    assert config["mcpServers"] == {"durumari": {
        "type": "streamable-http", "url": "https://mcp.durumari.cloud/mcp",
    }}
    assert not (PLUGIN / ".mcp.json").exists()
