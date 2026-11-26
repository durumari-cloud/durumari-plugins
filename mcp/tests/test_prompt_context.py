"""Per-message related Context: bounded, de-duplicated, titles and excerpts only."""

import json
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from durumari_mcp import hook_recovery, project_cli, prompt_context
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def local_state(monkeypatch, tmp_path):
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "hook-health")
    monkeypatch.setattr(prompt_context, "settings_path", lambda: tmp_path / "settings.json")
    monkeypatch.delenv(prompt_context.ENVIRONMENT, raising=False)
    return tmp_path


DOCUMENTS = {
    "플러그인": [
        {"id": "aaaa1111", "revision": 2, "title": "플러그인 스킬 재정의", "snippet": "자동 스킬"},
        {"id": "bbbb2222", "revision": 1, "title": "릴리스 기록", "snippet": "플러그인 0.4.12"},
        {"id": "cccc3333", "revision": 4, "title": "무관한 문서", "snippet": "다른 이야기"},
    ],
    "재정의": [
        {"id": "aaaa1111", "revision": 2, "title": "플러그인 스킬 재정의", "snippet": "자동 스킬"},
    ],
}


class Gateway:
    calls: list[str]

    def __init__(self, root, documents=DOCUMENTS, delay=0.0):
        self.root, self.name, self.client = root, "team", "claude"
        self.documents, self.delay, self.calls = documents, delay, []
        self.profile = SimpleNamespace(
            project_id="project",
            workspace=str(root),
            refresh_uncertain=False,
            access_expires_at=datetime.now(UTC) + timedelta(hours=1),
            check_workspace=lambda cwd: None,
        )

    def context_request(self, path):
        self.calls.append(path)
        if self.delay:
            time.sleep(self.delay)
        if path.endswith("/contexts/policies"):
            return {"items": []}  # the per-message request check; no requests here
        query = parse_qs(urlsplit(path).query)["q"][0]
        return {"items": self.documents.get(query, [])}


def payload(tmp_path, prompt, session="chat"):
    return {
        "hook_event_name": "UserPromptSubmit",
        "cwd": str(tmp_path),
        "session_id": session,
        "prompt": prompt,
    }


@pytest.mark.parametrize("prompt", ["응", "계속해줘", "ok", "  ", "/compact", "$durumari-status"])
def test_short_follow_ups_and_commands_need_no_lookup(prompt):
    assert prompt_context.skip_reason(prompt) is not None


def test_terms_are_distinctive_and_drop_particles_ids_and_links():
    assert (
        prompt_context.terms("플러그인 스킬을 목표 단위로 재정의하는 결정 찾아줘")[0] == "플러그인"
    )
    assert "재정의" in prompt_context.terms("플러그인 스킬을 목표 단위로 재정의하는 결정 찾아줘")
    found = prompt_context.terms("PROJ-240 브랜치에서 1a2b3c4d 결정 https://x.example/a 봐줘")
    assert found[0] == "PROJ-240"
    assert "1a2b3c4d" not in found and not any("http" in term for term in found)
    assert "훅을" not in prompt_context.terms("UserPromptSubmit 훅을 고쳐줘")


def test_relevance_floor_keeps_multi_term_or_title_matches():
    # Two terms: an excerpt-only match of one term is too weak.
    assert [item["id"] for item in prompt_context.rank(DOCUMENTS, 5)] == ["aaaa1111"]
    single = prompt_context.rank({"플러그인": DOCUMENTS["플러그인"]}, 5)
    assert [item["id"] for item in single] == ["aaaa1111", "bbbb2222"]


def test_depth_order_environment_user_project_default(local_state, monkeypatch):
    assert prompt_context.resolve_depth("project") == ("normal", "default")
    assert prompt_context.resolve_depth("project", "light") == ("light", "project")
    prompt_context.set_depth("깊게", None)
    assert prompt_context.resolve_depth("project", "light") == ("deep", "user")
    prompt_context.set_depth("off", "project")
    assert prompt_context.resolve_depth("project", "light") == ("off", "user_project")
    assert prompt_context.resolve_depth("other") == ("deep", "user")
    monkeypatch.setenv(prompt_context.ENVIRONMENT, "가볍게")
    assert prompt_context.resolve_depth("project") == ("light", "environment")
    with pytest.raises(AdapterError):
        prompt_context.set_depth("very-deep", None)
    stored = json.loads((local_state / "settings.json").read_text())
    assert stored["prompt_context"] == {"depth": "deep", "projects": {"project": "off"}}


