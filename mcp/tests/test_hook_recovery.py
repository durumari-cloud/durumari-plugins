"""Local hooks advise once per chat and never acquire server records or raw sessions."""

import json
from types import SimpleNamespace

import pytest

from durumari_mcp import cli, hook_recovery, project_cli
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "state")
    clock = [1000.0]
    monkeypatch.setattr(hook_recovery.time, "time", lambda: clock[0])
    payload = {"hook_event_name": "SessionStart", "cwd": str(tmp_path), "session_id": "a"}
    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: payload)
    args = SimpleNamespace(command="project-hook", profile="test-profile", client="codex")
    failure = [None]
    calls = []

    class Gateway:
        def __init__(self, *args):
            calls.append("local")
            if failure[0]:
                raise AdapterError(failure[0])
            self.root = tmp_path
            self.profile = SimpleNamespace(
                workspace=str(tmp_path),
                project_id="project-a",
                source_id="source-a",
                gateway_origin="https://example.com",
                check_workspace=lambda _: None,
            )

        def request(self, *args, **kwargs):
            raise AssertionError("a hook must not request server records")

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    return SimpleNamespace(
        args=args, payload=payload, clock=clock, root=tmp_path, failure=failure, calls=calls
    )


@pytest.mark.parametrize(
    "reason", ["project_credential_missing", "project_repository_mismatch_reconnect"]
)
def test_missing_credentials_or_wrong_repository_only_emit_an_advisory(hook, capsys, reason):
    hook.failure[0] = reason
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    assert output["continue"] is True
    context = json.loads(output["hookSpecificOutput"]["additionalContext"])
    assert context["status"] == "connection_required"
    assert "context-connect" in context["recovery_command"]
    project_cli.project_hook(hook.args)
    assert not capsys.readouterr().out
    assert len(hook.calls) == 1


@pytest.mark.parametrize("reason", sorted(hook_recovery.NETWORK_ERRORS))
def test_network_notice_never_requests_reauthentication(hook, reason):
    output = project_cli._hook_notice(hook.args, hook.payload, reason, str(hook.root))
    context = json.loads(output["hookSpecificOutput"]["additionalContext"])
    assert context["status"] == "temporarily_unavailable"
    assert "recovery_command" not in context
    assert "로그인" not in output["systemMessage"]


def test_non_lifecycle_events_do_nothing_even_with_a_missing_store(hook, capsys):
    hook.failure[0] = "protected_project_store_read_failed"
    for event in [
        "PreToolUse",
        "PostToolUse",
        "UserPromptSubmit",
        "SessionEnd",
        "PreCompact",
        "SubagentStop",
    ]:
        hook.payload["hook_event_name"] = event
        project_cli.project_hook(hook.args)
    output = capsys.readouterr()
    assert not hook.calls and not output.out and not output.err


def test_start_hint_is_once_per_chat_and_contains_no_document_snapshot(hook, capsys):
    project_cli.project_hook(hook.args)
    output = capsys.readouterr().out
    assert "bound project=project-a" in output and len(output.encode()) <= 1200
    assert "context_documents" not in output
    project_cli.project_hook(hook.args)
    assert not capsys.readouterr().out
    hook.payload["session_id"] = "b"
    project_cli.project_hook(hook.args)
    assert "bound project=project-a" in capsys.readouterr().out




