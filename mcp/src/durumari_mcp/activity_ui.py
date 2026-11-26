"""Metadata-only presentation of authoritative Context responses.

The projection never changes a write, infers comprehension, or turns a browser
acknowledgment into a storage receipt. Transport adapters retain the original JSON.
"""

import json
from functools import wraps
from typing import Any, cast

from mcp.server import MCPServer
from mcp.types import CallToolResult, TextContent

from .activity_assets import render_activity_html

UI_URI = "ui://durumari/context-activity/v1.html"
UI_META: dict[str, Any] = {"ui": {"resourceUri": UI_URI}}
WIDGET_DESCRIPTION = (
    "Shows the result of a Durumari tool call as a compact card: a Context document, a saved"
    " result, a request, a glossary term or secret names and states. It never shows a secret"
    " value and does not change anything by itself."
)
ENTRY_META: dict[str, Any] = {
    **UI_META,
    "openai/ui": {"entrypoints": [{"type": "global"}, {"type": "thread"}]},
}

ACTIONS = {
    "durumari_context_read": "read",
    "durumari_context_save": "create",
    "durumari_context_revise": "patch",
    "durumari_context_version": "version",
    "durumari_context_operation": "recover",
}

DOMAIN_TOOLS = {
    "durumari_context_save",
    "durumari_context_revise",
    "durumari_context_version",
    "durumari_context_operation",
    "durumari_glossary_read",
    "durumari_glossary_submit",
    "durumari_request_read",
    "durumari_request_create",
    "durumari_request_note",
    "durumari_request_revise",
    "durumari_context_import",
    "durumari_resource_status",
    "durumari_secret_list",
    "durumari_secret_status",
    "durumari_secret_request",
}
# A secret card shows names and states only. A result keeps just these keys, so a value
# under any other name never reaches the model or the view.
SECRET_TOOLS = {"durumari_secret_list", "durumari_secret_status", "durumari_secret_request"}
SECRET_KEPT_KEYS = {
    "id",
    "operation_id",
    "label",
    "name",
    "kind",
    "tier",
    "environment",
    "env_allowed",
    "revision",
    "state",
    "status",
    "expires_on",
    "expires_at",
    "created_at",
    "secure_url",
    "next_after",
    "items",
    "created_ids",
    "counts",
    "count",
    "total",
    "processed",
    "error_code",
    "created",
    "updated",
    "deleted",
    "ok",
    "error",
    "code",
    "message",
}
SECRET_OMITTED_TEXT = "값이 포함될 수 있어 생략됨"
# What an Agent needs from a refusal to act on it: when to retry and what to do next. These
# are codes, counts and field names, never a stored value; text is cut short all the same.
SECRET_ERROR_DETAIL_KEYS = {
    "retry_after_seconds",
    "reason",
    "cause",
    "next_action",
    "invalid_fields",
}
SECRET_DETAIL_TEXT_LIMIT = 200
SECRET_DETAIL_LIST_LIMIT = 20


def scrub_error_details(details: Any) -> dict[str, Any]:
    """The allowed fields of ``error.details``: numbers, short text and short lists of text."""
    kept: dict[str, Any] = {}
    if not isinstance(details, dict):
        return kept
    for key in sorted(SECRET_ERROR_DETAIL_KEYS & details.keys()):
        item = details[key]
        if isinstance(item, bool) or isinstance(item, int | float):
            kept[key] = item
        elif isinstance(item, str):
            kept[key] = item[:SECRET_DETAIL_TEXT_LIMIT]
        elif isinstance(item, list):
            kept[key] = [
                entry[:SECRET_DETAIL_TEXT_LIMIT] for entry in item if isinstance(entry, str)
            ][:SECRET_DETAIL_LIST_LIMIT]
    return kept


