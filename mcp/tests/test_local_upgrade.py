"""Local settings follow the plugin version without files left in the repository."""

import json
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest
import yaml
from test_context_v2_adapter import context_profile

from durumari_mcp import local_upgrade
from durumari_mcp.context_binding import install_binding, workspace_identity, write_files
from durumari_mcp.credentials import AdapterError
from durumari_mcp.local_upgrade import (
    local_config_status,
    manifest_schema,
    migrate_manifest,
    repository_backups,
)
from durumari_mcp.onboarding import load_manifest, read_manifest
from durumari_mcp.version_info import client_update

ORIGINAL = local_upgrade.user_data_directory


@pytest.mark.parametrize(
    ("platform", "environ", "home", "expected"),
    [
        (
            "win32",
            {"LOCALAPPDATA": r"C:\Users\kim\AppData\Local"},
            PureWindowsPath(r"C:\Users\kim"),
            PureWindowsPath(r"C:\Users\kim\AppData\Local\DURUMARI"),
        ),
        (
            "win32",
            {},
            PureWindowsPath(r"C:\Users\kim"),
            PureWindowsPath(r"C:\Users\kim\AppData\Local\DURUMARI"),
        ),
        (
            "darwin",
            {},
            PurePosixPath("/Users/kim"),
            PurePosixPath("/Users/kim/.local/share/durumari"),
        ),
        ("linux", {}, PurePosixPath("/home/kim"), PurePosixPath("/home/kim/.local/share/durumari")),
    ],
)
def test_user_data_folder_per_operating_system(platform, environ, home, expected):
    # The release state already lives here; backups sit next to it on every OS.
    result = ORIGINAL(platform, environ, Path(home) if platform == "win32" else home)
    assert PureWindowsPath(result) == expected if platform == "win32" else result == expected


def test_binding_backups_go_to_the_user_data_folder_one_per_file(tmp_path, isolated_user_data):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("first\n", encoding="utf-8")
    write_files(root, {"AGENTS.md": "second\n", "CLAUDE.md": "new\n"})
    write_files(root, {"AGENTS.md": "third\n"})
    assert not list(root.rglob("*.durumari-backup-*"))
    folder = isolated_user_data / "backups" / workspace_identity(root)[:16]
    backups = sorted(path.name for path in folder.iterdir())
    # CLAUDE.md was new, so it has no previous content; AGENTS.md keeps only the latest.
    assert len(backups) == 1 and backups[0].endswith("-AGENTS.md")
    assert (folder / backups[0]).read_text(encoding="utf-8") == "second\n"


def test_nested_files_with_the_same_name_keep_separate_backups(tmp_path, isolated_user_data):
    root = tmp_path / "repo"
    (root / ".claude").mkdir(parents=True)
    (root / ".gemini").mkdir()
    (root / ".claude/settings.json").write_text("{}", encoding="utf-8")
    (root / ".gemini/settings.json").write_text("[]", encoding="utf-8")
    write_files(root, {".claude/settings.json": "{1}", ".gemini/settings.json": "[1]"})
    folder = isolated_user_data / "backups" / workspace_identity(root)[:16]
    assert sorted(path.read_text() for path in folder.iterdir()) == ["[]", "{}"]


def test_older_repository_backups_are_reported_never_deleted(tmp_path):
    (tmp_path / ".codex").mkdir()
    old = [
        tmp_path / "AGENTS.md.durumari-backup-0123456789ab",
        tmp_path / ".codex/config.toml.durumari-backup-ba9876543210",
    ]
    for path in old:
        path.write_text("x", encoding="utf-8")
    status = local_config_status(tmp_path)["repository_backups"]
    assert status["count"] == 2
    assert status["files"] == [
        ".codex/config.toml.durumari-backup-ba9876543210",
        "AGENTS.md.durumari-backup-0123456789ab",
    ]
    assert "never deletes" in status["cleanup"]
    assert all(path.exists() for path in old)


def test_status_without_older_backups_has_no_cleanup_notice(tmp_path):
    assert repository_backups(tmp_path) == []
    assert "repository_backups" not in local_config_status(tmp_path)


def legacy_manifest(root, profile, name):
    """The layout before per-user keys moved to .durumari/context-binding.json."""
    path = root / "durumari.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value.update(
        context_mode="cloud-context",
        workspace_identity=profile.workspace_identity,
        profile=name,
    )
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    (root / ".durumari/context-binding.json").unlink()
    return value


