import hashlib
import importlib.util
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/durumari_plugin.py"
spec = importlib.util.spec_from_file_location("durumari_plugin", SCRIPT)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


@pytest.mark.parametrize("version", ["0.2.8", "0.3.6", "0.3.8", "unknown"])
def test_cloud_context_requires_v2_runtime(monkeypatch, version):
    monkeypatch.setattr(plugin, "run", lambda *a, **kw: version)
    with pytest.raises(
        plugin.SetupError, match="context_runtime_unavailable_update_plugin"
    ):
        plugin.require_guided_runtime(["python", "-m", "durumari_mcp"])


def test_context_runtime_accepts_v2(monkeypatch):
    monkeypatch.setattr(plugin, "run", lambda *a, **kw: "0.4.0")
    plugin.require_guided_runtime(["python", "-m", "durumari_mcp"])


@pytest.mark.parametrize("command", ["status", "remove"])
def test_local_commands_keep_exact_root_and_do_not_install_or_rebind(
    tmp_path, monkeypatch, command
):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/protected/runtime/python")
    monkeypatch.setattr(plugin, "run", lambda cmd, **kw: calls.append(cmd) or 0)
    assert (
        plugin.execute(
            plugin.parser().parse_args(
                [command, "--workspace", str(tmp_path), "--client", "opencode"]
            )
        )
        == 0
    )
    assert calls == [
        [
            "/protected/runtime/python",
            "-I",
            "-m",
            "durumari_mcp",
            "context-" + command,
            "--workspace",
            str(tmp_path.resolve()),
        ]
    ]


@pytest.mark.parametrize("change", ["git-init", "move", "detach"])
def test_status_change_check_is_forwarded_to_context_status(tmp_path, monkeypatch, change):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/protected/runtime/python")
    monkeypatch.setattr(plugin, "run", lambda cmd, **kw: calls.append(cmd) or 0)
    args = ["status", "--workspace", str(tmp_path), "--client", "claude", "--change", change]
    assert plugin.execute(plugin.parser().parse_args(args)) == 0
    assert calls == [
        [
            "/protected/runtime/python",
            "-I",
            "-m",
            "durumari_mcp",
            "context-status",
            "--workspace",
            str(tmp_path.resolve()),
            "--change",
            change,
        ]
    ]


def test_change_check_is_status_only(tmp_path):
    with pytest.raises(plugin.SetupError, match="invalid_plugin_arguments"):
        plugin.parser().parse_args(
            ["remove", "--workspace", str(tmp_path), "--change", "move"]
        )


def bundle(tmp_path):
    wheel = "durumari_mcp-0.4.0-py3-none-any.whl"
    files = {wheel: b"wheel bytes", "requirements.txt": b"locked dependencies"}
    for name, value in files.items():
        (tmp_path / name).write_bytes(value)
    metadata = {
        "schema_version": 1,
        "package": "durumari-mcp",
        "version": "0.4.0",
        "service": {"schema_version": 1, "gateway_origin": "https://durumari.example"},
        "files": {
            name: hashlib.sha256(value).hexdigest() for name, value in files.items()
        },
    }
    (tmp_path / "bundle.json").write_text(json.dumps(metadata))
    return metadata


@pytest.mark.parametrize(
    "name", ["durumari_mcp-0.4.0-py3-none-any.whl", "requirements.txt"]
)
def test_tampered_runtime_or_lock_is_rejected_before_install(tmp_path, name):
    bundle(tmp_path)
    (tmp_path / name).write_bytes(b"different bytes")
    with pytest.raises(plugin.SetupError, match="runtime_bundle_integrity_failed"):
        plugin.verify_bundle(tmp_path)


def test_bundle_path_escape_and_symlink_rejected(tmp_path):
    metadata = bundle(tmp_path)
    metadata["files"]["../outside.whl"] = "0" * 64
    (tmp_path / "bundle.json").write_text(json.dumps(metadata))
    with pytest.raises(plugin.SetupError, match="invalid_runtime_bundle"):
        plugin.verify_bundle(tmp_path)
    bundle(tmp_path)
    wheel = tmp_path / "durumari_mcp-0.4.0-py3-none-any.whl"
    real = tmp_path / "real.whl"
    wheel.rename(real)
    wheel.symlink_to(real)
    with pytest.raises(plugin.SetupError, match="runtime_bundle_integrity_failed"):
        plugin.verify_bundle(tmp_path)


