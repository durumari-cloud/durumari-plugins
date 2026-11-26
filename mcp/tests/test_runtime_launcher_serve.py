"""The launcher hands stdio to account-serve without the host seeing an early exit."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from durumari_mcp import runtime_launcher

PYTHON = Path("/runtime/bin/python")
COMMAND = [
    str(PYTHON), "-I", "-m", "durumari_mcp", "account-serve",
    "--client", "codex", "--profile", "personal",
]


def test_posix_replaces_the_launcher_process(monkeypatch):
    calls = []
    monkeypatch.setattr(runtime_launcher, "windows", lambda: False)
    monkeypatch.setattr(runtime_launcher.os, "execv", lambda *args: calls.append(args))
    monkeypatch.setattr(
        runtime_launcher.subprocess, "run", lambda *a, **k: pytest.fail("no child on POSIX")
    )
    runtime_launcher.serve(PYTHON, "codex", "personal")
    assert calls == [(str(PYTHON), COMMAND)]


@pytest.mark.parametrize("code", [0, 3])
def test_windows_runs_a_child_on_inherited_stdio_and_returns_its_code(monkeypatch, code):
    calls = []
    monkeypatch.setattr(runtime_launcher, "windows", lambda: True)
    monkeypatch.setattr(
        runtime_launcher.os, "execv", lambda *a: pytest.fail("execv ends the host's server")
    )

    def run(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=code)

    monkeypatch.setattr(runtime_launcher.subprocess, "run", run)
    with pytest.raises(SystemExit) as exited:
        runtime_launcher.serve(PYTHON, "codex", "personal")
    assert exited.value.code == code
    # No stdin/stdout/stderr redirection: the child inherits the host's MCP pipes.
    assert calls == [(COMMAND, {"check": False})]
