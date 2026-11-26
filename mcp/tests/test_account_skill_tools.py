"""Account MCP Skill tools name each conversation's target and keep account receipts."""

import json
from types import SimpleNamespace

import pytest
from mcp import Client
from test_account_skill_gateway import PROJECT, REF_A, REF_B, SKILL, Account

from durumari_mcp.account_client import AccountContextClient
from durumari_mcp.account_skill_gateway import AccountSkillGateway
from durumari_mcp.cloud_protocol import create_context_server

READS = {
    "durumari_skill_list",
    "durumari_skill_read",
    "durumari_skill_get",
    "durumari_skill_versions",
    "durumari_skill_principals",
    "durumari_skill_share",
    "durumari_skill_set_required",
    "durumari_skill_secret_links",
    "durumari_skill_operation",
}
WRITES = {
    "durumari_skill_import_git",
    "durumari_skill_publish",
    "durumari_skill_update",
    "durumari_skill_select",
    "durumari_skill_delete",
    "durumari_skill_link",
    "durumari_skill_unlink",
    "durumari_skill_import_git_checkout",
}
LOCAL = {"durumari_skill_install", "durumari_skill_sync_status", "durumari_skill_sync"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def account():
    # No profile is read: tool listing must not touch credentials or the network.
    client = object.__new__(AccountContextClient)
    client.name, client.client = "personal", "codex"
    return client


def served(tmp_path):
    fake = Account(tmp_path)
    return fake, SimpleNamespace(skill_gateway=AccountSkillGateway(fake))


async def invoke(server, name, **arguments):
    async with Client(server) as client:
        response = await client.call_tool(name, arguments)
    text = response.content[0].text
    return response.is_error, (text if response.is_error else json.loads(text))


def test_account_and_target_views_expose_skill_gateways_without_a_default_target():
    client = account()
    assert client.native_skills is True
    assert client.skill_gateway.project_ref is None
    assert client.targeted(REF_A).skill_gateway.project_ref == REF_A


@pytest.mark.anyio
async def test_account_server_lists_targeted_skill_tools_with_keys_for_writes():
    async with Client(create_context_server(account())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    assert READS | WRITES | LOCAL <= set(tools)
    for name in READS | WRITES | LOCAL:
        assert "project_ref" in tools[name].input_schema["properties"], name
    for name in WRITES:
        assert "request_key" in tools[name].input_schema["properties"], name
    assert tools["durumari_skill_import_git"].annotations.open_world_hint is True
    assert tools["durumari_skill_publish"].annotations.open_world_hint is False
    # Team-specific infrastructure tools are not part of the product.
    assert not any(name.endswith("_execute") for name in tools)


@pytest.mark.anyio
async def test_unselected_account_skill_calls_send_nothing(tmp_path):
    fake, client = served(tmp_path)
    server = create_context_server(client)
    for name, arguments in [
        ("durumari_skill_list", {}),
        ("durumari_skill_principals", {}),
        ("durumari_skill_operation", {"request_key": "key"}),
        ("durumari_skill_delete", {"skill_id": SKILL, "base_revision": 1, "request_key": "k"}),
    ]:
        failed, text = await invoke(server, name, **arguments)
        assert failed and "project_selection_required" in text
    assert fake.calls == []


@pytest.mark.anyio
async def test_account_reads_and_writes_go_to_the_named_target_only(tmp_path):
    fake, client = served(tmp_path)
    server = create_context_server(client)
    failed, row = await invoke(server, "durumari_skill_get", skill_id=SKILL, project_ref=REF_A)
    assert not failed and row["project_id"] == PROJECT
    sent = [call for call in fake.calls if call[0] != "resolve"]
    assert sent[-1][1]["body"]["project_ref"] == REF_A
    failed, text = await invoke(
        server,
        "durumari_skill_update",
        skill_id=SKILL,
        base_revision=1,
        label="Team",
        project_ref=REF_B,
    )
    assert failed and "account_skill_request_key_required" in text
    failed, receipt = await invoke(
        server,
        "durumari_skill_update",
        skill_id=SKILL,
        base_revision=1,
        label="Team",
        project_ref=REF_B,
        request_key="label",
    )
    assert not failed and receipt["state"] == "completed" and receipt["project_ref"] == REF_B
    assert fake.calls[-1][0] == "/api/v2/agent/account/skills/mutations"
    assert fake.calls[-1][1]["body"]["project_ref"] == REF_B
    failed, recovered = await invoke(
        server, "durumari_skill_operation", request_key="label", project_ref=REF_B
    )
    assert not failed and recovered["operation_id"] == receipt["operation_id"]


@pytest.mark.anyio
async def test_account_receipt_from_another_target_is_rejected(tmp_path):
    fake, client = served(tmp_path)
    original = fake.request

    def request(path, **kwargs):
        value = original(path, **kwargs)
        return {**value, "project_id": "99999999-9999-9999-9999-999999999999"}

    fake.request = request
    failed, text = await invoke(
        create_context_server(client), "durumari_skill_get", skill_id=SKILL, project_ref=REF_A
    )
    assert failed and "skill_scope_mismatch" in text


@pytest.mark.anyio
async def test_account_settings_link_uses_the_target_projects_web_screen(tmp_path):
    fake, client = served(tmp_path)
    original = fake.request

    def request(path, **kwargs):
        if path.endswith("/read") and kwargs["body"]["path"] == "/principals":
            fake.calls.append((path, kwargs))
            return {
                "skills_url": "https://durumari.kr/clouds/w1/projects/" + PROJECT + "/skills",
                "users": [],
                "groups": [],
            }
        return original(path, **kwargs)

    fake.request = request
    failed, link = await invoke(
        create_context_server(client),
        "durumari_skill_set_required",
        skill_id=SKILL,
        required=True,
        project_ref=REF_A,
    )
    assert not failed and link["status"] == "browser_confirmation_required"
    assert link["url"].endswith("/skills?skill=" + SKILL + "&settings=required")
    # Only reads were sent; the change itself happens on the web screen.
    assert {call[0] for call in fake.calls if call[0] != "resolve"} == {
        "/api/v2/agent/account/skills/read"
    }


@pytest.mark.anyio
async def test_account_install_never_enrolls(tmp_path):
    fake, client = served(tmp_path)
    server = create_context_server(client)
    failed, text = await invoke(
        server, "durumari_skill_install", skill_id=SKILL, enroll=True, project_ref=REF_A
    )
    assert failed and "account_device_approval_required" in text
    assert [call for call in fake.calls if call[0] != "resolve"] == []


@pytest.mark.anyio
async def test_unbound_folder_sync_status_leaves_the_work_folder_untouched(tmp_path, monkeypatch):
    # Without a binding the account copy lives in the user data folder.
    fake, client = served(tmp_path)
    workspace = tmp_path / "unbound"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    await invoke(create_context_server(client), "durumari_skill_sync_status", project_ref=REF_A)
    assert list(workspace.iterdir()) == []
