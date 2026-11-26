"""Selected-source migration through a human browser task, the same for every host.

The tool only opens an import task bound to the conversation's project_ref. The
person supplies and selects the source text or export files in the browser; the
model never sends source bodies here and recovers created Context IDs, revisions
and source hashes with durumari_resource_status.
"""

from collections.abc import Callable
from typing import Annotated, Any

from mcp.types import CallToolResult
from pydantic import Field

TOOLS = ("durumari_context_import",)
SCOPES = ["context:read", "context:write"]
Reference = Annotated[
    str,
    Field(
        min_length=36,
        max_length=36,
        pattern=r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    ),
]


def register_import_tools(
    resources: Callable[[str, dict[str, Any]], dict[str, Any]], register: Any
) -> None:
    from durumari_mcp.cloud_protocol import Key, safe

    def durumari_context_import(project_ref: Reference, request_key: Key) -> CallToolResult:
        """Preview and migrate selected accessible text/files through a human browser.

        The person supplies and selects source material. This does not retrieve a
        host's inaccessible chat history. Sources remain intact; duplicates are
        skipped by default. Open browser_url and recover verified created IDs with
        durumari_resource_status. Pending is not completion.
        """
        body = {
            "project_ref": project_ref,
            "request_key": request_key,
            "kind": "import",
            "action": "manage",
        }
        return safe(lambda: resources("begin", body))

    register(
        durumari_context_import,
        title="Migrate selected source material",
        read=False,
        required_scopes=SCOPES,
    )
