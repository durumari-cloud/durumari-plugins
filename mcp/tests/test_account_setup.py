"""Global account configuration owns one entry and preserves every folder binding."""

import json
import tomllib
from argparse import ArgumentParser
from pathlib import Path

import pytest

from durumari_mcp import account_setup, account_setup_cli
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def setup_paths(tmp_path, monkeypatch):
    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    monkeypatch.setattr(
        account_setup,
        "launcher_command",
        lambda **kwargs: [
            "/safe/uv",
            "run",
            "python",
            "/safe/launcher.py",
            "--profile",
            kwargs["profile"],
        ],
    )
    monkeypatch.setattr(
        account_setup,
        "prepare_runtime",
        lambda command: {"state": "ready", "policy": {"capability": "native_policy_v1"}},
    )
    return home, data


@pytest.mark.parametrize("client", ["codex", "claude-code", "cursor"])
def test_registers_one_owned_user_server_and_preserves_foreign_and_folder_settings(
    setup_paths, client
):
    home, data = setup_paths
    if client == "codex":
        path = home / ".codex/config.toml"
        previous = '# personal preference\nmodel = "keep"\n[mcp_servers.other]\ncommand = "other"\n'
    else:
        path = home / (".claude.json" if client == "claude-code" else ".cursor/mcp.json")
        previous = '{/* preference */"theme":"keep","mcpServers":{"other":{"command":"other"}}}\n'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(previous)
    folder = home / "work/durumari.yaml"
    folder.parent.mkdir()
    folder.write_text("keep existing binding\n")
    result = account_setup.configure(
        client, "personal", "https://durumari.kr", home=home, data=data
    )
    assert result["state"] == "configured"
    text = path.read_text()
    if client == "codex":
        parsed = tomllib.loads(text)
        servers = parsed["mcp_servers"]
        assert parsed["model"] == "keep" and "# personal preference" in text
    else:
        from durumari_mcp.config_json import load

        parsed = load(text)
        servers = parsed["mcpServers"]
        assert parsed["theme"] == "keep" and "/* preference */" in text
    assert servers["other"] == {"command": "other"}
    assert servers["durumari_account"]["command"] == "/safe/uv"
    assert folder.read_text() == "keep existing binding\n"
    assert (
        account_setup.configure(client, "personal", "https://durumari.kr", home=home, data=data)[
            "state"
        ]
        == "configured"
    )
    assert path.read_text() == text
    assert (
        any(p.read_bytes() == previous.encode() for p in data.rglob("*config.toml") if p.is_file())
        if client == "codex"
        else any(p.read_bytes() == previous.encode() for p in data.rglob("*") if p.is_file())
    )


def test_foreign_account_entry_is_not_overwritten(setup_paths):
    home, data = setup_paths
    path = home / ".claude.json"
    previous = json.dumps({"mcpServers": {"durumari_account": {"command": "user-owned"}}})
    path.write_text(previous)
    with pytest.raises(AdapterError, match="account_mcp_configuration_conflict"):
        account_setup.configure(
            "claude-code", "personal", "https://durumari.kr", home=home, data=data
        )
    assert path.read_text() == previous


def test_symlink_configuration_is_refused_without_touching_its_target(setup_paths):
    home, data = setup_paths
    target = home / "preserved"
    target.write_text("preserve\n")
    (home / ".claude.json").symlink_to(target)
    with pytest.raises(AdapterError, match="unsafe_account_setup_path"):
        account_setup.configure(
            "claude-code", "personal", "https://durumari.kr", home=home, data=data
        )
    assert target.read_text() == "preserve\n"


def test_pending_connection_does_not_register_host_and_same_command_can_resume(monkeypatch):
    responses = iter(
        [
            {"state": "pending", "approval_url": "https://durumari.kr/connect/account"},
            {"state": "active", "profile": "personal"},
        ]
    )
    monkeypatch.setattr(account_setup_cli, "preflight", lambda *args, **kwargs: None)
    from durumari_mcp import account_cli

    monkeypatch.setattr(account_cli, "connect", lambda args: next(responses))
    written = []
    monkeypatch.setattr(
        account_setup_cli,
        "configure",
        lambda *args, **kwargs: written.append(args) or {"state": "configured"},
    )
    args = type(
        "Args",
        (),
        {
            "client": "codex",
            "server": "https://durumari.kr",
            "profile": "personal",
            "no_browser": True,
        },
    )()
    assert account_setup_cli.setup(args)["state"] == "pending" and not written
    assert account_setup_cli.setup(args)["state"] == "configured" and len(written) == 1