def test_invalid_bundle_prevents_install_mutation(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(
        plugin, "run", lambda *a, **kw: pytest.fail("installer executed")
    )
    with pytest.raises(plugin.SetupError, match="invalid_runtime_bundle"):
        plugin.execute(plugin.parser().parse_args(["install"]))


def test_bad_arguments_never_echo_credentials(monkeypatch, capsys):
    monkeypatch.setattr(
        plugin.sys,
        "argv",
        ["plugin", "connect", "--password", "synthetic-private-value"],
    )
    assert plugin.main() == 1
    output = capsys.readouterr()
    assert "synthetic-private-value" not in output.out + output.err
    assert "invalid_plugin_arguments" in output.err


def updater_bundle(tmp_path, source):
    directory = tmp_path / "runtime"
    directory.mkdir()
    metadata = bundle(directory)
    wheel = directory / "durumari_mcp-0.4.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("durumari_mcp/releases.py", source)
    metadata["files"][wheel.name] = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (directory / "bundle.json").write_text(json.dumps(metadata))


@pytest.mark.parametrize("flag", ["--check", "--status"])
def test_update_uses_verified_bundled_updater_without_importing_runtime(
    tmp_path, monkeypatch, capsys, flag
):
    updater_bundle(
        tmp_path,
        """
class ReleaseError(Exception): pass
def install_runtime_bundle(*args): raise AssertionError('unexpected install')
def execute_cli_release(args):
    return {'status': 'fixture', 'check': args.check, 'read_status': args.status}
""",
    )
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "uv")
    monkeypatch.setattr(
        plugin, "runtime_python", lambda: pytest.fail("installed runtime imported")
    )
    assert plugin.execute(plugin.parser().parse_args(["update", flag])) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["check"] == (flag == "--check")
    assert result["read_status"] == (flag == "--status")


def test_old_unsafe_bundled_updater_is_not_executed(tmp_path, monkeypatch):
    updater_bundle(
        tmp_path,
        "class ReleaseError(Exception): pass\n"
        "def execute_cli_release(args): raise AssertionError('unsafe updater ran')",
    )
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "uv")
    with pytest.raises(
        plugin.SetupError, match="safe_runtime_installer_missing_update_plugin"
    ):
        plugin.execute(plugin.parser().parse_args(["update", "--check"]))


@pytest.mark.parametrize(
    "source",
    [
        "import module_missing_on_this_python",
        "def broken(:",
        "from pathlib import Path\nPath('.').attribute_from_a_newer_python()",
    ],
)
def test_incompatible_bundled_installer_is_a_setup_error(tmp_path, monkeypatch, source):
    updater_bundle(tmp_path, source)
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/uv")
    with pytest.raises(
        plugin.SetupError, match="safe_runtime_installer_missing_update_plugin"
    ):
        plugin.execute(plugin.parser().parse_args(["install"]))


INSTALLER = """
import os
class ReleaseError(Exception):
    def __init__(self, code, details=None):
        super().__init__(code)
        self.details = details or {}
        self.diagnostic_path = None
def execute_cli_release(args): raise AssertionError('unexpected update')
"""


def test_install_passes_resolved_uv_to_bundled_installer(tmp_path, monkeypatch, capsys):
    updater_bundle(
        tmp_path,
        INSTALLER
        + "def install_runtime_bundle(directory, version):\n"
        + "    open(os.environ['FIXTURE_OUT'], 'w').write(os.environ['DURUMARI_MCP_UV_EXECUTABLE'])\n",
    )
    out = tmp_path / "seen-uv"
    monkeypatch.setenv("FIXTURE_OUT", str(out))
    monkeypatch.delenv(plugin.UV_EXECUTABLE_ENV, raising=False)
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/bin/uv")
    assert plugin.execute(plugin.parser().parse_args(["install"])) == 0
    assert out.read_text() == "/resolved/bin/uv"
    assert plugin.UV_EXECUTABLE_ENV not in plugin.os.environ
    assert json.loads(capsys.readouterr().out)["runtime"] == "installed"


def test_release_recovery_details_are_printed_without_command_output(
    tmp_path, monkeypatch, capsys
):
    updater_bundle(
        tmp_path,
        INSTALLER
        + "def install_runtime_bundle(directory, version):\n"
        + "    raise ReleaseError('runtime_rollback_requires_recovery', {\n"
        + "        'snapshot_id': 'abc123', 'runtime_rollback': 'requires_recovery',\n"
        + "        'stderr': 'private command output'})\n",
    )
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/bin/uv")
    monkeypatch.setattr(plugin.sys, "argv", ["durumari_plugin.py", "install"])
    assert plugin.main() == 1
    error = json.loads(capsys.readouterr().err)
    assert error == {
        "error": "runtime_rollback_requires_recovery",
        "snapshot_id": "abc123",
        "runtime_rollback": "requires_recovery",
    }


def test_installer_internal_key_error_is_not_reported_as_missing_installer(
    tmp_path, monkeypatch
):
    updater_bundle(
        tmp_path,
        INSTALLER + "def install_runtime_bundle(directory, version):\n    {}['missing']\n",
    )
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/bin/uv")
    with pytest.raises(plugin.SetupError, match="runtime_installer_internal_error"):
        plugin.execute(plugin.parser().parse_args(["install"]))


