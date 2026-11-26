"""Shared native/remote management uses one metadata-only contract."""

from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from durumari_mcp.skill_tools import Requirement, register_skill_tools

ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SECRET_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


class Gateway:
    profile = SimpleNamespace(project_id="project")

    def __init__(self):
        self.calls = []

    def request(self, path, body=None, *, method="GET"):
        self.calls.append((path, body, method))
        return {"id": ID, "project_id": "project", "revision": 2}


def toolset(gateway, native=False):
    found = {}
    register_skill_tools(
        gateway, lambda fn, **kw: found.update({fn.__name__: fn}), ["read", "write"], native=native
    )
    return found


def test_search_filter_and_query_encoding():
    gateway = Gateway()
    tools = toolset(gateway)
    tools["durumari_skill_list"](
        q="ops 운영", permission="create_secret", requires_secret=True, limit=5, after=ID
    )
    url, body, method = gateway.calls[0]
    assert parse_qs(urlsplit(url).query) == {
        "q": ["ops 운영"],
        "permission": ["create_secret"],
        "requires_secret": ["true"],
        "limit": ["5"],
        "after": [ID],
    }
    assert body is None and method == "GET"


def test_list_sends_new_filters_only_when_chosen():
    gateway = Gateway()
    tools = toolset(gateway)
    tools["durumari_skill_list"]()
    tools["durumari_skill_list"](tag="infra", required=False, owned=True)
    assert set(parse_qs(urlsplit(gateway.calls[0][0]).query, keep_blank_values=True)) == {
        "q",
        "after",
        "limit",
    }
    query = parse_qs(urlsplit(gateway.calls[1][0]).query)
    assert (query["tag"], query["required"], query["owned"]) == (["infra"], ["false"], ["true"])


def test_management_uses_revision_and_reference_only_dtos():
    gateway = Gateway()
    tools = toolset(gateway)
    tools["durumari_skill_update"](ID, 1, "Team ops", ["infra"])
    tools["durumari_skill_select"](ID, 2, "1.0.0")
    tools["durumari_skill_delete"](ID, 3)
    tools["durumari_skill_link"](ID, SECRET_ID, 4, 1, 2, "api_token")
    tools["durumari_skill_unlink"](ID, SECRET_ID, 5, 6)
    assert [call[2] for call in gateway.calls] == ["PATCH", "PATCH", "DELETE", "PATCH", "DELETE"]
    assert gateway.calls[3][1] == {
        "base_revision": 4,
        "secret_revision": 1,
        "secret_policy_revision": 2,
        "slot": "api_token",
    }
    assert gateway.calls[4][1] == {"base_revision": 5, "link_revision": 6}


SKILLS_URL = "https://durumari.kr/clouds/cloud-1/projects/project/skills"


class Linking(Gateway):
    def __init__(self, skills_url=SKILLS_URL, project="project"):
        super().__init__()
        self.skills_url, self.project = skills_url, project

    def request(self, path, body=None, *, method="GET"):
        self.calls.append((path, body, method))
        if path.endswith("/principals"):
            return {"skills_url": self.skills_url, "users": [], "groups": []}
        return {
            "id": ID,
            "project_id": self.project,
            "name": "team",
            "revision": 2,
            "policy_revision": 3,
        }


def test_permission_and_required_changes_only_return_the_browser_settings_link():
    gateway = Linking()
    tools = toolset(gateway)
    shared = tools["durumari_skill_share"](ID, "group", SECRET_ID, ["read", "write"])
    required = tools["durumari_skill_set_required"](ID, True)
    assert {call[2] for call in gateway.calls} == {"GET"}
    assert all(call[1] is None for call in gateway.calls)
    assert shared["status"] == "browser_confirmation_required"
    assert shared["url"] == SKILLS_URL + "?skill=" + ID + "&settings=permissions"
    assert shared["requested"] == {
        "principal_type": "group",
        "principal_id": SECRET_ID,
        "permissions": ["read", "write"],
        "read_policy": None,
    }
    assert (shared["revision"], shared["policy_revision"]) == (2, 3)
    assert required["url"].endswith("&settings=required")
    assert required["requested"] == {"required": True}
    assert "password" in shared["next_step"]


