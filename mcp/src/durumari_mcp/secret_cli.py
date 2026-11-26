"""Pipe a newly issued value into the project vault without it reaching any output.

`secret put NAME --stdin` reads a pipe; `secret put NAME --from-cmd -- <cmd...>`
captures only the child's stdout while its stderr stays visible. `--replace` only
requests replacing a same-named secret you may write: the value is staged and a person
approves it on the secure screen (never applied directly). The value is never
printed, logged or placed in an error: output is the name, version and IDs only.
`secret run`, `secret env` and `secret cache` use values locally (`secret_local`).
"""

import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import IO, Any

from durumari_mcp.credentials import AdapterError
from durumari_mcp.json_output import emit_json

LABEL = r"[A-Za-z_][A-Za-z0-9_.-]{0,99}"
ID = r"[a-f0-9-]{36}"
MAX_CHARS = 64000
MAX_BYTES = 256000
ROUTE = "/api/v2/agent/secrets"


def add_commands(subcommands: Any) -> None:
    parser = subcommands.add_parser(
        "secret", help="Register piped secrets and list value-free vault metadata"
    )
    actions = parser.add_subparsers(dest="secret_action", required=True, parser_class=type(parser))
    put = actions.add_parser("put", help="Store a new value read from a pipe or a command")
    put.add_argument("name")
    source = put.add_mutually_exclusive_group(required=True)
    source.add_argument("--stdin", action="store_true", help="Read the value from a pipe")
    source.add_argument(
        "--from-cmd", action="store_true", help="Capture the stdout of the command after --"
    )
    put.add_argument(
        "--keep-newline",
        action="store_true",
        help="Keep the final line break that is otherwise removed once",
    )
    put.add_argument("--timeout", type=int, default=120, help="Seconds for --from-cmd")
    put.add_argument(
        "--replace",
        action="store_true",
        help="Request replacing the same-named secret you may write; a person approves it "
        "on the secure screen (nothing changes before that)",
    )
    put.add_argument("--request-key", help="Reuse to retry an approval request safely")
    put.add_argument("source_command", nargs="*", help="Issuing command after --")
    listing = actions.add_parser("list", help="List names you may see; never values")
    listing.add_argument("--after", default="")
    info = actions.add_parser("info", help="Show one secret's value-free metadata")
    info.add_argument("id")
    status = actions.add_parser("status", help="Show an approval request's state")
    status.add_argument("operation_id")
    for action in (put, listing, info, status):
        action.add_argument("--workspace", default=os.getcwd())
        action.add_argument("--profile")
        action.add_argument("--json", action="store_true", help="Print a JSON receipt")
    from durumari_mcp.secret_local import add_commands as add_local_commands

    add_local_commands(actions)


def read_capped(stream: IO[bytes]) -> bytes:
    data = bytearray()
    while len(data) <= MAX_BYTES:
        chunk = stream.read(min(65536, MAX_BYTES + 1 - len(data)))
        if not chunk:
            return bytes(data)
        data.extend(chunk)
    raise AdapterError("secret_value_too_large")


def from_stdin() -> bytes:
    stdin = sys.stdin
    if stdin is None or stdin.isatty():
        # Typing into a terminal would display the value; people use the secure screen.
        raise AdapterError("secret_stdin_requires_pipe")
    return read_capped(stdin.buffer)


def from_command(command: list[str], timeout: int) -> bytes:
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        raise AdapterError("source_command_required")
    if not 1 <= timeout <= 3600:
        raise AdapterError("invalid_command_arguments")
    executable = shutil.which(command[0])
    if executable is None:
        raise AdapterError("source_command_not_found")
    try:
        # No shell: arguments are passed as given. stderr and stdin stay attached
        # so the issuing tool can prompt and report; only stdout is captured.
        process = subprocess.Popen(
            [executable, *command[1:]], stdout=subprocess.PIPE, stderr=None, shell=False
        )
    except OSError:
        raise AdapterError("source_command_failed") from None
    expired = threading.Event()

    def stop() -> None:
        expired.set()
        process.kill()

    timer = threading.Timer(timeout, stop)
    timer.start()
    assert process.stdout is not None
    try:
        data = read_capped(process.stdout)
        code = process.wait()
    except AdapterError:
        process.kill()
        process.wait()
        raise
    finally:
        timer.cancel()
        process.stdout.close()
    if expired.is_set():
        raise AdapterError("source_command_timeout")
    if code != 0:
        print("두루마리: 발급 명령 종료 코드 " + str(code), file=sys.stderr)
        raise AdapterError("source_command_failed")
    return data


