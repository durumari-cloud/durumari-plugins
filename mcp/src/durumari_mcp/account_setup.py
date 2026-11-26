"""One owned user MCP entry; folder bindings and unrelated settings stay intact."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any

from durumari_mcp import releases, runtime_launcher, update_policy
from durumari_mcp.config_json import load as load_json
from durumari_mcp.config_json import render as render_json
from durumari_mcp.credentials import AdapterError, Profile, profile_name
from durumari_mcp.local_upgrade import backup_path, save_backup, user_data_directory
from durumari_mcp.project_outbox import process_lock
from durumari_mcp.server_aliases import same_server
from durumari_mcp.version_info import runtime_version

SERVER = "durumari_account"
CLIENTS = {"codex", "claude-code", "cursor"}


def checked(path: Path) -> Path:
    if not path.is_absolute():
        raise AdapterError("unsafe_account_setup_path")
    try:
        runtime_launcher.safe_path(path)
    except runtime_launcher.LauncherError:
        raise AdapterError("unsafe_account_setup_path") from None
    return path


def atomic(path: Path, data: bytes, *, expected: str | None = None) -> None:
    checked(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, name = tempfile.mkstemp(prefix=".durumari-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        checked(path)
        if expected is not None and digest(path.read_bytes() if path.exists() else b"") != expected:
            raise AdapterError("account_setup_configuration_changed_retry")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def paths(client: str, home: Path, data: Path) -> tuple[Path, Path]:
    if client not in CLIENTS:
        raise AdapterError("account_setup_client_unsupported")
    config = (
        home
        / {
            "codex": ".codex/config.toml",
            "claude-code": ".claude.json",
            "cursor": ".cursor/mcp.json",
        }[client]
    )
    return checked(config), checked(data / "account-setup" / client)


def read_configuration(path: Path, client: str) -> tuple[bytes, dict[str, Any], dict[str, Any]]:
    checked(path)
    try:
        previous = path.read_bytes() if path.exists() else b""
        if len(previous) > 2_000_000:
            raise ValueError
        text = previous.decode("utf-8")
        parsed = tomllib.loads(text) if client == "codex" else load_json(text or "{}")
        servers = parsed.get("mcp_servers" if client == "codex" else "mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError
        return previous, parsed, servers
    except (OSError, ValueError, UnicodeError):
        raise AdapterError("invalid_agent_configuration") from None


def record(path: Path) -> dict[str, Any] | None:
    checked(path)
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 16384:
            raise ValueError
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError
        return value
    except (OSError, ValueError):
        raise AdapterError("account_setup_recovery_required") from None


def owned(entry: Any, saved: dict[str, Any] | None, client: str, profile: str, origin: str) -> bool:
    return saved is not None and (
        saved.get("client"),
        saved.get("profile"),
        saved.get("server"),
        saved.get("entry"),
    ) == (client, profile, origin, entry)


def registered_origin(directory: Path, client: str, profile: str, origin: str) -> str:
    """Keep the host a registration was made with when asked for its moved pair."""
    for name in ("pending.json", "installed.json"):
        saved = record(directory / name)
        if (
            saved is not None
            and (saved.get("client"), saved.get("profile")) == (client, profile)
            and isinstance(saved.get("server"), str)
            and same_server(saved["server"], origin)
        ):
            return str(saved["server"])
    return origin


def preflight(
    client: str, profile: str, server: str, *, home: Path | None = None, data: Path | None = None
) -> None:
    home, data = home or Path.home(), data or user_data_directory()
    profile_name(profile)
    try:
        origin = Profile.trusted_origin(server)
    except ValueError:
        raise AdapterError("invalid_gateway_origin") from None
    path, directory = paths(client, home, data)
    origin = registered_origin(directory, client, profile, origin)
    _, _, servers = read_configuration(path, client)
    saved = record(directory / "installed.json")
    pending = record(directory / "pending.json")
    entry = servers.get(SERVER)
    if entry is not None and not (
        owned(entry, saved, client, profile, origin)
        or owned(entry, pending, client, profile, origin)
    ):
        raise AdapterError("account_mcp_configuration_conflict")
    if not shutil.which("uv"):
        raise AdapterError("uv_required_install_from_astral_documentation")


def launcher_command(*, client: str, profile: str, server: str, data: Path) -> list[str]:
    uv = shutil.which("uv")
    if not uv:
        raise AdapterError("uv_required_install_from_astral_documentation")
    uv = str(Path(uv).absolute())
    sources = {
        "runtime_launcher.py": Path(runtime_launcher.__file__).read_bytes(),
        "update_policy.py": Path(update_policy.__file__).read_bytes(),
        "releases.py": Path(releases.__file__).read_bytes(),
    }
    hashes = {name: digest(value) for name, value in sources.items()}
    identity = digest(
        json.dumps([server, sys.executable, runtime_version(), hashes], sort_keys=True).encode()
    )[:24]
    directory = checked(data / "account-launchers" / identity)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name, value in sources.items():
        path = checked(directory / name)
        if path.exists() and path.read_bytes() != value:
            raise AdapterError("native_launcher_integrity_failed")
        if not path.exists():
            atomic(path, value)
    configuration = {
        "schema_version": 1,
        "files": hashes,
        "origin": server,
        "uv": uv,
        "initial_python": sys.executable,
        "initial_version": runtime_version(),
        "runtime_root": str(checked(data / "account-runtimes" / digest(server.encode())[:20])),
    }
    path = checked(directory / "launcher.json")
    desired = json.dumps(configuration, sort_keys=True).encode()
    if path.exists() and path.read_bytes() != desired:
        raise AdapterError("native_launcher_integrity_failed")
    if not path.exists():
        atomic(path, desired)
    return [
        uv,
        "run",
        "--no-project",
        "--python",
        "3.12",
        "--managed-python",
        "python",
        "-I",
        str(directory / "runtime_launcher.py"),
        "--server",
        server,
        "--client",
        client,
        "--profile",
        profile,
    ]


def prepare_runtime(command: list[str]) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command + ["--prepare-only"],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
            check=False,
        )
        if result.returncode:
            code = "native_launcher_recovery_required"
            for line in reversed(result.stderr[-16384:].splitlines()):
                try:
                    value = json.loads(line)
                    candidate = value.get("error", {}).get("code")
                    if isinstance(candidate, str) and re.fullmatch(r"[a-z_]{1,100}", candidate):
                        code = candidate
                        break
                except (ValueError, AttributeError):
                    pass
            raise AdapterError(code)
        if len(result.stdout.encode()) > 16384:
            raise ValueError
        value = json.loads(result.stdout)
        if not isinstance(value, dict) or value.get("state") != "ready":
            raise ValueError
        return value
    except AdapterError:
        raise
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise AdapterError("native_launcher_recovery_required") from None


def configure(
    client: str, profile: str, server: str, *, home: Path | None = None, data: Path | None = None
) -> dict[str, Any]:
    home, data = home or Path.home(), data or user_data_directory()
    try:
        origin = Profile.trusted_origin(server)
    except ValueError:
        raise AdapterError("invalid_gateway_origin") from None
    path, directory = paths(client, home, data)
    with process_lock(directory / "lock"):
        origin = registered_origin(directory, client, profile, origin)
        preflight(client, profile, origin, home=home, data=data)
        previous, parsed, servers = read_configuration(path, client)
        command = launcher_command(client=client, profile=profile, server=origin, data=data)
        ready = prepare_runtime(command)
        entry: dict[str, Any] = {"command": command[0], "args": command[1:]}
        if client == "codex":
            entry["startup_timeout_sec"] = 300
            current = previous.decode("utf-8")
            if SERVER in servers:
                current, count = re.subn(
                    r"(?ms)^\[mcp_servers\.durumari_account\][^\n]*\n.*?(?=^\[|\Z)", "", current
                )
                if count != 1:
                    raise AdapterError("account_mcp_configuration_conflict")
            desired = (
                current.rstrip()
                + "\n\n[mcp_servers.durumari_account]\n"
                + "\n".join(key + " = " + json.dumps(value) for key, value in entry.items())
                + "\n"
            ).encode()
            if tomllib.loads(desired.decode()).get("mcp_servers", {}).get(SERVER) != entry:
                raise AdapterError("invalid_agent_configuration")
        else:
            entry["type"] = "stdio"
            parsed.setdefault("mcpServers", {})[SERVER] = entry
            desired = render_json(previous.decode() or "{}\n", parsed).encode()
        installation = {
            "schema_version": 1,
            "client": client,
            "profile": profile,
            "server": origin,
            "entry": entry,
        }
        pending_path = directory / "pending.json"
        pending = record(pending_path)
        if pending is not None:
            if not owned(entry, pending, client, profile, origin) or digest(previous) not in {
                pending.get("before_sha256"),
                pending.get("desired_sha256"),
            }:
                raise AdapterError("account_setup_recovery_required")
        if previous != desired:
            atomic(
                pending_path,
                json.dumps(
                    {
                        **installation,
                        "before_sha256": digest(previous),
                        "desired_sha256": digest(desired),
                    }
                ).encode(),
            )
            if previous:
                checked(backup_path(path.parent, path.name, data=data))
                save_backup(path.parent, path.name, previous, data=data)
            atomic(path, desired, expected=digest(previous))
        atomic(directory / "installed.json", json.dumps(installation).encode())
        if pending_path.exists():
            pending_path.unlink()
        return {
            "state": "configured",
            "client": client,
            "profile": profile,
            "server": origin,
            "config_path": str(path),
            "restart_required": True,
            "runtime": ready,
        }
