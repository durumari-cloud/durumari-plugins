"""Update Keychain data in place so token rotation preserves item identity and ACLs.

keyring's macOS set_password deletes and recreates items. Keep its read/delete
backend, but use Security.framework directly for writes. Never edit access rules
or fall back to deletion when an update is denied.
"""

import ctypes
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache

from keyring.backends.macOS import Keyring as MacOSKeyring
from keyring.errors import PasswordSetError

NOT_FOUND = -25300
DUPLICATE = -25299


class _Security:
    def __init__(self) -> None:
        self.security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
        self.cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        pointer = ctypes.c_void_p
        self.security.SecItemUpdate.argtypes = (pointer, pointer)
        self.security.SecItemUpdate.restype = ctypes.c_int32
        self.security.SecItemAdd.argtypes = (pointer, pointer)
        self.security.SecItemAdd.restype = ctypes.c_int32
        self.cf.CFStringCreateWithBytes.argtypes = (
            pointer, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32, ctypes.c_bool
        )
        self.cf.CFStringCreateWithBytes.restype = pointer
        self.cf.CFDataCreate.argtypes = (pointer, ctypes.c_char_p, ctypes.c_long)
        self.cf.CFDataCreate.restype = pointer
        self.cf.CFDictionaryCreate.argtypes = (
            pointer, pointer, pointer, ctypes.c_long, pointer, pointer
        )
        self.cf.CFDictionaryCreate.restype = pointer
        self.cf.CFRelease.argtypes = (pointer,)
        self.cf.CFRelease.restype = None
        self.key_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(self.cf, "kCFTypeDictionaryKeyCallBacks")
        )
        self.value_callbacks = ctypes.addressof(
            ctypes.c_byte.in_dll(self.cf, "kCFTypeDictionaryValueCallBacks")
        )

    def _constant(self, name: str) -> ctypes.c_void_p:
        return ctypes.c_void_p.in_dll(self.security, name)

    @contextmanager
    def _dictionary(self, values: dict[str, str | bytes | ctypes.c_void_p]) -> Iterator[int]:
        owned = []
        dictionary = None
        try:
            keys, objects = [], []
            for key, value in values.items():
                keys.append(self._constant(key))
                if isinstance(value, ctypes.c_void_p):
                    objects.append(value)
                    continue
                if isinstance(value, str):
                    data = value.encode("utf-8")
                    obj = self.cf.CFStringCreateWithBytes(None, data, len(data), 0x08000100, False)
                else:
                    obj = self.cf.CFDataCreate(None, value, len(value))
                if not obj:
                    raise MemoryError("keychain_allocation_failed")
                owned.append(obj)
                objects.append(ctypes.c_void_p(obj))
            array = ctypes.c_void_p * len(values)
            dictionary = self.cf.CFDictionaryCreate(
                None, array(*keys), array(*objects), len(values),
                self.key_callbacks, self.value_callbacks,
            )
            if not dictionary:
                raise MemoryError("keychain_allocation_failed")
            yield dictionary
        finally:
            if dictionary:
                self.cf.CFRelease(dictionary)
            for obj in owned:
                self.cf.CFRelease(obj)

    def _identity(self, service: str, account: str) -> dict[str, str | bytes | ctypes.c_void_p]:
        return {
            "kSecClass": self._constant("kSecClassGenericPassword"),
            "kSecAttrService": service,
            "kSecAttrAccount": account,
        }

    def update(self, service: str, account: str, password: str) -> int:
        with (
            self._dictionary(self._identity(service, account)) as query,
            self._dictionary({"kSecValueData": password.encode("utf-8")}) as changes,
        ):
            return int(self.security.SecItemUpdate(query, changes))

    def add(self, service: str, account: str, password: str) -> int:
        values = self._identity(service, account)
        values["kSecValueData"] = password.encode("utf-8")
        with self._dictionary(values) as query:
            return int(self.security.SecItemAdd(query, None))


@lru_cache(maxsize=1)
def _security() -> _Security:
    return _Security()


class Keyring(MacOSKeyring):
    def set_password(self, service: str, username: str | None, password: str) -> None:
        account = "" if username is None else username
        try:
            native = _security()
            status = native.update(service, account, password)
            if status == NOT_FOUND:
                status = native.add(service, account, password)
                # Another process may have created the item after our miss.
                if status == DUPLICATE:
                    status = native.update(service, account, password)
            if status != 0:
                raise PasswordSetError("keychain_write_failed")
        except Exception:
            # Native failures must not expose credential values or trigger a
            # delete/recreate fallback. Callers translate this to AdapterError.
            raise PasswordSetError("keychain_write_failed") from None
