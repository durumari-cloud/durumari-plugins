"""The MCP and CLI share one bounded, receipt-driven Cloud Context client."""

import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

from durumari_mcp.capture import contains_sensitive
from durumari_mcp.context_outbox import ContextOutbox
from durumari_mcp.credentials import AdapterError
from durumari_mcp.local_upgrade import local_config_status
from durumari_mcp.project_gateway import ContextFailure, ProjectGateway, spent_busy
from durumari_mcp.version_info import (
    client_update,
    parse_version,
    runtime_version,
    server_release,
)

WARNING_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
ISSUE_TAG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,39}$")


def _writing_help(value: Any, keep: Any) -> list[Any] | None:
    # Optional help never makes a stored write look failed: malformed entries are dropped.
    if not isinstance(value, list):
        return None
    return [item for item in (keep(entry) for entry in value[:20]) if item is not None]


class Receipt(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    status: str = Field(pattern=r"^stored$")
    project_id: str = Field(min_length=1, max_length=128)
    context_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1, strict=True)
    request_key: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")
    replayed: StrictBool
    # Writing-rule help from newer servers; absent from older servers.
    added_tags: list[str] | None = None
    warnings: list[dict[str, str]] | None = None

    @field_validator("added_tags", mode="before")
    @classmethod
    def _tags(cls, value: Any) -> list[Any] | None:
        return _writing_help(
            value, lambda tag: tag if isinstance(tag, str) and ISSUE_TAG.fullmatch(tag) else None
        )

    @field_validator("warnings", mode="before")
    @classmethod
    def _warnings(cls, value: Any) -> list[Any] | None:
        def keep(entry: Any) -> dict[str, str] | None:
            if not isinstance(entry, dict):
                return None
            code, message = entry.get("code"), entry.get("message")
            if not isinstance(code, str) or not WARNING_CODE.fullmatch(code):
                return None
            return {"code": code, "message": message[:300] if isinstance(message, str) else ""}

        return _writing_help(value, keep)


