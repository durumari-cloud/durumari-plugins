"""Interactive credential provisioning is separate from the MCP protocol process."""

import argparse
import json
import logging
import os
import shlex
import sys
from importlib.metadata import version
from pathlib import Path
from typing import Any, Never

from durumari_mcp.credentials import (
    AdapterError,
    Profile,
    delete_profile,
)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        # An accidentally pasted credential must not be echoed by argparse.
        raise AdapterError("invalid_command_arguments")


def parse_arguments(parser: Parser, argv: list[str]) -> Any:
    if argv[:2] == ["secret", "put"] and "--" in argv:
        boundary = argv.index("--")
        # argparse's '*' positional cannot resume after NAME and interleaved
        # options. Parse only our options, then keep the issuer argv opaque:
        # child --profile/--workspace/--stdin must never select our authority.
        args = parser.parse_args(argv[:boundary])
        if args.source_command:
            raise AdapterError("invalid_command_arguments")
        args.source_command = argv[boundary + 1:]
        return args
    return parser.parse_args(argv)


def stdin_line(max_bytes: int) -> bytes:
    # No buffered read-ahead: a protocol message may follow a bootstrap frame
    # in the same pipe write. The SDK must receive every remaining byte.
    data = bytearray()
    while len(data) <= max_bytes:
        byte = os.read(0, 1)
        if byte == b"\n":
            return bytes(data).rstrip(b"\r")
        if not byte:
            raise AdapterError("bootstrap_input_incomplete")
        data.extend(byte)
    raise AdapterError("bootstrap_input_too_large")


def bootstrap_profile() -> Profile:
    try:
        frame = json.loads(stdin_line(8192))
        if (
            set(frame) != {"jsonrpc", "method", "params"}
            or frame["jsonrpc"] != "2.0"
            or frame["method"] != "durumari/bootstrap"
        ):
            raise AdapterError("invalid_bootstrap_frame")
        return Profile.model_validate(frame["params"])
    except AdapterError:
        raise
    except Exception:
        raise AdapterError("invalid_bootstrap_frame") from None


