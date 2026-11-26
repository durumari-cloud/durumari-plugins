"""Plugin env vars sit under DURUMARI_MCP_; secret run reserves exactly those ."""

import re
from pathlib import Path

import pytest
from plugin_layout import ACP, AGENT, PACKAGE

from durumari_mcp import plugin_env, prompt_context, releases, secret_env
from durumari_mcp.secret_use import SecretUseError

TOKEN = re.compile(r"(?<![A-Z0-9_])DURUMARI_[A-Z0-9_]*[A-Z0-9]")
# Markers and file names that are not environment variables.
NOT_ENV = ("DURUMARI_GUARD_PROFILE",)
LEGACY = {
    "DURUMARI_UV_EXECUTABLE",
    "DURUMARI_EXECUTION_POLICY",
    "DURUMARI_SESSION_ID",
    "DURUMARI_CONTEXT_DEPTH",
    "DURUMARI_CLIENT",
    "DURUMARI_INSTALLER",
    "DURUMARI_PLUGIN_INSTALLER_DELEGATED",
    "DURUMARI_SERVER",
}


def plugin_sources() -> list[Path]:
    """Runtime sources of every plugin package (tests are not readers of plugin variables)."""
    found: list[Path] = []
    for folder, pattern in (
        (PACKAGE, "src/durumari_mcp/*.py"),
        (PACKAGE, "bootstrap/*"),
        (AGENT, "scripts/*.py"),
        (ACP, "*.py"),
    ):
        found += [
            path
            for path in sorted(folder.glob(pattern))
            if path.is_file() and not path.name.startswith("test_")
        ]
    return found


def test_the_legacy_list_is_exactly_the_documented_names():
    assert plugin_env.LEGACY_NAMES == LEGACY
    assert all(name.startswith(plugin_env.PREFIX) for name in plugin_env.LEGACY)
    assert {name.removeprefix(plugin_env.PREFIX) for name in plugin_env.LEGACY} == {
        old.removeprefix("DURUMARI_") for old in LEGACY
    }


def test_every_plugin_source_names_only_reserved_or_non_env_durumari_tokens():
    if not AGENT.is_dir():
        pytest.skip("sibling plugin packages are not part of this checkout")
    sources = plugin_sources()
    assert any(path.name == "plugin_env.py" for path in sources)
    assert any(path.name == "install.sh" for path in sources)
    assert any(path.name == "durumari_plugin.py" for path in sources)
    # The ACP adapter is private and absent from the plugin repository.
    assert any(path.name == "acp_client.py" for path in sources) == ACP.is_dir()
    stray = {}
    for path in sources:
        for token in TOKEN.findall(path.read_text(encoding="utf-8")):
            allowed = (
                token == "DURUMARI_MCP"
                or token.startswith(plugin_env.PREFIX)
                or token in LEGACY
                or token.startswith(NOT_ENV)
            )
            if not allowed:
                stray.setdefault(token, set()).add(path.name)
    assert not stray, f"plugin env names outside DURUMARI_MCP_ and the legacy list: {stray}"


def test_standalone_modules_repeat_the_same_names():
    uv = plugin_env.UV_EXECUTABLE
    assert releases.UV_EXECUTABLE_ENV == uv
    assert releases.LEGACY_UV_EXECUTABLE_ENV == plugin_env.LEGACY[uv]
    agent = AGENT / "scripts/durumari_plugin.py"
    acp = ACP / "acp_client.py"
    if not agent.is_file():
        pytest.skip("sibling plugin packages are not part of this checkout")
    agent_text = agent.read_text(encoding="utf-8")
    assert f'UV_EXECUTABLE_ENV = "{uv}"' in agent_text
    delegated = plugin_env.PLUGIN_INSTALLER_DELEGATED
    assert f'INSTALLER_DELEGATED_ENV = "{delegated}"' in agent_text
    assert f'LEGACY_INSTALLER_DELEGATED_ENV = "{plugin_env.LEGACY[delegated]}"' in agent_text
    if not acp.is_file():
        return  # the private ACP adapter is checked where it lives
    acp_text = acp.read_text(encoding="utf-8")
    policy = plugin_env.EXECUTION_POLICY
    assert f'POLICY_ENV = "{policy}"' in acp_text
    assert f'LEGACY_POLICY_ENV = "{plugin_env.LEGACY[policy]}"' in acp_text


def test_backend_settings_are_accepted_by_secret_run_validation():
    for name in ("DURUMARI_PADDLE_PRICES", "DURUMARI_DATABASE_URL", "DURUMARI_ENVIRONMENT"):
        assert secret_env.env_name(name) == name


@pytest.mark.parametrize(
    "name",
    ["DURUMARI_MCP_X", "durumari_mcp_x", "DURUMARI_MCP_UV_EXECUTABLE", *sorted(LEGACY)],
)
def test_plugin_names_and_their_legacy_spellings_are_rejected(name):
    with pytest.raises(SecretUseError, match="invalid_env_name"):
        secret_env.env_name(name)


@pytest.mark.parametrize(
    "name", ["PATH", "PATHEXT", "COMSPEC", "SYSTEMROOT", "LD_PRELOAD", "DYLD_X"]
)
def test_loader_and_search_path_names_stay_rejected(name):
    with pytest.raises(SecretUseError, match="invalid_env_name"):
        secret_env.env_name(name)


def test_readers_prefer_the_new_name_and_fall_back_to_the_old(monkeypatch):
    name = plugin_env.SESSION_ID
    old = plugin_env.LEGACY[name]
    monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv(old, raising=False)
    assert plugin_env.get(name) is None
    monkeypatch.setenv(old, "old")
    assert plugin_env.get(name) == "old"
    monkeypatch.setenv(name, "new")
    assert plugin_env.get(name) == "new"
    assert plugin_env.get(name, {old: "o"}) == "o"


def test_pop_removes_both_spellings(monkeypatch):
    name = plugin_env.EXECUTION_POLICY
    old = plugin_env.LEGACY[name]
    monkeypatch.setenv(old, "old")
    assert plugin_env.pop(name) == "old"
    monkeypatch.setenv(old, "old")
    monkeypatch.setenv(name, "new")
    assert plugin_env.pop(name) == "new"
    assert plugin_env.get(name) is None


def test_the_earlier_context_depth_name_still_selects_the_depth(monkeypatch, tmp_path):
    monkeypatch.setattr(prompt_context, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.delenv(plugin_env.CONTEXT_DEPTH, raising=False)
    monkeypatch.setenv(plugin_env.LEGACY[plugin_env.CONTEXT_DEPTH], "끄기")
    assert prompt_context.ENVIRONMENT == plugin_env.CONTEXT_DEPTH
    depth, source = prompt_context.resolve_depth(None)
    assert source == "environment" and depth == prompt_context.normalize_depth("끄기")


def test_the_earlier_uv_name_still_names_the_uv(monkeypatch, tmp_path):
    uv = tmp_path / "uv"
    uv.write_text("", encoding="utf-8")
    monkeypatch.delenv(releases.UV_EXECUTABLE_ENV, raising=False)
    monkeypatch.setenv(releases.LEGACY_UV_EXECUTABLE_ENV, str(uv))
    assert releases.uv_command() == str(uv)
    other = tmp_path / "other"
    other.write_text("", encoding="utf-8")
    monkeypatch.setenv(releases.UV_EXECUTABLE_ENV, str(other))
    assert releases.uv_command() == str(other)