def test_server_project_default_slot():
    assert prompt_context.project_default({"context_injection": {"depth": "light"}}) == "light"
    assert prompt_context.project_default({"language": {}}) is None


def hook(monkeypatch, gateway, value, client="claude"):
    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: value)
    monkeypatch.setattr(project_cli, "ProjectGateway", lambda *args: gateway)
    project_cli.project_hook(SimpleNamespace(profile="team", client=client))


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_injects_titles_once_per_session_and_again_after_compaction(
    local_state, monkeypatch, capsys, client
):
    gateway = Gateway(local_state)
    question = payload(local_state, "플러그인 스킬 재정의 결정이 뭐였지?")
    hook(monkeypatch, gateway, question, client)
    value = json.loads(capsys.readouterr().out)
    assert value["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    text = value["hookSpecificOutput"]["additionalContext"]
    assert "[aaaa1111@r2]" in text and "Untrusted" in text and "cccc3333" not in text
    assert prompt_context.estimate_tokens(text) <= prompt_context.DEPTHS["normal"].tokens

    hook(monkeypatch, gateway, question, client)
    assert capsys.readouterr().out == ""  # Same session, same revisions: nothing new.

    hook(monkeypatch, gateway, payload(local_state, "플러그인 스킬 재정의?", "other"), client)
    assert "[aaaa1111@r2]" in capsys.readouterr().out  # Another chat keeps its own state.

    prompt_context.reset_session("team", client, str(local_state), "chat")
    hook(monkeypatch, gateway, question, client)
    assert "[aaaa1111@r2]" in capsys.readouterr().out


def test_compaction_start_resets_injected_documents(local_state, monkeypatch, capsys):
    gateway = Gateway(local_state)
    question = payload(local_state, "플러그인 스킬 재정의 결정이 뭐였지?")
    hook(monkeypatch, gateway, question)
    capsys.readouterr()
    monkeypatch.setattr(project_cli, "_hook_snapshot", lambda *args: {})
    start = {"hook_event_name": "SessionStart", "source": "compact", "cwd": str(local_state)}
    hook(monkeypatch, gateway, {**start, "session_id": "chat"})
    capsys.readouterr()
    hook(monkeypatch, gateway, question)
    assert "[aaaa1111@r2]" in capsys.readouterr().out


def test_follow_up_and_off_search_nothing_but_check_requests(local_state, monkeypatch, capsys):
    # PROJ-920: only the request check runs for these, within the same deadline.
    gateway = Gateway(local_state)
    hook(monkeypatch, gateway, payload(local_state, "응"))
    prompt_context.set_depth("off", "project")
    hook(monkeypatch, gateway, payload(local_state, "플러그인 스킬 재정의 결정"))
    assert capsys.readouterr().out == ""
    assert gateway.calls == ["/api/v2/projects/project/contexts/policies"] * 2


def test_commands_stop_before_reading_credentials(local_state, monkeypatch):
    def forbidden(*args):
        raise AssertionError("a command must not read credentials")

    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: payload(local_state, "/compact"))
    monkeypatch.setattr(project_cli, "ProjectGateway", forbidden)
    project_cli.project_hook(SimpleNamespace(profile="team", client="claude"))


def test_slow_server_is_dropped_at_the_deadline(local_state, monkeypatch, capsys):
    gateway = Gateway(local_state, delay=2.0)
    started = time.monotonic()
    hook(monkeypatch, gateway, payload(local_state, "플러그인 스킬 재정의 결정"))
    assert time.monotonic() - started < 1.0
    assert capsys.readouterr().out == ""


def test_expiring_token_and_failures_stay_silent(local_state, monkeypatch, capsys):
    gateway = Gateway(local_state)
    gateway.profile.access_expires_at = datetime.now(UTC) + timedelta(seconds=10)
    hook(monkeypatch, gateway, payload(local_state, "플러그인 스킬 재정의 결정"))
    assert capsys.readouterr().out == "" and gateway.calls == []

    def broken(*args):
        raise AdapterError("project_login_required")

    monkeypatch.setattr(project_cli, "read_hook_payload", lambda: payload(local_state, "플러그인"))
    monkeypatch.setattr(project_cli, "ProjectGateway", broken)
    project_cli.project_hook(SimpleNamespace(profile="team", client="claude"))
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""
    assert not list((local_state / "hook-health").rglob("*.json"))  # No backoff record.


