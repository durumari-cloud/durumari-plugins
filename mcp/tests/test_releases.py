"""Release updates validate public artifacts before touching host installations."""

import hashlib
import http.client
import io
import json
import os
import shutil
import ssl
import sys
import urllib.error
import zipfile
from argparse import Namespace
from types import SimpleNamespace

import pytest

from durumari_mcp import releases
from durumari_mcp.releases import ReleaseError, extract_catalog, parse_release, release_origin


@pytest.mark.parametrize("path", [
    "plugins/another-plugin/mcp.json",
    "plugins/another-plugin/mcp-claude.json",
    "plugins/durumari-agent/extra/mcp.json",
    "mcp.json",
])
def test_remote_mcp_declarations_cannot_escape_the_single_public_plugin(path):
    assert not releases.allowed_member(path)


def manifest():
    return {
        "schema_version": 1,
        "version": "0.2.0",
        "published_at": "2026-10-02T00:00:00Z",
        "archive": {
            "path": "/downloads/releases/0.2.0/durumari-plugins.zip",
            "sha256": "a" * 64,
            "size": 1000,
        },
        "catalog": {
            "name": "durumari",
            "repository": "https://github.com/MyKnow/malmoa-plugins.git",
        },
    }


def bundle(extra=None, corrupt=False):
    wheel = b"synthetic wheel"
    lock = b"synthetic locked dependencies"
    package = {"name": "durumari", "version": "0.2.0"}
    data = {
        "durumari-catalog/.agents/plugins/marketplace.json": json.dumps(
            {
                "name": "durumari",
                "plugins": [
                    {
                        "name": "durumari",
                        "source": {"source": "local", "path": "./plugins/durumari-agent"},
                    }
                ],
            }
        ).encode(),
        "durumari-catalog/.claude-plugin/marketplace.json": json.dumps(
            {
                "name": "durumari",
                "owner": {"name": "DURUMARI"},
                "plugins": [{"name": "durumari", "source": "./plugins/durumari-agent"}],
            }
        ).encode(),
        "durumari-catalog/plugins/durumari-agent/plugin.json": json.dumps(package).encode(),
        "durumari-catalog/plugins/durumari-agent/.codex-plugin/plugin.json": json.dumps(
            package
        ).encode(),
        "durumari-catalog/plugins/durumari-agent/.claude-plugin/plugin.json": json.dumps(
            package
        ).encode(),
        (
            "durumari-catalog/plugins/durumari-agent/runtime/durumari_mcp-0.2.0-py3-none-any.whl"
        ): wheel,
        "durumari-catalog/plugins/durumari-agent/runtime/requirements.txt": lock,
        "durumari-catalog/plugins/durumari-agent/runtime/bundle.json": json.dumps(
            {
                "schema_version": 1,
                "package": "durumari-mcp",
                "version": "0.2.0",
                "files": {
                    "durumari_mcp-0.2.0-py3-none-any.whl": hashlib.sha256(wheel).hexdigest(),
                    "requirements.txt": hashlib.sha256(lock).hexdigest(),
                },
            }
        ).encode(),
    }
    if corrupt:
        data["durumari-catalog/plugins/durumari-agent/runtime/requirements.txt"] = b"tampered"
    if extra:
        data.update(extra)
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for path, body in data.items():
            archive.writestr(path, body)
    return output.getvalue()


@pytest.mark.parametrize(
    "value",
    [
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com/path",
        "https://example.com?key=private",
        "https://example.com/#secret",
        "file:///tmp/release",
    ],
)
def test_origin_rejects_plaintext_credentials_and_non_origins(value):
    with pytest.raises(ReleaseError):
        release_origin(value)


def test_valid_origin_and_manifest():
    assert release_origin("https://durumari.example.com/") == "https://durumari.example.com"
    assert parse_release(json.dumps(manifest()).encode()).version == "0.2.0"


@pytest.mark.parametrize(
    "origin",
    [
        "https://api.durumari.kr",
        "https://api.durumari.cloud",
        "https://api.durumari.com",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
    ],
)
def test_packaged_service_supports_configured_domains_and_local_testing(origin):
    assert releases.parse_service({"schema_version": 1, "gateway_origin": origin}) == origin


@pytest.mark.parametrize(
    "service",
    [
        None,
        {},
        {"schema_version": True, "gateway_origin": "https://example.com"},
        {"schema_version": 1, "gateway_origin": "https://example.com", "project_id": "other"},
        {"schema_version": 1, "gateway_origin": "http://example.com"},
        {"schema_version": 1, "gateway_origin": "http://localhost:8000"},
        {"schema_version": 1, "gateway_origin": "https://user:secret@example.com"},
        {"schema_version": 1, "gateway_origin": "https://example.com/path"},
        {"schema_version": 1, "gateway_origin": "https://example.com?secret=x"},
        {"schema_version": 1, "gateway_origin": "https://example.com/#secret"},
        {"schema_version": 1, "gateway_origin": "https://example.com:0"},
    ],
)
def test_packaged_service_rejects_invalid_or_credential_bearing_defaults(service):
    with pytest.raises(ReleaseError, match="invalid_plugin_service"):
        releases.parse_service(service)


