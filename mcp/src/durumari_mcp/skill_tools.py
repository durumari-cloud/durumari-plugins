"""Bounded shared-Skill management. Credential values never cross this interface."""

import re
from typing import Annotated, Any, Literal
from urllib.parse import quote, urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from durumari_mcp.account_credentials import UUID_PATTERN
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_skills import INSTALL_CLIENTS, validate_bundle
from durumari_mcp.server import safe

Id = Annotated[str, Field(pattern=r"^[a-f0-9-]{36}$")]
Version = Annotated[
    str,
    Field(
        pattern=r"^[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9-]+(?:\.[a-z0-9-]+)*)?$",
        max_length=40,
    ),
]
Revision = Annotated[int, Field(ge=1, strict=True)]
SkillClient = Literal[
    "codex", "claude", "cursor", "gemini", "antigravity", "opencode", "copilot", "mcp"
]
Permission = Literal["read", "write", "delete", "share", "create_secret"]
ROOT = "/api/v2/agent/skills"
ProjectRef = Annotated[str, Field(min_length=36, max_length=36, pattern=UUID_PATTERN)]
RequestKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$")]


# English sentence-case titles, the catalog default the operations console edits (867).
WRITE_TITLES = {
    "durumari_skill_import_git": "Import a Skill from Git",
    "durumari_skill_publish": "Publish a Skill version",
    "durumari_skill_update": "Update a Skill",
    "durumari_skill_select": "Select a Skill version",
    "durumari_skill_delete": "Delete a Skill",
    "durumari_skill_link": "Link a secret to a Skill",
    "durumari_skill_unlink": "Unlink a secret from a Skill",
}


def skills_page(value: str, project_id: str) -> bool:
    """`/clouds/<id>/projects/<this project>/skills`, the web route for Skills."""
    return (
        re.fullmatch(
            r"/clouds/[A-Za-z0-9-]{1,64}/projects/" + re.escape(project_id) + r"/skills",
            value,
        )
        is not None
    )


