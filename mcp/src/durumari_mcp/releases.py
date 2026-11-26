"""Public release installer. Never reads project credentials or capture queues.

Requires Python 3.12+ (see pyproject). The native plugin wrapper executes this
file from the verified bundled wheel only on 3.12+ and otherwise delegates to a
uv-provided Python 3.12, so no older interpreter runs this module.
"""

import argparse
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import ssl
import stat
import subprocess
import sys
import tempfile
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Never, cast
from uuid import uuid4

MAX_ARCHIVE = 8 * 1024 * 1024
MAX_EXPANDED = 24 * 1024 * 1024
MAX_MEMBERS = 64
# durumari_plugin.bundled_server reads bundle.json with the same bound.
MAX_BUNDLE = 16 * 1024
BUNDLE = "plugins/durumari-agent/runtime/bundle.json"
MCP_CONFIG = "plugins/durumari-agent/mcp.json"
CLAUDE_MCP_CONFIG = "plugins/durumari-agent/mcp-claude.json"
WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {
    f"{name}{index}" for name in ("COM", "LPT") for index in range(1, 10)
}
VERSION = r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,5}"
CATALOG = "https://github.com/MyKnow/malmoa-plugins.git"
# The catalog root README.md of every release archive (plugin READMEs moved to Durumari
# Context). Updaters accept README.md at the root, so the text may change.
CATALOG_README = (
    "# Durumari plugins\n"
    "\n"
    "Native plugin for Codex and Claude Code. "
    "Run these commands from this extracted catalog.\n"
    "\n"
    "```sh\n"
    "codex plugin marketplace add .\n"
    "codex plugin add durumari@durumari\n"
    "claude plugin marketplace add .\n"
    "claude plugin install durumari@durumari --scope user\n"
    "```\n"
    "\n"
    "GitHub Copilot CLI (experimental, not yet verified with a real account):\n"
    "\n"
    "```sh\n"
    "copilot plugin marketplace add .\n"
    "copilot plugin install durumari@durumari\n"
    "```\n"
    "\n"
    "Cursor: copy plugins/durumari-agent to ~/.cursor/plugins/local/durumari "
    "or run `durumari-mcp update --client cursor`, then reload the window.\n"
    "\n"
    "Gemini CLI: `gemini extensions install ./plugins/durumari-agent` "
    "or `durumari-mcp update --client gemini`, then restart Gemini CLI.\n"
    "\n"
    "Open the Durumari connect skill in your project to log in and select a project. "
    "Credentials stay in the OS credential store. "
    "Host trust approval and a new session may be required.\n"
)
# Exact pre-rename host identity; project data and credential stores stay intact.
LEGACY_PLUGIN = "malmoa@malmoa"
# Hosts whose plugin is installed by copying into a documented local directory.
LOCAL_DIRECTORY_HOSTS = {"cursor"}
# Only these hosts ever installed the pre-rename plugin identity.
LEGACY_PLUGIN_HOSTS = {"codex", "claude"}
# The plugin wrapper passes the uv it resolved; the CLI falls back to PATH.
# This file loads on its own, so the names repeat plugin_env (a test keeps them equal).
UV_EXECUTABLE_ENV = "DURUMARI_MCP_UV_EXECUTABLE"
LEGACY_UV_EXECUTABLE_ENV = "DURUMARI_UV_EXECUTABLE"
RUNTIME_PROCESS_NAMES = frozenset({"python.exe", "pythonw.exe", "durumari-mcp.exe"})
# Exited (ERROR_INVALID_PARAMETER) or another user's/protected process
# (ERROR_ACCESS_DENIED): its image cannot be read, so Restart Manager's file
# usage check decides instead. Any other API error stops replacement.
UNINSPECTABLE_PROCESS_ERRORS = frozenset({5, 87})
# server_aliases.MOVED, repeated because the launcher loads this file on its own.
MOVED_HOSTS = {
    "https://durumari.kr": "https://app.durumari.cloud",
    "https://preview.durumari.kr": "https://preview.durumari.cloud",
}
ANNOUNCEMENT_TIMEOUT = 3


def uv_command() -> str:
    configured = os.environ.get(UV_EXECUTABLE_ENV) or os.environ.get(LEGACY_UV_EXECUTABLE_ENV)
    if configured and Path(configured).is_absolute() and Path(configured).is_file():
        return configured
    return "uv"


def _is_junction(path: Path) -> bool:
    """Path.is_junction is 3.12+; without it no junction can be reported."""
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction()) if is_junction is not None else False


