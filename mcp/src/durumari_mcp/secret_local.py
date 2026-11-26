"""CLI for local secret use: `secret run`, `secret env` and `secret cache`.

Local commands only; no MCP tool runs them, and Chat connections cannot use values.
"""

import json
import os
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from durumari_mcp.secret_use import SecretUseError

HINTS = {
    "secret_use_denied": "보안 화면에서 이 비밀값의 'Agent 사용' 권한과 현재 버전을 확인해 주세요.",
    "secret_not_found": "이 연결에서 볼 수 있는 이름인지 secret list로 확인해 주세요.",
    "secret_name_ambiguous": "같은 이름의 공유 비밀값이 여럿입니다. 내 비밀값으로 등록해 주세요.",
    "secret_offline_unavailable": (
        "서버에 연결할 수 없고 이 대상의 유효한 캐시가 없습니다. "
        "연결된 상태에서 한 번 실행해 주세요."
    ),
    "target_approval_required": "이 대상에서 처음 쓰는 비밀값입니다. 보안 화면에서 승인해 주세요.",
    "env_not_gitignored": ".gitignore에 출력 파일을 추가한 뒤 다시 실행해 주세요.",
    "env_tracked_by_git": "git이 추적 중인 파일에는 비밀값을 쓰지 않습니다. 추적을 해제해 주세요.",
    "env_exists_confirm_required": "--force로 덮어쓰거나 터미널에서 실행해 확인해 주세요.",
    "invalid_template_line": "durumari://NAME 참조는 값 전체여야 합니다(KEY=durumari://NAME).",
    "tool_unavailable": "이 연결에서는 비밀값을 쓸 수 없습니다. Agent 기기 연결에서 실행해 주세요.",
    "secret_refs_required": "--env VAR[=NAME]를 주거나 .env.durumari 템플릿을 만들어 주세요.",
    "secret_env_tier_forbidden": (
        "복구용·실행용 또는 정책상 금지된 비밀값은 .env 평문 파일로 내보낼 수 없습니다. "
        "secret run을 사용해 주세요."
    ),
    "secret_tier_forbidden": "이 등급의 비밀값은 Agent에서 사용할 수 없습니다.",
    "secret_environment_denied": "이 연결의 환경 범위에서 사용할 수 없는 비밀값입니다.",
}


def add_commands(actions: Any) -> None:
    run = actions.add_parser(
        "run", help="Run a command with secrets injected as environment variables only"
    )
    run.add_argument(
        "--env",
        action="append",
        metavar="VAR[=NAME]",
        help="Inject secret NAME (default: VAR) as VAR; repeatable",
    )
    run.add_argument("--template", help="Read durumari:// references from this .env template")
    run.add_argument("--offline", action="store_true", help="Use only the local cache")
    run.add_argument(
        "--verbose", action="store_true", help="Print the injected names (never values)"
    )
    run.add_argument("child_command", nargs="*", help="Command after --")
    env = actions.add_parser(
        "env", help="Write .env from a .env.durumari template (gitignored, owner-only)"
    )
    env.add_argument("--template", help="Template path (default: .env.durumari)")
    env.add_argument("--output", help="Output path (default: .env)")
    env.add_argument("--force", action="store_true", help="Overwrite an existing file")
    env.add_argument("--offline", action="store_true", help="Use only the local cache")
    env.add_argument("--json", action="store_true", help="Print a JSON receipt")
    cache = actions.add_parser("cache", help="Show or clear the local OS-store secret cache")
    cache.add_argument("operation", choices=["status", "clear"])
    cache.add_argument("--json", action="store_true", help="Print JSON")
    for action in (run, env, cache):
        action.add_argument("--workspace", default=os.getcwd())
        action.add_argument("--profile")


def cache_status(cache: Any) -> dict[str, Any]:
    now = time.time()
    keys = cache.device_keys(create=False)
    items = [
        {
            "id": secret_id,
            "label": meta.get("label"),
            "revision": meta.get("revision"),
            "policy_revision": meta.get("policy_revision"),
            "targets": len(meta.get("targets", [])),
            "expires_in": max(0, int(meta.get("expires_at", 0) - now)),
        }
        for secret_id, meta in sorted(cache.index().items(), key=lambda kv: str(kv[1].get("label")))
    ]
    return {
        "device_id": keys.device_id if keys else None,
        "items": items,
    }


def execute(args: Any, gateway_for: Callable[[Any], Any]) -> None:
    from durumari_mcp.secret_cache import SecretCache
    from durumari_mcp.secret_env import execute_env, execute_run

    gateway = gateway_for(args)
    profile = gateway.profile
    cache = SecretCache(profile.connection_id, profile.project_id)
    workspace = Path(profile.workspace).resolve()
    try:
        if args.secret_action == "run":
            execute_run(args, gateway, cache, workspace)
        elif args.secret_action == "env":
            execute_env(args, gateway, cache, workspace)
        elif args.operation == "status":
            print(json.dumps(cache_status(cache), ensure_ascii=False))
        else:
            removed = cache.clear()
            result = {"status": "cleared", "removed": removed}
            print(json.dumps(result) if args.json else f"로컬 캐시 {removed}개를 지웠습니다.")
    except SecretUseError as error:
        if error.names:
            print("두루마리: 대상 " + ", ".join(error.names), file=sys.stderr)
        hint = HINTS.get(str(error))
        if hint:
            print(hint, file=sys.stderr)
        raise
    except Exception as error:
        hint = HINTS.get(str(error))
        if hint:
            print(hint, file=sys.stderr)
        raise
