"""Explicit host transcript adapters. Never scan host history or capture reasoning."""

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from durumari_mcp.capture_errors import fixed_read_failure
from durumari_mcp.legacy import tool_pattern

SENSITIVE = re.compile(
    r"(?:\b(?:mp[acgr]_|sk-(?:proj-)?|gh[pousr]_)[A-Za-z0-9_-]{16,}"
    r"|-----BEGIN [A-Z ]*PRIVATE[ ]KEY-----"
    r"|\b(?:password|passwd|api[_ -]?key|access[_ -]?token|refresh[_ -]?token|authorization)"
    r"[\"']?\s*[:=]\s*[\"']?\S+)",
    re.IGNORECASE,
)
SECRET_TOOLS = re.compile(tool_pattern(
    r"(?:durumari_use_secret|durumari_sign_payload|secret.*(?:reveal|resolve|use)|"
    r"(?:get|read|reveal).*secret|(?:login|credential|password)|"
    r"durumari_context_(?:read|query)|durumari_read_context)"),
    re.IGNORECASE,
)
PROJECT_READ = re.compile(tool_pattern(
    r"durumari_project_handoff|durumari_work_read|durumari_context_list|durumari_context_read|durumari_context_asset"),
    re.IGNORECASE,
)
DIRECT_PROJECT_READ = re.compile(tool_pattern(
    r"(?:mcp__[A-Za-z0-9_-]+__)?(?:durumari_project_handoff|durumari_work_read|"
    r"durumari_context_list|durumari_context_read|durumari_context_asset)"),
    re.IGNORECASE,
)


def project_tool_discovery(name: str, arguments: Any) -> bool:
    """Recognize Claude's explicit metadata selection, which does not run a tool."""
    if (
        name != "ToolSearch"
        or not isinstance(arguments, dict)
        or set(arguments) - {"query", "max_results"}
        or not isinstance(arguments.get("query"), str)
        or not arguments["query"].startswith("select:")
        or len(arguments["query"]) > 1000
        or (
            "max_results" in arguments
            and (
                type(arguments["max_results"]) is not int or not 1 <= arguments["max_results"] <= 20
            )
        )
    ):
        return False
    names = arguments["query"][7:].split(",")
    return bool(names) and all(
        DIRECT_PROJECT_READ.fullmatch(tool)
        or re.fullmatch(tool_pattern(r"(?:mcp__[A-Za-z0-9_-]+__)?durumari_project_status"), tool)
        for tool in names
    )


def simple_project_read_wrapper(name: str, arguments: Any) -> bool:
    """Recognize one literal Codex read forwarded unchanged; never evaluate JS.

    Variables, expressions, extra statements and arbitrary wrappers are not covered.
    The result still requires same-connection attestation in normalize_transcript.
    """
    if name not in {"exec", "functions.exec"} or not isinstance(arguments, str):
        return False
    if len(arguments) > 16_384:
        return False
    call = re.fullmatch(
        r"\s*text\(\s*await\s+tools\.([A-Za-z0-9_]+)\(\s*(\{.*\})\s*\)\s*\)\s*;?\s*",
        arguments, re.DOTALL,
    )
    if call is None or not DIRECT_PROJECT_READ.fullmatch(call[1]):
        return False
    try:
        value = json.loads(call[2])
    except (ValueError, RecursionError):
        return False
    return isinstance(value, dict) and not contains_sensitive(value)


def sensitive_operation(name: str, arguments: Any) -> bool:
    """Direct project reads stay pending until their same-connection result arrives.

    Wrapped/unknown operations and secret-bearing arguments remain conservative.
    A legacy pinned read without project attestation is still quarantined.
    """
    if contains_sensitive(arguments):
        return True
    if project_tool_discovery(name, arguments) or simple_project_read_wrapper(name, arguments):
        return False
    return bool(
        (SECRET_TOOLS.search(name) and not DIRECT_PROJECT_READ.fullmatch(name))
        or SECRET_TOOLS.search(arguments if isinstance(arguments, str) else json.dumps(arguments))
    )


