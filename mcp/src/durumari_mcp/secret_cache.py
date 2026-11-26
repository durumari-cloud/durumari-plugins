"""Agent device keys and the local secret cache, kept only in the OS credential store.

macOS Keychain, Windows Credential Manager and Linux Secret Service through
`credentials.os_store()`; nothing is written to disk in plaintext and there is no
fallback store. Each entry is bound to one repository connection. Windows limits one
credential to 2,560 bytes, so longer entries are split into numbered parts and
reassembled only when the digest matches.
"""

import base64
import hashlib
import json
import time
from typing import Any

from cryptography.hazmat.primitives.asymmetric import ed25519, x25519

from durumari_mcp.credentials import AdapterError, os_store

DEVICE = "DURUMARI.AgentSecretDevice.v1"
CACHE = "DURUMARI.AgentSecretCache.v1"
# UTF-16 on Windows: 1,000 characters stay under the 2,560-byte credential blob limit.
PART = 1000
MAX_PARTS = 600
MAX_CACHE_SECONDS = 7 * 86_400


def digest(*parts: str) -> str:
    return hashlib.sha256(":".join(parts).encode()).hexdigest()


def put_blob(store: Any, service: str, account: str, text: str) -> None:
    parts = [text[i : i + PART] for i in range(0, len(text), PART)] or [""]
    if len(parts) > MAX_PARTS:
        raise AdapterError("secret_cache_entry_too_large")
    old = head(store, service, account)
    for index, part in enumerate(parts):
        store.set_password(service, f"{account}#{index}", part)
    store.set_password(
        service,
        account,
        json.dumps({"parts": len(parts), "sha256": hashlib.sha256(text.encode()).hexdigest()}),
    )
    for index in range(len(parts), old.get("parts", 0) if old else 0):
        drop(store, service, f"{account}#{index}")


def head(store: Any, service: str, account: str) -> dict[str, Any] | None:
    raw = store.get_password(service, account)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("parts"), int)
            or not 1 <= value["parts"] <= MAX_PARTS
            or not isinstance(value.get("sha256"), str)
        ):
            raise ValueError
        return value
    except ValueError:
        return {"parts": 0, "sha256": ""}


def get_blob(store: Any, service: str, account: str) -> str | None:
    meta = head(store, service, account)
    if meta is None or not meta["parts"]:
        return None
    pieces = []
    for index in range(meta["parts"]):
        piece = store.get_password(service, f"{account}#{index}")
        if piece is None:
            return None
        pieces.append(piece)
    text = "".join(pieces)
    # A partial or interrupted write reads as a miss, never as a different value.
    if hashlib.sha256(text.encode()).hexdigest() != meta["sha256"]:
        return None
    return text


def drop(store: Any, service: str, account: str) -> None:
    try:
        if store.get_password(service, account) is not None:
            store.delete_password(service, account)
    except Exception:
        raise AdapterError("secret_cache_unavailable") from None


def delete_blob(store: Any, service: str, account: str) -> None:
    meta = head(store, service, account)
    for index in range(meta["parts"] if meta else 0):
        drop(store, service, f"{account}#{index}")
    drop(store, service, account)


class DeviceKeys:
    """The connection's Ed25519 signing and X25519 encryption keys."""

    def __init__(self, data: dict[str, str]) -> None:
        self.data = data

    @property
    def device_id(self) -> str | None:
        return self.data.get("device_id") or None

    def signing(self) -> ed25519.Ed25519PrivateKey:
        return ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(self.data["signing_private"], validate=True)
        )

    def encryption(self) -> x25519.X25519PrivateKey:
        return x25519.X25519PrivateKey.from_private_bytes(
            base64.b64decode(self.data["encryption_private"], validate=True)
        )

    def public(self) -> dict[str, str]:
        return {
            "signing_key": base64.b64encode(
                self.signing().public_key().public_bytes_raw()
            ).decode(),
            "encryption_key": base64.b64encode(
                self.encryption().public_key().public_bytes_raw()
            ).decode(),
        }


