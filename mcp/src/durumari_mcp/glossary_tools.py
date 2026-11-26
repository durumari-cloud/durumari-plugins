"""No inference or transcript scanning: transport reported glossary work with receipts."""

import json
import re
from collections.abc import Callable
from typing import Any

from durumari_mcp.capture import contains_sensitive
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_gateway import ContextFailure

WRITES = frozenset({"create", "update", "observe", "propose"})
READS = frozenset(
    {"terms", "term", "history", "candidates", "frequency", "observations", "operation"}
)


def deliver(
    send: Callable[[dict[str, Any]], dict[str, Any]],
    action: str,
    body: dict[str, Any],
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    if action not in READS | WRITES or "project_ref" in body or "action" in body:
        raise AdapterError("invalid_glossary_operation")
    if len(json.dumps(body, ensure_ascii=False).encode()) > 262144:
        raise AdapterError("glossary_request_too_large")
    key = body.get("request_key")
    if action in WRITES | {"operation"} and (
        not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key)
    ):
        raise AdapterError("invalid_glossary_request_key")
    if action in WRITES and contains_sensitive(body):
        raise AdapterError("sensitive_context_rejected")
    try:
        result = send({**body, "action": action})
    except ContextFailure:
        raise
    except (AdapterError, TimeoutError, ConnectionError) as error:
        if (
            action not in WRITES
            or isinstance(error, AdapterError)
            and str(error) != "project_delivery_unknown"
        ):
            raise
        return {
            "status": "unknown",
            "request_key": key,
            "next_action": "read_operation_or_retry_same_key_and_payload",
        }
    except Exception:
        raise AdapterError("glossary_delivery_failed") from None
    if action in WRITES | {"operation"}:
        if (
            result.get("status") != "stored"
            or result.get("request_key") != key
            or not isinstance(result.get("project_id"), str)
            or (project_id is not None and result["project_id"] != project_id)
        ):
            raise AdapterError("invalid_glossary_receipt")
    return result
