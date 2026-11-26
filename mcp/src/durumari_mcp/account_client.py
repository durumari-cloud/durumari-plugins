"""Account transport with immutable, explicit per-conversation project views."""

import hashlib
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode
from uuid import uuid4

from durumari_mcp.account_credentials import (
    UUID_PATTERN,
    AccountConnection,
    AccountProfile,
    load_account_profile,
    profile_from_response,
    save_account_profile,
)
from durumari_mcp.capture import contains_sensitive
from durumari_mcp.context_client import Receipt
from durumari_mcp.context_outbox import ContextOutbox
from durumari_mcp.credentials import AdapterError, profile_name
from durumari_mcp.project_gateway import ContextFailure, ProjectTransport, spent_busy
from durumari_mcp.project_outbox import process_lock, state_root
from durumari_mcp.version_info import client_update, parse_version, runtime_version

if TYPE_CHECKING:
    from durumari_mcp.account_skill_gateway import AccountSkillGateway


def reference(value: str) -> str:
    if not re.fullmatch(UUID_PATTERN, value):
        raise AdapterError("invalid_project_reference")
    return value


def shell_command(arguments: list[str]) -> str:
    """One command line an Agent can run as-is; every argument quoted for the host shell."""
    if os.name == "nt":
        return subprocess.list2cmdline(arguments)
    return shlex.join(arguments)


