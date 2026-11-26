from datetime import UTC, datetime, timedelta

import pytest
import yaml
from cryptography.fernet import Fernet

from durumari_mcp import server_aliases
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import ProjectProfile
from durumari_mcp.project_gateway import ProjectTransport
from durumari_mcp.server_aliases import request_origin, same_server


def test_only_official_pairs_name_the_same_server():
    assert same_server("https://durumari.kr", "https://app.durumari.cloud")
    assert same_server("https://app.durumari.cloud", "https://durumari.kr")
    assert same_server("https://preview.durumari.kr", "https://preview.durumari.cloud")
    assert not same_server("https://durumari.kr", "https://preview.durumari.cloud")
    assert not same_server("https://durumari.kr", "https://durumari.cloud")
    assert not same_server("https://durumari.kr", "https://app.durumari.cloud.evil.test")
    assert not same_server("https://app.evil.test", "https://app.durumari.cloud")


def test_requests_follow_the_app_origin_the_server_announces(monkeypatch):
    answers = {"https://durumari.kr": "https://durumari.kr"}
    monkeypatch.setattr(server_aliases, "fetch_app_origin", lambda origin: answers[origin])
    assert request_origin("https://durumari.kr") == "https://durumari.kr"
    assert ProjectTransport("https://example.test").origin == "https://example.test"
    # The server setting moves to durumari.cloud; a new process follows it.
    answers["https://durumari.kr"] = "https://app.durumari.cloud"
    monkeypatch.setattr(server_aliases, "_announced", {})
    monkeypatch.setattr(server_aliases, "ANNOUNCEMENT_TTL", 0)
    assert ProjectTransport("https://durumari.kr").origin == "https://app.durumari.cloud"
    # Restoring the setting brings a connection made on the new host back.
    answers["https://app.durumari.cloud"] = "https://durumari.kr"
    monkeypatch.setattr(server_aliases, "_announced", {})
    assert request_origin("https://app.durumari.cloud") == "https://durumari.kr"


def test_an_announcement_outside_the_pair_is_ignored(monkeypatch):
    monkeypatch.setattr(server_aliases, "fetch_app_origin", lambda origin: "https://evil.test")
    assert request_origin("https://durumari.kr") == "https://durumari.kr"
    monkeypatch.setattr(server_aliases, "_announced", {})
    monkeypatch.setattr(
        server_aliases, "fetch_app_origin", lambda origin: "https://preview.durumari.cloud"
    )
    assert request_origin("https://durumari.kr") == "https://durumari.kr"


def test_other_processes_reuse_the_answer_and_offline_keeps_it(monkeypatch):
    calls = []

    def answer(origin):
        calls.append(origin)
        return "https://app.durumari.cloud"

    monkeypatch.setattr(server_aliases, "fetch_app_origin", answer)
    assert request_origin("https://durumari.kr") == "https://app.durumari.cloud"
    monkeypatch.setattr(server_aliases, "_announced", {})
    assert request_origin("https://durumari.kr") == "https://app.durumari.cloud"
    assert calls == ["https://durumari.kr"]

    def offline(origin):
        calls.append(origin)
        raise OSError

    monkeypatch.setattr(server_aliases, "fetch_app_origin", offline)
    monkeypatch.setattr(server_aliases, "_announced", {})
    monkeypatch.setattr(server_aliases, "ANNOUNCEMENT_TTL", 0)
    assert request_origin("https://durumari.kr") == "https://app.durumari.cloud"
    # The stored host is down: its pair still answers.
    calls.clear()
    monkeypatch.setattr(
        server_aliases,
        "fetch_app_origin",
        lambda origin: calls.append(origin) or (None if origin.endswith(".kr") else origin),
    )
    monkeypatch.setattr(server_aliases, "_announced", {})
    assert request_origin("https://durumari.kr") == "https://app.durumari.cloud"
    assert calls == ["https://durumari.kr", "https://app.durumari.cloud"]


def profile(tmp_path, origin):
    return ProjectProfile(
        gateway_origin=origin,
        connection_id="conn-1",
        project_id="project-1",
        source_id="source-1",
        actor_id="actor-1",
        client="claude",
        workspace=str(tmp_path),
        access_token="mpa_" + "a" * 43,
        refresh_token="mpr_" + "b" * 43,
        access_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        outbox_key=Fernet.generate_key().decode(),
    )


def manifest(tmp_path, server):
    (tmp_path / "durumari.yaml").write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "server": server,
                "project_id": "project-1",
                "source_id": "source-1",
                "context_source": "server",
            }
        )
    )


def test_a_committed_previous_host_still_binds_a_moved_connection(tmp_path):
    manifest(tmp_path, "https://durumari.kr")
    profile(tmp_path, "https://app.durumari.cloud").check_workspace(str(tmp_path))
    manifest(tmp_path, "https://app.durumari.cloud")
    profile(tmp_path, "https://durumari.kr").check_workspace(str(tmp_path))


