"""Cursor receives the same verified catalog through its documented local plugin path."""

import hashlib
import json
from argparse import Namespace

import pytest
from test_releases import bundle, manifest

from durumari_mcp import releases
from durumari_mcp.releases import ReleaseError, extract_catalog

CURSOR_MANIFEST = "durumari-catalog/plugins/durumari-agent/.cursor-plugin/plugin.json"
CURSOR_MARKETPLACE = "durumari-catalog/.cursor-plugin/marketplace.json"


def cursor_files(*, manifest_item=None, source="plugins/durumari-agent"):
    item = manifest_item or {"name": "durumari", "version": "0.2.0", "skills": "./skills/"}
    return {
        CURSOR_MANIFEST: json.dumps(item).encode(),
        CURSOR_MARKETPLACE: json.dumps(
            {
                "name": "durumari",
                "owner": {"name": "Durumari"},
                "plugins": [{"name": "durumari", "source": source}],
            }
        ).encode(),
        "durumari-catalog/plugins/durumari-agent/skills/connect/SKILL.md": b"---\nname: connect\n",
    }


def test_catalog_accepts_exact_cursor_manifest_and_marketplace(tmp_path):
    root = extract_catalog(bundle(cursor_files()), tmp_path, "0.2.0")
    assert (root / ".cursor-plugin/marketplace.json").is_file()
    assert (root / "plugins/durumari-agent/.cursor-plugin/plugin.json").is_file()


@pytest.mark.parametrize(
    "files",
    [
        cursor_files(source="plugins/other"),
        cursor_files(manifest_item={"name": "durumari", "version": "0.1.9"}),
        cursor_files(manifest_item={"name": "other", "version": "0.2.0"}),
        # Credentials or a fixed project server never ship in the generic plugin.
        cursor_files(
            manifest_item={
                "name": "durumari",
                "version": "0.2.0",
                "mcpServers": {"durumari_project": {"command": "durumari-mcp"}},
            }
        ),
        {CURSOR_MANIFEST: cursor_files()[CURSOR_MANIFEST]},
        {"durumari-catalog/.cursor-plugin/hooks.json": b"{}"},
    ],
)
def test_catalog_rejects_mismatched_or_unexpected_cursor_files(tmp_path, files):
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(bundle(files), tmp_path, "0.2.0")


def catalog(tmp_path, version="0.2.0"):
    plugin = tmp_path / f"catalog-{version}/plugins/durumari-agent"
    (plugin / ".cursor-plugin").mkdir(parents=True)
    (plugin / ".cursor-plugin/plugin.json").write_text(
        json.dumps({"name": "durumari", "version": version})
    )
    (plugin / "skills/context").mkdir(parents=True)
    (plugin / "skills/context/SKILL.md").write_text(version)
    return plugin.parents[1]


@pytest.fixture
def cursor_home(tmp_path, monkeypatch):
    target = tmp_path / "home/.cursor/plugins/local/durumari"
    monkeypatch.setattr(releases, "cursor_plugin_directory", lambda: target)
    monkeypatch.setattr(releases, "run", lambda *a: pytest.fail("host command executed"))
    return target


def test_cursor_install_update_and_rollback_replace_only_own_plugin(tmp_path, cursor_home):
    other = cursor_home.parent / "someone-else"
    other.mkdir(parents=True)
    (other / "marker").write_text("keep")
    first = catalog(tmp_path, "0.2.0")
    releases.install_host("cursor", first, "0.2.0")
    assert (cursor_home / "skills/context/SKILL.md").read_text() == "0.2.0"
    releases.install_host("cursor", catalog(tmp_path, "0.3.0"), "0.3.0")
    assert (cursor_home / "skills/context/SKILL.md").read_text() == "0.3.0"
    # Rollback reinstalls the previous verified catalog.
    releases.install_host("cursor", first, "0.2.0")
    assert releases.cursor_plugin_version(cursor_home) == "0.2.0"
    assert (other / "marker").read_text() == "keep"
    assert sorted(path.name for path in cursor_home.parent.iterdir()) == [
        "durumari",
        "someone-else",
    ]


@pytest.mark.parametrize("kind", ["foreign", "symlink", "file"])
def test_cursor_install_refuses_directory_it_does_not_own(tmp_path, cursor_home, kind):
    cursor_home.parent.mkdir(parents=True)
    if kind == "foreign":
        (cursor_home / ".cursor-plugin").mkdir(parents=True)
        (cursor_home / ".cursor-plugin/plugin.json").write_text(
            json.dumps({"name": "durumari-fork", "version": "9.9.9"})
        )
    elif kind == "symlink":
        real = catalog(tmp_path, "0.1.0") / "plugins/durumari-agent"
        cursor_home.symlink_to(real, target_is_directory=True)
    else:
        cursor_home.write_text("user file")
    before = sorted(str(path) for path in cursor_home.parent.rglob("*"))
    with pytest.raises(ReleaseError, match="cursor_plugin_directory_not_owned"):
        releases.install_host("cursor", catalog(tmp_path), "0.2.0")
    assert sorted(str(path) for path in cursor_home.parent.rglob("*")) == before


def test_cursor_install_rejects_catalog_version_mismatch(tmp_path, cursor_home):
    with pytest.raises(ReleaseError, match="native_plugin_version_not_verified"):
        releases.install_host("cursor", catalog(tmp_path, "0.1.0"), "0.2.0")
    assert not cursor_home.exists()


def test_cursor_release_needs_no_host_command_or_legacy_cleanup(monkeypatch, tmp_path):
    data = bundle(cursor_files())
    metadata = manifest()
    metadata["archive"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(releases, "state_directory", lambda: tmp_path / "releases")
    monkeypatch.setattr(releases.shutil, "which", lambda name: name if name == "uv" else None)
    monkeypatch.setattr(
        releases,
        "download",
        lambda url, limit: json.dumps(metadata).encode() if url.endswith("latest.json") else data,
    )
    monkeypatch.setattr(releases, "install_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "preflight_runtime", lambda: None)
    installed = []
    monkeypatch.setattr(releases, "install_host", lambda *args: installed.append(args[0]))
    monkeypatch.setattr(
        releases, "retire_legacy_host", lambda host: pytest.fail("legacy cleanup for " + host)
    )
    args = Namespace(server="https://durumari.example", client="cursor", check=False)
    result = releases.execute_release(args)
    assert result["clients"] == ["cursor"] and installed == ["cursor"]
    state = json.loads((tmp_path / "releases/installation.json").read_text())
    assert state["clients"] == ["cursor"]
    assert releases.load_state(tmp_path / "releases")["clients"] == ["cursor"]


def test_cursor_project_binding_preserves_other_servers_across_reinstall(tmp_path):
    from test_context_v2_adapter import context_profile

    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    (tmp_path / ".cursor").mkdir()
    user = {
        "mcpServers": {"other": {"command": "other", "args": ["serve"]}},
        "userSetting": True,
    }
    (tmp_path / ".cursor/mcp.json").write_text(json.dumps(user))
    for _ in range(2):
        install_binding(scope, "shared", ["cursor"])
        current = json.loads((tmp_path / ".cursor/mcp.json").read_text())
        assert current["mcpServers"]["other"] == user["mcpServers"]["other"]
        assert current["userSetting"] is True
        entry = current["mcpServers"]["durumari_project"]
        assert "context-serve" in entry["args"] and "--profile" in entry["args"]
        assert "env" not in entry or not entry["env"]
        remove_binding(scope, "shared")
        assert json.loads((tmp_path / ".cursor/mcp.json").read_text()) == user
