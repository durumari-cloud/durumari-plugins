"""An update stopped from outside does not block the next one."""

import json
import os
import sys
import time
from argparse import Namespace
from datetime import UTC, datetime, timedelta

import pytest
from test_releases import installation

from durumari_mcp import releases
from durumari_mcp.releases import ReleaseError

GONE = 2**22 + 7  # no such process


def held(lock, pid):
    lock.mkdir(parents=True)
    (lock / releases.LOCK_OWNER).write_text(json.dumps({"pid": pid}), encoding="utf-8")


def quarantined(directory):
    kept = directory / "quarantine"
    return sorted(path.name.rsplit("-", 2)[0] for path in kept.iterdir()) if kept.exists() else []


@pytest.fixture
def stopped(monkeypatch):
    monkeypatch.setattr(releases, "process_alive", lambda pid, since=None: pid != GONE)


def test_a_lock_left_by_an_exited_runner_is_moved_aside(monkeypatch, tmp_path, stopped):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    held(directory / "update.lock", GONE)
    assert releases.execute_release(args)["installed"] == "0.2.0"
    assert quarantined(directory) == ["update.lock"]
    assert not (directory / "update.lock").exists()


def test_a_lock_whose_owner_still_runs_is_respected(monkeypatch, tmp_path, stopped):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    held(directory / "update.lock", os.getpid())
    with pytest.raises(ReleaseError, match="^release_update_already_running$"):
        releases.execute_release(args)
    assert (directory / "update.lock").exists() and quarantined(directory) == []


def test_a_lock_without_an_owner_waits_out_its_creation(monkeypatch, tmp_path, stopped):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    (directory / "update.lock").mkdir(parents=True)
    with pytest.raises(ReleaseError, match="already_running"):
        releases.execute_release(args)
    old = time.time() - releases.LOCK_OWNER_GRACE_SECONDS - 5
    os.utime(directory / "update.lock", (old, old))
    assert releases.execute_release(args)["installed"] == "0.2.0"
    assert quarantined(directory) == ["update.lock"]


def test_an_unreadable_owner_record_counts_as_held(monkeypatch, tmp_path, stopped):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    (directory / "update.lock").mkdir(parents=True)
    (directory / "update.lock" / releases.LOCK_OWNER).write_text("{", encoding="utf-8")
    with pytest.raises(ReleaseError, match="already_running"):
        releases.execute_release(args)


def test_a_half_switched_catalog_is_restored_and_the_update_finishes(
    monkeypatch, tmp_path, stopped
):
    args = installation(monkeypatch, tmp_path)
    releases.execute_release(args)
    directory = releases.state_directory()
    before = (directory / "installation.json").read_bytes()
    # The 10/27 state: the old catalog moved to `previous`, a new one active, the runner gone.
    (directory / "catalog").rename(directory / "previous")
    (directory / "catalog").mkdir()
    (directory / "catalog" / "half").write_text("partly installed", encoding="utf-8")
    held(directory / "update.lock", GONE)
    installs = []
    monkeypatch.setattr(releases, "install_runtime", lambda *args, **kwargs: installs.append(1))
    assert releases.execute_release(args)["installed"] == "0.2.0"
    assert installs == [1]
    assert not (directory / "previous").exists()
    assert quarantined(directory) == ["catalog", "update.lock"]
    kept = next((directory / "quarantine").glob("catalog-*"))
    assert (kept / "half").read_text(encoding="utf-8") == "partly installed"
    assert (
        json.loads((directory / "installation.json").read_bytes())["version"]
        == json.loads(before)["version"]
    )


