"""Per-user account grants and pending proofs live only in approved OS stores."""

import json
import re
from datetime import UTC, datetime
from typing import Literal

from cryptography.fernet import Fernet
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    field_validator,
    model_validator,
)

from durumari_mcp.credentials import AdapterError, Profile, os_store, profile_name

ACCOUNT_SERVICE = "DURUMARI.MCP.AccountConnection.v1"
ENROLLMENT_SERVICE = "DURUMARI.MCP.AccountEnrollment.v1"
CLIENT_FAMILIES = {
    "codex": "openai",
    "claude-code": "anthropic",
    "cursor": "cursor",
    "mcp": "local",
    "gemini": "google",
    "opencode": "opencode",
}
UUID_PATTERN = r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$"


class AccountConnection(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True, hide_input_in_errors=True)
    id: str = Field(pattern=UUID_PATTERN)
    actor_id: str = Field(pattern=UUID_PATTERN)
    client: str
    provider_family: str
    scope_kind: Literal["account"]
    expires_at: AwareDatetime


class AccountProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    gateway_origin: str
    connection_id: str = Field(pattern=UUID_PATTERN)
    actor_id: str = Field(pattern=UUID_PATTERN)
    client: str
    provider_family: str
    scope_kind: Literal["account"] = "account"
    expires_at: AwareDatetime
    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr = Field(repr=False)
    access_expires_at: AwareDatetime
    outbox_key: SecretStr = Field(repr=False)
    refresh_uncertain: bool = Field(default=False, strict=True)

    @field_validator("gateway_origin")
    @classmethod
    def origin(cls, value: str) -> str:
        return Profile.trusted_origin(value)

    @field_validator("client")
    @classmethod
    def known_client(cls, value: str) -> str:
        if value not in CLIENT_FAMILIES:
            raise ValueError("invalid account client")
        return value

    @field_validator("access_token", "refresh_token")
    @classmethod
    def scoped_token(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"mp[ar]_[A-Za-z0-9_-]{43}", value.get_secret_value()):
            raise ValueError("invalid account token")
        return value

    @field_validator("outbox_key")
    @classmethod
    def encryption_key(cls, value: SecretStr) -> SecretStr:
        Fernet(value.get_secret_value().encode("ascii"))
        return value

    def check_client(self, client: str) -> None:
        if client != self.client or CLIENT_FAMILIES.get(client) != self.provider_family:
            raise AdapterError("account_client_mismatch")
        if not self.access_token.get_secret_value().startswith("mpa_") or not (
            self.refresh_token.get_secret_value().startswith("mpr_")
        ):
            raise AdapterError("invalid_account_credential")

    def protected_json(self) -> str:
        value = self.model_dump(mode="json")
        for key in ("access_token", "refresh_token", "outbox_key"):
            value[key] = getattr(self, key).get_secret_value()
        return json.dumps(value, separators=(",", ":"))


def profile_from_response(
    value: dict[str, object],
    origin: str,
    client: str,
    outbox_key: str,
    *,
    expected: AccountProfile | None = None,
) -> AccountProfile:
    try:
        connection = AccountConnection.model_validate(value["connection"])
        tokens = value["tokens"]
        if not isinstance(tokens, dict) or set(tokens) != {
            "access_token",
            "refresh_token",
            "access_expires_at",
        }:
            raise ValueError
        profile = AccountProfile.model_validate(
            {
                "gateway_origin": origin,
                "connection_id": connection.id,
                "actor_id": connection.actor_id,
                "client": connection.client,
                "provider_family": connection.provider_family,
                "scope_kind": connection.scope_kind,
                "expires_at": connection.expires_at,
                "outbox_key": outbox_key,
                **tokens,
            }
        )
        profile.check_client(client)
        if expected is not None and (
            profile.gateway_origin,
            profile.connection_id,
            profile.actor_id,
            profile.client,
            profile.provider_family,
        ) != (
            expected.gateway_origin,
            expected.connection_id,
            expected.actor_id,
            expected.client,
            expected.provider_family,
        ):
            raise ValueError
        if profile.access_expires_at <= datetime.now(UTC) or profile.expires_at <= datetime.now(
            UTC
        ):
            raise ValueError
        return profile
    except Exception:
        raise AdapterError("invalid_account_response") from None


class PendingEnrollment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    gateway_origin: str
    client: str
    device_code: SecretStr = Field(repr=False)
    request_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    request_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    expires_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def complete_metadata(self) -> "PendingEnrollment":
        if (self.request_id is None) != (self.expires_at is None):
            raise ValueError("incomplete enrollment metadata")
        return self

    @field_validator("gateway_origin")
    @classmethod
    def origin(cls, value: str) -> str:
        return Profile.trusted_origin(value)

    @field_validator("client")
    @classmethod
    def known_client(cls, value: str) -> str:
        return AccountProfile.known_client(value)

    @field_validator("device_code")
    @classmethod
    def device_proof(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"mcd_[A-Za-z0-9_-]{43}", value.get_secret_value()):
            raise ValueError("invalid enrollment proof")
        return value

    def protected_json(self) -> str:
        value = self.model_dump(mode="json")
        value["device_code"] = self.device_code.get_secret_value()
        return json.dumps(value, separators=(",", ":"))


def save_account_profile(name: str, profile: AccountProfile) -> None:
    name = profile_name(name)
    profile.check_client(profile.client)
    try:
        store = os_store()
        value = profile.protected_json()
        store.set_password(ACCOUNT_SERVICE, name, value)
        if store.get_password(ACCOUNT_SERVICE, name) != value:
            raise ValueError
    except Exception:
        raise AdapterError("protected_account_store_write_failed") from None


def load_account_profile(name: str) -> AccountProfile:
    name = profile_name(name)
    try:
        value = os_store().get_password(ACCOUNT_SERVICE, name)
        if value is None:
            raise AdapterError("account_credential_missing")
        result = AccountProfile.model_validate_json(value)
        result.check_client(result.client)
        return result
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("protected_account_store_read_failed") from None


def save_pending(name: str, pending: PendingEnrollment) -> None:
    name = profile_name(name)
    try:
        store = os_store()
        value = pending.protected_json()
        store.set_password(ENROLLMENT_SERVICE, name, value)
        if store.get_password(ENROLLMENT_SERVICE, name) != value:
            raise ValueError
    except Exception:
        raise AdapterError("protected_account_store_write_failed") from None


def load_pending(name: str) -> PendingEnrollment | None:
    name = profile_name(name)
    try:
        value = os_store().get_password(ENROLLMENT_SERVICE, name)
        return PendingEnrollment.model_validate_json(value) if value is not None else None
    except Exception:
        raise AdapterError("protected_account_store_read_failed") from None


def delete_pending(name: str) -> None:
    name = profile_name(name)
    try:
        store = os_store()
        if store.get_password(ENROLLMENT_SERVICE, name) is not None:
            store.delete_password(ENROLLMENT_SERVICE, name)
    except Exception:
        raise AdapterError("protected_account_store_delete_failed") from None
