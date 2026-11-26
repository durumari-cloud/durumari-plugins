"""Project binding, response-loss recovery and overlapping v2 requests."""

import hashlib
import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from test_continuity import profile
from test_continuity import project_store as store_fixture

from durumari_mcp.context_outbox import ContextOutbox
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import ProjectProfile, save_project_profile
from durumari_mcp.project_gateway import ProjectGateway

project_store = store_fixture


def context_profile(root):
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", "origin", "git@github.com:team/repo.git"],
        check=True,
    )
    from durumari_mcp.onboarding import repository_identity

    old = profile(root)
    value = old.model_dump()
    value.update(
        client="mcp",
        context_only=True,
        capture_enabled=False,
        repository_id=repository_identity(root),
        workspace_identity=hashlib.sha256(
            ("workspace:" + os.path.normcase(str(root.resolve()))).encode()
        ).hexdigest(),
    )
    return ProjectProfile.model_validate(value)


def test_one_approved_profile_serves_generic_agents_but_rejects_nested_repository(tmp_path):
    scope = context_profile(tmp_path)
    for client in ["codex", "claude", "cursor", "gemini", "opencode", "custom-agent"]:
        scope.check_client(client)
    child = tmp_path / "module"
    child.mkdir()
    scope.check_workspace(str(child))
    subprocess.run(["git", "init", "-q", str(child)], check=True)
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(child))
    copied = scope.model_copy(update={"workspace": str(child)})
    with pytest.raises(AdapterError):
        copied.check_binding()


def test_git_binding_keeps_subfolders_with_unrelated_manifest_files(tmp_path):
    # Folder-source nesting rules must not tighten existing Git bindings: only Git's
    # own top-level decides their boundary, so example/fixture manifests stay usable.
    scope = context_profile(tmp_path)
    child = tmp_path / "examples" / "demo"
    child.mkdir(parents=True)
    (tmp_path / "examples" / "durumari.yaml").write_text("fixture", encoding="utf-8")
    (child / "durumari.yaml").write_text("fixture", encoding="utf-8")
    scope.check_workspace(str(child))
    scope.check_workspace(str(tmp_path / "examples"))
    subprocess.run(["git", "init", "-q", str(child)], check=True)
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(child))


def test_git_binding_stops_at_a_subfolder_connected_as_its_own_folder_source(tmp_path):
    from test_folder_connection import folder_profile

    from durumari_mcp.context_binding import install_binding

    scope = context_profile(tmp_path)
    selected = tmp_path / "planning"
    inner = selected / "notes"
    inner.mkdir(parents=True)
    install_binding(folder_profile(selected), "folder", ["claude"])
    # The selected subfolder has its own approval: the parent Git profile cannot use it.
    for path in (selected, inner):
        with pytest.raises(AdapterError, match="workspace_out_of_scope"):
            scope.check_workspace(str(path))
    sibling = tmp_path / "src"
    sibling.mkdir()
    scope.check_workspace(str(sibling))


def test_v2_outbox_rejects_queued_path_to_another_project(tmp_path):
    box = ContextOutbox(tmp_path / "state", "team", profile(tmp_path))
    with pytest.raises(AdapterError, match="invalid_context_request"):
        box.enqueue("key", "a", "POST", "/api/v2/projects/another/contexts", {})


def test_two_agents_overlap_and_response_loss_recovers_same_request_key(
    project_store, tmp_path, monkeypatch
):
    from durumari_mcp.context_client import ContextClient

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("team", scope)
    gateways = [
        ProjectGateway("team", agent, root=tmp_path / "state") for agent in ("codex", "claude")
    ]
    barrier = Barrier(2)
    keys = []

    def request(path, **kwargs):
        barrier.wait(timeout=3)
        keys.append(kwargs["idempotency_key"])
        return {
            "status": "stored",
            "project_id": scope.project_id,
            "context_id": "document",
            "revision": 1,
            "request_key": kwargs["idempotency_key"],
            "replayed": False,
        }

    for gateway in gateways:
        gateway.transport.request = request

    def deliver(pair):
        try:
            return ContextClient(pair[1], pair[0]).mutate(
                "POST", "", {"title": "report", "body": "selected result"}, "request-" + pair[0]
            )
        except Exception as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as workers:
        results = list(workers.map(deliver, [("a", gateways[0]), ("b", gateways[1])]))
    assert not any(isinstance(row, Exception) for row in results), results
    assert all(row["status"] == "stored" for row in results) and len(set(keys)) == 2
    client = ContextClient(gateways[0], "a")
    attempts = []

    def lost(path, **kwargs):
        attempts.append(kwargs["idempotency_key"])
        if len(attempts) == 1:
            raise AdapterError("project_delivery_unknown")
        return {
            "status": "stored",
            "project_id": scope.project_id,
            "context_id": "document",
            "revision": 1,
            "request_key": kwargs["idempotency_key"],
            "replayed": True,
        }

    gateways[0].transport.request = lost
    body = {"title": "after loss", "body": "durable result"}
    first = client.mutate("POST", "", body, "response-loss")
    assert first["status"] == "unknown"
    # The default CLI/MCP session changes after an actual process restart.
    restarted = ContextClient(gateways[0], "new-process")
    assert restarted.queue.claim("new-process") is None
    recovered = restarted.mutate("POST", "", body, "response-loss")
    assert recovered["status"] == "stored" and attempts == ["response-loss"] * 2
    assert client.queue.pending("a") == 0


def test_operation_receipt_reconciles_unknown_write_from_an_earlier_process(
    project_store, tmp_path, monkeypatch
):
    from durumari_mcp.context_client import ContextClient

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "codex", root=tmp_path / "state")
    client = ContextClient(gateway, "old-process")
    client.queue.enqueue(
        "lost",
        "old-process",
        "POST",
        client.prefix,
        {"title": "checkpoint", "body": "selected result"},
    )
    item = client.queue.claim("old-process")
    client.queue.unknown("lost", item["lease"])
    gateway.transport.request = lambda *a, **kw: {
        "status": "stored",
        "project_id": scope.project_id,
        "context_id": "doc",
        "revision": 1,
        "request_key": "lost",
        "replayed": True,
    }
    assert ContextClient(gateway, "new-process").operation("lost")["status"] == "stored"
    assert client.queue.pending("old-process") == 0


def test_receipt_from_another_project_never_marks_request_stored(
    project_store, tmp_path, monkeypatch
):
    from durumari_mcp.context_client import ContextClient

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "codex", root=tmp_path / "state")
    gateway.transport.request = lambda *a, **kw: {
        "status": "stored",
        "project_id": "other",
        "context_id": "doc",
        "revision": 1,
        "request_key": "key",
        "replayed": False,
    }
    client = ContextClient(gateway, "a")
    with pytest.raises(AdapterError, match="invalid_context_receipt"):
        client.mutate("POST", "", {"title": "one", "body": "two"}, "key")
    assert client.queue.pending("a") == 1
