"""Explicit, bounded work-log operations; never infer identity or scan transcripts."""

import hashlib
import json
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal

from mcp.types import CallToolResult
from pydantic import Field

from durumari_mcp.capture import contains_sensitive
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_gateway import ContextFailure

Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.:-]+$")]
Ref = Annotated[str, Field(min_length=1, max_length=128)]
READS = {"list", "read", "revision", "operation"}
WRITES = {"create", "update", "import"}
MIGRATION_FIELDS = {
    "title", "summary", "status", "completed", "remaining", "next_action", "constraints",
    "waiting_reason", "assignee_id", "agent", "session_ref", "evidence",
}


REVIEW_REASONS = frozenset({
    "source_not_current", "untagged_title_candidate", "unsafe_header", "title_mismatch",
})


# English sentence-case titles, the catalog default the operations console edits (867).
WORK_LOG_TITLES = {
    "durumari_work_log_migration_preview": "Preview work log migration",
    "durumari_work_log_migration_batch": "Migrate work logs",
    "durumari_work_log_list": "List work logs",
    "durumari_work_log_read": "Read a work log",
    "durumari_work_log_revision": "Read a work log revision",
    "durumari_work_log_create": "Create a work log",
    "durumari_work_log_update": "Update a work log",
    "durumari_work_log_import": "Import a work log",
    "durumari_work_log_operation": "Check a work log receipt",
}


def migration_preview(client: Any, after: str = "") -> dict[str, Any]:
    """Enumerate one bounded page without writing or guessing legacy fields."""
    page = client.work_logs("list", {"body": {"after": after, "limit": 20, "inventory": True}})
    if (page.get("scope") != "active_project_visible"
            or page.get("consistency") != "live_rescan_required"):
        raise AdapterError("migration_inventory_unavailable")
    candidates = []
    unavailable = 0
    for row in page["items"]:
        if row.get("origin") != "legacy_context":
            continue
        try:
            status = row.get("inventory_status")
            if (not isinstance(status, str)
                    or status not in {"ready", "already_imported", "source_changed",
                                      "requires_review"}
                    or type(row.get("source_revision")) is not int
                    or row["source_revision"] < 1
                    or not isinstance(row.get("id"), str)
                    or not isinstance(row.get("title"), str)
                    or row.get("identity_basis") not in ("tag", "title_candidate")):
                raise AdapterError("invalid_migration_inventory")
            candidate = {
                "context_id": row["id"],
                "source_revision": row["source_revision"],
                "title": row["title"],
                "identity_basis": row["identity_basis"],
            }
            if status in {"source_changed", "requires_review"}:
                candidate["preview_status"] = status
                if row.get("review_reason") in REVIEW_REASONS:
                    candidate["review_reason"] = row["review_reason"]
                candidates.append(candidate)
                continue
            try:
                detail = client.work_logs("read", {
                    "work_log_id": row["id"], "body": {"source_revision": row["source_revision"]}
                })
                status = detail.get("preview_status")
                if (
                    not isinstance(status, str)
                    or status not in {"ready", "already_imported"}
                    or detail.get("context_id") != row["id"]
                    or type(detail.get("source_revision")) is not int
                    or detail["source_revision"] != row["source_revision"]
                ):
                    raise AdapterError("invalid_migration_preview")
                for name in ("copied_fields", "unknown_fields"):
                    fields = detail.get(name)
                    if not isinstance(fields, list) or len(fields) > len(MIGRATION_FIELDS) or any(
                        not isinstance(field, str) or field not in MIGRATION_FIELDS
                        for field in fields
                    ):
                        raise AdapterError("invalid_migration_preview")
                    candidate[name] = fields
                copied = candidate["copied_fields"]
                unknown = candidate["unknown_fields"]
                if (
                    len(set(copied)) != len(copied)
                    or len(set(unknown)) != len(unknown)
                    or set(copied) & set(unknown)
                    or (status == "ready"
                        and set(copied) | set(unknown) != MIGRATION_FIELDS)
                    or (status == "already_imported" and (copied or unknown))
                ):
                    raise AdapterError("invalid_migration_preview")
                candidate["preview_status"] = status
            except (ContextFailure, AdapterError, TimeoutError, ConnectionError):
                candidate.pop("copied_fields", None)
                candidate.pop("unknown_fields", None)
                candidate["preview_status"] = "unavailable"
                unavailable += 1
            candidates.append(candidate)
        except (ContextFailure, AdapterError, TimeoutError, ConnectionError):
            unavailable += 1
    return {
        "scanned": len(page["items"]),
        "candidates": candidates,
        "unavailable": unavailable,
        "next_after": page["next_after"],
        "scope": page["scope"],
        "excluded": page["excluded"],
        "consistency": page["consistency"],
    }


