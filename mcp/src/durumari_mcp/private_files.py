"""Owner-only local state using POSIX modes or native Windows DACLs."""

import ctypes
import os
import stat
import sys
from ctypes import wintypes
from pathlib import Path
from typing import Any

from durumari_mcp.credentials import AdapterError


def safe_path(path: Path) -> None:
    if any(parent.is_symlink() or parent.is_junction() for parent in (path, *path.parents)):
        raise AdapterError("unsafe_private_state_path")


class WindowsAccess:
    """No shell, locale-dependent account names, credential output or ACL fallback."""

    def __init__(self) -> None:
        dll = vars(ctypes)["WinDLL"]
        self.security: Any = dll("advapi32", use_last_error=True)
        self.kernel: Any = dll("kernel32", use_last_error=True)
        pointer = ctypes.c_void_p
        out = ctypes.POINTER(pointer)
        definitions = {
            "OpenProcessToken": ([wintypes.HANDLE, wintypes.DWORD, out], wintypes.BOOL),
            "GetTokenInformation": (
                [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    pointer,
                    wintypes.DWORD,
                    ctypes.POINTER(wintypes.DWORD),
                ],
                wintypes.BOOL,
            ),
            "ConvertSidToStringSidW": ([pointer, out], wintypes.BOOL),
            "GetNamedSecurityInfoW": (
                [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, out, out, out, out, out],
                wintypes.DWORD,
            ),
            "ConvertSecurityDescriptorToStringSecurityDescriptorW": (
                [pointer, wintypes.DWORD, wintypes.DWORD, out, pointer],
                wintypes.BOOL,
            ),
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": (
                [wintypes.LPCWSTR, wintypes.DWORD, out, pointer],
                wintypes.BOOL,
            ),
            "GetSecurityDescriptorOwner": ([pointer, out, pointer], wintypes.BOOL),
            "GetSecurityDescriptorDacl": ([pointer, pointer, out, pointer], wintypes.BOOL),
            "SetNamedSecurityInfoW": (
                [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, pointer, pointer, pointer, pointer],
                wintypes.DWORD,
            ),
            "GetSecurityDescriptorControl": ([pointer, pointer, pointer], wintypes.BOOL),
            "GetAclInformation": ([pointer, pointer, wintypes.DWORD, ctypes.c_int], wintypes.BOOL),
            "GetAce": ([pointer, wintypes.DWORD, out], wintypes.BOOL),
            "IsValidSid": ([pointer], wintypes.BOOL),
        }
        for name, (arguments, result) in definitions.items():
            function = getattr(self.security, name)
            function.argtypes, function.restype = arguments, result
        self.kernel.GetCurrentProcess.restype = wintypes.HANDLE
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.kernel.LocalFree.argtypes = [pointer]
        self.kernel.LocalFree.restype = pointer

    @staticmethod
    def require(condition: Any) -> None:
        if not condition:
            raise AdapterError("private_state_access")

    def token_identities(self) -> tuple[str, str]:
        token = ctypes.c_void_p()
        self.require(
            self.security.OpenProcessToken(
                self.kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token)
            )
        )
        try:
            identities = []
            for kind in (1, 4):  # TokenUser and TokenOwner, including elevated defaults.
                size = wintypes.DWORD()
                self.security.GetTokenInformation(token, kind, None, 0, ctypes.byref(size))
                self.require(0 < size.value < 65536)
                buffer = ctypes.create_string_buffer(size.value)
                self.require(
                    self.security.GetTokenInformation(
                        token, kind, buffer, size.value, ctypes.byref(size)
                    )
                )
                sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p)).contents
                converted = ctypes.c_void_p()
                self.require(self.security.ConvertSidToStringSidW(sid, ctypes.byref(converted)))
                try:
                    identities.append(ctypes.wstring_at(converted))
                finally:
                    self.kernel.LocalFree(converted)
            return identities[0], identities[1]
        finally:
            self.kernel.CloseHandle(token)

    def descriptor(self, path: Path) -> tuple[str, str]:
        descriptor, owner = ctypes.c_void_p(), ctypes.c_void_p()
        code = self.security.GetNamedSecurityInfoW(
            str(path), 1, 5, ctypes.byref(owner), None, None, None, ctypes.byref(descriptor)
        )
        self.require(code == 0)
        converted, owner_text = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            self.require(self.security.ConvertSidToStringSidW(owner, ctypes.byref(owner_text)))
            self.require(
                self.security.ConvertSecurityDescriptorToStringSecurityDescriptorW(
                    descriptor, 1, 4, ctypes.byref(converted), None
                )
            )
            return ctypes.wstring_at(owner_text), ctypes.wstring_at(converted)
        finally:
            if owner_text.value:
                self.kernel.LocalFree(owner_text)
            if converted.value:
                self.kernel.LocalFree(converted)
            self.kernel.LocalFree(descriptor)

    def apply(self, path: Path) -> None:
        # Rewriting a directory DACL propagates to its children, so a concurrent writer's
        # freshly protected file could lose its protected flag before its own check.
        # An already compliant path is therefore never rewritten.
        try:
            self.check(path)
            return
        except AdapterError:
            pass
        user, default_owner = self.token_identities()
        current_owner, _ = self.descriptor(path)
        # Only the current operator's own token default may be normalized.
        self.require(current_owner in {user, default_owner})
        inheritance = "OICI" if path.is_dir() else ""
        wanted = f"O:{user}D:P(A;{inheritance};FA;;;{user})"
        descriptor, owner, dacl = (ctypes.c_void_p() for _ in range(3))
        present, defaulted = wintypes.BOOL(), wintypes.BOOL()
        self.require(
            self.security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                wanted, 1, ctypes.byref(descriptor), None
            )
        )
        try:
            self.require(
                self.security.GetSecurityDescriptorOwner(
                    descriptor, ctypes.byref(owner), ctypes.byref(defaulted)
                )
            )
            self.require(
                self.security.GetSecurityDescriptorDacl(
                    descriptor, ctypes.byref(present), ctypes.byref(dacl), ctypes.byref(defaulted)
                )
            )
            self.require(present.value and dacl.value)
            self.require(
                self.security.SetNamedSecurityInfoW(
                    str(path), 1, 0x80000005, owner, None, dacl, None
                )
                == 0
            )
        finally:
            self.kernel.LocalFree(descriptor)
        self.check(path)

    def check(self, path: Path) -> None:
        user, _ = self.token_identities()
        descriptor, owner, dacl = (ctypes.c_void_p() for _ in range(3))
        self.require(
            self.security.GetNamedSecurityInfoW(
                str(path),
                1,
                5,
                ctypes.byref(owner),
                None,
                ctypes.byref(dacl),
                None,
                ctypes.byref(descriptor),
            )
            == 0
        )
        converted = ctypes.c_void_p()
        try:
            self.require(self.security.ConvertSidToStringSidW(owner, ctypes.byref(converted)))
            self.require(ctypes.wstring_at(converted) == user)
            self.kernel.LocalFree(converted)
            converted = ctypes.c_void_p()
            control, revision = wintypes.WORD(), wintypes.DWORD()
            self.require(
                self.security.GetSecurityDescriptorControl(
                    descriptor, ctypes.byref(control), ctypes.byref(revision)
                )
            )
            self.require(control.value & 0x1000 and dacl.value)  # Protected, non-null DACL.
            sizes = (wintypes.DWORD * 3)()
            self.require(self.security.GetAclInformation(dacl, sizes, ctypes.sizeof(sizes), 2))
            self.require(sizes[0] == 1)
            ace = ctypes.c_void_p()
            self.require(self.security.GetAce(dacl, 0, ctypes.byref(ace)))
            address = ace.value
            if address is None:
                raise AdapterError("private_state_access")
            self.require(ctypes.c_ubyte.from_address(address).value == 0)
            self.require(ctypes.c_ushort.from_address(address + 2).value >= 16)
            self.require(
                ctypes.c_ubyte.from_address(address + 1).value == (3 if path.is_dir() else 0)
            )
            self.require(wintypes.DWORD.from_address(address + 4).value == 0x1F01FF)
            sid = ctypes.c_void_p(address + 8)
            self.require(self.security.IsValidSid(sid))
            self.require(self.security.ConvertSidToStringSidW(sid, ctypes.byref(converted)))
            self.require(ctypes.wstring_at(converted) == user)
        finally:
            if converted.value:
                self.kernel.LocalFree(converted)
            self.kernel.LocalFree(descriptor)


def protect(path: Path, *, directory: bool) -> None:
    safe_path(path)
    info = path.stat()
    if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
        raise AdapterError("unsafe_private_state_path")
    if sys.platform == "win32":
        WindowsAccess().apply(path)
    else:
        if info.st_uid != os.getuid():
            raise AdapterError("private_state_access")
        path.chmod(0o700 if directory else 0o600)


def protect_directory(path: Path) -> None:
    safe_path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    protect(path, directory=True)


def protect_file(path: Path) -> None:
    protect(path, directory=False)


def check_private_file(path: Path) -> None:
    safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise AdapterError("unsafe_private_state_path")
    if sys.platform == "win32":
        WindowsAccess().check(path)
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AdapterError("private_state_access")
