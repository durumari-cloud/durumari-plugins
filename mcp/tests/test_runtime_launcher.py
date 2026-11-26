"""Mandatory startup updates use isolated runtimes, never the running tool environment."""

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from durumari_mcp import releases, runtime_launcher
from durumari_mcp.update_policy import parse_policy


def policy(required="0.4.17"):
    return parse_policy(
        {
            "native": {
                "latest_version": "0.4.17",
                "supported_min_version": "0.4.15",
                "required_native_version": required,
                "update_url": "/downloads",
            }
        },
        "https://durumari.kr",
    )


class Installer:
    class ReleaseError(Exception):
        pass

    class ReleaseNetworkError(ReleaseError):
        pass

    def __init__(self):
        self.data = b"verified synthetic archive"
        self.release = SimpleNamespace(
            version="0.4.17",
            path="/archive",
            size=len(self.data),
            sha256=hashlib.sha256(self.data).hexdigest(),
        )
        self.installed = []

    def release_origin(self, origin):
        return origin

    def download(self, url, limit):
        return self.data

    def parse_release(self, data):
        return self.release

    def extract_catalog(self, data, root, version):
        bundle = root / "plugins/durumari-agent/runtime"
        bundle.mkdir(parents=True)
        (bundle / "bundle.json").write_text(
            json.dumps({"service": {"schema_version": 1, "gateway_origin": "https://durumari.kr"}})
        )
        return root

    def parse_service(self, value):
        return value["gateway_origin"]

    def same_server(self, left, right):
        return releases.same_server(left, right)

    def download_origin(self, origin):
        return origin

    def install_runtime_bundle(self, bundle, version):
        target = Path(os.environ["UV_TOOL_DIR"]) / "durumari-mcp"
        target.mkdir(parents=True)
        python = self.runtime_python(target)
        python.parent.mkdir()
        python.write_text("synthetic-runtime")
        self.installed.append(target)

    def installed_runtime_version(self, runtime):
        return "0.4.17" if self.runtime_python(runtime).exists() else None

    def runtime_python(self, runtime):
        return runtime / "bin/python3"


@pytest.fixture
def configuration(tmp_path, monkeypatch):
    original = tmp_path / "running/bin/python"
    original.parent.mkdir(parents=True)
    original.write_text("preserve running runtime")
    monkeypatch.setattr(runtime_launcher, "probe_version", lambda path: "0.4.16")
    return {
        "origin": "https://durumari.kr",
        "initial_python": str(original),
        "initial_version": "0.4.16",
        "runtime_root": str(tmp_path / "isolated"),
        "uv": "/safe/uv",
    }


def test_required_update_uses_new_uv_tool_root_and_reuses_verified_result(configuration):
    installer = Installer()
    previous = os.environ.get("UV_TOOL_DIR")
    result = runtime_launcher.ensure_runtime(configuration, policy(), installer)
    assert result != Path(configuration["initial_python"])
    assert Path(configuration["initial_python"]).read_text() == "preserve running runtime"
    assert installer.installed == [
        Path(configuration["runtime_root"]) / "0.4.17/tools/durumari-mcp"
    ]
    assert os.environ.get("UV_TOOL_DIR") == previous
    assert runtime_launcher.ensure_runtime(configuration, policy(), installer) == result
    assert len(installer.installed) == 1


def test_recommendation_alone_does_not_install(configuration):
    installer = Installer()
    assert runtime_launcher.ensure_runtime(configuration, policy(None), installer) == Path(
        configuration["initial_python"]
    )
    assert not installer.installed


@pytest.mark.parametrize("failure", ["busy", "integrity", "partial"])
def test_failed_update_never_executes_or_overwrites_old_runtime(configuration, failure):
    installer = Installer()
    root = Path(configuration["runtime_root"])
    root.mkdir()
    if failure == "busy":
        (root / "update.lock").mkdir()
    elif failure == "integrity":
        installer.release.sha256 = "0" * 64
    else:
        (root / "0.4.17").mkdir()
    with pytest.raises(runtime_launcher.LauncherError):
        runtime_launcher.ensure_runtime(configuration, policy(), installer)
    assert Path(configuration["initial_python"]).read_text() == "preserve running runtime"
    assert not installer.installed


