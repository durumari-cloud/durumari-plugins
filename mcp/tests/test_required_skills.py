"""Required Skills synchronize safely without blocking unrelated project work."""

import hashlib
import json
import subprocess
from types import SimpleNamespace

import pytest
from test_context_connect import (
    approval,
    connection,
    enrollment,
)
from test_context_connect import (
    project_store as project_store,
)
from test_context_connect import (
    setup as setup,
)

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_gateway import ContextFailure
from durumari_mcp.project_skills import MARKER, bundle_digest, install_bundle, release_marker
from durumari_mcp.required_skills import status, sync

PROJECT = "12345678-1234-1234-1234-123456789012"
SKILL = "87654321-4321-4321-4321-210987654321"


def bundle(version="1.0.0"):
    row = {
        "id": SKILL,
        "project_id": PROJECT,
        "name": "team-reader",
        "version": version,
        "files": {
            "SKILL.md": "---\nname: team-reader\ndescription: Team reader\n---\n" + version,
            "references/read.md": "Read-only guidance",
        },
        "requirements": [],
    }
    row["sha256"] = bundle_digest(row)
    return row


def legacy_copy(workspace, release, directory=".agents"):
    target = workspace / directory / "skills" / release["name"]
    for name, content in release["files"].items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (target / MARKER).write_text(json.dumps(release_marker(release)))
    return target


class Gateway:
    def __init__(self, tmp_path):
        self.profile = SimpleNamespace(project_id=PROJECT, workspace=str(tmp_path))
        self.name, self.client = "synthetic", "codex"
        self.selected = "1.0.0"
        self.bundles = {version: bundle(version) for version in ("1.0.0", "1.1.0")}
        self.calls = []
        self.failure = None
        self.change_after_report = False

    def request(self, path, body=None, *, method="GET"):
        self.calls.append((path, body, method))
        if self.failure:
            raise self.failure
        if path == "/api/v2/agent/skills/required":
            selected = self.bundles[self.selected]
            return {
                "items": [
                    {
                        "id": SKILL,
                        "project_id": PROJECT,
                        "name": selected["name"],
                        "selected_version": self.selected,
                        "sha256": selected["sha256"],
                        "required": True,
                    }
                ]
            }
        if "/versions/" in path:
            return self.bundles[path.rsplit("/", 1)[1]]
        if path.endswith("/installations"):
            if self.change_after_report:
                self.selected = "1.1.0"
            return {"status": "reported"}
        raise AssertionError("Unexpected route")


def test_status_is_read_only_and_sync_installs_without_enrolling(tmp_path):
    gateway = Gateway(tmp_path)
    assert status(gateway, "codex")["items"][0]["state"] == "missing"
    assert not (tmp_path / ".durumari").exists()
    result = sync(gateway, "codex")
    assert result["status"] == "current" and result["updated"] == [SKILL]
    assert (tmp_path / ".durumari/skills/team-reader/SKILL.md").is_file()
    assert result["mcp_ready"] is True
    assert result["access"] == "mcp" and result["storage"] == "project_shared"
    assert "recognition" not in result
    assert any(path.endswith("/installations") for path, _, _ in gateway.calls)
    assert not any("/devices" in path for path, _, _ in gateway.calls)


def test_server_selection_is_refreshed_and_unmodified_old_release_updates(tmp_path):
    gateway = Gateway(tmp_path)
    sync(gateway, "codex")
    gateway.selected = "1.1.0"
    assert status(gateway, "codex")["items"][0]["state"] == "outdated"
    assert sync(gateway, "codex")["status"] == "current"
    assert (tmp_path / ".durumari/skills/team-reader/SKILL.md").read_text().endswith("1.1.0")