class ContextClient:
    def __init__(self, gateway: ProjectGateway, session: str) -> None:
        self.gateway, self.session = gateway, session
        self.prefix = "/api/v2/projects/" + gateway.profile.project_id + "/contexts"
        self.queue = ContextOutbox(gateway.root, gateway.name, gateway.profile)

    def read(self, suffix: str = "", **query: Any) -> dict[str, Any]:
        path = self.prefix + suffix
        parameters = {
            key: value for key, value in query.items() if value is not None and value != ""
        }
        if parameters:
            path += "?" + urlencode(parameters)
        return self.gateway.context_request(path)

    def document_types(self) -> dict[str, Any]:
        return self.gateway.context_request(
            "/api/v2/projects/" + self.gateway.profile.project_id + "/document-types"
        )

    def glossary(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.glossary_tools import deliver

        return deliver(lambda payload: self.gateway.context_request(
            "/api/v2/agent/glossary", payload, method="POST",
            idempotency_key=payload.get("request_key")), action, body,
            project_id=self.gateway.profile.project_id)

    def work_logs(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.work_log_tools import deliver

        return deliver(
            lambda payload: self.gateway.context_request(
                "/api/v2/agent/work-logs/" + action, payload, method="POST",
                idempotency_key=payload.get("request_key"),
            ), action, body, project_id=self.gateway.profile.project_id,
        )

    def transcript_corrections(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Transcript corrections on a person's request.
        return self.gateway.context_request(
            "/api/v2/agent/meeting-transcripts/" + action, body, method="POST"
        )

    def chat_inbox(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Chat inbox; the bound connection names the space.
        return self.gateway.context_request(
            "/api/v2/agent/chat-inbox/" + action, body, method="POST"
        )

    def files(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.file_upload import deliver, prepare

        return deliver(
            lambda payload: self.gateway.context_request(
                "/api/v2/agent/file-uploads" + ("/status" if action == "status" else ""),
                payload,
                method="POST",
            ),
            prepare(body, action),
        )

    def secrets(self, suffix: str = "", **query: Any) -> dict[str, Any]:
        # Value-free vault metadata; registration is the local `secret put` command only.
        path = "/api/v2/agent/secrets" + suffix
        parameters = {key: value for key, value in query.items() if value}
        if parameters:
            path += "?" + urlencode(parameters)
        return self.gateway.context_request(path)

    def proposals(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.proposal_tools import ACTIONS

        if action not in ACTIONS:
            raise AdapterError("invalid_proposal_operation")
        key = body.get("request_key")
        return self.gateway.context_request(
            "/api/v2/agent/proposals/" + action, body, method="POST",
            idempotency_key=key if action in {"create", "apply"} and isinstance(key, str) else None,
        )

    def requests(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # The Agent route of review requests; writes replay on request_key.
        from durumari_mcp.request_tools import ACTIONS

        if action not in ACTIONS:
            raise AdapterError("invalid_request_operation")
        key = body.get("request_key")
        return self.gateway.context_request(
            "/api/v2/agent/requests/" + action,
            body,
            method="POST",
            idempotency_key=key if isinstance(key, str) else None,
        )

    def access_requests(self, body: dict[str, Any]) -> dict[str, Any]:
        # Ask the space's managers for view/edit; approval is on the web.
        return self.gateway.context_request("/api/v2/agent/access-requests", body, method="POST")

    def transcripts(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Meeting transcript search and bounded reads; read-only GETs.
        from durumari_mcp.transcript_tools import transcript_path

        suffix, query = transcript_path(action, body)
        return self.read(suffix, **query)

    def receipt(self, result: dict[str, Any], key: str) -> dict[str, Any]:
        try:
            saved = Receipt.model_validate(result)
            if saved.project_id != self.gateway.profile.project_id or saved.request_key != key:
                raise ValueError
            return saved.model_dump(exclude_none=True)
        except Exception:
            raise AdapterError("invalid_context_receipt") from None

    def mutate(self, method: str, suffix: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        if contains_sensitive(body):
            raise AdapterError("sensitive_context_rejected")
        self.gateway.profile.check_workspace(os.getcwd())
        owner = self.queue.enqueue(key, self.session, method, self.prefix + suffix, body)
        stored = self.queue.result(key)
        if stored is not None:
            # The same request answered again from the local receipt, as the server does.
            return {**self.receipt(stored, key), "replayed": True}
        rejected = self.queue.rejection(key)
        if rejected is not None:
            return {"status": "rejected", "request_key": key, "error": rejected}
        item = self.queue.claim(owner, key=key)
        if item is None:
            return {"status": "pending", "request_key": key}
        try:
            result = self.gateway.context_request(
                item["path"], item["body"], method=item["method"], idempotency_key=key
            )
            saved = self.receipt(result, key)
            self.queue.complete(key, item["lease"], saved)
            return saved
        except ContextFailure as error:
            if error.status == 426:
                return self.update_required(key, item["lease"], error.error)
            if spent_busy(error):
                self.queue.unknown(key, item["lease"])
                return {
                    "status": "pending",
                    "request_key": key,
                    "error": error.error,
                    "next_action": "retry_same_request_after_wait",
                }
            self.queue.reject(key, item["lease"], error.error)
            return {
                "status": "conflict" if error.status == 409 else "rejected",
                "request_key": key,
                "error": error.error,
            }
        except AdapterError as error:
            if str(error) == "server_requires_client_update":
                return self.update_required(key, item["lease"], {"code": "client_update_required"})
            self.queue.unknown(key, item["lease"])
            if str(error) in {
                "project_delivery_unknown",
                "project_unavailable",
                "project_rate_limited",
            }:
                return {
                    "status": "unknown",
                    "request_key": key,
                    "next_action": "retry_same_request_or_check_operation",
                }
            raise

    def update_required(self, key: str, lease: str, error: dict[str, Any]) -> dict[str, Any]:
        # Not a rejection: the encrypted queue keeps this exact request for the same
        # key, and the updated plugin delivers it once.
        self.queue.unknown(key, lease)
        return {
            "status": "pending",
            "request_key": key,
            "error": error,
            "next_action": "update_durumari_then_retry_same_request",
        }

    def policies(self, after: int | None = None) -> dict[str, Any]:
        # The hook keeps the cursor-based history; the tool result omits repeated events.
        return self.read("/policies", after=after, recent="0")

    def operation(self, key: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise AdapterError("invalid_context_request")
        try:
            saved = self.receipt(self.read("/operations/" + key), key)
            self.queue.confirm(key, saved)
            return saved
        except ContextFailure as error:
            if error.status == 404:
                return {"status": "unknown", "request_key": key}
            raise

    def status(self) -> dict[str, Any]:
        value = self.gateway.context_request("/api/v2/agent/connection")
        if (
            value.get("id"),
            value.get("project_id"),
            value.get("repository_id"),
            value.get("workspace_identity"),
        ) != (
            self.gateway.profile.connection_id,
            self.gateway.profile.project_id,
            self.gateway.profile.repository_id,
            self.gateway.profile.workspace_identity,
        ):
            raise AdapterError("project_connection_scope_changed")
        release = self.release()
        pending = self.queue.pending_status(None)
        session_pending = self.queue.pending_status(self.session)
        return {
            "connection_id": value["id"],
            "project_id": value["project_id"],
            "repository_id": value["repository_id"],
            "state": value["state"],
            # A new CLI process is its own session; report the whole connection's queue too.
            "pending_writes": pending.count,
            "pending_writes_this_session": session_pending.count,
            "quarantined_writes_recovery_required": (
                pending.recovery_required or session_pending.recovery_required
            ),
            "runtime_version": runtime_version(),
            "server": self.gateway.profile.gateway_origin,
            "server_release": (server_release(release) if release else None) or "unavailable",
            "update": client_update(release, runtime_version()),
            "local_config": local_config_status(Path(self.gateway.profile.workspace)),
        }

    def release(self) -> dict[str, Any] | None:
        # Public release metadata only; failure never affects the connection state.
        # Only a plain x.y.z version is sent: a long local build string would make the
        # server refuse the whole public release lookup.
        current = runtime_version()
        query = f"?{urlencode({'client_version': current})}" if parse_version(current) else ""
        try:
            return self.gateway.transport.request("/api/v2/release" + query)
        except (AdapterError, ContextFailure):
            return None