class ReleaseError(Exception):
    """Only fixed, safe error codes cross the CLI boundary."""

    def __init__(self, code: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(code)
        self.details = details or {}
        self.diagnostic_path: str | None = None


class ReleaseNetworkError(ReleaseError):
    """A transport failure that may safely be retried against the stored origin."""


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


def same_server(left: str, right: str) -> bool:
    previous = {moved: origin for origin, moved in MOVED_HOSTS.items()}
    return previous.get(left, left) == previous.get(right, right)


def announced_app_origin(host: str) -> Any:
    request = urllib.request.Request(
        host + "/api/v2/release", headers={"User-Agent": "DURUMARI-Release/1"}
    )
    with urllib.request.build_opener(SameOriginRedirect()).open(
        request, timeout=ANNOUNCEMENT_TIMEOUT
    ) as response:
        value = json.loads(response.read(32 * 1024 + 1)[: 32 * 1024])
    return value["service"]["app_origin"]


def download_origin(origin: str) -> str:
    """The pair host the server announces, so updates follow its switch.

    server_aliases.announced without its cache, because the launcher loads this file
    on its own. A host outside the pair, or no answer, keeps the stored origin.
    """
    previous = {moved: host for host, moved in MOVED_HOSTS.items()}
    key = previous.get(origin, origin)
    if key not in MOVED_HOSTS:
        return origin
    pair = (key, MOVED_HOSTS[key])
    # Ask the stored host first, then its pair, so either side being down still answers.
    for host in dict.fromkeys((origin, *pair)):
        try:
            app = announced_app_origin(host)
        except Exception:
            continue
        if app in pair:
            return str(app)
    return origin


def installed_origin(previous: dict[str, Any], supplied: str | None) -> str:
    """An installation keeps its origin; only its moved pair may be named instead."""
    origin = release_origin(supplied or previous.get("origin", ""))
    if previous and not same_server(origin, previous["origin"]):
        raise ReleaseError("release_origin_change_requires_new_installation")
    return str(previous["origin"]) if previous else origin


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
            headers = getattr(response, "headers", None)
            lengths = headers.get_all("Content-Length", []) if headers is not None else []
            declared_length = None
            if headers is not None and lengths:
                if (
                    len(lengths) != 1
                    or len(lengths[0]) > 20
                    or not re.fullmatch(r"[0-9]+", lengths[0])
                    or headers.get("Transfer-Encoding") is not None
                ):
                    raise ReleaseError("release_content_length_invalid")
                declared_length = int(lengths[0])
                if declared_length > limit:
                    raise ReleaseError("release_download_too_large")
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ReleaseError("release_download_too_large")
        if declared_length is not None and len(data) < declared_length:
            raise ReleaseNetworkError("release_download_failed")
        if declared_length is not None and len(data) > declared_length:
            raise ReleaseError("release_content_length_invalid")
        return cast(bytes, data)
    except urllib.error.HTTPError as error:
        if error.code in {500, 502, 503, 504}:
            raise ReleaseNetworkError("release_download_failed") from None
        raise ReleaseError("release_download_failed") from None
    except http.client.IncompleteRead:
        raise ReleaseNetworkError("release_download_failed") from None
    except (OSError, urllib.error.URLError) as error:
        reason = getattr(error, "reason", error)
        if isinstance(error, ssl.SSLError) or isinstance(reason, ssl.SSLError):
            raise ReleaseError("release_download_failed") from None
        raise ReleaseNetworkError("release_download_failed") from None


def download_with_fallback(base: str, origin: str, path: str, limit: int) -> bytes:
    try:
        return download(base + path, limit)
    except ReleaseNetworkError:
        if base == origin:
            raise
        return download(origin + path, limit)


def allowed_member(path: str) -> bool:
    """Files a release build selects; also the only files of a manifest-less archive.

    Updaters 0.4.0-0.4.4 accept nothing else, so a release must not contain other
    files until those updaters have been replaced (see docs/release-compatibility.md).
    """
    if path in {"README.md", ".agents/plugins/marketplace.json", ".claude-plugin/marketplace.json"}:
        return True
    if path == ".cursor-plugin/marketplace.json":
        return True
    prefix = "plugins/durumari-agent/"
    if not path.startswith(prefix):
        return False
    name = path[len(prefix) :]
    if name in {
        "README.md",
        "plugin.json",
        "mcp.json",
        "mcp-claude.json",
        ".codex-plugin/plugin.json",
        ".claude-plugin/plugin.json",
        ".cursor-plugin/plugin.json",
        "gemini-extension.json",
        "scripts/durumari_plugin.py",
        "runtime/requirements.txt",
        "runtime/bundle.json",
    }:
        return True
    return bool(
        # Releases up to 0.4.12 (manifest-less archives are still read with this list).
        re.fullmatch(r"skills/(connect|context|status|migrate|update)/SKILL\.md", name)
        # Goal-named skills: SKILL.md, Codex metadata, one reference level.
        or re.fullmatch(
            r"skills/durumari-(?:connect|context|secrets|status|update|migrate|share-skill|work-logs)/"
            r"(?:SKILL\.md|agents/openai\.yaml|references/(?:[a-z0-9-]+/)?[a-z0-9-]+\.md)",
            name,
        )
        or re.fullmatch(rf"runtime/durumari_mcp-{VERSION}-py3-none-any\.whl", name)
    )


def catalog_path_allowed(path: str, version: str) -> bool:
    """Shape of a manifest-listed catalog file; its content is fixed by the manifest hash.

    Host adapters add files here without an updater change: the plugin directory,
    root-level host metadata directories (JSON only) and the catalog README.
    """
    parts = path.split("/")
    if len(path) > 200 or not 1 <= len(parts) <= 8:
        return False
    for part in parts:
        if (
            len(part) > 96
            or not re.fullmatch(r"\.?[A-Za-z0-9][A-Za-z0-9._-]*", part)
            or part.endswith(".")
            or part.split(".")[0].upper() in WINDOWS_RESERVED
        ):
            return False
    if parts[-1].startswith("."):
        # Hidden files (.env, .npmrc, ...) are never part of a public release.
        return False
    if path == "README.md":
        return True
    if parts[0].startswith("."):
        # Host marketplace metadata such as .claude-plugin/marketplace.json.
        return len(parts) >= 2 and parts[-1].endswith(".json")
    if parts[:2] != ["plugins", "durumari-agent"] or len(parts) < 3 or path == BUNDLE:
        return False
    if parts[2] == "runtime":
        return parts[3:] in (["requirements.txt"], [f"durumari_mcp-{version}-py3-none-any.whl"])
    return True


def catalog_bundle(bundle: bytes, files: dict[str, bytes]) -> bytes:
    """Return runtime/bundle.json listing every other catalog file and its SHA-256.

    The manifest lives in an existing file because 0.4.0-0.4.4 updaters reject any
    new path but ignore unknown keys of bundle.json.
    """
    item = json.loads(bundle)
    if not isinstance(item, dict):
        raise ValueError("bundle.json must be an object")
    item["catalog"] = {
        "schema_version": 1,
        "files": {
            path: hashlib.sha256(data).hexdigest()
            for path, data in sorted(files.items())
            if path != BUNDLE
        },
    }
    output = (json.dumps(item, indent=2) + "\n").encode()
    if len(output) > MAX_BUNDLE or len(files) > MAX_MEMBERS:
        raise ValueError("Release catalog manifest exceeds updater limits")
    return output


def service_catalog(files: dict[str, bytes], origin: str) -> dict[str, bytes]:
    """Bind the runtime and optional native remote MCP to one packaged service.

    The visible mcp.json fits existing manifest-aware updaters. Hidden files stay
    forbidden. Only our credential-free HTTP declaration is selected for release.
    """
    origin = parse_service({"schema_version": 1, "gateway_origin": origin})
    variant = dict(files)
    for path, transport in [(MCP_CONFIG, "streamable-http"), (CLAUDE_MCP_CONFIG, "http")]:
        if path not in variant:
            continue
        try:
            config = json.loads(variant[path])
            keys = {"mcpServers", "$schema"} if path == MCP_CONFIG else {"mcpServers"}
            if set(config) != keys or set(config["mcpServers"]) != {"durumari"}:
                raise ValueError
            if path == MCP_CONFIG and config["$schema"] != (
                "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"
            ):
                raise ValueError
            remote = config["mcpServers"]["durumari"]
            if set(remote) != {"type", "url"} or remote["type"] != transport:
                raise ValueError
            if not isinstance(remote["url"], str) or not remote["url"].endswith("/mcp"):
                raise ValueError
            parse_service({"schema_version": 1, "gateway_origin": remote["url"][:-4]})
        except (TypeError, KeyError, ValueError, ReleaseError):
            raise ValueError("invalid_plugin_mcp_config") from None
        remote["url"] = origin + "/mcp"
        variant[path] = (json.dumps(config, indent=2) + "\n").encode()
    bundle = json.loads(variant[BUNDLE])
    bundle["service"] = {"schema_version": 1, "gateway_origin": origin}
    variant[BUNDLE] = catalog_bundle((json.dumps(bundle, indent=2) + "\n").encode(), variant)
    return variant


def catalog_manifest(bundle: bytes) -> dict[str, str] | None:
    """Parse the optional catalog manifest; None means a pre-manifest archive."""
    if len(bundle) > MAX_BUNDLE:
        raise ValueError
    item = json.loads(bundle)
    if not isinstance(item, dict):
        raise ValueError
    if "catalog" not in item:
        return None
    manifest = item["catalog"]
    if (
        not isinstance(manifest, dict)
        or set(manifest) != {"schema_version", "files"}
        or type(manifest["schema_version"]) is not int
        or manifest["schema_version"] != 1
        or not isinstance(manifest["files"], dict)
        or len(manifest["files"]) >= MAX_MEMBERS
    ):
        raise ValueError
    files = cast(dict[str, Any], manifest["files"])
    for digest in files.values():
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError
    return cast(dict[str, str], files)


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
            if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > MAX_EXPANDED:
                raise ValueError
            relative: dict[str, zipfile.ZipInfo] = {}
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
                    # Reject a/b and a//b aliases before any manifest comparison.
                    or "/".join(parts) != member.filename
                ):
                    raise ValueError
                names.add(member.filename)
                relative["/".join(parts[1:])] = member
            if BUNDLE not in relative or relative[BUNDLE].file_size > MAX_BUNDLE:
                raise ValueError
            manifest = catalog_manifest(archive.read(relative[BUNDLE]))
            contents: dict[str, bytes] = {}
            if manifest is None:
                # Archives built before 0.4.5 carry no manifest: the fixed list applies.
                if not all(allowed_member(name) for name in relative):
                    raise ValueError
            else:
                listed = set(relative) - {BUNDLE}
                folded = {name.casefold() for name in relative}
                if (
                    listed != set(manifest)
                    or len(folded) != len(relative)
                    or not all(catalog_path_allowed(name, version) for name in listed)
                ):
                    raise ValueError
                for name in listed:
                    contents[name] = archive.read(relative[name])
                    if hashlib.sha256(contents[name]).hexdigest() != manifest[name]:
                        raise ValueError
            for name, member in relative.items():
                path = target.joinpath("durumari-catalog", *name.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("xb") as output:
                    output.write(contents[name] if name in contents else archive.read(member))
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
        verify_cursor_catalog(root, version)
        verify_gemini_extension(plugin, version)
        verify_runtime(plugin / "runtime", version)
        return root
    except (ValueError, KeyError, TypeError, OSError, zipfile.BadZipFile):
        raise ReleaseError("invalid_release_archive") from None


def verify_cursor_catalog(root: Path, version: str) -> None:
    """Cursor files are optional for older archives, but must be exact when present."""
    manifest = root / "plugins/durumari-agent/.cursor-plugin/plugin.json"
    marketplace = root / ".cursor-plugin/marketplace.json"
    if not manifest.exists() and not marketplace.exists():
        return
    item = json.loads(manifest.read_text(encoding="utf-8"))
    if item["name"] != "durumari" or item["version"] != version:
        raise ValueError
    if item.get("skills", "./skills/") != "./skills/" or "mcpServers" in item:
        # Project MCP settings are created only after browser approval.
        raise ValueError
    item = json.loads(marketplace.read_text(encoding="utf-8"))
    if item["name"] != "durumari" or len(item["plugins"]) != 1:
        raise ValueError
    if item["plugins"][0]["name"] != "durumari" or item["plugins"][0]["source"] != (
        "plugins/durumari-agent"
    ):
        raise ValueError


def verify_gemini_extension(plugin: Path, version: str) -> None:
    """Optional for older archives; when present it carries no MCP server or secrets."""
    path = plugin / "gemini-extension.json"
    if not path.exists():
        return
    item = json.loads(path.read_text(encoding="utf-8"))
    # Project MCP settings are written to .gemini/settings.json only after approval.
    if set(item) != {"name", "version", "description"}:
        raise ValueError
    if item["name"] != "durumari" or item["version"] != version:
        raise ValueError


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
            or not set(item["clients"]) <= {"codex", "claude", "cursor", "gemini", "antigravity"}
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


# cmd.exe re-parses a batch file's arguments and would expand or split on these.
BATCH_UNSAFE = frozenset('"%^&|<>\r\n')


def launchable(command: list[str]) -> list[str]:
    """Windows starts only an .exe by bare name; npm installs Codex and Gemini CLI as .cmd shims.

    shutil.which already finds the shim through PATHEXT, so run what it found by full path.
    """
    if sys.platform != "win32":
        return command
    # A host found outside PATH already arrives by full path and may itself be a batch shim.
    executable = (
        command[0] if os.path.dirname(command[0]) else shutil.which(command[0]) or command[0]
    )
    if executable.lower().endswith((".cmd", ".bat")) and any(
        BATCH_UNSAFE & set(argument) for argument in command[1:]
    ):
        raise ReleaseError(
            "release_install_path_unsupported",
            details={"command": [Path(command[0]).stem, *command[1:3]]},
        )
    return [executable, *command[1:]]


def run(command: list[str]) -> str:
    # A host found outside PATH runs by full path; reports keep only its name.
    shown = [Path(command[0]).stem, *command[1:]]
    try:
        result = subprocess.run(
            launchable(command),
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
                    "command": shown[:4]
                    if command[1:3] == ["plugin", "marketplace"]
                    else shown[:3],
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
                "command": shown[:3],
                "timeout_seconds": 180,
            },
        ) from None
    except OSError as error:
        raise ReleaseError(
            "release_install_command_unavailable",
            details={
                "command": shown[:3],
                "errno": error.errno,
                "winerror": getattr(error, "winerror", None),
            },
        ) from None