def test_runner_leaves_the_callers_job_and_logs_its_output(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    calls = []

    def popen(command, **kwargs):
        calls.append(kwargs)
        return Namespace(pid=4242)

    monkeypatch.setattr(releases.subprocess, "Popen", popen)
    releases.start_windows_update(args)
    flags = calls[0]["creationflags"]
    assert flags & releases.CREATE_BREAKAWAY_FROM_JOB
    assert calls[0]["stdout"] is calls[0]["stderr"]
    assert calls[0]["stdout"].name.endswith(releases.RUNNER_OUTPUT)
    owner = releases.state_directory() / "launch.lock" / releases.LOCK_OWNER
    assert json.loads(owner.read_text(encoding="utf-8"))["pid"] == 4242
    assert "start runner" in (releases.state_directory() / releases.RUNNER_LOG).read_text(
        encoding="utf-8"
    )


def test_a_job_that_forbids_breakaway_still_starts_the_runner(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    flags = []

    def popen(command, **kwargs):
        flags.append(kwargs["creationflags"])
        if kwargs["creationflags"] & releases.CREATE_BREAKAWAY_FROM_JOB:
            raise PermissionError("synthetic job without breakaway")
        return Namespace(pid=4242)

    monkeypatch.setattr(releases.subprocess, "Popen", popen)
    assert releases.start_windows_update(args)["status"] == "started"
    plain = releases.RUNNER_FLAGS
    assert flags == [plain | releases.CREATE_BREAKAWAY_FROM_JOB, plain]


def test_a_launch_lock_from_a_killed_runner_does_not_block_the_next_update(
    monkeypatch, tmp_path, stopped
):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    held(directory / "launch.lock", GONE)
    monkeypatch.setattr(releases.subprocess, "Popen", lambda command, **kwargs: Namespace(pid=1))
    assert releases.start_windows_update(args)["status"] == "started"
    assert quarantined(directory) == ["launch.lock"]


def test_this_process_is_alive_and_an_unused_pid_is_not():
    assert releases.process_alive(os.getpid())
    assert not releases.process_alive(GONE)
    assert not releases.process_alive(0)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows reuses pids quickly")
def test_a_pid_reused_by_a_later_process_does_not_hold_the_lock(monkeypatch, tmp_path):
    # The runner recorded this lock before the process now holding its pid started.
    before = datetime.now(UTC) - timedelta(days=30)
    assert not releases.process_alive(os.getpid(), before)
    assert releases.process_alive(os.getpid(), datetime.now(UTC))
    lock = tmp_path / "update.lock"
    lock.mkdir()
    (lock / releases.LOCK_OWNER).write_text(
        json.dumps({"pid": os.getpid(), "at": before.isoformat()}), encoding="utf-8"
    )
    assert releases.lock_abandoned(lock)
    releases.record_lock_owner(lock, os.getpid())
    assert not releases.lock_abandoned(lock)


def test_a_run_that_waited_for_another_first_install_keeps_its_catalog(monkeypatch, tmp_path):
    # Two first installs: this run read no installation, then another finished first.
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    take, raced = releases.take_lock, []

    def after_the_other(lock, owner, busy):
        if not raced:
            raced.append("other")
            releases.execute_release(args)
            raced.append("done")
        take(lock, owner, busy)

    def install_runtime(catalog, version, previous_release=False):
        # This run's new runtime fails once the other run has installed; its rollback works.
        if "done" in raced and not previous_release:
            raise ReleaseError("runtime_install_failed")

    monkeypatch.setattr(releases, "take_lock", after_the_other)
    monkeypatch.setattr(releases, "install_runtime", install_runtime)
    with pytest.raises(ReleaseError):
        releases.execute_release(args)
    # The other run's catalog and record survive this run's failure.
    assert (directory / "catalog").exists() and quarantined(directory) == []
    assert json.loads((directory / "installation.json").read_text())["version"] == "0.2.0"


def test_status_reports_a_runner_that_was_stopped_before_recording_an_outcome(
    monkeypatch, tmp_path, stopped
):
    installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir(parents=True)
    releases.write_update_result({"status": "started"})
    held(directory / "launch.lock", os.getpid())
    assert releases.update_status() == {"status": "started"}
    (directory / "launch.lock" / releases.LOCK_OWNER).write_text(
        json.dumps({"pid": GONE}), encoding="utf-8"
    )
    assert releases.update_status() == {"status": "failed", "error": "release_runner_stopped"}
    releases.shutil.rmtree(directory / "launch.lock")
    assert releases.update_status()["error"] == "release_runner_stopped"


def test_a_lock_another_run_replaced_meanwhile_is_put_back(monkeypatch, tmp_path, stopped):
    """Two runs judged the same lock abandoned; the second must not move the first's new lock."""
    installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    lock = directory / "update.lock"
    held(lock, GONE)
    seen = releases.lock_seen
    calls = []

    def swapped(path):
        result = seen(path)
        calls.append(path.name)
        if len(calls) == 1:
            # The other run moves the abandoned lock aside and takes a fresh one now.
            releases.set_aside(lock)
            releases.claim_lock(lock, os.getpid())
        return result

    monkeypatch.setattr(releases, "lock_seen", swapped)
    with pytest.raises(ReleaseError, match="^release_update_already_running$"):
        releases.take_lock(lock, os.getpid(), "release_update_already_running")
    owner = json.loads((lock / releases.LOCK_OWNER).read_text(encoding="utf-8"))
    assert owner["pid"] == os.getpid()
    assert quarantined(directory) == ["update.lock"]
    assert not list(directory.glob("update.lock.moving-*"))


def test_a_failed_owner_record_after_start_does_not_undo_a_running_runner(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    monkeypatch.setattr(releases.subprocess, "Popen", lambda command, **kwargs: Namespace(pid=7))
    real = releases.record_lock_owner

    def busy(lock, owner):
        if owner == 7:
            raise PermissionError("synthetic: the runner is writing its own record")
        return real(lock, owner)

    monkeypatch.setattr(releases, "record_lock_owner", busy)
    assert releases.start_windows_update(args)["status"] == "started"
    assert (releases.state_directory() / "launch.lock").exists()
    assert releases.update_status()["status"] == "started"


def test_a_run_removes_only_a_lock_it_still_holds(tmp_path):
    lock = tmp_path / "update.lock"
    held(lock, GONE)
    releases.release_lock(lock, {os.getpid()})
    assert lock.exists()
    (lock / releases.LOCK_OWNER).write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    releases.release_lock(lock, {os.getpid()})
    assert not lock.exists()


def test_the_runner_removes_a_launch_lock_recorded_for_its_uv_parent(monkeypatch, tmp_path):
    args = installation(monkeypatch, tmp_path)
    directory = releases.state_directory()
    directory.mkdir(parents=True)
    held(directory / "launch.lock", os.getppid())
    # The CLI recorded the uv process after the runner recorded itself.
    monkeypatch.setattr(releases, "record_lock_owner", lambda lock, owner: None)
    monkeypatch.setattr(releases, "wait_for_parent", lambda pid: None)
    monkeypatch.setattr(releases, "wait_for_runtime_release", lambda: None)
    args.wait_for_pid = 0
    assert releases.run_independent_update(args) == 0
    assert not (directory / "launch.lock").exists()


def runtime_bundle(monkeypatch, tmp_path):
    """install_runtime_bundle with uv and the runtime replaced; it stops at the first uv call."""
    owned = tmp_path / "owned"
    owned.mkdir()
    monkeypatch.setattr(releases, "verify_runtime", lambda directory, version: (None, None))
    monkeypatch.setattr(
        releases, "runtime_paths", lambda: (tmp_path / "runtime", tmp_path / "launcher")
    )
    monkeypatch.setattr(releases, "preflight_runtime", lambda: None)
    monkeypatch.setattr(releases, "runtime_update_directory", lambda runtime: owned)
    monkeypatch.setattr(releases, "legacy_runtime_owns_launcher", lambda: False)

    def uv(*args, **kwargs):
        raise ReleaseError("uv_reached")

    monkeypatch.setattr(releases, "run", uv)
    return owned


def test_a_runtime_half_replaced_by_a_stopped_run_still_stops_for_recovery(
    monkeypatch, tmp_path, stopped
):
    owned = runtime_bundle(monkeypatch, tmp_path)
    held(owned / "update.lock", GONE)
    kept = owned / "snapshots" / "stopped-run"
    (kept / "environment").mkdir(parents=True)
    with pytest.raises(ReleaseError, match="^runtime_update_already_running_or_recovery_required$"):
        releases.install_runtime_bundle(tmp_path, "0.2.0")
    assert (owned / "update.lock" / releases.LOCK_OWNER).exists()
    assert (kept / "environment").exists()


def test_a_runtime_lock_left_before_any_copy_is_moved_aside(monkeypatch, tmp_path, stopped):
    owned = runtime_bundle(monkeypatch, tmp_path)
    held(owned / "update.lock", GONE)
    (owned / "snapshots" / "stopped-run").mkdir(parents=True)
    with pytest.raises(ReleaseError):
        releases.install_runtime_bundle(tmp_path, "0.2.0")
    assert quarantined(owned) == ["update.lock"]


def test_a_lock_whose_owner_cannot_be_recorded_is_not_left_behind(monkeypatch, tmp_path):
    def full(lock, owner):
        raise OSError("disk full")

    monkeypatch.setattr(releases, "record_lock_owner", full)
    with pytest.raises(OSError):
        releases.claim_lock(tmp_path / "update.lock", os.getpid())
    assert not (tmp_path / "update.lock").exists()
