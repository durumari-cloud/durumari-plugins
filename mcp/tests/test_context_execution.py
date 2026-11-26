"""The controlled executor owns its evidence; MCP arguments cannot manufacture it."""

import io
import json
import os
from argparse import Namespace
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from mcp import Client
from test_cloud_protocol import SyntheticOperations
from test_context_guard_read import SCOPE, SHA, TEXT, Reader

from durumari_mcp import plugin_env
from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.context_execution import ExecutionGuard, run_worker
from durumari_mcp.context_guard_read import GuardBlocked
from durumari_mcp.credentials import AdapterError


def guard(reader=None):
    return ExecutionGuard(reader or Reader(), SCOPE, ["자원"])


def test_check_without_owned_full_read_is_refused():
    with pytest.raises(GuardBlocked, match="execution_read_required"):
        guard().check(SCOPE)


def test_each_execution_check_repeats_search_full_read_and_current_metadata():
    reader = Reader()
    gate = guard(reader)
    assert gate.begin().texts == (TEXT,)
    reader.calls.clear()
    gate.check(SCOPE)
    assert [path for path, _ in reader.calls] == ["/search", "/context", "/context"]
    assert gate.launcher_policy()["reads"] == [
        {
            "context": "context",
            "section": "section",
            "revision": 3,
            "sha256": SHA,
        }
    ]


@pytest.mark.parametrize("field", ["project", "connection", "client", "session", "turn", "epoch"])
def test_other_execution_scope_cannot_reuse_the_owned_read(field):
    gate = guard()
    gate.begin()
    with pytest.raises(GuardBlocked, match="execution_scope_changed"):
        gate.check(replace(SCOPE, **{field: "other"}))
    with pytest.raises(GuardBlocked):
        gate.check(SCOPE)


def test_new_current_revision_blocks_even_after_a_successful_fresh_read():
    reader = Reader()
    gate = guard(reader)
    gate.begin()
    reader.search["items"][0]["revision"] = 4
    for value in (reader.document, reader.head):
        value["revision"] = value["efficacy"]["current_revision"] = 4
    with pytest.raises(GuardBlocked, match="execution_context_changed"):
        gate.check(SCOPE)


def test_new_related_section_cannot_be_ignored_by_only_refreshing_the_old_head():
    reader = Reader()
    gate = guard(reader)
    gate.begin()
    reader.search["items"][0]["section_id"] = "new-section"
    for value in (reader.document, reader.head):
        value["sections"][0]["id"] = "new-section"
    with pytest.raises(GuardBlocked, match="execution_context_changed"):
        gate.check(SCOPE)


@pytest.mark.parametrize(
    "error", [OSError("private network address"), RuntimeError("private auth")]
)
def test_transport_failure_is_sticky_and_does_not_fall_back_to_the_previous_read(error):
    reader = Reader()
    gate = guard(reader)
    gate.begin()
    reader.error = error
    with pytest.raises(GuardBlocked, match="server_unavailable"):
        gate.check(SCOPE)
    reader.error = None
    with pytest.raises(GuardBlocked, match="execution_read_required"):
        gate.check(SCOPE)


def test_no_result_after_begin_blocks_the_next_action():
    reader = Reader()
    gate = guard(reader)
    gate.begin()
    reader.search["items"] = []
    with pytest.raises(GuardBlocked, match="search_required"):
        gate.check(SCOPE)


def test_compaction_or_resume_invalidation_needs_a_new_owned_execution():
    gate = guard()
    gate.begin()
    gate.invalidate()
    with pytest.raises(GuardBlocked):
        gate.check(SCOPE)
    with pytest.raises(GuardBlocked):
        gate.begin()