# A lock directory names the process that holds it. A runner killed from
# outside (for example with the app whose job it belonged to) leaves its lock behind; the next
# run moves such a lock aside instead of refusing every later update.
LOCK_OWNER = "owner.json"
# A lock without an owner record is being created, or belongs to a runner from before owner
# records (0.4.51 and older) that may still be installing: each install command may take 15
# minutes, so only a lock left this long counts as abandoned.
LOCK_OWNER_GRACE_SECONDS = 6 * 3600.0
RUNNER_LOG = "update-runner.log"
# The runner's own stdout and stderr: a separate file, so its handle never overwrites or holds
# the stage log that note_runner appends to and rotates.
RUNNER_OUTPUT = "update-runner.out"
RUNNER_LOG_LIMIT = 256 * 1024


def process_alive(pid: int, since: datetime | None = None) -> bool:
    """Whether `pid` runs; with `since`, a process Windows started later reuses the pid."""
    if pid <= 0:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes

    windows_ctypes: Any = ctypes
    kernel = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE, *[ctypes.POINTER(wintypes.FILETIME)] * 4]
    kernel.GetProcessTimes.restype = wintypes.BOOL
    # PROCESS_QUERY_LIMITED_INFORMATION; an unknown pid is gone, any other refusal is alive.
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return bool(windows_ctypes.get_last_error() != 87)
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        if code.value != 259:  # STILL_ACTIVE
            return False
        times = [wintypes.FILETIME() for _ in range(4)]
        if since is None or not kernel.GetProcessTimes(handle, *map(ctypes.byref, times)):
            return True
        # FILETIME counts 100 ns from 1601; one second of slack for clock rounding.
        ticks = times[0].dwHighDateTime << 32 | times[0].dwLowDateTime
        created = datetime(1601, 1, 1, tzinfo=UTC) + timedelta(microseconds=ticks // 10)
        return created <= since + timedelta(seconds=1)
    finally:
        kernel.CloseHandle(handle)


def record_lock_owner(lock: Path, owner: int) -> None:
    # The CLI and the runner may both record the same lock; each writes its own file.
    pending = lock / f"{LOCK_OWNER}.{os.getpid()}-{uuid4().hex}.tmp"
    pending.write_text(
        json.dumps({"pid": owner, "at": datetime.now(UTC).isoformat()}), encoding="utf-8"
    )
    pending.replace(lock / LOCK_OWNER)


def claim_lock(lock: Path, owner: int) -> None:
    lock.mkdir()
    try:
        record_lock_owner(lock, owner)
    except OSError:
        # A lock without its owner would block every update for the owner-less grace period.
        shutil.rmtree(lock, ignore_errors=True)
        raise


LockSeen = tuple[str | None, int]


def lock_seen(lock: Path) -> LockSeen | None:
    """The owner record and the directory's change time: what a decision about it rests on."""
    try:
        try:
            text: str | None = (lock / LOCK_OWNER).read_text(encoding="utf-8")
        except FileNotFoundError:
            text = None
        return text, lock.stat().st_mtime_ns
    except OSError:
        return None


def lock_abandoned(lock: Path, seen: LockSeen | None = None) -> bool:
    """Whether the process that took this lock is gone; unreadable records count as held."""
    seen = seen if seen is not None else lock_seen(lock)
    if seen is None:
        return False
    text, changed = seen
    if text is None:
        import time

        return time.time() - changed / 1e9 > LOCK_OWNER_GRACE_SECONDS
    try:
        record = json.loads(text)
        pid = record["pid"]
        at = datetime.fromisoformat(record["at"]) if "at" in record else None
    except (ValueError, KeyError, TypeError):
        return False
    return type(pid) is int and not process_alive(pid, at)


def release_lock(lock: Path, owners: set[int]) -> None:
    """Remove a lock one of `owners` holds; one another run took over meanwhile stays."""
    seen = lock_seen(lock)
    if seen is None:
        return
    try:
        pid = json.loads(seen[0])["pid"] if seen[0] is not None else None
    except (ValueError, KeyError, TypeError):
        pid = None
    if pid not in owners:
        note_runner(f"left {lock.name} to the run that now holds it")
        return
    shutil.rmtree(lock, ignore_errors=True)


def set_aside(path: Path, name: str | None = None) -> Path:
    """Move an abandoned lock or a half-switched catalog aside, beside it; nothing is deleted."""
    kept = path.parent / "quarantine"
    kept.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    target = kept / f"{name or path.name}-{stamp}-{uuid4().hex[:8]}"
    path.rename(target)
    note_runner(f"set aside {name or path.name} as {target.name}")
    return target


def take_lock(lock: Path, owner: int, busy: str) -> None:
    """Take a lock, first moving aside one whose owner has exited."""
    try:
        claim_lock(lock, owner)
        return
    except FileExistsError:
        seen = lock_seen(lock)
        if seen is None or not lock_abandoned(lock, seen):
            raise ReleaseError(busy) from None
    # Move it under a name only this process knows, then check it is still the lock judged
    # abandoned: another run may have moved that one and taken a fresh lock meanwhile.
    moving = lock.with_name(f"{lock.name}.moving-{uuid4().hex}")
    try:
        lock.rename(moving)
    except OSError:
        raise ReleaseError(busy) from None
    if lock_seen(moving) != seen:
        try:
            moving.rename(lock)
        except OSError:
            note_runner(f"could not return {lock.name}; left as {moving.name}")
        raise ReleaseError(busy)
    set_aside(moving, lock.name)
    try:
        claim_lock(lock, owner)
    except FileExistsError:
        raise ReleaseError(busy) from None


def note_runner(line: str) -> None:
    """Append a stage line to the runner log; diagnostics never block an update."""
    try:
        directory = state_directory()
        directory.mkdir(parents=True, exist_ok=True)
        log = directory / RUNNER_LOG
        if log.exists() and log.stat().st_size > RUNNER_LOG_LIMIT:
            log.replace(directory / (RUNNER_LOG + ".1"))
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"{datetime.now(UTC).isoformat()} {os.getpid()} {line}\n")
    except OSError:
        pass


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
    except FileNotFoundError:
        return {"status": "not_started"}
    except (OSError, ValueError):
        raise ReleaseError("invalid_update_result") from None
    lock = state_directory() / "launch.lock"
    if result["status"] == "started" and (not lock.exists() or lock_abandoned(lock)):
        # The runner was stopped from outside before it could record an outcome.
        return {"status": "failed", "error": "release_runner_stopped"}
    return cast(dict[str, Any], result)