def build_parser() -> Parser:
    parser = Parser(prog="durumari-mcp")
    parser.add_argument("--version", action="version", version=version("durumari-mcp"))
    subcommands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    configure = subcommands.add_parser("configure", help="Store a short Plugin credential securely")
    configure.add_argument("--profile", required=True)
    configure.add_argument("--gateway-origin", required=True)
    configure.add_argument("--client", choices=["codex", "claude"], required=True)
    configure.add_argument(
        "--destination", choices=["codex-model", "claude-model", "local-tool"], required=True
    )
    configure.add_argument("--stdin", action="store_true", help="Read credential line from a pipe")
    serve = subcommands.add_parser("serve", help="Run the MCP stdio server")
    serve.add_argument("--profile", required=True)
    serve.add_argument("--client", choices=["codex", "claude"], required=True)
    ephemeral = subcommands.add_parser("serve-stdin", help="Use one in-memory bootstrap frame")
    ephemeral.add_argument("--client", choices=["codex", "claude"], required=True)
    forget = subcommands.add_parser("forget", help="Remove a locally stored credential")
    forget.add_argument("--profile", required=True)
    connect = subcommands.add_parser("project-connect", help="Guided project login and recovery")
    connect.add_argument("--workspace", default=os.getcwd())
    connect.add_argument("--client", choices=["codex", "claude"], required=True)
    connect.add_argument("--gateway-origin")
    connect.add_argument("--reauth", action="store_true")
    connect.add_argument("--browser-login", action="store_true")
    connect.add_argument("--allow-external-context", action="store_true")
    connect.add_argument("--for-migration", action="store_true")
    connect.add_argument("--create-workspace", action="store_true")
    connect.add_argument("--requested-project")
    connect.add_argument(
        "--trust-workspace",
        action="store_true",
        help="Register this exact Codex workspace after explicit human consent",
    )
    for command in [
        "project-login",
        "project-configure",
        "project-serve",
        "project-hook",
        "project-sync",
        "project-status",
        "project-logout",
        "project-forget",
    ]:
        project = subcommands.add_parser(command, help="Scoped project continuity operation")
        project.add_argument("--profile", required=True)
        project.add_argument("--client", choices=["codex", "claude"], required=True)
        if command in {"project-login", "project-configure"}:
            project.add_argument("--gateway-origin", required=True)
            project.add_argument("--workspace", required=True)
        if command == "project-login":
            project.add_argument("--project-id", required=True)
            project.add_argument("--source-id", required=True)
            project.add_argument("--environment-name", required=True)
            project.add_argument("--allow-external-context", action="store_true")
        if command == "project-configure":
            project.add_argument("--stdin", action="store_true")
        if command in {"project-logout", "project-forget"}:
            project.add_argument("--discard-pending", action="store_true")
    for name in ("release-install", "update"):
        release = subcommands.add_parser(name, help="Install or update the public native plugin")
        release.add_argument("--server")
        release.add_argument(
            "--client", choices=["codex", "claude", "both", "cursor", "gemini", "antigravity"]
        )
        release.add_argument("--check", action="store_true")
        release.add_argument("--status", action="store_true")
        release.add_argument(
            "--debug",
            action="store_true",
            help=(
                "Include host output in the local failure report; "
                "review for secrets before sharing."
            ),
        )
    from durumari_mcp.onboarding_cli import add_commands

    add_commands(subcommands)
    from durumari_mcp.context_cli import add_commands as add_context_commands

    add_context_commands(subcommands)
    from durumari_mcp.import_cli import add_commands as add_import_commands

    add_import_commands(subcommands)
    from durumari_mcp.account_cli import add_commands as add_account_commands

    add_account_commands(subcommands)
    from durumari_mcp.account_setup_cli import add_commands as add_account_setup_commands

    add_account_setup_commands(subcommands)
    from durumari_mcp.skill_cli import add_commands as add_skill_commands

    add_skill_commands(subcommands)
    from durumari_mcp.secret_cli import add_commands as add_secret_commands

    add_secret_commands(subcommands)
    from durumari_mcp.prompt_context import add_command as add_prompt_context_command

    add_prompt_context_command(subcommands)
    visible = {
        "secret",
        "account-connect",
        "account-serve",
        "account-setup",
        "account-migrate",
        "project-skills",
        "project-device",
        "context-connect",
        "context-serve",
        "context-status",
        "context-remove",
        "context",
        "import",
        "prompt-context",
        "release-install",
        "update",
    }
    subcommands._choices_actions = [
        action for action in subcommands._choices_actions if action.dest in visible
    ]
    subcommands.metavar = "{" + ",".join(sorted(visible)) + "}"
    return parser


def hook_parser() -> Parser:
    """Hooks run on every session start, message and stop; their arguments are fixed.

    Building the full parser imports the MCP SDK (about a second), which every
    submitted message would otherwise wait for.
    """
    parser = Parser(prog="durumari-mcp")
    subcommands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    hook = subcommands.add_parser("project-hook")
    hook.add_argument("--profile", required=True)
    hook.add_argument("--client", choices=["codex", "claude"], required=True)
    return parser


def execute(args: Any) -> None:
    if args.command in {"account-setup", "account-migrate"}:
        from durumari_mcp.account_setup_cli import execute as execute_account_setup

        execute_account_setup(args)
        return
    if args.command in {"account-connect", "account-serve"}:
        from durumari_mcp.account_cli import execute as execute_account

        execute_account(args)
        return
    if args.command == "secret":
        from durumari_mcp.secret_cli import execute as execute_secret

        execute_secret(args)
        return
    if args.command == "prompt-context":
        from durumari_mcp.prompt_context import execute as execute_prompt_context

        execute_prompt_context(args)
        return
    if args.command in {"project-skills", "project-device"}:
        from durumari_mcp.skill_cli import execute as execute_skills

        execute_skills(args)
        return
    if args.command == "import":
        from durumari_mcp.import_cli import execute as execute_import

        execute_import(args)
        return
    if args.command.startswith("context"):
        from durumari_mcp.context_cli import execute as execute_context

        execute_context(args)
        return
    if args.command in {"release-install", "update"}:
        from durumari_mcp.releases import ReleaseError, execute_cli_release

        try:
            print(json.dumps(execute_cli_release(args)))
        except ReleaseError as error:
            if error.diagnostic_path:
                print(json.dumps({"diagnostic_path": error.diagnostic_path}), file=sys.stderr)
            raise AdapterError(str(error)) from None
        return
    if args.command in {
        "project-connect",
        "project-init",
        "project-doctor",
        "project-preflight",
        "project-import",
        "project-export",
    }:
        # Replaced by context-connect and context-status [--change]. Only unsupported 0.3.x
        # wrappers call these; the old checks cannot read current context-serve settings,
        # project-connect configured every Agent and dropped --reauth, and import/export
        # called retired v1 endpoints (410).
        raise AdapterError("agent_upgrade_required_use_context_connect")
    if args.command.startswith("project-"):
        if args.command == "project-serve":
            from durumari_mcp.project_credentials import load_project_profile

            if not load_project_profile(args.profile).context_only:
                raise AdapterError("agent_upgrade_required_use_context_connect")
            from durumari_mcp.context_cli import execute as execute_context

            args.command = "context-serve"
            args.workspace = os.getcwd()
            execute_context(args)
            return
        if args.command != "project-hook":
            raise AdapterError("agent_upgrade_required_use_context_connect")
        from durumari_mcp.project_cli import execute_project

        execute_project(args, stdin_line)
        return
    if args.command == "forget":
        delete_profile(args.profile)
        print(
            "Durumari credential removed locally; revoke the connection in Durumari too.",
            file=sys.stderr,
        )
        return
    raise AdapterError("feature_retired_use_context_connect")


