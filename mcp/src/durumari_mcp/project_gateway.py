"""Fixed-origin project transport and serialized credential rotation."""

import hashlib
import io
import json
import math
import os
import random
import re
import ssl
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener
from uuid import uuid4

from cryptography.fernet import Fernet
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, SecretStr, StrictBool

from durumari_mcp.credentials import AdapterError
from durumari_mcp.gateway import NoRedirect
from durumari_mcp.project_credentials import (
    ProjectProfile,
    load_project_profile,
    save_project_profile,
)
from durumari_mcp.project_outbox import process_lock, state_root
from durumari_mcp.server_aliases import request_origin
from durumari_mcp.version_info import parse_version, runtime_version

MAX_PROJECT_RESPONSE_BYTES = 1_048_576


class ContextFailure(AdapterError):
    """Expected API failures carry bounded, structured conflict information."""

    def __init__(self, status: int, error: dict[str, Any]) -> None:
        code = error.get("code", "context_request_rejected")
        if not isinstance(code, str) or not re.fullmatch(r"[a-z_]{1,80}", code):
            code = "context_request_rejected"
        super().__init__(code)
        self.status = status
        self.error: dict[str, Any] = {"code": code}
        details = error.get("details")
        if isinstance(details, dict) and len(json.dumps(details).encode()) <= 16000:
            self.error["details"] = details


class EventReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    id: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(ge=0, le=2_147_483_647, strict=True)
    received_at: AwareDatetime
    cursor: int = Field(ge=0, strict=True)
    replayed: StrictBool


def validate_receipt(value: Any, event: dict[str, Any]) -> dict[str, Any]:
    try:
        receipt = EventReceipt.model_validate(value)
        if (receipt.event_id, receipt.session_id, receipt.sequence) != (
            event["event_id"],
            event["session_id"],
            event["sequence"],
        ):
            raise ValueError
        return receipt.model_dump(mode="json")
    except Exception:
        raise AdapterError("invalid_event_receipt") from None


class ProjectConnection(BaseModel):
    # Server responses may gain fields; released clients ignore them and keep
    # checking every declared one.
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    id: str
    project_id: str
    source_id: str
    actor_id: str
    host: Literal["codex", "claude", "mcp"]
    provider: str
    environment_name: str
    actions: list[Literal["read", "capture", "context:read", "context:write"]]
    revision: int = Field(ge=1, strict=True)
    state: Literal["active", "expired", "revoked", "unavailable"]
    created_at: datetime
    expires_at: datetime
    last_received_at: datetime | None
    capture_status: Literal["host_capture_not_verified"]


# Refusals the server or ingress makes before the request reaches the application
# say so with details.unprocessed: resending is then safe for
# every method. The same code without it (e.g. a handler's request_busy) may follow a
# commit, so only reads and idempotent requests are resent.
RETRY_STATUSES = {429, 503}
MAX_RETRIES = 3
MAX_RETRY_WAIT = 20.0
VAPOR_EMPTY = "vapor_empty"


def spent_busy(error: "ContextFailure") -> bool:
    """A 429/503 the server refused before processing, after the retries were spent.

    The request was never applied, so a queued write stays queued for the same key
    instead of becoming a rejection: a busy server must not lose a save.
    """
    return error.status in RETRY_STATUSES and error.error.get("code") == "project_rate_limited"


