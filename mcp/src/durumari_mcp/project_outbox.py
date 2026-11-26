"""Authenticated encrypted outbox, atomic writes and cross-process locking."""

import os
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from durumari_mcp.credentials import AdapterError
from durumari_mcp.legacy import data_directory
from durumari_mcp.private_files import protect_directory


def state_root() -> Path:
    # Only encrypted state is stored here; keys and credentials remain in OS storage.
    return data_directory(Path.home()) / "project-sync"


def protected_directory(path: Path) -> None:
    for parent in [*reversed(path.parents), path]:
        if parent.is_symlink():
            raise AdapterError("unsafe_outbox_path")
    protect_directory(path)


@contextmanager
def process_lock(directory: Path, *, timeout: float = 5) -> Iterator[None]:
    protected_directory(directory)
    lock = directory / "process.lock"
    if lock.is_symlink():
        raise AdapterError("unsafe_outbox_path")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock, flags, 0o600)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        if sys.platform == "win32":
            import msvcrt

            windows_lock: Any = msvcrt

            # Windows permits byte locks beyond EOF. Writing a marker before
            # locking races with another handle that already owns that byte.
            while True:
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    windows_lock.locking(fd, windows_lock.LK_NBLCK, 1)
                    acquired = True
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AdapterError("project_profile_busy") from None
                    time.sleep(0.02)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise AdapterError("project_profile_busy") from None
                    time.sleep(0.02)
        yield
    finally:
        if acquired:
            if sys.platform == "win32":
                os.lseek(fd, 0, os.SEEK_SET)
                windows_lock.locking(fd, windows_lock.LK_UNLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