def scrub_secret_values(value: Any) -> Any:
    """Keep allowed keys; a scalar stays only as a direct value, a list only as objects."""
    if not isinstance(value, dict):
        return {}
    kept: dict[str, Any] = {}
    for key, item in value.items():
        if key not in SECRET_KEPT_KEYS:
            continue
        if isinstance(item, dict):
            kept[key] = scrub_secret_values(item)
            if key == "error" and (details := scrub_error_details(item.get("details"))):
                kept[key]["details"] = details
        elif isinstance(item, list):
            kept[key] = [
                scrub_secret_values(entry) if isinstance(entry, dict) else entry
                for entry in item
                if isinstance(entry, dict) or (key == "created_ids" and isinstance(entry, str))
            ]
        else:
            kept[key] = item
    return kept


def clean_secret_result(result: CallToolResult) -> None:
    """Keep card fields only; anything else becomes a fixed notice."""
    kept: list[Any] = []
    for item in result.content:
        if isinstance(item, TextContent):
            try:
                parsed = json.loads(item.text)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict):
                text = json.dumps(scrub_secret_values(parsed), ensure_ascii=False)
            else:
                text = SECRET_OMITTED_TEXT
            kept.append(TextContent(type="text", text=text))
        else:
            # Embedded resources and links can carry values outside any key filter.
            kept.append(TextContent(type="text", text=SECRET_OMITTED_TEXT))
    result.content = kept
    # mcp.types.CallToolResult (SDK 2.x) names this field structured_content in Python;
    # structuredContent is only its wire alias. The secret card tests read it back.
    if result.structured_content is not None:
        result.structured_content = scrub_secret_values(result.structured_content)


def decorate_tool(tool: Any) -> None:
    """Keep the original validator/function and JSON; add a small view projection."""
    action = ACTIONS.get(tool.name)
    if action is None and tool.name not in DOMAIN_TOOLS:
        return
    original = tool.fn
    tool.meta = {**(tool.meta or {}), **UI_META}

    @wraps(original)
    def presented(**arguments: Any) -> CallToolResult:
        result = original(**arguments)
        if isinstance(result, CallToolResult) and tool.name in SECRET_TOOLS:
            clean_secret_result(result)
        if isinstance(result, CallToolResult) and result.content:
            first = result.content[0]
            if isinstance(first, TextContent):
                try:
                    value = json.loads(first.text)
                    if isinstance(value, dict):
                        if action is not None:
                            selected_action = (
                                arguments.get("action", action) if action == "version" else action
                            )
                            projection = activity_result(value, selected_action)
                            if isinstance(arguments.get("project_ref"), str):
                                projection["project_ref"] = arguments["project_ref"]
                            elif isinstance(arguments.get("target_project_id"), str):
                                # The view continues a read with the same target it used.
                                projection["target_project_id"] = arguments["target_project_id"]
                            result.meta = {**(result.meta or {}), "activity": projection}
                        if tool.name in DOMAIN_TOOLS:
                            descriptor: dict[str, Any] = {"tool": tool.name}
                            for key in (
                                "action",
                                "view",
                                "project_ref",
                                "context_id",
                                "base_revision",
                                "term_id",
                                "request_id",
                                "task_ref",
                                "secret_id",
                                "operation_id",
                            ):
                                item = arguments.get(key)
                                if isinstance(item, (str, int)) and not isinstance(item, bool):
                                    descriptor[key] = item
                            result.meta = {**(result.meta or {}), "domain": descriptor}
                except Exception:
                    # Presentation must never turn an already committed receipt
                    # into a tool failure. The original operation ran outside this try.
                    pass
        return cast(CallToolResult, result)

    tool.fn = presented