@pytest.mark.parametrize(
    ("argv", "forwarded"),
    [
        (["install"], ["install"]),
        (
            ["update", "--server", "https://durumari.example", "--client", "codex"],
            ["update", "--server", "https://durumari.example", "--client", "codex"],
        ),
        (["update", "--status"], ["update", "--status"]),
    ],
)
def test_older_python_delegates_installer_to_uv_python_312(
    tmp_path, monkeypatch, argv, forwarded
):
    updater_bundle(
        tmp_path,
        "raise AssertionError('installer must not load on this interpreter')",
    )
    calls = []
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/bin/uv")
    monkeypatch.setattr(plugin, "INSTALLER_PYTHON", (99, 0))
    monkeypatch.delenv(plugin.INSTALLER_DELEGATED_ENV, raising=False)
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda command, **kw: calls.append((command, kw["env"]))
        or SimpleNamespace(returncode=0, stderr=""),
    )
    assert plugin.execute(plugin.parser().parse_args(argv)) == 0
    [(command, env)] = calls
    assert command[:9] == [
        "/resolved/bin/uv",
        "run",
        "--no-project",
        "--python",
        "99.0",
        "python",
        "-X",
        "utf8",
        "-I",
    ]
    assert command[9] == str(SCRIPT.resolve())
    assert command[10:] == forwarded
    assert env[plugin.INSTALLER_DELEGATED_ENV] == "1"


def test_delegated_installer_never_recurses(tmp_path, monkeypatch):
    updater_bundle(tmp_path, "raise AssertionError('not loaded')")
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/bin/uv")
    monkeypatch.setattr(plugin, "INSTALLER_PYTHON", (99, 0))
    monkeypatch.setenv(plugin.INSTALLER_DELEGATED_ENV, "1")
    with pytest.raises(
        plugin.SetupError, match="python_3_12_required_for_runtime_installer"
    ):
        plugin.execute(plugin.parser().parse_args(["install"]))


@pytest.mark.parametrize(
    ("child_stderr", "printed"),
    [
        # uv could not start the 3.12 child: the parent reports a code.
        ("error: No interpreter found for Python 3.12\n", {"error": "runtime_installer_failed"}),
        # The child wrapper ran and already reported its own code.
        ('{"error": "runtime_in_use_close_hosts_and_retry"}\n', None),
    ],
)
def test_delegated_failure_always_ends_with_one_structured_error(
    tmp_path, monkeypatch, capsys, child_stderr, printed
):
    updater_bundle(tmp_path, "raise AssertionError('not loaded')")
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/bin/uv")
    monkeypatch.setattr(plugin, "INSTALLER_PYTHON", (99, 0))
    monkeypatch.delenv(plugin.INSTALLER_DELEGATED_ENV, raising=False)
    monkeypatch.setattr(
        plugin.subprocess,
        "run",
        lambda command, **kw: SimpleNamespace(returncode=2, stderr=child_stderr),
    )
    monkeypatch.setattr(plugin.sys, "argv", ["durumari_plugin.py", "install"])
    assert plugin.main() == 1
    lines = capsys.readouterr().err.splitlines()
    assert lines[0] == child_stderr.strip()
    assert (json.loads(lines[1]) if len(lines) > 1 else None) == printed


def test_delegated_child_guidance_round_trips_as_utf8(tmp_path, monkeypatch, capsys):
    import subprocess as real_subprocess
    import sys as real_sys

    child = tmp_path / "child.py"
    child.write_text(
        "import json, sys\n"
        "print(json.dumps({'error': 'runtime_update_required_then_restart_host'}), "
        "file=sys.stderr)\n"
        "print('연결 안내를 사용하려면 호스트를 다시 시작해 주세요.', file=sys.stderr)\n"
        "sys.exit(1)\n",
        encoding="utf-8",
    )
    run = real_subprocess.run

    def as_python(command, **kw):
        # Replace `uv run ... python` with this interpreter, keeping -X utf8 -I.
        index = command.index("python")
        return run([real_sys.executable, *command[index + 1 : -2], str(child)], **kw)

    updater_bundle(tmp_path, "raise AssertionError('not loaded')")
    monkeypatch.setattr(plugin, "PLUGIN_ROOT", tmp_path)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/resolved/bin/uv")
    monkeypatch.setattr(plugin, "INSTALLER_PYTHON", (99, 0))
    monkeypatch.delenv(plugin.INSTALLER_DELEGATED_ENV, raising=False)
    monkeypatch.setattr(plugin.subprocess, "run", as_python)
    monkeypatch.setattr(plugin.sys, "argv", ["durumari_plugin.py", "update"])
    assert plugin.main() == 1
    err = capsys.readouterr().err
    assert "연결 안내를 사용하려면" in err and "\ufffd" not in err


def test_prompt_context_depth_reaches_the_runtime(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/runtime/python")
    monkeypatch.setattr(plugin, "run", lambda cmd, **kw: calls.append(cmd) or 0)
    args = plugin.parser().parse_args(
        ["prompt-context", "--workspace", str(tmp_path), "--depth", "가볍게"]
    )
    assert plugin.execute(args) == 0
    assert calls[-1] == [
        "/runtime/python", "-I", "-m", "durumari_mcp", "prompt-context",
        "--workspace", str(tmp_path.resolve()), "--depth", "가볍게",
    ]
    args = plugin.parser().parse_args(
        ["prompt-context", "--workspace", str(tmp_path), "--all-projects", "--depth", "off"]
    )
    plugin.execute(args)
    assert calls[-1][-1] == "--all-projects"