@pytest.mark.parametrize("failure", ["install", "verify", "marker"])
def test_interrupted_owned_install_is_preserved_and_retry_succeeds(
    configuration, monkeypatch, failure
):
    installer = Installer()
    original_install = installer.install_runtime_bundle
    original_write = runtime_launcher.write_json
    failed = False

    def install(bundle, version):
        nonlocal failed
        original_install(bundle, version)
        if failure == "install" and not failed:
            failed = True
            raise OSError("synthetic transient install failure")

    def installed(runtime):
        nonlocal failed
        if failure == "verify" and installer.installed and not failed:
            failed = True
            return None
        return "0.4.17" if installer.runtime_python(runtime).exists() else None

    def write(path, value):
        nonlocal failed
        if failure == "marker" and path.name == "verified.json" and not failed:
            failed = True
            raise OSError("synthetic transient disk failure")
        return original_write(path, value)

    monkeypatch.setattr(installer, "install_runtime_bundle", install)
    monkeypatch.setattr(installer, "installed_runtime_version", installed)
    monkeypatch.setattr(runtime_launcher, "write_json", write)
    with pytest.raises(runtime_launcher.LauncherError):
        runtime_launcher.ensure_runtime(configuration, policy(), installer)
    result = runtime_launcher.ensure_runtime(configuration, policy(), installer)
    assert result.is_file() and len(installer.installed) == 2
    assert Path(configuration["initial_python"]).read_text() == "preserve running runtime"
    incomplete = list(Path(configuration["runtime_root"]).glob(".incomplete-*/installing.json"))
    assert len(incomplete) == 1


