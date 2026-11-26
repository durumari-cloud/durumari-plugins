"""Web-only features on MCP: public pages and web publication, document
references, project pages, Skill packages, and vault/account metadata.

A client exposes them by implementing `web_surfaces(tool, action, body)`; the official REST
API calls the same operation. Publishing, unpublishing, reissuing or re-addressing a public
page, a space's or document's web publication, and sharing a document into another space
only record an execution request: the answer is `human_approval_required` with the
`approval_url` where a person approves it on the web. Vault and account changes need the
person's password on the web: `durumari_secret_admin` returns that screen's link and never
a value.
"""

from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field

Id = Annotated[str, Field(min_length=36, max_length=36, pattern=r"^[0-9a-f-]{36}$")]
ContextId = Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$")]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
After = Annotated[str, Field(max_length=36)]
Limit = Annotated[int, Field(ge=1, le=200)]
Revision = Annotated[int, Field(ge=1)]
Generation = Annotated[int, Field(ge=0)]

PUBLICATION_ACTIONS = Literal[
    "page_status",
    "space_preview",
    "document_preview",
    "cloud_address",
    "document_address",
    "page_pin",
    "publish",
    "unpublish",
    "reissue",
    "follow",
    "space_publish",
    "document_publish",
    "cloud_address_change",
    "document_address_change",
]
TOOLS = (
    "durumari_web_publication",
    "durumari_context_reference",
    "durumari_space_page",
    "durumari_skill_package",
    "durumari_secret_admin",
)