def test_another_host_still_cannot_redirect_credentials(tmp_path):
    manifest(tmp_path, "https://durumari.cloud")
    with pytest.raises(AdapterError, match="project_connection_scope_changed"):
        profile(tmp_path, "https://durumari.kr").check_workspace(str(tmp_path))


def test_moved_host_keeps_the_profile_names_made_before_the_move(tmp_path):
    import hashlib

    from durumari_mcp.account_cli import connection_profile
    from durumari_mcp.context_binding import shared_profile

    before = "account-codex-" + hashlib.sha256(b"https://durumari.kr").hexdigest()[:12]
    assert connection_profile("codex", "https://durumari.kr") == before
    assert connection_profile("codex", "https://app.durumari.cloud") == before
    assert connection_profile("codex", "https://durumari.cloud") != before
    assert shared_profile("https://app.durumari.cloud", "project-1", tmp_path) == shared_profile(
        "https://durumari.kr", "project-1", tmp_path
    )
    assert shared_profile("https://durumari.cloud", "project-1", tmp_path) != shared_profile(
        "https://durumari.kr", "project-1", tmp_path
    )


def test_account_setup_rerun_through_the_moved_host_keeps_its_registration(tmp_path, monkeypatch):
    from durumari_mcp import account_setup

    home, data = tmp_path / "home", tmp_path / "data"
    home.mkdir()
    servers = []

    def command(**kwargs):
        servers.append(kwargs["server"])
        return ["/safe/uv", "run", "--server", kwargs["server"], "--profile", kwargs["profile"]]

    monkeypatch.setattr(account_setup, "launcher_command", command)
    monkeypatch.setattr(account_setup, "prepare_runtime", lambda command: {"state": "ready"})
    for server in ("https://durumari.kr", "https://app.durumari.cloud"):
        result = account_setup.configure("claude-code", "personal", server, home=home, data=data)
        assert result["server"] == "https://durumari.kr"
    configured = (home / ".claude.json").read_text()
    assert servers == ["https://durumari.kr", "https://durumari.kr"]
    saved = account_setup.record(data / "account-setup/claude-code/installed.json")
    assert saved["server"] == "https://durumari.kr"
    with pytest.raises(AdapterError, match="account_mcp_configuration_conflict"):
        account_setup.configure(
            "claude-code", "personal", "https://durumari.cloud", home=home, data=data
        )
    assert (home / ".claude.json").read_text() == configured


def test_updates_keep_the_installed_origin_across_the_move():
    from durumari_mcp import releases

    assert releases.MOVED_HOSTS == server_aliases.MOVED
    installed = {"origin": "https://durumari.kr"}
    assert releases.installed_origin(installed, "https://app.durumari.cloud") == installed["origin"]
    assert releases.installed_origin(installed, None) == installed["origin"]
    assert releases.installed_origin({}, "https://app.durumari.cloud") == (
        "https://app.durumari.cloud"
    )
    assert releases.same_server("https://app.durumari.cloud", "https://durumari.kr")
    assert not releases.same_server("https://durumari.cloud", "https://durumari.kr")
    with pytest.raises(releases.ReleaseError, match="release_origin_change_requires_new"):
        releases.installed_origin(installed, "https://durumari.cloud")


def test_updates_download_from_the_host_the_server_announces(monkeypatch):
    # the launcher's own copy follows the switch without the plugin cache.
    from durumari_mcp import releases

    asked = []

    def answer(host):
        asked.append(host)
        if host == "https://durumari.kr":
            raise OSError("stored host is down")
        return "https://app.durumari.cloud"

    monkeypatch.setattr(releases, "announced_app_origin", answer)
    assert releases.download_origin("https://durumari.kr") == "https://app.durumari.cloud"
    assert asked == ["https://durumari.kr", "https://app.durumari.cloud"]
    monkeypatch.setattr(releases, "announced_app_origin", lambda host: "https://durumari.kr")
    assert releases.download_origin("https://app.durumari.cloud") == "https://durumari.kr"
    monkeypatch.setattr(releases, "announced_app_origin", lambda host: "https://other.example")
    assert releases.download_origin("https://durumari.kr") == "https://durumari.kr"
    monkeypatch.setattr(releases, "announced_app_origin", lambda host: pytest.fail("asked"))
    assert releases.download_origin("https://durumari.example") == "https://durumari.example"


def test_new_installations_name_the_moved_host_by_default():
    import argparse

    from durumari_mcp import account_cli, account_setup_cli

    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    account_cli.add_commands(commands)
    account_setup_cli.add_commands(commands)
    for command in ("account-connect", "account-setup"):
        assert parser.parse_args([command, "--client", "codex"]).server == (
            "https://app.durumari.cloud"
        )
    # The profile name a durumari.kr connection made stays the same for the new default.
    assert account_cli.connection_profile("codex", "https://app.durumari.cloud") == (
        account_cli.connection_profile("codex", "https://durumari.kr")
    )