def test_service_metadata_survives_archive_extraction(tmp_path):
    service = {"schema_version": 1, "gateway_origin": "https://api.durumari.kr"}
    runtime = {
        "schema_version": 1,
        "package": "durumari-mcp",
        "version": "0.2.0",
        "files": {
            "durumari_mcp-0.2.0-py3-none-any.whl": hashlib.sha256(b"synthetic wheel").hexdigest(),
            "requirements.txt": hashlib.sha256(b"synthetic locked dependencies").hexdigest(),
        },
        "service": service,
    }
    data = bundle(
        {
            "durumari-catalog/plugins/durumari-agent/runtime/bundle.json": json.dumps(
                runtime
            ).encode(),
        }
    )
    catalog = extract_catalog(data, tmp_path, "0.2.0")
    assert (
        json.loads((catalog / "plugins/durumari-agent/runtime/bundle.json").read_text())["service"]
        == service
    )


def test_new_runtime_requires_packaged_service_before_install(tmp_path):
    wheel = "durumari_mcp-0.2.9-py3-none-any.whl"
    (tmp_path / wheel).write_bytes(b"synthetic wheel")
    (tmp_path / "requirements.txt").write_bytes(b"lock")
    runtime = {
        "schema_version": 1,
        "package": "durumari-mcp",
        "version": "0.2.9",
        "files": {
            wheel: hashlib.sha256(b"synthetic wheel").hexdigest(),
            "requirements.txt": hashlib.sha256(b"lock").hexdigest(),
        },
    }
    metadata = tmp_path / "bundle.json"
    metadata.write_text(json.dumps(runtime))
    with pytest.raises(ReleaseError, match="invalid_plugin_service"):
        releases.verify_runtime(tmp_path, "0.2.9")
    runtime["service"] = {"schema_version": 1, "gateway_origin": "http://127.0.0.1:8000"}
    metadata.write_text(json.dumps(runtime))
    assert releases.verify_runtime(tmp_path, "0.2.9")[0].name == wheel


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "https://attacker.invalid/package.zip"),
        ("path", "/downloads/releases/../private.zip"),
        ("sha256", "invalid"),
        ("size", 0),
        ("size", 100_000_000),
    ],
)
def test_metadata_rejects_untrusted_downloads(field, value):
    item = manifest()
    item["archive"][field] = value
    with pytest.raises(ReleaseError):
        parse_release(json.dumps(item).encode())


@pytest.mark.parametrize("version", ["../../private", "0.2.0;command", "999999999999.2.0", "0.2"])
def test_version_is_a_bounded_release_identifier(version):
    item = manifest()
    item["version"] = version
    with pytest.raises(ReleaseError):
        parse_release(json.dumps(item).encode())


@pytest.mark.parametrize(
    "path",
    [
        "../escape",
        "/absolute",
        "durumari-catalog/../escape",
        "durumari-catalog/private.env",
        "durumari-catalog/plugins/durumari-agent/.env",
        "durumari-catalog/plugins/durumari-agent/runtime/unexpected.py",
    ],
)
def test_archive_rejects_paths_and_unexpected_files(tmp_path, path):
    with pytest.raises(ReleaseError):
        extract_catalog(bundle({path: b"untrusted"}), tmp_path, "0.2.0")
    assert not (tmp_path.parent / "escape").exists()


def test_runtime_integrity_is_required(tmp_path):
    with pytest.raises(ReleaseError, match="runtime_integrity"):
        extract_catalog(bundle(corrupt=True), tmp_path, "0.2.0")


def test_extracts_only_verified_catalog(tmp_path):
    result = extract_catalog(bundle(), tmp_path, "0.2.0")
    assert result == tmp_path / "durumari-catalog"
    assert (
        result / "plugins/durumari-agent/runtime/requirements.txt"
    ).read_bytes() == b"synthetic locked dependencies"


def test_symlink_entries_are_rejected(tmp_path):
    buffer = io.BytesIO(bundle())
    with zipfile.ZipFile(buffer, "a") as archive:
        link = zipfile.ZipInfo("durumari-catalog/README.md")
        link.create_system = 3
        link.external_attr = 0o120777 << 16
        archive.writestr(link, "/etc/passwd")
    with pytest.raises(ReleaseError):
        extract_catalog(buffer.getvalue(), tmp_path, "0.2.0")