def migration_batch(
    client: Any, request_key: str, candidates: list[dict[str, Any]]
) -> dict[str, Any]:
    """Import at most 20 independently retryable source revisions."""
    if not 1 <= len(candidates) <= 20:
        raise AdapterError("invalid_migration_batch")
    ids = set()
    for item in candidates:
        if (
            set(item) != {"context_id", "source_revision"}
            or not isinstance(item["context_id"], str)
            or not re.fullmatch(r"[a-f0-9-]{36}", item["context_id"])
            or type(item["source_revision"]) is not int
            or item["source_revision"] < 1
            or item["context_id"] in ids
        ):
            raise AdapterError("invalid_migration_batch")
        ids.add(item["context_id"])
    results = []
    for item in candidates:
        key = (
            "migration-"
            + hashlib.sha256((request_key + "\0" + item["context_id"]).encode()).hexdigest()
        )
        try:
            receipt = client.work_logs("import", {"request_key": key, "body": item})
            results.append({"context_id": item["context_id"], "request_key": key, **receipt})
        except ContextFailure as error:
            results.append(
                {
                    "context_id": item["context_id"],
                    "request_key": key,
                    "status": "conflict" if error.status == 409 else "failed",
                }
            )
        except (AdapterError, TimeoutError, ConnectionError):
            results.append(
                {"context_id": item["context_id"], "request_key": key, "status": "unknown"}
            )
    return {"results": results, "stored": sum(row["status"] == "stored" for row in results)}


def deliver(
    send: Callable[[dict[str, Any]], dict[str, Any]],
    action: str,
    body: dict[str, Any],
    *,
    project_id: str | None = None,
) -> dict[str, Any]:
    if action not in READS | WRITES or set(body) - {"body", "work_log_id", "request_key"}:
        raise AdapterError("invalid_work_log_operation")
    if len(json.dumps(body, ensure_ascii=False).encode()) > 64_000:
        raise AdapterError("work_log_request_too_large")
    key = body.get("request_key")
    if action in WRITES | {"operation"} and (
        not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key)
    ):
        raise AdapterError("invalid_work_log_request_key")
    if action in WRITES and contains_sensitive(body):
        raise AdapterError("sensitive_context_rejected")
    try:
        result = send(body)
    except ContextFailure:
        raise
    except (AdapterError, TimeoutError, ConnectionError) as error:
        if action not in WRITES or (
            isinstance(error, AdapterError) and str(error) != "project_delivery_unknown"
        ):
            raise
        return {
            "status": "unknown",
            "request_key": key,
            "next_action": "read_operation_or_retry_same_key_and_payload",
        }
    if action in WRITES and (
        result.get("status") != "stored"
        or result.get("request_key") != key
        or not isinstance(result.get("work_log_id"), str)
        or not isinstance(result.get("project_id"), str)
        or (project_id is not None and result["project_id"] != project_id)
    ):
        raise AdapterError("invalid_work_log_receipt")
    return result


