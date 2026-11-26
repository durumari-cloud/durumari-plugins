
import pytest
from test_continuity import MemoryStore, profile

from durumari_mcp import project_credentials
from durumari_mcp.credentials import AdapterError
from durumari_mcp.legacy import DIRECTORY_V1, PROJECT_SERVICE_V1, data_directory


def test_old_secure_credentials_keep_scope_and_are_removed_on_disconnect(tmp_path, monkeypatch):
    store = MemoryStore()
    monkeypatch.setattr(project_credentials, "os_store", lambda: store)
    original = profile(tmp_path)
    store.set_password(PROJECT_SERVICE_V1, "existing", original.protected_json())
    loaded = project_credentials.load_project_profile("existing")
    assert loaded == original
    project_credentials.save_project_profile("existing", loaded)
    assert store.get_password(project_credentials.PROJECT_SERVICE, "existing") is not None
    assert (
        project_credentials.load_project_profile("existing").gateway_origin
        == original.gateway_origin
    )
    project_credentials.delete_project_profile("existing")
    assert not store.values


def test_pending_queue_never_disappears_or_merges_with_another_install(tmp_path):
    old = tmp_path / DIRECTORY_V1
    old.mkdir()
    (old / "project-sync").mkdir()
    (old / "project-sync" / "pending.enc").write_bytes(b"synthetic encrypted queue")
    assert data_directory(tmp_path) == old
    (tmp_path / ".durumari").mkdir()
    with pytest.raises(AdapterError, match="ambiguous_legacy_state"):
        data_directory(tmp_path)
    assert (old / "project-sync" / "pending.enc").read_bytes() == b"synthetic encrypted queue"


def test_backend_relay_directory_does_not_make_old_state_ambiguous(tmp_path):
    old = tmp_path / DIRECTORY_V1
    (old / "project-sync").mkdir(parents=True)
    spill = tmp_path / ".durumari" / "meeting-stt-relay"
    spill.mkdir(parents=True)
    (spill / "spill.bin").write_bytes(b"acked relay spill")
    assert data_directory(tmp_path) == old
    # Once the plugin itself wrote into the new directory, review is required again.
    (tmp_path / ".durumari" / "project-sync").mkdir()
    with pytest.raises(AdapterError, match="ambiguous_legacy_state"):
        data_directory(tmp_path)
    assert (spill / "spill.bin").read_bytes() == b"acked relay spill"


def test_relay_directory_alone_keeps_the_current_plugin_location(tmp_path):
    (tmp_path / ".durumari" / "meeting-stt-relay").mkdir(parents=True)
    assert data_directory(tmp_path) == tmp_path / ".durumari"