@pytest.mark.parametrize(
    "skills_url",
    [
        None,
        "https://evil.example/clouds/cloud-1/projects/other/skills",
        "https://durumari.kr/clouds/cloud-1/projects/project/skills?next=x",
        "https://user@durumari.kr/clouds/cloud-1/projects/project/skills",
        "http://durumari.kr/clouds/cloud-1/projects/project/skills",
        "javascript:alert(1)//clouds/c/projects/project/skills",
        "https://durumari.kr/clouds/cloud-1/projects/project/skills/../vault",
    ],
)
def test_settings_link_only_points_at_this_projects_skills_page(skills_url):
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError, match="skill_settings_link_unavailable"):
        toolset(Linking(skills_url))["durumari_skill_share"](ID, read_policy="members")


def test_settings_link_rejects_another_projects_skill():
    from mcp.server.mcpserver.exceptions import ToolError

    tools = toolset(Linking(project="other"))
    with pytest.raises(ToolError, match="skill_scope_mismatch"):
        tools["durumari_skill_set_required"](ID, False)
    with pytest.raises(ToolError, match="skill_scope_mismatch"):
        tools["durumari_skill_get"](ID)


def test_versions_and_principals_are_plain_reads():
    gateway = Gateway()
    tools = toolset(gateway)
    tools["durumari_skill_versions"](ID)
    tools["durumari_skill_principals"]()
    assert gateway.calls == [
        ("/api/v2/agent/skills/" + ID + "/versions", None, "GET"),
        ("/api/v2/agent/skills/principals", None, "GET"),
    ]


def test_publish_transmits_only_files_and_named_requirements():
    gateway = Gateway()
    tools = toolset(gateway)
    tools["durumari_skill_publish"](
        "team",
        "1.0.0",
        {"SKILL.md": "public instructions"},
        [Requirement(name="api_token", kind="api_token")],
    )
    assert gateway.calls[0][1] == {
        "name": "team",
        "version": "1.0.0",
        "files": {"SKILL.md": "public instructions"},
        "requirements": [{"name": "api_token", "kind": "api_token", "required": True}],
    }


def test_remote_has_no_local_install_adapter_or_credential_operations():
    tools = toolset(Gateway())
    assert "durumari_skill_install" not in tools
    assert not any(name.endswith("_execute") for name in tools)
    assert not any("approve" in name or "credential" in name for name in tools)


def test_remote_mismatched_project_is_a_fixed_error():
    gateway = Gateway()
    gateway.profile = SimpleNamespace(project_id="other")
    with pytest.raises(Exception, match="skill_scope_mismatch"):
        toolset(gateway)["durumari_skill_update"](ID, 1, "Team")


def test_cli_single_step_install_parses_without_client_and_requests_enrollment():
    from durumari_mcp.cli import build_parser

    args = build_parser().parse_args(["project-skills", "install", "--skill", "team", "--enroll"])
    assert args.client is None and args.enroll is True


def test_install_and_enroll_share_verified_bundle_and_calling_host(tmp_path, monkeypatch):
    from durumari_mcp import skill_cli
    from durumari_mcp.project_skills import bundle_digest

    row = {
        "id": ID,
        "project_id": "project",
        "name": "team",
        "version": "1.0.0",
        "selected_version": "1.0.0",
        "requirements": [],
        "files": {"SKILL.md": "---\nname: team\ndescription: Scoped operations\n---\n"},
    }
    row["sha256"] = bundle_digest(row)
    gateway = Gateway()
    gateway.profile = SimpleNamespace(project_id="project", workspace=str(tmp_path))
    gateway.request = lambda path, body=None, method="GET": row
    enrolled = []
    monkeypatch.setattr(
        skill_cli,
        "enroll_device",
        lambda g, r: (
            enrolled.append(r["sha256"])
            or {
                "device_id": "synthetic",
                "state": "pending",
            }
        ),
    )
    result = skill_cli.install_skill(gateway, row, "cursor", enroll=True)
    assert result["device"]["state"] == "pending"
    assert result["recognition"] == "available_via_durumari_mcp"
    assert (tmp_path / ".durumari/skills/team/SKILL.md").is_file()
    assert enrolled == [row["sha256"]]


