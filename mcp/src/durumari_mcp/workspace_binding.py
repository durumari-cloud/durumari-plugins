"""Public source hints never replace a protected device approval."""

import hashlib
import re
import subprocess
from pathlib import Path

from durumari_mcp.credentials import AdapterError
from durumari_mcp.legacy import manifest_path


def folder_identity(identifier: str) -> str:
    if not isinstance(identifier, str) or not re.fullmatch(
        r"[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}", identifier
    ):
        raise AdapterError("invalid_folder_source")
    return hashlib.sha256(("folder:" + identifier).encode()).hexdigest()


def git_remote(root: Path) -> str | None:
    """Only the selected root's own remote is a source hint, never an ancestor's.

    None means the root is definitely not a Git root with an origin. A lookup that cannot
    tell on a root with its own .git (git missing, unsafe directory, timeout, OS or
    decoding error) raises instead, so a Git root never silently becomes a new folder
    source; a folder source is then only an explicit --folder-source choice.
    """

    def git(*args: str) -> str:
        return subprocess.check_output(
            ["git", "-C", str(root), *args],
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            timeout=2,
        ).strip()

    try:
        # Outside a work tree this fails; the .git check below then decides.
        top = git("rev-parse", "--show-toplevel")
        if Path(top).resolve() != root:
            return None
        try:
            return git("config", "--get", "remote.origin.url") or None
        except subprocess.CalledProcessError as error:
            if error.returncode == 1:
                return None  # The key is not set: a Git root without origin.
            raise
    except (OSError, subprocess.SubprocessError, ValueError):
        if not (root / ".git").exists():
            return None  # Without its own .git the root cannot be a Git root.
        raise AdapterError("repository_root_and_remote_required") from None


def check_descendant(root: Path, cwd: Path) -> None:
    """Nested manifests and repositories must obtain their own explicit approval."""
    try:
        cwd.relative_to(root)
    except ValueError:
        raise AdapterError("workspace_out_of_scope") from None
    current = cwd
    while current != root:
        if manifest_path(current).exists() or (current / ".git").exists():
            raise AdapterError("workspace_out_of_scope")
        current = current.parent


def check_nested_folder_sources(root: Path, cwd: Path) -> None:
    """A Git binding stops only at a selected subfolder's own folder-source binding.

    Git's top-level already bounds Git profiles, so unrelated `durumari.yaml` files
    (examples, fixtures) inside the repository keep working as before folder sources.
    A subfolder connected as its own folder source, however, is a separate approval.
    """
    from durumari_mcp.onboarding import read_manifest

    current = cwd
    while current != root and root in current.parents:
        if manifest_path(current).exists():
            try:
                nested = read_manifest(current)
            except AdapterError:
                nested = {}
            if nested.get("folder_source_id"):
                raise AdapterError("workspace_out_of_scope")
        current = current.parent


def binding_root(cwd: Path) -> Path:
    """Bounded discovery for CLI diagnostics; the OS profile still authenticates it."""
    current = cwd
    for _ in range(64):
        if manifest_path(current).exists():
            return current
        # An unrelated nested repo cannot borrow its parent's project profile.
        if (current / ".git").exists() or current.parent == current:
            return cwd
        current = current.parent
    return cwd