def start_windows_update(args: argparse.Namespace) -> dict[str, Any]:
    # The running uv tool cannot safely replace its own Windows environment.
    # Start an independent interpreter and let this CLI exit before installation.
    directory = state_directory()
    previous = load_state(directory)
    origin = installed_origin(previous, getattr(args, "server", None))
    if not shutil.which(uv_command()):
        raise ReleaseError("release_required_command_missing")
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / "launch.lock"
    # The CLI holds it until the runner has started; the runner's process then owns it.
    take_lock(lock, os.getpid(), "release_update_already_running")
    try:
        runner = directory / "update-runner.py"
        runner.write_bytes(Path(__file__).read_bytes())
        command = [
            uv_command(),
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
        note_runner("start runner")
        with (directory / RUNNER_OUTPUT).open("ab") as log:
            process = start_runner(command, log)
    except OSError:
        release_lock(lock, {os.getpid()})
        write_update_result({"status": "failed", "error": "release_runner_start_failed"})
        raise ReleaseError("release_runner_start_failed") from None
    try:
        # Cover the moment until the runner records itself; it may already have, or finished.
        record_lock_owner(lock, process.pid)
    except OSError:
        note_runner("runner recorded the launch lock first")
    return result


# DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP, and leaving the caller's job: a host app that
# is closed or killed takes its whole job with it, which stopped runners mid-install.
RUNNER_FLAGS = 0x00000008 | 0x00000200
CREATE_BREAKAWAY_FROM_JOB = 0x01000000


def start_runner(command: list[str], log: Any) -> "subprocess.Popen[bytes]":
    options: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": log,
        "stderr": log,
        "close_fds": True,
    }
    try:
        return subprocess.Popen(
            command, creationflags=RUNNER_FLAGS | CREATE_BREAKAWAY_FROM_JOB, **options
        )
    except PermissionError:
        # A job that forbids breakaway refuses the flag; start inside it as before.
        note_runner("job forbids breakaway; runner stays in the caller's job")
        return subprocess.Popen(command, creationflags=RUNNER_FLAGS, **options)


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


# The CLI's pid is the base interpreter. Its uv launcher and the environment's
# python.exe redirector, both inside the runtime, exit a moment after it does.
RUNTIME_RELEASE_SECONDS = 15.0


def wait_for_runtime_release(seconds: float = RUNTIME_RELEASE_SECONDS) -> None:
    """Give the exiting CLI's own processes time to leave the runtime.

    An Agent that keeps using the runtime still fails the installation's own
    preflight after this wait, with the same close-and-retry guidance.
    """
    import time

    deadline = time.monotonic() + seconds
    while True:
        try:
            preflight_runtime()
            return
        except ReleaseError as error:
            if str(error) != "runtime_in_use_close_hosts_and_retry":
                return
            if time.monotonic() >= deadline:
                return
        time.sleep(0.5)


def run_independent_update(args: argparse.Namespace) -> int:
    note_runner("runner started")
    try:
        record_lock_owner(state_directory() / "launch.lock", os.getpid())
    except OSError:
        note_runner("could not record the runner as the launch lock owner")
    try:
        wait_for_parent(args.wait_for_pid)
        wait_for_runtime_release()
        result = execute_release(args)
        write_update_result({"status": "completed", **result})
        note_runner("runner completed " + str(result.get("installed")))
        return 0
    except (ReleaseError, OSError) as error:
        code = str(error) if isinstance(error, ReleaseError) else "release_update_failed"
        failed_result: dict[str, Any] = {"status": "failed", "error": code}
        if isinstance(error, ReleaseError) and error.diagnostic_path:
            failed_result["diagnostic_path"] = error.diagnostic_path
        write_update_result(failed_result)
        note_runner("runner failed " + code)
        return 2
    finally:
        # The CLI may have recorded the runner's uv parent instead of the runner itself.
        release_lock(state_directory() / "launch.lock", {os.getpid(), os.getppid()})


def execute_cli_release(args: argparse.Namespace) -> dict[str, Any]:
    if getattr(args, "status", False):
        return update_status()
    if sys.platform == "win32" and not getattr(args, "check", False):
        return start_windows_update(args)
    return execute_release(args)


def legacy_runtime_owns_launcher() -> bool:
    """Allow a rename only for a launcher still owned by the exact old uv tool."""
    tools = Path(run([uv_command(), "tool", "dir"]))
    bins = Path(run([uv_command(), "tool", "dir", "--bin"]))
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


def install_runtime(catalog: Path, version: str, *, previous_release: bool = False) -> None:
    install_runtime_bundle(
        catalog / "plugins/durumari-agent/runtime", version, previous_release=previous_release
    )


def runtime_paths() -> tuple[Path, Path]:
    tools = Path(run([uv_command(), "tool", "dir"]))
    bins = Path(run([uv_command(), "tool", "dir", "--bin"]))
    if not tools.is_absolute() or not bins.is_absolute():
        raise ReleaseError("invalid_runtime_installation_path")
    return tools / "durumari-mcp", bins / (
        "durumari-mcp.exe" if sys.platform == "win32" else "durumari-mcp"
    )


def process_lineage(parents: dict[int, int], pid: int) -> set[int]:
    """The process and its ancestors; stops at a missing parent or a PID cycle."""
    lineage: set[int] = set()
    while pid and pid not in lineage:
        lineage.add(pid)
        pid = parents.get(pid, 0)
    return lineage


def image_inside(image: str, directory: Path) -> bool:
    root = os.path.normcase(str(directory.absolute()))
    image = os.path.normcase(image)
    return image == root or image.startswith(root + os.sep)


def check_runtime_process_images(
    directory: Path,
    processes: list[tuple[int, int, str]],
    image_of: Callable[[int], str | None],
    current_pid: int,
) -> None:
    """Refuse replacement while any runtime-image process exists, ours included.

    The updater's own process or an ancestor (for example a CLI started from
    this runtime's python.exe or launcher) is never excluded: Windows cannot
    replace an image that is still running, so skipping it would reintroduce the
    partial deletion. It gets its own code that points to the plugin wrapper or
    the independent Windows runner, which run outside this runtime.
    """
    lineage = process_lineage({pid: parent for pid, parent, _name in processes}, current_pid)
    own = other = False
    for pid, _parent, name in processes:
        if name.lower() not in RUNTIME_PROCESS_NAMES:
            continue
        image = image_of(pid)
        if image is None or not image_inside(image, directory):
            continue
        if pid in lineage:
            own = True
        else:
            other = True
    if own:
        raise ReleaseError(
            "runtime_update_must_run_outside_runtime",
            details={"other_runtime_processes": other},
        )
    if other:
        raise ReleaseError("runtime_in_use_close_hosts_and_retry")


def check_windows_runtime_processes(directory: Path) -> None:
    """Compare image paths, not shared uv hardlinks or private command lines."""
    import ctypes
    from ctypes import wintypes

    class Entry(ctypes.Structure):
        _fields_ = [
            ("size", wintypes.DWORD),
            ("usage", wintypes.DWORD),
            ("pid", wintypes.DWORD),
            ("heap", ctypes.c_size_t),
            ("module", wintypes.DWORD),
            ("threads", wintypes.DWORD),
            ("parent", wintypes.DWORD),
            ("priority", wintypes.LONG),
            ("flags", wintypes.DWORD),
            ("exe", wintypes.WCHAR * 260),
        ]

    windows_ctypes: Any = ctypes
    kernel = windows_ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(Entry)]
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]

    def image_of(pid: int) -> str | None:
        process = kernel.OpenProcess(0x1000, False, pid)
        if not process:
            if windows_ctypes.get_last_error() in UNINSPECTABLE_PROCESS_ERRORS:
                return None
            raise ReleaseError("runtime_usage_check_failed")
        try:
            name, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if kernel.QueryFullProcessImageNameW(process, 0, name, ctypes.byref(size)):
                return str(name.value)
            if windows_ctypes.get_last_error() in UNINSPECTABLE_PROCESS_ERRORS:
                return None
            raise ReleaseError("runtime_usage_check_failed")
        finally:
            kernel.CloseHandle(process)

    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ctypes.c_void_p(-1).value:
        raise ReleaseError("runtime_usage_check_failed")
    processes: list[tuple[int, int, str]] = []
    entry = Entry()
    entry.size = ctypes.sizeof(entry)
    try:
        available = kernel.Process32FirstW(snapshot, ctypes.byref(entry))
        if not available:
            raise ReleaseError("runtime_usage_check_failed")
        while available:
            processes.append((int(entry.pid), int(entry.parent), str(entry.exe)))
            available = kernel.Process32NextW(snapshot, ctypes.byref(entry))
        if windows_ctypes.get_last_error() != 18:
            raise ReleaseError("runtime_usage_check_failed")
    finally:
        kernel.CloseHandle(snapshot)
    check_runtime_process_images(directory, processes, image_of, os.getpid())


