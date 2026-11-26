"""Private state uses the host OS access-control model, including real Windows DACLs."""

import subprocess
import sys

import pytest

from durumari_mcp.credentials import AdapterError
from durumari_mcp.private_files import check_private_file, protect_directory, protect_file


def test_private_directory_and_file_are_checked_with_the_actual_host_acl(tmp_path):
    directory = tmp_path / "private"
    protect_directory(directory)
    path = directory / "state"
    path.write_bytes(b"")
    protect_file(path)
    path.write_bytes(b"synthetic private state")
    check_private_file(path)
    if sys.platform != "win32":
        assert directory.stat().st_mode & 0o777 == 0o700
        assert path.stat().st_mode & 0o777 == 0o600


def test_broad_file_access_is_rejected_and_can_be_protected(tmp_path):
    directory = tmp_path / "private"
    protect_directory(directory)
    path = directory / "state"
    path.write_bytes(b"synthetic")
    protect_file(path)
    if sys.platform == "win32":
        subprocess.run(
            ["icacls.exe", str(path), "/grant", "*S-1-1-0:(R)"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        path.chmod(0o644)
    with pytest.raises(AdapterError, match="private_state_access"):
        check_private_file(path)
    protect_file(path)
    check_private_file(path)


def test_linked_state_cannot_change_the_target_permissions(tmp_path):
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(actual, target_is_directory=True)
    before = actual.stat().st_mode
    with pytest.raises(AdapterError, match="unsafe_private_state_path"):
        protect_directory(link / "state")
    assert actual.stat().st_mode == before
    assert not (actual / "state").exists()


def test_compliant_windows_directory_is_not_rewritten_under_concurrent_writers(
    tmp_path, monkeypatch
):
    # Rewriting a directory DACL propagates to its children and could clear the
    # protected flag of a file another writer just protected.
    from durumari_mcp import private_files

    directory = tmp_path / "private"
    protect_directory(directory)
    path = directory / "state"
    path.write_bytes(b"synthetic")
    protect_file(path)
    if sys.platform != "win32":
        # Exercise the Windows early-return contract without loading native DLLs.
        # The Windows run below also checks the real descriptor and records writes.
        access = private_files.WindowsAccess.__new__(private_files.WindowsAccess)
        checked = []

        def already_private(target):
            assert target.stat().st_mode & 0o077 == 0
            checked.append(target)

        monkeypatch.setattr(access, "check", already_private)
        access.apply(directory)
        access.apply(path)
        assert checked == [directory, path]
        check_private_file(path)
        return
    writes = []
    original = private_files.WindowsAccess.__init__

    class Recording:
        def __init__(self, security):
            self.security = security

        def __getattr__(self, name):
            if name == "SetNamedSecurityInfoW":
                writes.append(name)
            return getattr(self.security, name)

    def recording_init(self):
        original(self)
        self.security = Recording(self.security)

    monkeypatch.setattr(private_files.WindowsAccess, "__init__", recording_init)
    protect_directory(directory)
    protect_file(path)
    assert writes == []
    check_private_file(path)
