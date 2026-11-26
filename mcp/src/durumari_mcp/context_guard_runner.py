"""Standard-library watchdog for the opt-in Context hook candidate.

Keep the host timeout above this worker budget. This process catches missing
worker dependencies, nonzero exits, malformed output and timeouts. It cannot
protect a host that skips an untrusted hook or fails to launch this process.
"""

import argparse
import json
import subprocess
import sys
from typing import Any

from durumari_mcp.context_guard_diagnostics import safe_reason, timeout_reason, worker_reason

MAX_BYTES = 131072
WORKER_SECONDS = 30


def _blocked(event: str, reason: str = "guard_unavailable") -> dict[str, Any]:
    reason = safe_reason(reason)
    message = "두루마리 최신 Context 검사 실패로 의존 작업을 보류합니다. "
    if reason == "guard_profile_timeout":
        message = "로컬 연결 초기화가 시간 안에 끝나지 않아 의존 작업을 보류합니다. "
    elif reason == "protected_project_store_read_failed":
        message = "OS 보안 저장소의 프로젝트 연결 정보를 읽지 못해 의존 작업을 보류합니다. "
    message += "(" + reason + ")"
    if event in {"SessionStart", "PostCompact"}:
        return {"continue": False, "stopReason": message, "systemMessage": message}
    if event == "PreToolUse":
        return {"hookSpecificOutput": {"hookEventName": event,
                                       "permissionDecision": "deny",
                                       "permissionDecisionReason": message}}
    if event == "Stop":
        return {"systemMessage": message + " 이미 표시된 응답은 취소할 수 없습니다."}
    return {"decision": "block", "reason": message}


def run_guard(payload: dict[str, Any], profile: str, client: str) -> dict[str, Any]:
    event = payload.get("hook_event_name", "")
    try:
        if client != "codex" or event not in {
            "SessionStart", "UserPromptSubmit", "PreToolUse", "Stop", "PostCompact"
        }:
            return _blocked(event, "unsupported_guard_host" if client != "codex"
                            else "unsupported_guard_event")
        child = subprocess.run(
            [sys.executable, "-m", "durumari_mcp.context_guard_hook",
             "--profile", profile, "--client", client, "--stage-events"],
            input=json.dumps(payload, ensure_ascii=True), capture_output=True,
            text=True, timeout=WORKER_SECONDS, check=False,
        )
        if child.returncode:
            return _blocked(event, "guard_worker_failed")
        if len(child.stdout.encode("utf-8")) > MAX_BYTES:
            return _blocked(event, "guard_output_invalid")
        try:
            value = json.loads(child.stdout)
        except ValueError:
            return _blocked(event, "guard_output_invalid")
        if not isinstance(value, dict):
            return _blocked(event, "guard_output_invalid")
        if (event in {"SessionStart", "PreToolUse", "Stop"} and value == {}
                and not (event == "SessionStart" and payload.get("source") == "compact")):
            return {}
        specific = value.get("hookSpecificOutput")
        if event in {"SessionStart", "PostCompact"} and value.get("continue") is False:
            return _blocked(event, worker_reason(value.get("stopReason")))
        if value.get("decision") == "block":
            return _blocked(event, worker_reason(value.get("reason")))
        if (event == "PreToolUse" and isinstance(specific, dict)
                and specific.get("hookEventName") == event
                and specific.get("permissionDecision") == "deny"):
            return _blocked(event, worker_reason(specific.get("permissionDecisionReason")))
        if event == "Stop" and isinstance(value.get("systemMessage"), str):
            return _blocked(event, worker_reason(value["systemMessage"]))
        if (event == "UserPromptSubmit" and isinstance(specific, dict)
                and specific.get("hookEventName") == event
                and isinstance(specific.get("additionalContext"), str)
                and specific["additionalContext"]):
            return {"hookSpecificOutput": {"hookEventName": event,
                                           "additionalContext": specific["additionalContext"]}}
        return _blocked(event, "guard_output_invalid")
    except subprocess.TimeoutExpired as error:
        return _blocked(event, timeout_reason(error.stderr))
    except OSError:
        return _blocked(event, "guard_worker_unavailable")
    except Exception:
        return _blocked(event)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--client", choices=["codex"], required=True)
    args = parser.parse_args()
    try:
        raw = sys.stdin.buffer.read(1_048_577)
        if len(raw) > 1_048_576:
            raise ValueError
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError
        output = run_guard(payload, args.profile, args.client)
    except Exception:
        output = _blocked("", "guard_input_invalid")
    specific = output.get("hookSpecificOutput", {})
    if output.get("decision") == "block" or specific.get("permissionDecision") == "deny":
        # Native exit 2 is explicit blocking, including hosts that do not surface
        # a JSON block reason in their non-interactive event stream.
        sys.stderr.write(output.get("reason") or specific["permissionDecisionReason"])
        sys.stderr.write("\n")
        return 2
    sys.stdout.write(json.dumps(output, ensure_ascii=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