class AccountContextClient:
    # Account Skill tools use /api/v2/agent/account/skills with each call's project_ref.
    native_skills = True

    def __init__(
        self,
        name: str,
        client: str,
        *,
        root: Path | None = None,
        transport: ProjectTransport | None = None,
    ) -> None:
        self.name, self.client = profile_name(name), client
        self.profile = load_account_profile(name)
        self.profile.check_client(client)
        self.root = root or state_root() / "accounts"
        self.transport = transport or ProjectTransport(self.profile.gateway_origin)
        # Queue ownership only. This is deliberately not a host conversation identity.
        self.session = str(uuid4())

    def _profile(self) -> AccountProfile:
        with process_lock(self.root / (self.name + ".credentials")):
            profile = load_account_profile(self.name)
            profile.check_client(self.client)
            if (profile.gateway_origin, profile.connection_id, profile.actor_id) != (
                self.profile.gateway_origin,
                self.profile.connection_id,
                self.profile.actor_id,
            ):
                raise AdapterError("account_profile_changed_restart_required")
            if profile.refresh_uncertain or profile.expires_at <= datetime.now(UTC):
                raise AdapterError("account_reconnect_required")
            if profile.access_expires_at <= datetime.now(UTC) + timedelta(seconds=30):
                # Persist uncertainty before consuming the one-use refresh token. A crash,
                # lost reply or OS-store failure must never cause automatic replay.
                save_account_profile(
                    self.name, profile.model_copy(update={"refresh_uncertain": True})
                )
                try:
                    value = self.transport.request(
                        "/api/v2/agent/account/token",
                        body={
                            "refresh_token": profile.refresh_token.get_secret_value(),
                            "client": self.client,
                        },
                    )
                    replacement = profile_from_response(
                        value,
                        profile.gateway_origin,
                        self.client,
                        profile.outbox_key.get_secret_value(),
                        expected=profile,
                    )
                    if replacement.refresh_token == profile.refresh_token:
                        raise AdapterError("invalid_account_response")
                    save_account_profile(self.name, replacement)
                    profile = replacement
                except Exception:
                    raise AdapterError("account_reconnect_required") from None
            self.profile = profile
            return profile

    def request(self, path: str, **kwargs: Any) -> dict[str, Any]:
        profile = self._profile()
        return self.transport.request(path, token=profile.access_token, **kwargs)

    def targeted(self, ref: str) -> "AccountTargetView":
        return AccountTargetView(self, reference(ref))

    def direct_target(self, project_id: str) -> "AccountTargetView":
        return AccountTargetView(self, reference(project_id), _direct=True)

    def search_account(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("/api/v2/agent/context/search", body=body)

    def unified_search(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("/api/v2/agent/context/unified-search", body=body)

    def my_work(self, body: dict[str, Any]) -> dict[str, Any]:
        return self.request("/api/v2/agent/context/my-work", body=body)

    def glossary(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def document_types(self) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def work_logs(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def chat_inbox(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def transcript_corrections(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def places(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Cloud and space management, as on the official REST API.
        return self.request("/api/v2/agent/places/" + action, body=body)

    @property
    def skill_gateway(self) -> "AccountSkillGateway | None":
        if not self.native_skills:
            return None
        from durumari_mcp.account_skill_gateway import AccountSkillGateway

        return AccountSkillGateway(self)

    def proposals(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.proposal_tools import ACTIONS

        if action not in ACTIONS:
            raise AdapterError("invalid_proposal_operation")
        key = body.get("request_key")
        return self.request(
            "/api/v2/agent/proposals/" + action,
            body=body,
            idempotency_key=key if action in {"create", "apply"} and isinstance(key, str) else None,
        )

    def requests(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Same Agent route as a folder connection; the body names the project (647).
        from durumari_mcp.request_tools import ACTIONS

        if action not in ACTIONS:
            raise AdapterError("invalid_request_operation")
        key = body.get("request_key")
        return self.request(
            "/api/v2/agent/requests/" + action,
            body=body,
            idempotency_key=key if isinstance(key, str) else None,
        )

    def access_requests(self, body: dict[str, Any]) -> dict[str, Any]:
        # The body names the space (the refusal's project_id); approval is on the web.
        return self.request("/api/v2/agent/access-requests", body=body)

    def transcripts(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def projects(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action not in {"start", "catalog", "select", "browser", "disconnect", "status"}:
            raise AdapterError("invalid_project_operation")
        if action != "status":
            return self.request("/api/v2/agent/projects/" + action, body=body)
        wait = body.get("wait_seconds", 0)
        if type(wait) is not int or not 0 <= wait <= 15:
            raise AdapterError("invalid_project_operation")
        task_ref = body.get("task_ref")
        if not isinstance(task_ref, str):
            raise AdapterError("invalid_project_operation")
        reference(task_ref)
        deadline = time.monotonic() + wait
        while True:
            value = self.request("/api/v2/agent/projects/status", body={"task_ref": task_ref})
            remaining = deadline - time.monotonic()
            if value.get("state") != "pending" or remaining <= 0:
                return value
            time.sleep(min(2, remaining))

    def status(self) -> dict[str, Any]:
        try:
            value = AccountConnection.model_validate(
                self.request("/api/v2/agent/account/connection")
            )
            if (value.id, value.actor_id, value.client, value.provider_family) != (
                self.profile.connection_id,
                self.profile.actor_id,
                self.client,
                self.profile.provider_family,
            ):
                raise ValueError
            return {
                **value.model_dump(mode="json"),
                "state": "active",
                "runtime_version": runtime_version(),
                "server": self.profile.gateway_origin,
                "update": client_update(self.release(), runtime_version()),
            } | ({"legacy_folder": legacy} if (legacy := self.legacy_folder()) else {})
        except (ValueError, TypeError):
            raise AdapterError("account_connection_scope_changed") from None

    def legacy_folder(self) -> dict[str, Any] | None:
        """A folder connection this host has not moved to the account yet (649).

        Both serve the same tools, so the host lists them twice until the explicit,
        reviewed account-migrate plan retires the folder entry. Never changes files.
        Only reported when the binding serves this client on the account's server.
        """
        try:
            from durumari_mcp.context_binding import json_file
            from durumari_mcp.context_removal import local_profile
            from durumari_mcp.project_credentials import load_project_profile
            from durumari_mcp.server_aliases import same_server
            from durumari_mcp.workspace_binding import binding_root

            root = binding_root(Path.cwd().resolve())
            folder = local_profile(root)
            if folder is None:
                return None
            binding = json_file(root, ".durumari/context-binding.json")
            if not isinstance(binding, dict):
                return None
            moved = binding.get("account_clients", {})
            if not isinstance(moved, dict) or self.client in moved:
                return None
            agent = {"claude-code": "claude"}.get(self.client, self.client)
            agents = binding.get("agents")
            names: set[str] = set()
            if isinstance(agents, dict):
                names = {item for item in agents if isinstance(item, str)}
            elif isinstance(agents, list):
                for item in agents:
                    if isinstance(item, str):
                        names.add(item)
                    elif isinstance(item, dict):
                        names.update(
                            value
                            for value in (item.get("agent"), item.get("name"))
                            if isinstance(value, str)
                        )
            if agent not in names and self.client not in names:
                return None
            # local_profile returns the folder's profile name; its stored profile holds
            # the server and user the folder connection actually uses.
            project = load_project_profile(folder)
            server = getattr(project, "gateway_origin", None)
            if not isinstance(server, str):
                server = binding.get("server")
            if not isinstance(server, str) or not same_server(
                server, self.profile.gateway_origin
            ):
                return None
            for field in ("actor_id", "user_id"):
                expected = getattr(self.profile, field, None)
                found = getattr(project, field, None)
                if not isinstance(found, str):
                    found = binding.get(field)
                if isinstance(expected, str) and isinstance(found, str) and found != expected:
                    return None
        except Exception:
            return None
        return {
            "workspace": str(root),
            "state": "folder_connection_active",
            "next_action": shell_command(
                [
                    "durumari-mcp",
                    "account-migrate",
                    "plan",
                    "--client",
                    self.client,
                    "--profile",
                    self.name,
                    "--workspace",
                    str(root),
                ]
            ),
        }

    def release(self) -> dict[str, Any] | None:
        # Public release metadata; failure never affects the connection state.
        current = runtime_version()
        query = f"?{urlencode({'client_version': current})}" if parse_version(current) else ""
        try:
            return self.transport.request("/api/v2/release" + query)
        except (AdapterError, ContextFailure):
            return None

    def resources(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        if action == "begin":
            return self.request("/api/v2/agent/resource-interactions", body=body)
        if action != "status":
            raise AdapterError("invalid_resource_operation")
        wait = body.get("wait_seconds", 0)
        task_ref = body.get("task_ref")
        if type(wait) is not int or not 0 <= wait <= 15 or not isinstance(task_ref, str):
            raise AdapterError("invalid_resource_operation")
        reference(task_ref)
        deadline = time.monotonic() + wait
        while True:
            value = self.request(
                "/api/v2/agent/resource-interactions/status", body={"task_ref": task_ref}
            )
            remaining = deadline - time.monotonic()
            if value.get("state") != "pending" or remaining <= 0:
                return value
            time.sleep(min(2, remaining))

    def read(self, suffix: str = "", **query: Any) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def files(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def mutate(self, method: str, suffix: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def operation(self, key: str) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def policies(self, after: int | None = None) -> dict[str, Any]:
        raise AdapterError("project_selection_required")

    def secrets(self, suffix: str = "", **query: Any) -> dict[str, Any]:
        raise AdapterError("project_selection_required")


@dataclass(frozen=True)
class AccountTargetView:
    _account: AccountContextClient
    _ref: str
    _direct: bool = False

    def _target(self) -> dict[str, str]:
        return {"target_project_id" if self._direct else "project_ref": self._ref}

    def work_logs(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.work_log_tools import deliver

        return deliver(
            lambda payload: self._account.request(
                "/api/v2/agent/work-logs/" + action,
                body={**payload, **self._target()}, idempotency_key=payload.get("request_key"),
            ), action, body,
        )

    def transcript_corrections(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Transcript corrections in the selected space.
        return self._account.request(
            "/api/v2/agent/meeting-transcripts/" + action, body={**body, **self._target()}
        )

    def chat_inbox(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        # Chat inbox in the selected space.
        return self._account.request(
            "/api/v2/agent/chat-inbox/" + action, body={**body, **self._target()}
        )

    def glossary(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.glossary_tools import deliver

        return deliver(
            lambda payload: self._account.request(
                "/api/v2/agent/glossary",
                body={**payload, "project_ref": self._ref},
                idempotency_key=payload.get("request_key"),
            ),
            action,
            body,
        )

    @property
    def skill_gateway(self) -> "AccountSkillGateway | None":
        if not self._account.native_skills:
            return None
        from durumari_mcp.account_skill_gateway import AccountSkillGateway

        return AccountSkillGateway(self._account, self._ref)

    def _resolve(self) -> str:
        value = self._account.request(
            "/api/v2/agent/context/target" if self._direct else "/api/v2/agent/projects/resolve",
            body=self._target(),
        )
        project_id = value.get("project_id")
        if not isinstance(project_id, str) or not re.fullmatch(UUID_PATTERN, project_id):
            raise AdapterError("invalid_project_response")
        if self._direct and project_id != self._ref:
            raise AdapterError("invalid_project_response")
        return project_id

    def _queue(self, project_id: str) -> ContextOutbox:
        profile = self._account.profile
        scope = [
            "account-context-v1",
            profile.gateway_origin,
            profile.connection_id,
            profile.actor_id,
            profile.client,
            ("direct:" if self._direct else "") + self._ref,
            project_id,
        ]
        # The directory remains stable when a response lies about the project: the
        # persisted scope then rejects it instead of creating a second target queue.
        name = (
            "account-"
            + hashlib.sha256(
                (
                    self._account.name
                    + ":"
                    + profile.connection_id
                    + ":"
                    + ("direct:" if self._direct else "")
                    + self._ref
                ).encode()
            ).hexdigest()[:48]
        )
        return ContextOutbox.from_scope(
            self._account.root,
            name,
            scope=scope,
            outbox_key=profile.outbox_key.get_secret_value(),
            prefix="/api/v2/projects/" + project_id + "/contexts",
        )

    def _receipt(
        self, value: dict[str, Any], key: str, project_id: str | None = None
    ) -> dict[str, Any]:
        try:
            result = Receipt.model_validate(value)
            reference(result.project_id)
            if result.request_key != key or (
                project_id is not None and result.project_id != project_id
            ):
                raise ValueError
            return result.model_dump(exclude_none=True)
        except Exception:
            raise AdapterError("invalid_context_receipt") from None

    def files(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.file_upload import deliver, prepare

        return deliver(
            lambda payload: self._account.request(
                "/api/v2/agent/file-uploads" + ("/status" if action == "status" else ""),
                body={**payload, "project_ref": self._ref},
            ),
            prepare(body, action),
        )

    def document_types(self) -> dict[str, Any]:
        project_id = self._resolve()
        return self._account.request(
            "/api/v2/projects/" + project_id + "/document-types", **self._target()
        )

    def transcripts(self, action: str, body: dict[str, Any]) -> dict[str, Any]:
        from durumari_mcp.transcript_tools import transcript_path

        suffix, query = transcript_path(action, body)
        return self.read(suffix, **query)

    def read(self, suffix: str = "", **query: Any) -> dict[str, Any]:
        if not re.fullmatch(
            r"(?:/search|/policies|/[A-Za-z0-9-]{1,64}(?:/(?:history|related|attachments))?"
            r"|/meeting-transcripts/(?:search|sessions/[A-Za-z0-9_:-][A-Za-z0-9_.:-]{0,127}))?",
            suffix,
        ):
            raise AdapterError("invalid_context_request")
        project_id = self._resolve()
        path = "/api/v2/projects/" + project_id + "/contexts" + suffix
        parameters = {
            key: value for key, value in query.items() if value is not None and value != ""
        }
        if parameters:
            path += "?" + urlencode(parameters)
        return self._account.request(path, **self._target())

    def mutate(self, method: str, suffix: str, body: dict[str, Any], key: str) -> dict[str, Any]:
        if contains_sensitive(body):
            raise AdapterError("sensitive_context_rejected")
        action, context_id = self._mutation(method, suffix)
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise AdapterError("invalid_context_request")
        project_id = self._resolve()
        queue = self._queue(project_id)
        owner = queue.enqueue(key, self._account.session, method, queue.prefix + suffix, body)
        stored = queue.result(key)
        if stored is not None:
            return self._receipt(stored, key, project_id)
        rejected = queue.rejection(key)
        if rejected is not None:
            return {"status": "rejected", "request_key": key, "error": rejected}
        item = queue.claim(owner, key=key)
        if item is None:
            return {"status": "pending", "request_key": key}
        try:
            result = self._account.request(
                "/api/v2/agent/context/mutations",
                body={
                    **self._target(),
                    "request_key": key,
                    "action": action,
                    **({"context_id": context_id} if context_id is not None else {}),
                    "body": item["body"],
                },
                idempotency_key=key,
            )
            saved = self._receipt(result, key, project_id)
            queue.complete(key, item["lease"], saved)
            return saved
        except ContextFailure as error:
            if error.status == 426:
                # The server refused execution before the operation. Keep the exact
                # immutable target/body/key recoverable after a mandatory update.
                queue.unknown(key, item["lease"])
                raise
            if spent_busy(error):
                # Never processed: keep the exact request queued for the same key.
                queue.unknown(key, item["lease"])
                return {
                    "status": "pending",
                    "request_key": key,
                    "error": error.error,
                    "next_action": "retry_same_request_after_wait",
                }
            queue.reject(key, item["lease"], error.error)
            return {
                "status": "conflict" if error.status == 409 else "rejected",
                "request_key": key,
                "error": error.error,
            }
        except AdapterError as error:
            queue.unknown(key, item["lease"])
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

    @staticmethod
    def _mutation(method: str, suffix: str) -> tuple[str, str | None]:
        if method == "POST" and suffix == "":
            return "create", None
        match = re.fullmatch(r"/(" + UUID_PATTERN[1:-1] + r")(/restore)?", suffix)
        if match is None:
            raise AdapterError("invalid_context_request")
        if method in {"PATCH", "DELETE"} and match[2] is None:
            return ("patch" if method == "PATCH" else "delete"), match[1]
        if method == "POST" and match[2] == "/restore":
            return "restore", match[1]
        raise AdapterError("invalid_context_request")

    def operation(self, key: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise AdapterError("invalid_context_request")
        try:
            value = self._account.request(
                "/api/v2/agent/context/operation"
                if self._direct
                else "/api/v2/agent/projects/operation",
                body={**self._target(), "request_key": key},
            )
            saved = self._receipt(value, key)
            # Historical refs are checked by this server operation. No current resolve.
            self._queue(saved["project_id"]).confirm(key, saved)
            return saved
        except ContextFailure as error:
            if error.status == 404:
                return {"status": "unknown", "request_key": key}
            raise

    def policies(self, after: int | None = None) -> dict[str, Any]:
        # The hook keeps the cursor-based history; the tool result omits repeated events.
        return self.read("/policies", after=after, recent="0")

    def status(self) -> dict[str, Any]:
        """What a folder's session start hook shows, for a hook-free account (650)."""
        project_id = self._resolve()
        value = self._account.status()
        queue = self._queue(project_id)
        pending = queue.pending_status(None)
        session = queue.pending_status(self._account.session)
        try:
            # Policy index lines, work logs and open review requests for this project.
            notices: dict[str, Any] | str = self.policies()
        except (AdapterError, ContextFailure):
            notices = "unavailable"
        return {
            **value,
            "project_id": project_id,
            "pending_writes": pending.count,
            "pending_writes_this_session": session.count,
            "quarantined_writes_recovery_required": pending.recovery_required
            or session.recovery_required,
            "policy_index": notices,
        }

    def secrets(self, suffix: str = "", **query: Any) -> dict[str, Any]:
        if not re.fullmatch(r"(?:/[a-f0-9-]{36}|/operations/[a-f0-9-]{36})?", suffix):
            raise AdapterError("invalid_project_operation")
        self._resolve()
        path = "/api/v2/agent/context/secrets" + suffix
        parameters = {key: value for key, value in query.items() if value}
        if parameters:
            path += "?" + urlencode(parameters)
        return self._account.request(path, project_ref=self._ref)