def test_direct_authenticated_commands_remain_closed(hook, monkeypatch, capsys):
    hook.args.command = "project-status"
    parser = SimpleNamespace(parse_args=lambda _argv: hook.args)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr(
        cli, "execute", lambda *a: (_ for _ in ()).throw(AdapterError("project_login_required"))
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    capsys.readouterr()


def test_corrupt_or_future_cache_does_not_freeze_local_recovery(hook):
    recovery = hook_recovery.HookRecovery("test-profile", "codex", str(hook.root), "a")
    recovery.defer("project_login_required", "a")
    recovery.path.write_text('{"reason":["untrusted"]}')
    assert recovery.status() is None
    assert not recovery.cooling_down({"failed_at": hook.clock[0] + 99999})


def test_unsafe_cache_never_follows_a_symlink(hook, capsys):
    recovery = hook_recovery.HookRecovery("test-profile", "codex", str(hook.root), "a")
    outside = hook.root / "outside"
    outside.mkdir()
    recovery.directory.parent.mkdir(parents=True, exist_ok=True)
    recovery.directory.symlink_to(outside, target_is_directory=True)
    hook.failure[0] = "project_credential_missing"
    project_cli.project_hook(hook.args)
    assert json.loads(capsys.readouterr().out)["continue"] is True
    assert list(outside.iterdir()) == []


def test_unexpected_hook_failure_keeps_only_a_content_free_label(hook, monkeypatch):
    def broken(*args):
        raise KeyError("private-context-text")

    monkeypatch.setattr(project_cli, "ProjectGateway", broken)
    hook.payload["hook_event_name"] = "Stop"
    with pytest.raises(KeyError):
        project_cli.project_hook(hook.args)
    stored = hook.root / "state" / "test-profile" / hook_recovery.FAILURE_FILE
    text = stored.read_text()
    value = json.loads(text)
    assert value["event"] == "Stop"
    assert value["error"].startswith("KeyError at project_cli.py:")
    assert "private-context-text" not in text


def test_untrusted_policy_surrogates_reach_the_host_as_strict_utf8(hook, monkeypatch, capsys):
    index = {"items": [{"when": "배포 전 \ud800", "title": "정책 \udfff 🧶", "id": "p1"}]}
    monkeypatch.setattr(project_cli, "_quick_policy_index", lambda gateway: index)
    project_cli.project_hook(hook.args)
    raw = capsys.readouterr().out.encode("ascii").decode("utf-8", errors="strict")
    frame = json.loads(raw)
    context = frame["hookSpecificOutput"]["additionalContext"]
    context.encode("utf-8", errors="strict")
    assert "- 배포 전 � -> 정책 � 🧶 [p1@]" in context
    assert "systemMessage" not in frame


def test_surrogates_outside_policy_text_still_deliver_the_whole_frame(hook, monkeypatch, capsys):
    base = project_cli.ProjectGateway

    class Gateway(base):
        def __init__(self, *args):
            super().__init__(*args)
            # A surrogateescape-decoded local value can reach any field of the frame.
            self.profile.project_id = "project-\udc80"

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    monkeypatch.setattr(project_cli, "_quick_policy_index", lambda gateway: None)
    project_cli.project_hook(hook.args)
    raw = capsys.readouterr().out.encode("ascii").decode("utf-8", errors="strict")
    context = json.loads(raw)["hookSpecificOutput"]["additionalContext"]
    assert "bound project=project-\ufffd." in context
    recovery = hook_recovery.HookRecovery("test-profile", "codex", str(hook.root), "a")
    assert recovery.status() is None
    assert not (hook.root / "state" / "test-profile" / hook_recovery.FAILURE_FILE).exists()


def test_decode_failure_outside_output_keeps_the_advisory_path(hook, monkeypatch, capsys):
    def undecodable(*args):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "private-context-text")

    monkeypatch.setattr(project_cli, "ProjectGateway", undecodable)
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    assert output["continue"] is True
    context = json.loads(output["hookSpecificOutput"]["additionalContext"])
    # A local failure is not a server outage.
    assert context["reason"] == hook_recovery.LOCAL_CHECK_FAILED
    assert context["status"] == "connection_required"
    assert "private-context-text" not in json.dumps(output)


