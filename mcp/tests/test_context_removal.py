"""Removal must survive offline operation and never restart a stopped queue."""

import json
import subprocess
from types import SimpleNamespace

import pytest
from test_context_v2_adapter import context_profile
from test_continuity import project_store as store_fixture

from durumari_mcp import context_cli, context_removal
from durumari_mcp.context_binding import install_binding
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import load_project_profile, save_project_profile

project_store = store_fixture


def test_offline_remove_can_retry_without_local_binding(
    project_store, tmp_path, monkeypatch, capsys
):
    scope = context_profile(tmp_path)
    save_project_profile("shared", scope)
    install_binding(scope, "shared", ["claude"])
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    requests = []

    def offline(self, path, **kwargs):
        requests.append(path)
        raise AdapterError("project_delivery_unknown")

    monkeypatch.setattr(context_removal.ProjectTransport, "request", offline)
    args = SimpleNamespace(command="context-remove", workspace=str(tmp_path), profile=None)
    context_cli.execute(args)
    output = json.loads(capsys.readouterr().out)
    assert output["server_revocation"] == "pending"
    # Removal names what stays, so the user does not assume the server data is gone.
    assert output["kept"] == [*context_removal.KEPT, "shared_project_manifest"]
    assert "server_project_and_context" in output["kept"]
    assert set(output["local_files"]) == {"removed", "restored"}
    assert load_project_profile("shared").disabled
    # The shared manifest stays; the retry finds the grant through the removal marker.
    assert (tmp_path / "durumari.yaml").exists()
    assert not (tmp_path / ".durumari/context-binding.json").exists()
    assert context_removal.pending_profile(tmp_path) == "shared"
    marker = context_removal.marker(tmp_path).read_text()
    assert scope.refresh_token.get_secret_value() not in marker
    assert scope.outbox_key.get_secret_value() not in marker
    assert scope.access_token.get_secret_value() not in marker
    monkeypatch.setattr(context_removal.ProjectTransport, "request", lambda *a, **k: {})
    context_cli.execute(args)
    assert json.loads(capsys.readouterr().out)["server_revocation"] == "confirmed"
    assert context_removal.pending_profile(tmp_path) is None
    assert load_project_profile("shared").disabled
    assert requests == ["/api/v2/agent/revoke"]


def test_remove_rejects_another_root_before_mutating_anything(project_store, tmp_path, monkeypatch):
    scope = context_profile(tmp_path)
    save_project_profile("shared", scope)
    other = tmp_path / "another"
    other.mkdir()
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        context_removal.remove("shared", other)
    assert not load_project_profile("shared").disabled
    assert context_removal.pending_profile(other) is None


def test_reconnect_preserves_pending_server_revocation(project_store, tmp_path, monkeypatch):
    scope = context_profile(tmp_path)
    save_project_profile("shared", scope)
    install_binding(scope, "shared", ["claude"])
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(
        context_removal.ProjectTransport,
        "request",
        lambda *a, **k: (_ for _ in ()).throw(AdapterError("project_delivery_unknown")),
    )
    context_removal.remove("shared", tmp_path)
    with pytest.raises(AdapterError, match="project_removal_pending_retry"):
        context_removal.reconcile(tmp_path)
    assert context_removal.pending_profile(tmp_path) == "shared"
    assert load_project_profile("shared").disabled


def test_remove_can_use_owned_record_after_manifest_was_manually_removed(
    project_store, tmp_path, monkeypatch, capsys
):
    scope = context_profile(tmp_path)
    save_project_profile("shared", scope)
    install_binding(scope, "shared", ["claude"])
    (tmp_path / "durumari.yaml").unlink()
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(context_removal.ProjectTransport, "request", lambda *a, **k: {})
    args = SimpleNamespace(command="context-remove", workspace=str(tmp_path), profile=None)
    context_cli.execute(args)
    assert json.loads(capsys.readouterr().out)["server_revocation"] == "confirmed"
    context_cli.execute(args)
    assert json.loads(capsys.readouterr().out)["status"] == "not_connected"


def test_own_grant_can_be_removed_after_repository_remote_changes(
    project_store, tmp_path, monkeypatch
):
    scope = context_profile(tmp_path)
    save_project_profile("shared", scope)
    install_binding(scope, "shared", ["claude"])
    subprocess.run(
        ["git", "-C", str(tmp_path), "remote", "set-url", "origin", "git@github.com:new/repo.git"],
        check=True,
    )
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(context_removal.ProjectTransport, "request", lambda *a, **k: {})
    assert context_removal.remove("shared", tmp_path)["server_revocation"] == "confirmed"
    assert load_project_profile("shared").disabled
