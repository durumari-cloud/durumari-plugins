"""Catalog manifests let new host files ship without breaking already installed updaters.

`fixtures/releases_0_4_4.py` is the updater shipped in 0.4.0-0.4.4. Any archive it
rejects strands those users, so stage 1 (0.4.5) must still pass it unchanged.
"""

import hashlib
import importlib.util
import io
import json
import tomllib
import zipfile
from argparse import Namespace
from pathlib import Path
from types import ModuleType

import pytest
from plugin_layout import BACKEND_LOCK, PACKAGE, SKILLS

from durumari_mcp import releases
from durumari_mcp.releases import BUNDLE, ReleaseError, catalog_bundle, extract_catalog

FIXTURE = Path(__file__).parent / "fixtures/releases_0_4_4.py"
SERVICE = {"schema_version": 1, "gateway_origin": "https://durumari.example"}
AGENT = "plugins/durumari-agent/"


def test_api_image_lock_matches_the_bundled_mcp_distribution():
    # The API image pins this package; the plugin repository has no backend to check.
    if not BACKEND_LOCK.is_file():
        pytest.skip("backend/uv.lock is not part of this checkout")
    package = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    lock = tomllib.loads(BACKEND_LOCK.read_text(encoding="utf-8"))
    distribution = next(p for p in lock["package"] if p["name"] == "durumari-mcp")
    assert distribution["version"] == package["project"]["version"]


def shipped_updater() -> ModuleType:
    spec = importlib.util.spec_from_file_location("durumari_releases_0_4_4", FIXTURE)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def dumps(item: object) -> bytes:
    return (json.dumps(item, ensure_ascii=False, indent=2) + "\n").encode()


def core_files(version: str) -> dict[str, bytes]:
    """The file set build_release.py publishes today (legacy allowlist only)."""
    wheel = f"durumari_mcp-{version}-py3-none-any.whl"
    runtime = {wheel: b"synthetic wheel " + version.encode(), "requirements.txt": b"mcp==2.2.0\n"}
    package = {"name": "durumari", "version": version}
    files = {
        "README.md": b"# Durumari plugins\n",
        ".agents/plugins/marketplace.json": dumps(
            {
                "name": "durumari",
                "plugins": [
                    {
                        "name": "durumari",
                        "source": {"source": "local", "path": "./plugins/durumari-agent"},
                    }
                ],
            }
        ),
        ".claude-plugin/marketplace.json": dumps(
            {
                "name": "durumari",
                "owner": {"name": "Durumari"},
                "plugins": [{"name": "durumari", "source": "./plugins/durumari-agent"}],
            }
        ),
        AGENT + "README.md": b"# Durumari Agent\n",
        AGENT + "plugin.json": dumps(package),
        AGENT + ".codex-plugin/plugin.json": dumps(package),
        AGENT + ".claude-plugin/plugin.json": dumps(package),
        AGENT + "scripts/durumari_plugin.py": b"print('plugin')\n",
        AGENT + "skills/context/SKILL.md": b"---\nname: context\n---\n",
        AGENT + "runtime/" + wheel: runtime[wheel],
        AGENT + "runtime/requirements.txt": runtime["requirements.txt"],
    }
    files[BUNDLE] = dumps(
        {
            "schema_version": 1,
            "package": "durumari-mcp",
            "version": version,
            "service": SERVICE,
            "files": {name: hashlib.sha256(data).hexdigest() for name, data in runtime.items()},
        }
    )
    return files


def adapter_files(version: str) -> dict[str, bytes]:
    """Files the Cursor and Gemini CLI adapters add to the release."""
    return {
        ".cursor-plugin/marketplace.json": dumps(
            {
                "name": "durumari",
                "owner": {"name": "Durumari"},
                "plugins": [{"name": "durumari", "source": "plugins/durumari-agent"}],
            }
        ),
        AGENT + ".cursor-plugin/plugin.json": dumps(
            {"name": "durumari", "version": version, "skills": "./skills/"}
        ),
        AGENT + "gemini-extension.json": dumps(
            {"name": "durumari", "version": version, "description": "Durumari"}
        ),
    }


def with_manifest(files: dict[str, bytes]) -> dict[str, bytes]:
    return {**files, BUNDLE: catalog_bundle(files[BUNDLE], files)}