def test_parallel_checks_wait_for_the_inflight_read_instead_of_losing_evidence():
    reader, entered, release, second_started = Reader(), Event(), Event(), Event()
    gate = guard(reader)
    gate.begin()
    original = reader.read

    def delayed(suffix="", **query):
        if suffix == "/search" and not entered.is_set():
            entered.set()
            assert release.wait(3)
        return original(suffix, **query)

    def second():
        second_started.set()
        gate.check(SCOPE)

    reader.read = delayed
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(gate.check, SCOPE)
        assert entered.wait(3)
        another = pool.submit(second)
        try:
            assert second_started.wait(3)
        finally:
            release.set()
        first.result(timeout=3)
        another.result(timeout=3)


def test_launch_policy_is_only_metadata_and_the_mcp_process_rereads_the_actual_server():
    gate = guard()
    gate.begin()
    policy = gate.launcher_policy()
    assert TEXT not in json.dumps(policy) and set(policy) == {
        "version",
        "scope",
        "queries",
        "reads",
    }
    reader = Reader()
    restored = ExecutionGuard.from_launcher_policy(reader, "project", "connection", "codex", policy)
    assert len(reader.calls) == 3
    reader.calls.clear()
    restored.check(SCOPE)
    assert len(reader.calls) == 3


@pytest.mark.parametrize(
    "change",
    [
        {"version": 2},
        {"queries": []},
        {"reads": []},
        {"scope": {**SCOPE.__dict__, "connection": "other"}},
    ],
)
def test_invalid_or_other_connection_launch_policy_is_refused(change):
    gate = guard()
    gate.begin()
    with pytest.raises(GuardBlocked):
        ExecutionGuard.from_launcher_policy(
            Reader(),
            "project",
            "connection",
            "codex",
            {**gate.launcher_policy(), **change},
        )


def test_forged_metadata_never_replaces_a_real_server_read():
    gate = guard()
    gate.begin()
    policy = deepcopy(gate.launcher_policy())
    policy["reads"][0]["sha256"] = "a" * 64
    with pytest.raises(GuardBlocked, match="execution_context_changed"):
        ExecutionGuard.from_launcher_policy(Reader(), "project", "connection", "codex", policy)


@pytest.mark.anyio
async def test_actual_mcp_write_is_blocked_before_it_reaches_the_operation():
    reader, operations = Reader(), SyntheticOperations()
    gate = guard(reader)
    gate.begin()
    server = create_context_server(operations, before_tool=lambda: gate.check(SCOPE))
    async with Client(server) as client:
        payload = {"request_key": "owned-write", "title": "Result", "body": "Selected result"}
        saved = await client.call_tool("durumari_context_save", payload)
        assert not saved.is_error and len(operations.writes) == 1
        reader.error = OSError("private auth material")
        refused = await client.call_tool(
            "durumari_context_save", {**payload, "request_key": "next"}
        )
        assert refused.is_error and len(operations.writes) == 1
        assert "private auth material" not in json.dumps(refused.model_dump(mode="json"))


def test_private_stdio_worker_returns_only_real_reads_and_does_not_echo_the_prompt():
    prompt = "이 원문은 서버 검색이나 파일에 보관하지 않는다"
    begin = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "context/begin",
        "params": {
            "session": "session",
            "turn": "turn",
            "epoch": "epoch",
            "queries": ["자원"],
            "prompt": prompt,
        },
    }
    check = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "context/check",
        "params": {
            "session": "session",
            "turn": "turn",
            "epoch": "epoch",
        },
    }
    output, reader = io.StringIO(), Reader()
    assert (
        run_worker(
            reader,
            "project",
            "connection",
            "codex",
            io.StringIO(json.dumps(begin) + "\n" + json.dumps(check) + "\n"),
            output,
        )
        == 0
    )
    replies = [json.loads(line) for line in output.getvalue().splitlines()]
    assert replies[0]["result"]["context"][0]["text"] == TEXT
    assert replies[1]["result"] == {"allowed": True}
    assert prompt not in output.getvalue()
    assert all(q.get("q") == "자원" for path, q in reader.calls if path == "/search")


