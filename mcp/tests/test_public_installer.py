"""The public first-install script runs before anything else is installed."""

import subprocess
import sys
from pathlib import Path

INSTALLER = Path(__file__).parents[1] / "src/durumari_mcp/releases.py"


def test_public_installer_runs_with_only_the_standard_library():
    # -I ignores user environment; -S skips site-packages, so a third-party
    # import would fail exactly as on a new user's machine.
    result = subprocess.run(
        [sys.executable, "-I", "-S", str(INSTALLER), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "{install,update}" in result.stdout
