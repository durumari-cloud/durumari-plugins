"""Explicit project-local skill installation. Downloaded scripts are never executed."""

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_outbox import process_lock
from durumari_mcp.skill_files import file_bytes, validated_files

MARKER = ".durumari-release.json"
SKILL_ROOTS = {"claude": ".claude", "codex": ".agents", "cursor": ".cursor"}
INSTALL_CLIENTS = (
    "codex",
    "claude",
    "cursor",
    "gemini",
    "antigravity",
    "opencode",
    "copilot",
    "mcp",
)


def skill_root(workspace: Path, client: str) -> Path:
    if client not in INSTALL_CLIENTS:
        raise AdapterError("project_client_mismatch")
    return workspace / ".durumari" / "skills"


def bundle_digest(row: dict[str, Any]) -> str:
    # The server's sha256 covers the requirements as stored at publish time; its `requirements`
    # field is a normalized read view (kind "text", env, legacy_kind). Verify the stored shape.
    core = {k: row[k] for k in ("name", "version", "files")}
    core["requirements"] = (
        row["stored_requirements"] if "stored_requirements" in row else row["requirements"]
    )
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


# Mirrors backend durumari.skills.schemas.normalize_requirements (read-time view of stored rows).
LEGACY_SLOT_KINDS = {"api_token": "text", "dns_credential": "text", "password": "account"}
DEVICE_KEY_KINDS = frozenset({"ssh_key"})


def normalized_slots(stored: list[dict[str, Any]]) -> list[dict[str, Any]]:
    slots: list[dict[str, Any]] = []
    for item in stored:
        kind = str(item.get("kind", ""))
        if kind in DEVICE_KEY_KINDS:
            continue
        name = str(item["name"])
        env = item.get("env")
        slots.append(
            {
                "name": name,
                "kind": LEGACY_SLOT_KINDS.get(kind, kind),
                "required": bool(item.get("required", True)),
                "description": item.get("description") or None,
                "env": env if isinstance(env, str) and env else name.upper(),
                **({"legacy_kind": kind} if kind in LEGACY_SLOT_KINDS else {}),
            }
        )
    return slots


def validate_bundle(row: dict[str, Any]) -> None:
    try:
        if "stored_requirements" in row and row["requirements"] != normalized_slots(
            row["stored_requirements"]
        ):
            raise ValueError  # The unsigned view must be exactly what the signed form implies.
        if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", row["name"]) or len(row["name"]) > 64:
            raise ValueError
        files = row["files"]
        validated_files(files)
        match = re.match(r"\A---\r?\n(.*?)\r?\n---(?:\r?\n|$)", files["SKILL.md"], re.S)
        if match and (
            len(match.group(1).encode()) > 8192
            or any(
                isinstance(token, (yaml.tokens.AliasToken, yaml.tokens.AnchorToken))
                for token in yaml.scan(match.group(1))
            )
        ):
            raise ValueError
        header = yaml.safe_load(match.group(1)) if match else None
        if (
            not isinstance(header, dict)
            or header.get("name") != row["name"]
            or not isinstance(header.get("description"), str)
        ):
            raise ValueError
        if bundle_digest(row) != row["sha256"]:
            raise ValueError
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        yaml.YAMLError,
        RecursionError,
        AdapterError,
    ):
        raise AdapterError("invalid_skill_bundle") from None


def safe_parents(path: Path) -> None:
    for parent in [path, *path.parents]:
        if parent.is_symlink() or parent.is_junction():
            raise AdapterError("skill_symlink_denied")
        try:
            mode = parent.stat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(mode) and (parent != path or not stat.S_ISREG(mode)):
            raise AdapterError("skill_unsafe_path")


def regular_file(path: Path) -> None:
    safe_parents(path)
    if path.exists() and not stat.S_ISREG(path.stat().st_mode):
        raise AdapterError("skill_unsafe_path")


def no_git_metadata(workspace: Path) -> bool:
    """Prove a Git-less workspace is outside repository metadata without following links."""
    workspace = workspace.absolute()
    for parent in (workspace, *workspace.parents):
        if not stat.S_ISDIR(parent.lstat().st_mode) or parent.is_junction():
            return False
        try:
            (parent / ".git").lstat()
        except FileNotFoundError:
            pass
        else:
            # Any entry, including a gitfile or broken symlink, may hide tracked paths.
            return False
        for name in ("HEAD", "objects", "refs"):
            try:
                (parent / name).lstat()
            except FileNotFoundError:
                break
        else:
            # Bare repositories have no .git entry; preserve their existing rejection too.
            return False
    return True


