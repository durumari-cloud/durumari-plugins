"""Serve an MCP server over stdio, keeping a pipelined handshake ahead of a probe.

The SDK fixes a connection's protocol era from the client's first request. Claude Code
sometimes writes a 2026-07-28 ``server/discover`` probe and the ``initialize`` handshake
back to back; when the probe is read first the connection becomes modern and the
handshake is refused (-32022), so the session starts without Durumari tools. A client
that sends ``initialize`` needs the handshake era, so when one follows the opening
request within ``PROBE_WAIT`` it is served first and the probe gets the handshake era's
refusal, as when the two arrive the other way round.
"""

from __future__ import annotations

import contextvars
from types import TracebackType

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server
from mcp.shared._stream_protocols import ReadStream
from mcp.shared.message import SessionMessage
from mcp.types import JSONRPCRequest

Item = SessionMessage | Exception
PROTOCOL_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
# Pipelined frames arrive in one write; a modern-only client waits this long once.
PROBE_WAIT = 0.2


def _request(item: Item) -> JSONRPCRequest | None:
    if isinstance(item, SessionMessage) and isinstance(item.message, JSONRPCRequest):
        return item.message
    return None


def _is_probe(request: JSONRPCRequest) -> bool:
    if request.method == "initialize":
        return False
    meta = (request.params or {}).get("_meta")
    return isinstance(meta, dict) and PROTOCOL_VERSION_KEY in meta


class HandshakeFirst:
    """Read stream that moves an ``initialize`` sent right behind an opening probe ahead of it."""

    def __init__(self, inner: ReadStream[Item], wait: float = PROBE_WAIT) -> None:
        self._inner = inner
        self._wait = wait
        self._held: list[tuple[contextvars.Context | None, Item]] = []
        self._ended = False
        self._opened = False
        self.last_context: contextvars.Context | None = None

    async def _next(self) -> tuple[contextvars.Context | None, Item]:
        item = await self._inner.receive()
        return getattr(self._inner, "last_context", None), item

    async def receive(self) -> Item:
        if self._held:
            self.last_context, item = self._held.pop(0)
            return item
        if self._ended:
            raise anyio.EndOfStream
        context, item = await self._next()
        opening = _request(item)
        if not self._opened and opening is not None:
            self._opened = True
            if _is_probe(opening):
                following = None
                with anyio.move_on_after(self._wait):
                    try:
                        following = await self._next()
                    except anyio.EndOfStream:
                        self._ended = True
                if following is not None:
                    handshake = _request(following[1])
                    if handshake is not None and handshake.method == "initialize":
                        self._held.append((context, item))
                        self.last_context, item = following
                        return item
                    self._held.append(following)
        self.last_context = context
        return item

    def __aiter__(self) -> HandshakeFirst:
        return self

    async def __anext__(self) -> Item:
        try:
            return await self.receive()
        except anyio.EndOfStream:
            raise StopAsyncIteration from None

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> HandshakeFirst:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        await self.aclose()


async def _serve(server: MCPServer) -> None:
    lowlevel = server._lowlevel_server
    async with stdio_server() as (read_stream, write_stream):
        await lowlevel.run(
            HandshakeFirst(read_stream),
            write_stream,
            lowlevel.create_initialization_options(),
        )


def serve_stdio(server: MCPServer) -> None:
    """``server.run(transport="stdio")`` with the handshake kept ahead of a probe."""
    anyio.run(_serve, server)
