"""Independent stdlib launcher: verify policy before importing any native runtime."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Never, cast
from uuid import uuid4

FILES = {"runtime_launcher.py", "update_policy.py", "releases.py"}


class LauncherError(Exception):
    """Fixed recovery codes only; never print subprocess output or credentials."""


def safe_path(path: Path) -> None:
    for part in [*reversed(path.parents), path]:
        junction = getattr(part, "is_junction", None)
        if part.is_symlink() or (junction is not None and junction()):
            raise LauncherError("unsafe_native_launcher_path")


def read_json(path: Path) -> dict[str, Any]:
    safe_path(path)
    try:
        if path.stat().st_size > 16384:
            raise ValueError
        value = json.loads(path.read_bytes())
        if not isinstance(value, dict):
            raise ValueError
        return value
    except (OSError, ValueError):
        raise LauncherError("native_launcher_recovery_required") from None


def write_json(path: Path, value: dict[str, Any]) -> None:
    safe_path(path)
    descriptor, name = tempfile.mkstemp(prefix=".durumari-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, separators=(",", ":")))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def probe_version(python: Path) -> str:
    if not python.is_absolute() or not python.is_file():
        raise LauncherError("native_runtime_unavailable")
    try:
        result = subprocess.run(
            [
                str(python),
                "-I",
                "-c",
                "from importlib.metadata import version; print(version('durumari-mcp'))",
            ],
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        value = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,6}", value):
            raise ValueError
        return value
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise LauncherError("native_runtime_version_unknown") from None


@contextmanager
def update_lock(root: Path) -> Iterator[None]:
    """Keep one stable lock inode; the OS releases ownership on process exit."""
    path = root / "update.lock"
    safe_path(path)
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError:
        raise LauncherError("native_runtime_update_busy") from None
    with os.fdopen(descriptor, "r+b", buffering=0) as stream:
        try:
            if os.name == "nt":
                import msvcrt

                windows_lock: Any = msvcrt
                # Byte locks beyond EOF are supported; never write before ownership.
                stream.seek(0)
                windows_lock.locking(descriptor, windows_lock.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise LauncherError("native_runtime_update_busy") from None
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                windows_lock.locking(descriptor, windows_lock.LK_UNLCK, 1)
            else:
                fcntl.flock(descriptor, fcntl.LOCK_UN)


def ensure_runtime(
    configuration: dict[str, Any], policy: Any, updater: Any, host: str | None = None
) -> Path:
    root = Path(configuration["runtime_root"])
    safe_path(root)
    if not root.is_absolute():
        raise LauncherError("unsafe_native_launcher_path")
    active_path = root / "active.json"
    safe_path(active_path)
    if active_path.exists():
        active = read_json(active_path)
        if not isinstance(active.get("version"), str) or not re.fullmatch(
            r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,6}", active["version"]
        ):
            raise LauncherError("native_runtime_recovery_required")
        runtime = root / str(active.get("version")) / "tools/durumari-mcp"
        python = updater.runtime_python(runtime)
        safe_path(python.parent)
        if (
            active.get("origin") != configuration["origin"]
            or active.get("python") != str(python)
            or updater.installed_runtime_version(runtime) != active.get("version")
        ):
            raise LauncherError("native_runtime_recovery_required")
        current = active["version"]
    else:
        python = Path(configuration["initial_python"])
        current = probe_version(python)
    if not policy.needs_update(current):
        return Path(python)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with update_lock(root):
        return update_runtime(configuration, policy, updater, root, active_path, host)


def update_runtime(
    configuration: dict[str, Any],
    policy: Any,
    updater: Any,
    root: Path,
    active_path: Path,
    host: str | None = None,
) -> Path:
    previous_env = {
        key: os.environ.get(key)
        for key in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "DURUMARI_MCP_UV_EXECUTABLE")
    }
    try:
        origin = updater.release_origin(configuration["origin"])
        # Download from the host that served the policy, never from policy.update_url, and
        # only inside the stored origin's pair; anything else keeps the stored origin.
        base = origin
        same_server = getattr(updater, "same_server", None)
        if host and callable(same_server) and same_server(host, origin):
            base = updater.release_origin(host)
        release = updater.parse_release(
            download_with_fallback(updater, base, origin, "/downloads/latest.json", 32768)
        )
        if release.version != policy.latest_version:
            raise LauncherError("native_release_policy_mismatch")
        version_root = root / release.version
        safe_path(version_root)
        runtime = version_root / "tools/durumari-mcp"
        python = updater.runtime_python(runtime)
        safe_path(python.parent)
        marker = version_root / "verified.json"
        installing = {
            "schema_version": 1,
            "origin": origin,
            "version": release.version,
            "sha256": release.sha256,
        }
        if version_root.exists() and not marker.exists():
            # Only our exact unfinished installation can be moved out of the way.
            # Preserve it for recovery rather than deleting unknown contents.
            if (
                not (version_root / "installing.json").exists()
                or read_json(version_root / "installing.json") != installing
            ):
                raise LauncherError("native_runtime_recovery_required")
            os.replace(version_root, root / (".incomplete-" + release.version + "-" + uuid4().hex))
        if version_root.exists():
            if (
                not marker.exists()
                or read_json(marker).get("sha256") != release.sha256
                or updater.installed_runtime_version(runtime) != release.version
            ):
                raise LauncherError("native_runtime_recovery_required")
        else:
            data = download_with_fallback(
                updater, base, origin, release.path, getattr(updater, "MAX_ARCHIVE", 8388608)
            )
            if len(data) != release.size or hashlib.sha256(data).hexdigest() != release.sha256:
                raise LauncherError("release_archive_integrity_failed")
            with tempfile.TemporaryDirectory(prefix="stage-", dir=root) as temporary:
                catalog = updater.extract_catalog(data, Path(temporary), release.version)
                bundle = catalog / "plugins/durumari-agent/runtime"
                metadata = read_json(bundle / "bundle.json")
                if not updater.same_server(updater.parse_service(metadata["service"]), origin):
                    raise LauncherError("release_service_origin_mismatch")
                # Publish the ownership receipt atomically before installation starts.
                with tempfile.TemporaryDirectory(prefix="install-", dir=root) as staged:
                    staging = Path(staged)
                    write_json(staging / "installing.json", installing)
                    os.replace(staging, version_root)
                os.environ["UV_TOOL_DIR"] = str(version_root / "tools")
                os.environ["UV_TOOL_BIN_DIR"] = str(version_root / "bin")
                os.environ["DURUMARI_MCP_UV_EXECUTABLE"] = configuration["uv"]
                # The existing verified installer owns its lock/snapshot/rollback.
                # This destination has never hosted a process; existing venvs stay intact.
                updater.install_runtime_bundle(bundle, release.version)
                if updater.installed_runtime_version(runtime) != release.version:
                    raise LauncherError("native_runtime_version_unknown")
                write_json(marker, {"version": release.version, "sha256": release.sha256})
        if policy.needs_update(release.version):
            raise LauncherError("native_runtime_version_unknown")
        write_json(
            active_path, {"origin": origin, "version": release.version, "python": str(python)}
        )
        return Path(python)
    except LauncherError:
        raise
    except Exception as error:
        code = str(error)
        raise LauncherError(
            code if re.fullmatch(r"[a-z_]{1,100}", code) else "native_runtime_update_failed"
        ) from None
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def load_components(directory: Path) -> tuple[dict[str, Any], Any, Any]:
    configuration = read_json(directory / "launcher.json")
    hashes = configuration.get("files")
    if (
        configuration.get("schema_version") != 1
        or not isinstance(hashes, dict)
        or set(hashes) != FILES
    ):
        raise LauncherError("native_launcher_integrity_failed")
    for name, expected in hashes.items():
        path = directory / name
        safe_path(path)
        if (
            not isinstance(expected, str)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)
            or path.stat().st_size > 262144
            or hashlib.sha256(path.read_bytes()).hexdigest() != expected
        ):
            raise LauncherError("native_launcher_integrity_failed")

    def load(name: str, file: str) -> Any:
        spec = importlib.util.spec_from_file_location(name, directory / file)
        if spec is None or spec.loader is None:
            raise LauncherError("native_launcher_integrity_failed")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module

    return (
        configuration,
        load("durumari_launcher_policy", "update_policy.py"),
        load("durumari_launcher_updater", "releases.py"),
    )


def windows() -> bool:
    return os.name == "nt"


def serve(python: Path, client: str, profile: str) -> None:
    command = [
        str(python), "-I", "-m", "durumari_mcp", "account-serve",
        "--client", client, "--profile", profile,
    ]
    if windows():
        # os.execv on Windows starts a new process and ends this one, so the host
        # would see its MCP server exit. Run the child on the inherited stdio and
        # pass its exit code through instead.
        raise SystemExit(subprocess.run(command, check=False).returncode)
    os.execv(str(python), command)


def update_origin(updater: Any, origin: str) -> str:
    # An updater from 0.4.47 or earlier has no download_origin; it keeps the stored host.
    follow = getattr(updater, "download_origin", None)
    return str(follow(origin)) if callable(follow) else origin


def download_with_fallback(updater: Any, base: str, origin: str, path: str, limit: int) -> bytes:
    try:
        return cast(bytes, updater.download(base + path, limit))
    except updater.ReleaseError as error:
        network_error = getattr(updater, "ReleaseNetworkError", None)
        if base == origin or network_error is None or not isinstance(error, network_error):
            raise
        return cast(bytes, updater.download(origin + path, limit))


def fetch_policy_with_fallback(
    policy_module: Any, updater: Any, origin: str, host: str
) -> tuple[str, Any]:
    network_failure = False

    def tracked_download(url: str, limit: int) -> bytes:
        nonlocal network_failure
        try:
            return cast(bytes, updater.download(url, limit))
        except updater.ReleaseError as error:
            network_error = getattr(updater, "ReleaseNetworkError", None)
            network_failure = network_error is not None and isinstance(error, network_error)
            raise

    try:
        return host, policy_module.fetch_policy(host, tracked_download)
    except policy_module.PolicyError:
        if host == origin or not network_failure:
            raise
        return origin, policy_module.fetch_policy(origin, updater.download)


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise LauncherError("invalid_native_launcher_arguments")


def main() -> None:
    try:
        parser = Parser()
        parser.add_argument("--server", required=True)
        parser.add_argument("--client", choices=["codex", "claude-code", "cursor"], required=True)
        parser.add_argument("--profile", required=True)
        parser.add_argument("--prepare-only", action="store_true")
        args = parser.parse_args()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", args.profile):
            raise LauncherError("invalid_native_launcher_arguments")
        configuration, policy_module, updater = load_components(Path(__file__).absolute().parent)
        origin = policy_module.trusted_origin(args.server)
        if configuration.get("origin") != origin:
            raise LauncherError("native_launcher_origin_changed")
        host = update_origin(updater, origin)
        host, policy = fetch_policy_with_fallback(policy_module, updater, origin, host)
        if policy.capability == "legacy_advisory":
            print(
                json.dumps({"warning": "native_required_policy_unsupported", **policy.receipt()}),
                file=sys.stderr,
            )
        python = ensure_runtime(configuration, policy, updater, host)
        if args.prepare_only:
            print(
                json.dumps(
                    {"state": "ready", "version": probe_version(python), "policy": policy.receipt()}
                )
            )
            return
        serve(python, args.client, args.profile)
    except Exception as error:
        code = str(error)
        if not re.fullmatch(r"[a-z_]{1,100}", code):
            code = "native_launcher_recovery_required"
        print(
            json.dumps(
                {
                    "error": {"code": code},
                    "next_action": "retry_account_setup_or_update_after_recovery",
                }
            ),
            file=sys.stderr,
        )
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
