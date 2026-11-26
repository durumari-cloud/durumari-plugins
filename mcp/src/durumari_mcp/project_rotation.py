"""Recoverable native rotation using a durable encrypted credential journal."""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, field_validator

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import ProjectProfile

if TYPE_CHECKING:
    from durumari_mcp.project_gateway import ProjectTransport


class RotationJournal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    version: int = Field(default=2, ge=2, le=2, strict=True)
    connection_id: str
    refresh_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    attempt_id: str = Field(pattern=r"^[A-Za-z0-9_-]{43}$", repr=False)
    prepared_json: SecretStr | None = Field(default=None, repr=False)


class PreparedTokens(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    connection: dict[str, Any]
    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr = Field(repr=False)
    access_expires_at: AwareDatetime

    @field_validator("access_token", "refresh_token")
    @classmethod
    def scoped_token(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"mp[ar]_[A-Za-z0-9_-]{43}", value.get_secret_value()):
            raise ValueError("scoped token required")
        return value


class CommitReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    status: Literal["committed", "reprepare"]
    connection: dict[str, Any]
    access_expires_at: AwareDatetime


def fingerprint(profile: ProjectProfile) -> str:
    return hashlib.sha256(profile.refresh_token.get_secret_value().encode()).hexdigest()


def write_journal(path: Path, profile: ProjectProfile, value: RotationJournal) -> None:
    # Raw credential JSON exists only inside the authenticated encryption envelope.
    payload = value.model_dump(mode="json")
    payload["prepared_json"] = (
        value.prepared_json.get_secret_value() if value.prepared_json is not None else None
    )
    ciphertext = Fernet(profile.outbox_key.get_secret_value().encode()).encrypt(
        json.dumps(payload).encode()
    )
    if len(ciphertext) > 4096:
        raise AdapterError("project_refresh_journal_invalid")
    temporary = path.parent / ("refresh." + uuid4().hex + ".tmp")
    try:
        if path.is_symlink():
            raise AdapterError("project_refresh_journal_invalid")
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(ciphertext)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    except OSError:
        raise AdapterError("project_refresh_journal_write_failed") from None
    finally:
        temporary.unlink(missing_ok=True)


def read_journal(path: Path, profile: ProjectProfile) -> RotationJournal:
    try:
        if path.is_symlink() or path.stat().st_size > 4096:
            raise ValueError
        plaintext = Fernet(profile.outbox_key.get_secret_value().encode()).decrypt(
            path.read_bytes()
        )
        value = RotationJournal.model_validate_json(plaintext)
        if value.connection_id != profile.connection_id or value.refresh_fingerprint != fingerprint(
            profile
        ):
            raise ValueError
        return value
    except Exception:
        raise AdapterError("project_refresh_journal_invalid") from None


def replacement(
    profile: ProjectProfile, result: dict[str, Any], *, stored: bool = False
) -> tuple[ProjectProfile, PreparedTokens]:
    from durumari_mcp.project_gateway import validate_connection

    try:
        tokens = PreparedTokens.model_validate(result)
        # An authenticated local snapshot proves its original binding. Only the
        # fresh commit receipt proves current authority and inactivity expiry.
        connection = validate_connection(tokens.connection, profile, allow_expired_snapshot=stored)
        candidate = profile.model_copy(
            update={
                "access_token": tokens.access_token,
                "refresh_token": tokens.refresh_token,
                "access_expires_at": tokens.access_expires_at,
                "refresh_uncertain": False,
            }
        )
        candidate = ProjectProfile.model_validate_json(candidate.protected_json())
        candidate.check_client(profile.client)
        if (
            candidate.access_token == profile.access_token
            or candidate.refresh_token == profile.refresh_token
            or candidate.access_expires_at > connection.expires_at
        ):
            raise ValueError
        return candidate, tokens
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("invalid_project_response") from None


def rotate(profile: ProjectProfile, transport: "ProjectTransport", path: Path) -> ProjectProfile:
    from durumari_mcp.project_gateway import validate_connection

    if not path.exists():
        write_journal(
            path,
            profile,
            RotationJournal(
                connection_id=profile.connection_id,
                refresh_fingerprint=fingerprint(profile),
                attempt_id=secrets.token_urlsafe(32),
            ),
        )
    for attempt in range(2):
        journal = read_journal(path, profile)
        body = {
            "refresh_token": profile.refresh_token.get_secret_value(),
            "attempt_id": journal.attempt_id,
        }
        try:
            if journal.prepared_json is None:
                result = transport.request("/api/v1/project-agent/refresh/prepare", body=body)
                replacement(profile, result)
                journal = journal.model_copy(
                    update={"prepared_json": SecretStr(json.dumps(result))}
                )
                write_journal(path, profile, journal)
            assert journal.prepared_json is not None
            candidate, tokens = replacement(
                profile, json.loads(journal.prepared_json.get_secret_value()), stored=True
            )
            result = transport.request(
                "/api/v1/project-agent/refresh/commit",
                body={**body, "prepared_refresh_token": candidate.refresh_token.get_secret_value()},
            )
            try:
                receipt = CommitReceipt.model_validate(result)
            except Exception:
                raise AdapterError("invalid_project_response") from None
            validate_connection(receipt.connection, profile)
            if receipt.status == "reprepare":
                write_journal(path, profile, journal.model_copy(update={"prepared_json": None}))
                continue
            if receipt.access_expires_at != tokens.access_expires_at:
                raise AdapterError("invalid_project_response")
            return candidate
        except AdapterError as error:
            if str(error) == "project_refresh_not_supported" and journal.prepared_json is None:
                path.unlink()
            elif attempt == 0 and str(error) in {
                "project_delivery_unknown",
                "project_unavailable",
                "project_response_too_large",
                "invalid_project_response",
            }:
                continue
            raise
    raise AdapterError("project_unavailable")