def runtime_usage_resources(directory: Path) -> list[str]:
    """Private files of this runtime, as long-path-safe absolute paths.

    Walking, stat and Restart Manager registration all use the same
    filesystem_path() form as the snapshot copy, so files beyond the legacy
    260-character MAX_PATH are checked instead of failing the preflight.
    """
    root = filesystem_path(directory.absolute())
    resources = []
    for path in [root] if root.is_file() else root.rglob("*"):
        if _is_junction(path):
            raise ReleaseError("invalid_runtime_installation_path")
        # uv hardlinks shared cache files across environments. Another
        # environment loading one is not evidence that this one is running.
        if path.is_file() and not path.is_symlink() and path.stat().st_nlink == 1:
            resources.append(str(path))
    return resources


def check_runtime_usage(directory: Path) -> None:
    """Ask Windows about registered files; never stop or enumerate command lines."""
    if directory.is_symlink() or _is_junction(directory):
        raise ReleaseError("invalid_runtime_installation_path")
    if sys.platform != "win32" or not directory.exists():
        return
    check_windows_runtime_processes(directory)
    import ctypes
    from ctypes import wintypes

    windows_ctypes: Any = ctypes
    manager = windows_ctypes.WinDLL("Rstrtmgr")
    manager.RmStartSession.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        wintypes.LPWSTR,
    ]
    manager.RmRegisterResources.argtypes = [
        wintypes.DWORD,
        wintypes.UINT,
        ctypes.POINTER(wintypes.LPCWSTR),
        wintypes.UINT,
        ctypes.c_void_p,
        wintypes.UINT,
        ctypes.c_void_p,
    ]
    manager.RmGetList.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.UINT),
        ctypes.POINTER(wintypes.UINT),
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.DWORD),
    ]
    manager.RmEndSession.argtypes = [wintypes.DWORD]
    session, key = wintypes.DWORD(), ctypes.create_unicode_buffer(33)
    if manager.RmStartSession(ctypes.byref(session), 0, key):
        raise ReleaseError("runtime_usage_check_failed")
    try:
        resources = runtime_usage_resources(directory)
        for offset in range(0, len(resources), 256):
            batch = resources[offset : offset + 256]
            files = (wintypes.LPCWSTR * len(batch))(*batch)
            if manager.RmRegisterResources(session, len(batch), files, 0, None, 0, None):
                raise ReleaseError("runtime_usage_check_failed")
        needed, count, reasons = wintypes.UINT(), wintypes.UINT(), wintypes.DWORD()
        status = manager.RmGetList(
            session, ctypes.byref(needed), ctypes.byref(count), None, ctypes.byref(reasons)
        )
        if status not in {0, 234}:
            raise ReleaseError("runtime_usage_check_failed")
        if needed.value or count.value:
            raise ReleaseError("runtime_in_use_close_hosts_and_retry")
    finally:
        manager.RmEndSession(session)


def preflight_runtime() -> None:
    runtime, launcher = runtime_paths()
    check_runtime_usage(runtime)
    if legacy_runtime_owns_launcher():
        check_runtime_usage(runtime.parent / "malmoa-mcp")
    # A running copied launcher may live outside the tool environment.
    if sys.platform == "win32" and launcher.exists():
        if not launcher.is_symlink():
            check_runtime_usage(launcher)


def runtime_python(directory: Path) -> Path:
    return directory / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python3")


def installed_runtime_version(directory: Path) -> str | None:
    """Version of any release's runtime, old or current, or None if unusable.

    Uses only what every release provides (interpreter, top-level package and
    its metadata); no current-only module or CLI flag is assumed.
    """
    try:
        found = run(
            [
                str(runtime_python(directory)),
                "-I",
                "-c",
                "import durumari_mcp; "
                "from importlib.metadata import version; print(version('durumari-mcp'))",
            ]
        )
    except (ReleaseError, OSError):
        # run() reports a missing or dangling interpreter as ReleaseError.
        return None
    return found if re.fullmatch(VERSION, found) else None


def verify_previous_runtime(directory: Path, launcher: Path, version: str) -> None:
    """Version-appropriate check for an older release restored by rollback."""
    if installed_runtime_version(directory) != version:
        raise ReleaseError("runtime_version_not_verified")
    if not launcher.is_file():
        raise ReleaseError("runtime_launcher_not_verified")


def verify_installed_runtime(directory: Path, version: str) -> None:
    """Full check for the release this module belongs to (its modules and CLI)."""
    python = runtime_python(directory)
    imported = run(
        [
            str(python),
            "-I",
            "-c",
            "import durumari_mcp, durumari_mcp.cli, durumari_mcp.project_gateway; "
            "from importlib.metadata import version; print(version('durumari-mcp'))",
        ]
    )
    if (
        imported != version
        or run([str(python), "-I", "-m", "durumari_mcp", "--version"]) != version
    ):
        raise ReleaseError("runtime_version_not_verified")


def filesystem_path(path: Path) -> Path:
    """Windows snapshots must also preserve files beyond legacy MAX_PATH."""
    if sys.platform != "win32":
        return path
    value = str(path.absolute())
    if value.startswith("\\\\?\\"):
        return path
    return Path("\\\\?\\UNC\\" + value[2:] if value.startswith("\\\\") else "\\\\?\\" + value)


def runtime_update_directory(runtime: Path) -> Path:
    """Create and return the owned lock/snapshot root, never inside `uv tool dir`.

    uv treats every entry of the tool directory as a tool, so `uv tool list`
    fails on a foreign directory there, and uv tool cleanup could remove
    retained recovery snapshots. Rollback renames between the snapshot and the
    tool environment, so the root must be on the tool directory's filesystem.
    The sibling of `uv tool dir` is preferred; the Durumari state directory is
    the fallback (for example `UV_TOOL_DIR=/tools` with a read-only parent or a
    separate mount). Without either, replacement is refused before any change.
    """
    tools = runtime.parent
    tools.mkdir(parents=True, exist_ok=True)
    device = tools.stat().st_dev
    for owned in (tools.parent / "durumari-runtime-update", state_directory() / "runtime-update"):
        if owned.is_symlink() or _is_junction(owned):
            raise ReleaseError("invalid_runtime_installation_path")
        existed = owned.exists()
        try:
            owned.mkdir(parents=True, exist_ok=True)
            if owned.stat().st_dev == device and os.access(owned, os.W_OK):
                return owned
            if not existed:
                owned.rmdir()
        except OSError:
            continue
    raise ReleaseError("runtime_update_directory_unavailable_same_filesystem_required")


def interrupted_snapshot(snapshots: Path) -> bool:
    """Whether a run left a copy of the runtime environment or launcher it was replacing."""
    try:
        return any(entry.is_dir() and any(entry.iterdir()) for entry in snapshots.iterdir())
    except FileNotFoundError:
        return False
    except OSError:
        return True