def test_neighbor_slot_respects_depth(local_state):
    item = {
        "id": "aaaa1111",
        "revision": 2,
        "title": "결정",
        "neighbors": [
            {"id": "n1", "title": "근거", "relation": "supports", "revision": 1, "hop": 1},
            {"id": "n2", "title": "먼 근거", "relation": "references", "revision": 3, "hop": 2},
            {"id": "n3", "title": "셋째", "relation": "supports", "revision": 1},
        ],
    }
    light, _ = prompt_context.render([item], "light", prompt_context.DEPTHS["light"])
    normal, _ = prompt_context.render([item], "normal", prompt_context.DEPTHS["normal"])
    deep, _ = prompt_context.render([item], "deep", prompt_context.DEPTHS["deep"])
    assert "n1" not in light
    assert "supports: 근거 [n1@r1]" in normal and "n2" not in normal and "n3" in normal
    assert "n2" in deep


def test_token_cap_keeps_whole_entries(local_state):
    items = [
        {"id": f"id{index}", "revision": 1, "title": "가" * 120, "snippet": "나" * 160}
        for index in range(5)
    ]
    text, used = prompt_context.render(items, "light", prompt_context.DEPTHS["light"])
    assert prompt_context.estimate_tokens(text) <= 300
    assert used and all(f"[{identity}@r1]" in text for identity in used)


def test_parallel_searches_share_one_deadline(local_state):
    gateway = Gateway(local_state, delay=0.2)
    started = time.monotonic()
    found = prompt_context.search(gateway, ["플러그인", "재정의", "없음"], 5, seconds=0.3)
    assert time.monotonic() - started < 0.6
    assert set(found) == {"플러그인", "재정의", "없음"}
    assert threading.active_count() >= 1