def project_read_tool(name: str, arguments: Any) -> str | None:
    if DIRECT_PROJECT_READ.fullmatch(name):
        return name
    if simple_project_read_wrapper(name, arguments):
        match = re.search(r"tools\.([A-Za-z0-9_]+)\(", arguments)
        return match[1] if match else None
    return None


def contains_sensitive(value: Any) -> bool:
    """Bounded recursive inspection, including JSON strings with escaped quotes.

    This is an exclusion heuristic; it cannot establish that arbitrary data is
    free of secrets. Known operation/session quarantine remains the primary gate.
    """
    nodes = 0
    characters = 0

    def inspect(item: Any, depth: int) -> bool:
        nonlocal nodes, characters
        nodes += 1
        if nodes > 10_000 or depth > 32:
            return True
        if isinstance(item, str):
            characters += len(item)
            if characters > 1_048_576 or SENSITIVE.search(item):
                return True
            try:
                decoded = json.loads(item)
            except ValueError:
                return False
            return inspect(decoded, depth + 1) if decoded != item else False
        if isinstance(item, dict):
            for key, child in item.items():
                if re.fullmatch(
                    r"(?:password|passwd|api[_ -]?key|access[_ -]?token|"
                    r"refresh[_ -]?token|authorization)",
                    str(key),
                    re.IGNORECASE,
                ):
                    if child is not None and child != "":
                        return True
                if inspect(child, depth + 1):
                    return True
            return False
        if isinstance(item, list):
            return any(inspect(child, depth + 1) for child in item)
        return False

    return inspect(value, 0)


def attested_scope(text: str, scope: tuple[str, str] | None) -> bool:
    if scope is None or len(text) > 1_048_576:
        return False
    pairs: list[tuple[str, str]] = []
    projects: list[str] = []

    def visit(value: Any, depth: int = 0) -> None:
        if depth > 8:
            return
        if isinstance(value, str):
            try:
                visit(json.loads(value), depth + 1)
            except ValueError:
                pass
        elif isinstance(value, dict):
            if isinstance(value.get("project_id"), str):
                projects.append(value["project_id"])
                if isinstance(value.get("connection_id"), str):
                    pairs.append((value["project_id"], value["connection_id"]))
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)

    visit(text)
    return (
        bool(pairs)
        and all(pair == scope for pair in pairs)
        and all(project == scope[0] for project in projects)
    )


@dataclass
class TranscriptResult:
    events: list[dict[str, Any]] = field(default_factory=list)
    omissions: list[str] = field(default_factory=list)
    coverage: str = "partial_host_transcript"
    pending_read_ids: set[str] = field(default_factory=set)
    resolved_read_ids: set[str] = field(default_factory=set)


def _text(content: Any) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Tool outputs may be text/image arrays. Never stringify arbitrary objects.
        text = [
            row["text"]
            for row in content
            if isinstance(row, dict)
            and row.get("type") in {"text", "input_text", "output_text"}
            and isinstance(row.get("text"), str)
        ]
        return "\n".join(text) or None
    return None


def attested_codex_output(content: Any, scope: tuple[str, str] | None) -> bool:
    text = _text(content)
    if not text or len(text) > 1_048_576:
        return False
    if attested_scope(text, scope):
        return True
    # Codex 0.153.4 exec frames MCP results as separate input_text blocks after
    # this status header. Never scan arbitrary prose for a plausible JSON scope.
    if not isinstance(content, list) or not 2 <= len(content) <= 65:
        return False
    if not all(
        isinstance(block, dict)
        and set(block) == {"type", "text"}
        and block["type"] == "input_text"
        and isinstance(block["text"], str)
        for block in content
    ):
        return False
    if not re.fullmatch(
        r"Script completed\nWall time [0-9]+(?:\.[0-9]+)? seconds\nOutput:\n",
        content[0]["text"],
    ):
        return False
    for block in content[1:]:
        try:
            result = json.loads(block["text"])
        except ValueError:
            return False
        if (
            not isinstance(result, dict)
            or set(result) - {"content", "structuredContent", "isError", "_meta", "resultType"}
            or result.get("resultType", "complete") != "complete"
            or not isinstance(result.get("content"), list)
            or result.get("isError", False) is not False
            or not attested_scope(block["text"], scope)
        ):
            return False
    return True


