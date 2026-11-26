"""Host-owned Context gate for the controlled executor, never a model tool.

Evidence and selected text stay in memory. A launch policy is private process
configuration, not a signed receipt or an argument a model may attest to. Even
that policy must match a new search, complete current read and head check.
"""

import json
import re
from collections.abc import Sequence
from dataclasses import asdict
from threading import Lock
from typing import Any, TextIO

from durumari_mcp.capture import contains_sensitive
from durumari_mcp.context_guard import GuardScope
from durumari_mcp.context_guard_queries import guard_queries
from durumari_mcp.context_guard_read import (
    GuardBlocked,
    Reader,
    RequiredContext,
    read_required_context,
)

MAX_REQUEST_CHARS = 32768
_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")
_SHA = re.compile(r"[a-f0-9]{64}")


def _scope(value: Any) -> GuardScope:
    names = {"project", "connection", "client", "session", "turn", "epoch"}
    if (
        not isinstance(value, dict)
        or set(value) != names
        or any(not isinstance(v, str) or not _IDENTIFIER.fullmatch(v) for v in value.values())
    ):
        raise GuardBlocked("execution_scope_required")
    return GuardScope(**value)


def _reads(proof: RequiredContext) -> list[dict[str, Any]]:
    return [
        {
            "context": row.context,
            "section": row.section,
            "revision": row.revision,
            "sha256": row.sha256,
        }
        for row in proof.reads
    ]


def _fingerprint(rows: Any) -> tuple[tuple[str, str, int, str], ...]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= 3:
        raise GuardBlocked("execution_read_required")
    values = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or set(row) != {"context", "section", "revision", "sha256"}
            or any(
                not isinstance(row[k], str) or not _IDENTIFIER.fullmatch(row[k])
                for k in ("context", "section")
            )
            or type(row["revision"]) is not int
            or row["revision"] < 1
            or not isinstance(row["sha256"], str)
            or not _SHA.fullmatch(row["sha256"])
        ):
            raise GuardBlocked("execution_read_required")
        values.append((row["context"], row["section"], row["revision"], row["sha256"]))
    if len({(r[0], r[1]) for r in values}) != len(values):
        raise GuardBlocked("execution_read_required")
    return tuple(sorted(values))


class ExecutionGuard:
    """One owned turn; a failed or invalidated execution cannot reuse old proof."""

    def __init__(self, reader: Reader, scope: GuardScope, queries: Sequence[str]) -> None:
        self.reader = reader
        self.scope = _scope(asdict(scope))
        if (
            isinstance(queries, str)
            or not 1 <= len(queries) <= 3
            or any(
                not isinstance(q, str) or not 2 <= len(q) <= 200 or contains_sensitive(q)
                for q in queries
            )
        ):
            raise GuardBlocked("search_required")
        self.queries = tuple(queries)
        self._proof: RequiredContext | None = None
        self._started = False
        self._lock = Lock()

    def begin(self) -> RequiredContext:
        with self._lock:
            if self._started:
                raise GuardBlocked("execution_read_required")
            self._started = True
            self._proof = read_required_context(self.reader, self.scope, self.queries)
            return self._proof

    def invalidate(self) -> None:
        with self._lock:
            self._started = True
            self._proof = None

    def check(self, scope: GuardScope) -> None:
        # MCP hosts may issue parallel tool calls. Serialize only their freshness
        # checks, so an in-flight read is not mistaken for missing evidence.
        with self._lock:
            if self._proof is None:
                raise GuardBlocked("execution_read_required")
            previous = self._proof
            self._proof = None  # Failure is sticky, including transport/protocol errors.
            if scope != self.scope:
                raise GuardBlocked("execution_scope_changed")
            current = read_required_context(self.reader, self.scope, self.queries)
            if _fingerprint(_reads(previous)) != _fingerprint(_reads(current)):
                raise GuardBlocked("execution_context_changed")
            self._proof = current

    def launcher_policy(self) -> dict[str, Any]:
        with self._lock:
            if self._proof is None:
                raise GuardBlocked("execution_read_required")
            return {
                "version": 1,
                "scope": asdict(self.scope),
                "queries": list(self.queries),
                "reads": _reads(self._proof),
            }

    @classmethod
    def from_launcher_policy(
        cls,
        reader: Reader,
        project: str,
        connection: str,
        client: str,
        policy: Any,
    ) -> "ExecutionGuard":
        if (
            not isinstance(policy, dict)
            or set(policy) != {"version", "scope", "queries", "reads"}
            or type(policy["version"]) is not int
            or policy["version"] != 1
        ):
            raise GuardBlocked("execution_policy_required")
        scope = _scope(policy["scope"])
        if (scope.project, scope.connection, scope.client) != (project, connection, client):
            raise GuardBlocked("execution_scope_changed")
        expected = _fingerprint(policy["reads"])
        if not isinstance(policy["queries"], list):
            raise GuardBlocked("search_required")
        gate = cls(reader, scope, policy["queries"])
        actual = gate.begin()  # Metadata alone never establishes a current read.
        if expected != _fingerprint(_reads(actual)):
            gate.invalidate()
            raise GuardBlocked("execution_context_changed")
        return gate