def register_work_log_tools(
    targeted: Any, register: Any, safe: Callable[[Callable[[], dict[str, Any]]], CallToolResult]
) -> None:
    def durumari_work_log_migration_preview(
        after: Annotated[str, Field(max_length=64)] = "",
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Preview up to 20 existing records without changes; follow next_after to continue."""
        return safe(lambda: migration_preview(targeted(project_ref, target_project_id), after))

    def durumari_work_log_migration_batch(
        request_key: Key,
        candidates: Annotated[list[dict[str, Any]], Field(min_length=1, max_length=20)],
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Import a checked page with a stable batch key; inspect every item result."""
        return safe(
            lambda: migration_batch(
                targeted(project_ref, target_project_id), request_key, candidates
            )
        )

    def durumari_work_log_list(
        filters: dict[str, Any] | None = None,
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Find work records with server filters and a cursor.

        `last_saved_by` is the recorded saving account ID, not the task assignee;
        `__unknown__` selects records with no known saving account.
        """
        return safe(
            lambda: targeted(project_ref, target_project_id).work_logs(
                "list", {"body": filters or {}}
            )
        )

    def durumari_work_log_read(
        work_log_id: Ref,
        view: Literal["handoff"] | None = None,
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Read current status, next action, source and reported progress for one work record.

        view="handoff" returns a small bundle for taking the task over: goal, done,
        remaining, constraints, evidence, the pinned source and whether it changed, the
        last events with who reported them, and whether another session is active.
        Everything in it is a report; recheck the project binding, local Git state and
        permissions first. To take over, update with body {"accept_handoff": true,
        "agent": ..., "session_ref": ..., "base_revision": <revision>} and a new
        request_key; a 409 means someone else changed or took the record, so read again.
        acceptable is false while another session holds it; active_until says when that
        ends, and the holder can end it at once by setting status "waiting".
        """
        body = {"work_log_id": work_log_id, **({"body": {"view": view}} if view else {})}
        return safe(lambda: targeted(project_ref, target_project_id).work_logs("read", body))

    def durumari_work_log_revision(
        work_log_id: Ref,
        revision: Annotated[int, Field(ge=1, strict=True)],
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Read an immutable work-log revision; older revisions may be unavailable."""
        return safe(
            lambda: targeted(project_ref, target_project_id).work_logs(
                "revision", {"work_log_id": work_log_id, "body": {"revision": revision}}
            )
        )

    def durumari_work_log_create(
        request_key: Key,
        body: dict[str, Any],
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Create a work record with a stable key. Never include secrets."""
        return safe(
            lambda: targeted(project_ref, target_project_id).work_logs(
                "create", {"request_key": request_key, "body": body}
            )
        )

    def durumari_work_log_update(
        work_log_id: Ref,
        request_key: Key,
        body: dict[str, Any],
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Update at a checkpoint. Include base_revision; Agent events are reports."""
        return safe(
            lambda: targeted(project_ref, target_project_id).work_logs(
                "update", {"work_log_id": work_log_id, "request_key": request_key, "body": body}
            )
        )

    def durumari_work_log_operation(
        request_key: Key,
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Recover an uncertain work-log write using its original target and request key."""
        return safe(
            lambda: targeted(project_ref, target_project_id).work_logs(
                "operation", {"request_key": request_key}
            )
        )

    def durumari_work_log_import(
        request_key: Key,
        context_id: Ref,
        source_revision: Annotated[int, Field(ge=1, strict=True)],
        project_ref: Ref | None = None,
        target_project_id: Ref | None = None,
    ) -> CallToolResult:
        """Import a confirmed legacy work-log revision, keeping the source unchanged and linked."""
        return safe(
            lambda: targeted(project_ref, target_project_id).work_logs(
                "import",
                {
                    "request_key": request_key,
                    "body": {"context_id": context_id, "source_revision": source_revision},
                },
            )
        )

    for fn in (
        durumari_work_log_migration_preview,
        durumari_work_log_migration_batch,
        durumari_work_log_list,
        durumari_work_log_read,
        durumari_work_log_revision,
        durumari_work_log_create,
        durumari_work_log_update,
        durumari_work_log_import,
        durumari_work_log_operation,
    ):
        register(
            fn,
            read=fn
            in (
                durumari_work_log_list,
                durumari_work_log_migration_preview,
                durumari_work_log_read,
                durumari_work_log_revision,
                durumari_work_log_operation,
            ),
            # An update edits an existing work log in place, so it is destructive like revise.
            destructive=fn is durumari_work_log_update,
            title=WORK_LOG_TITLES[fn.__name__],
        )
