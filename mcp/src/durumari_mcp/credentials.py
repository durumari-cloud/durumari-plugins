"""Explicit OS stores only; no environment, plaintext file, or alternate backend fallback."""

import json
import re
import sys
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from durumari_mcp.legacy import PLUGIN_SERVICE_V1

SERVICE = "DURUMARI.MCP.PluginCredential.v1"


class AdapterError(Exception):
    """Only fixed, value-free error codes may cross the stdio boundary."""


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    gateway_origin: str = Field(max_length=250)
    plugin_token: SecretStr = Field(repr=False)
    client: Literal["codex", "claude"]
    destination_id: Literal["codex-model", "claude-model", "local-tool"]

    @field_validator("gateway_origin")
    @classmethod
    def trusted_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("invalid gateway origin") from None
        if (
            value != value.strip()
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)
            or (port is not None and not 1 <= port <= 65535)
        ):
            raise ValueError("invalid gateway origin")
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}
        ):
            raise ValueError("HTTPS required except numeric loopback development")
        return value.rstrip("/")

    @field_validator("plugin_token")
    @classmethod
    def scoped_token(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"mpc_[A-Za-z0-9_-]{43}", value.get_secret_value()):
            raise ValueError("a scoped Plugin credential is required")
        return value

    def check_client(self, client: str) -> None:
        if self.client != client or self.destination_id not in {client + "-model", "local-tool"}:
            raise AdapterError("client_destination_mismatch")

    def protected_json(self) -> str:
        body = self.model_dump(mode="json")
        body["plugin_token"] = self.plugin_token.get_secret_value()
        return json.dumps(body, separators=(",", ":"))


def profile_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise AdapterError("invalid_profile_name")
    return value


def os_store() -> Any:
    # Instantiate approved implementations directly. Never load configured
    # keyring plugins or a plaintext/third-party fallback through get_keyring().
    if sys.platform == "win32":
        from keyring.backends.Windows import WinVaultKeyring

        store = WinVaultKeyring()  # type: ignore[no-untyped-call]
        store.persist = "local machine"
        return store
    if sys.platform == "darwin":
        from durumari_mcp.macos_credentials import Keyring

        return Keyring()  # type: ignore[no-untyped-call]
    if sys.platform.startswith("linux"):
        try:
            from keyring.backends.SecretService import Keyring as SecretServiceKeyring

            store = SecretServiceKeyring()  # type: ignore[no-untyped-call]
            if store.priority < 1:
                raise AdapterError("protected_store_unavailable")
            return store
        except Exception:
            raise AdapterError("protected_store_unavailable") from None
    raise AdapterError("protected_store_unavailable")


def save_profile(name: str, profile: Profile) -> None:
    name = profile_name(name)
    try:
        os_store().set_password(SERVICE, name, profile.protected_json())
    except Exception:
        raise AdapterError("protected_store_write_failed") from None


def load_profile(name: str) -> Profile:
    name = profile_name(name)
    try:
        store = os_store()
        stored = store.get_password(SERVICE, name)
        if stored is None:
            stored = store.get_password(PLUGIN_SERVICE_V1, name)
        if stored is None:
            raise AdapterError("credential_missing")
        return Profile.model_validate_json(stored)
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("protected_store_read_failed") from None


def delete_profile(name: str) -> None:
    name = profile_name(name)
    try:
        store = os_store()
        for service in (SERVICE, PLUGIN_SERVICE_V1):
            if store.get_password(service, name) is not None:
                store.delete_password(service, name)
    except Exception:
        raise AdapterError("protected_store_delete_failed") from None