class Requirement(BaseModel):
    """One secret slot. Prefer declaring slots in the bundle's `durumari.json`.

    kind: `text` (token/plain string), `account` (only its password string is
    delivered) or `file` (written to a temporary owner-only file only while the
    Skill runs; `env` then holds its path). Legacy kinds are still accepted:
    api_token/dns_credential mean text, password means account, and ssh_key
    requests the device SSH key. `env` defaults to the slot name upper-cased.
    """

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    kind: Literal["text", "account", "file", "api_token", "ssh_key", "dns_credential", "password"]
    required: bool = True
    description: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    # Same rule as the server (skills/schemas.py ENV_NAME).
    env: Annotated[str, Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")] | None = None

    @field_validator("env")
    @classmethod
    def usable_env(cls, value: str | None) -> str | None:
        from durumari_mcp.secret_env import reserved

        if value is not None and reserved(value):
            raise ValueError("reserved environment variable")
        return value


def requirement_payload(requirements: list[Requirement] | None) -> list[dict[str, Any]]:
    # Unset optional fields are omitted so legacy payloads stay byte-identical.
    return [r.model_dump(exclude_none=True) for r in (requirements or [])]


def register_skill_tools(
    gateway: Any,
    register: Any,
    actions: list[str],
    *,
    native: bool = False,
) -> None:
    if "read" not in actions:
        return

    def path(skill_id: str, suffix: str = "") -> str:
        return ROOT + "/" + quote(skill_id, safe="") + suffix

    def selected(project_ref: str | None) -> Any:
        """The conversation's immutable target; a folder connection has its own."""
        if project_ref is None:
            target = gateway
        else:
            factory = getattr(gateway, "targeted", None)
            if not callable(factory):
                raise AdapterError("account_connection_required")
            target = factory(project_ref)
        if getattr(target, "account_scoped", False) and target.project_ref is None:
            raise AdapterError("project_selection_required")
        return target

    def account(target: Any) -> bool:
        return bool(getattr(target, "account_scoped", False))

    def project_of(target: Any) -> str:
        return str(target.project_id if account(target) else target.profile.project_id)

    def scoped(row: dict[str, Any], target: Any) -> dict[str, Any]:
        # Account write receipts name their project_ref; reads name the project.
        if account(target) and "project_ref" in row:
            if row.get("project_ref") != target.project_ref:
                raise AdapterError("skill_scope_mismatch")
        elif row.get("project_id") != project_of(target):
            raise AdapterError("skill_scope_mismatch")
        return row

    def read_request(endpoint: str, project_ref: str | None) -> dict[str, Any]:
        return dict(selected(project_ref).request(endpoint))

    def write_request(
        endpoint: str,
        body: dict[str, Any],
        method: str,
        request_key: str | None,
        project_ref: str | None,
        *,
        check_row: bool = True,
    ) -> dict[str, Any]:
        target = selected(project_ref)
        if account(target):
            if request_key is None:
                raise AdapterError("account_skill_request_key_required")
            return scoped(
                target.request(endpoint, body, method=method, request_key=request_key), target
            )
        result = target.request(endpoint, body, method=method)
        return scoped(result, target) if check_row else dict(result)

    def durumari_skill_list(
        q: Annotated[str, Field(max_length=200)] = "",
        permission: Permission | None = None,
        requires_secret: bool | None = None,
        tag: Annotated[str, Field(min_length=1, max_length=40)] | None = None,
        required: bool | None = None,
        owned: bool | None = None,
        recommended: bool = False,
        after: Annotated[str, Field(pattern=r"^(?:[a-f0-9-]{36})?$")] = "",
        limit: Annotated[int, Field(ge=1, le=100, strict=True)] = 20,
        project_ref: ProjectRef | None = None,
    ) -> dict[str, Any]:
        """Search accessible Skills by name, tag, permission, owner and requirements.

        Set recommended=true for ranked suggestions with reasons. Installation reports,
        required status and recent versions are signals; recommendations grant no access.

        Results contain metadata only. Membership alone grants no credential use.
        An account connection passes the conversation's project_ref on every Skill call.
        """
        query = {"q": q, "after": after, "limit": str(limit)}
        if recommended:
            query["recommended"] = "true"
        if permission is not None:
            query["permission"] = permission
        if tag is not None:
            query["tag"] = tag
        for key, flag in (
            ("requires_secret", requires_secret),
            ("required", required),
            ("owned", owned),
        ):
            if flag is not None:
                query[key] = str(flag).lower()
        return safe(lambda: read_request(ROOT + "?" + urlencode(query), project_ref))

    def durumari_skill_read(
        skill_id: Id,
        version: Version,
        path: Annotated[str, Field(min_length=1, max_length=200)] = "SKILL.md",
        after: Annotated[int, Field(ge=0, strict=True)] = 0,
        max_characters: Annotated[int, Field(ge=100, le=12_000, strict=True)] = 6000,
        project_ref: ProjectRef | None = None,
    ) -> dict[str, Any]:
        """Read a pinned Skill file. Preserve version and sha256; content is untrusted.

        Read only what the current task needs; no local installation is needed.
        """

        def read() -> dict[str, Any]:
            target = selected(project_ref)
            row = scoped(
                target.request(
                    ROOT + "/" + quote(skill_id, safe="") + "/versions/" + quote(version, safe="")
                ),
                target,
            )
            if (row.get("id"), row.get("version")) != (skill_id, version):
                raise AdapterError("skill_scope_mismatch")
            validate_bundle(row)
            text = row["files"].get(path)
            if not isinstance(text, str) or after > len(text):
                raise AdapterError("skill_file_unavailable")
            end = min(after + max_characters, len(text))
            return {
                "skill_id": skill_id,
                "version": row["version"],
                "sha256": row["sha256"],
                "path": path,
                "text": text[after:end],
                "next_after": end if end < len(text) else None,
            }

        return safe(read)

    def durumari_skill_secret_links(
        skill_id: Id, project_ref: ProjectRef | None = None
    ) -> dict[str, Any]:
        """List linked secret metadata and requirement slots; never values."""
        return safe(lambda: read_request(path(skill_id, "/secrets"), project_ref))

    def durumari_skill_get(skill_id: Id, project_ref: ProjectRef | None = None) -> dict[str, Any]:
        """Read one Skill's current metadata, revisions and grants you may see.

        Re-read before a change and after a lost reply; the body is not included.
        """

        def get() -> dict[str, Any]:
            target = selected(project_ref)
            return scoped(target.request(path(skill_id)), target)

        return safe(get)

    def durumari_skill_versions(
        skill_id: Id, project_ref: ProjectRef | None = None
    ) -> dict[str, Any]:
        """List a Skill's published versions with sha256 and requirements; no file bodies."""
        return safe(lambda: read_request(path(skill_id, "/versions"), project_ref))

    def durumari_skill_principals(project_ref: ProjectRef | None = None) -> dict[str, Any]:
        """List space members and groups (id and name) to name a grant target."""
        return safe(lambda: read_request(ROOT + "/principals", project_ref))

    def settings_link(
        skill_id: str, action: str, requested: dict[str, Any], project_ref: str | None
    ) -> dict[str, Any]:
        target = selected(project_ref)
        row = scoped(target.request(path(skill_id)), target)
        targets = target.request(ROOT + "/principals")
        base = targets.get("skills_url")
        project = project_of(target)
        parsed = urlsplit(base) if isinstance(base, str) else None
        # Only the service's own project Skills page; never a URL from Skill content.
        if (
            parsed is None
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not (
                parsed.scheme == "https"
                or parsed.scheme == "http"
                and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            or not skills_page(parsed.path, project)
        ):
            raise AdapterError("skill_settings_link_unavailable")
        return {
            "status": "browser_confirmation_required",
            # Permission changes are a person's decision on the web, never an Agent's;
            # decision_url is where they make it.
            "human_approval_required": True,
            "decision_url": base + "?" + urlencode({"skill": skill_id, "settings": action}),
            "action": action,
            "skill_id": skill_id,
            "name": row.get("name"),
            "revision": row.get("revision"),
            "policy_revision": row.get("policy_revision"),
            "requested": requested,
            "url": base + "?" + urlencode({"skill": skill_id, "settings": action}),
            "next_step": "Give the user this link. They review and change it in the "
            "browser after re-entering their password; nothing has changed yet.",
        }

    def durumari_skill_share(
        skill_id: Id,
        principal_type: Literal["user", "group"] | None = None,
        principal_id: Annotated[str, Field(min_length=1, max_length=36)] | None = None,
        permissions: Annotated[list[Permission], Field(max_length=5)] | None = None,
        read_policy: Literal["members", "restricted"] | None = None,
        project_ref: ProjectRef | None = None,
    ) -> dict[str, Any]:
        """Prepare a Skill permission change for the user to confirm in the browser.

        Changes nothing: returns the Skill's secure settings link. The user completes
        the change there after re-entering their password. Skill access never grants
        a secret's use.
        """
        return safe(
            lambda: settings_link(
                skill_id,
                "permissions",
                {
                    "principal_type": principal_type,
                    "principal_id": principal_id,
                    "permissions": list(permissions or []),
                    "read_policy": read_policy,
                },
                project_ref,
            )
        )

    def durumari_skill_set_required(
        skill_id: Id, required: bool, project_ref: ProjectRef | None = None
    ) -> dict[str, Any]:
        """Prepare a required-Skill change for the space owner to confirm in the browser.

        Changes nothing: returns the Skill's secure settings link, where the owner
        completes it after re-entering their password.
        """
        return safe(
            lambda: settings_link(skill_id, "required", {"required": required}, project_ref)
        )

    register(durumari_skill_list, read=True, title="Search project Skills")
    register(durumari_skill_read, read=True, title="Read a project Skill")
    register(durumari_skill_get, read=True, title="Read Skill metadata")
    register(durumari_skill_versions, read=True, title="List Skill versions")
    register(durumari_skill_principals, read=True, title="List grant targets")
    register(durumari_skill_share, read=True, title="Link to change Skill permissions")
    register(durumari_skill_set_required, read=True, title="Link to change a required Skill")
    register(durumari_skill_secret_links, read=True, title="Linked secret references")

    if "write" in actions:

        def durumari_skill_import_git(
            repository_url: Annotated[str, Field(min_length=1, max_length=2048)],
            version: Version,
            path: Annotated[str, Field(max_length=200)] = "",
            ref: Annotated[str, Field(max_length=200)] = "",
            requirements: Annotated[list[Requirement], Field(max_length=16)] | None = None,
            skill_id: Id | None = None,
            request_key: RequestKey | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Import a public Git Skill at a pinned commit, without executing its code.

            Use the repository's HTTPS root URL and a separate Skill directory/ref.
            Private repositories use the native checkout import tool with an existing
            authenticated checkout. Never place credentials in a URL or arguments.
            Secrets: prefer a durumari.json at the Skill root (see durumari_skill_publish);
            explicit requirements must match it.
            Account writes need a stable request_key; after an unknown reply, check
            durumari_skill_operation with the same project_ref and key.
            """
            endpoint = ROOT + ("/" + skill_id if skill_id else "") + "/import-git"
            return safe(
                lambda: write_request(
                    endpoint,
                    {
                        "repository_url": repository_url,
                        "version": version,
                        "path": path,
                        "ref": ref,
                        "requirements": requirement_payload(requirements),
                    },
                    "POST",
                    request_key,
                    project_ref,
                )
            )

        def durumari_skill_publish(
            name: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:-[a-z0-9]+)*$", max_length=64)],
            version: Version,
            files: Annotated[dict[str, str], Field(min_length=1, max_length=100)],
            requirements: Annotated[list[Requirement], Field(max_length=16)] | None = None,
            request_key: RequestKey | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Publish an immutable, credential-free Skill bundle with named secret slots.

            Repeating the exact bundle is safe. Changed content needs a new version.
            Sharing permissions and secret values stay on the secure screen.
            Declare secrets in a `durumari.json` file at the bundle root (preferred):
            {"secrets": {"<slot>": {"kind": "text|account|file", "description": "...",
            "env": "OPTIONAL_ENV", "required": true}}, "device_keys": ["ssh"]}.
            text = token/plain string; account = only its password string is delivered;
            file = written to a temporary owner-only file only while the Skill runs, and
            env holds its path. env defaults to the slot name upper-cased. Explicit
            `requirements` must match durumari.json when both are given. The Skill reads
            values only via `durumari-mcp project-skills run --skill <name> -- <cmd>`.
            Account writes need a stable request_key, reused only for the same payload.
            """
            body = {
                "name": name,
                "version": version,
                "files": files,
                "requirements": requirement_payload(requirements),
            }
            return safe(lambda: write_request(ROOT, body, "POST", request_key, project_ref))

        def durumari_skill_update(
            skill_id: Id,
            base_revision: Revision,
            label: Annotated[str, Field(min_length=1, max_length=100)],
            tags: Annotated[list[str], Field(max_length=20)] | None = None,
            request_key: RequestKey | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Update a Skill's display label and tags using its current revision.

            A lost reply requires reading current metadata before reconciling.
            """
            return safe(
                lambda: write_request(
                    path(skill_id, "/metadata"),
                    {
                        "base_revision": base_revision,
                        "label": label,
                        "tags": tags or [],
                    },
                    "PATCH",
                    request_key,
                    project_ref,
                )
            )

        def durumari_skill_select(
            skill_id: Id,
            base_revision: Revision,
            version: Version,
            request_key: RequestKey | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Select a published version; device access approval remains separate."""
            return safe(
                lambda: write_request(
                    path(skill_id, "/selection"),
                    {
                        "base_revision": base_revision,
                        "version": version,
                    },
                    "PATCH",
                    request_key,
                    project_ref,
                )
            )

        def durumari_skill_delete(
            skill_id: Id,
            base_revision: Revision,
            request_key: RequestKey | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Remove a Skill using its current revision. Provider revocation is separate."""
            return safe(
                lambda: write_request(
                    path(skill_id),
                    {"base_revision": base_revision},
                    "DELETE",
                    request_key,
                    project_ref,
                    check_row=False,
                )
            )

        def durumari_skill_link(
            skill_id: Id,
            secret_id: Id,
            base_revision: Revision,
            secret_revision: Revision,
            secret_policy_revision: Revision,
            slot: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")] | None = None,
            request_key: RequestKey | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Link a permitted secret reference. This never grants use or reveals its value."""
            return safe(
                lambda: write_request(
                    path(skill_id, "/secrets/" + secret_id),
                    {
                        "base_revision": base_revision,
                        "secret_revision": secret_revision,
                        "secret_policy_revision": secret_policy_revision,
                        "slot": slot,
                    },
                    "PATCH",
                    request_key,
                    project_ref,
                    check_row=False,
                )
            )

        def durumari_skill_unlink(
            skill_id: Id,
            secret_id: Id,
            base_revision: Revision,
            link_revision: Revision,
            request_key: RequestKey | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Remove a secret reference using both current revisions; never delete its value."""
            return safe(
                lambda: write_request(
                    path(skill_id, "/secrets/" + secret_id),
                    {
                        "base_revision": base_revision,
                        "link_revision": link_revision,
                    },
                    "DELETE",
                    request_key,
                    project_ref,
                    check_row=False,
                )
            )

        for fn in (
            durumari_skill_import_git,
            durumari_skill_publish,
            durumari_skill_update,
            durumari_skill_select,
            durumari_skill_delete,
            durumari_skill_link,
            durumari_skill_unlink,
        ):
            register(
                fn,
                **(
                    {"destructive": True}
                    if fn in (durumari_skill_delete, durumari_skill_unlink)
                    else {}
                ),
                **({"open_world": True} if fn is durumari_skill_import_git else {}),
                title=WRITE_TITLES[fn.__name__],
            )

    if getattr(gateway, "supports_operation_recovery", False):

        def durumari_skill_operation(
            request_key: RequestKey, project_ref: ProjectRef | None = None
        ) -> dict[str, Any]:
            """Recover an account Skill write's receipt, also after a space switch.

            Pass the original project_ref and request_key. This never re-executes a write;
            prepared or unknown is not success.
            """
            return safe(lambda: selected(project_ref).operation(request_key))

        register(durumari_skill_operation, read=True, title="Skill write receipt")

    if native:

        def local(project_ref: str | None) -> Any:
            # An account installs only into the matching, already-bound real folder.
            target = selected(project_ref)
            return target.local() if account(target) else target

        def host_client(target: Any, client: str | None) -> str:
            from durumari_mcp.skill_cli import infer_client

            return infer_client(
                target.name, client or (target.client if target.client in INSTALL_CLIENTS else None)
            )

        def durumari_skill_sync_status(
            client: SkillClient | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Verify required Skills against current versions and actual local files.

            Native Agents check at session start and before dependent work.
            Reports missing, outdated or locally modified files without blocking work.
            Device approval and credential use remain independent.
            """
            from durumari_mcp.required_skills import status

            def check() -> dict[str, Any]:
                target = local(project_ref)
                return dict(status(target, host_client(target, client)))

            return safe(check)

        def durumari_skill_sync(
            client: SkillClient | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Safely update required Skills to the space's selected immutable versions.

            Use for required updates or legacy-copy warnings, even if the central copy
            is current. Do not retry unchanged conflicts; continue unrelated work.
            Preserve local changes and report conflicts; never execute downloaded code,
            force an overwrite, enroll a device, or grant credential access.
            """
            from durumari_mcp.required_skills import sync

            def synchronize() -> dict[str, Any]:
                target = local(project_ref)
                return dict(sync(target, host_client(target, client)))

            return safe(synchronize)

        if "write" in actions:

            def durumari_skill_import_git_checkout(
                repository_url: Annotated[str, Field(min_length=1, max_length=2048)],
                version: Version,
                path: Annotated[str, Field(max_length=200)] = "",
                ref: Annotated[str, Field(max_length=200)] = "",
                checkout_path: Annotated[str, Field(min_length=1, max_length=4096)] | None = None,
                requirements: Annotated[list[Requirement], Field(max_length=16)] | None = None,
                request_key: RequestKey | None = None,
                project_ref: ProjectRef | None = None,
            ) -> dict[str, Any]:
                """Publish committed Skill files from an existing public or private checkout.

                The checkout origin must match the HTTPS repository URL. This reads
                Git objects only: uncommitted files, credentials, hooks and repository
                scripts are never uploaded or executed. Prepare private checkout access
                using the user's existing Git login, then pass its directory here.
                Secrets: prefer a durumari.json at the Skill root (see
                durumari_skill_publish); explicit requirements must match it.
                """

                def publish_checkout() -> dict[str, Any]:
                    from pathlib import Path

                    from durumari_mcp.skill_git import read_checkout, skill_name

                    if account(selected(project_ref)) and request_key is None:
                        raise AdapterError("account_skill_request_key_required")
                    workspace = checkout_path or local(project_ref).profile.workspace
                    result = read_checkout(repository_url, Path(workspace), ref, path)
                    body = {
                        "name": skill_name(result["files"]),
                        "version": version,
                        "files": result["files"],
                        "source": result["source"],
                        "requirements": requirement_payload(requirements),
                    }
                    return write_request(ROOT, body, "POST", request_key, project_ref)

                return safe(publish_checkout)

            register(
                durumari_skill_import_git_checkout, title="Import a private Git Skill checkout"
            )

        def durumari_skill_install(
            skill_id: Id,
            version: Version | None = None,
            enroll: bool | None = None,
            client: SkillClient | None = None,
            project_ref: ProjectRef | None = None,
        ) -> dict[str, Any]:
            """Install a pinned Skill into .durumari/skills/<name> for every local Agent.

            By default, device access is requested only when the version names secret
            slots. Preserve local edits. No downloaded script executes. Access needs
            owner approval. An account connection never requests device access.
            """

            def install() -> dict[str, Any]:
                from durumari_mcp.skill_cli import install_skill

                accounted = account(selected(project_ref))
                if accounted and enroll:
                    raise AdapterError("account_device_approval_required")
                target = local(project_ref)
                row = scoped(target.request(path(skill_id)), target)
                return install_skill(
                    target,
                    row,
                    host_client(target, client),
                    version,
                    enroll=False if accounted else enroll,
                )

            return safe(install)

        register(durumari_skill_install, title="Install a project Skill and request access")
        register(durumari_skill_sync_status, read=True, title="Verify required project Skills")
        register(durumari_skill_sync, title="Update required project Skills")