@pytest.mark.parametrize("change", ["modified", "extra", "symlink", "marker_and_file"])
def test_local_changes_are_preserved_even_if_marker_hashes_are_forged(tmp_path, change):
    gateway = Gateway(tmp_path)
    target = install_bundle(tmp_path, "codex", bundle())
    reference = target / "references/read.md"
    if change == "extra":
        (target / "references/local.md").write_text("local addition")
    elif change == "symlink":
        reference.unlink()
        reference.symlink_to(tmp_path / "outside")
    else:
        reference.write_text("local work")
        if change == "marker_and_file":
            marker = json.loads((target / MARKER).read_text())
            marker["files"]["references/read.md"] = hashlib.sha256(b"local work").hexdigest()
            (target / MARKER).write_text(json.dumps(marker))
    gateway.selected = "1.1.0"
    result = sync(gateway, "codex")
    assert result["status"] == "conflict"
    assert result["items"][0]["state"] == "conflict"
    assert not any(path.endswith("/installations") for path, _, _ in gateway.calls)
    if change in {"modified", "marker_and_file"}:
        assert reference.read_text() == "local work"


def test_missing_marker_does_not_adopt_an_unmanaged_skill(tmp_path):
    gateway = Gateway(tmp_path)
    target = tmp_path / ".durumari/skills/team-reader"
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("unmanaged")
    assert sync(gateway, "codex")["status"] == "conflict"
    assert (target / "SKILL.md").read_text() == "unmanaged"


@pytest.mark.parametrize(
    "failure",
    [AdapterError("project_delivery_unknown"), ContextFailure(403, {"code": "access_denied"})],
)
def test_network_or_permission_failure_returns_a_safe_status(tmp_path, failure):
    gateway = Gateway(tmp_path)
    gateway.failure = failure
    result = sync(gateway, "codex")
    assert result["status"] == "unavailable" and "error" in result
    assert "recovery_tool" in result
    assert not (tmp_path / ".durumari").exists()


def test_unsupported_agent_is_reported_without_installing(tmp_path):
    gateway = Gateway(tmp_path)
    result = sync(gateway, "unknown_agent")
    assert result["status"] == "unsupported_client"
    assert result["mcp_ready"] is False
    assert not (tmp_path / ".durumari").exists()


def test_bundle_scope_and_digest_are_verified_before_install(tmp_path):
    gateway = Gateway(tmp_path)
    gateway.bundles["1.0.0"]["files"]["references/read.md"] = "tampered"
    result = sync(gateway, "codex")
    assert result["status"] == "unavailable"
    assert not (tmp_path / ".durumari").exists()


def test_selection_change_during_install_is_reported_for_another_sync(tmp_path):
    gateway = Gateway(tmp_path)
    gateway.change_after_report = True
    result = sync(gateway, "codex")
    assert result["status"] == "needs_sync"
    assert result["items"][0]["version"] == "1.1.0"
    assert result["items"][0]["state"] == "outdated"


def test_connect_sync_reports_each_agent_without_changing_connection_result(monkeypatch):
    from durumari_mcp import context_cli, required_skills, skill_gateway

    calls = []
    monkeypatch.setattr(skill_gateway, "SkillGateway", lambda *args: "gateway")

    def synchronized(gateway, client):
        calls.append((gateway, client))
        return {"client": client, "status": "conflict", "items": []}

    monkeypatch.setattr(required_skills, "sync", synchronized)
    configured = {
        "project_id": PROJECT,
        "profile": "synthetic",
        "configured_agents": ["gemini", "copilot", "mcp"],
    }
    result = context_cli._sync_connected_skills(configured, ["codex"])
    assert calls == [("gateway", "gemini"), ("gateway", "copilot"), ("gateway", "mcp")]
    assert result["profile"] == configured["profile"]
    assert result["required_skills"]["clients"][0]["status"] == "conflict"
    assert "required_skills" not in configured


@pytest.mark.parametrize(
    "client", ["codex", "claude", "cursor", "gemini", "opencode", "copilot", "mcp"]
)
def test_all_project_agents_reuse_one_central_copy(tmp_path, client):
    gateway = Gateway(tmp_path)
    assert sync(gateway, "mcp")["status"] == "current"
    reports = sum(path.endswith("/installations") for path, _, _ in gateway.calls)
    result = sync(gateway, client)
    assert result["status"] == "current" and result["mcp_ready"] is True
    assert result["updated"] == []
    assert sum(path.endswith("/installations") for path, _, _ in gateway.calls) == reports + 1
    assert [
        path for path in (tmp_path / ".durumari/skills").iterdir() if (path / "SKILL.md").is_file()
    ] == [tmp_path / ".durumari/skills/team-reader"]
    assert not any((tmp_path / path).exists() for path in (".agents", ".claude", ".cursor"))


