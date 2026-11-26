"""OpenCode v1/v2 settings: detection, migration, preservation and skill discovery."""

import json
import re
from pathlib import Path

import pytest
from test_context_v2_adapter import context_profile

from durumari_mcp import opencode_config
from durumari_mcp.config_json import load
from durumari_mcp.context_binding import install_binding, remove_binding
from durumari_mcp.credentials import AdapterError

# Field sets from https://opencode.ai/config.json (v1 McpLocalConfig, additionalProperties
# false) and https://opencode.ai/v2/docs/mcp-servers (v2 local server fields).
V1_FIELDS = {"type", "command", "cwd", "environment", "enabled", "timeout"}
V2_FIELDS = {"type", "command", "cwd", "environment", "disabled", "codemode", "timeout", "protocol"}
SKILL_ID = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


@pytest.fixture
def version(monkeypatch):
    def use(value):
        monkeypatch.setattr(opencode_config, "installed_version", lambda: value)

    use(None)
    return use


def config(root):
    return load((root / "opencode.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, (None, "not_installed")),
        ("", (None, "unknown_version")),
        ("1.18.35", ("v1", "installed:1.18.35")),
        ("0.15.2", ("v1", "installed:0.15.2")),
        ("2.0.26", ("v2", "installed:2.0.26")),
        ("0.0.0-beta-19507", (None, "channel_build:0.0.0-beta-19507")),
        ("0.0.0-dev-202610092229", (None, "channel_build:0.0.0-dev-202610092229")),
    ],
)
def test_version_policy(value, expected):
    assert opencode_config.detected_format(value) == expected


def test_unsupported_major_is_rejected_unless_explicit(tmp_path, version):
    scope = context_profile(tmp_path)
    version("3.0.0")
    with pytest.raises(AdapterError, match="unsupported_opencode_version"):
        install_binding(scope, "shared", ["codex", "opencode"])
    assert not (tmp_path / "opencode.json").exists()
    assert not (tmp_path / ".codex/config.toml").exists()
    result = install_binding(scope, "shared", ["opencode"], opencode_format="v2")
    assert result["opencode"]["config"] == "v2"


def test_v1_install_is_schema_shaped_and_skill_is_discoverable(tmp_path, version):
    scope = context_profile(tmp_path)
    version("1.18.35")
    result = install_binding(scope, "shared", ["opencode"])
    assert result["opencode"] == {
        "config": "v1",
        "basis": "installed:1.18.35",
        "skill": "managed",
    }
    entry = config(tmp_path)["mcp"]["durumari_project"]
    assert set(entry) <= V1_FIELDS and entry["type"] == "local" and entry["enabled"] is True
    assert entry["command"][1:5] == ["-m", "durumari_mcp", "context-serve", "--profile"]
    assert "servers" not in config(tmp_path)["mcp"]
    skill = tmp_path / opencode_config.SKILL_PATH
    front = skill.read_text(encoding="utf-8").split("---")[1]
    name = re.search(r"(?m)^name: (.*)$", front).group(1)
    assert SKILL_ID.fullmatch(name) and name == skill.parent.name
    assert 1 <= len(re.search(r"(?m)^description: (.*)$", front).group(1)) <= 1024
    assert "/" + opencode_config.SKILL_PATH in (tmp_path / ".gitignore").read_text().splitlines()
    remove_binding(scope, "shared")
    # The file only held our server, so removal deletes it (contract decision 2).
    assert not (tmp_path / "opencode.json").exists()
    assert not skill.exists()


def test_v2_install_uses_servers_and_disabled_and_removes_cleanly(tmp_path, version):
    scope = context_profile(tmp_path)
    version("2.0.26")
    install_binding(scope, "shared", ["opencode"])
    mcp = config(tmp_path)["mcp"]
    entry = mcp["servers"]["durumari_project"]
    assert set(entry) <= V2_FIELDS and "enabled" not in entry and "disabled" not in entry
    assert "durumari_project" not in mcp
    assert install_binding(scope, "shared", ["opencode"])["opencode"]["config"] == "v2"
    remove_binding(scope, "shared")
    assert not (tmp_path / "opencode.json").exists()


def test_connected_v1_file_migrates_to_v2_keeping_profile_disable_and_local_state(
    tmp_path, version
):
    scope = context_profile(tmp_path)
    version("1.18.35")
    install_binding(scope, "shared", ["opencode"])
    text = (tmp_path / "opencode.json").read_text().replace('"enabled": true', '"enabled": false')
    (tmp_path / "opencode.json").write_text(text)
    pending = tmp_path / ".durumari" / "pending-sentinel"
    pending.write_text("queued")
    before = config(tmp_path)["mcp"]["durumari_project"]["command"]
    version("2.0.26")
    install_binding(scope, "shared", ["opencode"])
    mcp = config(tmp_path)["mcp"]
    assert "durumari_project" not in mcp
    assert mcp["servers"]["durumari_project"] == {
        "type": "local",
        "command": before,
        "disabled": True,
    }
    assert pending.read_text() == "queued"
    assert json.loads((tmp_path / ".durumari/context-binding.json").read_text())["profile"] == (
        "shared"
    )