def git_output(
    workspace: Path, *args: str, allow_non_git: bool = False, allow_no_match: bool = False
) -> bytes | None:
    # A host's GIT_DIR/GIT_WORK_TREE must not redirect this workspace's exclusions.
    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    environment["LC_ALL"] = "C"
    try:
        pathspec_options = ["--literal-pathspecs"] if args[0] == "ls-files" else []
        result = subprocess.run(
            ["git", *pathspec_options, "-c", "core.fsmonitor=false", *args],
            cwd=workspace,
            env=environment,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError:
        try:
            # Match subprocess cwd for relative and empty PATH entries.
            search_path = os.pathsep.join(
                str(workspace.absolute() / entry) for entry in os.get_exec_path(environment)
            )
            if (
                allow_non_git
                and shutil.which("git", path=search_path) is None
                and no_git_metadata(workspace)
            ):
                return None
        except OSError:
            pass
        raise AdapterError("skill_git_unavailable") from None
    except (OSError, subprocess.TimeoutExpired):
        raise AdapterError("skill_git_unavailable") from None
    if result.returncode:
        if allow_no_match and result.returncode == 1:
            return None
        if allow_non_git and result.stderr.startswith(b"fatal: not a git repository"):
            return None
        raise AdapterError("skill_git_failed")
    return result.stdout


def git_path(workspace: Path, option: str) -> Path:
    raw = git_output(workspace, "rev-parse", option)
    assert raw is not None
    text = os.fsdecode(raw.removesuffix(b"\n"))
    if not text or "\n" in text or "\r" in text:
        raise AdapterError("skill_git_path_invalid")
    path = Path(text)
    return Path(os.path.abspath(path if path.is_absolute() else workspace / path))


def git_install_paths(workspace: Path, paths: list[Path]) -> tuple[Path, Path, list[str]] | None:
    workspace = Path(os.path.abspath(workspace))
    bare = git_output(workspace, "rev-parse", "--is-bare-repository", allow_non_git=True)
    if bare is None:
        return None
    if bare != b"false\n":
        raise AdapterError("skill_git_bare_repository")
    root = git_path(workspace, "--show-toplevel")
    common = git_path(workspace, "--git-common-dir")
    raw = git_output(workspace, "rev-parse", "--git-path", "info/exclude")
    assert raw is not None
    text = os.fsdecode(raw.removesuffix(b"\n"))
    if "\n" in text or "\r" in text:
        raise AdapterError("skill_git_path_invalid")
    exclude = Path(os.path.abspath(workspace / text))
    if exclude != common / "info" / "exclude":
        raise AdapterError("skill_git_path_invalid")
    safe_parents(root)
    regular_file(exclude)
    regular_file(exclude.with_name("exclude.lock"))
    try:
        workspace.relative_to(root)
        relative = [Path(os.path.abspath(path)).relative_to(root).as_posix() for path in paths]
    except ValueError:
        raise AdapterError("skill_git_path_invalid") from None
    if any("\n" in path or "\r" in path for path in relative):
        raise AdapterError("skill_git_path_invalid")
    # Include ancestors to catch a tracked file/gitlink that contains a target.
    ancestors = {parent.as_posix() for path in relative for parent in Path(path).parents}
    ancestors.discard(".")
    indexed = git_output(workspace, "ls-files", "--stage", "-z", "--", *relative, *ancestors)
    assert indexed is not None
    for entry in indexed.split(b"\0"):
        if not entry:
            continue
        name = os.fsdecode(entry.split(b"\t", 1)[-1])
        if name in ancestors or any(
            name == path or name.startswith(path + "/") for path in relative
        ):
            raise AdapterError("skill_git_tracked_path")
    patterns = []
    for index, path in enumerate(relative):
        escaped = re.sub(r"([\\*?\[\]!# ])", r"\\\1", path)
        patterns.append("/" + escaped + ("/" if index < 2 else ""))
    return root, exclude, patterns


def exclude_install_paths(exclude: Path, patterns: list[str]) -> None:
    """Use Git's lockfile convention, preserving all existing user-owned bytes."""
    lock = exclude.with_name("exclude.lock")
    deadline = time.monotonic() + 5
    safe_parents(exclude.parent)
    exclude.parent.mkdir(parents=True, exist_ok=True)
    while True:
        regular_file(lock)
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise AdapterError("skill_git_busy") from None
            time.sleep(0.02)
        except OSError:
            raise AdapterError("skill_git_exclude_failed") from None
    try:
        with os.fdopen(fd, "wb") as output:
            regular_file(exclude)
            original = b""
            mode = 0o600
            if exclude.exists():
                source_fd = os.open(
                    exclude,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
                )
                with os.fdopen(source_fd, "rb") as source:
                    info = os.fstat(source.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise AdapterError("skill_unsafe_path")
                    mode = stat.S_IMODE(info.st_mode)
                    original = source.read()
            missing = [
                pattern.encode()
                for pattern in patterns
                if pattern.encode() not in original.splitlines()
            ]
            if not missing:
                return
            output.write(original)
            if original and not original.endswith(b"\n"):
                output.write(b"\n")
            output.write(b"\n".join(missing) + b"\n")
            output.flush()
            os.fsync(output.fileno())
            if hasattr(os, "fchmod"):
                os.fchmod(output.fileno(), mode)
        regular_file(exclude)
        os.replace(lock, exclude)
    except OSError:
        raise AdapterError("skill_git_exclude_failed") from None
    finally:
        lock.unlink(missing_ok=True)


def read_regular(path: Path, limit: int) -> bytes:
    regular_file(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(fd, "rb") as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise AdapterError("skill_local_changes")
        value = source.read(limit + 1)
    if len(value) > limit:
        raise AdapterError("skill_local_changes")
    return value


def read_marker(root: Path) -> dict[str, Any]:
    try:
        marker: Any = json.loads(read_regular(root / MARKER, 64_000))
        if not isinstance(marker, dict):
            raise ValueError
        return marker
    except (ValueError, OSError):
        raise AdapterError("skill_name_collision") from None


def release_marker(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "project_id": row["project_id"],
        "skill_id": row["id"],
        "version": row["version"],
        "sha256": row["sha256"],
        "files": {p: hashlib.sha256(file_bytes(p, v)).hexdigest() for p, v in row["files"].items()},
    }


def hashes(root: Path) -> dict[str, str]:
    found: dict[str, str] = {}
    pending = [root]
    entries, total = 0, 0
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as children:
            for entry in children:
                entries += 1
                path = Path(entry.path)
                if entries > 10_000 or path.is_symlink() or path.is_junction():
                    raise AdapterError("skill_local_changes")
                mode = entry.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode):
                    pending.append(path)
                    continue
                if not stat.S_ISREG(mode):
                    raise AdapterError("skill_local_changes")
                if path == root / MARKER:
                    continue
                if len(found) >= 100:
                    raise AdapterError("skill_local_changes")
                content = read_regular(path, 128_000)
                total += len(content)
                if total > 512_000:
                    raise AdapterError("skill_local_changes")
                found[path.relative_to(root).as_posix()] = hashlib.sha256(content).hexdigest()
    return found


def verified_copy(root: Path, release: dict[str, Any]) -> None:
    safe_parents(root)
    validate_bundle(release)
    expected = release_marker(release)
    if read_marker(root) != expected or hashes(root) != expected["files"]:
        raise AdapterError("skill_local_changes")


def migrate_legacy_copies(
    workspace: Path,
    client: str,
    release: dict[str, Any],
    load_release: Callable[[str], dict[str, Any]],
) -> list[dict[str, str]]:
    """Retire only immutable-server-verified discovery copies; all conflicts stay local."""
    reports: list[dict[str, str]] = []
    central = skill_root(workspace, client) / release["name"]
    try:
        install_lock = central.parent / ".durumari-install-lock"
        safe_parents(install_lock)
        regular_file(install_lock / "process.lock")
        with process_lock(install_lock):
            verified_copy(central, release)
            for host, directory in SKILL_ROOTS.items():
                legacy = workspace / directory / "skills" / release["name"]
                report = {"client": host, "path": legacy.relative_to(workspace).as_posix()}
                backup: Path | None = None
                try:
                    safe_parents(legacy)
                    if not legacy.exists():
                        continue
                    marker = read_marker(legacy)
                    if (marker.get("project_id"), marker.get("skill_id")) != (
                        release["project_id"],
                        release["id"],
                    ):
                        raise AdapterError("skill_name_collision")
                    version = marker.get("version")
                    if (
                        not isinstance(version, str)
                        or len(version) > 40
                        or not re.fullmatch(
                            r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-z0-9-]+(?:\.[a-z0-9-]+)*)?", version
                        )
                    ):
                        raise AdapterError("skill_local_changes")
                    previous = load_release(version)
                    if (
                        previous.get("id"),
                        previous.get("project_id"),
                        previous.get("name"),
                        previous.get("version"),
                    ) != (release["id"], release["project_id"], release["name"], version):
                        raise AdapterError("skill_scope_mismatch")
                    verified_copy(legacy, previous)
                    git_install_paths(workspace, [legacy])
                    backups = workspace / ".durumari" / "skill-backups"
                    safe_parents(backups)
                    backup = backups / f"legacy-{host}-{release['name']}-{uuid4().hex}"
                    legacy.rename(backup)
                    verified_copy(backup, previous)
                    report.update(
                        status="backed_up", backup_path=backup.relative_to(workspace).as_posix()
                    )
                except Exception as error:
                    code = (
                        str(error)
                        if isinstance(error, AdapterError)
                        else "skill_legacy_backup_failed"
                    )
                    allowed = {
                        "skill_name_collision",
                        "skill_local_changes",
                        "skill_scope_mismatch",
                        "skill_symlink_denied",
                        "skill_unsafe_path",
                        "skill_git_tracked_path",
                        "skill_legacy_release_unavailable",
                        "skill_legacy_backup_failed",
                        "invalid_skill_bundle",
                    }
                    report.update(
                        status="preserved",
                        reason=code if code in allowed else "skill_legacy_unavailable",
                    )
                    if backup is not None and backup.exists():
                        try:
                            if legacy.exists() or legacy.is_symlink() or legacy.is_junction():
                                raise OSError
                            backup.rename(legacy)
                        except OSError:
                            report.update(
                                status="recovery_required",
                                reason="skill_legacy_restore_conflict",
                                backup_path=backup.relative_to(workspace).as_posix(),
                            )
                reports.append(report)
    except Exception:
        for host, directory in SKILL_ROOTS.items():
            legacy = workspace / directory / "skills" / release["name"]
            if legacy.exists() or legacy.is_symlink() or legacy.is_junction():
                reports.append(
                    {
                        "client": host,
                        "path": legacy.relative_to(workspace).as_posix(),
                        "status": "preserved",
                        "reason": "skill_central_not_verified",
                    }
                )
    return reports


