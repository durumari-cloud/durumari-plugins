"""A single project copy is shared through MCP; legacy discovery copies are preserved safely."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from test_skill_git_ignore import bundle, git, ignored, repo

from durumari_mcp import project_skills, skill_cli
from durumari_mcp.credentials import AdapterError

CLIENTS = ("codex", "claude", "cursor", "gemini", "opencode", "copilot", "mcp")


def legacy_copy(workspace, client="codex", data=None):
    data = data or bundle()
    target = workspace / project_skills.SKILL_ROOTS[client] / "skills" / data["name"]
    target.mkdir(parents=True)
    for name, content in data["files"].items():
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    (target / project_skills.MARKER).write_text(
        json.dumps(
            {
                "project_id": data["project_id"],
                "skill_id": data["id"],
                "version": data["version"],
                "sha256": data["sha256"],
                "files": {
                    name: hashlib.sha256(text.encode()).hexdigest()
                    for name, text in data["files"].items()
                },
            }
        )
    )
    return target


def gateway(workspace, current=None, previous=None):
    current = current or bundle(version="1.1.0")
    previous = previous or bundle()
    calls = []

    def request(path, body=None, *, method="GET"):
        calls.append((path, method, body))
        if path.endswith("/versions/" + current["version"]):
            return current
        if path.endswith("/versions/" + previous["version"]):
            return previous
        if path.endswith("/installations"):
            return {"status": "recorded"}
        raise AssertionError("unexpected synthetic request")

    return SimpleNamespace(
        profile=SimpleNamespace(workspace=str(workspace), project_id="project"),
        request=request,
        calls=calls,
    ), {**current, "selected_version": current["version"]}


@pytest.mark.parametrize("client", CLIENTS)
def test_every_supported_client_uses_the_same_project_copy(tmp_path, client):
    assert client in project_skills.INSTALL_CLIENTS
    assert project_skills.skill_root(tmp_path, client) == tmp_path / ".durumari/skills"
    result = project_skills.install_bundle(tmp_path, client, bundle())
    assert result == tmp_path / ".durumari/skills/durumari-ops"
    assert not any(
        (tmp_path / directory).exists() for directory in project_skills.SKILL_ROOTS.values()
    )


def test_clients_share_updates_without_duplicate_discovery_copies(tmp_path):
    first = project_skills.install_bundle(tmp_path, "codex", bundle())
    second = project_skills.install_bundle(tmp_path, "cursor", bundle(version="1.1.0"))
    assert first == second
    assert json.loads((first / project_skills.MARKER).read_text())["version"] == "1.1.0"
    assert len(list((tmp_path / ".durumari/skills").glob("*/SKILL.md"))) == 1
    assert not (tmp_path / ".cursor").exists()


def test_unknown_client_is_rejected_before_writing(tmp_path):
    with pytest.raises(AdapterError, match="project_client_mismatch"):
        project_skills.install_bundle(tmp_path, "unknown", bundle())
    assert not (tmp_path / ".durumari").exists()


def test_native_install_moves_only_server_verified_legacy_copies_to_backups(tmp_path):
    copies = [legacy_copy(tmp_path, client) for client in project_skills.SKILL_ROOTS]
    server, row = gateway(tmp_path)
    result = skill_cli.install_skill(server, row, "mcp")
    assert result["recognition"] == "available_via_durumari_mcp"
    assert result["path"] == str(tmp_path / ".durumari/skills/durumari-ops")
    assert all(not path.exists() for path in copies)
    assert len(result["legacy_copies"]) == 3
    assert all(item["status"] == "backed_up" for item in result["legacy_copies"])
    backups = list((tmp_path / ".durumari/skill-backups").glob("legacy-*/SKILL.md"))
    assert len(backups) == 3
    assert any(path.endswith("/versions/1.0.0") for path, _, _ in server.calls)
    assert any(method == "POST" and body["client"] == "mcp" for _, method, body in server.calls)


@pytest.mark.parametrize(
    "conflict",
    ["unmanaged", "other_project", "local_changes", "forged_marker", "symlink", "tracked"],
)
def test_legacy_conflicts_are_preserved_without_blocking_central_install(tmp_path, conflict):
    if conflict == "tracked":
        repo(tmp_path)
    target = legacy_copy(tmp_path)
    marker = target / project_skills.MARKER
    if conflict == "unmanaged":
        marker.unlink()
    elif conflict == "other_project":
        data = json.loads(marker.read_text())
        data["project_id"] = "other"
        marker.write_text(json.dumps(data))
    elif conflict in {"local_changes", "forged_marker"}:
        (target / "SKILL.md").write_text("local work, preserve me")
        if conflict == "forged_marker":
            data = json.loads(marker.read_text())
            data["files"]["SKILL.md"] = hashlib.sha256(b"local work, preserve me").hexdigest()
            marker.write_text(json.dumps(data))
    elif conflict == "symlink":
        (target / "SKILL.md").rename(target / "original")
        (target / "SKILL.md").symlink_to(target / "original")
    elif conflict == "tracked":
        git(tmp_path, "add", "--", str(target))
    original = (target / "SKILL.md").read_text()
    server, row = gateway(tmp_path)
    result = skill_cli.install_skill(server, row, "codex")
    assert result["status"] == "installed"
    assert target.exists() and (target / "SKILL.md").read_text() == original
    assert result["legacy_copies"][0]["status"] == "preserved"
    assert set(result["legacy_copies"][0]) <= {"client", "path", "status", "reason", "backup_path"}
    assert "local work" not in json.dumps(result)
    if conflict == "tracked":
        assert git(tmp_path, "ls-files", "--", str(target)).stdout
        assert not ignored(tmp_path, target / "SKILL.md")


def test_unavailable_old_release_preserves_legacy_copy_and_reports_metadata(tmp_path):
    target = legacy_copy(tmp_path)
    server, row = gateway(tmp_path)
    request = server.request

    def unavailable(path, *args, **kwargs):
        if path.endswith("/versions/1.0.0"):
            raise AdapterError("synthetic_unavailable")
        return request(path, *args, **kwargs)

    server.request = unavailable
    result = skill_cli.install_skill(server, row, "codex")
    assert target.exists()
    assert result["status"] == "installed"
    assert result["legacy_copies"][0]["status"] == "preserved"


def test_central_conflict_preserves_all_legacy_copies(tmp_path):
    target = legacy_copy(tmp_path)
    central = project_skills.install_bundle(tmp_path, "codex", bundle())
    (central / "SKILL.md").write_text("central local changes")
    server, row = gateway(tmp_path)
    with pytest.raises(AdapterError, match="skill_local_changes"):
        skill_cli.install_skill(server, row, "codex")
    assert target.exists()
    assert (central / "SKILL.md").read_text() == "central local changes"
    assert not any(method == "POST" for _, method, _ in server.calls)


def test_failed_backup_verification_restores_legacy_copy(tmp_path, monkeypatch):
    target = legacy_copy(tmp_path)
    original = project_skills.hashes

    def changed(path):
        if path.name.startswith("legacy-"):
            raise AdapterError("skill_local_changes")
        return original(path)

    monkeypatch.setattr(project_skills, "hashes", changed)
    server, row = gateway(tmp_path)
    result = skill_cli.install_skill(server, row, "codex")
    assert target.exists()
    assert (target / "SKILL.md").read_text() == bundle()["files"]["SKILL.md"]
    assert result["legacy_copies"][0]["status"] == "preserved"
    assert not list((tmp_path / ".durumari/skill-backups").glob("legacy-*"))


@pytest.mark.parametrize("client", CLIENTS)
def test_inference_keeps_the_actual_host_in_installation_report(monkeypatch, client):
    from durumari_mcp import project_credentials

    monkeypatch.setattr(
        project_credentials, "load_project_profile", lambda _: SimpleNamespace(client=client)
    )
    assert skill_cli.infer_client("synthetic") == client


def test_inference_rejects_an_explicit_unknown_host():
    with pytest.raises(AdapterError, match="project_client_mismatch"):
        skill_cli.infer_client("synthetic", "unknown")
