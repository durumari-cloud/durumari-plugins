"""Claude Code may pipeline a 2026-07-28 discovery probe ahead of ``initialize``."""

import json
import os
import queue
import subprocess
import sys
import threading
from pathlib import Path

import pytest

SERVER = """
import logging
from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.stdio_serve import serve_stdio
logging.disable(logging.CRITICAL)
class Fixture:
    def read(self, suffix="", **query):
        return {"project_id":"synthetic", "items":[], "next_after":None}
    def status(self):
        return {"project_id":"synthetic", "ready":True}
    def operation(self, key):
        return {"request_key":key, "status":"stored", "project_id":"synthetic"}
    def mutate(self, method, suffix, body, key):
        return {"request_key":key, "status":"stored", "project_id":"synthetic"}
serve_stdio(create_context_server(Fixture()))
"""
CLIENT = {"name": "claude-code", "version": "2.1.275"}
PROBE = {
    "jsonrpc": "2.0",
    "id": "server-discover-probe-1",
    "method": "server/discover",
    "params": {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": CLIENT,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    },
}
INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": CLIENT},
}


class Server:
    def __init__(self) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-c", SERVER],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
        )
        self.lines: queue.Queue[dict] = queue.Queue()
        self.seen: dict[object, dict] = {}
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.process.stdout:
            self.lines.put(json.loads(line))

    def send(self, *messages: dict) -> None:
        # One write, as Claude Code pipelines the probe and the handshake.
        self.process.stdin.write(b"".join(json.dumps(m).encode() + b"\n" for m in messages))
        self.process.stdin.flush()

    def reply(self, request_id: object) -> dict:
        while request_id not in self.seen:
            message = self.lines.get(timeout=20)
            self.seen[message.get("id")] = message
        return self.seen.pop(request_id)

    def close(self) -> None:
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()


@pytest.fixture
def server():
    running = Server()
    yield running
    running.close()


def handshake_then_tools(server: Server) -> set[str]:
    initialized = server.reply(0)
    assert "result" in initialized, initialized
    server.send(
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    return {tool["name"] for tool in server.reply(1)["result"]["tools"]}


@pytest.mark.parametrize("order", ["probe-first", "initialize-first"])
def test_pipelined_probe_and_initialize_connect_in_either_order(server, order):
    server.send(*([PROBE, INITIALIZE] if order == "probe-first" else [INITIALIZE, PROBE]))
    assert "durumari_context_search" in handshake_then_tools(server)
    # The probe is refused by the handshake era in both orders, never answered as modern.
    assert "error" in server.reply(PROBE["id"])


def test_a_probe_without_initialize_still_opens_the_modern_era(server):
    server.send(PROBE)
    discovered = server.reply(PROBE["id"])
    assert "result" in discovered, discovered