def archive(files: dict[str, bytes], raw: dict[str, bytes] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zipped:
        for path, data in sorted(files.items()):
            entry = zipfile.ZipInfo("durumari-catalog/" + path, (2026, 1, 1, 0, 0, 0))
            entry.external_attr = 0o100644 << 16
            zipped.writestr(entry, data)
        for name, data in (raw or {}).items():
            zipped.writestr(name, data)
    return output.getvalue()


def stage1() -> bytes:
    return archive(with_manifest(core_files("0.4.5")))


def stage2() -> bytes:
    return archive(with_manifest({**core_files("0.4.6"), **adapter_files("0.4.6")}))


def test_shipped_updater_accepts_stage1_release(tmp_path):
    old = shipped_updater()
    root = old.extract_catalog(stage1(), tmp_path, "0.4.5")
    bundle = json.loads((root / BUNDLE).read_text())
    assert set(bundle["catalog"]["files"]) == set(core_files("0.4.5")) - {BUNDLE}


def test_shipped_updater_rejects_adapter_files_which_is_why_stage1_exists(tmp_path):
    old = shipped_updater()
    with pytest.raises(old.ReleaseError, match="invalid_release_archive"):
        old.extract_catalog(stage2(), tmp_path, "0.4.6")


def test_manifest_accepts_adapter_and_future_host_files(tmp_path):
    files = {**core_files("0.4.6"), **adapter_files("0.4.6")}
    files[".github/plugin/marketplace.json"] = b"{}\n"
    files[AGENT + "opencode/durumari.md"] = b"# OpenCode\n"
    root = extract_catalog(archive(with_manifest(files)), tmp_path, "0.4.6")
    for path, data in files.items():
        if path != BUNDLE:
            assert (root / path).read_bytes() == data


def test_stage1_is_accepted_by_its_own_updater(tmp_path):
    assert extract_catalog(stage1(), tmp_path, "0.4.5").is_dir()


def test_manifestless_archive_keeps_the_fixed_list(tmp_path):
    assert extract_catalog(archive(core_files("0.4.5")), tmp_path / "ok", "0.4.5").is_dir()
    # Cursor and Gemini files joined the fixed list in 0.4.6; any other new file
    # still needs a manifest.
    files = {**core_files("0.4.6"), AGENT + ".future-host/plugin.json": dumps({})}
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(archive(files), tmp_path / "rejected", "0.4.6")


def manifest_case(name: str) -> bytes:
    files = {**core_files("0.4.6"), **adapter_files("0.4.6")}
    listed = with_manifest(files)
    if name == "unlisted":
        return archive({**listed, AGENT + "extra.json": b"{}"})
    if name == "missing":
        listed.pop(AGENT + "gemini-extension.json")
        return archive(listed)
    if name == "hash_mismatch":
        listed[AGENT + "gemini-extension.json"] = b'{"name": "durumari", "mcpServers": {}}'
        return archive(listed)
    if name == "bundle_listed":
        bundle = json.loads(listed[BUNDLE])
        bundle["catalog"]["files"][BUNDLE] = "0" * 64
        return archive({**listed, BUNDLE: dumps(bundle)})
    if name == "bad_digest":
        bundle = json.loads(listed[BUNDLE])
        bundle["catalog"]["files"][AGENT + "gemini-extension.json"] = "not-a-digest"
        return archive({**listed, BUNDLE: dumps(bundle)})
    if name == "bad_schema":
        bundle = json.loads(listed[BUNDLE])
        bundle["catalog"]["schema_version"] = 2
        return archive({**listed, BUNDLE: dumps(bundle)})
    if name == "case_alias":
        files[AGENT + "Gemini-Extension.json"] = b"{}"
        return archive(with_manifest(files))
    raise AssertionError(name)


@pytest.mark.parametrize(
    "name",
    [
        "unlisted",
        "missing",
        "hash_mismatch",
        "bundle_listed",
        "bad_digest",
        "bad_schema",
        "case_alias",
    ],
)
def test_manifest_mismatches_are_rejected(tmp_path, name):
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(manifest_case(name), tmp_path, "0.4.6")


@pytest.mark.parametrize(
    "path",
    [
        "private.env",
        "install.sh",
        "plugins/other-plugin/plugin.json",
        "plugins/durumari-agent/.env",
        ".cursor-plugin/hook.sh",
        ".cursor-plugin/.secret.json",
        "plugins/durumari-agent/runtime/unexpected.py",
        "plugins/durumari-agent/runtime/durumari_mcp-0.4.1-py3-none-any.whl",
        "plugins/durumari-agent/CON.json",
        "plugins/durumari-agent/name with space.md",
        "plugins/durumari-agent/trailing.",
        "plugins/durumari-agent/" + "a/" * 8 + "deep.md",
    ],
)
def test_manifest_cannot_list_unexpected_paths(tmp_path, path):
    files = with_manifest({**core_files("0.4.6"), path: b"{}"})
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(archive(files), tmp_path, "0.4.6")


@pytest.mark.parametrize(
    "name",
    [
        "../escape",
        "/absolute",
        "durumari-catalog/../escape",
        "durumari-catalog/./README.md",
        "durumari-catalog//double.md",
        "durumari-catalog\\windows.md",
        "other-root/README.md",
    ],
)
def test_manifest_archive_rejects_path_escapes(tmp_path, name):
    files = with_manifest(core_files("0.4.6"))
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(archive(files, {name: b"x"}), tmp_path / "target", "0.4.6")
    assert not (tmp_path / "escape").exists()


def test_manifest_listed_symlink_is_rejected(tmp_path):
    link = AGENT + "skills/link.md"
    files = with_manifest({**core_files("0.4.6"), link: b"/etc/passwd"})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zipped:
        for path, data in files.items():
            entry = zipfile.ZipInfo("durumari-catalog/" + path)
            entry.create_system = 3
            entry.external_attr = (0o120777 if path == link else 0o100644) << 16
            zipped.writestr(entry, data)
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(buffer.getvalue(), tmp_path, "0.4.6")


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_duplicate_member_names_are_rejected(tmp_path):
    files = with_manifest(core_files("0.4.6"))
    data = archive(files, {"durumari-catalog/README.md": b"shadow"})
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(data, tmp_path, "0.4.6")


def test_size_and_count_limits_still_apply(tmp_path, monkeypatch):
    files = core_files("0.4.6")
    many = {**files, **{f"{AGENT}skills/s{index}.md": b"x" for index in range(60)}}
    with pytest.raises(ValueError, match="limits"):
        catalog_bundle(many[BUNDLE], many)
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(
            archive(
                with_manifest(files),
                {f"durumari-catalog/{AGENT}skills/s{index}.md": b"x" for index in range(60)},
            ),
            tmp_path / "count",
            "0.4.6",
        )
    monkeypatch.setattr(releases, "MAX_EXPANDED", 1024)
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(archive(with_manifest(files)), tmp_path / "size", "0.4.6")


def test_oversized_bundle_is_rejected_before_parsing(tmp_path):
    files = core_files("0.4.6")
    bundle = json.loads(files[BUNDLE])
    bundle["padding"] = "x" * releases.MAX_BUNDLE
    files[BUNDLE] = dumps(bundle)
    with pytest.raises(ValueError, match="limits"):
        catalog_bundle(files[BUNDLE], files)
    bundle["catalog"] = {"schema_version": 1, "files": {}}
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(archive({**files, BUNDLE: dumps(bundle)}), tmp_path, "0.4.6")


def serve(module: ModuleType, monkeypatch, tmp_path: Path, data: bytes, version: str) -> None:
    metadata = {
        "schema_version": 1,
        "version": version,
        "archive": {
            "path": f"/downloads/releases/{version}/durumari-plugins.zip",
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
        },
        "catalog": {"name": "durumari", "repository": module.CATALOG},
    }
    monkeypatch.setattr(module, "state_directory", lambda: tmp_path / "releases")
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        module,
        "download",
        lambda url, limit: dumps(metadata) if url.endswith("latest.json") else data,
    )
    monkeypatch.setattr(module, "install_runtime", lambda *args, **kwargs: None)
    if hasattr(module, "preflight_runtime"):
        monkeypatch.setattr(module, "preflight_runtime", lambda: None)
    monkeypatch.setattr(module, "install_host", lambda *args: None)
    monkeypatch.setattr(module, "retire_legacy_host", lambda *args: None)


