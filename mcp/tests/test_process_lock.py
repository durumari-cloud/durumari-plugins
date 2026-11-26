"""A contended empty lock file must wait safely on every supported OS."""

import os
import sys

import pytest

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_outbox import process_lock, protected_directory


def test_contended_empty_lock_waits_then_releases(tmp_path):
    directory = tmp_path / "state"
    protected_directory(directory)
    path = directory / "process.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    if sys.platform == "win32":
        import msvcrt

        def lock():
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)

        def unlock():
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        def lock():
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)

        def unlock():
            fcntl.flock(descriptor, fcntl.LOCK_UN)

    try:
        lock()
        assert path.stat().st_size == 0
        with pytest.raises(AdapterError, match="project_profile_busy"):
            with process_lock(directory, timeout=0.05):
                pytest.fail("A sibling handle still owns the lock")
    finally:
        unlock()
        os.close(descriptor)

    with process_lock(directory, timeout=0.2):
        pass
