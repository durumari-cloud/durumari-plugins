"""Portable native-plugin setup. Credentials stay in the Durumari OS profile store."""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
# The bundled runtime installer (durumari_mcp/releases.py) targets this Python.
INSTALLER_PYTHON = (3, 12)
# Plugin variables sit under DURUMARI_MCP_ (secret run reserves that prefix). This script
# stands alone, so the names repeat durumari_mcp.plugin_env; a test keeps them equal.
INSTALLER_DELEGATED_ENV = "DURUMARI_MCP_PLUGIN_INSTALLER_DELEGATED"
LEGACY_INSTALLER_DELEGATED_ENV = "DURUMARI_PLUGIN_INSTALLER_DELEGATED"
# Must match durumari_mcp.releases.UV_EXECUTABLE_ENV. The bundled installer prefers this name
# and still reads the earlier DURUMARI_UV_EXECUTABLE, so only the new name is written.
UV_EXECUTABLE_ENV = "DURUMARI_MCP_UV_EXECUTABLE"
# Recovery facts that are safe to print; command output stays in diagnostics.
SAFE_RELEASE_DETAILS = ("snapshot_id", "runtime_rollback", "other_runtime_processes")


class SetupError(Exception):
    def __init__(self, code, *, details=None, reported=False):
        super().__init__(code)
        self.details = details or {}
        # A delegated installer already printed its own error.
        self.reported = reported


class PluginParser(argparse.ArgumentParser):
    def error(self, message):
        # Invalid command lines can contain accidentally pasted credentials.
        raise SetupError("invalid_plugin_arguments")


def run(command, *, capture=False):
    # Browser approval owns login. Never collect credentials in this wrapper.
    result = subprocess.run(
        command, encoding="utf-8", errors="replace", capture_output=capture, check=False
    )
    if result.returncode:
        raise SetupError("setup_command_failed")
    return result.stdout.strip() if capture else result.returncode


def uv_executable():
    executable = shutil.which("uv")
    if not executable:
        raise SetupError("uv_required_install_from_astral_documentation")
    return executable


def runtime_python():
    directory = (
        Path(run([uv_executable(), "tool", "dir"], capture=True)) / "durumari-mcp"
    )
    executable = directory / (
        "Scripts/python.exe" if sys.platform == "win32" else "bin/python3"
    )
    if not executable.is_file():
        raise SetupError("runtime_not_installed_run_plugin_install")
    return str(executable)


