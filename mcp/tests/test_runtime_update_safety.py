"""Isolated real uv installations: no user runtime, host, credentials or queues."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from test_releases import installation

from durumari_mcp import releases
from durumari_mcp.releases import ReleaseError

# uv messages meaning packages or Python 3.12 cannot be obtained here (no network
# and no cache). Any other build/install failure is real and is not skipped.
UNAVAILABLE_OFFLINE = (
    "network connectivity is disabled",
    "offline",
    "not found in the cache",
    "no interpreter found",
    "failed to fetch",
    "failed to download",
    "dns error",
)


def run_uv_or_skip(command, environment, timeout):
    """Real uv runtime fixture: runs wherever uv and its offline cache work."""
    try:
        subprocess.run(
            command,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError:
        pytest.skip("uv is not installed; real runtime replacement tests need uv")
    except subprocess.CalledProcessError as error:
        output = (error.stderr or "") + (error.stdout or "")
        if any(text in output.lower() for text in UNAVAILABLE_OFFLINE):
            pytest.skip(
                "uv cannot obtain packages or Python 3.12 here "
                f"({command[1]} {command[2]}); needs network or a primed uv cache"
            )
        pytest.fail(f"{' '.join(command[:3])} failed:\n{output[-4000:]}")


@pytest.fixture(scope="module")
def isolated_runtime(tmp_path_factory):
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed; real runtime replacement tests need uv")
    root = tmp_path_factory.mktemp("owned-runtime-upgrade")
    package = Path(__file__).resolve().parents[1]
    wheels = root / "wheels"
    environment = {
        **os.environ,
        "UV_TOOL_DIR": str(root / "tools"),
        "UV_TOOL_BIN_DIR": str(root / "bin"),
    }
    run_uv_or_skip(
        ["uv", "build", "--wheel", "--out-dir", str(wheels), str(package)], environment, 90
    )
    wheel = next(wheels.glob("*.whl"))
    version = wheel.name.split("-")[1]
    run_uv_or_skip(
        # Same network policy as install_runtime_bundle's later reinstalls: if
        # this succeeds they can resolve too; if it cannot, every test skips.
        ["uv", "tool", "install", str(wheel), "--python", "3.12"],
        environment,
        180,
    )
    bundle = root / "plugin/runtime"
    bundle.mkdir(parents=True)
    shutil.copy2(wheel, bundle / wheel.name)
    (bundle / "requirements.txt").write_text("", encoding="utf-8")
    (bundle / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package": "durumari-mcp",
                "version": version,
                "service": {
                    "schema_version": 1,
                    "gateway_origin": "https://fixture.example.invalid",
                },
                "files": {
                    name: hashlib.sha256((bundle / name).read_bytes()).hexdigest()
                    for name in (wheel.name, "requirements.txt")
                },
            }
        ),
        encoding="utf-8",
    )
    # Synthetic protected data lives outside the tool environment and is never copied.
    for name in ("credential", "profile", "binding", "pending", "quarantine"):
        (root / name).write_text("synthetic preserved " + name, encoding="utf-8")
    return root, bundle, version, environment


@pytest.fixture
def runtime(isolated_runtime, monkeypatch):
    root, bundle, version, environment = isolated_runtime
    for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR"):
        monkeypatch.setenv(name, environment[name])
    directory, launcher = releases.runtime_paths()
    assert directory.resolve().is_relative_to(root.resolve())
    assert launcher.parent.resolve().is_relative_to(root.resolve())
    return root, bundle, version, directory, launcher


def assert_preserved(root):
    for name in ("credential", "profile", "binding", "pending", "quarantine"):
        assert (root / name).read_text(encoding="utf-8") == "synthetic preserved " + name


if sys.platform == "win32":  # Windows-only; not collected elsewhere, so CI sees no skips.

    def test_running_interpreter_is_refused_before_any_mutation(runtime):
        root, bundle, version, directory, launcher = runtime
        before = launcher.read_bytes()
        python = directory / "Scripts/python.exe"
        child = subprocess.Popen(
            [
                str(python),
                "-I",
                "-c",
                "import sys; print('ready', flush=True); sys.stdin.readline()",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            creationflags=0x08000000,
        )
        try:
            assert child.stdout.readline().strip() == "ready"
            with pytest.raises(ReleaseError, match="runtime_in_use_close_hosts_and_retry"):
                releases.install_runtime_bundle(bundle, version)
            assert launcher.read_bytes() == before
            assert not (releases.runtime_update_directory(directory) / "update.lock").exists()
        finally:
            child.communicate("finish\n", timeout=15)
        releases.check_runtime_usage(directory)
        releases.verify_installed_runtime(directory, version)
        assert_preserved(root)


@pytest.mark.parametrize(
    "code", ["release_install_command_failed", "release_install_command_timeout"]
)
def test_partial_uv_deletion_restores_real_import_cli_and_launcher(runtime, monkeypatch, code):
    root, bundle, version, directory, launcher = runtime
    before = launcher.read_bytes()
    snapshots = releases.runtime_update_directory(directory) / "snapshots"
    retained = set(snapshots.iterdir()) if snapshots.exists() else set()
    original = releases.run

    def failed_install(command):
        if command[:3] == ["uv", "tool", "install"]:
            module = next(directory.rglob("site-packages/durumari_mcp"))
            assert module.resolve().is_relative_to(root.resolve())
            shutil.rmtree(module)
            launcher.write_bytes(b"synthetic partial launcher")
            raise ReleaseError(code)
        return original(command)

    monkeypatch.setattr(releases, "run", failed_install)
    with pytest.raises(ReleaseError, match=code) as caught:
        releases.install_runtime_bundle(bundle, version)
    assert caught.value.details["runtime_rollback"] == "restored"
    # A verified restore leaves nothing to recover: no environment copy accumulates.
    assert set(snapshots.iterdir()) == retained
    releases.verify_installed_runtime(directory, version)
    assert original([str(launcher), "--version"]) == version
    assert launcher.read_bytes() == before
    assert not (releases.runtime_update_directory(directory) / "update.lock").exists()
    assert_preserved(root)


def test_real_reinstall_is_verified_and_can_retry_after_failure(runtime):
    root, bundle, version, directory, launcher = runtime
    releases.install_runtime_bundle(bundle, version)
    releases.verify_installed_runtime(directory, version)
    assert releases.run([str(launcher), "--version"]) == version
    assert_preserved(root)


if sys.platform == "win32":  # Windows-only; not collected elsewhere, so CI sees no skips.

    def test_private_file_lock_is_refused_without_a_runtime_process(runtime):
        import ctypes
        from ctypes import wintypes

        _root, _bundle, _version, directory, _launcher = runtime
        private = directory / "synthetic-lock.txt"
        private.write_text("owned test file")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(private), 0x80000000, 0, None, 3, 0, None)
        assert handle != ctypes.c_void_p(-1).value
        try:
            with pytest.raises(ReleaseError, match="runtime_in_use_close_hosts_and_retry"):
                releases.check_runtime_usage(directory)
        finally:
            kernel.CloseHandle(handle)
            private.unlink()
        releases.check_runtime_usage(directory)


def test_broken_module_repair_uses_verified_wheel_without_installed_import(
    runtime, monkeypatch, capsys
):
    import importlib.util

    root, _bundle, version, directory, launcher = runtime
    module = next(directory.rglob("site-packages/durumari_mcp"))
    assert module.resolve().is_relative_to(root.resolve())
    shutil.rmtree(module)
    with pytest.raises(ReleaseError):
        releases.verify_installed_runtime(directory, version)
    from plugin_layout import PLUGIN_SCRIPT as source
    spec = importlib.util.spec_from_file_location("fixture_runtime_bootstrap", source)
    bootstrap = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bootstrap)
    bootstrap.PLUGIN_ROOT = root / "plugin"
    # Status is available through the verified wheel even while import is broken.
    monkeypatch.setenv("LOCALAPPDATA", str(root / "state"))
    monkeypatch.setenv("HOME", str(root / "state"))
    state = releases.state_directory()
    assert state.resolve().is_relative_to(root.resolve())
    state.mkdir(parents=True)
    (state / "update-result.json").write_text('{"status":"failed","error":"fixture_failure"}')
    assert bootstrap.execute(bootstrap.parser().parse_args(["update", "--status"])) == 0
    assert json.loads(capsys.readouterr().out)["error"] == "fixture_failure"
    assert bootstrap.execute(bootstrap.parser().parse_args(["install"])) == 0
    releases.verify_installed_runtime(directory, version)
    assert releases.run([str(launcher), "--version"]) == version
    assert_preserved(root)


def test_failed_runtime_install_does_not_reinstall_untouched_hosts(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)

    def fail(*args):
        raise ReleaseError("synthetic_runtime_failure", details={"runtime_rollback": "restored"})

    monkeypatch.setattr(releases, "install_runtime", fail)
    monkeypatch.setattr(
        releases, "install_host", lambda *args: pytest.fail("untouched host was changed")
    )
    with pytest.raises(ReleaseError, match="retry_or_restore"):
        releases.execute_release(args)
    report = json.loads((tmp_path / "releases/install-diagnostic.json").read_text())
    assert report["rollback"] == "restored"


def test_unavailable_previous_runtime_is_not_reported_as_healthy_rollback(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)

    def fail(*args):
        raise ReleaseError(
            "synthetic_runtime_failure",
            details={"runtime_rollback": "restored_unavailable_snapshot"},
        )

    monkeypatch.setattr(releases, "install_runtime", fail)
    with pytest.raises(ReleaseError, match="retry_or_restore"):
        releases.execute_release(args)
    report = json.loads((tmp_path / "releases/install-diagnostic.json").read_text())
    assert report["rollback"] == "restored_catalog_runtime_unavailable"


def test_failed_candidate_verification_restores_usable_runtime(runtime, monkeypatch):
    root, bundle, version, directory, _launcher = runtime
    original_run, original_verify = releases.run, releases.verify_installed_runtime
    candidate = False

    def run(command):
        nonlocal candidate
        result = original_run(command)
        if command[:3] == ["uv", "tool", "install"]:
            candidate = True
        return result

    def verify(path, expected):
        nonlocal candidate
        if candidate:
            candidate = False
            raise ReleaseError("runtime_version_not_verified")
        return original_verify(path, expected)

    monkeypatch.setattr(releases, "run", run)
    monkeypatch.setattr(releases, "verify_installed_runtime", verify)
    with pytest.raises(ReleaseError, match="runtime_version_not_verified") as caught:
        releases.install_runtime_bundle(bundle, version)
    assert caught.value.details["runtime_rollback"] == "restored"
    original_verify(directory, version)
    assert_preserved(root)


def test_runtime_preflight_precedes_catalog_switch(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    catalog = tmp_path / "releases/catalog/plugins/durumari-agent/plugin.json"
    before = catalog.read_bytes()

    def refuse():
        raise ReleaseError("runtime_in_use_close_hosts_and_retry")

    monkeypatch.setattr(releases, "preflight_runtime", refuse)
    with pytest.raises(ReleaseError, match="runtime_in_use_close_hosts_and_retry"):
        releases.execute_release(args)
    assert catalog.read_bytes() == before
    assert not (tmp_path / "releases/previous").exists()
    report = json.loads((tmp_path / "releases/install-diagnostic.json").read_text())
    assert report["stage"] == "runtime_preflight" and report["rollback"] == "not_needed"


def test_old_failed_catalog_does_not_block_current_rollback(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    old = tmp_path / "releases/failed"
    old.mkdir()
    (old / "evidence").write_text("old failure")

    failures = []

    def failed_host(*args):
        if not failures:
            failures.append(True)
            raise ReleaseError("synthetic_host_failure")

    monkeypatch.setattr(releases, "install_host", failed_host)
    with pytest.raises(ReleaseError, match="retry_or_restore"):
        releases.execute_release(args)
    assert (old / "evidence").read_text() == "old failure"
    report = json.loads((tmp_path / "releases/install-diagnostic.json").read_text())
    assert report["rollback"] == "restored"
    assert not (tmp_path / "releases/previous").exists()
    monkeypatch.setattr(releases, "install_host", lambda *args: None)
    assert releases.execute_release(args)["installed"] == "0.2.0"


def test_successful_install_inside_caller_except_block_removes_its_snapshot(runtime):
    root, bundle, version, directory, _launcher = runtime
    snapshots = releases.runtime_update_directory(directory) / "snapshots"
    # Earlier failure tests in this module intentionally retain their snapshots.
    retained = set(snapshots.iterdir()) if snapshots.exists() else set()
    try:
        raise RuntimeError("caller is handling an unrelated failure")
    except RuntimeError:
        # Release rollback reinstalls from an except block; exc_info is not ours.
        releases.install_runtime_bundle(bundle, version)
    assert set(snapshots.iterdir()) == retained
    releases.verify_installed_runtime(directory, version)
    assert_preserved(root)


def test_refusal_before_any_change_removes_its_snapshot(runtime, monkeypatch):
    root, bundle, version, directory, launcher = runtime
    snapshots = releases.runtime_update_directory(directory) / "snapshots"
    retained = set(snapshots.iterdir()) if snapshots.exists() else set()
    before = launcher.read_bytes()
    checks = []

    def second_check_refuses():
        checks.append(True)
        if len(checks) == 2:
            raise ReleaseError("runtime_in_use_close_hosts_and_retry")

    monkeypatch.setattr(releases, "preflight_runtime", second_check_refuses)
    with pytest.raises(ReleaseError, match="runtime_in_use_close_hosts_and_retry"):
        releases.install_runtime_bundle(bundle, version)
    assert len(checks) == 2
    assert set(snapshots.iterdir()) == retained
    assert launcher.read_bytes() == before
    assert not (releases.runtime_update_directory(directory) / "update.lock").exists()
    releases.verify_installed_runtime(directory, version)
    assert_preserved(root)


RUNTIME = Path("owned/tools/durumari-mcp").absolute()
IMAGES = {
    10: str(RUNTIME / "Scripts" / "python.exe"),
    20: str(Path("other/python/python.exe").absolute()),
    30: str(RUNTIME / "Scripts" / "python.exe"),
    40: None,  # exited or access denied: Restart Manager decides
    50: str(RUNTIME) + "-previous" + os.sep + "python.exe",  # sibling, not inside
}


@pytest.mark.parametrize(
    ("processes", "current", "code"),
    [
        # The updater itself runs from this runtime.
        ([(10, 1, "python.exe"), (1, 0, "explorer.exe")], 10, "must_run_outside_runtime"),
        # An ancestor (runtime CLI) launched the updater from another Python.
        ([(10, 1, "python.exe"), (20, 10, "python.exe")], 20, "must_run_outside_runtime"),
        # An unrelated runtime process is a host that must exit first.
        ([(30, 1, "python.exe"), (20, 1, "python.exe")], 20, "in_use_close_hosts_and_retry"),
    ],
)
def test_runtime_image_owned_by_updater_lineage_is_reported_distinctly(processes, current, code):
    with pytest.raises(ReleaseError, match=code):
        releases.check_runtime_process_images(RUNTIME, processes, IMAGES.get, current)


def test_uninspectable_and_unrelated_processes_do_not_block():
    processes = [
        (20, 1, "python.exe"),
        (40, 1, "python.exe"),
        (50, 1, "pythonw.exe"),
        (10, 1, "notepad.exe"),
    ]
    releases.check_runtime_process_images(RUNTIME, processes, IMAGES.get, 20)


def test_process_lineage_stops_at_cycles_and_missing_parents():
    assert releases.process_lineage({3: 2, 2: 1, 1: 3}, 3) == {1, 2, 3}
    assert releases.process_lineage({3: 2}, 3) == {2, 3}
    assert releases.process_lineage({}, 0) == set()


def test_runtime_paths_use_uv_resolved_by_wrapper(monkeypatch, tmp_path):
    uv = tmp_path / "custom-bin" / "uv"
    uv.parent.mkdir()
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    calls = []
    monkeypatch.setattr(releases, "run", lambda command: calls.append(command) or str(tmp_path))
    monkeypatch.setenv(releases.UV_EXECUTABLE_ENV, str(uv))
    releases.runtime_paths()
    assert [command[0] for command in calls] == [str(uv), str(uv)]
    monkeypatch.setenv(releases.UV_EXECUTABLE_ENV, "relative/uv")
    assert releases.uv_command() == "uv"
    monkeypatch.delenv(releases.UV_EXECUTABLE_ENV)
    assert releases.uv_command() == "uv"


def test_junction_check_tolerates_path_without_is_junction():
    class OldPath:
        pass

    assert releases._is_junction(OldPath()) is False  # type: ignore[arg-type]


def test_update_state_stays_out_of_uv_tool_dir_and_uv_tool_list_works(runtime):
    root, bundle, version, directory, _launcher = runtime
    releases.install_runtime_bundle(bundle, version)
    owned = releases.runtime_update_directory(directory)
    assert owned.resolve().is_relative_to(root.resolve())
    assert not owned.resolve().is_relative_to(directory.parent.resolve())
    # uv rejects foreign entries in its tool directory.
    assert sorted(path.name for path in directory.parent.iterdir() if path.is_dir()) == [
        "durumari-mcp"
    ]
    assert "durumari-mcp" in releases.run([releases.uv_command(), "tool", "list"])


def test_missing_old_interpreter_is_an_unavailable_snapshot_not_a_crash(runtime, tmp_path):
    root, bundle, version, directory, launcher = runtime
    python = directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python3")
    backup = tmp_path / "python-backup"
    shutil.copy2(python, backup, follow_symlinks=False)
    snapshots = releases.runtime_update_directory(directory) / "snapshots"
    retained = set(snapshots.iterdir()) if snapshots.exists() else set()
    python.unlink()
    try:
        # run() reports the missing interpreter as ReleaseError, never OSError.
        with pytest.raises(ReleaseError, match="release_install_command_unavailable"):
            releases.run([str(python), "--version"])
        with pytest.raises(ReleaseError) as caught:
            releases.install_runtime_bundle(bundle, version)
        assert caught.value.details["runtime_rollback"] == "restored_unavailable_snapshot"
        # An unverifiable restore keeps exactly one snapshot for recovery.
        assert len(set(snapshots.iterdir()) - retained) == 1
        assert not (releases.runtime_update_directory(directory) / "update.lock").exists()
    finally:
        if not (python.exists() or python.is_symlink()):
            shutil.copy2(backup, python, follow_symlinks=False)
    releases.verify_installed_runtime(directory, version)
    assert releases.run([str(launcher), "--version"]) == version
    assert_preserved(root)


def test_usage_check_lists_files_beyond_max_path_through_long_path_form(monkeypatch, tmp_path):
    runtime = tmp_path / "durumari-mcp"
    deep = runtime.joinpath(*(["nested-directory-segment"] * 12))
    # Creating the fixture itself needs the long-path form on Windows.
    releases.filesystem_path(deep).mkdir(parents=True)
    private = deep / "private-module.py"
    releases.filesystem_path(private).write_text("owned")
    assert len(str(private)) > 260
    shared = runtime / "shared-cache-file"
    shared.write_text("hardlinked")
    os.link(shared, tmp_path / "other-environment-link")
    if sys.platform != "win32":
        (runtime / "link").symlink_to(private)
    seen = []
    original = releases.filesystem_path

    def long_path(path):
        seen.append(path)
        return original(path)

    monkeypatch.setattr(releases, "filesystem_path", long_path)
    assert releases.runtime_usage_resources(runtime) == [str(original(private))]
    # Enumeration starts from the long-path-safe root, as the snapshot copy does.
    assert seen == [runtime.absolute()]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX directory permissions")
def test_update_directory_falls_back_when_tool_dir_parent_is_not_writable(monkeypatch, tmp_path):
    readonly = tmp_path / "readonly"
    runtime = readonly / "tools" / "durumari-mcp"
    runtime.mkdir(parents=True)
    state = tmp_path / "state" / "releases"
    monkeypatch.setattr(releases, "state_directory", lambda: state)
    readonly.chmod(0o555)
    try:
        if os.access(readonly, os.W_OK):
            pytest.skip("permissions are not enforced for this user")
        assert releases.runtime_update_directory(runtime) == state / "runtime-update"
        assert not (readonly / "durumari-runtime-update").exists()
        # Neither candidate usable: refused before any lock, snapshot or uv change.
        blocked = tmp_path / "blocked"
        blocked.mkdir(mode=0o555)
        monkeypatch.setattr(releases, "state_directory", lambda: blocked / "releases")
        with pytest.raises(ReleaseError, match="same_filesystem_required"):
            releases.runtime_update_directory(runtime)
    finally:
        readonly.chmod(0o755)
        if (tmp_path / "blocked").exists():
            (tmp_path / "blocked").chmod(0o755)


@pytest.mark.parametrize(
    ("stderr", "outcome"),
    [
        ("error: Network connectivity is disabled, but cryptography was not found", "skip"),
        ("error: No interpreter found for Python 3.12 in managed installations", "skip"),
        ("error: Build backend failed: SyntaxError in setup", "fail"),
    ],
)
def test_runtime_fixture_skips_only_for_unavailable_offline_uv(stderr, outcome):
    command = [sys.executable, "-c", f"import sys; sys.stderr.write({stderr!r}); sys.exit(2)"]
    expected = pytest.skip.Exception if outcome == "skip" else pytest.fail.Exception
    with pytest.raises(expected):
        run_uv_or_skip(command, dict(os.environ), 30)


def test_failed_first_install_reports_not_installed_and_keeps_no_snapshot(
    runtime, monkeypatch, tmp_path
):
    root, bundle, version, directory, launcher = runtime
    owned = releases.runtime_update_directory(directory)
    retained = set((owned / "snapshots").iterdir()) if (owned / "snapshots").exists() else set()
    fresh_tools, fresh_bin = tmp_path / "fresh-tools", tmp_path / "fresh-bin"
    monkeypatch.setenv("UV_TOOL_DIR", str(fresh_tools))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(fresh_bin))
    fresh_runtime, fresh_launcher = releases.runtime_paths()
    original = releases.run

    def partial_install(command):
        if command[1:3] == ["tool", "install"]:
            (fresh_runtime / "bin").mkdir(parents=True)
            fresh_launcher.parent.mkdir(parents=True, exist_ok=True)
            fresh_launcher.write_text("partial")
            raise ReleaseError("release_install_command_failed")
        return original(command)

    monkeypatch.setattr(releases, "run", partial_install)
    with pytest.raises(ReleaseError, match="release_install_command_failed") as caught:
        releases.install_runtime_bundle(bundle, version)
    assert caught.value.details["runtime_rollback"] == "not_installed"
    assert not fresh_runtime.exists() and not fresh_launcher.exists()
    fresh_owned = releases.runtime_update_directory(fresh_runtime)
    assert not any((fresh_owned / "snapshots").iterdir())
    assert not (fresh_owned / "update.lock").exists()
    # The shared fixture runtime and its retained snapshots were not involved.
    assert (set((owned / "snapshots").iterdir()) if retained else set()) == retained
    releases.verify_installed_runtime(directory, version)
    assert_preserved(root)


def test_release_rollback_reinstalls_previous_runtime_as_previous_release(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    calls = []
    monkeypatch.setattr(
        releases, "install_runtime", lambda *args, **kwargs: calls.append((args[1], kwargs))
    )
    failures = []

    def failed_host(*args):
        if not failures:
            failures.append(True)
            raise ReleaseError("synthetic_host_failure")

    monkeypatch.setattr(releases, "install_host", failed_host)
    with pytest.raises(ReleaseError, match="retry_or_restore"):
        releases.execute_release(args)
    # The new runtime is installed with the full check; the restored previous
    # release is checked only by what every release provides.
    assert calls[0][1] == {}
    assert calls[-1][1] == {"previous_release": True}


def old_release_bundle(directory, version):
    """A minimal older release: no cli/project_gateway modules, no --version flag."""
    import zipfile

    directory.mkdir(parents=True)
    name = f"durumari_mcp-{version}"
    files = {
        "durumari_mcp/__init__.py": "def main():\n    print('old release')\n",
        f"{name}.dist-info/METADATA": (
            f"Metadata-Version: 2.1\nName: durumari-mcp\nVersion: {version}\n"
        ),
        f"{name}.dist-info/WHEEL": (
            "Wheel-Version: 1.0\nGenerator: fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
        ),
        f"{name}.dist-info/entry_points.txt": (
            "[console_scripts]\ndurumari-mcp = durumari_mcp:main\n"
        ),
    }
    wheel = directory / f"{name}-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
        archive.writestr(
            f"{name}.dist-info/RECORD",
            "".join(f"{path},,\n" for path in files) + f"{name}.dist-info/RECORD,,\n",
        )
    (directory / "requirements.txt").write_text("")
    (directory / "bundle.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "package": "durumari-mcp",
                "version": version,
                "files": {
                    item.name: hashlib.sha256(item.read_bytes()).hexdigest()
                    for item in (wheel, directory / "requirements.txt")
                },
            }
        )
    )
    return directory


def test_previous_release_without_current_modules_is_restored_and_verified(monkeypatch, tmp_path):
    if shutil.which("uv") is None:
        pytest.skip("uv is not installed; real runtime replacement tests need uv")
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(tmp_path / "bin"))
    monkeypatch.setattr(releases, "state_directory", lambda: tmp_path / "state")
    bundle = old_release_bundle(tmp_path / "old-runtime", "0.0.1")
    runtime, launcher = releases.runtime_paths()
    try:
        releases.install_runtime_bundle(bundle, "0.0.1", previous_release=True)
    except ReleaseError as error:
        if error.details.get("reason") == "dependency_resolution_failed":
            pytest.skip("uv cannot obtain Python 3.12 here")
        raise
    assert releases.installed_runtime_version(runtime) == "0.0.1"
    releases.verify_previous_runtime(runtime, launcher, "0.0.1")
    # The current-release check would reject this older runtime: rollback must
    # not use it, and a failed reinstall restores and verifies it the same way.
    with pytest.raises(
        ReleaseError, match="release_install_command_failed|runtime_version_not_verified"
    ) as caught:
        releases.install_runtime_bundle(bundle, "0.0.1")
    assert caught.value.details["runtime_rollback"] == "restored"
    releases.verify_previous_runtime(runtime, launcher, "0.0.1")