def test_cli_reports_exception_class_without_its_message(hook, monkeypatch, capsys):
    parser = SimpleNamespace(parse_args=lambda _argv: hook.args)
    monkeypatch.setattr(cli, "build_parser", lambda: parser)
    monkeypatch.setattr(
        cli, "execute", lambda *a: (_ for _ in ()).throw(RuntimeError("private-context-text"))
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2
    message = capsys.readouterr().err
    assert message.startswith("두루마리: operation_failed (RuntimeError at cli.py:")
    assert "private-context-text" not in message


def test_label_failure_never_leaks_the_original_message(monkeypatch):
    class Odd(Exception):
        __module__ = None

    label = hook_recovery.failure_label(Odd("private-context-text"))
    assert "private-context-text" not in label
    monkeypatch.setattr(hook_recovery, "_failure_label", lambda error: 1 / 0)
    assert hook_recovery.failure_label(KeyError("private-context-text")) == "unlabelled"


def policy_gateway(monkeypatch, tmp_path, *, expires_in=3600, delay=0.0, calls=None):
    import time
    from datetime import UTC, datetime, timedelta

    calls = [] if calls is None else calls

    class Gateway:
        def __init__(self, *args):
            self.root, self.name = tmp_path, "test-profile"
            self.profile = SimpleNamespace(
                project_id="project-a",
                refresh_uncertain=False,
                access_expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
                check_workspace=lambda _: None,
            )

        def context_request(self, path):
            calls.append(path)
            time.sleep(delay)
            return {
                "items": [
                    {
                        "id": "p1",
                        "revision": 6,
                        "title": "Jira 이슈 편성",
                        "when": "Jira 이슈를 만들거나 편성할 때",
                        "status": "active",
                    }
                ],
                "total": 1,
                "truncated": False,
            }

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    return calls


def start_hint(hook, capsys):
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    return output["hookSpecificOutput"]["additionalContext"]


def test_start_hint_includes_the_short_policy_index(hook, monkeypatch, capsys):
    calls = policy_gateway(monkeypatch, hook.root)
    hint = start_hint(hook, capsys)
    assert sorted(calls) == [
        "/api/v2/agent/connection",
        "/api/v2/projects/project-a/contexts/work-logs/session",
    ]
    assert "- Jira 이슈를 만들거나 편성할 때 -> Jira 이슈 편성 [p1@6]" in hint
    assert "durumari_context_read" in hint


def test_policy_lines_name_the_cursor_and_flag_cut_when_lines():
    row = {"id": "p1", "revision": 1, "title": "병합", "when": "병합 전에…"}
    paged = project_cli._policy_lines({"items": [row], "truncated": True, "next_after": 12})
    assert "call durumari_policy_index(after=12)" in paged
    older_server = project_cli._policy_lines({"items": [row], "truncated": True})
    assert older_server.endswith("call durumari_policy_index.")
    cut = project_cli._policy_lines({"items": [row], "truncated": False})
    assert cut.endswith("- Cut when-line: read it if it may apply.")


def test_expiring_credentials_skip_the_index_instead_of_refreshing(hook, monkeypatch, capsys):
    calls = policy_gateway(monkeypatch, hook.root, expires_in=10)
    hint = start_hint(hook, capsys)
    assert calls == []
    assert "call durumari_policy_index" in hint


def test_slow_index_never_delays_the_hook_past_its_deadline(hook, monkeypatch, capsys):
    import time

    monkeypatch.setattr(project_cli, "POLICY_INDEX_SECONDS", 0.2)
    policy_gateway(monkeypatch, hook.root, delay=2.0)
    started = time.monotonic()
    hint = start_hint(hook, capsys)
    assert time.monotonic() - started < 1.0
    assert "call durumari_policy_index" in hint


def test_compaction_reinjects_the_rules_within_the_same_chat(hook, monkeypatch, capsys):
    policy_gateway(monkeypatch, hook.root)
    assert "Jira 이슈 편성" in start_hint(hook, capsys)
    project_cli.project_hook(hook.args)
    assert not capsys.readouterr().out  # A repeated start in one chat stays quiet.
    hook.payload["source"] = "compact"
    hint = start_hint(hook, capsys)
    assert hint.startswith("Re-injected after compaction.")
    assert "Jira 이슈 편성" in hint


def test_connection_languages_are_injected_but_never_written_to_files(
    hook, monkeypatch, capsys
):
    from durumari_mcp import instruction_files

    policy_gateway(monkeypatch, hook.root)
    base = project_cli.ProjectGateway
    original = base.context_request

    class Gateway(base):
        def __init__(self, *args):
            super().__init__(*args)
            self.profile.workspace = str(hook.root)

        def context_request(self, path):
            if path == "/api/v2/agent/connection":
                return {"language": {"response": "en", "shared_documents": "ko"}}
            return original(self, path)

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    block = instruction_files.agents_block("project-a") + "\n"
    (hook.root / "AGENTS.md").write_text(block)
    hint = start_hint(hook, capsys)
    assert "Response language: en;" in hint
    assert "Write shared Context documents in ko." in hint
    # A changing value never churns the committed file.
    assert (hook.root / "AGENTS.md").read_text() == block


def test_missing_language_field_falls_back_to_the_users_messages(hook, monkeypatch, capsys):
    policy_gateway(monkeypatch, hook.root)
    assert "Response language: not provided by the server" in start_hint(hook, capsys)


def test_unreachable_server_shows_cached_rules_and_says_so(hook, monkeypatch, capsys):
    policy_gateway(monkeypatch, hook.root)
    start_hint(hook, capsys)  # A good start keeps the index.
    monkeypatch.setattr(project_cli, "POLICY_INDEX_SECONDS", 0.1)
    policy_gateway(monkeypatch, hook.root, delay=1.0)
    hook.payload["session_id"] = "b"
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    assert output["systemMessage"].startswith("두루마리 연결 끊김, 캐시 사용 (")
    hint = output["hookSpecificOutput"]["additionalContext"]
    assert "Durumari is disconnected: tell the user before starting work" in hint
    assert "Cached project Policy index from" in hint
    assert "- Jira 이슈를 만들거나 편성할 때 -> Jira 이슈 편성 [p1@6]" in hint


def test_failed_connection_notice_carries_the_cached_index(hook, monkeypatch, capsys):
    # Offline cache selection still requires this folder's project and user profile.
    (hook.root / "durumari.yaml").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "server": "https://example.com",
                "project_id": "project-a",
                "source_id": "source-a",
                "context_source": "server",
            }
        ),
        encoding="utf-8",
    )
    binding = hook.root / ".durumari/context-binding.json"
    binding.parent.mkdir()
    binding.write_text(json.dumps({"profile": hook.args.profile}), encoding="utf-8")
    policy_gateway(monkeypatch, hook.root)
    start_hint(hook, capsys)
    monkeypatch.setattr(
        project_cli,
        "ProjectGateway",
        lambda *a: (_ for _ in ()).throw(AdapterError("project_credential_missing")),
    )
    hook.payload["session_id"] = "b"
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    assert output["systemMessage"].startswith("두루마리 연결 끊김, 캐시 사용 (")
    context = json.loads(output["hookSpecificOutput"]["additionalContext"])
    assert context["cached_policy_index"] == [
        "- Jira 이슈를 만들거나 편성할 때 -> Jira 이슈 편성 [p1@6]"
    ]
    assert "tell the user before starting work" in context["notice"]