def normalize(raw: bytes, *, keep_newline: bool) -> str:
    """Strict UTF-8; one final line break removed by default; never empty or NUL."""
    try:
        value = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise AdapterError("secret_value_not_utf8") from None
    if not keep_newline:
        value = value.removesuffix("\r\n") if value.endswith("\r\n") else value.removesuffix("\n")
    if not value.strip():
        raise AdapterError("secret_value_empty")
    if "\x00" in value:
        raise AdapterError("secret_value_invalid")
    if len(value) > MAX_CHARS:
        raise AdapterError("secret_value_too_large")
    return value


def gateway_for(args: Any) -> Any:
    from durumari_mcp.context_binding import bound_profile
    from durumari_mcp.legacy import manifest_path
    from durumari_mcp.onboarding import read_manifest
    from durumari_mcp.project_credentials import load_project_profile
    from durumari_mcp.project_gateway import ProjectGateway
    from durumari_mcp.workspace_binding import binding_root

    cwd = Path(args.workspace).resolve(strict=True)
    root = binding_root(cwd)
    name = args.profile or (
        bound_profile(root, read_manifest(root)) if manifest_path(root).exists() else None
    )
    if not name:
        raise AdapterError("project_credential_missing")
    load_project_profile(name).check_workspace(str(cwd))
    os.chdir(cwd)
    return ProjectGateway(name, "mcp")


def receipt(result: dict[str, Any], name: str) -> dict[str, Any]:
    """Allowlisted fields only; a response is never echoed as received."""
    status = result.get("status")
    keys = {
        "created": ("status", "label", "id", "revision", "project_id"),
        "approval_required": (
            "status",
            "label",
            "operation_id",
            "state",
            "secure_url",
            "expires_at",
            "project_id",
        ),
    }.get(status if isinstance(status, str) else "")
    if keys is None or result.get("label") != name:
        raise AdapterError("invalid_secret_receipt")
    return {key: result.get(key) for key in keys}


def execute(args: Any) -> None:
    action = args.secret_action
    if action in {"run", "env", "cache"}:
        from durumari_mcp.secret_local import execute as execute_local

        execute_local(args, gateway_for)
        return
    if action == "put":
        if not re.fullmatch(LABEL, args.name):
            raise AdapterError("invalid_secret_name")
        if args.stdin and args.source_command:
            raise AdapterError("invalid_command_arguments")
        key = args.request_key or "secret-put:" + uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise AdapterError("invalid_request_key")
        # Resolve the approved device connection before running an issuing command.
        gateway = gateway_for(args)
        raw = from_stdin() if args.stdin else from_command(args.source_command, args.timeout)
        value = normalize(raw, keep_newline=args.keep_newline)
        del raw
        body: dict[str, Any] = {"label": args.name, "value": value, "request_key": key}
        if args.replace:
            body["replace"] = True
        del value
        from durumari_mcp.project_gateway import ContextFailure

        try:
            result = receipt(gateway.context_request(ROUTE, body, method="POST"), args.name)
        except ContextFailure as failure:
            if failure.error["code"] == "label_exists" and not args.replace:
                print(
                    "두루마리: 같은 이름이 있습니다. 교체하려면 --replace를 붙여 요청하고 "
                    "보안 화면에서 승인해 주세요.",
                    file=sys.stderr,
                )
            if failure.error["code"] == "token_metadata_required":
                print(
                    "두루마리: 토큰 비밀값은 만료 정보가 필요해 CLI로 교체할 수 없습니다. "
                    "보안 화면(웹)에서 교체해 주세요.",
                    file=sys.stderr,
                )
            raise
        finally:
            body.clear()
        if args.replace and result["status"] != "approval_required":
            raise AdapterError("invalid_secret_receipt")
        if args.json:
            emit_json(result)
        elif result["status"] == "created":
            print(f"저장됨: {result['label']} (revision {result['revision']}, id {result['id']})")
        else:
            label = "교체 승인 필요" if args.replace else "승인 필요"
            print(f"{label}: {result['label']} (요청 {result['operation_id']})")
            print("보안 화면에서 확인·승인해 주세요: " + str(result["secure_url"]))
        return
    gateway = gateway_for(args)
    if action == "list":
        if args.after and not re.fullmatch(ID, args.after):
            raise AdapterError("invalid_command_arguments")
        result = gateway.context_request(ROUTE + ("?after=" + args.after if args.after else ""))
    elif action == "info":
        if not re.fullmatch(ID, args.id):
            raise AdapterError("invalid_command_arguments")
        result = gateway.context_request(ROUTE + "/" + args.id)
    else:
        if not re.fullmatch(ID, args.operation_id):
            raise AdapterError("invalid_command_arguments")
        result = gateway.context_request(ROUTE + "/operations/" + args.operation_id)
    emit_json(result)
