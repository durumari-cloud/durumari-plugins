"""A teammate's connection in the shared durumari.yaml never blocks ours."""

import pytest
import yaml
from test_context_v2_adapter import context_profile

from durumari_mcp import context_removal
from durumari_mcp.context_binding import install_binding
from durumari_mcp.credentials import AdapterError


def teammate(profile, suffix="b"):
    # The server creates a new source for every approved connection.
    return profile.model_copy(
        update={"source_id": "source-teammate-" + suffix, "connection_id": "conn-" + suffix}
    )


def manifest(root):
    return yaml.safe_load((root / "durumari.yaml").read_text(encoding="utf-8"))


def commit_teammate_manifest(root, other):
    path = root / "durumari.yaml"
    value = manifest(root)
    value["source_id"] = other.source_id
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")


def test_teammate_source_id_in_the_shared_manifest_does_not_block_our_connection(tmp_path):
    mine = context_profile(tmp_path)
    install_binding(mine, "member-a", ["claude"])
    commit_teammate_manifest(tmp_path, teammate(mine))
    mine.check_workspace(str(tmp_path))  # Previously project_connection_scope_changed.
    context_removal.removal_scope(mine, tmp_path)


def test_teammate_connecting_keeps_the_committed_source_id(tmp_path):
    mine = context_profile(tmp_path)
    install_binding(mine, "member-a", ["claude"])
    first = manifest(tmp_path)
    install_binding(teammate(mine), "member-b", ["claude"])
    # The shared file no longer flips per teammate, so commits stop ping-ponging.
    assert manifest(tmp_path) == first
    mine.check_workspace(str(tmp_path))
    teammate(mine).check_workspace(str(tmp_path))


@pytest.mark.parametrize(
    ("key", "value"),
    [("project_id", "other-project"), ("server", "https://other.example.com")],
)
def test_a_manifest_redirected_to_another_project_or_server_is_still_rejected(tmp_path, key, value):
    mine = context_profile(tmp_path)
    install_binding(mine, "member-a", ["claude"])
    path = tmp_path / "durumari.yaml"
    changed = manifest(tmp_path)
    changed[key] = value
    path.write_text(yaml.safe_dump(changed, sort_keys=False), encoding="utf-8")
    with pytest.raises(AdapterError, match="project_connection_scope_changed"):
        mine.check_workspace(str(tmp_path))
    with pytest.raises(AdapterError, match="project_connection_scope_changed"):
        context_removal.removal_scope(mine, tmp_path)


def test_the_shared_manifest_keeps_the_source_id_key_for_older_runtimes(tmp_path):
    from durumari_mcp.onboarding import read_manifest

    mine = context_profile(tmp_path)
    install_binding(mine, "member-a", ["claude"])
    assert read_manifest(tmp_path)["source_id"] == mine.source_id