def register_activity_tools(client: Any, register: Any) -> None:
    from durumari_mcp.cloud_protocol import Key, Reference, safe

    def durumari_open_context_workspace() -> CallToolResult:
        """Open Durumari's Context workspace from the host's plugin menu.

        The view starts without a selected space; it never borrows another chat's
        selection. This entrypoint does not grant permissions or read documents.
        """
        return CallToolResult(
            content=[TextContent(type="text", text="두루마리 Context 화면을 열었습니다.")],
            structured_content={
                "activity": {
                    "schema_version": 1,
                    "action": "open",
                    "outcome": "ready",
                    "navigation": callable(getattr(client, "projects", None)),
                }
            },
        )

    register(
        durumari_open_context_workspace,
        title="Open the Context workspace",
        read=True,
        meta=ENTRY_META,
    )
    from mcp.server.mcpserver.exceptions import ToolError

    from durumari_mcp.exploration import register_exploration_tools

    def exploration_target(project_ref: str | None) -> Any:
        factory = getattr(client, "targeted", None)
        if callable(factory):
            return factory(project_ref)
        if project_ref is not None:
            raise ToolError("bound_project_ref_not_allowed")
        return client

    register_exploration_tools(exploration_target, register)
    resources = getattr(client, "resources", None)
    if not callable(resources):
        return

    def durumari_context_result_browser(
        project_ref: Reference,
        request_key: Key,
        stored_request_key: Key,
    ) -> CallToolResult:
        """Open a server-confirmed write result in an authenticated browser.

        Offer browser_url for a confirmed write when inline result UI is unavailable.

        Use the original write's project_ref/stored_request_key. request_key is a
        separate stable key for this view. After opening browser_url recover the
        same task_ref with resource_status. completed means display acknowledgment,
        not another save. An unknown write must be recovered before opening a view.
        """

        def begin() -> dict[str, Any]:
            target = client.targeted(project_ref)
            saved = target.operation(stored_request_key)
            card = activity_result(saved, "recover")
            if card["outcome"] != "stored":
                return cast(dict[str, Any], saved)
            task = resources(
                "begin",
                {
                    "project_ref": project_ref,
                    "request_key": request_key,
                    "kind": "context_activity",
                    "action": "browse",
                    "resource_id": saved["context_id"],
                    "revision": saved["revision"],
                },
            )
            return {**task, "activity": card}

        return safe(begin)

    register(
        durumari_context_result_browser,
        title="Show the saved Context",
        required_scopes=["context:read"],
    )


def register_activity_resource(server: MCPServer) -> None:
    @server.resource(
        UI_URI,
        mime_type="text/html;profile=mcp-app",
        title="두루마리 Context",
        meta={
            "ui": {"csp": {"connectDomains": [], "resourceDomains": []}},
            "openai/ui": {
                "preferredDisplayMode": "inline",
                "availableDisplayModes": ["inline", "fullscreen"],
            },
            # Every tool shares this one view. Without its own description, the host filled
            # it with one tool's text (the glossary's) and showed it on every tool, the secret
            # list included (OpenAI app review, 2026-11-21).
            "openai/widgetDescription": WIDGET_DESCRIPTION,
        },
    )
    def activity_html() -> str:
        return render_activity_html()


def activity_result(value: dict[str, Any], action: str) -> dict[str, Any]:
    """Space a server response, not caller-authored display assertions."""
    result: dict[str, Any] = {"schema_version": 1, "action": action, "outcome": "unknown"}
    error = value.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        if code in {"forbidden", "insufficient_scope", "membership_required"}:
            result["outcome"] = "forbidden"
        elif code not in {"operation_unknown", "durumari_operation_failed"}:
            result["outcome"] = "failed"
        return result
    if action == "view":
        result["outcome"] = {
            "completed": "displayed",
            "pending": "pending",
            "canceled": "canceled",
            "expired": "expired",
        }.get(str(value.get("state")), "unknown")
        return result
    context = value.get("id") if action == "read" else value.get("context_id")
    revision = value.get("revision")
    project = value.get("project_id")
    if not (
        isinstance(context, str)
        and isinstance(project, str)
        and type(revision) is int
        and revision > 0
    ):
        return result
    if action != "read" and value.get("status") != "stored":
        return result
    result.update(
        project_id=project,
        context_id=context,
        revision=revision,
        outcome="retrieved" if action == "read" else "stored",
    )
    if action == "read":
        result["coverage"] = value.get("coverage", "document_page")
        result["partial"] = value.get("truncated") is True
        if isinstance(value.get("title"), str):
            result["title"] = value["title"][:200]
    elif isinstance(value.get("request_key"), str):
        result["request_key"] = value["request_key"]
    return result