def run_worker(
    reader: Reader,
    project: str,
    connection: str,
    client: str,
    incoming: TextIO,
    outgoing: TextIO,
) -> int:
    """Private parent/child JSON-RPC stream; never publish it as an MCP tool."""
    gate: ExecutionGuard | None = None
    while True:
        ident = None
        try:
            line = incoming.readline(MAX_REQUEST_CHARS + 1)
            if not line:
                return 0
            if len(line) > MAX_REQUEST_CHARS or not line.endswith("\n"):
                raise GuardBlocked("execution_request_invalid")
            request = json.loads(line)
            if (
                not isinstance(request, dict)
                or request.get("jsonrpc") != "2.0"
                or type(request.get("id")) is not int
                or not 1 <= request["id"] <= 2_147_483_647
                or not isinstance(request.get("params"), dict)
            ):
                raise GuardBlocked("execution_request_invalid")
            ident = request["id"]
            params = request["params"]
            scope = _scope(
                {
                    "project": project,
                    "connection": connection,
                    "client": client,
                    **{key: params.get(key) for key in ("session", "turn", "epoch")},
                }
            )
            method = request.get("method")
            result: dict[str, Any]
            if method == "context/begin" and gate is None:
                if set(params) - {"session", "turn", "epoch", "queries", "prompt"}:
                    raise GuardBlocked("execution_request_invalid")
                prompt = params.get("prompt", "")
                if not isinstance(prompt, str) or len(prompt) > 16000 or contains_sensitive(prompt):
                    raise GuardBlocked("execution_request_invalid")
                queries = params.get("queries")
                if queries is None:
                    queries = guard_queries(prompt)
                if not isinstance(queries, list):
                    raise GuardBlocked("search_required")
                gate = ExecutionGuard(reader, scope, queries)
                proof = gate.begin()
                result = {
                    "policy": gate.launcher_policy(),
                    "context": [
                        {**row, "text": text}
                        for row, text in zip(_reads(proof), proof.texts, strict=True)
                    ],
                }
            elif method == "context/check" and gate is not None:
                if set(params) != {"session", "turn", "epoch"}:
                    raise GuardBlocked("execution_request_invalid")
                gate.check(scope)
                result = {"allowed": True}
            elif method == "context/invalidate" and gate is not None:
                gate.invalidate()
                result = {"allowed": False}
            else:
                raise GuardBlocked("execution_request_invalid")
            response = {"jsonrpc": "2.0", "id": ident, "result": result}
        except Exception:
            if gate is not None:
                gate.invalidate()
            # No exception strings, prompts, credentials or raw provider responses.
            outgoing.write(
                json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": ident,
                        "error": {
                            "code": -32001,
                            "message": "Required current Context is unavailable",
                        },
                    }
                )
                + "\n"
            )
            outgoing.flush()
            return 1
        outgoing.write(json.dumps(response, ensure_ascii=False) + "\n")
        outgoing.flush()
