"""Git import and synchronization expose bounded native/remote interfaces."""

import json
from io import BytesIO
from types import SimpleNamespace

import pytest

from durumari_mcp import required_skills, skill_cli, skill_git
from durumari_mcp.cli import build_parser
from durumari_mcp.skill_tools import Requirement, register_skill_tools

ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
URL = "https://gitlab.example.invalid/team/skills.git"


@pytest.mark.parametrize(
    ("path", "method", "expected"),
    [
        ("/api/v2/agent/skills/import-git", "POST", 60),
        (f"/api/v2/agent/skills/{ID}/import-git", "POST", 60),
        ("/api/v2/agent/skills/import-git", "GET", 10),
        ("/api/v2/agent/skills/import-git/extra", "POST", 10),
        ("/api/v2/agent/skills", "POST", 10),
    ],
)
def test_public_git_transport_waits_for_the_bounded_provider_budget(path, method, expected):
    from durumari_mcp.project_gateway import ProjectTransport

    class Opener:
        def open(self, request, *, timeout):
            assert timeout == expected
            response = BytesIO(b"{}")
            response.status = 200
            return response

    transport = ProjectTransport("https://durumari.kr")
    transport.opener = Opener()
    assert transport.request(path, body={} if method == "POST" else None, method=method) == {}


class Gateway:
    name, client = "fixture-profile", "mcp"

    def __init__(self, workspace):
        self.profile = SimpleNamespace(
            project_id="project", workspace=str(workspace), check_workspace=lambda _: None
        )
        self.calls = []

    def request(self, path, body=None, *, method="GET"):
        self.calls.append((path, body, method))
        return {"id": ID, "project_id": "project", "revision": 1}


def toolset(gateway, *, native):
    tools = {}
    register_skill_tools(
        gateway,
        lambda fn, **kwargs: tools.update({fn.__name__: fn}),
        ["read", "write"],
        native=native,
    )
    return tools


def imported():
    return {
        "files": {"SKILL.md": "---\nname: team\ndescription: Committed fixture\n---\n"},
        "source": {
            "repository_url": URL,
            "path": "skills/team",
            "requested_ref": "release",
            "commit": "a" * 40,
            "transport": "agent_checkout",
        },
    }


def test_remote_git_import_has_no_local_checkout_or_sync_interface(tmp_path):
    gateway = Gateway(tmp_path)
    tools = toolset(gateway, native=False)
    assert (
        not {
            "durumari_skill_import_git_checkout",
            "durumari_skill_sync",
            "durumari_skill_sync_status",
        }
        & tools.keys()
    )
    tools["durumari_skill_import_git"](
        URL,
        "1.2.0",
        "skills/team",
        "release",
        [Requirement(name="api_token", kind="api_token")],
        ID,
    )
    path, body, method = gateway.calls[-1]
    assert path == f"/api/v2/agent/skills/{ID}/import-git" and method == "POST"
    assert body == {
        "repository_url": URL,
        "version": "1.2.0",
        "path": "skills/team",
        "ref": "release",
        "requirements": [{"name": "api_token", "kind": "api_token", "required": True}],
    }


def test_native_checkout_publishes_only_committed_bundle_and_source(tmp_path, monkeypatch):
    gateway = Gateway(tmp_path)
    seen = []
    monkeypatch.setattr(skill_git, "read_checkout", lambda *args: seen.append(args) or imported())
    result = toolset(gateway, native=True)["durumari_skill_import_git_checkout"](
        URL, "1.2.0", "skills/team", "release"
    )
    assert seen == [(URL, tmp_path, "release", "skills/team")]
    path, body, method = gateway.calls[-1]
    assert path == "/api/v2/agent/skills" and method == "POST"
    assert body == {"name": "team", "version": "1.2.0", "requirements": [], **imported()}
    assert result == {"id": ID, "project_id": "project", "revision": 1}


def test_native_sync_tools_keep_generic_mcp_client_and_do_not_gate_other_tools(
    tmp_path, monkeypatch
):
    gateway = Gateway(tmp_path)
    seen = []
    monkeypatch.setattr(
        required_skills, "status", lambda g, c: seen.append((g, c)) or {"status": "conflict"}
    )
    monkeypatch.setattr(
        required_skills, "sync", lambda g, c: seen.append((g, c)) or {"status": "current"}
    )
    tools = toolset(gateway, native=True)
    assert tools["durumari_skill_sync_status"]() == {"status": "conflict"}
    assert tools["durumari_skill_sync"]("gemini") == {"status": "current"}
    assert seen == [(gateway, "mcp"), (gateway, "gemini")]
    tools["durumari_skill_list"]()
    assert gateway.calls[-1][2] == "GET"


