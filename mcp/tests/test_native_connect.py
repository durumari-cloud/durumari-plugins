"""The packaged entrypoint forwards only the v2 repository-scoped contract."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from plugin_layout import PLUGIN_SCRIPT


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    script = PLUGIN_SCRIPT
    spec = importlib.util.spec_from_file_location("native_connect", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "plugin"
    (root / "runtime").mkdir(parents=True)
    (root / "runtime/bundle.json").write_text(
        json.dumps(
            {
                "version": "0.4.0",
                "service": {"schema_version": 1, "gateway_origin": "https://durumari.example.com"},
            }
        )
    )
    monkeypatch.setattr(module, "PLUGIN_ROOT", root)
    return module


def record_verified_installs(plugin, monkeypatch, calls):
    def verified(wheel, action, uv):
        assert uv == "/synthetic/uv"
        assert wheel == Path("bundle.whl")
        return action(
            SimpleNamespace(
                install_runtime_bundle=lambda directory, version: calls.append(
                    [
                        "verified-install",
                        str(directory),
                        version,
                    ]
                )
            )
        )

    monkeypatch.setattr(plugin, "run_verified_installer", verified)


@pytest.mark.parametrize("old_runtime", [False, True])
def test_native_connect_installs_when_needed_then_forwards_only_v2(
    plugin, monkeypatch, tmp_path, old_runtime
):
    calls, versions = [], []
    record_verified_installs(plugin, monkeypatch, calls)
    monkeypatch.setattr(plugin.sys, "platform", "darwin")
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/synthetic/python")
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/synthetic/uv")
    monkeypatch.setattr(plugin, "verify_bundle", lambda _: (Path("bundle.whl"), Path("lock.txt")))

    def run(command, *, capture=False):
        calls.append(command)
        if command[-1] == "--version":
            versions.append(1)
            return "0.3.8" if old_runtime and len(versions) == 1 else "0.4.0"
        return 0

    monkeypatch.setattr(plugin, "run", run)
    args = plugin.parser().parse_args(
        ["connect", "--workspace", str(tmp_path), "--client", "codex"]
    )
    assert plugin.execute(args) == 0
    assert calls[-1] == [
        "/synthetic/python",
        "-I",
        "-m",
        "durumari_mcp",
        "context-connect",
        "--workspace",
        str(tmp_path),
        "--server",
        "https://durumari.example.com",
        "--agents",
        "codex",
    ]
    assert any(call[0] == "verified-install" for call in calls) is old_runtime
    assert not any("tool" in call for call in calls)
    assert not any(
        "project-connect" in call or "--allow-external-context" in call for call in calls
    )


@pytest.mark.parametrize(
    "flag", ["--password-login", "--allow-external-context", "--source-id", "--trust-workspace"]
)
def test_retired_authority_flags_fail_without_side_effects(plugin, monkeypatch, tmp_path, flag):
    monkeypatch.setattr(plugin, "runtime_python", lambda: pytest.fail("runtime touched"))
    with pytest.raises(plugin.SetupError, match="invalid_plugin_arguments"):
        plugin.parser().parse_args(["connect", "--workspace", str(tmp_path), flag])


def test_older_windows_runtime_is_not_replaced_while_host_may_use_it(plugin, monkeypatch, tmp_path):
    monkeypatch.setattr(plugin.sys, "platform", "win32")
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/synthetic/python")
    monkeypatch.setattr(plugin, "verify_bundle", lambda _: pytest.fail("live replacement"))
    monkeypatch.setattr(plugin, "run", lambda *args, **kwargs: "0.3.8")
    args = plugin.parser().parse_args(
        ["connect", "--workspace", str(tmp_path), "--client", "codex"]
    )
    with pytest.raises(plugin.SetupError, match="runtime_update_required_then_restart_host"):
        plugin.execute(args)


def test_first_connect_installs_the_verified_bundle(plugin, monkeypatch, tmp_path):
    calls, attempts = [], []
    record_verified_installs(plugin, monkeypatch, calls)

    def runtime():
        attempts.append(1)
        if len(attempts) == 1:
            raise plugin.SetupError("runtime_not_installed_run_plugin_install")
        return "/synthetic/python"

    monkeypatch.setattr(plugin, "runtime_python", runtime)
    monkeypatch.setattr(plugin, "uv_executable", lambda: "/synthetic/uv")
    monkeypatch.setattr(plugin, "verify_bundle", lambda _: (Path("bundle.whl"), Path("lock.txt")))
    monkeypatch.setattr(
        plugin,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or ("0.4.0" if command[-1] == "--version" else 0)
        ),
    )
    assert (
        plugin.execute(
            plugin.parser().parse_args(
                ["connect", "--workspace", str(tmp_path), "--client", "claude"]
            )
        )
        == 0
    )
    assert calls[0] == ["verified-install", str(plugin.PLUGIN_ROOT / "runtime"), "0.4.0"]
    assert calls[-1][:5] == [
        "/synthetic/python",
        "-I",
        "-m",
        "durumari_mcp",
        "context-connect",
    ]
    assert calls[-1][-2:] == ["--agents", "claude"]


@pytest.mark.parametrize(
    "origin",
    ["https://user:credential@example.com", "https://example.com/path", "http://untrusted.example"],
)
def test_invalid_package_origin_fails_before_runtime_mutation(
    plugin, monkeypatch, tmp_path, origin
):
    path = plugin.PLUGIN_ROOT / "runtime/bundle.json"
    d = json.loads(path.read_text())
    d["service"]["gateway_origin"] = origin
    path.write_text(json.dumps(d))
    monkeypatch.setattr(plugin, "runtime_python", lambda: pytest.fail("runtime touched"))
    with pytest.raises(plugin.SetupError, match="invalid_plugin_service"):
        plugin.execute(
            plugin.parser().parse_args(
                ["connect", "--workspace", str(tmp_path), "--client", "codex"]
            )
        )


def test_existing_manifest_does_not_borrow_a_new_package_project(plugin, monkeypatch, tmp_path):
    (tmp_path / "durumari.yaml").write_text(
        "server: https://selected.example\nproject_id: selected\n"
    )
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/synthetic/python")
    monkeypatch.setattr(
        plugin,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or ("0.4.0" if command[-1] == "--version" else 0)
        ),
    )
    plugin.execute(
        plugin.parser().parse_args(["connect", "--workspace", str(tmp_path), "--client", "cursor"])
    )
    assert "--server" not in calls[-1]
    assert "--project" not in calls[-1]
    assert calls[-1][-2:] == ["--agents", "cursor"]


def test_explicit_self_hosted_origin_forwards_without_any_secret(plugin, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/synthetic/python")
    monkeypatch.setattr(
        plugin,
        "run",
        lambda command, **kwargs: (
            calls.append(command) or ("0.4.0" if command[-1] == "--version" else 0)
        ),
    )
    plugin.execute(
        plugin.parser().parse_args(
            [
                "connect",
                "--workspace",
                str(tmp_path),
                "--client",
                "gemini",
                "--server",
                "https://self-hosted.example",
            ]
        )
    )
    assert calls[-1][-4:] == ["--server", "https://self-hosted.example", "--agents", "gemini"]


def test_interrupt_keeps_connection_and_reports_only_safe_cancellation(plugin, monkeypatch, capsys):
    monkeypatch.setattr(plugin, "execute", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(plugin.sys, "argv", ["plugin", "install"])
    assert plugin.main() == 130
    assert "Traceback" not in capsys.readouterr().err


@pytest.mark.parametrize(
    "origin",
    ["https://user:credential@example.com", "https://example.com/path", "http://untrusted.example"],
)
def test_invalid_explicit_origin_fails_before_runtime_mutation(
    plugin, monkeypatch, tmp_path, origin
):
    monkeypatch.setattr(plugin, "runtime_python", lambda: pytest.fail("runtime touched"))
    with pytest.raises(plugin.SetupError, match="invalid_plugin_service"):
        plugin.execute(
            plugin.parser().parse_args(
                ["connect", "--workspace", str(tmp_path), "--server", origin]
            )
        )
