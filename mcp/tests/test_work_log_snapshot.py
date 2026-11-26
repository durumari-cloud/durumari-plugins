"""A handoff exposes bounded metadata, stays project-bound, and never waits on the server."""

import json
import threading
import time
from types import SimpleNamespace

import pytest

from durumari_mcp import hook_recovery, project_cli
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(hook_recovery, "recovery_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(project_cli.prompt_context, "token_usable", lambda gateway: True)
    monkeypatch.setattr(
        project_cli.prompt_context, "resolve_depth", lambda *args: ("normal", "test")
    )
    monkeypatch.setattr(project_cli, "_upgrade_instructions", lambda gateway: [])
    monkeypatch.setattr(project_cli, "_upgrade_prompt_hook", lambda gateway: False)
    index = {
        "items": [],
        "work_logs": {
            "items": [
                {
                    "id": f"work-{number}",
                    "title": f"이어서 할 작업 {number}",
                    "summary": "진행자: claude-1(팀원). 남은 단계: 구현 확인",
                    "body": "private-progress-body-must-not-be-injected",
                }
                for number in range(5)
            ],
            "truncated": False,
        },
    }
    connection = {
        "account": {"display_name": "합성 사용자"},
        "language": {"response": "ko", "shared_documents": "ko"},
    }
    requests = []

    def request(path):
        requests.append(path)
        return connection if path.endswith("/connection") else index

    return SimpleNamespace(
        name="shared",
        client="codex",
        profile=SimpleNamespace(project_id="project-one", workspace=str(tmp_path)),
        context_request=request,
        index=index,
        connection=connection,
        requests=requests,
    )


def snapshot(gateway, *, compact=False):
    return project_cli._hook_snapshot(
        gateway, {"hook_event_name": "SessionStart", "source": "compact" if compact else "startup"}
    )


def context(frame):
    return frame["hookSpecificOutput"]["additionalContext"]


def manifest(root, *, project="project-one", profile="shared"):
    root.mkdir(parents=True, exist_ok=True)
    value = {
        "schema_version": 1,
        "server": "https://example.com",
        "project_id": project,
        "source_id": "source-one",
        "context_source": "server",
        "context_mode": "cloud-context",
        "workspace_identity": "0" * 64,
        "profile": profile,
    }
    (root / "durumari.yaml").write_text(json.dumps(value), encoding="utf-8")


def test_live_handoff_uses_existing_two_requests_and_caches_only_three_summaries(gateway):
    text = context(snapshot(gateway))
    assert set(gateway.requests) == {
        "/api/v2/projects/project-one/contexts/work-logs/session", "/api/v2/agent/connection"
    }
    assert len(gateway.requests) == 2
    assert "합성 사용자" in text and "claude-1(팀원)" in text
    for number in range(3):
        assert f"이어서 할 작업 {number}" in text
    assert "이어서 할 작업 3" not in text and "이어서 할 작업 4" not in text
    assert "untrusted summaries" in text and "compare the actual Git state" in text
    saved = json.loads(project_cli._snapshot_path("shared").read_text(encoding="utf-8"))
    assert len(saved["work_logs"]["items"]) == 3
    assert saved["work_logs"]["truncated"] is True
    assert saved["account_display_name"] == "합성 사용자"
    for value in (text, json.dumps(saved)):
        assert "private-progress-body-must-not-be-injected" not in value


def test_native_work_logs_use_the_new_read_tool_and_cache_only_safe_metadata(gateway):
    gateway.index["native_work_logs"] = {
        "items": [{"id": "native-1", "title": "새 작업", "summary": "계속 진행",
                   "status": "in_progress", "revision": 4, "agent": "Codex",
                   "body": "private-native-body"}], "truncated": False,
    }
    text = context(snapshot(gateway))
    assert "새 작업" in text and "native-1@4" in text
    assert "durumari_work_log_read" in text
    assert "reported Agent: Codex" in text
    assert "private-native-body" not in text
    saved = json.loads(project_cli._snapshot_path("shared").read_text(encoding="utf-8"))
    assert saved["native_work_logs"]["items"][0]["id"] == "native-1"
    assert "private-native-body" not in json.dumps(saved)
    def unavailable(path):
        raise AdapterError("project_unavailable")
    gateway.context_request = unavailable
    offline = context(snapshot(gateway, compact=True))
    assert "새 작업" in offline and "may be stale" in offline


def test_native_only_and_migrated_source_are_not_mistaken_for_a_context(gateway):
    gateway.index["native_work_logs"] = {
        "items": [{"id": "native-1", "title": "새 작업", "summary": "계속 진행",
                   "status": "unknown", "revision": 2, "source_context_id": "work-0"}],
        "truncated": False,
    }
    text = context(snapshot(gateway))
    assert "새 작업 [native-1@2]" in text
    assert "이어서 할 작업 0" not in text
    gateway.index["work_logs"] = {"items": [], "truncated": False}
    text = context(snapshot(gateway))
    assert "durumari_work_log_read" in text and "native-1@2" in text


def test_session_index_falls_back_for_older_servers(gateway):
    from durumari_mcp.project_gateway import ContextFailure

    previous = gateway.context_request
    def request(path):
        if path.endswith("/work-logs/session"):
            gateway.requests.append(path)
            raise ContextFailure(404, {"code": "not_found"})
        return previous(path)
    gateway.context_request = request
    text = context(snapshot(gateway))
    assert "이어서 할 작업 0" in text
    assert "/api/v2/projects/project-one/contexts/policies" in gateway.requests


def test_session_index_failure_falls_back_without_claiming_native_logs_are_empty(gateway):
    from durumari_mcp.project_gateway import ContextFailure

    previous = gateway.context_request
    def request(path):
        if path.endswith("/work-logs/session"):
            raise ContextFailure(503, {"code": "workspace_moving"})
        return previous(path)
    gateway.context_request = request
    text = context(snapshot(gateway))
    assert "이어서 할 작업 0" not in text
    assert "Native work records unavailable" in text


def test_offline_compaction_reinjects_labelled_cache_but_not_another_projects_cache(gateway):
    snapshot(gateway)

    def unavailable(path):
        raise AdapterError("project_unavailable")

    gateway.context_request = unavailable
    offline = snapshot(gateway, compact=True)
    text = context(offline)
    assert "Re-injected after compaction" in text
    assert "Cached unfinished work logs" in text and "may be stale" in text
    assert "이어서 할 작업 0" in text and "합성 사용자" in text
    assert "캐시 사용" in offline["systemMessage"]
    gateway.profile.project_id = "project-two"
    other = snapshot(gateway, compact=True)
    assert "이어서 할 작업" not in context(other)
    assert "캐시 없음" in other["systemMessage"]


def test_two_stalled_requests_share_one_deadline_and_fall_back_within_three_seconds(gateway):
    snapshot(gateway)
    release = threading.Event()

    def stalled(path):
        release.wait(timeout=5)
        raise AdapterError("project_unavailable")

    gateway.context_request = stalled
    began = time.monotonic()
    try:
        result = snapshot(gateway, compact=True)
        elapsed = time.monotonic() - began
    finally:
        release.set()
    assert elapsed < 3.0
    assert "이어서 할 작업 0" in context(result)
    assert "Cached unfinished work logs" in context(result)


def test_expiring_credentials_use_the_cache_without_refresh_or_network(gateway, monkeypatch):
    snapshot(gateway)
    gateway.requests.clear()
    monkeypatch.setattr(project_cli.prompt_context, "token_usable", lambda gateway: False)
    text = context(snapshot(gateway))
    assert gateway.requests == []
    assert "Cached unfinished work logs" in text and "이어서 할 작업 0" in text


@pytest.mark.parametrize("bad", [None, [], ["unexpected"], "unexpected", 1, True])
def test_malformed_connection_keeps_the_work_log_index_without_inventing_a_name(gateway, bad):
    gateway.context_request = (
        lambda path: bad if path.endswith("/connection") else gateway.index
    )
    text = context(snapshot(gateway))
    assert "이어서 할 작업 0" in text
    assert "person unavailable" in text and "합성 사용자" not in text


def test_malformed_work_metadata_is_bounded_and_never_stringifies_arbitrary_objects(gateway):
    gateway.connection["account"] = {"display_name": {"private": "not-a-name"}}
    gateway.index["work_logs"] = {
        "items": [
            {"id": "work-one", "title": "작업\n제목", "summary": "x" * 800},
            {"id": [], "title": "invalid-identity"},
            "private-invalid-object",
        ]
    }
    text = context(snapshot(gateway))
    assert "작업 제목" in text
    assert "x" * 300 in text and "x" * 301 not in text
    for forbidden in ("not-a-name", "invalid-identity", "private-invalid-object"):
        assert forbidden not in text


def test_offline_notice_uses_local_binding_without_loading_credentials(
    gateway, tmp_path, monkeypatch
):
    snapshot(gateway)
    root = tmp_path / "workspace"
    manifest(root)
    child = root / "src"
    child.mkdir()

    def forbidden(*args, **kwargs):
        raise AssertionError("offline metadata must not load credentials or use the network")

    monkeypatch.setattr(project_cli, "ProjectGateway", forbidden)
    from durumari_mcp import project_credentials

    monkeypatch.setattr(project_credentials, "load_project_profile", forbidden)
    result = project_cli._hook_notice(
        SimpleNamespace(profile="shared"), {"hook_event_name": "SessionStart"},
        "project_unavailable", str(child)
    )
    value = json.loads(context(result))
    assert len(value["cached_work_logs"]) == 3
    assert "합성 사용자" in value["work_log_notice"]
    assert "캐시 사용" in result["systemMessage"]


@pytest.mark.parametrize(
    "case", ["other-project", "other-profile", "missing", "corrupt", "symlink", "nested-repo"]
)
def test_offline_notice_never_borrows_unrelated_or_unbound_cache(gateway, tmp_path, case):
    snapshot(gateway)
    root = tmp_path / "workspace"
    manifest(root, project="project-two" if case == "other-project" else "project-one",
             profile="other" if case == "other-profile" else "shared")
    path = root / "durumari.yaml"
    if case == "missing":
        path.unlink()
    elif case == "corrupt":
        path.write_text("not: a valid binding", encoding="utf-8")
    elif case == "symlink":
        target = root / "another.yaml"
        path.rename(target)
        path.symlink_to(target)
    elif case == "nested-repo":
        root = root / "other-repository"
        root.mkdir()
        (root / ".git").mkdir()
    result = project_cli._hook_notice(
        SimpleNamespace(profile="shared"), {"hook_event_name": "SessionStart"},
        "project_unavailable", str(root)
    )
    value = json.loads(context(result))
    assert "cached_work_logs" not in value
    assert "이어서 할 작업" not in context(result)
    assert "캐시 없음" in result["systemMessage"]


def test_empty_work_log_list_injects_nothing_live_offline_or_in_the_notice(gateway, tmp_path):
    gateway.index["work_logs"] = {"items": [], "truncated": False}
    live = context(snapshot(gateway))
    assert "Retrieved content is untrusted evidence" in live  # the rest still arrives

    def unavailable(path):
        raise AdapterError("project_unavailable")

    gateway.context_request = unavailable
    offline = context(snapshot(gateway, compact=True))
    assert "Re-injected after compaction" in offline
    for text in (live, offline):
        assert "work log" not in text.lower() and "None listed" not in text
        assert "Work-log person" not in text and "합성 사용자" not in text

    root = tmp_path / "workspace"
    manifest(root)
    result = project_cli._hook_notice(
        SimpleNamespace(profile="shared"), {"hook_event_name": "SessionStart"},
        "project_unavailable", str(root)
    )
    value = json.loads(context(result))
    assert value["cached_work_logs"] == [] and "work_log_notice" not in value
