"""Frozen copy of the 0.4.0-0.4.4 public updater (dev 87d4a81 through 2d648e0). Do not edit."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Never, cast
from uuid import uuid4

MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 24 * 1024 * 1024
VERSION = r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}"
CATALOG = "https://github.com/MyKnow/malmoa-plugins.git"
# Exact pre-rename host identity; project data and credential stores stay intact.
LEGACY_PLUGIN = "malmoa@malmoa"


class ReleaseError(Exception):
    """Only fixed, safe error codes cross the CLI boundary."""

    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.details = details or {}
        self.diagnostic_path: str | None = None


def failure_details(error: ReleaseError | OSError) -> dict[str, Any]:
    if isinstance(error, ReleaseError):
        return {"error": str(error), **error.details}
    return {
        "error": "release_filesystem_error",
        "errno": error.errno,
        "winerror": getattr(error, "winerror", None),
    }


def write_failure_report(directory: Path, report: dict[str, Any], debug: bool) -> str | None:
    # Raw subprocess output is opt-in: host tools can print credentials or private paths.
    payload = dict(report)
    for key in ("failure", "rollback_failure"):
        if key in payload:
            detail = dict(payload[key])
            if not debug:
                detail.pop("stdout", None)
                detail.pop("stderr", None)
            payload[key] = detail
    try:
        path = directory / "install-diagnostic.json"
        temporary = directory / "install-diagnostic.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            stream.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        temporary.replace(path)
        return str(path)
    except OSError:
        # Diagnostics must never replace the original failure or prevent rollback.
        return None


@dataclass(frozen=True)
class Release:
    version: str
    path: str
    sha256: str
    size: int


def release_origin(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or parsed.port not in {None, 443}
        ):
            raise ValueError
        if not re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname):
            raise ValueError
        return f"https://{parsed.hostname.lower()}"
    except (ValueError, TypeError):
        raise ReleaseError("release_https_origin_required") from None


def parse_service(value: Any) -> str:
    """Packaged API origin; public HTTPS or explicit numeric-loopback development."""
    try:
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "gateway_origin"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
        ):
            raise ValueError
        origin = value["gateway_origin"]
        if not isinstance(origin, str) or origin != origin.strip():
            raise ValueError
        parsed = urllib.parse.urlsplit(origin)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in origin)
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or not (
                parsed.scheme == "https"
                or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"})
            )
        ):
            raise ValueError
        return origin.rstrip("/")
    except (TypeError, ValueError, KeyError):
        raise ReleaseError("invalid_plugin_service") from None


def parse_release(data: bytes) -> Release:
    try:
        item = json.loads(data)
        version, archive = item["version"], item["archive"]
        if (
            type(item["schema_version"]) is not int
            or item["schema_version"] != 1
            or not isinstance(version, str)
            or not re.fullmatch(VERSION, version)
            or archive["path"] != f"/downloads/releases/{version}/durumari-plugins.zip"
            or not isinstance(archive["sha256"], str)
            or not re.fullmatch(r"[a-f0-9]{64}", archive["sha256"])
            or type(archive["size"]) is not int
            or not 0 < archive["size"] <= MAX_ARCHIVE
            or item["catalog"] != {"name": "durumari", "repository": CATALOG}
        ):
            raise ValueError
        return Release(version, archive["path"], archive["sha256"], archive["size"])
    except (ValueError, KeyError, TypeError):
        raise ReleaseError("invalid_release_metadata") from None


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> urllib.request.Request | None:
        old, new = urllib.parse.urlsplit(req.full_url), urllib.parse.urlsplit(newurl)
        if (new.scheme, new.netloc) != (old.scheme, old.netloc) or new.username or new.password:
            raise ReleaseError("release_redirect_rejected")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url: str, limit: int) -> bytes:
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "DURUMARI-Release/1"})
        with urllib.request.build_opener(SameOriginRedirect()).open(
            request, timeout=30
        ) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ReleaseError("release_download_too_large")
        return cast(bytes, data)
    except (OSError, urllib.error.URLError):
        raise ReleaseError("release_download_failed") from None


def allowed_member(path: str) -> bool:
    if path in {"README.md", ".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"}:
        return True
    prefix = "plugins/durumari-agent/"
    if not path.startswith(prefix):
        return False
    name = path[len(prefix) :]
    if name in {
        "README.md",
        "plugin.json",
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        "scripts/durumari_plugin.py",
        "runtime/requirements.txt",
        "runtime/bundle.json",
    }:
        return True
    return bool(
        re.fullmatch(r"skills/(connect|context|status|migrate|update)/SKILL\.md", name)
        or re.fullmatch(rf"runtime/durumari_mcp-{VERSION}-py3-none-any\.whl", name)
    )


def verify_runtime(directory: Path, version: str) -> tuple[Path, Path]:
    wheel = f"durumari_mcp-{version}-py3-none-any.whl"
    try:
        item = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
        if (
            item["schema_version"] != 1
            or item["package"] != "durumari-mcp"
            or item["version"] != version
            or set(item["files"]) != {wheel, "requirements.txt"}
        ):
            raise ValueError
        for name, digest in item["files"].items():
            path = directory / name
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
                raise ValueError
        if "service" in item or tuple(map(int, version.split("."))) >= (0, 2, 9):
            parse_service(item.get("service"))
        return directory / wheel, directory / "requirements.txt"
    except (ValueError, KeyError, TypeError, OSError):
        raise ReleaseError("runtime_integrity_failed") from None


def extract_catalog(data: bytes, target: Path, version: str) -> Path:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            members, names = archive.infolist(), set()
            if len(members) > 64 or sum(m.file_size for m in members) > MAX_EXPANDED:
                raise ValueError
            for member in members:
                parts = PurePosixPath(member.filename).parts
                mode = member.external_attr >> 16
                if (
                    not parts
                    or parts[0] != "durumari-catalog"
                    or ".." in parts
                    or "\\" in member.filename
                    or member.is_dir()
                    or member.filename in names
                    or PurePosixPath(member.filename).is_absolute()
                    or (stat.S_IFMT(mode) not in {0, stat.S_IFREG})
                    or not allowed_member("/".join(parts[1:]))
                ):
                    raise ValueError
                names.add(member.filename)
            for member in members:
                path = target.joinpath(*PurePosixPath(member.filename).parts)
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    output.write(archive.read(member))
        root = target / "durumari-catalog"
        plugin = root / "plugins/durumari-agent"
        for name in ("plugin.json", ".codex-plugin/plugin.json", ".claude-plugin/plugin.json"):
            item = json.loads((plugin / name).read_text(encoding="utf-8"))
            if item["name"] != "durumari" or item["version"] != version:
                raise ValueError
        for name, expected in (
            (
                ".agents/plugins/marketplace.json",
                {"source": "local", "path": "./plugins/durumari-agent"},
            ),
            (".claude-plugin/marketplace.json", "./plugins/durumari-agent"),
        ):
            item = json.loads((root / name).read_text(encoding="utf-8"))
            if item["name"] != "durumari" or len(item["plugins"]) != 1:
                raise ValueError
            if item["plugins"][0]["name"] != "durumari" or item["plugins"][0]["source"] != expected:
                raise ValueError
        verify_runtime(plugin / "runtime", version)
        return root
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile):
        raise ReleaseError("invalid_release_archive") from None


def state_directory() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return base / "DURUMARI/releases"
    return Path.home() / ".local/share/durumari/releases"


def load_state(directory: Path) -> dict[str, Any]:
    path = directory / "installation.json"
    if not path.exists():
        return {}
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
        if (
            item["schema_version"] != 1
            or release_origin(item["origin"]) != item["origin"]
            or not re.fullmatch(VERSION, item["version"])
            or not re.fullmatch(r"[0-9a-f]{64}", item["sha256"])
            or not isinstance(item["clients"], list)
            or not item["clients"]
            or not set(item["clients"]) <= {"codex", "claude"}
        ):
            raise ValueError
        return cast(dict[str, Any], item)
    except (ValueError, KeyError, TypeError, OSError):
        raise ReleaseError("invalid_release_installation_state") from None


def command_failure_reason(output: str) -> str:
    """Classify evidence without retaining paths, credentials or raw tool output."""
    output = output.lower()
    if any(
        text in output
        for text in ("winerror 32", "being used by another process", "sharing violation")
    ):
        return "runtime_file_in_use"
    if any(text in output for text in ("permission denied", "access is denied", "winerror 5")):
        return "permission_denied"
    if any(
        text in output
        for text in ("certificate verify failed", "invalid peer certificate", "unknown issuer")
    ):
        return "tls_certificate_error"
    if any(
        text in output
        for text in ("connection refused", "timed out", "failed to download", "dns error")
    ):
        return "network_error"
    if any(
        text in output
        for text in ("no solution found", "unsatisfiable", "conflicting dependencies")
    ):
        return "dependency_resolution_failed"
    return "unclassified_command_failure"


def run(command: list[str]) -> str:
    try:
        result = subprocess.run(
            command,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=180,
            check=False,
        )
        if result.returncode:
            raise ReleaseError(
                "release_install_command_failed",
                details={
                    "command": command[:4]
                    if command[1:3] == ["plugin", "marketplace"]
                    else command[:3],
                    "returncode": result.returncode,
                    "reason": command_failure_reason(result.stderr + "\n" + result.stdout),
                    "stdout": result.stdout[-8192:],
                    "stderr": result.stderr[-8192:],
                },
            )
        return result.stdout.strip()
    except subprocess.TimeoutExpired:
        raise ReleaseError(
            "release_install_command_timeout",
            details={
                "command": command[:3],
                "timeout_seconds": 180,
            },
        ) from None
    except OSError as error:
        raise ReleaseError(
            "release_install_command_unavailable",
            details={
                "command": command[:3],
                "errno": error.errno,
                "winerror": getattr(error, "winerror", None),
            },
        ) from None


def write_update_result(result: dict[str, Any]) -> None:
    directory = state_directory()
    temporary = directory / "update-result.tmp"
    temporary.write_text(json.dumps(result) + "\n", encoding="utf-8")
    temporary.replace(directory / "update-result.json")


def update_status() -> dict[str, Any]:
    try:
        data = (state_directory() / "update-result.json").read_bytes()
        if len(data) > 4096:
            raise ValueError
        result = json.loads(data)
        if not isinstance(result, dict) or result.get("status") not in {
            "started",
            "completed",
            "failed",
        }:
            raise ValueError
        return cast(dict[str, Any], result)
    except FileNotFoundError:
        return {"status": "not_started"}
    except (OSError, ValueError):
        raise ReleaseError("invalid_update_result") from None


def start_windows_update(args: argparse.Namespace) -> dict[str, Any]:
    # The running uv tool cannot safely replace its own Windows environment.
    # Start an independent interpreter and let this CLI exit before installation.
    directory = state_directory()
    previous = load_state(directory)
    origin = release_origin(getattr(args, "server", None) or previous.get("origin", ""))
    if previous and origin != previous["origin"]:
        raise ReleaseError("release_origin_change_requires_new_installation")
    if not shutil.which("uv"):
        raise ReleaseError("release_required_command_missing")
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "launch.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ReleaseError("release_update_already_running") from None
    try:
        runner = directory / "update-runner.py"
        runner.write_bytes(Path(__file__).read_bytes())
        command = [
            "uv",
            "run",
            "--no-project",
            "--python",
            "3.12",
            "--managed-python",
            str(runner),
            "update",
            "--server",
            origin,
            "--wait-for-pid",
            str(os.getpid()),
        ]
        if getattr(args, "client", None):
            command.extend(["--client", args.client])
        if getattr(args, "debug", False):
            command.append("--debug")
        result = {
            "status": "started",
            "completion_command": subprocess.list2cmdline(
                command[:6] + [str(runner), "update", "--status"]
            ),
            "status_file": str(directory / "update-result.json"),
        }
        write_update_result(result)
        subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200,
            close_fds=True,
        )
        return result
    except OSError:
        lock.rmdir()
        write_update_result({"status": "failed", "error": "release_runner_start_failed"})
        raise ReleaseError("release_runner_start_failed") from None


def wait_for_parent(pid: int) -> None:
    import ctypes
    from ctypes import wintypes

    windows_ctypes: Any = ctypes
    kernel = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x00100000, False, pid)
    if not handle:
        if windows_ctypes.get_last_error() == 87:
            # Parent already exited.
            return
        raise ReleaseError("release_parent_wait_failed")
    try:
        if kernel.WaitForSingleObject(handle, 30000) != 0:
            raise ReleaseError("release_parent_still_running")
    finally:
        kernel.CloseHandle(handle)


def run_independent_update(args: argparse.Namespace) -> int:
    try:
        wait_for_parent(args.wait_for_pid)
        result = execute_release(args)
        write_update_result({"status": "completed", **result})
        return 0
    except (ReleaseError, OSError) as error:
        code = str(error) if isinstance(error, ReleaseError) else "release_update_failed"
        failed_result: dict[str, Any] = {"status": "failed", "error": code}
        if isinstance(error, ReleaseError) and error.diagnostic_path:
            failed_result["diagnostic_path"] = error.diagnostic_path
        write_update_result(failed_result)
        return 2
    finally:
        (state_directory() / "launch.lock").rmdir()


def execute_cli_release(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "status", False):
        return update_status()
    if sys.platform == "win32" and not getattr(args, "check", False):
        return start_windows_update(args)
    return execute_release(args)


def legacy_runtime_owns_launcher() -> bool:
    """Allow a rename only for a launcher still owned by the exact old uv tool."""
    tools = Path(run(["uv", "tool", "dir"]))
    bins = Path(run(["uv", "tool", "dir", "--bin"]))
    legacy = tools / "malmoa-mcp"
    name = "durumari-mcp.exe" if sys.platform == "win32" else "durumari-mcp"
    launcher = bins / name
    original = legacy / ("Scripts" if sys.platform == "win32" else "bin") / name
    try:
        receipt = tomllib.loads((legacy / "uv-receipt.toml").read_text(encoding="utf-8"))
        entries = receipt["tool"]["entrypoints"]
        registered = any(
            entry.get("name") in {"durumari-mcp", name}
            and entry.get("from") == "malmoa-mcp"
            and Path(entry.get("install-path", "")).absolute() == launcher.absolute()
            for entry in entries
        )
        if not registered or not launcher.is_file() or not original.is_file():
            return False
        if launcher.samefile(original):
            return True
        # Windows uv can copy executables instead of linking them.
        return sys.platform == "win32" and launcher.read_bytes() == original.read_bytes()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def install_runtime(catalog: Path, version: str) -> None:
    wheel, constraints = verify_runtime(catalog / "plugins/durumari-agent/runtime", version)
    rename_options = ["--force"] if legacy_runtime_owns_launcher() else []
    run(
        [
            "uv",
            "tool",
            "install",
            str(wheel),
            "--python",
            "3.12",
            "--constraints",
            str(constraints),
            "--reinstall-package",
            "durumari-mcp",
            *rename_options,
        ]
    )


def install_host(client: str, catalog: Path, version: str) -> None:
    # Hosts own their trust and native plugin lifecycle. No private cache edits.
    if client == "codex":
        run(["codex", "plugin", "marketplace", "add", str(catalog), "--json"])
        run(["codex", "plugin", "add", "durumari@durumari", "--json"])
        output = run(["codex", "plugin", "list", "--marketplace", "durumari", "--json"])
    else:
        run(["claude", "plugin", "marketplace", "add", str(catalog)])
        run(["claude", "plugin", "install", "durumari@durumari", "--scope", "user"])
        run(["claude", "plugin", "update", "durumari@durumari", "--scope", "user"])
        output = run(["claude", "plugin", "list", "--json"])
    # The exact host response shape is adapter-specific; reject an unconfirmed version.
    try:
        rows = json.loads(output)
        entries = rows["installed"] if client == "codex" else rows
        identity = "pluginId" if client == "codex" else "id"
        if not any(
            row.get(identity) == "durumari@durumari" and row.get("version") == version
            for row in entries
        ):
            raise ValueError
    except (ValueError, TypeError, KeyError, AttributeError):
        raise ReleaseError("native_plugin_version_not_verified") from None


def retire_legacy_host(client: str) -> None:
    """Only after every replacement is verified, use the host's native lifecycle."""
    command = [client, "plugin", "list", "--json"]
    try:
        rows = json.loads(run(command))
        entries = rows["installed"] if client == "codex" else rows
        if not isinstance(entries, list):
            raise ValueError
        identity = "pluginId" if client == "codex" else "id"
        found = any(
            isinstance(row, dict)
            and row.get(identity) == LEGACY_PLUGIN
            and (client == "codex" or row.get("scope") == "user")
            for row in entries
        )
    except (ValueError, KeyError, TypeError):
        raise ReleaseError("legacy_plugin_state_not_verified") from None
    if found:
        if client == "codex":
            run(["codex", "plugin", "remove", LEGACY_PLUGIN, "--json"])
        else:
            run(["claude", "plugin", "uninstall", LEGACY_PLUGIN, "--scope", "user", "--keep-data"])