class SecretCache:
    """Values delivered to one connection, pinned to version, grant revision and target."""

    def __init__(self, connection_id: str, project_id: str, *, store: Any = None) -> None:
        self.connection_id, self.project_id = connection_id, project_id
        try:
            self.store = store if store is not None else os_store()
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("protected_store_unavailable") from None

    # Device keys -----------------------------------------------------------------------

    def device_keys(self, *, create: bool) -> DeviceKeys | None:
        account = digest(self.connection_id)
        try:
            raw = self.store.get_password(DEVICE, account)
        except Exception:
            raise AdapterError("secret_cache_unavailable") from None
        if raw:
            try:
                data = json.loads(raw)
                if data["connection_id"] != self.connection_id:
                    raise ValueError
                keys = DeviceKeys(data)
                keys.public()
                return keys
            except (ValueError, KeyError, TypeError):
                raise AdapterError("invalid_device_store") from None
        if not create:
            return None
        data = {
            "connection_id": self.connection_id,
            "signing_private": base64.b64encode(
                ed25519.Ed25519PrivateKey.generate().private_bytes_raw()
            ).decode(),
            "encryption_private": base64.b64encode(
                x25519.X25519PrivateKey.generate().private_bytes_raw()
            ).decode(),
            "device_id": "",
        }
        self.save_keys(data)
        return DeviceKeys(data)

    def save_keys(self, data: dict[str, str]) -> None:
        try:
            self.store.set_password(DEVICE, digest(self.connection_id), json.dumps(data))
        except Exception:
            raise AdapterError("secret_cache_unavailable") from None

    # Cached values ---------------------------------------------------------------------

    def _index_account(self) -> str:
        return "index:" + digest(self.connection_id)

    def _entry_account(self, secret_id: str) -> str:
        return "value:" + digest(self.connection_id, secret_id)

    def index(self) -> dict[str, dict[str, Any]]:
        """Value-free map of secret ID to label, versions and expiry for this connection."""
        try:
            raw = get_blob(self.store, CACHE, self._index_account())
        except Exception:
            raise AdapterError("secret_cache_unavailable") from None
        if not raw:
            return {}
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or value.get("connection_id") != self.connection_id:
                return {}
            entries = value.get("entries")
            return entries if isinstance(entries, dict) else {}
        except ValueError:
            return {}

    def _save_index(self, entries: dict[str, dict[str, Any]]) -> None:
        text = json.dumps({"connection_id": self.connection_id, "entries": entries})
        try:
            if entries:
                put_blob(self.store, CACHE, self._index_account(), text)
            else:
                delete_blob(self.store, CACHE, self._index_account())
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("secret_cache_unavailable") from None

    def get(self, secret_id: str) -> dict[str, Any] | None:
        """A current, unexpired entry or None; expired entries are removed."""
        meta = self.index().get(secret_id)
        if meta is None:
            return None
        if meta.get("expires_at", 0) <= time.time():
            self.forget(secret_id)
            return None
        try:
            raw = get_blob(self.store, CACHE, self._entry_account(secret_id))
        except Exception:
            raise AdapterError("secret_cache_unavailable") from None
        if raw is None:
            return None
        try:
            entry = json.loads(raw)
            if (
                entry.get("connection_id") != self.connection_id
                or entry.get("project_id") != self.project_id
                or entry.get("secret_id") != secret_id
                or entry.get("revision") != meta.get("revision")
                or entry.get("policy_revision") != meta.get("policy_revision")
                or not isinstance(entry.get("value"), str)
            ):
                raise ValueError
        except (ValueError, AttributeError):
            self.forget(secret_id)
            return None
        entry["targets"] = list(meta.get("targets", []))
        entry["expires_at"] = meta["expires_at"]
        return dict(entry)

    def put(self, entry: dict[str, Any], target: str, cache_seconds: int) -> None:
        seconds = max(0, min(int(cache_seconds), MAX_CACHE_SECONDS))
        secret_id = entry["secret_id"]
        entries = self.index()
        previous = entries.get(secret_id) or {}
        same = (previous.get("revision"), previous.get("policy_revision")) == (
            entry["revision"],
            entry["policy_revision"],
        )
        targets = sorted(set(previous.get("targets", []) if same else []) | {target})
        if seconds == 0:
            self.forget(secret_id)
            return
        body = {
            "connection_id": self.connection_id,
            "project_id": self.project_id,
            "secret_id": secret_id,
            "label": entry["label"],
            "revision": entry["revision"],
            "policy_revision": entry["policy_revision"],
            "value": entry["value"],
        }
        try:
            put_blob(self.store, CACHE, self._entry_account(secret_id), json.dumps(body))
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("secret_cache_unavailable") from None
        finally:
            body.clear()
        index_entry: dict[str, Any] = {
            "label": entry["label"],
            "revision": entry["revision"],
            "policy_revision": entry["policy_revision"],
            "targets": targets,
            "expires_at": int(time.time()) + seconds,
        }
        if isinstance(entry.get("tier"), str):
            index_entry["tier"] = entry["tier"]
        if isinstance(entry.get("env_allowed"), bool):
            index_entry["env_allowed"] = entry["env_allowed"]
        entries[secret_id] = index_entry
        self._save_index(entries)

    def forget(self, secret_id: str) -> None:
        entries = self.index()
        try:
            delete_blob(self.store, CACHE, self._entry_account(secret_id))
        except AdapterError:
            raise
        except Exception:
            raise AdapterError("secret_cache_unavailable") from None
        if entries.pop(secret_id, None) is not None:
            self._save_index(entries)

    def clear(self) -> int:
        entries = self.index()
        for secret_id in list(entries):
            self.forget(secret_id)
        return len(entries)
