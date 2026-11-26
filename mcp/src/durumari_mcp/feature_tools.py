"""Which tools belong to a feature the operator can turn off.

The operations console publishes on, beta or off per feature. Off removes a feature's
tools from the tool list. The server does it for the remote MCP endpoint; the local
stdio servers (``account-serve``, ``context-serve``) do it here from the hidden list
the public release metadata carries, so no plugin release is needed to hide or show a
feature, only to learn of a feature that is new.

A call to a hidden tool (a host keeps tools it listed earlier) is refused with
``feature_unavailable``, the same code the HTTP gate answers with (404). It is not
``service_feature_paused``: that 503 with Retry-After means a temporary pause the client
may retry, while off stays until an operator turns the feature back on, so a retry hint
would mislead (decision).

The core Context tools (search, list, read, save, revise, version, operation, explore,
related, policy index, project status and the project/conversation navigation) match
nothing below and can never be hidden. The server imports this table, so both sides
agree on it.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Collection
from typing import Any

import anyio
from mcp.server.mcpserver.exceptions import ToolError

# (tool-name prefix, feature key), most specific first.
FEATURE_TOOLS: tuple[tuple[str, str], ...] = (
    ("durumari_skill_secret_links", "vault.enabled"),
    ("durumari_skill_link", "vault.enabled"),
    ("durumari_skill_unlink", "vault.enabled"),
    ("durumari_skill_", "skills.enabled"),
    ("durumari_secret_", "vault.enabled"),
    ("durumari_work_log_", "work_logs.enabled"),
    ("durumari_glossary_", "glossary.enabled"),
    ("durumari_request_", "requests.enabled"),
    ("durumari_proposal_", "requests.enabled"),
    ("durumari_reviews_browser", "requests.enabled"),
    ("durumari_meeting_transcript_", "meetings.enabled"),
    ("durumari_file_upload", "files.enabled"),
    ("durumari_files_browser", "files.enabled"),
    ("durumari_board_", "boards.enabled"),
    ("durumari_file_list", "files.enabled"),
    ("durumari_file_manage", "files.enabled"),
    ("durumari_graph_", "graph.enabled"),
    ("durumari_context_archive", "archive.enabled"),
)
UNAVAILABLE = "feature_unavailable"
MESSAGE = "지금 제공하지 않는 기능입니다."
KEY = re.compile(r"[a-z][a-z_]{0,40}\.enabled")


def feature_for_tool(name: str) -> str | None:
    return next((key for prefix, key in FEATURE_TOOLS if name.startswith(prefix)), None)


def tool_hidden(name: str, hidden: Collection[str]) -> bool:
    key = feature_for_tool(name)
    return key is not None and key in hidden


def hidden_features(release: Any) -> frozenset[str]:
    """The feature keys a release lookup reports as off; anything unexpected reports none."""
    features = release.get("features") if isinstance(release, dict) else None
    keys = features.get("hidden") if isinstance(features, dict) else None
    if not isinstance(keys, list) or len(keys) > 64:
        return frozenset()
    return frozenset(key for key in keys if isinstance(key, str) and KEY.fullmatch(key))


class HiddenFeatures:
    """The hidden feature keys, read from the release metadata at most once per ``ttl``.

    Reading fails open: a lookup that fails or does not answer keeps the last known
    list (none at first), so an outage never hides a feature the operator left on.
    """

    def __init__(
        self,
        release: Callable[[], dict[str, Any] | None],
        *,
        ttl: float = 60.0,
        wait: float = 3.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.release, self.ttl, self.wait, self.clock = release, ttl, wait, clock
        self.value: frozenset[str] = frozenset()
        self.read_at: float | None = None

    def current(self) -> frozenset[str]:
        now = self.clock()
        if self.read_at is not None and now - self.read_at < self.ttl:
            return self.value
        # Marked read first: a slow or failing lookup is retried after ``ttl``, not every call.
        self.read_at = now
        try:
            value = self.release()
        except Exception:  # noqa: BLE001 - fail open, see the class docstring
            return self.value
        if value is not None:
            self.value = hidden_features(value)
        return self.value

    async def acurrent(self) -> frozenset[str]:
        with anyio.move_on_after(self.wait):
            return await anyio.to_thread.run_sync(self.current, abandon_on_cancel=True)
        return self.value


def hide_unavailable(server: Any, hidden: HiddenFeatures) -> Any:
    """Keep a hidden feature's tools out of the list and refuse a call that names one."""
    list_tools, call_tool = server.list_tools, server.call_tool

    async def scoped_list() -> list[Any]:
        off = await hidden.acurrent()
        return [tool for tool in await list_tools() if not tool_hidden(tool.name, off)]

    async def scoped_call(name: str, arguments: dict[str, Any], context: Any = None) -> Any:
        # Hosts keep tool definitions they listed earlier: answer with a stable code.
        if tool_hidden(name, await hidden.acurrent()):
            raise ToolError(f"{UNAVAILABLE}: {MESSAGE}")
        return await call_tool(name, arguments, context)

    server.list_tools, server.call_tool = scoped_list, scoped_call
    return server