def test_update_chain_from_shipped_updater_to_adapter_release(monkeypatch, tmp_path):
    """0.4.4 updater -> 0.4.5 (stage 1) -> 0.4.6 with adapter files, without manual steps."""
    old = shipped_updater()
    args = Namespace(server=SERVICE["gateway_origin"], client="codex", check=False)
    serve(old, monkeypatch, tmp_path, archive(core_files("0.4.4")), "0.4.4")
    assert old.execute_release(args)["installed"] == "0.4.4"
    # Users who skip stage 1 stay on their working installation.
    serve(old, monkeypatch, tmp_path, stage2(), "0.4.6")
    with pytest.raises(old.ReleaseError, match="invalid_release_archive"):
        old.execute_release(args)
    serve(old, monkeypatch, tmp_path, stage1(), "0.4.5")
    assert old.execute_release(args)["installed"] == "0.4.5"
    # The stage 1 runtime now owns updates and accepts the adapter release.
    serve(releases, monkeypatch, tmp_path, stage2(), "0.4.6")
    assert releases.execute_release(Namespace(server=None, check=False))["installed"] == "0.4.6"
    state = json.loads((tmp_path / "releases/installation.json").read_text())
    assert (state["version"], state["clients"]) == ("0.4.6", ["codex"])
    catalog = tmp_path / "releases/catalog"
    for path, data in adapter_files("0.4.6").items():
        assert (catalog / path).read_bytes() == data


def test_every_shipped_skill_file_is_selected_and_catalog_valid():
    from durumari_mcp.releases import allowed_member, catalog_path_allowed

    files = [
        "plugins/durumari-agent/skills/" + path.relative_to(SKILLS).as_posix()
        for path in SKILLS.rglob("*")
        if path.is_file()
    ]
    assert len(files) >= 14
    for name in files:
        assert allowed_member(name), name
        assert catalog_path_allowed(name, "0.4.12"), name
    # Older manifest-less archives keep their five-skill layout.
    assert allowed_member("plugins/durumari-agent/skills/context/SKILL.md")
    assert allowed_member("plugins/durumari-agent/skills/durumari-work-logs/SKILL.md")
    assert not allowed_member("plugins/durumari-agent/skills/durumari-context/notes.txt")