def verify_bundle(directory):
    try:
        metadata = json.loads((directory / "bundle.json").read_text(encoding="utf-8"))
        files = metadata["files"]
        version = metadata["version"]
        wheel = f"durumari_mcp-{version}-py3-none-any.whl"
        if (
            metadata.get("schema_version") != 1
            or metadata.get("package") != "durumari-mcp"
            or not isinstance(version, str)
            or not re.fullmatch(r"\d+\.\d+\.\d+", version)
            or not isinstance(files, dict)
            or set(files) != {wheel, "requirements.txt"}
        ):
            raise SetupError("invalid_runtime_bundle")
        for name, expected in files.items():
            path = directory / name
            if (
                not isinstance(expected, str)
                or not re.fullmatch(r"[0-9a-f]{64}", expected)
                or path.is_symlink()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                raise SetupError("runtime_bundle_integrity_failed")
        if "service" in metadata or tuple(map(int, version.split("."))) >= (0, 2, 9):
            bundled_server(directory)
        return directory / wheel, directory / "requirements.txt"
    except (OSError, ValueError, KeyError, TypeError):
        raise SetupError("invalid_runtime_bundle_build_before_install") from None


def bundled_server(directory=None):
    """Only the installed package supplies a default, never a different repository."""
    try:
        path = (directory or PLUGIN_ROOT / "runtime") / "bundle.json"
        if path.is_symlink() or path.stat().st_size > 16_384:
            raise ValueError
        service = json.loads(path.read_text(encoding="utf-8"))["service"]
        if (
            not isinstance(service, dict)
            or set(service) != {"schema_version", "gateway_origin"}
            or type(service["schema_version"]) is not int
            or service["schema_version"] != 1
        ):
            raise ValueError
        origin = service["gateway_origin"]
        return safe_server(origin)
    except (OSError, ValueError, KeyError, TypeError):
        raise SetupError("invalid_plugin_service") from None


def safe_server(origin):
    try:
        if not isinstance(origin, str) or origin != origin.strip():
            raise ValueError
        parsed = urlsplit(origin)
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
    except (ValueError, TypeError):
        raise SetupError("invalid_plugin_service") from None


def parser():
    result = PluginParser(description=__doc__)
    commands = result.add_subparsers(
        dest="command", required=True, parser_class=PluginParser
    )
    commands.add_parser("install", help="Install the verified Cloud Context runtime")
    update = commands.add_parser(
        "update",
        help="Use the verified updater without importing the installed runtime",
    )
    update.add_argument("--check", action="store_true")
    update.add_argument("--status", action="store_true")
    update.add_argument("--server")
    update.add_argument(
        "--client", choices=("codex", "claude", "cursor", "gemini", "antigravity", "both")
    )
    account = commands.add_parser(
        "account-setup", help="Connect an account and register user MCP"
    )
    account.add_argument(
        "--client", choices=("codex", "claude-code", "cursor"), required=True
    )
    account.add_argument("--server")
    account.add_argument("--profile")
    account.add_argument("--no-browser", action="store_true")
    migration = commands.add_parser("account-migrate", help="Safely transition a selected folder")
    migration.add_argument("action", choices=("plan", "status", "apply", "rollback"))
    migration.add_argument("--client", choices=("codex", "claude-code", "cursor"), required=True)
    migration.add_argument("--profile", required=True)
    migration.add_argument("--workspace")
    migration.add_argument("--migration-id")
    for name in ("status", "connect", "remove"):
        command = commands.add_parser(name)
        command.add_argument("--workspace", required=name != "connect")
        command.add_argument(
            "--client",
            choices=("codex", "claude", "cursor", "gemini", "antigravity", "opencode", "copilot"),
        )
        if name == "connect":
            command.add_argument("--server")
            command.add_argument("--project")
            command.add_argument("--folder-source")
        if name == "status":
            command.add_argument("--change", choices=("git-init", "move", "detach"))
    depth = commands.add_parser(
        "prompt-context", help="Show or set per-message related Context depth"
    )
    depth.add_argument("--workspace", required=True)
    depth.add_argument("--depth")
    depth.add_argument("--all-projects", action="store_true")
    return result


def require_guided_runtime(command):
    current = run(command + ["--version"], capture=True)
    if not isinstance(current, str) or not re.fullmatch(r"\d+\.\d+\.\d+", current):
        raise SetupError("context_runtime_unavailable_update_plugin")
    if tuple(map(int, current.split("."))) < (0, 4, 0):
        raise SetupError("context_runtime_unavailable_update_plugin")


def release_error_details(error):
    details = {
        key: error.details[key]
        for key in SAFE_RELEASE_DETAILS
        if isinstance(getattr(error, "details", None), dict) and key in error.details
    }
    if getattr(error, "diagnostic_path", None):
        details["diagnostic_path"] = error.diagnostic_path
    return details


def run_verified_installer(wheel, action, uv):
    # Use the stdlib-only updater from the verified wheel, even if the installed
    # package is missing. This shares the same preservation/verification contract.
    try:
        with zipfile.ZipFile(wheel) as archive:
            source = archive.read("durumari_mcp/releases.py")
    except (OSError, zipfile.BadZipFile, KeyError):
        raise SetupError("safe_runtime_installer_missing_update_plugin") from None
    name = "durumari_verified_runtime_installer"
    try:
        with tempfile.TemporaryDirectory(prefix="durumari-installer-") as temporary:
            path = Path(temporary) / "installer.py"
            path.write_bytes(source)
            try:
                spec = importlib.util.spec_from_file_location(name, path)
                module = importlib.util.module_from_spec(spec)
                sys.modules[name] = module
                spec.loader.exec_module(module)
            except Exception:  # noqa: BLE001 - untrusted installer failures stay private
                # A broken or incompatible bundled installer never runs partially.
                raise SetupError(
                    "safe_runtime_installer_missing_update_plugin"
                ) from None
            if not hasattr(module, "install_runtime_bundle"):
                raise SetupError("safe_runtime_installer_missing_update_plugin")
            # The installer runs uv itself; give it the uv this wrapper resolved.
            previous_uv = os.environ.get(UV_EXECUTABLE_ENV)
            os.environ[UV_EXECUTABLE_ENV] = uv
            try:
                return action(module)
            except module.ReleaseError as error:
                raise SetupError(
                    str(error), details=release_error_details(error)
                ) from None
            except OSError:
                raise SetupError("runtime_install_filesystem_failed") from None
            except Exception:  # noqa: BLE001 - sanitize the external installer boundary
                raise SetupError("runtime_installer_internal_error") from None
            finally:
                if previous_uv is None:
                    os.environ.pop(UV_EXECUTABLE_ENV, None)
                else:
                    os.environ[UV_EXECUTABLE_ENV] = previous_uv
    except OSError:
        raise SetupError("runtime_install_filesystem_failed") from None
    finally:
        sys.modules.pop(name, None)


def installer_arguments(args):
    if args.command == "install":
        return ["install"]
    arguments = ["update"]
    for flag in ("check", "status"):
        if getattr(args, flag, False):
            arguments.append("--" + flag)
    for option in ("server", "client"):
        value = getattr(args, option, None)
        if value:
            arguments += ["--" + option, value]
    return arguments


def delegate_installer(uv, args):
    """Run install/update under uv's Python 3.12 when this python3 is older."""
    if os.environ.get(INSTALLER_DELEGATED_ENV) or os.environ.get(LEGACY_INSTALLER_DELEGATED_ENV):
        raise SetupError("python_3_12_required_for_runtime_installer")
    result = subprocess.run(
        [
            uv,
            "run",
            "--no-project",
            "--python",
            "{}.{}".format(*INSTALLER_PYTHON),
            "python",
            # The child's piped stderr is decoded as UTF-8 below; -I ignores
            # PYTHONUTF8/PYTHONIOENCODING, so force it (Windows uses the ANSI
            # code page for pipes and would corrupt the Korean guidance).
            "-X",
            "utf8",
            "-I",
            str(Path(__file__).resolve()),
            *installer_arguments(args),
        ],
        env={**os.environ, INSTALLER_DELEGATED_ENV: "1"},
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.returncode:
        # Only a child wrapper that ran prints its own {"error": ...}. When uv
        # itself fails (offline, no 3.12 available) report a structured code.
        raise SetupError(
            "runtime_installer_failed", reported=reports_structured_error(result.stderr)
        )
    return 0


def reports_structured_error(stderr):
    for line in reversed((stderr or "").splitlines()):
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("error"), str):
            return True
    return False


def execute(args):
    if args.command == "account-migrate":
        command = [runtime_python(), "-I", "-m", "durumari_mcp"]
        if "account-migrate" not in run(command + ["--help"], capture=True):
            raise SetupError("account_runtime_update_required_then_restart_host")
        command += ["account-migrate", args.action, "--client", args.client, "--profile", args.profile]
        if args.workspace:
            command += ["--workspace", args.workspace]
        if args.migration_id:
            command += ["--migration-id", args.migration_id]
        return run(command)
    if args.command == "connect" and args.workspace is None:
        if args.project or args.folder_source:
            raise SetupError("explicit_workspace_required_for_folder_connection")
        account_client = {"codex": "codex", "claude": "claude-code", "cursor": "cursor"}.get(args.client)
        if account_client is None:
            raise SetupError("account_setup_client_unsupported_use_explicit_folder")
        # An explicit --workspace retains the old folder-scoped contract. Only the
        # new no-folder entry point opts into the separately approved account grant.
        args = argparse.Namespace(
            command="account-setup", client=account_client, server=args.server,
            profile=None, no_browser=False,
        )
    if args.command == "account-setup":
        command = [runtime_python(), "-I", "-m", "durumari_mcp"]
        if "account-setup" not in run(command + ["--help"], capture=True):
            raise SetupError("account_runtime_update_required_then_restart_host")
        command += [
            "account-setup",
            "--client",
            args.client,
            "--server",
            safe_server(args.server) if args.server else bundled_server(),
        ]
        if args.profile:
            command += ["--profile", args.profile]
        if args.no_browser:
            command.append("--no-browser")
        return run(command)
    if args.command in {"install", "update"}:
        wheel, _constraints = verify_bundle(PLUGIN_ROOT / "runtime")
        metadata = json.loads((PLUGIN_ROOT / "runtime/bundle.json").read_text())
        if tuple(map(int, metadata["version"].split("."))) < (0, 4, 0):
            raise SetupError("context_runtime_unavailable_update_plugin")
        uv = uv_executable()
        if args.command == "update" and args.server:
            args.server = safe_server(args.server)
        if sys.version_info < INSTALLER_PYTHON:
            return delegate_installer(uv, args)
        if args.command == "update":
            result = run_verified_installer(
                wheel, lambda module: module.execute_cli_release(args), uv
            )
            print(json.dumps(result))
            return 0
        run_verified_installer(
            wheel,
            lambda module: module.install_runtime_bundle(
                PLUGIN_ROOT / "runtime", metadata["version"]
            ),
            uv,
        )
        print(json.dumps({"runtime": "installed", "credentials": "not_changed"}))
        return 0
    root = Path(args.workspace).resolve()
    if not root.is_dir():
        raise SetupError("workspace_directory_required")
    server = None
    if args.command == "connect":
        server = safe_server(args.server) if args.server else None
        if not server and not (root / "durumari.yaml").exists():
            server = bundled_server()
    try:
        python = runtime_python()
    except SetupError as error:
        if (
            args.command != "connect"
            or str(error) != "runtime_not_installed_run_plugin_install"
        ):
            raise
        execute(SimpleNamespace(command="install"))
        python = runtime_python()
    command = [python, "-I", "-m", "durumari_mcp"]
    if args.command == "connect":
        try:
            require_guided_runtime(command)
        except SetupError:
            if sys.platform == "win32":
                raise SetupError("runtime_update_required_then_restart_host") from None
            execute(SimpleNamespace(command="install"))
            python = runtime_python()
            command = [python, "-I", "-m", "durumari_mcp"]
            require_guided_runtime(command)
        command += ["context-connect", "--workspace", str(root)]
        if server:
            command += ["--server", server]
        if args.project:
            command += ["--project", args.project]
        if getattr(args, "folder_source", None):
            command += ["--folder-source", args.folder_source]
        if args.client:
            command += ["--agents", args.client]
    elif args.command == "prompt-context":
        command += ["prompt-context", "--workspace", str(root)]
        if args.depth:
            command += ["--depth", args.depth]
        if args.all_projects:
            command.append("--all-projects")
    else:
        command += [
            "context-status" if args.command == "status" else "context-remove",
            "--workspace",
            str(root),
        ]
        if args.command == "status" and args.change:
            command += ["--change", args.change]
    return run(command)


def main():
    try:
        return execute(parser().parse_args())
    except KeyboardInterrupt:
        print(
            "연결을 취소했습니다. 기존 로그인과 작업 기록은 유지됩니다.",
            file=sys.stderr,
        )
        return 130
    except SetupError as error:
        if not error.reported:
            print(json.dumps({"error": str(error), **error.details}), file=sys.stderr)
        if str(error) == "runtime_update_required_then_restart_host":
            print(
                "연결 안내를 사용하려면 플러그인 update 절차를 완료한 뒤 "
                "호스트를 다시 시작해 주세요. 기존 로그인과 기록은 유지됩니다.",
                file=sys.stderr,
            )
        return 1
    except (OSError, ValueError):
        print(json.dumps({"error": "plugin_setup_failed"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
