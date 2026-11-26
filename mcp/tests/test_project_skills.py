"""Native installation preserves user edits and never follows a symlink."""

import pytest
from test_continuity import project_store as project_store_fixture

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_skills import bundle_digest, install_bundle

project_store = project_store_fixture


def test_install_refuses_a_windows_junction_before_creating_files(tmp_path, monkeypatch):
    from pathlib import Path

    junction = tmp_path / ".durumari"
    monkeypatch.setattr(Path, "is_junction", lambda path: path == junction)
    with pytest.raises(AdapterError, match="skill_symlink_denied"):
        install_bundle(tmp_path, "cursor", bundle())
    assert not junction.exists()


def bundle():
    row = {
        "id": "skill",
        "project_id": "project",
        "name": "durumari-ops",
        "version": "1.0.0",
        "requirements": [],
        "files": {
            "SKILL.md": "---\nname: durumari-ops\ndescription: Dedicated operations\n---\n",
            "scripts/status.py": "print('synthetic')\n",
        },
    }
    row["sha256"] = bundle_digest(row)
    return row


def test_external_layout_and_image_install_preserve_bytes_and_detect_image_edits(tmp_path):
    import base64

    from durumari_mcp.required_skills import _expected
    from durumari_mcp.skill_files import encode_file

    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH8sAAAAASUVORK5CYII="
    )
    row = bundle()
    row["files"].update(
        {
            "LICENSE.txt": "Original license\r\n",
            "AGENTS.md": "Original instructions",
            "agents/analyzer.md": "Agent guide",
            "eval-viewer/viewer.html": "<html>data only</html>",
            "assets/icon.png": encode_file("assets/icon.png", image),
        }
    )
    row["sha256"] = bundle_digest(row)
    target = install_bundle(tmp_path, "codex", row)
    assert (target / "assets/icon.png").read_bytes() == image
    assert (target / "LICENSE.txt").read_bytes() == b"Original license\r\n"
    assert (target / "agents/analyzer.md").read_text() == "Agent guide"
    assert _expected(row)["assets/icon.png"] == __import__("hashlib").sha256(image).hexdigest()
    assert install_bundle(tmp_path, "codex", row) == target
    (target / "assets/icon.png").write_bytes(b"user edits")
    with pytest.raises(AdapterError, match="skill_local_changes"):
        install_bundle(tmp_path, "codex", row)
    assert (target / "assets/icon.png").read_bytes() == b"user edits"


@pytest.mark.parametrize(
    "client, directory", [("claude", ".claude"), ("codex", ".agents"), ("cursor", ".cursor")]
)
def test_install_is_atomic_and_local_edits_are_preserved(tmp_path, client, directory):
    data = bundle()
    target = install_bundle(tmp_path, client, data)
    assert target == tmp_path / ".durumari" / "skills" / "durumari-ops"
    assert not (tmp_path / directory).exists()
    assert (target / "SKILL.md").read_text() == data["files"]["SKILL.md"]
    assert install_bundle(tmp_path, client, data) == target
    (target / "scripts/status.py").write_text("local work")
    with pytest.raises(AdapterError, match="skill_local_changes"):
        install_bundle(tmp_path, client, data)
    assert (target / "scripts/status.py").read_text() == "local work"


@pytest.mark.parametrize(
    "client, directory", [("claude", ".claude"), ("codex", ".agents"), ("cursor", ".cursor")]
)
def test_unmanaged_name_and_symlink_collisions_are_not_overwritten(tmp_path, client, directory):
    existing = tmp_path / ".durumari" / "skills" / "durumari-ops"
    existing.mkdir(parents=True)
    with pytest.raises(AdapterError, match="skill_name_collision"):
        install_bundle(tmp_path, client, bundle())
    existing.rmdir()
    existing.symlink_to(tmp_path / "outside")
    with pytest.raises(AdapterError):
        install_bundle(tmp_path, client, bundle())


@pytest.mark.parametrize(
    "path",
    [
        "../stolen",
        "references/../stolen",
        "/stolen",
        "scripts\\a",
        "references/.env",
        "references/CON",
    ],
)
def test_download_validation_rejects_unsafe_paths(tmp_path, path):
    data = bundle()
    data["files"][path] = "x"
    data["sha256"] = bundle_digest(data)
    with pytest.raises(AdapterError):
        install_bundle(tmp_path, "claude", data)
    assert not (tmp_path / ".durumari/skills/durumari-ops").exists()


@pytest.mark.parametrize("client", ["claude", "codex", "cursor"])
def test_update_retains_previous_bundle_for_recovery(tmp_path, client):
    original = bundle()
    install_bundle(tmp_path, client, original)
    updated = bundle()
    updated["version"] = "1.1.0"
    updated["files"]["references/read.md"] = "new"
    updated["sha256"] = bundle_digest(updated)
    target = install_bundle(tmp_path, client, updated)
    assert (target / "references/read.md").read_text() == "new"
    backups = list((tmp_path / ".durumari/skill-backups").glob("durumari-ops-*"))
    assert len(backups) == 1 and (backups[0] / "SKILL.md").is_file()


