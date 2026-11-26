"""Read immutable Skill objects without running repository code or borrowing credentials.

Public reads implement GitHub git trees/blobs and GitLab repository tree/raw blobs:
https://docs.github.com/en/rest/git/trees
https://docs.github.com/en/rest/git/blobs
https://docs.github.com/en/rest/commits/commits
https://docs.gitlab.com/api/repositories/
https://docs.gitlab.com/api/commits/#retrieve-a-commit
Private reads use an existing matching checkout, never clone, fetch, filters or hooks.
"""

import hashlib
import json
import os
import re
import shutil
import ssl
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

import yaml

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_skills import bundle_digest, validate_bundle
from durumari_mcp.skill_files import SkillFile, encode_file, safe_file_path

MAX_FILES = 100
MAX_FILE_BYTES = 128_000
MAX_TOTAL_BYTES = 512_000
MAX_HTTP_BYTES = 2_000_000
MAX_RESPONSE_BYTES = 1_000_000
MAX_REQUESTS = 120
MAX_TREE_ENTRIES = 1000
MAX_SECONDS = 30.0
OID = r"(?:[a-f0-9]{40}|[a-f0-9]{64})"


def normalize_repository_url(url: str) -> str:
    """Canonical HTTPS repository origin/path, never a browsing or credential URL."""
    try:
        if (
            not isinstance(url, str)
            or len(url) > 600
            or any(c.isspace() for c in url)
            or any(c in url for c in "\\%?#")
            or any(ord(c) < 33 for c in url)
        ):
            raise ValueError
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host)
            or ".." in host
            or parsed.netloc.endswith(":")
            or not parsed.path.startswith("/")
        ):
            raise ValueError
        parts = parsed.path.removesuffix("/").split("/")[1:]
        if not 2 <= len(parts) <= 16 or any(
            not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in {".", "..", "-"} or p.startswith(".")
            for p in parts
        ):
            raise ValueError
        parts[-1] = re.sub(r"\.git$", "", parts[-1], flags=re.I)
        if not parts[-1] or parts[-1] in {".", ".."}:
            raise ValueError
        host = host.lower()
        if host == "github.com":
            if len(parts) != 2:
                raise ValueError
            parts = [part.lower() for part in parts]
        return "https://" + host + "/" + "/".join(parts)
    except (ValueError, TypeError, AttributeError):
        raise AdapterError("invalid_git_repository_url") from None


def _path(path: str) -> str:
    if not isinstance(path, str) or len(path) > 200 or len(path.split("/")) > 16:
        raise AdapterError("invalid_git_path")
    if path and any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", p) or p in {".", ".."} for p in path.split("/")
    ):
        raise AdapterError("invalid_git_path")
    return path


def _ref(ref: str) -> str:
    if (
        not isinstance(ref, str)
        or len(ref) > 200
        or (
            ref
            and (
                not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", ref)
                or ".." in ref
                or "//" in ref
                or ref.endswith(("/", ".", ".lock"))
            )
        )
    ):
        raise AdapterError("invalid_git_ref")
    return ref


