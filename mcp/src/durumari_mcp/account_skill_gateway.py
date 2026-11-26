"""Account Skills keep an immutable target and a separate encrypted request namespace."""

import hashlib
import json
import re
from typing import Annotated, Any, ClassVar, Literal, cast
from urllib.parse import parse_qsl, urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from durumari_mcp.account_credentials import UUID_PATTERN
from durumari_mcp.capture import contains_sensitive
from durumari_mcp.context_outbox import ContextOutbox
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_gateway import ContextFailure, spent_busy
from durumari_mcp.project_skills import bundle_digest

ROOT = "/api/v2/agent/skills"
ACCOUNT_ROOT = "/api/v2/agent/account/skills"
UUID = UUID_PATTERN.removeprefix("^").removesuffix("$")
VERSION = r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9-]+(?:\.[a-z0-9-]+)*)?"
KEY = r"[A-Za-z0-9_.:-]{1,128}"
# The server's account Skill actions; sharing and required stay on the web screen.
Action = Literal["publish", "metadata", "select", "delete", "link", "unlink", "import_git"]


class SkillSource(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    repository_url: str = Field(min_length=1, max_length=2048)
    commit: str = Field(pattern=r"^(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})$")
    path: str = Field(default="", max_length=200)
    requested_ref: str | None = Field(default=None, min_length=1, max_length=200)
    transport: Literal["agent_checkout", "public_api"]

    @model_validator(mode="after")
    def value_free(self) -> "SkillSource":
        parsed = urlsplit(self.repository_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or not parsed.path.strip("/")
            or parsed.query
            or parsed.fragment
            or any(ord(char) <= 32 or ord(char) == 127 for char in self.repository_url)
            or self.path.startswith("/")
            or "\\" in self.path
            or ":" in self.path
            or bool(self.path)
            and any(part in {"", ".", ".."} for part in self.path.split("/"))
            or any(ord(char) < 32 or ord(char) == 127 for char in self.path)
            or self.requested_ref is not None
            and (
                not self.requested_ref.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in self.requested_ref)
            )
            or contains_sensitive(self.model_dump())
        ):
            raise ValueError("invalid source")
        return self