def test_current_central_release_retires_a_server_verified_old_discovery_copy(tmp_path):
    gateway = Gateway(tmp_path)
    gateway.selected = "1.1.0"
    install_bundle(tmp_path, "mcp", gateway.bundles["1.1.0"])
    legacy = legacy_copy(tmp_path, gateway.bundles["1.0.0"])

    observed = status(gateway, "mcp")
    assert observed["status"] == "current" and observed["mcp_ready"] is True
    assert observed["legacy_copies"][0]["status"] == "pending"
    assert legacy.exists() and not any(
        path.endswith("/installations") for path, _, _ in gateway.calls
    )
    assert list((tmp_path / ".durumari/skill-backups").glob("legacy-*")) == []

    result = sync(gateway, "mcp")
    assert result["status"] == "current" and result["updated"] == []
    assert result["legacy_copies"][0]["status"] == "backed_up"
    assert result["legacy_conflicts"] == [] and not legacy.exists()
    backup = tmp_path / result["legacy_copies"][0]["backup_path"]
    assert (backup / "SKILL.md").read_text().endswith("1.0.0")


@pytest.mark.parametrize("change", ["modified", "extra", "symlink", "tracked"])
def test_current_central_release_reports_preserved_legacy_conflicts_on_every_sync(tmp_path, change):
    gateway = Gateway(tmp_path)
    gateway.selected = "1.1.0"
    install_bundle(tmp_path, "mcp", gateway.bundles["1.1.0"])
    legacy = legacy_copy(tmp_path, gateway.bundles["1.0.0"])
    reference = legacy / "references/read.md"
    if change == "modified":
        reference.write_text("local work")
    elif change == "extra":
        (legacy / "references/local.md").write_text("local addition")
    elif change == "symlink":
        reference.unlink()
        reference.symlink_to(tmp_path / "outside")
    else:
        subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
        subprocess.run(["git", "add", ".agents"], cwd=tmp_path, check=True)

    for operation in (status, sync, sync, status):
        result = operation(gateway, "mcp")
        assert result["status"] == "current" and result["mcp_ready"] is True
        assert result["legacy_conflicts"][0]["status"] == "preserved"
        assert result["warnings"] == [{"skill_id": SKILL, "code": "legacy_skill_copy_conflict"}]
        assert legacy.exists()
    if change == "modified":
        assert reference.read_text() == "local work"


def test_sync_retries_legacy_migration_after_an_installation_report_failure(tmp_path):
    gateway = Gateway(tmp_path)
    gateway.selected = "1.1.0"
    install_bundle(tmp_path, "mcp", gateway.bundles["1.1.0"])
    legacy = legacy_copy(tmp_path, gateway.bundles["1.0.0"])
    original = gateway.request
    failed = False

    def request(path, body=None, *, method="GET"):
        nonlocal failed
        if path.endswith("/installations") and not failed:
            failed = True
            raise AdapterError("project_delivery_unknown")
        return original(path, body, method=method)

    gateway.request = request
    first = sync(gateway, "mcp")
    assert first["status"] == "current" and legacy.exists()
    assert first["legacy_copies"][0]["status"] == "pending"
    assert any(warning["code"] == "project_delivery_unknown" for warning in first["warnings"])
    second = sync(gateway, "mcp")
    assert second["status"] == "current" and not legacy.exists()
    assert second["legacy_copies"][0]["status"] == "backed_up"