def normalize_transcript(
    client: str,
    lines: list[str],
    session_id: str,
    *,
    scope: tuple[str, str] | None = None,
) -> TranscriptResult:
    result = TranscriptResult()
    secret_session = False
    pending_reads: set[str] = set()
    read_tools: dict[str, str | None] = {}
    discoveries: set[str] = set()
    observed = datetime.now(UTC).isoformat()
    for line_index, line in enumerate(lines):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError
        except ValueError:
            result.omissions.append("incomplete_or_invalid_record")
            continue
        timestamp = row.get("timestamp", observed)
        timestamp_trusted = "timestamp" in row
        try:
            parsed_time = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                raise ValueError
        except (ValueError, AttributeError, TypeError):
            timestamp = observed
            timestamp_trusted = False
            result.omissions.append("host_timestamp_unavailable")
        chunks: list[tuple[str, str]] = []
        if client == "claude" and row.get("type") in {"user", "assistant"}:
            message = row.get("message")
            if not isinstance(message, dict):
                result.omissions.append("unsupported_record")
                continue
            if message.get("channel") in {"analysis", "reasoning"} or row.get("channel") in {
                "analysis",
                "reasoning",
            }:
                continue
            content = message.get("content")
            if isinstance(content, str):
                chunks.append((row["type"], content))
            elif isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        result.omissions.append("unsupported_content_block")
                        continue
                    if block.get("type") == "tool_use":
                        if project_tool_discovery(str(block.get("name", "")), block.get("input")):
                            discoveries.add(str(block.get("id", "")))
                            result.omissions.append("tool_discovery_metadata_excluded")
                            continue
                        if sensitive_operation(str(block.get("name", "")), block.get("input", {})):
                            secret_session = True
                            result.omissions.append("sensitive_operation_detected")
                        if PROJECT_READ.search(str(block.get("name", ""))) or PROJECT_READ.search(
                            json.dumps(block.get("input", {}))
                        ):
                            read_id = str(block.get("id", ""))
                            pending_reads.add(read_id)
                            read_tools[read_id] = project_read_tool(
                                str(block.get("name", "")), block.get("input", {}),
                            )
                        # Tool arguments can contain arbitrary file contents/credentials.
                        result.omissions.append("tool_arguments_excluded")
                    elif block.get("type") == "text" and isinstance(block.get("text"), str):
                        chunks.append((row["type"], block["text"]))
                    elif block.get("type") == "tool_result":
                        value = _text(block.get("content"))
                        read_id = str(block.get("tool_use_id", ""))
                        if read_id in discoveries:
                            discoveries.remove(read_id)
                            if contains_sensitive(block.get("content")):
                                secret_session = True
                                result.omissions.append("sensitive_content_detected")
                            continue
                        if read_id in pending_reads:
                            pending_reads.remove(read_id)
                            result.resolved_read_ids.add(hashlib.sha256(read_id.encode()).hexdigest())
                            if fixed_read_failure(block.get("content"), read_tools.get(read_id)):
                                result.omissions.append("context_read_failed_without_content")
                                continue
                            if not value or not attested_scope(value, scope):
                                secret_session = True
                                result.omissions.append("unattested_context_read_quarantined")
                        if value:
                            chunks.append(("tool", value))
                    elif block.get("type") not in {"thinking", "redacted_thinking"}:
                        result.omissions.append("unsupported_content_block")
            else:
                result.omissions.append("unsupported_content_block")
        elif client == "codex" and row.get("type") == "response_item":
            payload = row.get("payload")
            if not isinstance(payload, dict):
                result.omissions.append("unsupported_record")
                continue
            if payload.get("type") == "message" and payload.get("role") in {"user", "assistant"}:
                if payload.get("channel") in {"analysis", "reasoning"} or payload.get("phase") in {
                    "analysis",
                    "reasoning",
                }:
                    continue
                value = _text(payload.get("content"))
                if value:
                    chunks.append((payload["role"], value))
            elif payload.get("type") in {"function_call", "custom_tool_call"}:
                if sensitive_operation(
                    str(payload.get("name", "")), payload.get("arguments", payload.get("input", ""))
                ):
                    secret_session = True
                    result.omissions.append("sensitive_operation_detected")
                if PROJECT_READ.search(str(payload.get("name", ""))) or PROJECT_READ.search(
                    str(payload.get("arguments", payload.get("input", "")))
                ):
                    read_id = str(payload.get("call_id", ""))
                    pending_reads.add(read_id)
                    read_tools[read_id] = project_read_tool(
                        str(payload.get("name", "")),
                        payload.get("arguments", payload.get("input", "")),
                    )
                result.omissions.append("tool_arguments_excluded")
            elif payload.get("type") in {"function_call_output", "custom_tool_call_output"}:
                value = _text(payload.get("output"))
                read_id = str(payload.get("call_id", ""))
                if read_id in pending_reads:
                    pending_reads.remove(read_id)
                    result.resolved_read_ids.add(hashlib.sha256(read_id.encode()).hexdigest())
                    if fixed_read_failure(payload.get("output"), read_tools.get(read_id)):
                        result.omissions.append("context_read_failed_without_content")
                        continue
                    if not value or not attested_codex_output(payload.get("output"), scope):
                        secret_session = True
                        result.omissions.append("unattested_context_read_quarantined")
                if value:
                    chunks.append(("tool", value))
            elif payload.get("type") != "reasoning":
                result.omissions.append("unsupported_record")
        elif row.get("type") in {
            "session_meta",
            "turn_context",
            "event_msg",
            "system",
            "progress",
            "file-history-snapshot",
            "summary",
        }:
            # These include internal context and reasoning duplicates in some hosts.
            continue
        else:
            result.omissions.append("unsupported_record")
        for block_index, (kind, text) in enumerate(chunks):
            if contains_sensitive(text):
                secret_session = True
                result.omissions.append("sensitive_content_detected")
            if secret_session:
                result.omissions.append("sensitive_session_quarantined")
                continue
            if pending_reads:
                result.omissions.append("unattested_intermediate_output_excluded")
                continue
            if not text.strip() or "\x00" in text:
                result.omissions.append("invalid_record_excluded")
                continue
            if len(text) > 40_000 or len(text.encode("utf-8")) > 120_000:
                result.omissions.append("oversized_record_excluded")
                continue
            event_seed = f"{client}\0{session_id}\0{line_index}\0{block_index}\0{kind}\0{text}"
            result.events.append(
                {
                    "event_id": hashlib.sha256(event_seed.encode()).hexdigest(),
                    "session_id": session_id,
                    "sequence": line_index * 100 + block_index,
                    "kind": kind,
                    "text": text,
                    "observed_at": timestamp,
                    "summary": None,
                    "remaining_work": None,
                    "sensitive": False,
                    "_source_line": line_index,
                    "_timestamp_trusted": timestamp_trusted,
                }
            )
    if secret_session:
        result.omissions.append("sensitive_session_quarantined")
    if pending_reads:
        # PreToolUse can see the call before its result has been written. Keep
        # derived output blocked until attestation arrives, without permanently
        # quarantining a valid same-connection read merely because it is in flight.
        result.omissions.append("context_read_pending")
    result.pending_read_ids = {hashlib.sha256(read_id.encode()).hexdigest()
                               for read_id in pending_reads}
    result.omissions = sorted(set(result.omissions))
    return result




