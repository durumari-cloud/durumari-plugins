"""The host wrapper must never import project-local code during account setup."""

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts/durumari_plugin.py"
spec = importlib.util.spec_from_file_location("account_setup_plugin", SCRIPT)
plugin = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plugin)


def test_account_setup_probes_and_executes_isolated_runtime(tmp_path, monkeypatch):
    marker = tmp_path / "shadow-executed"
    (tmp_path / "durumari_mcp.py").write_text(
        "from pathlib import Path\nPath("
        + repr(str(marker))
        + ").write_text('unsafe')\n"
        "print('account-setup')\n"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(plugin, "runtime_python", lambda: sys.executable)
    monkeypatch.setattr(plugin, "bundled_server", lambda: "https://durumari.kr")
    original_run = plugin.run
    monkeypatch.setattr(
        plugin,
        "run",
        lambda command, **kwargs: (
            original_run(command, **kwargs) if kwargs.get("capture") else 0
        ),
    )
    args = plugin.parser().parse_args(
        ["account-setup", "--client", "codex", "--no-browser"]
    )
    # Installed test runtime can return a recovery outcome; the project module may never run.
    try:
        plugin.execute(args)
    except plugin.SetupError:
        pass
    assert not marker.exists()


def test_account_setup_preserves_optional_default_and_forwards_only_explicit_profile(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/protected/runtime/python")
    monkeypatch.setattr(plugin, "bundled_server", lambda: "https://durumari.kr")

    def run(command, **kwargs):
        calls.append(command)
        return "account-setup" if kwargs.get("capture") else 0

    monkeypatch.setattr(plugin, "run", run)
    for extra in ([], ["--profile", "my-account"]):
        assert (
            plugin.execute(
                plugin.parser().parse_args(
                    ["account-setup", "--client", "cursor", *extra]
                )
            )
            == 0
        )
        assert calls[-1][:4] == [
            "/protected/runtime/python",
            "-I",
            "-m",
            "durumari_mcp",
        ]
        assert ("--profile" in calls[-1]) == bool(extra)


def test_account_setup_old_runtime_has_fixed_update_recovery(monkeypatch):
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/protected/runtime/python")
    monkeypatch.setattr(plugin, "run", lambda *args, **kwargs: "legacy-only")
    with pytest.raises(
        plugin.SetupError, match="account_runtime_update_required_then_restart_host"
    ):
        plugin.execute(
            plugin.parser().parse_args(["account-setup", "--client", "codex"])
        )


@pytest.mark.parametrize(
    "client,expected",
    [("codex", "codex"), ("claude", "claude-code"), ("cursor", "cursor")],
)
def test_connect_without_workspace_defaults_to_account(client, expected, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/protected/python")
    monkeypatch.setattr(plugin, "bundled_server", lambda: "https://durumari.kr")
    monkeypatch.setattr(
        plugin,
        "run",
        lambda cmd, **kw: (
            "account-setup" if kw.get("capture") else calls.append(cmd) or 0
        ),
    )
    assert (
        plugin.execute(plugin.parser().parse_args(["connect", "--client", client])) == 0
    )
    assert calls[0][4:] == [
        "account-setup",
        "--client",
        expected,
        "--server",
        "https://durumari.kr",
    ]


def test_folder_target_is_never_silently_promoted_to_account(monkeypatch):
    monkeypatch.setattr(
        plugin, "runtime_python", lambda: pytest.fail("must reject before execution")
    )
    with pytest.raises(
        plugin.SetupError, match="explicit_workspace_required_for_folder_connection"
    ):
        plugin.execute(
            plugin.parser().parse_args(
                ["connect", "--client", "codex", "--project", "project"]
            )
        )


def test_unsupported_host_needs_explicit_folder_connection():
    with pytest.raises(
        plugin.SetupError, match="account_setup_client_unsupported_use_explicit_folder"
    ):
        plugin.execute(plugin.parser().parse_args(["connect", "--client", "gemini"]))


def test_migration_wrapper_preserves_explicit_path_as_one_argument(monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "runtime_python", lambda: "/protected/python")
    monkeypatch.setattr(
        plugin,
        "run",
        lambda cmd, **kw: (
            "account-migrate" if kw.get("capture") else calls.append(cmd) or 0
        ),
    )
    args = plugin.parser().parse_args(
        [
            "account-migrate",
            "plan",
            "--client",
            "codex",
            "--profile",
            "personal",
            "--workspace",
            "/selected/work folder",
        ]
    )
    assert plugin.execute(args) == 0
    assert calls[-1][-2:] == ["--workspace", "/selected/work folder"]