def test_failed_connection_is_shown_again_after_compaction(hook, capsys):
    hook.failure[0] = "project_credential_missing"
    project_cli.project_hook(hook.args)
    assert capsys.readouterr().out
    hook.payload["source"] = "compact"
    project_cli.project_hook(hook.args)
    assert "연결 끊김" in json.loads(capsys.readouterr().out)["systemMessage"]


def test_session_start_converts_copied_instruction_blocks(hook, monkeypatch, capsys):
    from durumari_mcp import instruction_files

    policy_gateway(monkeypatch, hook.root)
    base = project_cli.ProjectGateway

    class Gateway(base):
        def __init__(self, *args):
            super().__init__(*args)
            self.profile.workspace = str(hook.root)

    monkeypatch.setattr(project_cli, "ProjectGateway", Gateway)
    old = instruction_files.START + "\nProject: project-a\n- copied\n" + instruction_files.END
    (hook.root / "CLAUDE.md").write_text(old + "\n", encoding="utf-8")
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    assert "AGENTS.md, CLAUDE.md" in output["systemMessage"]
    assert (hook.root / "CLAUDE.md").read_text() == instruction_files.import_block() + "\n"
    assert "Project: project-a" in (hook.root / "AGENTS.md").read_text()


@pytest.mark.parametrize(
    "reason", ["project_removed", "project_client_mismatch", "invalid_project_credential"]
)
def test_agent_settings_naming_a_replaced_connection_ask_to_reconnect_this_agent(
    hook, capsys, reason
):
    # another Agent reconnected the folder and the old profile was
    # disabled, but this Agent's settings still named it. That is not a server outage.
    hook.failure[0] = reason
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    context = json.loads(output["hookSpecificOutput"]["additionalContext"])
    assert context["status"] == "connection_required"
    assert context["reason"] == reason
    assert "context-connect --agents codex" in context["recovery_command"]
    assert "지연" not in output["systemMessage"]
    assert context["pending_records"] == "preserved"
    project_cli.project_hook(hook.args)
    assert not capsys.readouterr().out  # Same chat: the notice is not repeated.


def test_unclassified_local_failure_is_not_reported_as_a_server_outage(hook, capsys):
    hook.failure[0] = "invalid_project_manifest"
    project_cli.project_hook(hook.args)
    output = json.loads(capsys.readouterr().out)
    context = json.loads(output["hookSpecificOutput"]["additionalContext"])
    assert context["status"] == "connection_required"
    assert context["reason"] == "project_local_check_failed"
    assert "context-connect" in context["recovery_command"]


def test_recovered_start_clears_the_previous_failure(hook, capsys):
    hook.failure[0] = "project_unavailable"
    project_cli.project_hook(hook.args)
    assert json.loads(capsys.readouterr().out)["systemMessage"]
    recovery = hook_recovery.HookRecovery("test-profile", "codex", str(hook.root), "a")
    assert recovery.status()["reason"] == "project_unavailable"
    hook.clock[0] += hook_recovery.RETRY_SECONDS + 1
    hook.failure[0] = None
    hook.payload["source"] = "compact"
    project_cli.project_hook(hook.args)
    assert "bound project=project-a" in capsys.readouterr().out
    assert recovery.status() is None
    # A new outage after recovery is announced again instead of hiding behind the old one.
    hook.clock[0] += 1
    hook.failure[0] = "project_unavailable"
    hook.payload.pop("source")
    project_cli.project_hook(hook.args)
    assert "temporarily_unavailable" in capsys.readouterr().out
