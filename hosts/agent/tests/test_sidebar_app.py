"""The shipped native and portable plugin both advertise the existing MCP App."""

import json
from pathlib import Path

import pytest

PLUGIN = Path(__file__).parents[1]


@pytest.mark.parametrize("native", [False, True])
def test_official_plugin_advertises_interactive_context_workspace(native):
    path = PLUGIN / (".codex-plugin/plugin.json" if native else "plugin.json")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    interface = (
        manifest["interface"] if native else manifest["extensions"]["com.openai"]["interface"]
    )
    assert manifest["name"] == "durumari"
    assert "Interactive" in interface["capabilities"]
    # Account/folder setup remains responsible for the authenticated MCP connection.
    assert "mcpServers" not in manifest


def test_official_host_interfaces_stay_in_sync():
    portable = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    native = json.loads((PLUGIN / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
    assert native["interface"] == portable["extensions"]["com.openai"]["interface"]
