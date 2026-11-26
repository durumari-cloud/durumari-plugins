"""Project-only credentials. Account sessions never enter the MCP process."""

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from durumari_mcp.credentials import AdapterError, Profile, os_store, profile_name
from durumari_mcp.legacy import PROJECT_SERVICE_V1, manifest_path
from durumari_mcp.server_aliases import same_server

PROJECT_SERVICE = "DURUMARI.MCP.ProjectConnection.v1"


class ProjectProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    gateway_origin: str
    connection_id: str = Field(min_length=1, max_length=128)
    project_id: str = Field(min_length=1, max_length=128)
    source_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=128)
    client: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    workspace: str
    access_token: SecretStr = Field(repr=False)
    refresh_token: SecretStr = Field(repr=False)
    access_expires_at: datetime
    enrolled_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    outbox_key: SecretStr = Field(repr=False)
    refresh_uncertain: bool = False
    capture_enabled: bool = Field(default=True, strict=True)
    repository_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    context_only: bool = Field(default=False, strict=True)
    workspace_identity: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    folder_source_id: str | None = Field(
        default=None, pattern=r"^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$"
    )
    disabled: bool = Field(default=False, strict=True)

    @field_validator("gateway_origin")
    @classmethod
    def origin(cls, value: str) -> str:
        return Profile.trusted_origin(value)

    @field_validator("workspace")
    @classmethod
    def workspace_path(cls, value: str) -> str:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("explicit absolute workspace required")
        return str(path.resolve())

    @field_validator("access_token", "refresh_token")
    @classmethod
    def scoped_token(cls, value: SecretStr) -> SecretStr:
        if not re.fullmatch(r"mp[ar]_[A-Za-z0-9_-]{43}", value.get_secret_value()):
            raise ValueError("scoped project credential required")
        return value

    @field_validator("access_expires_at", "enrolled_at")
    @classmethod
    def aware_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("aware expiry required")
        return value

    @field_validator("outbox_key")
    @classmethod
    def encryption_key(cls, value: SecretStr) -> SecretStr:
        Fernet(value.get_secret_value().encode("ascii"))
        return value

    def check_client(self, client: str) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", client) or (
            client != self.client and not (self.context_only and self.client == "mcp")
        ):
            raise AdapterError("project_client_mismatch")
        if self.context_only and (
            self.client != "mcp"
            or self.capture_enabled
            or not self.repository_id
            or not self.workspace_identity
        ):
            raise AdapterError("invalid_project_credential")
        if not self.context_only and self.client not in {"codex", "claude"}:
            raise AdapterError("project_client_mismatch")
        if not self.access_token.get_secret_value().startswith("mpa_") or not (
            self.refresh_token.get_secret_value().startswith("mpr_")
        ):
            raise AdapterError("invalid_project_credential")

    def check_workspace(self, cwd: str) -> None:
        try:
            Path(cwd).resolve(strict=True).relative_to(Path(self.workspace))
        except (ValueError, OSError):
            raise AdapterError("workspace_out_of_scope") from None
        self.check_binding()
        if self.context_only and self.folder_source_id is not None:
            # Folder sources have no Git top-level to bound them, so any nested manifest
            # or repository marks the edge.
            from durumari_mcp.workspace_binding import check_descendant

            check_descendant(Path(self.workspace), Path(cwd).resolve())
        elif self.context_only:
            # Existing Git bindings keep the rev-parse boundary below; unrelated manifest
            # files stay usable and only a separately connected folder source stops them.
            from durumari_mcp.workspace_binding import check_nested_folder_sources

            check_nested_folder_sources(Path(self.workspace), Path(cwd).resolve())
            try:
                top = subprocess.check_output(
                    ["git", "-C", str(Path(cwd).resolve()), "rev-parse", "--show-toplevel"],
                    stderr=subprocess.DEVNULL,
                    text=True,
                    # Git prints paths in UTF-8; Windows' code page would garble non-ASCII.
                    encoding="utf-8",
                    timeout=2,
                ).strip()
                if Path(top).resolve() != Path(self.workspace):
                    raise ValueError
            except (ValueError, OSError, subprocess.SubprocessError):
                raise AdapterError("workspace_out_of_scope") from None

    def check_binding(self) -> None:
        root = Path(self.workspace)
        if not root.is_dir():
            raise AdapterError("workspace_out_of_scope")
        if (
            self.context_only
            and self.workspace_identity
            != hashlib.sha256(
                ("workspace:" + os.path.normcase(str(root.resolve()))).encode()
            ).hexdigest()
        ):
            raise AdapterError("workspace_out_of_scope")
        if self.repository_id is not None:
            from durumari_mcp.onboarding import repository_identity
            from durumari_mcp.workspace_binding import folder_identity

            identity = (
                folder_identity(self.folder_source_id)
                if self.folder_source_id
                else repository_identity(root)
            )
            if self.repository_id != identity:
                raise AdapterError("project_repository_mismatch_reconnect")
        if manifest_path(root).exists():
            from durumari_mcp.onboarding import load_manifest

            manifest = load_manifest(root)
            # source_id names one approved connection, so each teammate's differs; the
            # server verifies this profile's own source. A shared manifest can only
            # redirect by changing the server, project or folder source.
            if (
                not same_server(manifest["server"], self.gateway_origin)
                or manifest["project_id"] != self.project_id
                or manifest.get("folder_source_id") != self.folder_source_id
            ):
                raise AdapterError("project_connection_scope_changed")

    def protected_json(self) -> str:
        value = self.model_dump(mode="json")
        # Retain the legacy representation for previously enrolled capture grants.
        if self.capture_enabled:
            value.pop("capture_enabled")
        if self.repository_id is None:
            value.pop("repository_id")
        if not self.context_only:
            value.pop("context_only")
        if self.workspace_identity is None:
            value.pop("workspace_identity")
        if self.folder_source_id is None:
            value.pop("folder_source_id")
        if not self.disabled:
            value.pop("disabled")
        for key in ("access_token", "refresh_token", "outbox_key"):
            value[key] = getattr(self, key).get_secret_value()
        return json.dumps(value, separators=(",", ":"))


def require_project_store() -> None:
    """Reject unsupported credential stores before interactive account login."""
    os_store()


def save_project_profile(name: str, profile: ProjectProfile) -> None:
    name = profile_name(name)
    profile.check_client(profile.client)
    try:
        os_store().set_password(PROJECT_SERVICE, name, profile.protected_json())
    except Exception:
        raise AdapterError("protected_project_store_write_failed") from None
    from durumari_mcp.hook_recovery import clear_hook_recovery

    clear_hook_recovery(name)


def load_project_profile(name: str) -> ProjectProfile:
    name = profile_name(name)
    try:
        store = os_store()
        stored = store.get_password(PROJECT_SERVICE, name)
        if stored is None:
            stored = store.get_password(PROJECT_SERVICE_V1, name)
        if stored is None:
            raise AdapterError("project_credential_missing")
        profile = ProjectProfile.model_validate_json(stored)
        profile.check_client(profile.client)
        return profile
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("protected_project_store_read_failed") from None


def delete_project_profile(name: str) -> None:
    name = profile_name(name)
    try:
        store = os_store()
        for service in (PROJECT_SERVICE, PROJECT_SERVICE_V1):
            if store.get_password(service, name) is not None:
                store.delete_password(service, name)
    except Exception:
        raise AdapterError("protected_project_store_delete_failed") from None