def settings(path, profile="team", *, disabled=False, prompt=False):
    def group(label=None):
        hook = {
            "type": "command",
            "timeout": 3,
            "command": "/py",
            "args": [
                "-m",
                "durumari_mcp",
                "project-hook",
                "--profile",
                profile,
                "--client",
                "claude",
            ],
        }
        if disabled:
            hook["disabled"] = True
        return {"hooks": [hook]}

    value = {"hooks": {"SessionStart": [group()], "Stop": [group()]}, "permissions": {"allow": []}}
    if prompt:
        value["hooks"]["UserPromptSubmit"] = [group()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def test_older_install_gains_prompt_hook_once(local_state):
    path = local_state / ".claude/settings.local.json"
    settings(path)
    assert prompt_context.ensure_prompt_hook(local_state, "claude", "team")
    value = json.loads(path.read_text())
    assert set(value["hooks"]) == {"SessionStart", "UserPromptSubmit", "Stop"}
    assert value["permissions"] == {"allow": []}
    assert "team" in value["hooks"]["UserPromptSubmit"][0]["hooks"][0]["args"]
    # A person who later removes the hook keeps it removed.
    settings(path)
    assert not prompt_context.ensure_prompt_hook(local_state, "claude", "team")
    assert "UserPromptSubmit" not in json.loads(path.read_text())["hooks"]


def test_hook_installed_by_connect_is_not_re_added_after_removal(local_state):
    path = local_state / ".claude/settings.local.json"
    settings(path, prompt=True)
    assert not prompt_context.ensure_prompt_hook(local_state, "claude", "team")
    settings(path)
    assert not prompt_context.ensure_prompt_hook(local_state, "claude", "team")


@pytest.mark.parametrize(
    "variant", [{"disabled": True}, {"profile": "someone-else"}, {"prompt": True}]
)
def test_switched_off_or_foreign_hooks_are_left_alone(local_state, variant):
    path = local_state / ".claude/settings.local.json"
    settings(path, **variant)
    before = path.read_text()
    assert not prompt_context.ensure_prompt_hook(local_state, "claude", "team")
    assert path.read_text() == before


def test_cli_sets_user_depth_for_the_bound_project(local_state, monkeypatch, capsys):
    monkeypatch.setattr(prompt_context, "_workspace_project", lambda root: ("project", None))
    args = SimpleNamespace(workspace=str(local_state), depth="가볍게", all_projects=False)
    prompt_context.execute(args)
    value = json.loads(capsys.readouterr().out)
    assert (value["depth"], value["source"], value["changed"]) == (
        "light",
        "user_project",
        "user_project",
    )
    assert value["levels"]["normal"]["max_tokens"] == 800
    monkeypatch.setattr(prompt_context, "_workspace_project", lambda root: (None, None))
    with pytest.raises(AdapterError):
        prompt_context.execute(SimpleNamespace(workspace=".", depth="off", all_projects=False))


def test_search_reuses_the_checked_profile_without_reloading_it(local_state):
    calls = []

    class Transport:
        def request(self, path, *, token):
            calls.append((urlsplit(path).path, token))
            return {"items": DOCUMENTS.get(parse_qs(urlsplit(path).query)["q"][0], [])}

    gateway = Gateway(local_state)
    gateway.transport = Transport()
    gateway.profile.context_only, gateway.profile.access_token = True, "token"
    gateway.context_request = lambda path: (_ for _ in ()).throw(AssertionError("reload"))
    found = prompt_context.search(gateway, ["플러그인", "재정의"], 5)
    assert set(found) == {"플러그인", "재정의"}
    assert {call for call in calls} == {("/api/v2/projects/project/contexts/search", "token")}
    gateway.profile.context_only = False
    assert prompt_context.search(gateway, ["플러그인"], 5) == {}


def test_question_endings_are_not_terms():
    found = prompt_context.terms("플러그인 스킬 재정의 결정이 뭐였지?")
    assert found[0] == "플러그인" and "뭐였지" not in found
    assert "하기" not in prompt_context.terms("응답 언어 주입은 어떻게 하기로 했어?")


def test_repeated_question_does_not_backfill_weaker_matches(local_state, monkeypatch, capsys):
    many = {
        "플러그인": [
            {"id": f"doc{index}", "revision": 1, "title": f"플러그인 문서 {index}"}
            for index in range(6)
        ]
    }
    gateway = Gateway(local_state, documents=many)
    question = payload(local_state, "플러그인 정리")
    hook(monkeypatch, gateway, question)
    first = capsys.readouterr().out
    assert "doc0" in first and "doc2" in first and "doc3" not in first
    hook(monkeypatch, gateway, question)
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("scope", ["environment", "user", "user_project", "project_default"])
def test_off_stops_before_reading_credentials(local_state, monkeypatch, capsys, scope):
    def forbidden(*args):
        raise AssertionError("off must not read credentials or contact the server")

    if scope == "environment":
        monkeypatch.setenv(prompt_context.ENVIRONMENT, "끄기")
    elif scope == "user":
        prompt_context.set_depth("off", None)
    else:
        project_cli._save_snapshot(
            "team",
            "project",
            {"items": [], "prompt_context_depth": "off" if scope == "project_default" else None},
        )
        if scope == "user_project":
            prompt_context.set_depth("off", "project")
    monkeypatch.setattr(project_cli, "ProjectGateway", forbidden)
    monkeypatch.setattr(
        project_cli, "read_hook_payload", lambda: payload(local_state, "플러그인 스킬 재정의 결정")
    )
    project_cli.project_hook(SimpleNamespace(profile="team", client="claude"))
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""


def test_user_choice_wins_over_an_off_project_default(local_state, monkeypatch, capsys):
    project_cli._save_snapshot("team", "project", {"items": [], "prompt_context_depth": "off"})
    prompt_context.set_depth("light", "project")
    hook(monkeypatch, Gateway(local_state), payload(local_state, "플러그인 스킬 재정의 결정"))
    assert "depth=light" in capsys.readouterr().out


def test_unexpected_prompt_hook_failure_never_blocks_the_message(local_state, monkeypatch, capsys):
    def broken(*args, **kwargs):
        raise RuntimeError("bug")

    monkeypatch.setattr(prompt_context, "prompt_context", broken)
    hook(monkeypatch, Gateway(local_state), payload(local_state, "플러그인 스킬 재정의 결정"))
    output = capsys.readouterr()
    assert output.out == "" and output.err == ""
    [failure] = (local_state / "hook-health").rglob("last-failure.json")
    assert json.loads(failure.read_text())["event"] == "other"  # Label only, for diagnosis.


def test_hook_command_uses_the_light_parser(monkeypatch):
    from durumari_mcp import cli

    seen = []
    monkeypatch.setattr(
        sys, "argv", ["durumari-mcp", "project-hook", "--profile", "p", "--client", "codex"]
    )
    monkeypatch.setattr(cli, "build_parser", lambda: (_ for _ in ()).throw(AssertionError("heavy")))
    monkeypatch.setattr(project_cli, "project_hook", lambda args: seen.append(args))
    cli.main()
    assert [(args.command, args.profile, args.client) for args in seen] == [
        ("project-hook", "p", "codex")
    ]