def retry_after(error: HTTPError) -> float | None:
    value = error.headers.get("Retry-After", "") if error.headers else ""
    try:
        seconds = float(str(value).strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 120 else None


def refusal_error(data: bytes) -> dict[str, Any]:
    try:
        value = json.loads(data) if len(data) <= 16000 else {}
    except (ValueError, UnicodeError):
        return {}  # an ingress page may be HTML
    detail = value.get("error") if isinstance(value, dict) else None
    return detail if isinstance(detail, dict) else {}


def refusal(data: bytes) -> tuple[str | None, bool]:
    """The refusal's code, and whether it says the request was never processed.

    `vapor_empty` (the cloud's processing rate is used up) is refused by the
    server's dependency before any handler runs, so it counts as unprocessed too.
    """
    detail = refusal_error(data)
    code = detail.get("code")
    details = detail.get("details")
    marked = isinstance(details, dict) and details.get("unprocessed") is True
    code = code if isinstance(code, str) else None
    return code, marked or code == VAPOR_EMPTY


def vapor_wait(data: bytes) -> float | None:
    """Seconds a `vapor_empty` refusal asks to wait (details.retry_after_seconds)."""
    details = refusal_error(data).get("details")
    value = details.get("retry_after_seconds") if isinstance(details, dict) else None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        return None
    return float(value)


class ProjectTransport:
    # Replaced in tests; jitter spreads clients refused together.
    sleep = staticmethod(time.sleep)
    jitter = staticmethod(lambda: random.uniform(1.0, 1.3))

    def _open(self, request: Request, timeout: float, replayable: bool) -> Any:
        """Open, waiting out 429/503 with Retry-After using exponential backoff.

        Only a request the server did not process (or a GET/idempotent one) is resent,
        so a queued write is never applied twice. Retries stop within MAX_RETRY_WAIT.
        """
        waited = 0.0
        for attempt in range(MAX_RETRIES + 1):
            try:
                return self.opener.open(request, timeout=timeout)
            except HTTPError as error:
                if error.code not in RETRY_STATUSES:
                    raise
                data = error.read(16001)
                error.close()
                code, unprocessed = refusal(data)
                after = retry_after(error)
                if after is None and code == VAPOR_EMPTY:
                    after = vapor_wait(data)
                wait = max(after or 0.0, 0.5 * 2**attempt) * self.jitter()
                retry = (
                    (unprocessed or replayable)
                    and after is not None
                    and attempt < MAX_RETRIES
                    and waited + wait <= MAX_RETRY_WAIT
                )
                if not retry:
                    copy = HTTPError(
                        error.url, error.code, error.msg, error.headers, io.BytesIO(data)
                    )
                    if code == VAPOR_EMPTY:
                        # The wait is the cloud's, not the server's: say so, and that a
                        # bigger plan ends it. Queued writes stay queued (spent_busy).
                        wanted = vapor_wait(data)
                        seconds = int(math.ceil(wanted if wanted is not None else after or 1))
                        copy.durumari_busy = {  # type: ignore[attr-defined]
                            "code": "project_rate_limited",
                            "details": {
                                "retry_after_seconds": max(1, seconds),
                                "reason": VAPOR_EMPTY,
                                "next_action": "wait_or_upgrade",
                            },
                        }
                    elif unprocessed:
                        copy.durumari_busy = {  # type: ignore[attr-defined]
                            "code": "project_rate_limited",
                            "details": {
                                "retry_after_seconds": int(after or 1),
                                "reason": "account_busy"
                                if code == "account_busy"
                                else "server_busy",
                            },
                        }
                    raise copy from None
                self.sleep(wait)
                waited += wait
        raise AssertionError("unreachable")

    def __init__(self, origin: str) -> None:
        # Which surface this client is (`mcp` or `cli`), told to the server so a document
        # value's history names it; unset means a plain REST client.
        self.via: str | None = None
        self.origin = request_origin(ProjectProfile.origin(origin))
        self.opener = build_opener(
            ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
        )

    def request(
        self,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: SecretStr | None = None,
        scheme: str = "ProjectPlugin",
        method: str | None = None,
        idempotency_key: str | None = None,
        project_ref: str | None = None,
        target_project_id: str | None = None,
    ) -> dict[str, Any]:
        # Callers use only fixed route families and validated opaque identifiers.
        if (
            not path.startswith(("/api/v1/", "/api/v2/"))
            or "#" in path
            or ".." in path.split("?", 1)[0]
        ):
            raise AdapterError("invalid_project_operation")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if target_project_id is not None:
            if project_ref is not None or not re.fullmatch(
                r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", target_project_id
            ):
                raise AdapterError("invalid_context_target")
            headers["X-Durumari-Target-Project"] = target_project_id
        # The server compares this with its required Agent version before any work.
        current_version = runtime_version()
        headers["X-Durumari-Client-Version"] = (
            current_version if parse_version(current_version) else "unknown"
        )
        if project_ref is not None:
            # An account connection names its conversation's project per request.
            if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", project_ref):
                raise AdapterError("invalid_project_reference")
            headers["X-Durumari-Project-Ref"] = project_ref
        if self.via in ("mcp", "cli"):
            headers["X-Durumari-Via"] = self.via
        if token is not None:
            headers["Authorization"] = scheme + " " + token.get_secret_value()
        if idempotency_key:
            if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", idempotency_key):
                raise AdapterError("invalid_event_id")
            headers["Idempotency-Key"] = idempotency_key
        request = Request(
            self.origin + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers=headers,
            method=method or ("POST" if body is not None else "GET"),
        )
        # A public import has a 30s provider budget, followed by authorization
        # and publication. Keep other operations on their usual short timeout.
        # Analysis and execution of an import read or write a whole slice of the file.
        slow_import = (
            request.get_method() == "POST"
            and "/issue-imports/" in path
            and path.rsplit("/", 1)[-1] in {"complete", "analyze", "run"}
        )
        timeout = (
            120
            if slow_import
            else 60
            if request.get_method() == "POST"
            and (
                re.fullmatch(
                    r"/api/v2/agent/skills/(?:[a-f0-9-]{36}/)?import-(?:git|npm|zip)", path
                )
                or (
                    path == "/api/v2/agent/account/skills/mutations"
                    and isinstance(body, dict)
                    and body.get("action") == "import_git"
                )
            )
            else 10
        )
        replayable = request.get_method() == "GET" or idempotency_key is not None
        try:
            with self._open(request, timeout, replayable) as response:
                if response.status == 204:
                    return {}
                data = response.read(MAX_PROJECT_RESPONSE_BYTES + 1)
                if len(data) > MAX_PROJECT_RESPONSE_BYTES:
                    raise AdapterError("project_response_too_large")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise AdapterError("invalid_project_response")
                return value
        except HTTPError as error:
            if error.code == 429 and path in {
                "/api/v2/agent/file-uploads",
                "/api/v2/agent/file-uploads/status",
            }:
                try:
                    data = error.read(16001)
                    value = json.loads(data) if len(data) <= 16000 else {}
                    detail = value.get("error") if isinstance(value, dict) else None
                    if isinstance(detail, dict) and detail.get("code") in {
                        "upload_rate_limited",
                        "upload_compressor_busy",
                    }:
                        raise ContextFailure(429, detail)
                except (ValueError, UnicodeError):
                    pass  # Ingress 429 may be HTML rather than the API envelope.
                raise AdapterError("project_rate_limited") from None
            # 426: nothing was applied; connections, credentials and queued writes stay.
            if path.startswith("/api/v2/") and error.code in {
                400,
                401,
                403,
                404,
                409,
                410,
                422,
                426,
            }:
                try:
                    data = error.read(16001)
                    value = json.loads(data) if len(data) <= 16000 else {}
                    details = value.get("error", {}) if isinstance(value, dict) else {}
                    raise ContextFailure(error.code, details if isinstance(details, dict) else {})
                except (ValueError, UnicodeError):
                    raise AdapterError("invalid_project_response") from None
            if error.code == 426:
                raise AdapterError("server_requires_client_update") from None
            if error.code == 404 and path == "/api/v1/project-agent/refresh/prepare":
                raise AdapterError("project_refresh_not_supported") from None
            if error.code in {401, 403, 404, 410}:
                raise AdapterError("project_access_denied") from None
            if error.code in {400, 409, 422}:
                raise AdapterError("project_event_rejected") from None
            busy = getattr(error, "durumari_busy", None)
            if busy is not None:
                # Retries are spent: say how long to wait instead of a bare refusal.
                raise ContextFailure(error.code, busy) from None
            if error.code == 429:
                raise AdapterError("project_rate_limited") from None
            raise AdapterError("project_unavailable") from None
        except (URLError, TimeoutError, OSError):
            raise AdapterError("project_delivery_unknown") from None
        except (ValueError, UnicodeError):
            raise AdapterError("invalid_project_response") from None


class ProjectGateway:
    def __init__(self, name: str, client: str, *, root: Path | None = None) -> None:
        self.name, self.client = name, client
        self.root = root or state_root()
        profile = load_project_profile(name)
        if profile.disabled:
            raise AdapterError("project_removed")
        profile.check_client(client)
        profile.check_binding()
        self.transport = ProjectTransport(profile.gateway_origin)
        self.profile = profile

    def _refresh_journal(self, profile: ProjectProfile, *, begin: bool = False) -> bool:
        # This independent encrypted journal is under the credentials lock, never
        # the capture queue lock. It remains durable even if OS-store writes fail
        # after the server has consumed a refresh token.
        directory = self.root / (self.name + ".credentials")
        path = directory / "refresh.enc"
        cipher = Fernet(profile.outbox_key.get_secret_value().encode("ascii"))
        fingerprint = hashlib.sha256(profile.refresh_token.get_secret_value().encode()).hexdigest()
        if path.exists():
            try:
                if path.is_symlink() or path.stat().st_size > 4096:
                    raise ValueError
                previous = json.loads(cipher.decrypt(path.read_bytes()))
                if previous["connection_id"] != profile.connection_id:
                    raise ValueError
                if previous["refresh_fingerprint"] == fingerprint:
                    if previous.get("version") == 2:
                        return True
                    raise AdapterError("project_login_required")
                # A different protected token means replacement was durably saved.
                path.unlink()
            except AdapterError:
                raise
            except Exception:
                raise AdapterError("project_refresh_journal_invalid") from None
        if begin:
            temporary = directory / ("refresh." + uuid4().hex + ".tmp")
            payload = cipher.encrypt(
                json.dumps(
                    {"connection_id": profile.connection_id, "refresh_fingerprint": fingerprint}
                ).encode()
            )
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
                if os.name != "nt":
                    directory_fd = os.open(directory, os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
            finally:
                temporary.unlink(missing_ok=True)
        return False

    def _profile(self) -> ProjectProfile:
        profile = load_project_profile(self.name)
        if profile.disabled:
            raise AdapterError("project_removed")
        profile.check_client(self.client)
        if (profile.gateway_origin, profile.connection_id) != (
            self.profile.gateway_origin,
            self.profile.connection_id,
        ):
            raise AdapterError("project_profile_changed")
        if profile.refresh_uncertain:
            raise AdapterError("project_login_required")
        for _ in range(2):
            pending = self._refresh_journal(profile)
            if not pending and profile.access_expires_at > datetime.now(UTC) + timedelta(
                seconds=30
            ):
                self.profile = profile
                return profile
            from durumari_mcp.project_rotation import rotate

            try:
                replacement = rotate(
                    profile,
                    self.transport,
                    self.root / (self.name + ".credentials") / "refresh.enc",
                )
            except AdapterError as error:
                if str(error) != "project_refresh_not_supported":
                    raise
                profile = self._legacy_refresh(profile)
            else:
                save_project_profile(self.name, replacement)
                profile = replacement
                self._refresh_journal(profile)
        if profile.access_expires_at <= datetime.now(UTC) + timedelta(seconds=30):
            raise AdapterError("project_connection_unavailable")
        self.profile = profile
        return profile

    def _legacy_refresh(self, profile: ProjectProfile) -> ProjectProfile:
        # Older servers keep their established one-use refresh recovery policy.
        if profile.access_expires_at <= datetime.now(UTC) + timedelta(seconds=30):
            self._refresh_journal(profile, begin=True)
            try:
                result = self.transport.request(
                    "/api/v1/project-agent/refresh",
                    body={"refresh_token": profile.refresh_token.get_secret_value()},
                )
                connection = validate_connection(result.get("connection"), profile)
                replacement = profile.model_copy(
                    update={
                        "access_token": SecretStr(result["access_token"]),
                        "refresh_token": SecretStr(result["refresh_token"]),
                        "access_expires_at": datetime.fromisoformat(result["access_expires_at"]),
                    }
                )
                replacement = ProjectProfile.model_validate_json(replacement.protected_json())
                validate_connection(connection.model_dump(), replacement)
                save_project_profile(self.name, replacement)
                profile = replacement
                self._refresh_journal(profile)
            except AdapterError as error:
                if str(error) not in {
                    "project_access_denied",
                    "project_event_rejected",
                    "project_rate_limited",
                }:
                    save_project_profile(
                        self.name, profile.model_copy(update={"refresh_uncertain": True})
                    )
                else:
                    # Explicit pre-commit denials may be retried after correction.
                    (self.root / (self.name + ".credentials") / "refresh.enc").unlink(
                        missing_ok=True
                    )
                raise
            except Exception:
                # A successful rotation with an invalid response cannot safely be retried.
                save_project_profile(
                    self.name, profile.model_copy(update={"refresh_uncertain": True})
                )
                raise AdapterError("project_login_required") from None
        self.profile = profile
        return profile

    def request(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        action: str | None = "read",
        method: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if not path.startswith("/api/v1/project-agent/"):
            raise AdapterError("invalid_project_operation")
        profile = load_project_profile(self.name)
        profile.check_client(self.client)
        profile.check_binding()
        if (profile.gateway_origin, profile.connection_id) != (
            self.profile.gateway_origin,
            self.profile.connection_id,
        ):
            raise AdapterError("project_profile_changed")
        if profile.refresh_uncertain:
            raise AdapterError("project_login_required")
        journal = self.root / (self.name + ".credentials") / "refresh.enc"
        if journal.exists() or profile.access_expires_at <= datetime.now(UTC) + timedelta(
            seconds=30
        ):
            # Only rotation shares a lock; requests and connection checks overlap.
            with process_lock(self.root / (self.name + ".credentials")):
                profile = self._profile()
        self.profile = profile
        current = validate_connection(
            self.transport.request("/api/v1/project-agent/connection", token=profile.access_token),
            profile,
        )
        if action is not None and action not in current.actions:
            raise AdapterError("project_action_denied")
        if path == "/api/v1/project-agent/connection":
            return current.model_dump(mode="json")
        return self.transport.request(
            path,
            body=body,
            token=profile.access_token,
            method=method,
            idempotency_key=idempotency_key,
        )

    def context_request(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        profile = load_project_profile(self.name)
        if profile.disabled:
            raise AdapterError("project_removed")
        profile.check_client(self.client)
        profile.check_workspace(os.getcwd())
        if not profile.context_only:
            raise AdapterError("agent_upgrade_required")
        if (profile.gateway_origin, profile.connection_id) != (
            self.profile.gateway_origin,
            self.profile.connection_id,
        ):
            raise AdapterError("project_profile_changed")
        prefix = "/api/v2/projects/" + profile.project_id + "/contexts"
        route = path.split("?", 1)[0]
        valid = (
            re.fullmatch(
                re.escape(prefix) + r"(?:/(?:search|changes|operations/[A-Za-z0-9_.:-]{1,128}|"
                r"meeting-transcripts/(?:search|sessions/[A-Za-z0-9_:-][A-Za-z0-9_.:-]{0,127})|"
                r"[A-Za-z0-9-]{1,64}(?:/(?:history|restore|related|attachments|sprint-close))?))?",
                route,
            )
            or (route == prefix + "/work-logs/session" and method == "GET")
            or (
                route == "/api/v2/projects/" + profile.project_id + "/document-types"
                and method == "GET"
            )
            or re.fullmatch(
                re.escape("/api/v2/projects/" + profile.project_id + "/issue-imports")
                + r"(?:/[A-Za-z0-9-]{1,64}(?:/(?:chunks/[0-9]{1,5}|complete|analyze|mapping|"
                r"approve|run|retry|cancel))?)?",
                route,
            )
            or (
                method == "POST"
                and re.fullmatch(
                    r"/api/v2/agent/work-logs/(?:list|read|revision|create|update|import|operation)",
                    route,
                )
            )
            or re.fullmatch(r"/api/v2/agent/proposals/(?:create|list|read|apply|status)", route)
            or route == "/api/v2/agent/connection"
            or route == "/api/v2/agent/glossary"
            or route in {"/api/v2/agent/file-uploads", "/api/v2/agent/file-uploads/status"}
            or re.fullmatch(
                r"/api/v2/agent/requests/"
                r"(?:participants|list|read|create|note|revise|respond|decide)",
                route,
            )
            or re.fullmatch(
                r"/api/v2/agent/skills(?:/(?:required|import-(?:git|npm|zip)|principals)"
                r"|/devices(?:/[A-Za-z0-9-]{36}/(?:challenges|deliveries))?"
                r"|/[a-f0-9-]{36}(?:/(?:versions(?:/[0-9a-z.-]{1,40})?|installations|metadata|"
                r"selection|import-(?:git|npm|zip)|secrets(?:/[a-f0-9-]{36})?))?)?",
                route,
            )
            or re.fullmatch(
                r"/api/v2/agent/secrets(?:/(?:operations(?:/[a-f0-9-]{36})?|[a-f0-9-]{36}))?", route
            )
            or re.fullmatch(
                r"/api/v2/agent/secret-devices(?:/[a-f0-9-]{36}/(?:challenges|deliveries))?",
                route,
            )
        )
        if not valid or method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise AdapterError("invalid_project_operation")
        if profile.refresh_uncertain:
            raise AdapterError("project_login_required")
        journal = self.root / (self.name + ".credentials") / "refresh.enc"
        if journal.exists() or profile.access_expires_at <= datetime.now(UTC) + timedelta(
            seconds=30
        ):
            with process_lock(self.root / (self.name + ".credentials")):
                profile = self._profile()
        self.profile = profile
        try:
            return self.transport.request(
                path,
                body=body,
                token=profile.access_token,
                method=method,
                idempotency_key=idempotency_key,
            )
        except ContextFailure as error:
            if error.status != 401:
                raise
            # Another chat may rotate after this request loads its credential.
            # Retry only a durably stored successor for the identical scoped request.
            successor = load_project_profile(self.name)
            if successor.disabled:
                raise AdapterError("project_removed") from None
            successor.check_client(self.client)
            successor.check_workspace(os.getcwd())
            if (
                successor.connection_id != profile.connection_id
                or successor.gateway_origin != profile.gateway_origin
                or successor.access_token == profile.access_token
            ):
                raise
            self.profile = successor
            return self.transport.request(
                path,
                body=body,
                token=successor.access_token,
                method=method,
                idempotency_key=idempotency_key,
            )

    def handoff(
        self,
        after: int = 0,
        through_cursor: int | None = None,
        work_after: str = "",
    ) -> dict[str, Any]:
        if isinstance(after, bool) or after < 0:
            raise AdapterError("invalid_handoff_cursor")
        # Provenance attestation can be costly for large imported documents.
        # Keep each page within the bounded transport timeout; callers follow cursors.
        parameters = {"after": after, "limit": 1}
        if not re.fullmatch(r"[A-Za-z0-9-]{0,36}", work_after):
            raise AdapterError("invalid_work_cursor")
        if through_cursor is not None:
            if isinstance(through_cursor, bool) or through_cursor < after:
                raise AdapterError("invalid_handoff_cursor")
            parameters["through_cursor"] = through_cursor
        result = self.request(
            "/api/v1/project-agent/handoff?" + urlencode({**parameters, "work_after": work_after})
        )
        if result.get("project_id") != self.profile.project_id:
            raise AdapterError("handoff_scope_mismatch")
        required = {
            "server_time",
            "from_cursor",
            "through_cursor",
            "next_cursor",
            "has_more",
            "coverage",
            "capture_status",
            "decisions",
            "actions",
            "contexts",
            "records",
            "omissions",
            "connection_id",
            "work",
            "work_from_after",
            "work_next_after",
        }
        if not required <= result.keys():
            raise AdapterError("invalid_handoff_response")
        if result.get("connection_id") != self.profile.connection_id:
            raise AdapterError("handoff_connection_mismatch")
        return {"trust": "untrusted_source_material", **result}


def validate_connection(
    value: Any, profile: ProjectProfile, *, allow_expired_snapshot: bool = False
) -> ProjectConnection:
    try:
        row = ProjectConnection.model_validate(value)
    except Exception:
        raise AdapterError("invalid_project_connection") from None
    if (row.id, row.project_id, row.source_id, row.actor_id, row.host) != (
        profile.connection_id,
        profile.project_id,
        profile.source_id,
        profile.actor_id,
        profile.client,
    ):
        raise AdapterError("project_connection_scope_changed")
    if (
        row.state != "active"
        or row.expires_at.tzinfo is None
        or (not allow_expired_snapshot and row.expires_at <= datetime.now(UTC))
    ):
        raise AdapterError("project_connection_unavailable")
    if row.provider != {"codex": "openai", "claude": "anthropic", "mcp": "mcp"}.get(profile.client):
        raise AdapterError("project_provider_mismatch")
    if profile.context_only and set(row.actions) != {"context:read", "context:write"}:
        raise AdapterError("project_action_denied")
    return row