def execute_release(args: argparse.Namespace) -> dict[str, Any]:
    directory = state_directory()
    previous = load_state(directory)
    supplied_origin = getattr(args, "server", None)
    origin = release_origin(supplied_origin or previous.get("origin", ""))
    if previous and origin != previous["origin"]:
        raise ReleaseError("release_origin_change_requires_new_installation")
    release = parse_release(download(origin + "/downloads/latest.json", 32 * 1024))
    check = getattr(args, "check", False)
    if check:
        return {
            "installed": previous.get("version"),
            "latest": release.version,
            "update_available": previous.get("version") != release.version,
        }
    client = getattr(args, "client", None)
    clients = set(previous.get("clients", []))
    clients.update(["codex", "claude"] if client == "both" else [client] if client else [])
    if not clients:
        raise ReleaseError("release_client_required")
    for name in ["uv", *sorted(clients)]:
        if not shutil.which(name):
            raise ReleaseError("release_required_command_missing")
    data = download(origin + release.path, MAX_ARCHIVE)
    if len(data) != release.size or hashlib.sha256(data).hexdigest() != release.sha256:
        raise ReleaseError("release_archive_integrity_failed")
    directory.mkdir(parents=True, exist_ok=True)
    catalog, backup = directory / "catalog", directory / "previous"
    if (catalog.exists() and not previous) or backup.exists():
        raise ReleaseError("release_recovery_required")
    lock = directory / "update.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ReleaseError("release_update_already_running") from None
    attempt_id = uuid4().hex
    started_at = datetime.now(UTC).isoformat()
    switched = False
    stage = "extract_catalog"
    try:
        with tempfile.TemporaryDirectory(prefix="stage-", dir=directory) as temporary:
            staged = extract_catalog(data, Path(temporary), release.version)
            bundle = json.loads((staged / "plugins/durumari-agent/runtime/bundle.json").read_text())
            if "service" in bundle and parse_service(bundle["service"]) != origin:
                raise ReleaseError("release_service_origin_mismatch")
            if catalog.exists():
                stage = "backup_catalog"
                catalog.rename(backup)
            stage = "activate_catalog"
            staged.rename(catalog)
            switched = True
            stage = "install_runtime"
            install_runtime(catalog, release.version)
            for host in sorted(clients):
                stage = "install_host_" + host
                install_host(host, catalog, release.version)
            stage = "write_installation_state"
            state = {
                "schema_version": 1,
                "origin": origin,
                "version": release.version,
                "sha256": release.sha256,
                "clients": sorted(clients),
            }
            pending = directory / "installation.tmp"
            pending.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            pending.replace(directory / "installation.json")
        result: dict[str, Any] = {
            "installed": release.version,
            "clients": sorted(clients),
            "restart_hosts": True,
        }
        for host in sorted(clients):
            try:
                retire_legacy_host(host)
            except ReleaseError:
                # The new installation is valid. Keep its state for a safe retry;
                # never remove working code or old credentials on cleanup failure.
                result.setdefault("legacy_cleanup_required", []).append(host)
        if backup.exists():
            try:
                shutil.rmtree(backup)
            except OSError:
                result["cleanup_required"] = True
        # Current diagnostic follows the latest attempt; retain the prior failure separately.
        diagnostic = directory / "install-diagnostic.json"
        try:
            old_report = json.loads(diagnostic.read_text(encoding="utf-8"))
            if isinstance(old_report, dict) and "failure" in old_report:
                archived = directory / "last-install-failure.json"
                archived.write_text(
                    json.dumps(old_report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                os.chmod(archived, 0o600)
                result["previous_failure_path"] = str(archived)
        except (OSError, ValueError):
            pass
        report_path = write_failure_report(
            directory,
            {
                "schema_version": 1,
                "status": "completed",
                "version": release.version,
                "attempt_id": attempt_id,
                "started_at": started_at,
                "finished_at": datetime.now(UTC).isoformat(),
                "previous_failure_path": result.get("previous_failure_path"),
            },
            False,
        )
        if report_path:
            result["diagnostic_path"] = report_path
        return result
    except (ReleaseError, OSError) as error:
        report: dict[str, Any] = {
            "schema_version": 1,
            "version": release.version,
            "status": "failed",
            "attempt_id": attempt_id,
            "started_at": started_at,
            "finished_at": datetime.now(UTC).isoformat(),
            "stage": stage,
            "failure": failure_details(error),
            "rollback": "not_needed",
        }
        debug = getattr(args, "debug", False)
        diagnostic_path = write_failure_report(directory, report, debug)
        if not switched and isinstance(error, ReleaseError):
            error.diagnostic_path = diagnostic_path
            raise
        if switched:
            if previous and backup.exists():
                report["rollback"] = "started"
                failed = directory / "failed"
                if not failed.exists():
                    try:
                        catalog.rename(failed)
                        backup.rename(catalog)
                        install_runtime(catalog, previous["version"])
                        for host in previous["clients"]:
                            install_host(host, catalog, previous["version"])
                        shutil.rmtree(failed)
                        report["rollback"] = "restored"
                    except (ReleaseError, OSError) as rollback_error:
                        report["rollback"] = "failed"
                        report["rollback_failure"] = failure_details(rollback_error)
                        failure = ReleaseError("release_failed_rollback_requires_recovery")
                        failure.diagnostic_path = write_failure_report(directory, report, debug)
                        raise failure from None
                else:
                    report["rollback"] = "blocked_existing_failed_catalog"
            elif not previous:
                # Native/runtime may have installed partially; retain verified files for retry.
                try:
                    shutil.rmtree(catalog)
                    report["rollback"] = "removed_partial_catalog"
                except OSError as cleanup_error:
                    report["rollback"] = "cleanup_failed"
                    report["rollback_failure"] = failure_details(cleanup_error)
        failure = ReleaseError("release_install_failed_retry_or_restore")
        failure.diagnostic_path = write_failure_report(directory, report, debug)
        raise failure from None
    finally:
        lock.rmdir()


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ReleaseError("invalid_release_arguments")


def main() -> None:
    parser = Parser(
        description="Install or update the Durumari native plugin from an HTTPS release."
    )
    parser.add_argument("command", choices=["install", "update"])
    parser.add_argument("--server")
    parser.add_argument("--client", choices=["codex", "claude", "both"])
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Include bounded host stdout/stderr in the local failure report; "
            "may contain sensitive data. Review before sharing."
        ),
    )
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--wait-for-pid", type=int, help=argparse.SUPPRESS)
    try:
        args = parser.parse_args()
        if args.wait_for_pid is not None:
            raise SystemExit(run_independent_update(args))
        print(json.dumps(update_status() if args.status else execute_release(args)))
    except (ReleaseError, OSError) as error:
        print(
            str(error) if isinstance(error, ReleaseError) else "release_filesystem_error",
            file=sys.stderr,
        )
        if isinstance(error, ReleaseError) and error.diagnostic_path:
            print(json.dumps({"diagnostic_path": error.diagnostic_path}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