CONTEXT_ARGUMENT_GUIDANCE = {
    "context_id_required": "--id <Context ID>를 함께 지정해 주세요.",
    "context_query_required": "--query <검색어>를 함께 지정해 주세요.",
    "context_request_key_required": (
        "--request-key <안정적인 요청 키>를 함께 지정해 주세요. 같은 변경을 다시 보낼 때는 "
        "같은 키를 씁니다."
    ),
    "context_input_required": "--input <JSON 파일>(표준 입력은 -)을 함께 지정해 주세요.",
    "context_input_fields_required": "--input JSON에 필요한 revision 값이 빠졌습니다.",
}
CONTEXT_INPUT_EXAMPLES = {
    "revise": (
        '{"section_appends": [{"id": "<절 id>", "text": "<덧붙일 한 줄>"}]} — 읽지 않고 절 끝에 '
        "붙입니다(base_revision·sha256 불필요). 절을 교체할 때는 "
        '{"base_revision": N, "sections": [{"id", "before_sha256", "text"}]}이며 truncated면 '
        "next_after로 끝까지 읽은 뒤 보내고, 절을 절반 이하로 줄이는 의도된 교체는 "
        '"allow_shrink": ["<절 id>"]를 더합니다.'
    ),
    "delete": '{"base_revision": <현재 revision>}',
    "restore": (
        '{"base_revision": <현재 revision>, "restore_revision": <복원할 revision>} '
        "— activate 없이 복원하면 효력 없는 stale 버전이 되어 기본 보기에 절이 나오지 않습니다. "
        '현재 문서로 되살릴 때만 "activate": true를 넣습니다.'
    ),
}


def _connected_before(args: Any) -> bool:
    """Whether this folder has a local connection record, to tell reconnect from first use."""
    if getattr(args, "profile", None):
        return True
    try:
        from durumari_mcp.context_removal import local_profile
        from durumari_mcp.legacy import manifest_path
        from durumari_mcp.onboarding import read_manifest
        from durumari_mcp.workspace_binding import binding_root

        root = binding_root(Path(getattr(args, "workspace", None) or os.getcwd()).resolve())
        if local_profile(root):
            return True
        # A legacy manifest kept the per-user profile key in the shared file.
        return manifest_path(root).exists() and bool(read_manifest(root).get("profile"))
    except Exception:
        return True  # Unknown: keep the reconnect wording, which never loses records.