def test_resume_after_configuration_write_preserves_the_original_backup(setup_paths, monkeypatch):
    home, data = setup_paths
    path = home / ".claude.json"
    previous = '{"theme":"original"}\n'
    path.write_text(previous)
    original = account_setup.atomic
    failed = False

    def interrupted(path, value, **kwargs):
        nonlocal failed
        if path.name == "installed.json" and not failed:
            failed = True
            raise OSError("synthetic interruption")
        original(path, value, **kwargs)

    monkeypatch.setattr(account_setup, "atomic", interrupted)
    with pytest.raises(OSError):
        account_setup.configure(
            "claude-code", "personal", "https://durumari.kr", home=home, data=data
        )
    configured = path.read_bytes()
    assert (
        account_setup.configure(
            "claude-code", "personal", "https://durumari.kr", home=home, data=data
        )["state"]
        == "configured"
    )
    assert path.read_bytes() == configured
    assert not list(data.rglob("pending.json"))
    assert any(
        p.read_bytes() == previous.encode() for p in (data / "backups").rglob("*") if p.is_file()
    )


def test_backup_symlink_and_failed_update_preserve_host_configuration(setup_paths, monkeypatch):
    from durumari_mcp.local_upgrade import backup_path

    home, data = setup_paths
    config = home / ".claude.json"
    config.write_text('{"theme":"original"}')
    preserved = home / "preserved"
    preserved.write_text("preserve")
    backup = backup_path(home, config.name, data=data)
    backup.parent.mkdir(parents=True)
    backup.symlink_to(preserved)
    with pytest.raises(AdapterError, match="unsafe_account_setup_path"):
        account_setup.configure(
            "claude-code", "personal", "https://durumari.kr", home=home, data=data
        )
    assert preserved.read_text() == "preserve" and config.read_text() == '{"theme":"original"}'


def test_independent_launcher_bundle_is_verified_before_loading_or_registering(tmp_path):
    from durumari_mcp.runtime_launcher import LauncherError, load_components

    command = account_setup.launcher_command(
        client="codex", profile="personal", server="https://durumari.kr", data=tmp_path
    )
    directory = Path(command[command.index("-I") + 1]).parent
    config, policy, updater = load_components(directory)
    assert config["origin"] == "https://durumari.kr" and hasattr(updater, "install_runtime_bundle")
    assert hasattr(policy, "parse_policy")
    (directory / "update_policy.py").write_text("raise RuntimeError('must never execute')")
    with pytest.raises(LauncherError, match="native_launcher_integrity_failed"):
        load_components(directory)


def test_default_setup_profiles_are_distinct_for_three_hosts_in_one_home(setup_paths, monkeypatch):
    from durumari_mcp import account_cli

    home, data = setup_paths
    monkeypatch.setattr(account_setup, "user_data_directory", lambda: data)
    monkeypatch.setattr(Path, "home", lambda: home)
    clients_by_profile = {}

    def active(name, origin, client):
        assert name == account_cli.connection_profile(client, origin)
        assert clients_by_profile.setdefault(name, client) == client
        return {"state": "active", "profile": name}

    monkeypatch.setattr(account_cli, "_step", active)
    monkeypatch.setattr(account_cli, "state_root", lambda: data / "state")
    parser = ArgumentParser()
    account_setup_cli.add_commands(parser.add_subparsers(dest="command"))
    for client in ("codex", "claude-code", "cursor"):
        args = parser.parse_args(["account-setup", "--client", client, "--no-browser"])
        first = account_setup_cli.setup(args)
        assert first["state"] == "configured"
        assert account_setup_cli.setup(args)["profile"] == first["profile"]
    assert len(clients_by_profile) == 3
    for client in clients_by_profile.values():
        saved = account_setup.record(data / "account-setup" / client / "installed.json")
        assert clients_by_profile[saved["profile"]] == client


def test_failed_runtime_preparation_does_not_register_or_overwrite_host(setup_paths, monkeypatch):
    home, data = setup_paths
    path = home / ".claude.json"
    path.write_text('{"theme":"preserved"}\n')

    def unavailable(command):
        raise AdapterError("native_update_policy_unavailable")

    monkeypatch.setattr(account_setup, "prepare_runtime", unavailable)
    with pytest.raises(AdapterError, match="native_update_policy_unavailable"):
        account_setup.configure(
            "claude-code", "personal", "https://durumari.kr", home=home, data=data
        )
    assert path.read_text() == '{"theme":"preserved"}\n'
    assert not list(data.rglob("installed.json")) and not list(data.rglob("pending.json"))


def test_main_cli_registers_account_setup_with_optional_profile():
    from durumari_mcp.cli import build_parser

    args = build_parser().parse_args(["account-setup", "--client", "cursor", "--no-browser"])
    assert args.profile is None and args.no_browser