def test_native_v2_file_is_never_handed_to_v1(tmp_path, version):
    scope = context_profile(tmp_path)
    original = '{"mcp": {"servers": {"other": {"type": "local", "command": ["x"]}}}}\n'
    (tmp_path / "opencode.json").write_text(original)
    version("1.18.35")
    with pytest.raises(AdapterError, match="opencode_v1_cannot_read_v2_configuration"):
        install_binding(scope, "shared", ["claude", "opencode"])
    assert (tmp_path / "opencode.json").read_text() == original
    assert not (tmp_path / ".mcp.json").exists()
    version(None)
    result = install_binding(scope, "shared", ["opencode"])
    assert result["opencode"]["basis"] == "existing_v2_configuration"
    assert config(tmp_path)["mcp"]["servers"]["other"] == {"type": "local", "command": ["x"]}
    remove_binding(scope, "shared")
    assert load(original) == config(tmp_path)


def test_existing_v1_servers_stay_v1_under_v2_and_mixed_files_are_refused(tmp_path, version):
    scope = context_profile(tmp_path)
    original = '{"mcp": {"other": {"type": "remote", "url": "https://x.invalid"}}}\n'
    (tmp_path / "opencode.json").write_text(original)
    version("2.0.26")
    result = install_binding(scope, "shared", ["opencode"])
    assert result["opencode"]["basis"] == "existing_v1_configuration"
    assert config(tmp_path)["mcp"]["durumari_project"]["enabled"] is True
    with pytest.raises(AdapterError, match="opencode_configuration_mixed_formats"):
        install_binding(scope, "shared", ["opencode"], opencode_format="v2")
    remove_binding(scope, "shared")
    assert config(tmp_path) == load(original)
    mixed = '{"mcp": {"other": {"type": "local", "command": ["a"]}, "servers": {"b": {}}}}'
    (tmp_path / "opencode.json").write_text(mixed)
    with pytest.raises(AdapterError, match="opencode_configuration_mixed_formats"):
        install_binding(scope, "shared", ["opencode"])
    assert (tmp_path / "opencode.json").read_text() == mixed


def test_jsonc_comments_and_user_conflicts_survive_v2(tmp_path, version):
    scope = context_profile(tmp_path)
    version("2.0.26")
    original = (
        '{\n  // keep me\n  "model": "own",\n  "mcp": {\n    "servers": {\n'
        '      /* keep */ "other": {"type": "local", "command": ["o"],},\n    },\n  },\n}\n'
    )
    path = tmp_path / "opencode.jsonc"
    path.write_text(original)
    install_binding(scope, "shared", ["opencode"])
    assert "// keep me" in path.read_text() and "/* keep */" in path.read_text()
    assert not (tmp_path / "opencode.json").exists()
    remove_binding(scope, "shared")
    assert load(path.read_text()) == load(original)
    user = '{"mcp": {"servers": {"durumari_project": {"type": "local", "command": ["mine"]}}}}'
    path.write_text(user)
    with pytest.raises(AdapterError, match="existing_mcp_binding_conflict"):
        install_binding(scope, "shared", ["opencode"])
    assert path.read_text() == user
    (tmp_path / "opencode.json").write_text("{}")
    with pytest.raises(AdapterError, match="ambiguous_agent_configuration"):
        install_binding(scope, "shared", ["opencode"])


def test_unknown_or_missing_versions_use_the_shape_both_versions_read(tmp_path, version):
    scope = context_profile(tmp_path)
    version("0.0.0-beta-19507")
    result = install_binding(scope, "shared", ["opencode"])
    assert result["opencode"]["config"] == "v1"
    assert result["opencode"]["basis"] == "channel_build:0.0.0-beta-19507_compatible_v1"


def test_users_own_skill_is_preserved(tmp_path, version):
    scope = context_profile(tmp_path)
    skill = tmp_path / opencode_config.SKILL_PATH
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: durumari-context\ndescription: mine\n---\nmine\n")
    assert install_binding(scope, "shared", ["opencode"])["opencode"]["skill"] == "kept_existing"
    remove_binding(scope, "shared")
    assert skill.read_text().endswith("mine\n")


def test_packaged_skill_matches_the_shared_plugin_skill():
    from plugin_layout import SKILLS

    plugin = SKILLS / "durumari-context/SKILL.md"
    packaged = (
        Path(opencode_config.__file__).parent / "examples/skills/durumari-context/SKILL.md"
    ).read_text(encoding="utf-8")
    assert packaged == plugin.read_text(encoding="utf-8")
    assert "durumari_context_search" in opencode_config.packaged_skill()