def test_sync_distinguishes_central_readiness_from_legacy_copy_conflicts(tmp_path, monkeypatch):
    from durumari_mcp import skill_cli

    gateway = Gateway(tmp_path)
    original = skill_cli.install_skill

    def installed(*args, **kwargs):
        result = original(*args, **kwargs)
        result["legacy_copies"] = [
            {
                "client": "codex",
                "path": ".agents/skills/team-reader",
                "status": "backed_up",
                "backup_path": ".durumari/skill-backups/old",
            },
            {
                "client": "claude",
                "path": ".claude/skills/team-reader",
                "status": "preserved",
                "reason": "skill_local_changes",
                "files": {"SKILL.md": "private local contents"},
            },
            {
                "client": "cursor",
                "path": ".cursor/skills/team-reader",
                "status": "recovery_required",
                "reason": "skill_legacy_recovery_required",
            },
        ]
        return result

    monkeypatch.setattr(skill_cli, "install_skill", installed)
    result = sync(gateway, "mcp")
    assert result["status"] == "current" and result["mcp_ready"] is True
    assert [item["status"] for item in result["legacy_copies"]] == [
        "backed_up",
        "preserved",
        "recovery_required",
    ]
    assert [item["client"] for item in result["legacy_conflicts"]] == ["claude", "cursor"]
    assert all(item["skill_id"] == SKILL for item in result["legacy_copies"])
    assert all("files" not in item for item in result["legacy_copies"])
    assert "private local contents" not in json.dumps(result)
    assert result["warnings"] == [{"skill_id": SKILL, "code": "legacy_skill_copy_conflict"}]


def test_sync_accepts_an_installer_without_a_return_value(tmp_path, monkeypatch):
    from durumari_mcp import skill_cli

    gateway = Gateway(tmp_path)
    original = skill_cli.install_skill

    def installed(*args, **kwargs):
        original(*args, **kwargs)

    monkeypatch.setattr(skill_cli, "install_skill", installed)
    result = sync(gateway, "mcp")
    assert result["status"] == "current" and result["updated"] == [SKILL]
    assert "warnings" not in result


def test_fresh_legacy_observation_retains_a_recovery_backup_receipt(tmp_path, monkeypatch):
    from durumari_mcp import skill_cli

    gateway = Gateway(tmp_path)
    install_bundle(tmp_path, "mcp", gateway.bundles["1.0.0"])
    legacy = legacy_copy(tmp_path, gateway.bundles["1.0.0"])
    (legacy / "references/read.md").write_text("local work")
    original = skill_cli.install_skill

    def installed(*args, **kwargs):
        result = original(*args, **kwargs)
        result["legacy_copies"] = [
            {
                "client": "codex",
                "path": ".agents/skills/team-reader",
                "status": "recovery_required",
                "reason": "skill_legacy_restore_conflict",
                "backup_path": ".durumari/skill-backups/recover-this-copy",
            }
        ]
        return result

    monkeypatch.setattr(skill_cli, "install_skill", installed)
    result = sync(gateway, "mcp")
    assert result["legacy_conflicts"][0]["status"] == "recovery_required"
    assert result["legacy_conflicts"][0]["backup_path"].endswith("recover-this-copy")


def test_connect_sync_failure_keeps_the_successfully_created_connection(monkeypatch):
    from durumari_mcp import context_cli, skill_gateway

    def unavailable(*args):
        raise AdapterError("project_delivery_unknown")

    monkeypatch.setattr(skill_gateway, "SkillGateway", unavailable)
    result = context_cli._sync_connected_skills({"profile": "synthetic"}, ["codex"])
    assert result["profile"] == "synthetic"
    assert result["required_skills"]["status"] == "unavailable"


@pytest.mark.parametrize("existing", [False, True])
def test_new_and_reused_connections_both_sync_after_binding(
    setup, project_store, monkeypatch, existing
):
    from durumari_mcp import context_cli, required_skills
    from durumari_mcp.context_binding import install_binding
    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.project_gateway import ProjectTransport

    scope, args, _browsers = setup
    if existing:
        save_project_profile("shared", scope)
        install_binding(scope, "shared", args.agents)
    calls = []

    def synchronized(gateway, client):
        calls.append(client)
        return {"status": "needs_sync", "items": []}

    def request(self, path, **kwargs):
        if path.endswith("/connection"):
            return connection(scope)
        if path.endswith("/config"):
            return {"browser_origin": scope.gateway_origin}
        if path.endswith("/enrollments"):
            return enrollment(scope)
        if path.endswith("/poll"):
            return approval(scope)
        if path.endswith("/finish"):
            return {}
        pytest.fail("Unexpected request")

    monkeypatch.setattr(required_skills, "sync", synchronized)
    monkeypatch.setattr(ProjectTransport, "request", request)
    result = context_cli.connect(args)
    assert calls == args.agents
    assert result["required_skills"]["clients"][0]["status"] == "needs_sync"