def register_web_surface_tools(
    web_surfaces: Callable[[str, str, dict[str, Any]], dict[str, Any]],
    register: Any,
    safe: Callable[[Callable[[], dict[str, Any]]], CallToolResult],
) -> None:
    def call(tool: str, action: str, body: dict[str, Any]) -> CallToolResult:
        clean = {key: value for key, value in body.items() if value is not None}
        return safe(lambda: web_surfaces(tool, action, clean))

    def durumari_web_publication(
        action: PUBLICATION_ACTIONS,
        context_id: ContextId | None = None,
        cloud_id: Id | None = None,
        target_project_id: Id | None = None,
        request_key: Key | None = None,
        base_revision: Revision | None = None,
        generation: Generation | None = None,
        params: dict[str, Any] | None = None,
        public: bool | None = None,
        indexable: bool | None = None,
        visibility: Literal["inherit", "private", "public"] | None = None,
        mode: Literal["latest", "snapshot"] | None = None,
        revision: Revision | None = None,
        expose_history: bool | None = None,
        include_assets: bool | None = None,
        include_related: bool | None = None,
        after: After | None = None,
        limit: Limit | None = None,
    ) -> CallToolResult:
        """Read or change how a space and its documents are published on the web.

        Reads: page_status (a document's public page; context_id), space_preview,
        document_preview (visibility, mode, revision, ...), cloud_address (cloud_id),
        document_address. page_pin returns a following public page to a snapshot
        (base_revision, generation from page_status).
        Person-approved (nothing runs here; answers human_approval_required with
        approval_url): publish, unpublish, reissue, follow (base_revision, generation),
        space_publish (params: generation, preview_digest, public, indexable),
        document_publish (params: generation, base_revision, preview_digest and the
        preview's options), cloud_address_change / document_address_change (params:
        generation, slug). Give request_key; reuse it after an unclear reply.
        """
        return call(
            "durumari_web_publication",
            action,
            {
                "context_id": context_id,
                "cloud_id": cloud_id,
                "target_project_id": target_project_id,
                "request_key": request_key,
                "base_revision": base_revision,
                "generation": generation,
                "params": params,
                "public": public,
                "indexable": indexable,
                "visibility": visibility,
                "mode": mode,
                "revision": revision,
                "expose_history": expose_history,
                "include_assets": include_assets,
                "include_related": include_related,
                "after": after,
                "limit": limit,
            },
        )

    def durumari_context_reference(
        action: Literal["list", "read", "outgoing", "share", "change", "decide"],
        context_id: ContextId | None = None,
        target_project_id: Id | None = None,
        reference_id: Id | None = None,
        revision: Revision | None = None,
        request_key: Key | None = None,
        params: dict[str, Any] | None = None,
        after: After | None = None,
        limit: Limit | None = None,
    ) -> CallToolResult:
        """Documents shared between spaces (references). list: the space's shared-in
        documents; read: one shared-in document (revision); outgoing: one share you made
        (reference_id). Person-approved, nothing runs here (human_approval_required):
        share (params: target_space_id, rights, target_generation), change (params:
        reference_id, generation, rights or null to revoke), decide (params: generation,
        source_generation, accept). Give request_key.
        """
        return call(
            "durumari_context_reference",
            action,
            {
                "context_id": context_id,
                "target_project_id": target_project_id,
                "reference_id": reference_id,
                "revision": revision,
                "request_key": request_key,
                "params": params,
                "after": after,
                "limit": limit,
            },
        )

    def durumari_space_page(
        action: Literal["list", "read", "create", "edit"],
        target_project_id: Id | None = None,
        page_id: Id | None = None,
        request_key: Key | None = None,
        page: dict[str, Any] | None = None,
        after: After | None = None,
    ) -> CallToolResult:
        """A space's pages: a summary Context over linked Context. create (page: title,
        parent_id, context_ids) and edit (page: base_version, base_summary_revision and the
        changes) take request_key; the same key replays the first result."""
        return call(
            "durumari_space_page",
            action,
            {
                "target_project_id": target_project_id,
                "page_id": page_id,
                "request_key": request_key,
                "page": page,
                "after": after,
            },
        )

    def durumari_skill_package(
        action: Literal["import_npm", "import_zip", "installations", "recommendations"],
        target_project_id: Id | None = None,
        skill_id: Id | None = None,
        package: dict[str, Any] | None = None,
    ) -> CallToolResult:
        """Publish a shared Skill release from an npm package (package: package,
        package_version, version, path, requirements) or a zip (package: archive_base64,
        version, path, requirements); with skill_id it adds a version. installations: where
        a Skill is installed; recommendations: the published recommended Skills.
        Access and secret links use durumari_skill_share and durumari_skill_link."""
        return call(
            "durumari_skill_package",
            action,
            {"target_project_id": target_project_id, "skill_id": skill_id, "package": package},
        )

    def durumari_secret_admin(
        action: Literal[
            "capabilities",
            "access_history",
            "policy",
            "groups",
            "resource_grants",
            "account_grants",
            "accounts",
            "account",
            "change",
        ],
        target_project_id: Id | None = None,
        secret_id: Id | None = None,
        account_id: Id | None = None,
        limit: Limit | None = None,
        before: Annotated[str, Field(max_length=100)] | None = None,
        change: Literal[
            "secret_metadata",
            "file_metadata",
            "secret_grants",
            "policy",
            "group_create",
            "group_update",
            "group_delete",
            "resource_grants",
            "account_grants",
            "account",
        ]
        | None = None,
    ) -> CallToolResult:
        """Vault and account metadata, never a value: capabilities, access_history
        (secret_id), policy, groups, resource_grants, account_grants, accounts, account
        (account_id). change returns the vault screen's web_url where the person makes that
        change after entering their password; nothing changes here."""
        return call(
            "durumari_secret_admin",
            action,
            {
                "target_project_id": target_project_id,
                "secret_id": secret_id,
                "account_id": account_id,
                "limit": limit,
                "before": before,
                "change": change,
            },
        )

    both = ["context:read", "context:write"]
    register(durumari_web_publication, title="Web publication", required_scopes=both)
    register(durumari_context_reference, title="Shared documents", required_scopes=both)
    register(durumari_space_page, title="Space pages", required_scopes=both)
    register(durumari_skill_package, title="Skill packages", required_scopes=both)
    register(
        durumari_secret_admin,
        read=True,
        title="Vault and account metadata",
        required_scopes=["secrets:list"],
    )