def install_bundle(workspace: Path, client: str, row: dict[str, Any]) -> Path:
    validate_bundle(row)
    parent = skill_root(workspace, client)
    target: Path = parent / str(row["name"])
    safe_parents(target)
    backups = workspace / ".durumari" / "skill-backups"
    install_lock = parent / ".durumari-install-lock"
    safe_parents(backups)
    regular_file(install_lock / "process.lock")
    git_paths = git_install_paths(workspace, [target, backups, install_lock / "process.lock"])
    parent.mkdir(parents=True, exist_ok=True)
    marker = release_marker(row)
    with process_lock(install_lock):
        safe_parents(target)
        regular_file(install_lock / "process.lock")
        if git_paths:
            # Recheck tracking after waiting for another installation.
            git_paths = git_install_paths(
                workspace, [target, backups, install_lock / "process.lock"]
            )
            assert git_paths is not None
            exclude_install_paths(git_paths[1], git_paths[2])
            for artifact in [
                target / "SKILL.md",
                backups / "recovery" / "SKILL.md",
                install_lock / "process.lock",
            ]:
                if (
                    git_output(
                        workspace,
                        "check-ignore",
                        "--quiet",
                        "--no-index",
                        "--",
                        str(artifact.absolute()),
                        allow_no_match=True,
                    )
                    is None
                ):
                    raise AdapterError("skill_git_ignore_conflict")
        if target.exists():
            old = read_marker(target)
            if (old.get("project_id"), old.get("skill_id")) != (row["project_id"], row["id"]):
                raise AdapterError("skill_name_collision")
            if hashes(target) != old.get("files"):
                raise AdapterError("skill_local_changes")
            if old == marker:
                return target
        safe_parents(backups)
        backups.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Cursor discovers SKILL.md recursively; do not expose incomplete staging.
        temporary = Path(tempfile.mkdtemp(prefix=".durumari-install-", dir=backups))
        backup = backups / (str(row["name"]) + "-" + uuid4().hex)
        try:
            for rel, value in row["files"].items():
                path = temporary / rel
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as out:
                    out.write(file_bytes(rel, value))
            (temporary / MARKER).write_text(json.dumps(marker))
            if target.exists():
                target.rename(backup)
            try:
                temporary.rename(target)
            except OSError:
                if backup.exists():
                    backup.rename(target)
                raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    return target
