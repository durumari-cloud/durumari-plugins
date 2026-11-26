import json

import pytest
from test_context_v2_adapter import context_profile

from durumari_mcp import context_binding, releases
from durumari_mcp.releases import ReleaseError


def test_detects_agy_independently_of_gemini(monkeypatch, tmp_path):
    monkeypatch.setattr(
        releases.shutil, "which", lambda name: "/bin/agy" if name == "agy" else None
    )
    monkeypatch.setattr(releases.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(releases, "bundled_claude", lambda: None)
    monkeypatch.setattr(releases, "cursor_present", lambda: False)
    assert releases.detect_clients() == {"antigravity"}
    assert releases.host_command("antigravity") == "/bin/agy"


def test_finds_fresh_official_install_outside_path(monkeypatch, tmp_path):
    monkeypatch.setattr(releases.shutil, "which", lambda name: None)
    monkeypatch.setattr(releases.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(releases.sys, "platform", "darwin")
    binary = tmp_path / ".local/bin/agy"
    binary.parent.mkdir(parents=True)
    binary.write_text("synthetic test")
    binary.chmod(0o755)
    assert releases.antigravity_command() == str(binary)


@pytest.mark.parametrize(
    "receipt",
    [
        [],
        [{"name": "durumari", "version": "1.0.0", "enabled": False}],
        [{"name": "durumari", "version": "0.9.0", "enabled": True}],
        {"name": "durumari"},
        None,
    ],
)
def test_unconfirmed_native_install_is_rejected(monkeypatch, tmp_path, receipt):
    monkeypatch.setattr(releases, "host_command", lambda name: "agy")
    monkeypatch.setattr(releases, "run", lambda command: json.dumps(receipt))
    with pytest.raises(ReleaseError, match="native_plugin_version_not_verified"):
        releases.install_host("antigravity", tmp_path, "1.0.0")


def test_native_install_uses_agy_and_verifies_enabled_version(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(releases, "host_command", lambda name: "/bin/agy")

    def run(command):
        calls.append(command)
        return json.dumps([{"name": "durumari", "version": "1.0.0", "enabled": True}])

    monkeypatch.setattr(releases, "run", run)
    releases.install_host("antigravity", tmp_path, "1.0.0")
    assert calls == [
        ["/bin/agy", "plugin", "install", str(tmp_path / "plugins/durumari-agent")],
        ["/bin/agy", "plugin", "list", "--json"],
    ]


def test_binding_preserves_other_mcp_servers_and_legacy_gemini(monkeypatch, tmp_path):
    scope = context_profile(tmp_path)
    target = tmp_path / ".agents/mcp_config.json"
    target.parent.mkdir()
    target.write_text(json.dumps({"mcpServers": {"other": {"command": "other"}}, "keep": True}))
    result = context_binding.install_binding(scope, "shared", ["antigravity"])
    config = json.loads(target.read_text())
    assert result["configured_agents"] == ["antigravity"]
    assert config["keep"] is True
    assert config["mcpServers"]["other"] == {"command": "other"}
    args = config["mcpServers"]["durumari_project"]["args"]
    assert args[-2:] == ["--client", "antigravity"]
    assert not (tmp_path / ".gemini/settings.json").exists()
    context_binding.remove_binding(scope, "shared")
    assert json.loads(target.read_text()) == {
        "mcpServers": {"other": {"command": "other"}},
        "keep": True,
    }


def test_one_line_install_detects_antigravity_and_records_it(monkeypatch, tmp_path):
    from test_install_detect import release, run

    installed = release(monkeypatch, tmp_path, on_path={"agy"})
    assert run()["clients"] == ["antigravity"]
    assert installed == ["antigravity"]
    assert releases.load_state(tmp_path / "releases")["clients"] == ["antigravity"]
