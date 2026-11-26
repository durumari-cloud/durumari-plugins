"""Normal project requests must overlap while credential rotation remains serialized."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

from test_continuity import connection, profile
from test_continuity import project_store as store_fixture

from durumari_mcp.project_credentials import save_project_profile
from durumari_mcp.project_gateway import ProjectGateway

project_store = store_fixture


def test_two_chats_make_requests_without_a_shared_network_lock(project_store, tmp_path):
    scope = profile(tmp_path)
    save_project_profile("team", scope)
    gateways = [ProjectGateway("team", "claude", root=tmp_path / "state") for _ in range(2)]
    both_requests = Barrier(2)

    def request(path, **kwargs):
        both_requests.wait(timeout=2)
        return connection(scope)

    for gateway in gateways:
        gateway.transport.request = request
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(g.request, "/api/v1/project-agent/connection") for g in gateways]
        assert all(f.result()["id"] == scope.connection_id for f in futures)