def main() -> None:
    # CLI diagnostics use UTF-8 even when redirected under a legacy Windows
    # locale. Leave protocol stdout to its transport; never echo unsafe input.
    reconfigure = getattr(sys.stderr, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8", errors="backslashreplace")
    # No dependency debug logging or input/response exception tracebacks.
    logging.disable(logging.CRITICAL)
    args: Any = None
    try:
        hook = sys.argv[1:2] == ["project-hook"]
        args = parse_arguments(hook_parser() if hook else build_parser(), sys.argv[1:])
        execute(args)
    except AdapterError as error:
        print("두루마리: " + str(error), file=sys.stderr)
        if str(error) in {
            "account_credential_missing",
            "account_reconnect_required",
            "account_profile_changed_restart_required",
        }:
            command = [
                "durumari-mcp",
                "account-connect",
                "--client",
                getattr(args, "client", "codex"),
            ]
            if getattr(args, "profile", None):
                command += ["--profile", args.profile]
                try:
                    from durumari_mcp.account_credentials import load_account_profile

                    command += ["--server", load_account_profile(args.profile).gateway_origin]
                except AdapterError:
                    pass
            print(
                "브라우저에서 현재 계정과 권한을 확인하고 다시 연결해 주세요. "
                "기존 기록과 미확인 저장은 보존됩니다:",
                file=sys.stderr,
            )
            print(shlex.join(command), file=sys.stderr)
        elif str(error) in {
            "project_login_required",
            "interactive_project_login_required",
            "project_profile_exists_use_new_name",
            "profile_has_existing_outbox_use_new_name",
            "project_credential_missing",
            "project_repository_mismatch_reconnect",
        }:
            command = ["durumari-mcp", "context-connect"]
            workspace = getattr(args, "workspace", None)
            if not workspace and getattr(args, "profile", None):
                try:
                    from durumari_mcp.project_credentials import load_project_profile

                    workspace = load_project_profile(args.profile).workspace
                except AdapterError:
                    pass
            if workspace:
                command += ["--workspace", workspace]
            if str(error) == "project_credential_missing" and not _connected_before(args):
                print(
                    "이 폴더는 이 기기에서 아직 두루마리에 연결되지 않았습니다. "
                    "브라우저 승인으로 연결해 주세요:",
                    file=sys.stderr,
                )
            else:
                print(
                    "브라우저 승인으로 이 폴더를 다시 연결해 주세요. 기존 작업 기록은 보존됩니다:",
                    file=sys.stderr,
                )
            print(shlex.join(command), file=sys.stderr)
        elif str(error) == "workspace_not_found":
            print(
                "선택한 작업 폴더가 없습니다. 경로를 확인하거나 폴더를 만든 뒤 "
                "durumari-mcp context-connect --workspace <폴더>를 다시 실행해 주세요. "
                "아무것도 바꾸지 않았습니다.",
                file=sys.stderr,
            )
        elif str(error) in CONTEXT_ARGUMENT_GUIDANCE:
            print(CONTEXT_ARGUMENT_GUIDANCE[str(error)], file=sys.stderr)
            fields = CONTEXT_INPUT_EXAMPLES.get(getattr(args, "action", ""))
            if fields and str(error) in {"context_input_required", "context_input_fields_required"}:
                print("--input 예: " + fields, file=sys.stderr)
        elif str(error) == "workspace_directory_required":
            print(
                "존재하는 작업 폴더에서 "
                "durumari-mcp context-connect --workspace <루트>를 실행해 주세요. "
                "Git 연결은 저장소 최상위 폴더를 사용합니다.",
                file=sys.stderr,
            )
        elif str(error) == "repository_root_and_remote_required":
            print(
                "Git 저장소의 origin 조회 또는 기존 Git 연결 확인에 실패했습니다. "
                "Git 설치·저장소 접근 권한·origin 설정을 확인한 뒤 "
                "같은 폴더에서 다시 시도해 주세요. "
                "새 비Git·origin 없는 폴더는 UUID 입력 없이 "
                "durumari-mcp context-connect --workspace <폴더>로 승인 요청할 수 있습니다. "
                "기존 Git 연결은 폴더 소스로 자동 전환하지 않습니다.",
                file=sys.stderr,
            )
        elif str(error) == "project_removal_pending_retry":
            print(
                "이전 연결 해제의 서버 회수가 아직 확인되지 않았습니다. 네트워크를 확인한 뒤 "
                "durumari-mcp context-remove를 다시 실행해 주세요. 기존 기록은 보존됩니다.",
                file=sys.stderr,
            )
        elif str(error) == "existing_mcp_binding_conflict":
            print(
                "이 폴더의 Agent 설정에 두루마리가 만들지 않은 durumari_project 항목이 있습니다. "
                "해당 항목을 확인한 뒤 다시 연결해 주세요. 다른 설정은 바꾸지 않았습니다.",
                file=sys.stderr,
            )
        elif str(error) == "agent_upgrade_required_use_context_connect":
            print(
                "이 명령은 현재 연결 방식에서 쓰지 않습니다. 연결은 durumari-mcp context-connect, "
                "상태와 폴더·Git 변경 전 점검은 durumari-mcp context-status [--change "
                "git-init|move|detach]를 사용해 주세요. 기존 기록은 유지됩니다.",
                file=sys.stderr,
            )
        elif str(error) == "client_update_required":
            print(
                "이 폴더의 두루마리 설정은 더 새 버전 플러그인이 만든 형식입니다. "
                "durumari-mcp update로 업데이트한 뒤 다시 실행해 주세요. 설정은 바꾸지 않았습니다.",
                file=sys.stderr,
            )
        elif str(error) == "server_requires_client_update":
            print(
                "두루마리 서버가 이 플러그인 버전의 사용을 막았습니다. durumari-mcp update로 "
                "업데이트한 뒤 다시 실행해 주세요. 연결, 자격 증명과 저장 대기 중인 기록은 "
                "그대로 유지됩니다.",
                file=sys.stderr,
            )
        elif str(error) == "project_connection_cancelled":
            print(
                "연결을 취소했습니다. 기존 기록은 유지됩니다. "
                "다시 연결하려면 같은 명령을 다시 실행해 주세요.",
                file=sys.stderr,
            )
        elif str(error) in {"browser_approval_pending", "enrollment_expired"}:
            print(
                "브라우저 승인이 완료되지 않았거나 승인 요청이 만료되었습니다. "
                "같은 연결 명령을 다시 실행하면 새 승인 링크가 열립니다. "
                "프로젝트가 없으면 승인 화면에서 새로 만들 수 있습니다.",
                file=sys.stderr,
            )
        elif str(error) == "context_outbox_recovery_required":
            print(
                "손상된 저장 큐의 원본이 격리되어 기존 미확인 저장 수를 알 수 없습니다. "
                "같은 요청 키로 서버 영수증을 확인하고 격리된 기록을 복구해 주세요. "
                "새 저장은 계속할 수 있으며 기존 기록은 삭제하지 않습니다.",
                file=sys.stderr,
            )
        elif str(error) in {"project_delivery_unknown", "project_unavailable"}:
            print(
                "서버 연결을 확인해 주세요. 기존 로그인 정보와 기록은 보존됩니다.", file=sys.stderr
            )
        elif str(error) in {"project_access_denied", "project_connection_unavailable"}:
            print(
                "서버가 이 연결의 요청을 거부했습니다. 연결이 회수·만료되었거나, 프로젝트 접근 "
                "권한이 없거나, 더 이상 제공하지 않는 이전 방식의 요청일 수 있습니다. 최신 "
                "플러그인으로 durumari-mcp context-connect를 실행해 다시 승인해 주세요. "
                "기존 기록은 보존됩니다.",
                file=sys.stderr,
            )
        elif str(error) in {
            "protected_project_store_write_failed",
            "protected_project_store_read_failed",
        }:
            print(
                "OS 보안 저장소의 접근 승인을 확인한 뒤 다시 연결해 주세요. "
                "기존 기록은 보존됩니다.",
                file=sys.stderr,
            )
        elif str(error) == "workspace_text_invalid_utf8":
            print(
                "프로젝트 설정 파일을 UTF-8로 저장한 뒤 다시 연결하세요. 기존 파일은 보존됩니다.",
                file=sys.stderr,
            )
        raise SystemExit(2) from None
    except (KeyboardInterrupt, BrokenPipeError):
        raise SystemExit(0) from None
    except UnicodeDecodeError:
        # Only reading workspace text decodes; an output encoding failure is reported
        # as operation_failed below instead of blaming the project files.
        print("두루마리: workspace_text_invalid_utf8", file=sys.stderr)
        print(
            "프로젝트 설정 파일을 UTF-8로 저장한 뒤 다시 연결하세요. 기존 파일은 보존됩니다.",
            file=sys.stderr,
        )
        raise SystemExit(2) from None
    except Exception as error:
        from durumari_mcp.hook_recovery import failure_label

        # The class and adapter frame identify the cause without echoing its message.
        print("두루마리: operation_failed (" + failure_label(error) + ")", file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
