"""Environment variables the plugin itself reads, defined once.

Plugin variables live under `DURUMARI_MCP_`; `secret run` reserves that prefix so a shared
secret cannot change how the plugin or its hooks load code or record state. The names
without the prefix are the earlier spellings: readers still accept them (already installed
hooks, launchers and documented install commands keep working) and `secret run` keeps
rejecting them for the compatibility period. Backend `DURUMARI_*` settings are not plugin
variables and stay injectable.
"""

import os
from collections.abc import Mapping

PREFIX = "DURUMARI_MCP_"

UV_EXECUTABLE = PREFIX + "UV_EXECUTABLE"
EXECUTION_POLICY = PREFIX + "EXECUTION_POLICY"
SESSION_ID = PREFIX + "SESSION_ID"
CONTEXT_DEPTH = PREFIX + "CONTEXT_DEPTH"
CLIENT = PREFIX + "CLIENT"
INSTALLER = PREFIX + "INSTALLER"
PLUGIN_INSTALLER_DELEGATED = PREFIX + "PLUGIN_INSTALLER_DELEGATED"
SERVER = PREFIX + "SERVER"

# Earlier names, exact. Remove together with the fallback once no old hook can read them.
LEGACY = {
    UV_EXECUTABLE: "DURUMARI_UV_EXECUTABLE",
    EXECUTION_POLICY: "DURUMARI_EXECUTION_POLICY",
    SESSION_ID: "DURUMARI_SESSION_ID",
    CONTEXT_DEPTH: "DURUMARI_CONTEXT_DEPTH",
    CLIENT: "DURUMARI_CLIENT",
    INSTALLER: "DURUMARI_INSTALLER",
    PLUGIN_INSTALLER_DELEGATED: "DURUMARI_PLUGIN_INSTALLER_DELEGATED",
    SERVER: "DURUMARI_SERVER",
}
LEGACY_NAMES = frozenset(LEGACY.values())


def reserved(name: str) -> bool:
    upper = name.upper()
    return upper.startswith(PREFIX) or upper in LEGACY_NAMES


def get(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    """The new name wins; the earlier name answers only when the new one is unset."""
    source = os.environ if environ is None else environ
    value = source.get(name)
    return value if value is not None else source.get(LEGACY[name])


def pop(name: str) -> str | None:
    """Remove both spellings from this process; return the new name's value, else the old."""
    old = os.environ.pop(LEGACY[name], None)
    new = os.environ.pop(name, None)
    return new if new is not None else old