def install_runtime_bundle(
    directory: Path, version: str, *, previous_release: bool = False
) -> None:
    """Preserve the old environment before uv's destructive in-place reinstall.

    previous_release marks a release rollback to an older bundle: that runtime
    predates this module's modules/CLI, so only the version-agnostic check runs.
    """
    wheel, constraints = verify_runtime(directory, version)
    runtime, launcher = runtime_paths()
    preflight_runtime()
    owned = runtime_update_directory(runtime)
    try:
        # Pre-release 0.4.7 builds left this inside `uv tool dir`; only an empty
        # one is removed, so a retained recovery snapshot is never touched.
        (runtime.parent / ".durumari-runtime-snapshots").rmdir()
    except OSError:
        pass
    lock = owned / "update.lock"
    snapshots = owned / "snapshots"
    # A stopped run that had already copied the runtime may have left it half replaced; its
    # snapshot is the good copy, so that case still stops for recovery instead of moving on.
    if lock.exists() and interrupted_snapshot(snapshots):
        raise ReleaseError("runtime_update_already_running_or_recovery_required")
    take_lock(lock, os.getpid(), "runtime_update_already_running_or_recovery_required")
    snapshot = snapshots / uuid4().hex
    old_version = None
    snapshot_created = changed = succeeded = False
    # Whether an old environment or launcher was copied; without one a failed
    # first install has nothing to restore or recover.
    preserved = discard_failed_snapshot = False
    try:
        if snapshots.is_symlink() or _is_junction(snapshots):
            raise ReleaseError("invalid_runtime_installation_path")
        snapshots.mkdir(exist_ok=True)
        snapshot.mkdir()
        snapshot_created = True
        rename_options = ["--force"] if legacy_runtime_owns_launcher() else []
        if runtime.exists():
            shutil.copytree(
                filesystem_path(runtime),
                filesystem_path(snapshot / "environment"),
                symlinks=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            preserved = True
            # The old environment may be any earlier release: identify it only
            # by what every release provides. None means an unavailable snapshot.
            old_version = installed_runtime_version(runtime)
        if launcher.exists() or launcher.is_symlink():
            shutil.copy2(
                filesystem_path(launcher),
                filesystem_path(snapshot / "launcher"),
                follow_symlinks=False,
            )
            preserved = True
        # Check again after snapshotting; a new process may have started meanwhile.
        preflight_runtime()
        changed = True
        run(
            [
                uv_command(),
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
        if previous_release:
            verify_previous_runtime(runtime, launcher, version)
        else:
            verify_installed_runtime(runtime, version)
            if run([str(launcher), "--version"]) != version:
                raise ReleaseError("runtime_launcher_not_verified")
        # A local flag, not sys.exc_info(): callers may run this inside their own
        # except block (release rollback), where exc_info reports their exception.
        succeeded = True
    except BaseException as error:
        if changed:
            try:
                # Move failed files aside first. No old environment is deleted.
                if runtime.exists():
                    filesystem_path(runtime).rename(
                        filesystem_path(snapshot / "failed-environment")
                    )
                if (snapshot / "environment").exists():
                    filesystem_path(snapshot / "environment").rename(filesystem_path(runtime))
                if launcher.exists() or launcher.is_symlink():
                    launcher.unlink()
                if (snapshot / "launcher").exists() or (snapshot / "launcher").is_symlink():
                    shutil.copy2(
                        filesystem_path(snapshot / "launcher"),
                        filesystem_path(launcher),
                        follow_symlinks=False,
                    )
                if old_version is not None:
                    verify_previous_runtime(runtime, launcher, old_version)
            except (OSError, ReleaseError):
                raise ReleaseError(
                    "runtime_rollback_requires_recovery",
                    details={
                        "snapshot_id": snapshot.name,
                        "runtime_rollback": "requires_recovery",
                    },
                ) from None
            if not preserved or old_version is not None:
                # Nothing left to recover: either a first install whose partial
                # files were removed, or a previous runtime that was restored
                # and passed verification. Only an unverifiable restore
                # (restored_unavailable_snapshot) or a failed rollback
                # (requires_recovery) keeps its snapshot.
                discard_failed_snapshot = True
            if isinstance(error, ReleaseError):
                error.details["runtime_rollback"] = (
                    "not_installed"
                    if not preserved
                    else "restored"
                    if old_version
                    else "restored_unavailable_snapshot"
                )
        raise
    finally:
        # Kept only when recovery may still need it (see above). A completed
        # install, a refusal before any change (the runtime is untouched), a
        # rolled-back first install or a verified restore removes its copy.
        if snapshot_created and (succeeded or not changed or discard_failed_snapshot):
            try:
                shutil.rmtree(filesystem_path(snapshot))
            except OSError:
                pass
        release_lock(lock, {os.getpid()})


def cursor_plugin_directory() -> Path:
    # Cursor's documented local plugin location; not a private marketplace cache.
    return Path.home() / ".cursor/plugins/local/durumari"


def cursor_plugin_version(directory: Path) -> str | None:
    """Return the version only for a real directory that is this exact plugin."""
    manifest = directory / ".cursor-plugin/plugin.json"
    if directory.is_symlink() or not directory.is_dir() or manifest.is_symlink():
        return None
    try:
        item = json.loads(manifest.read_text(encoding="utf-8"))
        version = item["version"]
        if item["name"] != "durumari" or not re.fullmatch(VERSION, version):
            return None
        return cast(str, version)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def install_cursor(catalog: Path, version: str) -> None:
    """Replace only Durumari's own local Cursor plugin; other plugins stay untouched."""
    source = catalog / "plugins/durumari-agent"
    target = cursor_plugin_directory()
    if (target.exists() or target.is_symlink()) and cursor_plugin_version(target) is None:
        raise ReleaseError("cursor_plugin_directory_not_owned")
    if cursor_plugin_version(source) != version:
        raise ReleaseError("native_plugin_version_not_verified")
    target.parent.mkdir(parents=True, exist_ok=True)
    suffix = uuid4().hex
    staged = target.with_name(f".durumari-stage-{suffix}")
    retired = target.with_name(f".durumari-previous-{suffix}")
    try:
        shutil.copytree(source, staged, symlinks=False)
        if target.exists():
            target.rename(retired)
        staged.rename(target)
    except OSError:
        if retired.exists() and not target.exists():
            retired.rename(target)
        shutil.rmtree(staged, ignore_errors=True)
        raise ReleaseError("cursor_plugin_install_failed") from None
    shutil.rmtree(retired, ignore_errors=True)
    if cursor_plugin_version(target) != version:
        raise ReleaseError("native_plugin_version_not_verified")


def gemini_extension_directory() -> Path:
    # Gemini CLI installs extensions under <GEMINI_CLI_HOME or home>/.gemini/extensions/<name>.
    home = os.environ.get("GEMINI_CLI_HOME") or str(Path.home())
    return Path(home) / ".gemini/extensions/durumari"


def gemini_extension_version(directory: Path) -> str | None:
    manifest = directory / "gemini-extension.json"
    if directory.is_symlink() or manifest.is_symlink() or not manifest.is_file():
        return None
    try:
        item = json.loads(manifest.read_text(encoding="utf-8"))
        version = item["version"]
        if item["name"] != "durumari" or not re.fullmatch(VERSION, version):
            return None
        return cast(str, version)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def stdin_is_terminal() -> bool:
    """True only when a person can answer a prompt on stdin.

    Python 3.12 on Windows reports the NUL device (the detached runner's
    ``stdin=DEVNULL``) as a tty, so a plain ``isatty()`` let Gemini CLI start its consent prompt
    in a hidden process where nobody can answer, and the update hung. A Windows console handle
    must also accept ``GetConsoleMode``.
    """
    stdin = sys.stdin
    if not stdin or not stdin.isatty():
        return False
    if sys.platform != "win32":
        return True
    try:
        import ctypes
        import msvcrt
        from ctypes import wintypes

        handle = msvcrt.get_osfhandle(stdin.fileno())
        mode = wintypes.DWORD()
        windll: Any = getattr(ctypes, "windll")  # noqa: B009 - absent off Windows
        return bool(windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode)))
    except (AttributeError, OSError, ValueError):
        # No real file descriptor (an in-memory stand-in): trust isatty().
        return True


def run_with_host_consent(command: list[str]) -> None:
    """Gemini CLI asks the human to confirm extension contents; never answer for them.

    Host output goes to stderr so this command's JSON result on stdout stays parseable.
    """
    if not stdin_is_terminal():
        # Gemini CLI waits indefinitely for an answer that a pipe or Agent cannot give.
        raise ReleaseError("gemini_consent_requires_interactive_terminal")
    try:
        result = subprocess.run(
            launchable(command), stdout=sys.stderr, stderr=sys.stderr, timeout=900, check=False
        )
    except subprocess.TimeoutExpired:
        raise ReleaseError(
            "release_install_command_timeout",
            details={"command": command[:3], "timeout_seconds": 900},
        ) from None
    except OSError as error:
        raise ReleaseError(
            "release_install_command_unavailable",
            details={"command": command[:3], "errno": error.errno},
        ) from None
    if result.returncode:
        raise ReleaseError(
            "release_install_command_failed",
            details={"command": command[:3], "returncode": result.returncode},
        )


def install_gemini(catalog: Path, version: str) -> None:
    source = catalog / "plugins/durumari-agent"
    if gemini_extension_version(source) != version:
        raise ReleaseError("native_plugin_version_not_verified")
    target = gemini_extension_directory()
    if target.exists() or target.is_symlink():
        if gemini_extension_version(target) is None:
            raise ReleaseError("gemini_extension_directory_not_owned")
        try:
            metadata = json.loads(
                (target / ".gemini-extension-install.json").read_text(encoding="utf-8")
            )
            installed_source = Path(metadata["source"]).resolve()
            local = metadata["type"] == "local"
        except (OSError, ValueError, KeyError, TypeError):
            raise ReleaseError("gemini_extension_directory_not_owned") from None
        if not local or installed_source != source.resolve():
            # A linked or Git installation belongs to the human; do not migrate it silently.
            raise ReleaseError("gemini_extension_installed_from_other_source")
        if gemini_extension_version(target) != version:
            run_with_host_consent(["gemini", "extensions", "update", "durumari"])
    else:
        run_with_host_consent(["gemini", "extensions", "install", str(source)])
    # `extensions update` can exit 0 without changes; trust only the installed manifest.
    if gemini_extension_version(target) != version:
        raise ReleaseError("native_plugin_version_not_verified")


def install_host(client: str, catalog: Path, version: str) -> None:
    # Hosts own their trust and native plugin lifecycle. No private cache edits.
    if client == "antigravity":
        agy = host_command(client)
        run([agy, "plugin", "install", str(catalog / "plugins/durumari-agent")])
        try:
            rows = json.loads(run([agy, "plugin", "list", "--json"]))
            if not isinstance(rows, list) or not any(
                isinstance(row, dict)
                and row.get("name") == "durumari"
                and row.get("version") == version
                and row.get("enabled") is True
                for row in rows
            ):
                raise ValueError
        except (ValueError, TypeError):
            raise ReleaseError("native_plugin_version_not_verified") from None
        return
    if client == "gemini":
        install_gemini(catalog, version)
        return
    if client == "cursor":
        install_cursor(catalog, version)
        return
    if client == "codex":
        run(["codex", "plugin", "marketplace", "add", str(catalog), "--json"])
        run(["codex", "plugin", "add", "durumari@durumari", "--json"])
        output = run(["codex", "plugin", "list", "--marketplace", "durumari", "--json"])
    else:
        claude = host_command("claude")
        run([claude, "plugin", "marketplace", "add", str(catalog)])
        run([claude, "plugin", "install", "durumari@durumari", "--scope", "user"])
        run([claude, "plugin", "update", "durumari@durumari", "--scope", "user"])
        output = run([claude, "plugin", "list", "--json"])
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
    command = [host_command(client), "plugin", "list", "--json"]
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
            run(
                [command[0], "plugin", "uninstall", LEGACY_PLUGIN, "--scope", "user", "--keep-data"]
            )


# Host names for people; the public one-line install covers each one found here.
HOST_NAMES = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "gemini": "Gemini CLI"}