@pytest.mark.parametrize("wire", ["not json\n", "[]\n", "x" * 32769 + "\n"])
def test_malformed_or_oversized_worker_input_has_a_fixed_error_and_stops(wire):
    output = io.StringIO()
    assert run_worker(Reader(), "project", "connection", "codex", io.StringIO(wire), output) == 1
    assert "private" not in output.getvalue() and len(output.getvalue()) < 500


def test_worker_input_decoding_failure_does_not_expose_the_raw_stream():
    class BrokenInput:
        def readline(self, _limit):
            raise UnicodeError("private raw stream")

    output = io.StringIO()
    assert run_worker(Reader(), "project", "connection", "codex", BrokenInput(), output) == 1
    assert "private" not in output.getvalue()


def setup_cli(monkeypatch, tmp_path, reader):
    from durumari_mcp import context_cli

    monkeypatch.chdir(tmp_path)
    profile = SimpleNamespace(
        project_id="project",
        connection_id="connection",
        workspace_identity="identity",
        check_workspace=lambda _cwd: None,
    )
    monkeypatch.setattr(context_cli, "selected_workspace", lambda _cwd: tmp_path)
    monkeypatch.setattr(context_cli, "binding_root", lambda _cwd: tmp_path)
    monkeypatch.setattr(context_cli, "load_project_profile", lambda _name: profile)
    monkeypatch.setattr(context_cli, "migrate_manifest", lambda *_args: None)
    monkeypatch.setattr(
        context_cli, "ProjectGateway", lambda *_args: SimpleNamespace(profile=profile)
    )
    monkeypatch.setattr(context_cli, "ContextClient", lambda *_args: reader)
    return context_cli


def test_cli_parses_the_private_worker_and_controlled_server_policy():
    from durumari_mcp.cli import build_parser

    parser = build_parser()
    worker = parser.parse_args(
        ["context-execution", "--workspace", str(Path.cwd()), "--client", "codex"]
    )
    assert worker.command == "context-execution"
    server = parser.parse_args(["context-serve"])
    assert server.command == "context-serve"
    with pytest.raises(AdapterError):
        parser.parse_args(["context-serve", "--execution-policy", "{}"])


def test_actual_cli_dispatch_connects_the_owned_worker_to_the_existing_reader(
    monkeypatch, tmp_path
):
    reader = Reader()
    cli = setup_cli(monkeypatch, tmp_path, reader)
    request = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "context/begin",
        "params": {
            "session": "session",
            "turn": "turn",
            "epoch": "epoch",
            "queries": ["자원"],
        },
    }
    output = io.StringIO()
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(request) + "\n"))
    monkeypatch.setattr("sys.stdout", output)
    cli.execute(
        Namespace(
            command="context-execution", workspace=str(tmp_path), profile="approved", client="codex"
        )
    )
    assert json.loads(output.getvalue())["result"]["context"][0]["text"] == TEXT


def test_cli_attaches_the_gate_to_the_actual_server_boundary(monkeypatch, tmp_path):
    reader, callbacks = Reader(), []
    # The server hides a turned-off feature's tools from the public release lookup.
    reader.release = lambda: None
    gate = guard(reader)
    gate.begin()
    cli = setup_cli(monkeypatch, tmp_path, reader)

    def create(_client, *, before_tool):
        callbacks.append(before_tool)
        return SimpleNamespace(
            run=lambda **_kwargs: before_tool(), list_tools=None, call_tool=None
        )

    monkeypatch.setattr(cli, "create_context_server", create)
    # `context-serve` hands the server to `serve_stdio`; run it directly here.
    monkeypatch.setattr(cli, "serve_stdio", lambda server: server.run())
    monkeypatch.setenv(plugin_env.EXECUTION_POLICY, json.dumps(gate.launcher_policy()))
    cli.execute(
        Namespace(
            command="context-serve",
            workspace=str(tmp_path),
            profile="approved",
            client="codex",
        )
    )
    assert len(callbacks) == 1 and len(reader.calls) == 9
    assert plugin_env.EXECUTION_POLICY not in os.environ
    assert plugin_env.LEGACY[plugin_env.EXECUTION_POLICY] not in os.environ
