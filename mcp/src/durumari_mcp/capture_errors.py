"""Recognize only fixed, content-free errors from an observed project read."""

import json
import re
from typing import Any

# These adapter errors never contain a server response body or exception details.
# This is not a generic error-pattern allowance: unknown errors remain untrusted.
READ_FAILURE_CODES = frozenset({
    "project_access_denied", "project_rate_limited", "project_unavailable",
    "project_delivery_unknown", "project_login_required", "invalid_project_response",
    "document_scope_mismatch", "durumari_operation_failed",
})


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, child in pairs:
        if key in value:
            raise ValueError
        value[key] = child
    return value


def fixed_read_failure(content: Any, tool: str | None, *, depth: int = 0) -> bool:
    """Resolve a failed call, not its authorization or any existing quarantine.

    No additional text, attachments, metadata, structured payload or duplicate
    JSON keys may accompany the fixed error. Match the observed tool as well as
    the call ID (the latter is checked by the transcript adapter).
    """
    if not tool or depth > 3:
        return False
    expected = {
        f"Error executing tool {name}: {code}"
        for name in {tool, tool.rsplit("__", 1)[-1]} for code in READ_FAILURE_CODES
    }
    if isinstance(content, str):
        if len(content) > 4096:
            return False
        if content in expected:
            return True
        try:
            value = json.loads(content, object_pairs_hook=_unique_object)
        except (ValueError, RecursionError):
            return False
        return fixed_read_failure(value, tool, depth=depth + 1)
    if isinstance(content, dict):
        if (
            set(content) - {"content", "isError", "structuredContent", "_meta", "resultType"}
            or content.get("isError") is not True
            or content.get("resultType", "complete") != "complete"
            or content.get("structuredContent") is not None
            or content.get("_meta") is not None
        ):
            return False
        return fixed_read_failure(content.get("content"), tool, depth=depth + 1)
    if not isinstance(content, list):
        return False
    if len(content) == 1:
        block = content[0]
        return (
            isinstance(block, dict) and set(block) == {"type", "text"}
            and block["type"] == "text" and isinstance(block["text"], str)
            and block["text"] in expected
        )
    # A single MCP result framed by Codex's exec runner. No extra result blocks.
    if len(content) == 2 and all(
        isinstance(block, dict) and set(block) == {"type", "text"}
        and block["type"] == "input_text" and isinstance(block["text"], str)
        for block in content
    ) and re.fullmatch(
        r"Script completed\nWall time [0-9]+(?:\.[0-9]+)? seconds\nOutput:\n",
        content[0]["text"],
    ):
        return fixed_read_failure(content[1]["text"], tool, depth=depth + 1)
    return False
