"""`secret put` keeps a piped value out of every output; synthetic values only."""

import io
import json
import sys

import pytest
from mcp import Client
from test_continuity import project_store as project_store_fixture

from durumari_mcp import cli, secret_cli
from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.credentials import AdapterError

project_store = project_store_fixture
VALUE = "synthetic-issued-token-7f3a9c1e5b"
SECRET_ID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OPERATION_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


class Gateway:
    def __init__(self, result=None):
        self.calls = []
        self.result = result

    def context_request(self, path, body=None, *, method="GET", idempotency_key=None):
        self.calls.append((path, dict(body) if body else None, method))
        if self.result is not None:
            return self.result
        return {
            "status": "created",
            "id": SECRET_ID,
            "project_id": "project",
            "label": body["label"],
            "revision": 1,
            "policy_revision": 1,
            "unexpected": VALUE,
        }


class Pipe(io.BytesIO):
    def isatty(self):
        return False


def run(monkeypatch, capfd, argv, *, stdin=None, gateway=None):
    gateway = gateway or Gateway()
    monkeypatch.setattr(secret_cli, "gateway_for", lambda args: gateway)
    if stdin is not None:
        monkeypatch.setattr(
            sys, "stdin", io.TextIOWrapper(Pipe(stdin), encoding="utf-8", newline="")
        )
    monkeypatch.setattr(sys, "argv", ["durumari-mcp", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exit:
        code = exit.code or 0
    out, err = capfd.readouterr()
    return code, out, err, gateway


def child(script):
    return [sys.executable, "-c", script]


def test_stdin_value_is_sent_once_and_never_printed(monkeypatch, capfd):
    code, out, err, gateway = run(
        monkeypatch,
        capfd,
        ["secret", "put", "OPENAI_API_KEY", "--stdin"],
        stdin=VALUE.encode() + b"\n",
    )
    assert code == 0, err
    assert out == f"저장됨: OPENAI_API_KEY (revision 1, id {SECRET_ID})\n"
    [(path, body, method)] = gateway.calls
    assert (path, method) == ("/api/v2/agent/secrets", "POST")
    assert body["label"] == "OPENAI_API_KEY" and body["value"] == VALUE
    assert body["request_key"].startswith("secret-put:")
    assert VALUE not in out + err


def test_json_receipt_is_allowlisted(monkeypatch, capfd):
    code, out, err, _ = run(
        monkeypatch,
        capfd,
        ["secret", "put", "OPENAI_API_KEY", "--stdin", "--json"],
        stdin=VALUE.encode(),
    )
    assert code == 0, err
    assert json.loads(out) == {
        "status": "created",
        "label": "OPENAI_API_KEY",
        "id": SECRET_ID,
        "revision": 1,
        "project_id": "project",
    }
    assert VALUE not in out + err


def test_from_cmd_captures_stdout_only_and_shows_child_stderr(monkeypatch, capfd):
    script = (
        "import sys; sys.stderr.write('issuer progress\\n'); "
        f"sys.stdout.write({VALUE!r} + '\\r\\n')"
    )
    code, out, err, gateway = run(
        monkeypatch,
        capfd,
        ["secret", "put", "GH_TOKEN", "--from-cmd", "--", *child(script)],
    )
    assert code == 0, err
    assert "issuer progress" in err
    assert gateway.calls[0][1]["value"] == VALUE
    assert VALUE not in out + err


def test_from_cmd_separator_preserves_child_options_without_changing_the_gateway():
    command = [
        "issuer",
        "--profile",
        "child-profile",
        "--workspace",
        "child-workspace",
        "--stdin",
        "--timeout",
        "0",
        "--",
        "argument",
    ]
    args = cli.parse_arguments(
        cli.build_parser(),
        [
            "secret",
            "put",
            "KEY",
            "--profile",
            "approved-profile",
            "--workspace",
            "approved-workspace",
            "--timeout",
            "7",
            "--from-cmd",
            "--",
            *command,
        ],
    )
    assert args.from_cmd and not args.stdin and args.timeout == 7
    assert args.profile == "approved-profile" and args.workspace == "approved-workspace"
    assert args.source_command == command


def test_approval_receipt_prints_only_the_secure_link(monkeypatch, capfd):
    result = {
        "status": "approval_required",
        "label": "GH_TOKEN",
        "operation_id": OPERATION_ID,
        "project_id": "project",
        "state": "ready_for_review",
        "secure_url": "https://durumari.example/secure/secret-operations/" + OPERATION_ID,
        "expires_at": "2026-10-18T00:00:00+00:00",
    }
    code, out, err, _ = run(
        monkeypatch,
        capfd,
        ["secret", "put", "GH_TOKEN", "--stdin"],
        stdin=VALUE.encode(),
        gateway=Gateway(result),
    )
    assert code == 0, err
    assert result["secure_url"] in out and VALUE not in out + err


@pytest.mark.parametrize(
    "raw,keep,expected",
    [
        (b"abc\n", False, "abc"),
        (b"abc\r\n", False, "abc"),
        (b"abc\n\n", False, "abc\n"),
        (b"abc\r", False, "abc\r"),
        (b"abc\n", True, "abc\n"),
        (b"line1\nline2\n", False, "line1\nline2"),
        ("가나다\n".encode(), False, "가나다"),
    ],
)
def test_newline_rule_removes_one_final_line_break(raw, keep, expected):
    assert secret_cli.normalize(raw, keep_newline=keep) == expected


@pytest.mark.parametrize(
    "raw,code",
    [
        (b"", "secret_value_empty"),
        (b"\n", "secret_value_empty"),
        (b"  \t\n", "secret_value_empty"),
        (b"\xff\xfe" + VALUE.encode(), "secret_value_not_utf8"),
        (VALUE.encode() + b"\x00", "secret_value_invalid"),
        (b"v" * 64001, "secret_value_too_large"),
    ],
)
def test_invalid_values_fail_with_fixed_codes(monkeypatch, capfd, raw, code):
    status, out, err, gateway = run(
        monkeypatch, capfd, ["secret", "put", "OPENAI_API_KEY", "--stdin"], stdin=raw
    )
    assert status == 2 and err.strip() == "두루마리: " + code
    assert not gateway.calls and VALUE not in out + err


def test_oversized_pipe_stops_reading_at_the_byte_cap():
    with pytest.raises(AdapterError, match="secret_value_too_large"):
        secret_cli.read_capped(io.BytesIO(b"v" * (secret_cli.MAX_BYTES + 1)))
    assert secret_cli.read_capped(io.BytesIO(b"v" * secret_cli.MAX_BYTES)) == b"v" * 256000


@pytest.mark.parametrize(
    "argv,code",
    [
        (["secret", "put", "BAD NAME", "--stdin"], "invalid_secret_name"),
        (["secret", "put", "1KEY", "--stdin"], "invalid_secret_name"),
        (["secret", "put", "KEY"], "invalid_command_arguments"),
        (["secret", "put", "KEY", "--stdin", "--from-cmd"], "invalid_command_arguments"),
        (["secret", "put", "KEY", "--stdin", "--", "issuer"], "invalid_command_arguments"),
        (
            ["secret", "put", "KEY", "issuer", "--from-cmd", "--", "other"],
            "invalid_command_arguments",
        ),
        (["secret", "put", "KEY", "--from-cmd"], "source_command_required"),
        (
            ["secret", "put", "KEY", "--from-cmd", "--", "durumari-missing-cmd"],
            "source_command_not_found",
        ),
        (["secret", "put", VALUE + " --stdin"], "invalid_command_arguments"),
    ],
)
def test_argument_errors_never_echo_input(monkeypatch, capfd, argv, code):
    status, out, err, gateway = run(monkeypatch, capfd, argv, stdin=b"x")
    assert status == 2 and err.strip() == "두루마리: " + code
    assert not gateway.calls and VALUE not in out + err


def test_failing_or_slow_issuer_sends_nothing(monkeypatch, capfd):
    failed = child(f"import sys; sys.stdout.write({VALUE!r}); sys.exit(3)")
    status, out, err, gateway = run(
        monkeypatch, capfd, ["secret", "put", "KEY", "--from-cmd", "--", *failed]
    )
    assert status == 2 and "source_command_failed" in err and "종료 코드 3" in err
    slow = child(
        f"import sys, time; sys.stdout.write({VALUE!r}); sys.stdout.flush(); time.sleep(30)"
    )
    status, out2, err2, gateway2 = run(
        monkeypatch, capfd, ["secret", "put", "KEY", "--timeout", "1", "--from-cmd", "--", *slow]
    )
    assert status == 2 and "source_command_timeout" in err2
    huge = child("import sys; sys.stdout.write('v' * 300000)")
    status, out3, err3, gateway3 = run(
        monkeypatch, capfd, ["secret", "put", "KEY", "--from-cmd", "--", *huge]
    )
    assert status == 2 and "secret_value_too_large" in err3
    assert not (gateway.calls or gateway2.calls or gateway3.calls)
    assert VALUE not in out + err + out2 + err2 + out3 + err3


def test_terminal_stdin_is_refused(monkeypatch, capfd):
    class Terminal(io.BytesIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(Terminal(VALUE.encode())))
    status, out, err, gateway = run(monkeypatch, capfd, ["secret", "put", "KEY", "--stdin"])
    assert status == 2 and err.strip() == "두루마리: secret_stdin_requires_pipe"
    assert not gateway.calls


def test_server_rejection_and_unexpected_receipt_never_echo(monkeypatch, capfd):
    from durumari_mcp.project_gateway import ContextFailure

    class Rejecting(Gateway):
        def context_request(self, path, body=None, **kwargs):
            raise ContextFailure(409, {"code": "label_exists", "message": VALUE})

    status, out, err, _ = run(
        monkeypatch,
        capfd,
        ["secret", "put", "KEY", "--stdin"],
        stdin=VALUE.encode(),
        gateway=Rejecting(),
    )
    assert status == 2 and err.strip().endswith("두루마리: label_exists")
    status, out2, err2, _ = run(
        monkeypatch,
        capfd,
        ["secret", "put", "KEY", "--stdin"],
        stdin=VALUE.encode(),
        gateway=Gateway({"status": "created", "label": "OTHER", "value": VALUE}),
    )
    assert status == 2 and err2.strip() == "두루마리: invalid_secret_receipt"
    assert VALUE not in out + err + out2 + err2


@pytest.mark.parametrize("platform", ["win32", "darwin", "linux"])
def test_put_never_touches_os_credential_stores_or_local_files(
    monkeypatch, capfd, tmp_path, platform
):
    """Registration needs only the existing connection; no new store entries or files."""
    from durumari_mcp import credentials, project_credentials, skill_cli

    def forbidden():
        raise AssertionError("secret put must not open an OS credential store")

    for module in (credentials, project_credentials, skill_cli):
        monkeypatch.setattr(module, "os_store", forbidden)
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
    monkeypatch.chdir(tmp_path)
    code, out, err, _ = run(
        monkeypatch, capfd, ["secret", "put", "KEY", "--stdin"], stdin=VALUE.encode()
    )
    assert code == 0, err
    assert list(tmp_path.rglob("*")) == []


def test_read_commands_use_value_free_routes(monkeypatch, capfd):
    gateway = Gateway({"items": []})
    for argv, path in [
        (["secret", "list"], "/api/v2/agent/secrets"),
        (["secret", "list", "--after", SECRET_ID], "/api/v2/agent/secrets?after=" + SECRET_ID),
        (["secret", "info", SECRET_ID], "/api/v2/agent/secrets/" + SECRET_ID),
        (["secret", "status", OPERATION_ID], "/api/v2/agent/secrets/operations/" + OPERATION_ID),
    ]:
        code, out, err, _ = run(monkeypatch, capfd, argv, gateway=gateway)
        assert code == 0, err
        assert gateway.calls[-1] == (path, None, "GET")
    code, out, err, _ = run(monkeypatch, capfd, ["secret", "info", "../x"], gateway=gateway)
    assert code == 2 and len(gateway.calls) == 4


@pytest.mark.parametrize(
    "path,allowed",
    [
        ("/api/v2/agent/secrets", True),
        ("/api/v2/agent/secrets?after=" + SECRET_ID, True),
        ("/api/v2/agent/secrets/" + SECRET_ID, True),
        ("/api/v2/agent/secrets/operations/" + OPERATION_ID, True),
        ("/api/v2/agent/secrets/" + SECRET_ID + "/reveal", False),
        ("/api/v2/agent/secrets/operations/" + OPERATION_ID + "/approve", False),
        ("/api/v2/projects/project/skill-secrets", False),
    ],
)
def test_gateway_allows_only_the_agent_secret_routes(
    project_store, tmp_path, monkeypatch, path, allowed
):
    from test_context_v2_adapter import context_profile

    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.project_gateway import ProjectGateway

    monkeypatch.chdir(tmp_path)
    save_project_profile("synthetic", context_profile(tmp_path))
    gateway = ProjectGateway("synthetic", "mcp", root=tmp_path / "state")
    seen = []
    gateway.transport.request = lambda route, **kwargs: seen.append(route) or {"ok": True}
    if allowed:
        assert gateway.context_request(path) == {"ok": True} and seen == [path]
    else:
        with pytest.raises(AdapterError, match="invalid_project_operation"):
            gateway.context_request(path, {}, method="POST")
        assert seen == []


class Operations:
    def __init__(self):
        self.calls = []

    def read(self, suffix="", **query):
        return {}

    def mutate(self, method, suffix, body, key):
        return {}

    def operation(self, key):
        return {}

    def status(self):
        return {}

    def policies(self, after=None):
        return {}

    def secrets(self, suffix="", **query):
        self.calls.append((suffix, query))
        return {"items": [{"id": SECRET_ID, "label": "GH_TOKEN"}], "next_after": None}


@pytest.mark.anyio
async def test_mcp_secret_tools_are_read_only_titled_and_take_no_value():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        for name in ("durumari_secret_list", "durumari_secret_status"):
            tool = tools[name]
            assert tool.title and tool.annotations.title == tool.title
            assert tool.annotations.read_only_hint and not tool.annotations.destructive_hint
            assert not {"value", "secret", "token", "password"} & set(
                tool.input_schema.get("properties", {})
            )
        assert tools["durumari_secret_list"].title == "List secret names and metadata"
        assert "never values" in tools["durumari_secret_list"].description.lower()
        listed = await client.call_tool("durumari_secret_list", {})
        assert json.loads(listed.content[0].text)["items"][0]["label"] == "GH_TOKEN"
        await client.call_tool("durumari_secret_list", {"secret_id": SECRET_ID})
        await client.call_tool("durumari_secret_status", {"operation_id": OPERATION_ID})
        assert operations.calls == [
            ("", {"after": ""}),
            ("/" + SECRET_ID, {}),
            ("/operations/" + OPERATION_ID, {}),
        ]
        rejected = await client.call_tool(
            "durumari_secret_list", {"secret_id": SECRET_ID, "value": VALUE}
        )
        assert rejected.is_error and VALUE not in json.dumps(rejected.model_dump(mode="json"))
        bad = await client.call_tool("durumari_secret_status", {"operation_id": "../approve"})
        assert bad.is_error and len(operations.calls) == 3


@pytest.mark.anyio
async def test_secret_tools_are_hidden_and_refused_per_request():
    operations = Operations()
    allowed = {"value": False}
    server = create_context_server(operations, secret_tools=lambda: allowed["value"])
    async with Client(server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert "durumari_context_search" in names and not any("secret" in n for n in names)
        refused = await client.call_tool("durumari_secret_list", {})
        assert json.loads(refused.content[0].text) == {"error": {"code": "tool_unavailable"}}
        assert operations.calls == []
        allowed["value"] = True
        names = {tool.name for tool in (await client.list_tools()).tools}
        assert {"durumari_secret_list", "durumari_secret_status"} <= names


def test_replace_sends_flag_and_prints_only_the_secure_link(monkeypatch, capfd):
    result = {
        "status": "approval_required",
        "label": "GH_TOKEN",
        "operation_id": OPERATION_ID,
        "project_id": "project",
        "state": "ready_for_review",
        "secure_url": "https://durumari.example/secure/secret-operations/" + OPERATION_ID,
        "expires_at": "2026-10-18T00:00:00+00:00",
    }
    code, out, err, gateway = run(
        monkeypatch,
        capfd,
        ["secret", "put", "GH_TOKEN", "--stdin", "--replace"],
        stdin=VALUE.encode(),
        gateway=Gateway(result),
    )
    assert code == 0, err
    assert out.startswith("교체 승인 필요: GH_TOKEN") and OPERATION_ID in out
    assert gateway.calls[0][1]["replace"] is True
    assert VALUE not in out + err


def test_replace_refuses_a_direct_created_receipt(monkeypatch, capfd):
    code, out, err, _ = run(
        monkeypatch,
        capfd,
        ["secret", "put", "KEY", "--stdin", "--replace"],
        stdin=VALUE.encode(),
    )
    assert code == 2 and err.strip() == "두루마리: invalid_secret_receipt"
    assert VALUE not in out + err


def test_label_exists_without_replace_hints_at_replace(monkeypatch, capfd):
    from durumari_mcp.project_gateway import ContextFailure

    class Rejecting(Gateway):
        def context_request(self, path, body=None, **kwargs):
            raise ContextFailure(409, {"code": "label_exists"})

    code, out, err, gateway = run(
        monkeypatch,
        capfd,
        ["secret", "put", "KEY", "--stdin"],
        stdin=VALUE.encode(),
        gateway=Rejecting(),
    )
    assert code == 2 and "--replace" in err and "두루마리: label_exists" in err
    assert VALUE not in out + err


def test_token_secret_replace_hints_at_the_secure_screen(monkeypatch, capfd):
    from durumari_mcp.project_gateway import ContextFailure

    class Rejecting(Gateway):
        def context_request(self, path, body=None, **kwargs):
            raise ContextFailure(422, {"code": "token_metadata_required"})

    code, out, err, _ = run(
        monkeypatch,
        capfd,
        ["secret", "put", "KEY", "--stdin", "--replace"],
        stdin=VALUE.encode(),
        gateway=Rejecting(),
    )
    assert code == 2 and "보안 화면" in err and "두루마리: token_metadata_required" in err
    assert VALUE not in out + err
