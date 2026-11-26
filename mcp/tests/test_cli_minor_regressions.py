"""Minor CLI defects found in the 2026-10-21 Windows 11 regression."""

import json
import sys
from types import SimpleNamespace

import pytest
from test_context_v2_adapter import context_profile
from test_continuity import project_store as store_fixture

from durumari_mcp import cli, context_cli, context_removal
from durumari_mcp.context_binding import install_binding
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import save_project_profile
from durumari_mcp.project_gateway import ProjectGateway

project_store = store_fixture


def run_cli(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, "argv", ["durumari-mcp", *argv])
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    return capsys.readouterr().err


@pytest.mark.parametrize("command", ["context-connect", "context-status"])
def test_missing_workspace_has_its_own_code_and_guidance(monkeypatch, capsys, tmp_path, command):
    missing = tmp_path / "not-created"
    error = run_cli(monkeypatch, capsys, command, "--workspace", str(missing))
    assert error.startswith("두루마리: workspace_not_found")
    assert "작업 폴더가 없습니다" in error
    assert "operation_failed" not in error and "FileNotFoundError" not in error
    assert not missing.exists()


def test_never_connected_folder_is_not_told_to_reconnect(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    folder = tmp_path / "fresh"
    folder.mkdir()
    error = run_cli(monkeypatch, capsys, "context-status", "--workspace", str(folder))
    assert error.startswith("두루마리: project_credential_missing")
    assert "아직 두루마리에 연결되지 않았습니다" in error
    assert "다시 연결" not in error and "기존 작업 기록" not in error
    assert "context-connect" in error


def test_connected_folder_without_credentials_is_told_to_reconnect(
    project_store, monkeypatch, capsys, tmp_path
):
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    install_binding(context_profile(tmp_path), "shared", ["claude"])  # No stored credential.
    error = run_cli(monkeypatch, capsys, "context-status", "--workspace", str(tmp_path))
    assert error.startswith("두루마리: project_credential_missing")
    assert "다시 연결해 주세요. 기존 작업 기록은 보존됩니다" in error


def test_resent_save_reports_replayed(project_store, tmp_path, monkeypatch):
    from durumari_mcp.context_client import ContextClient

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "codex", root=tmp_path / "state")
    sent = []

    def request(path, **kwargs):
        sent.append(kwargs["idempotency_key"])
        return {
            "status": "stored",
            "project_id": scope.project_id,
            "context_id": "doc",
            "revision": 1,
            "request_key": kwargs["idempotency_key"],
            "replayed": False,
        }

    gateway.transport.request = request
    body = {"title": "checkpoint", "body": "result"}
    first = ContextClient(gateway, "a").mutate("POST", "", body, "same-key")
    # A new CLI process sends the same request again with the same key.
    again = ContextClient(gateway, "b").mutate("POST", "", body, "same-key")
    assert first["replayed"] is False
    assert again["replayed"] is True
    assert {k: v for k, v in again.items() if k != "replayed"} == {
        k: v for k, v in first.items() if k != "replayed"
    }
    assert sent == ["same-key"]  # Answered from the local receipt, as the server would.
    with pytest.raises(AdapterError, match="context_idempotency_conflict"):
        ContextClient(gateway, "b").mutate("POST", "", {**body, "body": "other"}, "same-key")


@pytest.fixture
def bound(project_store, tmp_path, monkeypatch):
    scope = context_profile(tmp_path)
    save_project_profile("shared", scope)
    install_binding(scope, "shared", ["claude"])
    calls = []

    class Client:
        def __init__(self, *args):
            pass

        def read(self, suffix="", **query):
            calls.append(("read", suffix, query))
            return {"items": []}

        def mutate(self, method, suffix, body, key):
            calls.append((method, suffix, body, key))
            return {
                "status": "stored",
                "project_id": scope.project_id,
                "context_id": "doc",
                "revision": 4,
                "request_key": key,
                "replayed": False,
            }

    monkeypatch.setattr(context_cli, "ProjectGateway", lambda *a: None)
    monkeypatch.setattr(context_cli, "ContextClient", Client)
    monkeypatch.setattr(context_cli, "migrate_manifest", lambda *a: None)
    return SimpleNamespace(root=tmp_path, calls=calls)


def context_args(root, action, **values):
    base = {
        "command": "context",
        "workspace": str(root),
        "profile": None,
        "client": "mcp",
        "action": action,
        "id": "doc",
        "query": None,
        "section": None,
        "view": "document",
        "revision": None,
        "after": "",
        "request_key": "key-1",
        "input": None,
    }
    return SimpleNamespace(**{**base, **values})


