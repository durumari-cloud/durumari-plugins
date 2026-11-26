"""A bundle installs only against the service origin it was packaged for."""

import json
from argparse import Namespace

import pytest
from test_release_manifest import BUNDLE, archive, core_files, serve, with_manifest

from durumari_mcp import releases

LIVE, PREVIEW = "https://durumari.kr", "https://preview.durumari.kr"


def bundle_for(origin: str) -> bytes:
    files = core_files("0.4.6")
    item = json.loads(files[BUNDLE])
    item["service"] = {"schema_version": 1, "gateway_origin": origin}
    files[BUNDLE] = json.dumps(item).encode()
    return archive(with_manifest(files))


@pytest.mark.parametrize(
    "server,packaged,accepted",
    [
        (PREVIEW, PREVIEW, True),
        (LIVE, LIVE, True),
        (PREVIEW, LIVE, False),
        (LIVE, PREVIEW, False),
    ],
)
def test_installer_never_crosses_environments(monkeypatch, tmp_path, server, packaged, accepted):
    serve(releases, monkeypatch, tmp_path, bundle_for(packaged), "0.4.6")
    args = Namespace(server=server, client="codex", check=False)
    if accepted:
        assert releases.execute_release(args)["installed"] == "0.4.6"
        state = json.loads((tmp_path / "releases/installation.json").read_text())
        assert state["version"] == "0.4.6"
        # Later updates without --server keep using the installed environment.
        assert releases.execute_release(Namespace(server=None, check=False))["installed"]
    else:
        with pytest.raises(releases.ReleaseError, match="release_service_origin_mismatch"):
            releases.execute_release(args)