def test_legacy_manifest_is_converted_once_and_repeats_do_nothing(tmp_path):
    profile = context_profile(tmp_path)
    install_binding(profile, "member-a", ["claude"])
    legacy_manifest(tmp_path, profile, "member-a")
    assert manifest_schema(tmp_path)["state"] == "convert_on_run"

    assert migrate_manifest(tmp_path, "member-a", profile.workspace_identity) == "migrated"
    shared = read_manifest(tmp_path)
    assert not {"profile", "workspace_identity"} & set(shared)
    record = json.loads((tmp_path / ".durumari/context-binding.json").read_text())
    assert record["profile"] == "member-a"
    assert record["workspace_identity"] == profile.workspace_identity
    assert record["agents"] == ["claude"]
    load_manifest(tmp_path)
    profile.check_workspace(str(tmp_path))

    before = (tmp_path / "durumari.yaml").read_bytes()
    assert migrate_manifest(tmp_path, "member-a", profile.workspace_identity) == "current"
    assert (tmp_path / "durumari.yaml").read_bytes() == before


def test_a_teammates_legacy_keys_are_left_for_them(tmp_path):
    profile = context_profile(tmp_path)
    install_binding(profile, "member-a", ["claude"])
    value = legacy_manifest(tmp_path, profile, "member-b")
    assert migrate_manifest(tmp_path, "member-a", profile.workspace_identity) == "current"
    assert read_manifest(tmp_path) == value


def test_a_newer_manifest_asks_for_an_update_instead_of_failing(tmp_path):
    profile = context_profile(tmp_path)
    install_binding(profile, "member-a", ["claude"])
    path = tmp_path / "durumari.yaml"
    value = yaml.safe_load(path.read_text(encoding="utf-8"))
    value["schema_version"] = local_upgrade.MANIFEST_SCHEMA_VERSION + 1
    path.write_text(yaml.safe_dump(value, sort_keys=False), encoding="utf-8")
    before = path.read_bytes()
    assert local_config_status(tmp_path)["manifest"]["state"] == "update_required"
    for check in (
        lambda: read_manifest(tmp_path),
        lambda: migrate_manifest(tmp_path, "member-a", profile.workspace_identity),
    ):
        with pytest.raises(AdapterError, match="client_update_required"):
            check()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("announcement", "installed", "state"),
    [
        (
            {"latest_version": "0.4.11", "minimum_version": "0.4.10", "status": "current"},
            "0.4.11",
            "current",
        ),
        (
            {"latest_version": "0.4.12", "minimum_version": "0.4.11", "status": "update_available"},
            "0.4.11",
            "update_available",
        ),
        ({"latest_version": "0.4.13", "minimum_version": "0.4.12"}, "0.4.11", "update_required"),
        ({"latest_version": "0.4.12", "minimum_version": "0.4.11"}, "0.4.11", "update_available"),
        ({"latest_version": "0.4.11", "minimum_version": "0.4.10"}, "0+unknown", "unknown"),
        (None, "0.4.11", "unknown"),
        ({"status": "update_required"}, "0.4.11", "unknown"),
    ],
)
def test_update_state_for_a_session_start_hook(announcement, installed, state):
    value = {"release_id": "r", "context_api": 2}
    if announcement is not None:
        value["client"] = announcement
    result = client_update(value, installed)
    assert result["state"] == state and result["installed_version"] == installed
    assert ("action" in result) == (state in {"update_available", "update_required"})


def test_refused_backup_folder_never_fails_or_half_applies_the_binding(
    tmp_path, monkeypatch, capsys
):
    # An Agent sandbox or read-only home refuses the user data folder: every file is
    # still written, and the skipped backup is only reported.
    import durumari_mcp.context_binding as binding

    root = tmp_path / "repo"
    root.mkdir()
    (root / "AGENTS.md").write_text("first\n", encoding="utf-8")
    (root / "CLAUDE.md").write_text("old\n", encoding="utf-8")

    def refuse(*args, **kwargs):
        raise PermissionError("sandbox")

    monkeypatch.setattr(binding, "save_backup", refuse)
    write_files(root, {"AGENTS.md": "second\n", "CLAUDE.md": "new\n"})
    assert (root / "AGENTS.md").read_text(encoding="utf-8") == "second\n"
    assert (root / "CLAUDE.md").read_text(encoding="utf-8") == "new\n"
    assert "backup skipped for AGENTS.md" in capsys.readouterr().err
