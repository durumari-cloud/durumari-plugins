"""The one-line install sets up every Agent found here."""

import hashlib
import json
from argparse import Namespace

import pytest
from test_cursor_release import cursor_files
from test_releases import bundle, manifest

from durumari_mcp import releases
from durumari_mcp.releases import ReleaseError


def release(monkeypatch, tmp_path, *, on_path, cursor=False, tty=False):
    data = bundle(cursor_files())
    metadata = manifest()
    metadata["archive"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(releases, "state_directory", lambda: tmp_path / "releases")
    monkeypatch.setattr(
        releases.shutil, "which", lambda name: name if name in {"uv", *on_path} else None
    )
    monkeypatch.setattr(releases, "cursor_present", lambda: cursor)
    monkeypatch.setattr(releases.sys.stdin, "isatty", lambda: tty, raising=False)
    monkeypatch.setattr(
        releases,
        "download",
        lambda url, limit: json.dumps(metadata).encode() if url.endswith("latest.json") else data,
    )
    monkeypatch.setattr(releases, "install_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "preflight_runtime", lambda: None)
    monkeypatch.setattr(releases, "retire_legacy_host", lambda host: None)
    installed: list[str] = []
    monkeypatch.setattr(releases, "install_host", lambda *args: installed.append(args[0]))
    return installed


def run(command="install", client=None):
    return releases.execute_release(
        Namespace(server="https://durumari.example", client=client, check=False, command=command)
    )


def test_install_without_client_sets_up_every_agent_found(monkeypatch, tmp_path):
    installed = release(monkeypatch, tmp_path, on_path={"claude", "codex"}, cursor=True)
    result = run()
    assert result["clients"] == ["claude", "codex", "cursor"]
    assert installed == ["claude", "codex", "cursor"]
    assert releases.load_state(tmp_path / "releases")["clients"] == ["claude", "codex", "cursor"]


def test_install_with_client_sets_up_only_that_agent(monkeypatch, tmp_path):
    installed = release(monkeypatch, tmp_path, on_path={"claude", "codex"})
    assert run(client="codex")["clients"] == ["codex"]
    assert installed == ["codex"]


def test_install_finding_no_agent_says_so(monkeypatch, tmp_path):
    installed = release(monkeypatch, tmp_path, on_path=set())
    with pytest.raises(ReleaseError, match="release_no_supported_agent"):
        run()
    assert installed == []


def test_update_keeps_the_agents_installed_before(monkeypatch, tmp_path):
    installed = release(monkeypatch, tmp_path, on_path={"claude"})
    run()
    # Codex installed afterwards is added only by the next install, never by an update.
    monkeypatch.setattr(
        releases.shutil, "which", lambda name: name if name in {"uv", "claude", "codex"} else None
    )
    installed.clear()
    assert run(command="update")["clients"] == ["claude"]
    assert installed == ["claude"]
    installed.clear()
    assert run()["clients"] == ["claude", "codex"]


def test_update_without_previous_install_still_needs_a_client(monkeypatch, tmp_path):
    release(monkeypatch, tmp_path, on_path={"claude"})
    with pytest.raises(ReleaseError, match="release_client_required"):
        run(command="update")


def test_found_gemini_without_a_terminal_is_skipped_and_kept_if_installed_before(
    monkeypatch, tmp_path
):
    installed = release(monkeypatch, tmp_path, on_path={"claude", "gemini"}, tty=True)
    assert run()["clients"] == ["claude", "gemini"]
    monkeypatch.setattr(releases.sys.stdin, "isatty", lambda: False, raising=False)
    installed.clear()
    result = run()
    assert result["clients"] == ["claude"] and result["skipped"] == ["gemini"]
    assert installed == ["claude"]
    # The earlier terminal install of Gemini stays recorded for its own updates.
    assert releases.load_state(tmp_path / "releases")["clients"] == ["claude", "gemini"]


def test_update_for_another_agent_skips_recorded_gemini_without_a_terminal(monkeypatch, tmp_path):
    # The detached runner has no terminal, and `gemini extensions update` waits for a consent
    # answer once the version changes, so it must not run there (it hung the update for 10 min).
    installed = release(monkeypatch, tmp_path, on_path={"claude", "gemini"}, tty=True)
    assert run()["clients"] == ["claude", "gemini"]
    monkeypatch.setattr(releases.sys.stdin, "isatty", lambda: False, raising=False)
    installed.clear()
    result = run(command="update", client="claude")
    assert result["clients"] == ["claude"] and result["skipped"] == ["gemini"]
    assert installed == ["claude"]
    assert releases.load_state(tmp_path / "releases")["clients"] == ["claude", "gemini"]


def test_gemini_asked_by_name_is_not_skipped(monkeypatch, tmp_path):
    # Named explicitly, Gemini goes to its host install, which fails clearly without a terminal.
    installed = release(monkeypatch, tmp_path, on_path={"gemini"})
    assert run(client="gemini")["clients"] == ["gemini"]
    assert installed == ["gemini"]


def test_summary_tells_what_was_installed_and_what_to_do_next():
    lines = "\n".join(
        releases.summary_lines(
            {"installed": "0.4.20", "clients": ["claude", "codex"], "skipped": ["gemini"]},
            "https://durumari.kr",
        )
    )
    assert "두루마리 0.4.20을(를) 설치했습니다: Claude Code, Codex" in lines
    assert "--client gemini" in lines
    assert "“두루마리 연결해 줘”" in lines
    assert "https://durumari.kr/downloads#chat" in lines


def test_windows_runtime_in_use_summary_says_which_agents_to_close(monkeypatch, capsys):
    # the one-line installer printed only the code, not what to close.
    def busy(_args):
        raise ReleaseError("runtime_in_use_close_hosts_and_retry")

    monkeypatch.setattr(releases, "execute_release", busy)
    argv = ["durumari-mcp", "install", "--server", "https://durumari.kr", "--summary"]
    monkeypatch.setattr("sys.argv", argv)
    with pytest.raises(SystemExit):
        releases.main()
    error = capsys.readouterr().err
    assert "Cursor·Claude Code·Codex·Gemini CLI 창을 모두 닫은 뒤 다시 실행하세요" in error
    assert "(runtime_in_use_close_hosts_and_retry)" in error


# Claude Code used only through the Claude app is not on PATH.
FIND_BUNDLED = releases.__dict__["bundled_claude"]


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    path.chmod(0o755)
    return path


def test_claude_app_copy_counts_as_claude_code(monkeypatch, tmp_path):
    bundled = str(tmp_path / "claude")
    monkeypatch.setattr(releases, "bundled_claude", lambda: bundled)
    installed = release(monkeypatch, tmp_path, on_path=set())
    monkeypatch.setattr(
        releases.shutil, "which", lambda name: name if name in {"uv", bundled} else None
    )
    assert run()["clients"] == ["claude"]
    assert installed == ["claude"]


def test_claude_app_copy_runs_the_plugin_commands(monkeypatch, tmp_path):
    bundled = str(tmp_path / "claude")
    monkeypatch.setattr(releases, "bundled_claude", lambda: bundled)
    monkeypatch.setattr(releases.shutil, "which", lambda name: None)
    commands: list[list[str]] = []

    def record(command):
        commands.append(command)
        return json.dumps([{"id": "durumari@durumari", "version": "0.4.26"}])

    monkeypatch.setattr(releases, "run", record)
    releases.install_host("claude", tmp_path, "0.4.26")
    assert commands and all(command[0] == bundled for command in commands)


def test_failure_report_names_the_host_not_its_path(monkeypatch, tmp_path):
    host = executable(tmp_path / "secret-user" / "claude")
    host.write_text("#!/bin/sh\nexit 3\n")
    with pytest.raises(ReleaseError) as error:
        releases.run([str(host), "plugin", "list", "--json"])
    assert error.value.details["command"] == ["claude", "plugin", "list"]


def test_newest_claude_app_copy_is_found_on_macos(monkeypatch, tmp_path):
    monkeypatch.setattr(releases.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(releases.sys, "platform", "darwin")
    root = tmp_path / "Library/Application Support/Claude/claude-code"
    executable(root / "2.1.295/aaaa/claude.app/Contents/MacOS/claude")
    newest = executable(root / "2.1.299/bbbb/claude.app/Contents/MacOS/claude")
    (root / "2.1.300").mkdir()  # an unfinished download has no executable yet
    assert FIND_BUNDLED() == str(newest)


def test_claude_app_copy_is_found_on_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(releases.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(releases.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    found = executable(tmp_path / "Roaming/Claude/claude-code/2.1.299/bin/claude.exe")
    assert FIND_BUNDLED() == str(found)


def test_store_claude_app_copy_outside_its_package_is_found_on_windows(monkeypatch, tmp_path):
    # The Store build 3.6556 keeps Claude Code under Claude-Data, not the package's LocalCache.
    monkeypatch.setattr(releases.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(releases.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    (tmp_path / "Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude").mkdir(parents=True)
    root = tmp_path / "Local/Claude-Data/claude_pzs8sxrjxfjjc/claude-code"
    executable(root / "2.1.299/aaaa/claude.exe")
    newest = executable(root / "2.1.308/c17a819afb40/claude.exe")
    assert FIND_BUNDLED() == str(newest)


def test_native_installer_location_is_used_without_the_app(monkeypatch, tmp_path):
    monkeypatch.setattr(releases.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(releases.sys, "platform", "linux")
    assert FIND_BUNDLED() is None
    native = executable(tmp_path / ".local/bin/claude")
    assert FIND_BUNDLED() == str(native)
