"""Plugin, runtime wheel and MCP handshake report one version; status stays secret-free."""

import json
import tomllib
from types import SimpleNamespace

import pytest
from mcp import Client
from plugin_layout import AGENT as PLUGIN
from plugin_layout import PACKAGE
from test_cloud_protocol import SyntheticOperations

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.context_client import ContextClient
from durumari_mcp.context_outbox import PendingStatus
from durumari_mcp.credentials import AdapterError
from durumari_mcp.version_info import runtime_version, server_release


def test_package_and_every_plugin_manifest_share_one_version():
    project = tomllib.loads((PACKAGE / "pyproject.toml").read_text(encoding="utf-8"))
    version = project["project"]["version"]
    assert runtime_version() == version
    for name in (
        "plugin.json",
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        "gemini-extension.json",
    ):
        assert json.loads((PLUGIN / name).read_text(encoding="utf-8"))["version"] == version


@pytest.mark.anyio
async def test_mcp_server_identifier_uses_the_runtime_version():
    async with Client(create_context_server(SyntheticOperations())) as client:
        info = client.server_info
        assert (info.name, info.version) == ("Durumari Cloud Context", runtime_version())


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"release_id": "cc-40615038bf33-229494", "source_tree": "6bb9", "context_api": 2},
            {"release_id": "cc-40615038bf33-229494", "context_api": 2},
        ),
        ({"release_id": "x y", "context_api": 2}, None),
        ({"release_id": "ok", "context_api": "2"}, None),
        ({}, None),
    ],
)
def test_server_release_keeps_only_bounded_public_fields(value, expected):
    assert server_release(value) == expected


def status_client(release, workspace="/nonexistent-durumari-workspace"):
    client = ContextClient.__new__(ContextClient)
    profile = SimpleNamespace(
        connection_id="c1",
        project_id="p1",
        repository_id="r1",
        workspace_identity="w1",
        gateway_origin="https://durumari.example",
        workspace=workspace,
    )

    def transport_request(path):
        assert path == "/api/v2/release?client_version=" + runtime_version()
        if isinstance(release, Exception):
            raise release
        return release

    client.gateway = SimpleNamespace(
        profile=profile,
        context_request=lambda path: {
            "id": "c1",
            "project_id": "p1",
            "repository_id": "r1",
            "workspace_identity": "w1",
            "state": "active",
        },
        transport=SimpleNamespace(request=transport_request),
    )
    client.session = "s"
    client.queue = SimpleNamespace(pending_status=lambda session: PendingStatus(0, False))
    return client


def test_status_shows_runtime_and_server_versions_without_secrets():
    result = status_client({"release_id": "rel-1", "context_api": 2}).status()
    assert result["runtime_version"] == runtime_version()
    assert result["server"] == "https://durumari.example"
    assert result["server_release"] == {"release_id": "rel-1", "context_api": 2}
    assert not any(word in json.dumps(result) for word in ("token", "mpa_", "mpr_"))


def test_status_survives_an_unavailable_release_endpoint():
    result = status_client(AdapterError("project_delivery_unknown")).status()
    assert result["server_release"] == "unavailable" and result["state"] == "active"


def test_status_reports_the_update_state_from_the_server_announcement():
    announcement = {"latest_version": "99.0.1", "minimum_version": "99.0.0"}
    result = status_client(
        {"release_id": "rel-1", "context_api": 2, "client": announcement}
    ).status()
    assert result["server_release"] == {"release_id": "rel-1", "context_api": 2}
    assert result["update"]["state"] == "update_required"
    assert result["update"]["action"] == "durumari-mcp update"


def test_status_update_state_is_unknown_when_the_server_is_unreachable():
    result = status_client(AdapterError("project_delivery_unknown")).status()
    assert result["update"] == {"state": "unknown", "installed_version": runtime_version()}


def test_status_reports_local_settings_and_older_repository_backups(tmp_path):
    (tmp_path / "CLAUDE.md.durumari-backup-0123456789ab").write_text("x", encoding="utf-8")
    result = status_client({"release_id": "rel-1", "context_api": 2}, str(tmp_path)).status()
    assert result["local_config"]["manifest"] == {"state": "missing"}
    assert result["local_config"]["repository_backups"]["count"] == 1