def cursor_present() -> bool:
    """Cursor may have no CLI on PATH; its user folder or app marks an installation."""
    if shutil.which("cursor") or (Path.home() / ".cursor").is_dir():
        return True
    if sys.platform == "darwin":
        return Path("/Applications/Cursor.app").is_dir()
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        return (local / "Programs/cursor").is_dir()
    return False


def claude_version_key(path: Path) -> tuple[int, ...]:
    return tuple(int(part) if part.isdigit() else -1 for part in path.name.split("."))


def bundled_claude() -> str | None:
    """Claude Code that is not on PATH: the Claude app's bundled copy or the native installer's.

    The Claude app keeps Claude Code under its data folder, one directory per version, and that
    copy reads the same user-scope plugins (~/.claude) as the CLI.
    """
    home = Path.home()
    # A plain str keeps every platform's branch type-checked wherever mypy runs.
    platform: str = sys.platform
    exe = "claude.exe" if platform == "win32" else "claude"
    native = [home / ".local/bin" / exe, home / ".claude/local" / exe]
    roots: list[Path] = []
    patterns: tuple[str, ...] = ()
    if platform == "darwin":
        roots.append(home / "Library/Application Support/Claude/claude-code")
        patterns = ("*/claude.app/Contents/MacOS/claude", "claude.app/Contents/MacOS/claude")
    elif platform == "win32":
        roaming = Path(os.environ.get("APPDATA", str(home / "AppData/Roaming")))
        local = Path(os.environ.get("LOCALAPPDATA", str(home / "AppData/Local")))
        roots.append(roaming / "Claude/claude-code")
        # The Microsoft Store (MSIX) build redirects AppData into its package folder.
        roots.extend(sorted(local.glob("Packages/*Claude*/LocalCache/Roaming/Claude/claude-code")))
        # Newer Store builds keep user data outside the package, under Claude-Data\claude_<id>
        # (seen with app 3.6556 on Windows).
        roots.extend(sorted(local.glob("Claude-Data/claude_*/claude-code")))
        patterns = ("claude.exe", "*/claude.exe", "*/*/claude.exe")
    for root in roots:
        if not root.is_dir():
            continue
        versions = [item for item in root.glob("*") if item.is_dir()]
        for version in sorted(versions, key=claude_version_key, reverse=True):
            for pattern in patterns:
                for candidate in sorted(version.glob(pattern)):
                    if candidate.is_file() and os.access(candidate, os.X_OK):
                        return str(candidate)
    return next((str(path) for path in native if path.is_file() and os.access(path, os.X_OK)), None)


def antigravity_command() -> str | None:
    """Official Antigravity CLI, including a fresh install outside the shell PATH."""
    found = shutil.which("agy")
    if found:
        return found
    if sys.platform == "win32":
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        candidates = [local / "agy/bin/agy.exe"]
    else:
        candidates = [Path.home() / ".local/bin/agy"]
    return next(
        (str(path) for path in candidates if path.is_file() and os.access(path, os.X_OK)), None
    )


def host_command(client: str) -> str:
    """The executable that runs a host's plugin commands."""
    if client == "antigravity":
        return antigravity_command() or "agy"
    if client == "claude" and not shutil.which("claude"):
        return bundled_claude() or "claude"
    return client


def detect_clients() -> set[str]:
    """Agents installed on this computer; an install without --client sets up all of them."""
    found = {name for name in ("claude", "codex", "gemini") if shutil.which(name)}
    if "claude" not in found and bundled_claude():
        found.add("claude")
    if cursor_present():
        found.add("cursor")
    if antigravity_command():
        found.add("antigravity")
    return found


def restore_interrupted_catalog(catalog: Path, backup: Path, previous: dict[str, Any]) -> None:
    """Undo a catalog switch an interrupted run left behind; the new copy is kept aside.

    `previous` exists only between moving the old catalog aside and finishing, so finding it
    under a lock this run holds means that run stopped. This run installs the runtime and
    hosts again.
    """
    if backup.exists():
        if catalog.exists():
            set_aside(catalog)
        backup.rename(catalog)
        note_runner("restored the catalog an interrupted update had replaced")
    elif catalog.exists() and not previous:
        # A first installation stopped before recording itself.
        set_aside(catalog)


def execute_release(args: argparse.Namespace) -> dict[str, Any]:
    directory = state_directory()
    previous = load_state(directory)
    supplied_origin = getattr(args, "server", None)
    origin = installed_origin(previous, supplied_origin)
    # The installation keeps its stored origin; downloads go where the server points.
    base = download_origin(origin)
    release = parse_release(
        download_with_fallback(base, origin, "/downloads/latest.json", 32 * 1024)
    )
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
    # A first install, or a rerun after another Agent was installed, without --client covers every
    # Agent found here. An update keeps exactly the Agents it installed before.
    detected = not client and getattr(args, "command", "update") == "install"
    if detected:
        clients |= detect_clients()
    if not clients:
        raise ReleaseError("release_no_supported_agent" if detected else "release_client_required")
    # Gemini CLI asks its own question that a pipe cannot answer; a found Gemini is left for a
    # terminal run instead of failing every other Agent. Asked for by name, it still fails clearly.
    # This holds for an update too: it re-installs every recorded Agent, so `update --client
    # claude` from the detached runner would otherwise reach Gemini's consent prompt and hang.
    skipped: list[str] = []
    if (
        client != "gemini"
        and "gemini" in clients
        and not stdin_is_terminal()
    ):
        clients.discard("gemini")
        skipped.append("gemini")
        if not clients:
            raise ReleaseError("gemini_consent_requires_interactive_terminal")
    # An Agent whose command is not found now (removed, or not on this shell's PATH) is skipped
    # this time without --client, so the others still update. It stays recorded and is updated
    # again by the next run that finds it.
    removed: list[str] = []
    if not client:
        removed = sorted(
            name for name in clients - LOCAL_DIRECTORY_HOSTS if not shutil.which(host_command(name))
        )
        if len(removed) < len(clients):
            clients.difference_update(removed)
        else:
            removed = []
    # Hosts installed by a documented local directory copy need no host command.
    for name in [uv_command(), *sorted(clients - LOCAL_DIRECTORY_HOSTS)]:
        if not shutil.which(host_command(name)):
            raise ReleaseError("release_required_command_missing")
    data = download_with_fallback(base, origin, release.path, MAX_ARCHIVE)
    if len(data) != release.size or hashlib.sha256(data).hexdigest() != release.sha256:
        raise ReleaseError("release_archive_integrity_failed")
    directory.mkdir(parents=True, exist_ok=True)
    catalog, backup = directory / "catalog", directory / "previous"
    lock = directory / "update.lock"
    take_lock(lock, os.getpid(), "release_update_already_running")
    try:
        # Another run may have finished while this one downloaded; decide from the state the
        # lock now protects, for recovery and for any rollback below.
        previous = load_state(directory)
        restore_interrupted_catalog(catalog, backup, previous)
    except (OSError, ReleaseError) as error:
        release_lock(lock, {os.getpid()})
        if isinstance(error, ReleaseError):
            raise
        raise ReleaseError("release_recovery_required") from None
    attempt_id = uuid4().hex
    started_at = datetime.now(UTC).isoformat()
    switched = False
    stage = "extract_catalog"
    try:
        with tempfile.TemporaryDirectory(prefix="stage-", dir=directory) as temporary:
            staged = extract_catalog(data, Path(temporary), release.version)
            bundle = json.loads((staged / "plugins/durumari-agent/runtime/bundle.json").read_text())
            if "service" in bundle and not same_server(parse_service(bundle["service"]), origin):
                raise ReleaseError("release_service_origin_mismatch")
            stage = "runtime_preflight"
            preflight_runtime()
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
                # A Gemini installed earlier from a terminal, or an Agent not found this time,
                # stays recorded when skipped now.
                "clients": sorted(
                    clients | ((set(skipped) | set(removed)) & set(previous.get("clients", [])))
                ),
            }
            pending = directory / "installation.tmp"
            pending.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
            pending.replace(directory / "installation.json")
        result: dict[str, Any] = {
            "installed": release.version,
            "clients": sorted(clients),
            "restart_hosts": True,
        }
        if skipped:
            result["skipped"] = skipped
        if removed:
            result["not_found"] = removed
        for host in sorted(clients & LEGACY_PLUGIN_HOSTS):
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
                if failed.exists():
                    # Retain old failure evidence instead of letting it block this rollback.
                    failed = directory / ("failed-" + attempt_id)
                if not failed.exists():
                    try:
                        catalog.rename(failed)
                        backup.rename(catalog)
                        if stage != "install_runtime":
                            install_runtime(catalog, previous["version"], previous_release=True)
                            for host in previous["clients"]:
                                install_host(host, catalog, previous["version"])
                        elif str(error) == "runtime_rollback_requires_recovery":
                            raise error
                        shutil.rmtree(failed)
                        report["rollback"] = (
                            "restored_catalog_runtime_unavailable"
                            if isinstance(error, ReleaseError)
                            and error.details.get("runtime_rollback")
                            in {"restored_unavailable_snapshot", "not_installed"}
                            else "restored"
                        )
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
        # The stage and cause let the one-line install name the Agent and what went wrong.
        failure = ReleaseError(
            "release_install_failed_retry_or_restore",
            details={"stage": stage, "cause": failure_details(error)["error"]},
        )
        failure.diagnostic_path = write_failure_report(directory, report, debug)
        raise failure from None
    finally:
        release_lock(lock, {os.getpid()})


class Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise ReleaseError("invalid_release_arguments")


def summary_lines(result: dict[str, Any], origin: str) -> list[str]:
    """What the one-line install prints for a person: what was set up and what to do next."""
    names = ", ".join(HOST_NAMES.get(name, name) for name in result.get("clients", []))
    lines = [f"두루마리 {result.get('installed')}을(를) 설치했습니다: {names}"]
    if "gemini" in result.get("skipped", []):
        lines.append(
            "Gemini CLI는 설치 중 직접 확인이 필요해 건너뛰었습니다. "
            "터미널에서 같은 명령에 --client gemini를 붙여 실행하세요."
        )
    removed = ", ".join(HOST_NAMES.get(name, name) for name in result.get("not_found", []))
    if removed:
        lines.append(
            f"{removed}을(를) 찾지 못해 이번에는 건너뛰었습니다. "
            "다시 설치하거나 PATH에 추가한 뒤 같은 명령을 실행하면 함께 업데이트됩니다."
        )
    lines += [
        "",
        "다음 할 일",
        "  1. 실행 중인 Agent를 다시 시작하거나 새 세션을 엽니다. "
        "Cursor는 Developer: Reload Window를 실행합니다.",
        "  2. 연결할 저장소 폴더에서 Agent에게 “두루마리 연결해 줘”라고 말합니다.",
        f"Chat(ChatGPT·Claude)에서도 쓰려면: {origin}/downloads#chat",
    ]
    return lines


FAILURE_TEXT = {
    "release_no_supported_agent": "이 컴퓨터에서 Claude Code·Codex·Cursor·Gemini CLI를 "
    "찾지 못했습니다. Agent를 설치한 뒤 다시 실행하세요.",
    "release_required_command_missing": "필요한 명령을 찾지 못했습니다. "
    "Agent CLI와 uv가 설치되어 있는지 확인하세요.",
    "release_update_already_running": "다른 설치가 진행 중입니다. 1분쯤 뒤 다시 실행하세요. "
    "중단된 설치가 남긴 잠금은 다음 실행이 자동으로 옆에 옮겨 둡니다.",
    "release_runner_stopped": "설치 프로그램이 끝나기 전에 꺼졌습니다. 같은 update 명령을 "
    "다시 실행하면 남은 잠금을 옮기고 이어서 설치합니다. 경과는 update-runner.log에 있습니다.",
    "release_recovery_required": "중단된 이전 설치를 되돌리지 못했습니다. Agent를 모두 닫고 "
    "다시 실행하세요. 계속되면 설치 폴더의 update-runner.log를 함께 알려 주세요.",
    "gemini_consent_requires_interactive_terminal": "Gemini CLI는 설치 중 직접 확인이 "
    "필요합니다. 터미널에서 직접 실행하세요.",
    # Windows cannot replace a running runtime image; say what to close.
    "runtime_in_use_close_hosts_and_retry": "두루마리를 쓰는 Agent가 실행 중이라 교체할 수 "
    "없습니다. Cursor·Claude Code·Codex·Gemini CLI 창을 모두 닫은 뒤 다시 실행하세요.",
    "runtime_update_must_run_outside_runtime": "두루마리 런타임 안에서 실행해 교체할 수 "
    "없습니다. Agent를 모두 닫고 새 PowerShell 창에서 설치 명령을 다시 실행하세요.",
    "runtime_usage_check_failed": "실행 중인 프로그램을 확인하지 못했습니다. Agent를 모두 "
    "닫은 뒤 다시 실행하세요.",
    "release_install_path_unsupported": "설치 경로에 Windows 명령이 처리할 수 없는 문자"
    "(%, &, ^ 등)가 있어 Agent 플러그인을 설치하지 못했습니다.",
}


def failure_reason(error: ReleaseError | OSError, code: str) -> str | None:
    """A sentence for a person; an Agent's own failure names that Agent and how to skip it."""
    details = error.details if isinstance(error, ReleaseError) else {}
    if code != "release_install_failed_retry_or_restore":
        return FAILURE_TEXT.get(code)
    # A known cause says what to fix; retrying or skipping the Agent would not help.
    if details.get("cause") in FAILURE_TEXT:
        return FAILURE_TEXT[details["cause"]]
    stage = details.get("stage", "")
    host = stage.removeprefix("install_host_") if stage.startswith("install_host_") else ""
    if host not in HOST_NAMES:
        return None
    reason = f"{HOST_NAMES[host]}에 플러그인을 설치하지 못했습니다."
    if sys.platform == "win32":
        reason += (
            " 다른 Agent만 먼저 설치하려면 $env:DURUMARI_MCP_CLIENT에 설치할 Agent"
            "(claude·codex·cursor·gemini)를 지정해 다시 실행하세요."
        )
    return reason


def main() -> None:
    parser = Parser(
        description="Install or update the Durumari native plugin from an HTTPS release."
    )
    parser.add_argument("command", choices=["install", "update"])
    parser.add_argument("--server")
    parser.add_argument("--client", choices=["codex", "claude", "both", "cursor", "gemini"])
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
    # The public one-line install (install.sh / install.ps1) prints sentences instead of JSON.
    parser.add_argument("--summary", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--wait-for-pid", type=int, help=argparse.SUPPRESS)
    summary = False
    try:
        args = parser.parse_args()
        summary = args.summary
        if args.wait_for_pid is not None:
            raise SystemExit(run_independent_update(args))
        result = update_status() if args.status else execute_release(args)
        if summary and "clients" in result:
            origin = load_state(state_directory()).get("origin", "")
            print("\n".join(summary_lines(result, origin)))
        else:
            print(json.dumps(result))
    except (ReleaseError, OSError) as error:
        code = str(error) if isinstance(error, ReleaseError) else "release_filesystem_error"
        if summary:
            reason = failure_reason(error, code)
            print(
                f"두루마리를 설치하지 못했습니다. {reason + ' ' if reason else ''}({code})",
                file=sys.stderr,
            )
        else:
            print(code, file=sys.stderr)
        if isinstance(error, ReleaseError) and error.diagnostic_path:
            print(json.dumps({"diagnostic_path": error.diagnostic_path}), file=sys.stderr)
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