def test_offline_forget_does_not_require_a_connection(monkeypatch, capsys):
    import json
    from argparse import Namespace

    from durumari_mcp import skill_cli

    class Store:
        def __init__(self):
            self.values = {
                (skill_cli.SERVICE, "index:synthetic:durumari-ops"): json.dumps(
                    {"credential_store_id": "id"}
                ),
                (skill_cli.VALUES, "id"): "synthetic-private-value",
            }

        def get_password(self, service, name):
            return self.values.get((service, name))

        def delete_password(self, service, name):
            self.values.pop((service, name))

    store = Store()
    monkeypatch.setattr(skill_cli, "os_store", lambda: store)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline operation used the network")

    monkeypatch.setattr(skill_cli, "ProjectGateway", forbidden)
    skill_cli.execute(
        Namespace(
            command="project-device",
            operation="forget",
            profile="synthetic",
            client="claude",
            skill="durumari-ops",
        )
    )
    assert (skill_cli.VALUES, "id") not in store.values
    assert "synthetic-private-value" not in capsys.readouterr().out


def test_ssh_key_permissions_are_applied_before_any_private_bytes(monkeypatch, tmp_path):
    from pathlib import Path
    from types import SimpleNamespace

    from durumari_mcp import skill_cli
    from durumari_mcp.private_files import check_private_file

    class Store:
        def get_password(self, *args):
            return None

        def set_password(self, *args):
            pass

    monkeypatch.setattr(skill_cli, "os_store", lambda: Store())
    monkeypatch.setattr(skill_cli.Path, "home", lambda: tmp_path)
    checked = []
    protect_file = skill_cli.protect_private_file

    def protect(path):
        assert path.stat().st_size == 0
        checked.append(path)
        protect_file(path)

    monkeypatch.setattr(skill_cli, "protect_private_file", protect)
    gateway = SimpleNamespace(profile=SimpleNamespace(connection_id="synthetic"))
    keys = skill_cli.local_keys(gateway, "skill", create=True)
    assert checked == [Path(keys["ssh_key_path"])]
    check_private_file(checked[0])


def test_cursor_cli_status_reads_the_shared_project_install(monkeypatch, tmp_path, capsys):
    import json
    from types import SimpleNamespace

    from durumari_mcp import skill_cli
    from durumari_mcp.cli import build_parser

    data = bundle()
    install_bundle(tmp_path, "cursor", data)
    assert install_bundle(tmp_path, "claude", data) == tmp_path / ".durumari/skills/durumari-ops"
    profile = SimpleNamespace(
        project_id="project", workspace=str(tmp_path), check_workspace=lambda cwd: None
    )
    gateway = SimpleNamespace(
        profile=profile, request=lambda path: {"items": [{**data, "selected_version": "1.0.0"}]}
    )
    monkeypatch.setattr(skill_cli, "ProjectGateway", lambda name, client: gateway)
    args = build_parser().parse_args(
        [
            "project-skills",
            "status",
            "--profile",
            "synthetic",
            "--client",
            "cursor",
            "--skill",
            "durumari-ops",
        ]
    )
    skill_cli.execute(args)
    assert json.loads(capsys.readouterr().out)["installed_version"] == "1.0.0"
    assert (
        build_parser()
        .parse_args(
            [
                "project-device",
                "enroll",
                "--profile",
                "synthetic",
                "--client",
                "cursor",
                "--skill",
                "durumari-ops",
            ]
        )
        .client
        == "cursor"
    )


def test_shared_context_profile_allows_cursor_without_legacy_authority(tmp_path):
    from test_context_v2_adapter import context_profile

    profile = context_profile(tmp_path)
    profile.check_client("cursor")
    assert profile.context_only and not profile.capture_enabled
    assert profile.repository_id and profile.workspace_identity


def test_cursor_staging_is_outside_recursively_discovered_skills(monkeypatch, tmp_path):
    from durumari_mcp import project_skills

    original = project_skills.tempfile.mkdtemp
    staging = []

    def create(**kwargs):
        directory = kwargs["dir"]
        assert directory == tmp_path / ".durumari" / "skill-backups"
        result = original(**kwargs)
        staging.append(result)
        return result

    monkeypatch.setattr(project_skills.tempfile, "mkdtemp", create)
    install_bundle(tmp_path, "cursor", bundle())
    assert len(staging) == 1
    assert len(list((tmp_path / ".durumari/skills").rglob("SKILL.md"))) == 1
    assert not (tmp_path / ".cursor").exists()