@pytest.mark.parametrize(
    ("action", "values", "code"),
    [
        ("delete", {}, "context_input_required"),
        ("restore", {}, "context_input_required"),
        ("delete", {"request_key": None}, "context_request_key_required"),
        ("restore", {"id": None}, "context_id_required"),
        ("search", {"query": None}, "context_query_required"),
        ("history", {"id": None}, "context_id_required"),
    ],
)
def test_missing_arguments_are_named(bound, action, values, code):
    with pytest.raises(AdapterError, match="^" + code + "$"):
        context_cli.execute(context_args(bound.root, action, **values))
    assert bound.calls == []


@pytest.mark.parametrize(("archived", "sent"), [(False, None), (True, "true")])
def test_search_asks_for_archived_context_only_when_requested(bound, archived, sent):
    # cold Context is hidden unless --include-archived is given.
    args = context_args(bound.root, "search", query="결정", include_archived=archived)
    context_cli.execute(args)
    assert bound.calls == [("read", "/search", {"q": "결정", "after": 0, "include_archived": sent})]


@pytest.mark.parametrize(
    ("action", "body"),
    [
        ("delete", {}),
        ("restore", {"base_revision": 3}),
        ("restore", {"restore_revision": 1}),
        ("restore", {"base_revision": "3", "restore_revision": 1}),
    ],
)
def test_missing_revision_fields_are_named(bound, action, body):
    path = bound.root / "input.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    with pytest.raises(AdapterError, match="^context_input_fields_required$"):
        context_cli.execute(context_args(bound.root, action, input=str(path)))
    assert bound.calls == []


def test_missing_input_guidance_shows_the_restore_shape(monkeypatch, capsys, tmp_path):
    def fail(args):
        raise AdapterError("context_input_required")

    monkeypatch.setattr(cli, "execute", fail)
    error = run_cli(monkeypatch, capsys, "context", "restore", "--id", "doc", "--request-key", "k")
    assert "--input" in error
    assert "restore_revision" in error and "activate" in error


@pytest.mark.parametrize(("activate", "stale"), [(None, True), (False, True), (True, False)])
def test_restore_result_says_whether_it_is_stale(bound, capsys, activate, stale):
    body = {"base_revision": 3, "restore_revision": 1}
    if activate is not None:
        body["activate"] = activate
    path = bound.root / "input.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    context_cli.execute(context_args(bound.root, "restore", input=str(path)))
    result = json.loads(capsys.readouterr().out)
    assert bound.calls[0][:2] == ("POST", "/doc/restore")
    assert result["status"] == "stored"
    if stale:
        assert result["state"] == "stale"
        assert result["next_action"] == "read_with_revision_or_restore_with_activate"
    else:
        assert "state" not in result and "next_action" not in result


def test_busy_server_keeps_a_project_save_queued(project_store, tmp_path, monkeypatch):
    # a spent 429 is not a rejection; the same key delivers it later.
    from durumari_mcp.context_client import ContextClient
    from durumari_mcp.project_gateway import ContextFailure

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "codex", root=tmp_path / "state")
    sent = []

    def request(path, **kwargs):
        sent.append((path, kwargs["body"], kwargs["idempotency_key"]))
        if len(sent) == 1:
            raise ContextFailure(
                429,
                {
                    "code": "project_rate_limited",
                    "details": {"retry_after_seconds": 2, "reason": "server_busy"},
                },
            )
        return {
            "status": "stored",
            "project_id": scope.project_id,
            "context_id": "doc",
            "revision": 1,
            "request_key": kwargs["idempotency_key"],
            "replayed": False,
        }

    gateway.transport.request = request
    body = {"title": "checkpoint", "body": "result"}
    first = ContextClient(gateway, "a").mutate("POST", "", body, "busy-key")
    assert first["status"] == "pending"
    assert first["error"]["details"]["retry_after_seconds"] == 2
    assert first["next_action"] == "retry_same_request_after_wait"
    again = ContextClient(gateway, "b").mutate("POST", "", body, "busy-key")
    assert again["status"] == "stored"
    assert len(sent) == 2 and sent[0] == sent[1]


def test_revise_passes_section_appends_and_allow_shrink_through(bound):
    # the CLI forwards the append and shrink-confirmation fields untouched.
    appends = {"section_appends": [{"id": "sec", "text": "한 줄"}]}
    shrink = {
        "base_revision": 2,
        "sections": [{"id": "sec", "before_sha256": "a" * 64, "text": "짧게"}],
        "allow_shrink": ["sec"],
    }
    for n, body in enumerate((appends, shrink)):
        path = bound.root / f"input-{n}.json"
        path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
        context_cli.execute(context_args(bound.root, "revise", input=str(path)))
    assert [call[:3] for call in bound.calls] == [
        ("PATCH", "/doc", appends),
        ("PATCH", "/doc", shrink),
    ]