def test_killed_updater_lock_is_released_without_releasing_a_live_owner(tmp_path):
    script = """
import importlib.util, pathlib, sys, time
spec = importlib.util.spec_from_file_location('owned_launcher', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
with module.update_lock(pathlib.Path(sys.argv[2])):
    pathlib.Path(sys.argv[3]).write_text('locked')
    time.sleep(30)
"""
    marker = tmp_path / "ready"
    child = subprocess.Popen(
        [sys.executable, "-I", "-c", script, runtime_launcher.__file__, str(tmp_path), str(marker)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        import time

        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline and child.poll() is None:
            time.sleep(0.01)
        assert marker.exists(), "owned updater did not acquire lock"
        with pytest.raises(runtime_launcher.LauncherError, match="native_runtime_update_busy"):
            with runtime_launcher.update_lock(tmp_path):
                pytest.fail("another updater's live lock was released")
    finally:
        child.kill()
        child.wait(timeout=5)
    with runtime_launcher.update_lock(tmp_path):
        assert (tmp_path / "update.lock").is_file()


def test_an_older_updater_without_download_origin_keeps_the_stored_host():
    installer = Installer()
    assert runtime_launcher.update_origin(installer, "https://durumari.kr") == "https://durumari.kr"
    installer.download_origin = lambda origin: "https://app.durumari.cloud"
    assert runtime_launcher.update_origin(installer, "https://durumari.kr") == (
        "https://app.durumari.cloud"
    )
    older = SimpleNamespace(download=installer.download)
    assert runtime_launcher.update_origin(older, "https://durumari.kr") == "https://durumari.kr"


@pytest.mark.parametrize(
    ("host", "update_url"),
    [
        (None, "https://cdn.example.test/downloads"),
        ("https://evil.example.test", "https://evil.example.test/downloads"),
        ("https://preview.durumari.kr", "https://app.durumari.cloud/downloads/"),
    ],
)
def test_runtime_never_downloads_outside_the_stored_pair(configuration, host, update_url):
    # policy.update_url is not a download base.
    installer = Installer()
    urls = []
    original = installer.download

    def download(url, limit):
        urls.append(url)
        return original(url, limit)

    installer.download = download
    required = policy()
    object.__setattr__(required, "update_url", update_url)
    runtime_launcher.ensure_runtime(configuration, required, installer, host)
    assert urls == ["https://durumari.kr/downloads/latest.json", "https://durumari.kr/archive"]


def test_runtime_downloads_from_the_host_that_served_the_policy(configuration):
    # the policy came from the announced host, so its release does too.
    installer = Installer()
    urls = []
    original = installer.download

    def download(url, limit):
        urls.append(url)
        return original(url, limit)

    installer.download = download
    moved = parse_policy(
        {
            "native": {
                "latest_version": "0.4.17",
                "supported_min_version": "0.4.15",
                "required_native_version": "0.4.17",
                "update_url": "/downloads",
            }
        },
        "https://app.durumari.cloud",
    )
    runtime_launcher.ensure_runtime(configuration, moved, installer, "https://app.durumari.cloud")
    assert urls == [
        "https://app.durumari.cloud/downloads/latest.json",
        "https://app.durumari.cloud/archive",
    ]
    # Switching back reuses the verified runtime instead of asking for recovery.
    result = runtime_launcher.ensure_runtime(configuration, policy(), installer)
    assert result == Path(configuration["runtime_root"]) / "0.4.17/tools/durumari-mcp/bin/python3"
    assert len(installer.installed) == 1


def test_policy_falls_back_only_on_announced_host_download_failure():
    from durumari_mcp import update_policy

    installer = Installer()
    calls = []
    payload = json.dumps(
        {
            "native": {
                "latest_version": "0.4.17",
                "supported_min_version": "0.4.15",
                "required_native_version": None,
                "update_url": "/downloads",
            }
        }
    ).encode()

    def download(url, limit):
        calls.append(url)
        if url.startswith("https://app.durumari.cloud"):
            raise installer.ReleaseNetworkError("release_download_failed")
        return payload

    installer.download = download
    host, result = runtime_launcher.fetch_policy_with_fallback(
        update_policy, installer, "https://durumari.kr", "https://app.durumari.cloud"
    )
    assert host == "https://durumari.kr"
    assert result.update_url == "https://durumari.kr/downloads"
    assert len(calls) == 2


@pytest.mark.parametrize("bad", [b"not json", b"{}"])
def test_invalid_announced_policy_does_not_fall_back(bad):
    from durumari_mcp import update_policy

    installer = Installer()
    calls = []
    installer.download = lambda url, limit: calls.append(url) or bad
    with pytest.raises(update_policy.PolicyError):
        runtime_launcher.fetch_policy_with_fallback(
            update_policy, installer, "https://durumari.kr", "https://app.durumari.cloud"
        )
    assert len(calls) == 1


def test_required_runtime_update_falls_back_when_announced_download_is_unavailable(configuration):
    installer = Installer()
    calls = []

    def download(url, limit):
        calls.append(url)
        if url.startswith("https://app.durumari.cloud"):
            raise installer.ReleaseNetworkError("release_download_failed")
        return installer.data

    installer.download = download
    result = runtime_launcher.ensure_runtime(
        configuration, policy(), installer, "https://app.durumari.cloud"
    )
    assert result.exists()
    assert calls == [
        "https://app.durumari.cloud/downloads/latest.json",
        "https://durumari.kr/downloads/latest.json",
        "https://app.durumari.cloud/archive",
        "https://durumari.kr/archive",
    ]


def test_policy_falls_back_on_real_http_503(monkeypatch):
    import io
    import urllib.error

    from durumari_mcp import update_policy

    calls = []
    payload = json.dumps(
        {
            "native": {
                "latest_version": "0.4.17",
                "supported_min_version": "0.4.15",
                "required_native_version": None,
                "update_url": "/downloads",
            }
        }
    ).encode()

    def open_request(request, timeout):
        calls.append(request.full_url)
        if request.full_url.startswith("https://app.durumari.cloud"):
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
        return io.BytesIO(payload)

    monkeypatch.setattr(
        releases.urllib.request, "build_opener", lambda *args: SimpleNamespace(open=open_request)
    )
    host, result = runtime_launcher.fetch_policy_with_fallback(
        update_policy, releases, "https://durumari.kr", "https://app.durumari.cloud"
    )
    assert host == "https://durumari.kr"
    assert result.update_url == "https://durumari.kr/downloads"
    assert len(calls) == 2