def _oid(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch(OID, value):
        raise AdapterError("git_tree_invalid")
    return value


def skill_name(files: dict[str, SkillFile]) -> str:
    """Read a bounded safe frontmatter; no repository content is executed."""
    try:
        text = files["SKILL.md"]
        if not isinstance(text, str) or len(text.encode()) > MAX_FILE_BYTES:
            raise ValueError
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.S)
        if not match or len(match.group(1).encode()) > 8192:
            raise ValueError
        if any(
            isinstance(t, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
            for t in yaml.scan(match.group(1))
        ):
            raise ValueError
        header = yaml.safe_load(match.group(1))
        name = header.get("name") if isinstance(header, dict) else None
        if (
            not isinstance(name, str)
            or len(name) > 64
            or not re.fullmatch(
                r"[a-z0-9]+(?:-[a-z0-9]+)*",
                name,
            )
        ):
            raise ValueError
        return name
    except (KeyError, ValueError, TypeError, AttributeError, yaml.YAMLError, RecursionError):
        raise AdapterError("invalid_git_skill") from None


def _portable(path: str, *, directory: bool = False) -> None:
    try:
        safe_file_path(path)
    except AdapterError:
        raise AdapterError("git_tree_invalid") from None
    if directory and path == "SKILL.md":
        raise AdapterError("git_tree_invalid")


def _text(data: bytes, expected_oid: str, path: str = "SKILL.md") -> SkillFile:
    if len(data) > MAX_FILE_BYTES:
        raise AdapterError("git_import_limit")
    try:
        digest = hashlib.sha1 if len(expected_oid) == 40 else hashlib.sha256
        if digest(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != expected_oid:
            raise ValueError
        if data.startswith(b"version https://git-lfs.github.com/spec/v1"):
            raise ValueError
        return encode_file(path, data)
    except (UnicodeError, ValueError, AdapterError):
        raise AdapterError("invalid_git_skill") from None


def _finish(
    files: dict[str, SkillFile],
    repository_url: str,
    ref: str,
    path: str,
    commit: str,
    transport: str,
) -> dict[str, Any]:
    row = {"name": skill_name(files), "version": "0.0.0", "files": files, "requirements": []}
    row["sha256"] = bundle_digest(row)
    validate_bundle(row)
    return {
        "files": files,
        "source": {
            "repository_url": repository_url,
            "path": path,
            "requested_ref": ref or None,
            "commit": commit,
            "transport": transport,
        },
    }


@dataclass
class Budget:
    deadline: float = field(default_factory=lambda: time.monotonic() + MAX_SECONDS)
    requests: int = 0
    bytes: int = 0

    def remaining(self) -> float:
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise AdapterError("git_import_limit")
        return min(10.0, value)

    def request(self) -> None:
        self.remaining()
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise AdapterError("git_import_limit")

    def add(self, size: int) -> None:
        self.bytes += size
        if self.bytes > MAX_HTTP_BYTES:
            raise AdapterError("git_import_limit")
        self.remaining()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class PublicAPI:
    def __init__(self, origin: str) -> None:
        if origin not in {"https://api.github.com", "https://gitlab.com"}:
            raise AdapterError("checkout_required")
        self.origin = origin
        self.budget = Budget()
        self.opener = build_opener(
            ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
        )

    def raw(self, path: str, *, limit: int = MAX_RESPONSE_BYTES) -> tuple[bytes, Any]:
        return self._raw(self.origin, path, limit)

    def github_file(self, repository_url: str, commit: str, folder: str, name: str) -> bytes:
        repository = normalize_repository_url(repository_url)
        if self.origin != "https://api.github.com" or urlsplit(repository).hostname != "github.com":
            raise AdapterError("checkout_required")
        pinned = _oid(commit)
        selected = _path(folder)
        safe_file_path(name)
        path = (
            urlsplit(repository).path
            + "/"
            + pinned
            + "/"
            + "/".join(p for p in [selected, name] if p)
        )
        data, _ = self._raw("https://raw.githubusercontent.com", path, MAX_FILE_BYTES)
        return data

    def _raw(self, origin: str, path: str, limit: int) -> tuple[bytes, Any]:
        # Origins are fixed here; filenames and server-supplied download URLs can
        # never introduce another host. Every content byte is checked against its
        # pinned Git tree object below. Raw content avoids one API request per blob.
        if origin != self.origin and not (
            self.origin == "https://api.github.com"
            and origin == "https://raw.githubusercontent.com"
        ):
            raise AdapterError("checkout_required")
        self.budget.request()
        request = Request(
            origin + path,
            headers={
                "Accept": "application/json",
                "User-Agent": "Durumari-Skill-Import",
            },
            method="GET",
        )
        try:
            with self.opener.open(request, timeout=self.budget.remaining()) as response:
                if response.status != 200:
                    raise AdapterError("git_fetch_failed")
                data = bytearray()
                while len(data) <= limit:
                    chunk = response.read(min(65536, limit + 1 - len(data)))
                    if not chunk:
                        break
                    self.budget.add(len(chunk))
                    data.extend(chunk)
                if len(data) > limit:
                    raise AdapterError("git_import_limit")
                self.budget.remaining()
                return bytes(data), response.headers
        except HTTPError as error:
            if error.code == 429 or (
                self.origin == "https://api.github.com"
                and error.code == 403
                and error.headers is not None
                and error.headers.get("X-RateLimit-Remaining") == "0"
            ):
                code = "git_fetch_rate_limited"
            elif 300 <= error.code < 400:
                code = "git_redirect_denied"
            elif error.code in {401, 403, 404}:
                code = "checkout_required"
            else:
                code = "git_fetch_failed"
            raise AdapterError(code) from None
        except (URLError, TimeoutError, OSError):
            raise AdapterError("git_fetch_failed") from None

    def json(self, path: str) -> tuple[Any, Any]:
        raw, headers = self.raw(path)
        try:
            return json.loads(raw), headers
        except (ValueError, UnicodeError, RecursionError):
            raise AdapterError("git_tree_invalid") from None


def _tree_rows(value: Any) -> list[dict[str, Any]]:
    if (
        not isinstance(value, list)
        or len(value) > MAX_TREE_ENTRIES
        or any(not isinstance(row, dict) for row in value)
    ):
        raise AdapterError("git_tree_invalid")
    return value


def _github_tree(api: PublicAPI, root: str, sha: str, *, recursive: bool) -> list[dict[str, Any]]:
    data, _ = api.json(root + "/git/trees/" + sha + ("?recursive=1" if recursive else ""))
    if not isinstance(data, dict) or data.get("sha") != sha or data.get("truncated") is not False:
        raise AdapterError("git_tree_invalid")
    return _tree_rows(data.get("tree"))


def _selected_entry(entries: list[dict[str, Any]], name: str) -> dict[str, Any]:
    paths = [entry.get("path") for entry in entries]
    if any(not isinstance(p, str) for p in paths) or len(set(paths)) != len(paths):
        raise AdapterError("git_tree_invalid")
    chosen = [e for e in entries if e["path"] == name]
    if len(chosen) != 1 or chosen[0].get("mode") != "040000" or chosen[0].get("type") != "tree":
        raise AdapterError("invalid_git_path")
    return chosen[0]


def _files(entries: list[dict[str, Any]], prefix: str = "") -> list[tuple[str, str, int | None]]:
    results = []
    seen = set()
    declared = 0
    for entry in entries:
        path = entry.get("path")
        if not isinstance(path, str) or (prefix and not path.startswith(prefix + "/")):
            raise AdapterError("git_tree_invalid")
        name = path[len(prefix) + 1 :] if prefix else path
        if name.lower() in seen:
            raise AdapterError("git_tree_invalid")
        seen.add(name.lower())
        mode = entry.get("mode")
        if not isinstance(mode, str):
            raise AdapterError("git_tree_invalid")
        directory = mode == "040000" and entry.get("type") == "tree"
        _portable(name, directory=directory)
        if directory:
            continue
        if mode not in {"100644", "100755"} or entry.get("type") != "blob":
            raise AdapterError("git_tree_invalid")
        size = entry.get("size")
        if size is not None and (not isinstance(size, int) or isinstance(size, bool) or size < 0):
            raise AdapterError("git_tree_invalid")
        if size is not None:
            if size > MAX_FILE_BYTES:
                raise AdapterError("git_import_limit")
            declared += size
        results.append((name, _oid(entry.get("sha", entry.get("id"))), size))
        if len(results) > MAX_FILES or declared > MAX_TOTAL_BYTES:
            raise AdapterError("git_import_limit")
    return results


def _github(api: PublicAPI, repository_url: str, ref: str, path: str) -> dict[str, Any]:
    repo = urlsplit(repository_url).path
    root = "/repos" + repo
    value, _ = api.json(root + "/commits/" + quote(ref or "HEAD", safe=""))
    try:
        commit = _oid(value["sha"])
        tree = _oid(value["commit"]["tree"]["sha"])
    except (TypeError, KeyError):
        raise AdapterError("git_tree_invalid") from None
    for part in path.split("/") if path else []:
        tree = _oid(_selected_entry(_github_tree(api, root, tree, recursive=False), part)["sha"])
    entries = _files(_github_tree(api, root, tree, recursive=True))
    files = {}
    total = 0
    for name, sha, size in entries:
        data = api.github_file(repository_url, commit, path, name)
        if size is not None and len(data) != size:
            raise AdapterError("git_tree_invalid")
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise AdapterError("git_import_limit")
        files[name] = _text(data, sha, name)
    return _finish(files, repository_url, ref, path, commit, "public_api")


def _gitlab(api: PublicAPI, repository_url: str, ref: str, path: str) -> dict[str, Any]:
    root = "/api/v4/projects/" + quote(urlsplit(repository_url).path[1:], safe="") + "/repository"
    value, _ = api.json(root + "/commits/" + quote(ref or "HEAD", safe=""))
    try:
        commit = _oid(value["id"])
    except (TypeError, KeyError):
        raise AdapterError("git_tree_invalid") from None
    entries = []
    page = 1
    advertised_total: int | None = None
    while True:
        query = {"ref": commit, "recursive": "true", "per_page": "100", "page": str(page)}
        if path:
            query["path"] = path
        value, headers = api.json(root + "/tree?" + urlencode(query))
        rows = _tree_rows(value)
        if len(rows) > 100:
            raise AdapterError("git_tree_invalid")
        entries.extend(rows)
        if len(entries) > MAX_TREE_ENTRIES:
            raise AdapterError("git_import_limit")
        page_size = headers.get("X-Per-Page", headers.get("x-per-page", ""))
        current_page = headers.get("X-Page", headers.get("x-page", ""))
        total = headers.get("X-Total", headers.get("x-total", ""))
        if page_size not in {"", "100"} or current_page not in {"", str(page)}:
            raise AdapterError("git_tree_invalid")
        if total:
            if not isinstance(total, str) or not total.isascii() or not total.isdigit():
                raise AdapterError("git_tree_invalid")
            count = int(total)
            if count > MAX_TREE_ENTRIES or (
                advertised_total is not None and count != advertised_total
            ):
                raise AdapterError("git_import_limit")
            advertised_total = count
        next_page = headers.get("X-Next-Page", headers.get("x-next-page", ""))
        link = headers.get("Link", headers.get("link", ""))
        link_has_next = bool(re.search(r'\brel\s*=\s*["\']?next\b', link, re.I))
        if next_page and next_page != str(page + 1):
            raise AdapterError("git_tree_invalid")
        if len(rows) < 100:
            if (
                next_page
                or link_has_next
                or (advertised_total is not None and advertised_total != len(entries))
            ):
                raise AdapterError("git_tree_invalid")
            break
        page += 1
    files = {}
    total = 0
    for name, sha, _ in _files(entries, path):
        data, _ = api.raw(root + "/blobs/" + sha + "/raw", limit=MAX_FILE_BYTES)
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise AdapterError("git_import_limit")
        files[name] = _text(data, sha, name)
    return _finish(files, repository_url, ref, path, commit, "public_api")


def fetch_public(repository_url: str, ref: str = "", path: str = "") -> dict[str, Any]:
    repository_url = normalize_repository_url(repository_url)
    ref, path = _ref(ref), _path(path)
    host = urlsplit(repository_url).hostname
    if host == "github.com":
        return _github(PublicAPI("https://api.github.com"), repository_url, ref, path)
    if host == "gitlab.com":
        return _gitlab(PublicAPI("https://" + host), repository_url, ref, path)
    raise AdapterError("checkout_required")


def _origin(value: str) -> str:
    # Only standard Git SSH origin spellings are translated; no auth is read or used.
    ssh = re.fullmatch(r"git@([A-Za-z0-9.-]+):([A-Za-z0-9_./-]+)", value)
    if ssh:
        value = "https://" + ssh[1] + "/" + ssh[2]
    elif value.startswith("ssh://"):
        parsed = urlsplit(value)
        if parsed.username != "git" or parsed.password or parsed.port not in {None, 22}:
            raise AdapterError("git_origin_mismatch")
        value = "https://" + (parsed.hostname or "") + parsed.path
    try:
        return normalize_repository_url(value)
    except AdapterError:
        raise AdapterError("git_origin_mismatch") from None


def _git(
    checkout: Path, args: list[str], budget: Budget, *, limit: int = MAX_RESPONSE_BYTES
) -> bytes:
    budget.request()
    executable = shutil.which("git")
    if not executable:
        raise AdapterError("checkout_required")
    environment = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    environment.update(
        {
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_ALLOW_PROTOCOL": "",
            "GIT_PAGER": "",
            "GCM_INTERACTIVE": "never",
        }
    )
    command = [
        executable,
        "--no-pager",
        "-C",
        str(checkout),
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.hooksPath=" + os.devnull,
        "-c",
        "protocol.allow=never",
        *args,
    ]
    try:
        timeout = budget.remaining()
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            env=environment,
            shell=False,
        )
        expired = threading.Event()

        def stop() -> None:
            expired.set()
            process.kill()

        timer = threading.Timer(timeout, stop)
        timer.start()
        assert process.stdout is not None
        try:
            data = bytearray()
            while len(data) <= limit:
                chunk = process.stdout.read(min(65536, limit + 1 - len(data)))
                if not chunk:
                    break
                budget.add(len(chunk))
                data.extend(chunk)
            if len(data) > limit:
                raise AdapterError("git_import_limit")
            code = process.wait(timeout=budget.remaining())
            if expired.is_set():
                raise AdapterError("git_import_limit")
            if code != 0:
                raise AdapterError("checkout_required")
            return bytes(data)
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
    except (OSError, subprocess.TimeoutExpired):
        raise AdapterError("checkout_required") from None


def read_checkout(
    repository_url: str, checkout_path: Path, ref: str = "", path: str = ""
) -> dict[str, Any]:
    repository_url = normalize_repository_url(repository_url)
    ref, path = _ref(ref), _path(path)
    try:
        checkout = Path(checkout_path).resolve(strict=True)
        if not checkout.is_dir():
            raise OSError
    except (OSError, ValueError, RuntimeError):
        raise AdapterError("checkout_required") from None
    budget = Budget()
    try:
        root = _git(checkout, ["rev-parse", "--show-toplevel"], budget, limit=4096).decode().strip()
        if Path(root).resolve() != checkout:
            raise AdapterError("checkout_required")
        origins = (
            _git(
                checkout,
                ["config", "--local", "--no-includes", "--get-all", "remote.origin.url"],
                budget,
                limit=4096,
            )
            .decode("utf-8")
            .splitlines()
        )
        if len(origins) != 1 or _origin(origins[0]) != repository_url:
            raise AdapterError("git_origin_mismatch")
        effective_origin = (
            _git(
                checkout,
                ["remote", "get-url", "origin"],
                budget,
                limit=4096,
            )
            .decode("utf-8")
            .splitlines()
        )
        if len(effective_origin) != 1 or _origin(effective_origin[0]) != repository_url:
            raise AdapterError("git_origin_mismatch")
        commit = _oid(
            _git(
                checkout,
                ["rev-parse", "--verify", "--end-of-options", (ref or "HEAD") + "^{commit}"],
                budget,
                limit=100,
            )
            .decode()
            .strip()
        )
        tree = _git(
            checkout,
            ["ls-tree", "-rz", "--full-tree", commit, "--", path]
            if path
            else ["ls-tree", "-rz", "--full-tree", commit],
            budget,
        )
        entries = []
        for row in tree.split(b"\0"):
            if not row:
                continue
            info, name_bytes = row.split(b"\t", 1)
            mode, kind, sha = info.decode("ascii").split(" ")
            entries.append(
                {"mode": mode, "type": kind, "sha": sha, "path": name_bytes.decode("utf-8")}
            )
        files = {}
        total = 0
        for name, sha, _ in _files(entries, path):
            data = _git(checkout, ["cat-file", "blob", sha], budget, limit=MAX_FILE_BYTES)
            total += len(data)
            if total > MAX_TOTAL_BYTES:
                raise AdapterError("git_import_limit")
            files[name] = _text(data, sha, name)
        return _finish(files, repository_url, ref, path, commit, "agent_checkout")
    except (UnicodeError, ValueError, TypeError, KeyError):
        raise AdapterError("git_tree_invalid") from None