@pytest.mark.parametrize(
    ("requirements", "expected"),
    [([], []), ([{"name": "api_token", "kind": "api_token", "required": True}], ["enrolled"])],
)
def test_default_install_requests_device_access_only_for_secret_slots(
    tmp_path, monkeypatch, requirements, expected
):
    from durumari_mcp import skill_cli
    from durumari_mcp.project_skills import bundle_digest

    row = {
        "id": ID,
        "project_id": "project",
        "name": "team",
        "version": "1.0.0",
        "selected_version": "1.0.0",
        "requirements": requirements,
        "files": {"SKILL.md": "---\nname: team\ndescription: Scoped operations\n---\n"},
    }
    row["sha256"] = bundle_digest(row)
    gateway = Gateway()
    gateway.profile = SimpleNamespace(project_id="project", workspace=str(tmp_path))
    gateway.request = lambda path, body=None, method="GET": row
    enrolled = []
    monkeypatch.setattr(
        skill_cli, "enroll_device", lambda g, r: enrolled.append("enrolled") or {"state": "pending"}
    )
    result = skill_cli.install_skill(gateway, row, "gemini", enroll=None)
    assert result["status"] == "installed" and enrolled == expected
    assert ("device" in result) == bool(expected)
    assert (tmp_path / ".durumari/skills/team/SKILL.md").is_file()


@pytest.mark.anyio
async def test_mcp_tool_listing_advertises_every_skill_scope_required_by_the_gateway():
    from mcp import Client
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    class Operations(SyntheticOperations):
        skill_gateway = Gateway()

        def secrets(self, suffix="", **query):
            return {"items": []}

        def secret_request(self, body):
            return {"state": "pending"}

    async with Client(create_context_server(Operations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    expected = {
        "durumari_skill_list": ["context:read"],
        "durumari_skill_read": ["context:read"],
        "durumari_skill_publish": ["context:read", "context:write"],
        "durumari_skill_update": ["context:read", "context:write"],
        "durumari_skill_select": ["context:read", "context:write"],
        "durumari_skill_get": ["context:read"],
        "durumari_skill_principals": ["context:read"],
        "durumari_skill_versions": ["context:read"],
        "durumari_skill_share": ["context:read"],
        "durumari_skill_set_required": ["context:read"],
        "durumari_skill_delete": ["context:read", "context:delete"],
        "durumari_skill_secret_links": ["context:read", "secrets:list"],
        "durumari_skill_link": ["context:read", "context:write", "secrets:list"],
        "durumari_skill_unlink": ["context:read", "context:write", "secrets:list"],
        "durumari_secret_list": ["secrets:list"],
        "durumari_secret_status": ["secrets:list"],
        "durumari_secret_request": ["secrets:create"],
        "durumari_context_read": ["context:read"],
        "durumari_context_save": ["context:read", "context:write"],
    }
    for name, scopes in expected.items():
        assert tools[name].meta["securitySchemes"] == [{"type": "oauth2", "scopes": scopes}]


@pytest.mark.anyio
async def test_secret_request_accepts_metadata_only_and_requires_live_tool_visibility():
    import json

    from mcp import Client
    from test_cloud_protocol import SyntheticOperations

    from durumari_mcp.cloud_protocol import create_context_server

    class Operations(SyntheticOperations):
        def secrets(self, suffix="", **query):
            return {"items": []}

        def secret_request(self, body):
            self.writes.append(body)
            return {"operation_id": ID, "state": "pending", "secure_url": "/secure/request"}

    operations = Operations()
    visible = [True]
    async with Client(create_context_server(operations, secret_tools=lambda: visible[0])) as client:
        tool = next(
            t for t in (await client.list_tools()).tools if t.name == "durumari_secret_request"
        )
        assert tool.meta["securitySchemes"][0]["scopes"] == ["secrets:create"]
        result = await client.call_tool(
            "durumari_secret_request",
            {
                "kind": "replace",
                "request_key": "request-one",
                "target": {"id": SECRET_ID, "revision": 1, "policy_revision": 2},
            },
        )
        assert not result.is_error
        assert json.loads(result.content[0].text)["state"] == "pending"
        bad = await client.call_tool(
            "durumari_secret_request",
            {
                "kind": "create",
                "request_key": "request-two",
                "value": "synthetic-private-value",
            },
        )
        assert bad.is_error and "synthetic-private-value" not in str(bad)
        assert len(operations.writes) == 1
        visible[0] = False
        assert "durumari_secret_request" not in {t.name for t in (await client.list_tools()).tools}
        denied = await client.call_tool(
            "durumari_secret_request",
            {
                "kind": "create",
                "request_key": "request-three",
            },
        )
        assert denied.is_error and len(operations.writes) == 1