def test_skill_cli_uses_current_connection_and_optional_shared_profile():
    from durumari_mcp.cli import build_parser as parser

    args = parser().parse_args(
        ["project-skills", "install", "--client", "cursor", "--skill", "durumari-ops"]
    )
    assert args.profile is None
    assert args.client == "cursor"
    connect = parser().parse_args(["context-connect", "--agents", "cursor"])
    assert connect.agents == ["cursor"]


@pytest.mark.parametrize("field", ["project_id", "id", "name", "version", "sha256"])
def test_install_checks_response_scope_and_selected_digest_before_writing(
    tmp_path, monkeypatch, field
):
    from types import SimpleNamespace

    from durumari_mcp import skill_cli
    from durumari_mcp.cli import build_parser

    row = {**bundle(), "selected_version": "1.0.0"}
    wrong = {**row, field: "foreign"}
    gateway = SimpleNamespace(
        profile=SimpleNamespace(
            project_id="project", workspace=str(tmp_path), check_workspace=lambda _: None
        ),
        request=lambda path: {"items": [row]} if path == "/api/v2/agent/skills" else wrong,
    )
    monkeypatch.setattr(skill_cli, "ProjectGateway", lambda *_: gateway)
    with pytest.raises(AdapterError, match="skill_scope_mismatch"):
        skill_cli.execute(
            build_parser().parse_args(
                [
                    "project-skills",
                    "install",
                    "--profile",
                    "synthetic",
                    "--client",
                    "cursor",
                    "--skill",
                    "durumari-ops",
                ]
            )
        )
    assert not (tmp_path / ".durumari/skills").exists()


@pytest.mark.parametrize(
    "path",
    [
        "/api/v2/projects/project/skill-secrets",
        "/api/v2/projects/foreign/contexts",
        "/api/v2/agent/skills/devices/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/approve",
        "/api/v2/agent/skills/../secrets",
        "/api/v2/agent/skills/principals/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        "/api/v2/agent/skills/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/grants",
        "/api/v2/agent/skills/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/required",
    ],
)
def test_native_skill_gateway_cannot_call_credential_administration(
    project_store, tmp_path, monkeypatch, path
):
    from test_context_v2_adapter import context_profile

    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.skill_gateway import SkillGateway

    scope = context_profile(tmp_path)
    monkeypatch.chdir(tmp_path)
    save_project_profile("synthetic", scope)
    gateway = SkillGateway("synthetic", "cursor", root=tmp_path / "state")

    def forbidden(*args, **kwargs):
        raise AssertionError("An invalid native route reached the network")

    gateway.transport.request = forbidden
    with pytest.raises(AdapterError, match="invalid_project_operation"):
        gateway.request(path, {}, method="POST")


@pytest.mark.anyio
async def test_native_mcp_exposes_management_and_fixed_adapter_without_raw_credentials(
    project_store, tmp_path, monkeypatch
):
    from mcp import Client
    from test_context_v2_adapter import context_profile

    from durumari_mcp.cloud_protocol import create_context_server
    from durumari_mcp.context_client import ContextClient
    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.skill_gateway import SkillGateway

    monkeypatch.chdir(tmp_path)
    save_project_profile("synthetic", context_profile(tmp_path))
    gateway = SkillGateway("synthetic", "cursor", root=tmp_path / "state")
    async with Client(create_context_server(ContextClient(gateway, "native-test"))) as client:
        tools = (await client.list_tools()).tools
        skills = [tool for tool in tools if tool.name.startswith("durumari_skill_")]
        assert {tool.name for tool in skills} == {
            "durumari_skill_list",
            "durumari_skill_read",
            "durumari_skill_get",
            "durumari_skill_versions",
            "durumari_skill_principals",
            "durumari_skill_publish",
            "durumari_skill_update",
            "durumari_skill_select",
            "durumari_skill_share",
            "durumari_skill_set_required",
            "durumari_skill_delete",
            "durumari_skill_secret_links",
            "durumari_skill_link",
            "durumari_skill_unlink",
            "durumari_skill_install",
            "durumari_skill_import_git",
            "durumari_skill_import_git_checkout",
            "durumari_skill_sync_status",
            "durumari_skill_sync",
        }
        reads = {
            "durumari_skill_list",
            "durumari_skill_read",
            "durumari_skill_get",
            "durumari_skill_versions",
            "durumari_skill_principals",
            "durumari_skill_share",
            "durumari_skill_set_required",
            "durumari_skill_secret_links",
            "durumari_skill_sync_status",
        }
        assert all(tool.annotations.read_only_hint == (tool.name in reads) for tool in skills)
        assert not any("credential" in tool.name for tool in tools)
        vault = [tool for tool in tools if tool.name.startswith("durumari_secret_")]
        assert {tool.name for tool in vault} == {
            "durumari_secret_list",
            "durumari_secret_status",
            "durumari_secret_request",
        }
        assert all(tool.title for tool in vault)
        assert all(
            tool.annotations.read_only_hint == (tool.name != "durumari_secret_request")
            for tool in vault
        )