def test_cli_private_import_preserves_source_and_does_not_echo_bundle(
    tmp_path, monkeypatch, capsys
):
    gateway = Gateway(tmp_path)
    monkeypatch.setattr(skill_cli, "ProjectGateway", lambda *args: gateway)
    monkeypatch.setattr(skill_git, "read_checkout", lambda *args: imported())
    args = build_parser().parse_args(
        [
            "project-skills",
            "import-git",
            "--profile",
            "fixture-profile",
            "--client",
            "mcp",
            "--repository",
            URL,
            "--checkout",
            str(tmp_path),
            "--path",
            "skills/team",
            "--ref",
            "release",
            "--version",
            "1.2.0",
            "--requirement",
            "api_token:api_token",
        ]
    )
    skill_cli.execute(args)
    result = json.loads(capsys.readouterr().out)
    assert result == {"id": ID, "project_id": "project", "revision": 1}
    assert gateway.calls[-1][1]["source"] == imported()["source"]
    assert gateway.calls[-1][1]["requirements"] == [
        {"name": "api_token", "kind": "api_token", "required": True}
    ]


def test_cli_sync_is_available_without_a_skill_name(tmp_path, monkeypatch, capsys):
    gateway = Gateway(tmp_path)
    monkeypatch.setattr(skill_cli, "ProjectGateway", lambda *args: gateway)
    monkeypatch.setattr(required_skills, "sync", lambda g, c: {"status": "current", "client": c})
    args = build_parser().parse_args(
        ["project-skills", "sync", "--profile", "fixture-profile", "--client", "opencode"]
    )
    skill_cli.execute(args)
    assert json.loads(capsys.readouterr().out) == {"status": "current", "client": "opencode"}


def test_recommendation_filter_reaches_the_same_authorized_skill_listing(tmp_path):
    from urllib.parse import parse_qs, urlsplit

    gateway = Gateway(tmp_path)
    tools = toolset(gateway, native=True)
    tools["durumari_skill_list"](recommended=True, limit=6)
    query = parse_qs(urlsplit(gateway.calls[-1][0]).query)
    assert query["recommended"] == ["true"]
    assert query["limit"] == ["6"]


@pytest.mark.parametrize("kind", ["npm", "zip"])
def test_npm_and_zip_imports_use_bounded_native_routes(kind):
    """the server reads and verifies npm/zip sources; nothing runs here."""
    from durumari_mcp.project_gateway import ProjectTransport

    class Opener:
        def open(self, request, *, timeout):
            assert timeout == 60
            response = BytesIO(b"{}")
            response.status = 200
            return response

    transport = ProjectTransport("https://durumari.kr")
    transport.opener = Opener()
    for path in (f"/api/v2/agent/skills/import-{kind}", f"/api/v2/agent/skills/{ID}/import-{kind}"):
        assert transport.request(path, body={}, method="POST") == {}


def test_import_cli_sends_npm_names_and_zip_bytes_only(tmp_path):
    import base64

    gateway = Gateway(tmp_path)
    parser = build_parser()
    npm = parser.parse_args(
        [
            "project-skills", "import-npm", "--package", "@team/deploy",
            "--package-version", "2.1.0", "--version", "2.1.0", "--path", "skill",
        ]
    )
    skill_cli.import_package(gateway, npm)
    path, body, method = gateway.calls[-1]
    assert (path, method) == ("/api/v2/agent/skills/import-npm", "POST")
    assert body == {
        "version": "2.1.0",
        "requirements": [],
        "path": "skill",
        "package": "@team/deploy",
        "package_version": "2.1.0",
    }
    archive = tmp_path / "skill.zip"
    archive.write_bytes(b"PK\x05\x06" + b"\0" * 18)
    zipped = parser.parse_args(
        ["project-skills", "import-zip", "--archive", str(archive), "--version", "1.0.0"]
    )
    skill_cli.import_package(gateway, zipped)
    path, body, _ = gateway.calls[-1]
    assert path == "/api/v2/agent/skills/import-zip"
    assert base64.b64decode(body["archive_base64"]) == archive.read_bytes()
    assert str(tmp_path) not in json.dumps(body)


@pytest.mark.parametrize(
    ("source", "accepted"),
    [
        (
            {
                "transport": "npm_registry",
                "registry_url": "https://registry.npmjs.org",
                "package": "@team/deploy",
                "version": "2.1.0",
                "integrity": "sha512-" + "A" * 86 + "==",
                "tarball_sha256": "a" * 64,
                "path": "",
            },
            True,
        ),
        ({"transport": "zip_upload", "sha256": "a" * 64, "size_bytes": 9, "path": "s"}, True),
        ({"transport": "zip_upload", "sha256": "a" * 64, "size_bytes": 9, "path": "../s"}, False),
        ({"transport": "npm_registry", "registry_url": "https://evil.example"}, False),
    ],
)
def test_account_receipts_read_npm_and_zip_provenance(source, accepted):
    from pydantic import ValidationError

    from durumari_mcp.account_skill_gateway import SkillReceipt

    uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    receipt = {
        "operation_id": uuid,
        "project_ref": uuid,
        "request_key": "key",
        "project_id": uuid,
        "action": "metadata",
        "state": "completed",
        "source": source,
    }
    if accepted:
        assert SkillReceipt.model_validate(receipt).source is not None
    else:
        with pytest.raises(ValidationError):
            SkillReceipt.model_validate(receipt)