def installation(monkeypatch, tmp_path):
    data = bundle()
    metadata = manifest()
    metadata["archive"]["size"] = len(data)
    metadata["archive"]["sha256"] = hashlib.sha256(data).hexdigest()
    monkeypatch.setattr(releases, "state_directory", lambda: tmp_path / "releases")
    monkeypatch.setattr(releases.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        releases,
        "download",
        lambda url, limit: json.dumps(metadata).encode() if url.endswith("latest.json") else data,
    )
    monkeypatch.setattr(releases, "install_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "preflight_runtime", lambda: None)
    monkeypatch.setattr(releases, "install_host", lambda *args: None)
    monkeypatch.setattr(releases, "retire_legacy_host", lambda *args: None)
    return Namespace(server="https://durumari.example", client="codex", check=False)


def test_check_does_not_install_or_change_credentials(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    args.check = True
    monkeypatch.setattr(
        releases, "install_runtime", lambda *args: pytest.fail("unexpected installation")
    )
    assert releases.execute_release(args)["update_available"]
    assert not (tmp_path / "releases").exists()


def test_downloaded_plugin_cannot_silently_select_another_service(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    path = "durumari-catalog/plugins/durumari-agent/runtime/bundle.json"
    with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
        runtime = json.loads(archive.read(path))
    runtime["service"] = {"schema_version": 1, "gateway_origin": "https://other.example"}
    data = bundle({path: json.dumps(runtime).encode()})
    metadata = manifest()
    metadata["archive"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(
        releases,
        "download",
        lambda url, limit: json.dumps(metadata).encode() if url.endswith("latest.json") else data,
    )
    monkeypatch.setattr(
        releases, "install_runtime", lambda *args: pytest.fail("wrong service installed")
    )
    with pytest.raises(ReleaseError, match="release_service_origin_mismatch"):
        releases.execute_release(args)
    assert not (tmp_path / "releases/catalog").exists()


def test_update_skips_an_agent_removed_since_the_last_installation(monkeypatch, tmp_path):
    # Claude Code uninstalled after a Claude Code + Codex installation
    # must not block the Codex update.
    args = installation(monkeypatch, tmp_path)
    args.client = "both"
    releases.execute_release(args)
    monkeypatch.setattr(releases.shutil, "which", lambda name: None if name == "claude" else name)
    monkeypatch.setattr(releases, "bundled_claude", lambda: None)
    args.client = None
    result = releases.execute_release(args)
    assert result["clients"] == ["codex"]
    assert result["not_found"] == ["claude"]
    # Only this run skips it: a command hidden from this shell's PATH must not drop the Agent.
    state = json.loads((tmp_path / "releases/installation.json").read_text())
    assert state["clients"] == ["claude", "codex"]
    assert any("Claude Code을(를) 찾지 못해" in line for line in releases.summary_lines(result, ""))
    args.client = "claude"
    with pytest.raises(ReleaseError, match="release_required_command_missing"):
        releases.execute_release(args)
    monkeypatch.setattr(releases.shutil, "which", lambda name: name)
    args.client = None
    result = releases.execute_release(args)
    assert result["clients"] == ["claude", "codex"]
    assert "not_found" not in result


def test_update_with_every_agent_removed_still_reports_the_missing_command(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    monkeypatch.setattr(releases.shutil, "which", lambda name: None if name == "codex" else name)
    args.client = None
    with pytest.raises(ReleaseError, match="release_required_command_missing"):
        releases.execute_release(args)


def test_failed_update_restores_previous_state_and_is_retryable(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    credential = tmp_path / "credentials"
    credential.write_text("synthetic preserved credential")
    releases.execute_release(args)
    before = (tmp_path / "releases/installation.json").read_bytes()
    attempts = []

    def fail_once(*unused):
        attempts.append(1)
        if len(attempts) == 1:
            raise ReleaseError("synthetic_failure")

    monkeypatch.setattr(releases, "install_host", fail_once)
    with pytest.raises(ReleaseError, match="retry_or_restore"):
        releases.execute_release(args)
    assert (tmp_path / "releases/installation.json").read_bytes() == before
    assert credential.read_text() == "synthetic preserved credential"
    assert not (tmp_path / "releases/update.lock").exists()
    assert releases.execute_release(args)["installed"] == "0.2.0"


def test_native_version_check_cannot_match_another_marketplace(monkeypatch, tmp_path):
    def response(command):
        return json.dumps({"installed": [{"pluginId": "durumari@other", "version": "0.2.0"}]})

    monkeypatch.setattr(releases, "run", response)
    with pytest.raises(ReleaseError, match="version_not_verified"):
        releases.install_host("codex", tmp_path, "0.2.0")


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_legacy_cleanup_targets_only_exact_host_identity_and_preserves_data(monkeypatch, client):
    calls = []
    rows = [{"pluginId": "malmoa@malmoa", "id": "malmoa@malmoa", "scope": "user"}]

    def command(args):
        calls.append(args)
        return json.dumps({"installed": rows} if client == "codex" else rows)

    monkeypatch.setattr(releases, "run", command)
    releases.retire_legacy_host(client)
    assert len(calls) == 2
    assert calls[1][3] == "malmoa@malmoa"
    if client == "claude":
        assert "--keep-data" in calls[1] and calls[1][5] == "user"
    rows[0].update(pluginId="malmoa@other", id="malmoa@other")
    calls.clear()
    releases.retire_legacy_host(client)
    assert len(calls) == 1


def test_legacy_cleanup_waits_for_verified_replacement_and_is_retryable(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    cleaned = []

    def cleanup(host):
        assert (tmp_path / "releases/installation.json").exists()
        cleaned.append(host)
        raise ReleaseError("synthetic_cleanup_failure")

    monkeypatch.setattr(releases, "retire_legacy_host", cleanup)
    result = releases.execute_release(args)
    assert result["legacy_cleanup_required"] == ["codex"]
    assert result["installed"] == "0.2.0"
    assert cleaned == ["codex"]


def test_command_output_uses_utf8_independent_of_windows_locale(monkeypatch):
    calls = []

    def subprocess_run(command, **kwargs):
        calls.append(kwargs)
        return Namespace(returncode=0, stdout="설치됨")

    monkeypatch.setattr(releases.subprocess, "run", subprocess_run)
    assert releases.run(["codex", "plugin", "list"]) == "설치됨"
    assert calls[0]["encoding"] == "utf-8"
    assert calls[0]["errors"] == "replace"


def test_windows_update_starts_independent_runner_and_reports_only_started(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    calls = []

    def popen(command, **kwargs):
        calls.append(command)
        return Namespace(pid=os.getpid())

    monkeypatch.setattr(releases.subprocess, "Popen", popen)
    result = releases.start_windows_update(args)
    assert result["status"] == "started"
    assert "installed" not in result
    assert calls[0][:6] == ["uv", "run", "--no-project", "--python", "3.12", "--managed-python"]
    assert "--wait-for-pid" in calls[0]
    assert releases.update_status()["status"] == "started"
    with pytest.raises(ReleaseError, match="already_running"):
        releases.start_windows_update(args)


def test_independent_runner_records_safe_failure_and_releases_launch_lock(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir()
    (directory / "launch.lock").mkdir()
    args.wait_for_pid = 123
    monkeypatch.setattr(releases, "wait_for_parent", lambda pid: None)

    def fail(unused):
        raise ReleaseError("release_install_failed_retry_or_restore")

    monkeypatch.setattr(releases, "execute_release", fail)
    assert releases.run_independent_update(args) == 2
    assert releases.update_status() == {
        "status": "failed",
        "error": "release_install_failed_retry_or_restore",
    }
    assert not (directory / "launch.lock").exists()


def test_independent_runner_records_completed_installation(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir()
    (directory / "launch.lock").mkdir()
    args.wait_for_pid = 123
    monkeypatch.setattr(releases, "wait_for_parent", lambda pid: None)
    assert releases.run_independent_update(args) == 0
    assert releases.update_status()["status"] == "completed"
    assert releases.update_status()["installed"] == "0.2.0"
    assert not (directory / "launch.lock").exists()


def test_independent_runner_waits_for_the_exiting_cli_launcher(monkeypatch, tmp_path):
    # Windows keeps the CLI's uv launcher and python.exe redirector inside the
    # runtime a moment after the waited interpreter exits.
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir()
    (directory / "launch.lock").mkdir()
    args.wait_for_pid = 123
    monkeypatch.setattr(releases, "wait_for_parent", lambda pid: None)
    checks = []

    def preflight():
        checks.append(1)
        if len(checks) < 3:
            raise ReleaseError("runtime_in_use_close_hosts_and_retry")

    monkeypatch.setattr(releases, "preflight_runtime", preflight)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    assert releases.run_independent_update(args) == 0
    assert releases.update_status()["status"] == "completed"


def test_independent_runner_still_reports_an_agent_that_keeps_the_runtime(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir()
    (directory / "launch.lock").mkdir()
    args.wait_for_pid = 123
    monkeypatch.setattr(releases, "wait_for_parent", lambda pid: None)

    def preflight():
        raise ReleaseError("runtime_in_use_close_hosts_and_retry")

    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(releases, "preflight_runtime", preflight)
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setattr("time.monotonic", lambda: next(clock))
    assert releases.run_independent_update(args) == 2
    assert releases.update_status()["error"] == "runtime_in_use_close_hosts_and_retry"
    assert not (directory / "launch.lock").exists()


def test_runner_start_failure_does_not_leave_a_running_lock(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)

    def fail(*unused, **kwargs):
        raise OSError("synthetic launch failure")

    monkeypatch.setattr(releases.subprocess, "Popen", fail)
    with pytest.raises(ReleaseError, match="runner_start_failed"):
        releases.start_windows_update(args)
    assert releases.update_status()["status"] == "failed"
    assert not (releases.state_directory() / "launch.lock").exists()


def test_backup_cleanup_failure_keeps_completed_installation(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    actual = releases.shutil.rmtree

    def cleanup(path, *extra, **kwargs):
        if path == tmp_path / "releases/previous":
            raise OSError("synthetic busy backup")
        return actual(path, *extra, **kwargs)

    monkeypatch.setattr(releases.shutil, "rmtree", cleanup)
    result = releases.execute_release(args)
    assert result["installed"] == "0.2.0"
    assert result["cleanup_required"] is True
    assert releases.load_state(tmp_path / "releases")["version"] == "0.2.0"


@pytest.mark.parametrize("debug", [False, True])
def test_command_failure_report_keeps_cause_and_limits_output(monkeypatch, tmp_path, debug):
    args = installation(monkeypatch, tmp_path)
    args.debug = debug
    calls = []

    def failed_command(command, **kwargs):
        calls.append(command)
        return Namespace(returncode=17, stdout="x" * 9000, stderr="synthetic private output")

    def install(*unused):
        releases.run(["codex", "plugin", "marketplace", "add", "private/catalog", "--json"])

    monkeypatch.setattr(releases.subprocess, "run", failed_command)
    monkeypatch.setattr(releases, "install_host", install)
    with pytest.raises(ReleaseError, match="retry_or_restore") as caught:
        releases.execute_release(args)
    report = json.loads((tmp_path / "releases/install-diagnostic.json").read_text())
    assert len(calls) == 1  # Diagnostics must never rerun a failed mutation.
    assert report["stage"] == "install_host_codex"
    assert report["failure"]["error"] == "release_install_command_failed"
    assert report["failure"]["returncode"] == 17
    assert report["failure"]["command"] == ["codex", "plugin", "marketplace", "add"]
    assert report["rollback"] == "removed_partial_catalog"
    assert caught.value.diagnostic_path == str(tmp_path / "releases/install-diagnostic.json")
    assert caught.value.details["stage"] == "install_host_codex"
    assert caught.value.details["cause"] == "release_install_command_failed"
    if debug:
        assert len(report["failure"]["stdout"]) == 8192
        assert report["failure"]["stderr"] == "synthetic private output"
    else:
        assert "stdout" not in report["failure"] and "stderr" not in report["failure"]
        assert "private" not in json.dumps(report)


def test_rollback_report_preserves_both_failures(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    before = (tmp_path / "releases/installation.json").read_bytes()
    attempts = []

    def fail(*unused):
        attempts.append(1)
        raise ReleaseError("install_failed" if len(attempts) == 1 else "rollback_failed")

    monkeypatch.setattr(releases, "install_host", fail)
    with pytest.raises(ReleaseError, match="rollback_requires_recovery"):
        releases.execute_release(args)
    report = json.loads((tmp_path / "releases/install-diagnostic.json").read_text())
    assert report["failure"]["error"] == "install_failed"
    assert report["rollback_failure"]["error"] == "rollback_failed"
    assert report["rollback"] == "failed"
    assert (tmp_path / "releases/installation.json").read_bytes() == before
    assert not (tmp_path / "releases/update.lock").exists()


@pytest.mark.parametrize("timeout", [False, True])
def test_spawn_failure_and_timeout_have_distinct_causes(monkeypatch, timeout):
    def fail(*unused, **kwargs):
        if timeout:
            raise releases.subprocess.TimeoutExpired("private argument", 180)
        raise FileNotFoundError(2, "synthetic private message")

    monkeypatch.setattr(releases.subprocess, "run", fail)
    with pytest.raises(ReleaseError) as caught:
        releases.run(["uv", "tool", "install", "private/wheel"])
    assert caught.value.details["command"] == ["uv", "tool", "install"]
    if timeout:
        assert str(caught.value) == "release_install_command_timeout"
        assert caught.value.details["timeout_seconds"] == 180
    else:
        assert str(caught.value) == "release_install_command_unavailable"
        assert caught.value.details["errno"] == 2


@pytest.mark.parametrize("platform", ["win32", "linux"])
def test_windows_host_shim_runs_by_the_path_which_found(monkeypatch, platform):
    """npm installs Codex as codex.cmd; CreateProcess cannot start it by bare name."""
    shim = r"C:\Users\person\AppData\Roaming\npm\codex.CMD"
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        return Namespace(returncode=1, stdout="", stderr="")

    monkeypatch.setattr(releases.sys, "platform", platform)
    monkeypatch.setattr(releases.shutil, "which", lambda name: shim)
    monkeypatch.setattr(releases.subprocess, "run", fail)
    with pytest.raises(ReleaseError) as caught:
        releases.run(["codex", "plugin", "marketplace", "add", r"C:\catalog dir", "--json"])
    expected = shim if platform == "win32" else "codex"
    assert calls == [[expected, "plugin", "marketplace", "add", r"C:\catalog dir", "--json"]]
    assert caught.value.details["command"] == ["codex", "plugin", "marketplace", "add"]


@pytest.mark.parametrize("host", ["codex", "C:/Users/person/npm/codex.cmd"])
@pytest.mark.parametrize("argument", [r"C:\100%\catalog", r"C:\a&b\catalog", 'C:\\"q"'])
def test_batch_shim_refuses_arguments_cmd_would_reparse(monkeypatch, host, argument):
    """Bare or already full, a batch shim never receives what cmd.exe would expand or split."""
    monkeypatch.setattr(releases.sys, "platform", "win32")
    monkeypatch.setattr(releases.shutil, "which", lambda name: r"C:\npm\codex.cmd")
    monkeypatch.setattr(
        releases.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected launch")
    )
    with pytest.raises(ReleaseError, match="release_install_path_unsupported") as caught:
        releases.run([host, "plugin", "marketplace", "add", argument, "--json"])
    assert caught.value.details["command"] == ["codex", "plugin", "marketplace"]
    assert argument not in json.dumps(caught.value.details)


def test_executable_host_keeps_arguments_cmd_would_reparse(monkeypatch):
    calls = []
    monkeypatch.setattr(releases.sys, "platform", "win32")
    monkeypatch.setattr(releases.shutil, "which", lambda name: r"C:\bin\claude.exe")
    monkeypatch.setattr(
        releases.subprocess,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or Namespace(returncode=0, stdout="[]", stderr="")
        ),
    )
    releases.run(["claude", "plugin", "marketplace", "add", r"C:\100%\catalog"])
    assert calls[0][0] == r"C:\bin\claude.exe"


@pytest.mark.parametrize(
    ("platform", "stage", "expected"),
    [
        ("win32", "install_host_codex", "Codex에 플러그인을 설치하지 못했습니다. 다른 Agent만"),
        ("linux", "install_host_claude", "Claude Code에 플러그인을 설치하지 못했습니다."),
        ("win32", "install_runtime", None),
    ],
)
def test_install_failure_names_the_agent_that_failed(monkeypatch, platform, stage, expected):
    monkeypatch.setattr(releases.sys, "platform", platform)
    error = ReleaseError("release_install_failed_retry_or_restore", details={"stage": stage})
    reason = releases.failure_reason(error, str(error))
    if expected is None:
        assert reason is None
    else:
        assert reason is not None and reason.startswith(expected)
        assert ("DURUMARI_MCP_CLIENT" in reason) == (platform == "win32")


def test_known_cause_inside_an_agent_failure_says_what_to_fix(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)

    def install(*unused):
        raise ReleaseError("release_install_path_unsupported")

    monkeypatch.setattr(releases, "install_host", install)
    with pytest.raises(ReleaseError, match="retry_or_restore") as caught:
        releases.execute_release(args)
    assert caught.value.details["cause"] == "release_install_path_unsupported"
    reason = releases.failure_reason(caught.value, str(caught.value))
    assert reason == releases.FAILURE_TEXT["release_install_path_unsupported"]


def test_diagnostic_write_failure_does_not_block_rollback(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    (tmp_path / "releases/install-diagnostic.tmp").mkdir()
    attempts = []

    def fail_once(*unused):
        attempts.append(1)
        if len(attempts) == 1:
            raise ReleaseError("synthetic_failure")

    monkeypatch.setattr(releases, "install_host", fail_once)
    with pytest.raises(ReleaseError, match="retry_or_restore") as caught:
        releases.execute_release(args)
    assert caught.value.diagnostic_path is None
    assert len(attempts) == 2
    assert not (tmp_path / "releases/previous").exists()


def test_independent_runner_reports_diagnostic_path(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir()
    (directory / "launch.lock").mkdir()
    args.wait_for_pid = 123
    monkeypatch.setattr(releases, "wait_for_parent", lambda pid: None)

    def fail(*unused):
        error = ReleaseError("release_install_failed_retry_or_restore")
        error.diagnostic_path = str(directory / "install-diagnostic.json")
        raise error

    monkeypatch.setattr(releases, "execute_release", fail)
    assert releases.run_independent_update(args) == 2
    assert releases.update_status()["diagnostic_path"].endswith("install-diagnostic.json")


@pytest.mark.parametrize("owner", ["malmoa-mcp", "unrelated-tool", None])
def test_runtime_only_replaces_verified_legacy_launcher(monkeypatch, tmp_path, owner):
    tools, bins = tmp_path / "tools", tmp_path / "bin"
    legacy = tools / "malmoa-mcp"
    scripts = "Scripts" if sys.platform == "win32" else "bin"
    name = "durumari-mcp.exe" if sys.platform == "win32" else "durumari-mcp"
    (legacy / scripts).mkdir(parents=True)
    bins.mkdir()
    launcher = legacy / scripts / name
    launcher.write_text("legacy launcher")
    if sys.platform == "win32":
        # uv supports copied launchers; ordinary Windows workers need no symlink privilege.
        shutil.copy2(launcher, bins / name)
    else:
        (bins / name).symlink_to(launcher)
    if owner:
        (legacy / "uv-receipt.toml").write_text(
            '[tool]\nentrypoints = [{name = "durumari-mcp", '
            f'from = "{owner}", install-path = {json.dumps(str(bins / name))}}}]\n'
        )
    calls = []

    def run(command):
        calls.append(command)
        if command == ["uv", "tool", "dir"]:
            return str(tools)
        if command == ["uv", "tool", "dir", "--bin"]:
            return str(bins)
        if command[-1] == "--version":
            return "0.3.2"
        return ""

    monkeypatch.setattr(releases, "run", run)
    monkeypatch.setattr(releases, "verify_installed_runtime", lambda *args: None)
    monkeypatch.setattr(
        releases, "verify_runtime", lambda *args: (tmp_path / "wheel", tmp_path / "lock")
    )
    releases.install_runtime(tmp_path, "0.3.2")
    install = next(c for c in calls if c[:3] == ["uv", "tool", "install"])
    assert ("--force" in install) == (owner == "malmoa-mcp")
    assert launcher.read_text() == "legacy launcher"


def test_runtime_does_not_force_a_replaced_or_unregistered_launcher(monkeypatch, tmp_path):
    tools, bins = tmp_path / "tools", tmp_path / "bin"
    legacy = tools / "malmoa-mcp"
    (legacy / "bin").mkdir(parents=True)
    bins.mkdir()
    (legacy / "bin/durumari-mcp").write_text("legacy")
    (bins / "durumari-mcp").write_text("unrelated")
    (legacy / "uv-receipt.toml").write_text(
        '[tool]\nentrypoints = [{name = "durumari-mcp", from = "malmoa-mcp", '
        f'install-path = "{bins / "durumari-mcp"}"}}]\n'
    )
    monkeypatch.setattr(releases, "run", lambda c: str(bins if "--bin" in c else tools))
    assert not releases.legacy_runtime_owns_launcher()


@pytest.mark.parametrize("same_contents", [True, False])
def test_legacy_windows_copied_launcher_requires_matching_contents(
    monkeypatch, tmp_path, same_contents
):
    tools, bins = tmp_path / "tools", tmp_path / "bin"
    legacy = tools / "malmoa-mcp"
    (legacy / "Scripts").mkdir(parents=True)
    bins.mkdir()
    launcher = bins / "durumari-mcp.exe"
    launcher.write_bytes(b"legacy" if same_contents else b"unrelated")
    (legacy / "Scripts/durumari-mcp.exe").write_bytes(b"legacy")
    # Forward slashes keep the test receipt valid TOML on every test host.
    (legacy / "uv-receipt.toml").write_text(
        '[tool]\nentrypoints = [{name = "durumari-mcp.exe", from = "malmoa-mcp", '
        f'install-path = "{launcher.as_posix()}"}}]\n'
    )
    monkeypatch.setattr(releases.sys, "platform", "win32")
    monkeypatch.setattr(releases, "run", lambda c: str(bins if "--bin" in c else tools))
    assert releases.legacy_runtime_owns_launcher() == same_contents


@pytest.mark.parametrize(
    "output,reason",
    [
        ("error: file being used by another process C:/private/token", "runtime_file_in_use"),
        ("Access is denied", "permission_denied"),
        ("certificate verify failed", "tls_certificate_error"),
        ("failed to download https://secret@example.invalid", "network_error"),
        ("No solution found", "dependency_resolution_failed"),
        ("unknown private output", "unclassified_command_failure"),
    ],
)
def test_failure_reason_retains_no_raw_output(output, reason):
    assert releases.command_failure_reason(output) == reason


def test_successful_retry_replaces_current_diagnostic_and_keeps_failure(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir()
    failure = {
        "schema_version": 1,
        "version": "0.1.0",
        "stage": "install_runtime",
        "failure": {"error": "release_install_command_failed", "returncode": 2},
        "rollback": "restored",
    }
    (directory / "install-diagnostic.json").write_text(json.dumps(failure))
    result = releases.execute_release(args)
    report = json.loads((directory / "install-diagnostic.json").read_text())
    assert report["status"] == "completed" and "failure" not in report
    assert report["version"] == result["installed"]
    assert report["attempt_id"] and report["finished_at"]
    assert json.loads((directory / "last-install-failure.json").read_text()) == failure


def test_standalone_status_does_not_install(monkeypatch, tmp_path, capsys):
    installation(monkeypatch, tmp_path)
    releases.state_directory().mkdir()
    releases.write_update_result({"status": "started"})
    # A runner that is still running holds the launch lock.
    releases.claim_lock(releases.state_directory() / "launch.lock", os.getpid())
    monkeypatch.setattr(releases.sys, "argv", ["installer", "update", "--status"])
    monkeypatch.setattr(releases, "execute_release", lambda *a: pytest.fail("unexpected install"))
    releases.main()
    assert json.loads(capsys.readouterr().out) == {"status": "started"}


def test_update_downloads_from_the_announced_host_and_keeps_the_installed_origin(
    monkeypatch, tmp_path
):
    # an installation made with durumari.kr updates while only the
    # announced pair host answers, and keeps naming the server it was installed with.
    args = installation(monkeypatch, tmp_path)
    args.server = "https://durumari.kr"
    path = "durumari-catalog/plugins/durumari-agent/runtime/bundle.json"
    with zipfile.ZipFile(io.BytesIO(bundle())) as archive:
        runtime = json.loads(archive.read(path))
    runtime["service"] = {"schema_version": 1, "gateway_origin": "https://durumari.kr"}
    data = bundle({path: json.dumps(runtime).encode()})
    metadata = manifest()
    metadata["archive"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    urls = []

    def download(url, limit):
        urls.append(url)
        return json.dumps(metadata).encode() if url.endswith("latest.json") else data

    monkeypatch.setattr(releases, "download", download)
    monkeypatch.setattr(releases, "announced_app_origin", lambda host: "https://app.durumari.cloud")
    releases.execute_release(args)
    assert len(urls) == 2
    assert all(url.startswith("https://app.durumari.cloud/downloads/") for url in urls)
    state = json.loads((tmp_path / "releases/installation.json").read_text())
    assert state["origin"] == "https://durumari.kr"


def test_download_fallback_only_retries_network_failure(monkeypatch):
    urls = []

    def download(url, limit):
        urls.append(url)
        if url.startswith("https://app.durumari.cloud"):
            raise releases.ReleaseNetworkError("release_download_failed")
        return b"original"

    monkeypatch.setattr(releases, "download", download)
    assert (
        releases.download_with_fallback(
            "https://app.durumari.cloud", "https://durumari.kr", "/downloads/latest.json", 100
        )
        == b"original"
    )
    assert urls == [
        "https://app.durumari.cloud/downloads/latest.json",
        "https://durumari.kr/downloads/latest.json",
    ]


@pytest.mark.parametrize(
    "code", ["release_redirect_rejected", "release_download_too_large", "release_download_failed"]
)
def test_download_fallback_does_not_bypass_security_errors(monkeypatch, code):
    calls = []

    def download(url, limit):
        calls.append(url)
        raise releases.ReleaseError(code)

    monkeypatch.setattr(releases, "download", download)
    with pytest.raises(releases.ReleaseError, match=code):
        releases.download_with_fallback(
            "https://app.durumari.cloud", "https://durumari.kr", "/downloads/latest.json", 100
        )
    assert len(calls) == 1


def test_update_check_falls_back_when_announced_origin_is_unavailable(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    args.server = "https://durumari.kr"
    args.check = True
    calls = []

    def download(url, limit):
        calls.append(url)
        if url.startswith("https://app.durumari.cloud"):
            raise releases.ReleaseNetworkError("release_download_failed")
        return json.dumps(manifest()).encode()

    monkeypatch.setattr(releases, "download", download)
    monkeypatch.setattr(releases, "announced_app_origin", lambda host: "https://app.durumari.cloud")
    result = releases.execute_release(args)
    assert result["latest"] == manifest()["version"]
    assert calls == [
        "https://app.durumari.cloud/downloads/latest.json",
        "https://durumari.kr/downloads/latest.json",
    ]


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError("https://app.durumari.cloud", 403, "forbidden", {}, None),
        urllib.error.HTTPError("https://app.durumari.cloud", 404, "missing", {}, None),
        urllib.error.HTTPError("https://app.durumari.cloud", 429, "limited", {}, None),
        urllib.error.HTTPError("https://app.durumari.cloud", 501, "not implemented", {}, None),
        urllib.error.URLError(ssl.SSLCertVerificationError("certificate rejected")),
        ssl.SSLError("certificate rejected"),
    ],
)
def test_http_and_tls_errors_are_not_retryable(monkeypatch, failure):
    opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(releases.urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(releases.ReleaseError) as caught:
        releases.download("https://app.durumari.cloud/downloads/latest.json", 100)
    assert not isinstance(caught.value, releases.ReleaseNetworkError)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_transient_http_errors_are_retryable(monkeypatch, status):
    failure = urllib.error.HTTPError("https://app.durumari.cloud", status, "unavailable", {}, None)
    opener = SimpleNamespace(open=lambda *args, **kwargs: (_ for _ in ()).throw(failure))
    monkeypatch.setattr(releases.urllib.request, "build_opener", lambda *args: opener)
    with pytest.raises(releases.ReleaseNetworkError):
        releases.download("https://app.durumari.cloud/downloads/latest.json", 100)


@pytest.mark.parametrize("path", ["/downloads/latest.json", "/downloads/release.zip"])
def test_real_http_503_falls_back_for_metadata_and_archive(monkeypatch, path):
    calls = []

    def open_request(request, timeout):
        calls.append(request.full_url)
        if request.full_url.startswith("https://app.durumari.cloud"):
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return io.BytesIO(b"saved-origin")

    monkeypatch.setattr(
        releases.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request)
    )
    assert (
        releases.download_with_fallback(
            "https://app.durumari.cloud", "https://durumari.kr", path, 100
        )
        == b"saved-origin"
    )
    assert calls == ["https://app.durumari.cloud" + path, "https://durumari.kr" + path]


def test_incomplete_transfer_retries_without_using_partial_data(monkeypatch):
    calls = []

    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            raise http.client.IncompleteRead(b"partial", 10)

    def open_request(request, timeout):
        calls.append(request.full_url)
        if request.full_url.startswith("https://app.durumari.cloud"):
            return Interrupted()
        return io.BytesIO(b"complete")

    monkeypatch.setattr(
        releases.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request)
    )
    assert (
        releases.download_with_fallback(
            "https://app.durumari.cloud", "https://durumari.kr", "/downloads/release.zip", 100
        )
        == b"complete"
    )
    assert len(calls) == 2


def test_short_content_length_retries_real_http_without_using_partial_data():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/interrupted":
                self.send_header("Content-Length", "100")
                self.end_headers()
                self.wfile.write(b"truncated")
            else:
                self.send_header("Content-Length", "8")
                self.end_headers()
                self.wfile.write(b"complete")
            self.close_connection = True

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        # Map the primary and stored origins to two local paths on the same server.
        assert (
            releases.download_with_fallback(base, base + "/saved", "/interrupted", 200)
            == b"complete"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize(
    ("header_values", "limit", "expected"),
    [
        (["not-a-number"], 100, "release_content_length_invalid"),
        (["9", "9"], 100, "release_content_length_invalid"),
        (["2"], 100, "release_content_length_invalid"),
        (["101"], 100, "release_download_too_large"),
    ],
)
def test_invalid_or_oversize_content_length_never_falls_back(
    monkeypatch, header_values, limit, expected
):
    from email.message import Message

    calls = []
    headers = Message()
    for value in header_values:
        headers.add_header("Content-Length", value)

    class Response(io.BytesIO):
        pass

    def open_request(request, timeout):
        calls.append(request.full_url)
        response = Response(b"partial")
        response.headers = headers
        return response

    monkeypatch.setattr(
        releases.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request)
    )
    with pytest.raises(releases.ReleaseError, match=expected) as caught:
        releases.download_with_fallback(
            "https://app.durumari.cloud", "https://durumari.kr", "/download", limit
        )
    assert not isinstance(caught.value, releases.ReleaseNetworkError)
    assert len(calls) == 1