def _relative(path: str) -> str:
    if (
        path.startswith("/")
        or "\\" in path
        or ":" in path
        or bool(path)
        and any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ValueError("invalid source")
    return path


RelativePath = Annotated[str, Field(max_length=200), AfterValidator(_relative)]


class NpmSkillSource(BaseModel):
    """A public npm tarball the server verified by its sha512 integrity."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    transport: Literal["npm_registry"]
    registry_url: Literal["https://registry.npmjs.org"]
    package: str = Field(
        max_length=214, pattern=r"^(?:@[a-z0-9][a-z0-9._-]{0,99}/)?[a-z0-9][a-z0-9._-]{0,99}$"
    )
    version: str = Field(min_length=5, max_length=64)
    integrity: str = Field(pattern=r"^sha512-[A-Za-z0-9+/]{86}==$")
    tarball_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    path: RelativePath = ""


class ArchiveSkillSource(BaseModel):
    """A zip archive a person uploaded; the server hashed the bytes it read."""

    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    transport: Literal["zip_upload"]
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    size_bytes: int = Field(ge=1)
    path: RelativePath = ""


class SkillReceipt(BaseModel):
    model_config = ConfigDict(extra="ignore", hide_input_in_errors=True)
    operation_id: str = Field(pattern=UUID_PATTERN)
    project_ref: str = Field(pattern=UUID_PATTERN)
    request_key: str = Field(pattern="^" + KEY + "$")
    project_id: str = Field(pattern=UUID_PATTERN)
    action: Action
    state: Literal["prepared", "completed", "failed"]
    skill_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    secret_id: str | None = Field(default=None, pattern=UUID_PATTERN)
    revision: int | None = Field(default=None, ge=1, strict=True)
    policy_revision: int | None = Field(default=None, ge=1, strict=True)
    version: str | None = Field(default=None, pattern="^" + VERSION + "$", max_length=40)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    source: (
        Annotated[
            SkillSource | NpmSkillSource | ArchiveSkillSource, Field(discriminator="transport")
        ]
        | None
    ) = None
    error_code: str | None = Field(default=None, pattern=r"^[a-z_]{1,100}$")

    @model_validator(mode="before")
    @classmethod
    def no_bundle_content(cls, value: Any) -> Any:
        # Other added fields are ignored; bundle content in a receipt is still rejected.
        if isinstance(value, dict) and {"files", "content", "body"} & value.keys():
            raise ValueError("bundle content in receipt")
        return value


class SkillOutbox(ContextOutbox):
    PREFIX_PATTERN: ClassVar[str] = re.escape(ACCOUNT_ROOT + "/mutations")
    REQUEST_SUFFIX_PATTERN: ClassVar[str] = ""
    # ReleaseInput caps ensure_ascii=False JSON at 768000 bytes. The remainder
    # bounds the fixed action/target and queue envelope without widening Context.
    MAX_PAYLOAD_BYTES: ClassVar[int] = 800_000

    def expected(self, key: str) -> dict[str, Any] | None:
        """Read the original encrypted envelope without claiming or delivering it."""
        with self.transaction() as database:
            row = database.execute(
                "SELECT payload FROM requests WHERE request_key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            value = json.loads(self.cipher.decrypt(row[0]))
            body = value.get("body")
            if not isinstance(body, dict):
                raise AdapterError("invalid_skill_receipt")
            return body


class AccountSkillGateway:
    account_scoped = True
    supports_operation_recovery = True

    def __init__(self, account: Any, project_ref: str | None = None) -> None:
        if project_ref is not None and not re.fullmatch(UUID_PATTERN, project_ref):
            raise AdapterError("invalid_project_reference")
        self._account, self.project_ref = account, project_ref
        self.name, self.client = account.name, account.client

    def targeted(self, project_ref: str) -> "AccountSkillGateway":
        return AccountSkillGateway(self._account, project_ref)

    def _ref(self) -> str:
        if self.project_ref is None:
            raise AdapterError("project_selection_required")
        return self.project_ref

    @property
    def project_id(self) -> str:
        return str(self._account.targeted(self._ref())._resolve())

    def _queue(self, project_id: str) -> SkillOutbox:
        profile = self._account.profile
        scope = [
            "account-skills-v1",
            profile.gateway_origin,
            profile.connection_id,
            profile.actor_id,
            profile.client,
            self._ref(),
            project_id,
        ]
        name = (
            "skills-"
            + hashlib.sha256(
                (self.name + ":" + profile.connection_id + ":" + self._ref()).encode()
            ).hexdigest()[:48]
        )
        return cast(
            SkillOutbox,
            SkillOutbox.from_scope(
                self._account.root,
                name,
                scope=scope,
                outbox_key=profile.outbox_key.get_secret_value(),
                prefix=ACCOUNT_ROOT + "/mutations",
            ),
        )

    def _receipt(
        self,
        value: dict[str, Any],
        key: str,
        *,
        project_id: str | None = None,
        action: str | None = None,
        skill_id: str | None = None,
        secret_id: str | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            result = SkillReceipt.model_validate(value)
            if (
                (result.project_ref, result.request_key) != (self._ref(), key)
                or (project_id is not None and result.project_id != project_id)
                or (action is not None and result.action != action)
                or (skill_id is not None and result.skill_id != skill_id)
                or (secret_id is not None and result.secret_id != secret_id)
            ):
                raise ValueError
            if result.state == "completed" and body is not None:
                if result.action in {
                    "publish",
                    "select",
                    "import_git",
                } and result.version != body.get("version"):
                    raise ValueError
                if result.action == "publish":
                    normalized = {
                        **body,
                        "requirements": [
                            {
                                "name": item["name"],
                                "kind": item["kind"],
                                "required": item.get("required", True),
                            }
                            for item in body.get("requirements", [])
                        ],
                    }
                    if result.sha256 != bundle_digest(normalized):
                        raise ValueError
            return result.model_dump(exclude_none=True)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise AdapterError("invalid_skill_receipt") from None

    @staticmethod
    def _key(value: str | None) -> str:
        if value is None:
            raise AdapterError("account_skill_request_key_required")
        if re.fullmatch(KEY, value) is None:
            raise AdapterError("invalid_skill_request")
        return value

    @staticmethod
    def _mutation(method: str, suffix: str) -> tuple[str, str | None, str | None]:
        if method == "POST" and suffix == "":
            return "publish", None, None
        if method == "POST" and suffix == "/import-git":
            return "import_git", None, None
        match = re.fullmatch(
            r"/(" + UUID + r")(?:(/metadata|/selection|/import-git)|/secrets/(" + UUID + r"))?",
            suffix,
        )
        if match:
            identifier, operation, secret = match.groups()
            if secret and method in {"PATCH", "DELETE"}:
                return ("link" if method == "PATCH" else "unlink"), identifier, secret
            actions = {
                ("PATCH", "/metadata"): "metadata",
                ("PATCH", "/selection"): "select",
                ("DELETE", None): "delete",
                ("POST", "/import-git"): "import_git",
            }
            action = actions.get((method, operation))
            if action:
                return action, identifier, None
        raise AdapterError("invalid_skill_request")

    def request(
        self,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        request_key: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment or not parsed.path.startswith(ROOT):
            raise AdapterError("invalid_skill_request")
        suffix = parsed.path[len(ROOT) :]
        if method == "GET" and body is None:
            if (
                re.fullmatch(
                    r"(?:/required|/principals|/"
                    + UUID
                    + r"(?:/versions(?:/"
                    + VERSION
                    + r")?|/secrets)?)?",
                    suffix,
                )
                is None
            ):
                raise AdapterError("invalid_skill_request")
            pairs = parse_qsl(parsed.query, keep_blank_values=True)
            query = dict(pairs)
            if len(pairs) != len(query) or set(query) - {
                "q",
                "permission",
                "requires_secret",
                "tag",
                "required",
                "owned",
                "recommended",
                "after",
                "limit",
            }:
                raise AdapterError("invalid_skill_request")
            typed_query: dict[str, Any] = dict(query)
            if "limit" in query:
                try:
                    typed_query["limit"] = int(query["limit"])
                except ValueError:
                    raise AdapterError("invalid_skill_request") from None
            for flag in ("requires_secret", "required", "owned", "recommended"):
                if flag in query:
                    if query[flag] not in {"true", "false"}:
                        raise AdapterError("invalid_skill_request")
                    typed_query[flag] = query[flag] == "true"
            _ = self.project_id
            return dict(
                self._account.request(
                    ACCOUNT_ROOT + "/read",
                    body={"project_ref": self._ref(), "path": suffix, "query": typed_query},
                )
            )
        installed = re.fullmatch(r"/(" + UUID + r")/installations", suffix)
        if method == "POST" and installed and not parsed.query and isinstance(body, dict):
            if set(body) != {"client", "version", "sha256"}:
                raise AdapterError("invalid_skill_request")
            _ = self.project_id
            return dict(
                self._account.request(
                    ACCOUNT_ROOT + "/installations",
                    body={
                        "project_ref": self._ref(),
                        "skill_id": installed[1],
                        **body,
                    },
                )
            )
        if parsed.query:
            raise AdapterError("invalid_skill_request")
        key = self._key(request_key)
        action, skill_id, secret_id = self._mutation(method, suffix)
        if body is None or contains_sensitive(body):
            raise AdapterError("sensitive_skill_rejected")
        project_id = self.project_id
        queue = self._queue(project_id)
        envelope = {
            "action": action,
            "body": body,
            **({"skill_id": skill_id} if skill_id else {}),
            **({"secret_id": secret_id} if secret_id else {}),
        }
        owner = queue.enqueue(key, self._account.session, "POST", queue.prefix, envelope)
        stored = queue.result(key)
        if stored is not None:
            return self._receipt(
                stored,
                key,
                project_id=project_id,
                action=action,
                skill_id=skill_id,
                secret_id=secret_id,
                body=body,
            )
        rejected = queue.rejection(key)
        if rejected is not None:
            return {
                "state": "rejected",
                "request_key": key,
                "project_ref": self._ref(),
                "error": rejected,
            }
        item = queue.claim(owner, key=key)
        if item is None:
            return {"state": "pending", "request_key": key, "project_ref": self._ref()}
        try:
            result = self._account.request(
                ACCOUNT_ROOT + "/mutations",
                body={"project_ref": self._ref(), "request_key": key, **item["body"]},
            )
            receipt = self._receipt(
                result,
                key,
                project_id=project_id,
                action=action,
                skill_id=skill_id,
                secret_id=secret_id,
                body=body,
            )
            if receipt["state"] == "prepared":
                queue.unknown(key, item["lease"])
            else:
                queue.complete(key, item["lease"], receipt)
            return receipt
        except ContextFailure as error:
            if error.status == 426:
                queue.unknown(key, item["lease"])
                raise
            if spent_busy(error):
                # Never processed: keep the exact request queued for the same key.
                queue.unknown(key, item["lease"])
                return {
                    "state": "pending",
                    "request_key": key,
                    "project_ref": self._ref(),
                    "error": error.error,
                    "next_action": "retry_same_request_after_wait",
                }
            queue.reject(key, item["lease"], error.error)
            return {
                "state": "conflict" if error.status == 409 else "rejected",
                "request_key": key,
                "project_ref": self._ref(),
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
                    "state": "unknown",
                    "request_key": key,
                    "project_ref": self._ref(),
                    "next_action": "check_skill_operation_or_retry_same_request",
                }
            raise

    def operation(self, request_key: str) -> dict[str, Any]:
        key = self._key(request_key)
        try:
            result = self._account.request(
                ACCOUNT_ROOT + "/operations", body={"project_ref": self._ref(), "request_key": key}
            )
        except ContextFailure as error:
            if error.status != 404:
                raise
            return {"state": "unknown", "request_key": key, "project_ref": self._ref()}
        receipt = self._receipt(result, key)
        queue = self._queue(receipt["project_id"])
        expected = queue.expected(key)
        if expected is not None:
            receipt = self._receipt(
                receipt,
                key,
                action=expected["action"],
                skill_id=expected.get("skill_id"),
                secret_id=expected.get("secret_id"),
                body=expected.get("body"),
            )
        if receipt["state"] in {"completed", "failed"}:
            queue.confirm(key, receipt)
        return receipt

    def local(self) -> Any:
        from durumari_mcp.account_skill_install import local_gateway

        _ = self.project_id
        return local_gateway(self)
