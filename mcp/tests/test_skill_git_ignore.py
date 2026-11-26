"""Only downloaded skill copies and installer artifacts leave Git's source view."""

import errno
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_skills import INSTALL_CLIENTS, bundle_digest, git_output, install_bundle


def git(root, *args, check=True):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=check
    )


def repo(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    return root


def bundle(name="durumari-ops", version="1.0.0"):
    row = {
        "id": name,
        "project_id": "project",
        "name": name,
        "version": version,
        "files": {"SKILL.md": f"---\nname: {name}\ndescription: Synthetic skill\n---\n"},
        "requirements": [],
    }
    row["sha256"] = bundle_digest(row)
    return row


def exclude_path(root):
    path = Path(git(root, "rev-parse", "--git-path", "info/exclude").stdout.rstrip("\n"))
    return path if path.is_absolute() else root / path


def ignored(root, path):
    return git(root, "check-ignore", "--quiet", "--", str(path), check=False).returncode == 0


@pytest.mark.parametrize(
    "client, directory", [("codex", ".agents"), ("claude", ".claude"), ("cursor", ".cursor")]
)
def test_install_excludes_only_managed_copy_and_artifacts(tmp_path, client, directory):
    root = repo(tmp_path)
    unrelated = root / directory / "skills" / "source-skill" / "SKILL.md"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("author's original source")
    target = install_bundle(root, client, bundle())
    assert ignored(root, target / "SKILL.md")
    assert ignored(root, root / ".durumari/skill-backups/recovery/SKILL.md")
    assert ignored(root, root / ".durumari/skills/.durumari-install-lock/process.lock")
    assert not ignored(root, unrelated)
    assert not ignored(root, root / ".durumari/project.json")
    assert not ignored(root, root / ".durumari/skills/.durumari-install-lock/user-file")
    assert not ignored(root, root / directory / "skills/.durumari-install-lock/process.lock")
    assert not (root / ".gitignore").exists()


def test_existing_exclude_bytes_are_preserved_and_update_is_idempotent(tmp_path):
    root = repo(tmp_path)
    exclude = exclude_path(root)
    original = b"# user rules\r\nuser-file\r\n# non-utf8: \xff"
    exclude.write_bytes(original)
    install_bundle(root, "codex", bundle())
    first = exclude.read_bytes()
    assert first.startswith(original + b"\n")
    install_bundle(root, "codex", bundle())
    install_bundle(root, "codex", bundle(version="1.1.0"))
    assert exclude.read_bytes() == first
    assert first.count(b"/.durumari/skills/durumari-ops/\n") == 1


def test_monorepo_patterns_are_anchored_and_escape_gitignore_metacharacters(tmp_path):
    root = repo(tmp_path)
    workspace = root / "apps" / "space [ab]*!? #"
    workspace.mkdir(parents=True)
    target = install_bundle(workspace, "codex", bundle())
    assert ignored(root, target / "SKILL.md")
    assert not ignored(root, root / ".durumari/skills/durumari-ops/SKILL.md")
    assert not ignored(root, root / "apps/space aXYZ!x #/.durumari/skills/durumari-ops/SKILL.md")


def test_linked_worktree_uses_git_common_exclude(tmp_path):
    root = repo(tmp_path / "main")
    git(
        root,
        "-c",
        "user.name=Synthetic",
        "-c",
        "user.email=synthetic@example.invalid",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "fixture",
    )
    worktree = tmp_path / "linked"
    git(root, "worktree", "add", "-q", "-b", "linked", str(worktree))
    target = install_bundle(worktree, "codex", bundle())
    assert ignored(worktree, target / "SKILL.md")
    assert exclude_path(worktree).resolve() == exclude_path(root).resolve()
    assert not (worktree / ".gitignore").exists()


@pytest.mark.parametrize(
    "artifact",
    [
        ".durumari/skills/durumari-ops/SKILL.md",
        ".durumari/skill-backups/source",
        ".durumari/skills/.durumari-install-lock/process.lock",
    ],
)
def test_tracked_targets_or_artifacts_are_rejected_without_untracking(tmp_path, artifact):
    root = repo(tmp_path)
    tracked = root / artifact
    tracked.parent.mkdir(parents=True)
    tracked.write_text("tracked original")
    git(root, "add", "--", artifact)
    original = exclude_path(root).read_bytes()
    with pytest.raises(AdapterError, match="skill_git_tracked_path"):
        install_bundle(root, "codex", bundle())
    assert tracked.read_text() == "tracked original"
    assert git(root, "ls-files", "--", artifact).stdout.rstrip("\n") == artifact
    assert exclude_path(root).read_bytes() == original


def test_a_managed_copy_later_tracked_is_preserved_even_when_bundle_is_unchanged(tmp_path):
    root = repo(tmp_path)
    target = install_bundle(root, "codex", bundle())
    git(root, "add", "-f", "--", str(target))
    with pytest.raises(AdapterError, match="skill_git_tracked_path"):
        install_bundle(root, "codex", bundle())
    assert (target / "SKILL.md").read_text() == bundle()["files"]["SKILL.md"]


@pytest.mark.parametrize("link_target", ["exclude", "info"])
def test_exclude_symlink_cannot_modify_another_file(tmp_path, link_target):
    root = repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "exclude"
    sentinel.write_text("keep me")
    exclude = exclude_path(root)
    if link_target == "exclude":
        exclude.unlink()
        exclude.symlink_to(sentinel)
    else:
        exclude.unlink()
        exclude.parent.rmdir()
        exclude.parent.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdapterError, match="skill_symlink_denied"):
        install_bundle(root, "codex", bundle())
    assert sentinel.read_text() == "keep me"
    assert not (root / ".durumari").exists()


def test_install_lock_symlink_is_rejected_before_its_target_is_opened(tmp_path):
    root = repo(tmp_path / "repo")
    outside = tmp_path / "outside"
    outside.write_text("keep me")
    lock = root / ".durumari/skills/.durumari-install-lock/process.lock"
    lock.parent.mkdir(parents=True)
    lock.symlink_to(outside)
    with pytest.raises(AdapterError, match="skill_symlink_denied"):
        install_bundle(root, "codex", bundle())
    assert outside.read_text() == "keep me"


def test_exclude_special_file_is_rejected_without_opening_it(tmp_path):
    import os

    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO is not available on this platform")
    root = repo(tmp_path)
    exclude = exclude_path(root)
    exclude.unlink()
    os.mkfifo(exclude)
    with pytest.raises(AdapterError, match="skill_unsafe_path"):
        install_bundle(root, "codex", bundle())
    assert not (root / ".durumari").exists()


def test_parallel_clients_preserve_each_others_ignore_rules(tmp_path):
    root = repo(tmp_path)
    with ThreadPoolExecutor(max_workers=3) as workers:
        targets = list(
            workers.map(
                lambda client: install_bundle(root, client, bundle()), ["codex", "claude", "cursor"]
            )
        )
    assert all(ignored(root, target / "SKILL.md") for target in targets)
    lines = exclude_path(root).read_text().splitlines()
    assert lines.count("/.durumari/skill-backups/") == 1
    assert not exclude_path(root).with_name("exclude.lock").exists()


def test_non_git_workspace_still_installs_without_creating_git_metadata(tmp_path):
    target = install_bundle(tmp_path, "codex", bundle())
    assert (target / "SKILL.md").is_file()
    assert not (tmp_path / ".git").exists()


def disable_git(monkeypatch):
    from durumari_mcp import project_skills

    def unavailable(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "Synthetic missing executable", "git")

    monkeypatch.setattr(project_skills.subprocess, "run", unavailable)
    monkeypatch.setattr(project_skills.shutil, "which", lambda *args, **kwargs: None)


@pytest.fixture
def missing_git(monkeypatch):
    disable_git(monkeypatch)


@pytest.mark.parametrize("client", INSTALL_CLIENTS)
def test_missing_git_installs_and_updates_only_in_non_git_workspace(tmp_path, missing_git, client):
    workspace = tmp_path / "plain" / "workspace"
    workspace.mkdir(parents=True)
    original = workspace / "authored-source.md"
    original.write_bytes(b"author's original source\r\n")
    target = install_bundle(workspace, client, bundle())
    updated = bundle(version="1.1.0")
    updated["files"]["SKILL.md"] += "\nUpdated instructions\n"
    updated["sha256"] = bundle_digest(updated)
    assert install_bundle(workspace, client, updated) == target
    assert install_bundle(workspace, client, updated) == target
    assert (target / "SKILL.md").read_text() == updated["files"]["SKILL.md"]
    assert original.read_bytes() == b"author's original source\r\n"
    assert not (workspace / ".git").exists()
    assert not (workspace / ".gitignore").exists()


@pytest.mark.parametrize("location", ["workspace", "ancestor"])
@pytest.mark.parametrize("kind", ["directory", "file", "symlink", "broken_link", "bare"])
def test_missing_git_preserves_files_when_metadata_is_present(
    tmp_path, missing_git, location, kind
):
    ancestor = tmp_path / "parent"
    workspace = ancestor / "workspace"
    workspace.mkdir(parents=True)
    metadata_root = workspace if location == "workspace" else ancestor
    metadata = metadata_root / ".git"
    outside = tmp_path / "metadata-target"
    if kind == "directory":
        metadata.mkdir()
        (metadata / "index").write_bytes(b"synthetic index")
    elif kind == "file":
        metadata.write_bytes(b"gitdir: ../linked-git-dir\n")
    elif kind in {"symlink", "broken_link"}:
        if kind == "symlink":
            outside.mkdir()
            (outside / "index").write_bytes(b"outside index")
        metadata.symlink_to(outside, target_is_directory=True)
    else:
        (metadata_root / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (metadata_root / "objects").mkdir()
        (metadata_root / "refs").mkdir()
    original = workspace / ".durumari/skills/durumari-ops/SKILL.md"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"user-owned source\r\n")
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}
    with pytest.raises(AdapterError, match="skill_git_unavailable"):
        install_bundle(workspace, "codex", bundle())
    assert {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()} == before
    assert not (workspace / ".durumari/skill-backups").exists()
    assert not (workspace / ".durumari/skills/.durumari-install-lock").exists()


@pytest.mark.parametrize("layout", ["root", "nested", "linked"])
def test_missing_git_preserves_real_repository_index_exclude_and_tracked_skill(
    tmp_path, monkeypatch, layout
):
    root = repo(tmp_path / "repo")
    workspace = root / "nested" if layout == "nested" else root
    if layout == "linked":
        git(
            root, "-c", "user.name=Synthetic", "-c", "user.email=synthetic@example.invalid",
            "commit", "-q", "--allow-empty", "-m", "fixture",
        )
        workspace = tmp_path / "linked"
        git(root, "worktree", "add", "-q", "-b", "linked", str(workspace))
    workspace.mkdir(exist_ok=True)
    original = workspace / ".durumari/skills/durumari-ops/SKILL.md"
    original.parent.mkdir(parents=True)
    original.write_bytes(b"tracked original\r\n")
    git(workspace, "add", "--", str(original))
    index_path = Path(git(workspace, "rev-parse", "--git-path", "index").stdout.rstrip("\n"))
    if not index_path.is_absolute():
        index_path = workspace / index_path
    index = index_path.read_bytes()
    exclude = exclude_path(workspace)
    rules = exclude.read_bytes()
    disable_git(monkeypatch)
    with pytest.raises(AdapterError, match="skill_git_unavailable"):
        install_bundle(workspace, "codex", bundle())
    assert original.read_bytes() == b"tracked original\r\n"
    assert index_path.read_bytes() == index
    assert exclude.read_bytes() == rules
    assert not (workspace / ".durumari/skill-backups").exists()


@pytest.mark.parametrize("error", [errno.EACCES, errno.EIO])
def test_missing_git_and_unknown_metadata_stat_error_remain_unavailable(
    tmp_path, monkeypatch, missing_git, error
):
    original_lstat = Path.lstat

    def denied(path, *args, **kwargs):
        if path == tmp_path / ".git":
            raise OSError(error, "Synthetic metadata access denied")
        return original_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(AdapterError, match="skill_git_unavailable"):
        install_bundle(tmp_path, "codex", bundle())
    assert not (tmp_path / ".durumari").exists()


def test_missing_git_does_not_relax_commands_that_require_git(tmp_path, missing_git):
    with pytest.raises(AdapterError, match="skill_git_unavailable"):
        git_output(tmp_path, "rev-parse", "--show-toplevel")


@pytest.mark.parametrize(
    "failure",
    [
        PermissionError("Synthetic failure"),
        OSError("Synthetic failure"),
        subprocess.TimeoutExpired("git", 5),
    ],
)
def test_other_git_execution_failures_do_not_use_non_git_fallback(tmp_path, monkeypatch, failure):
    from durumari_mcp import project_skills

    def unavailable(*args, **kwargs):
        raise failure

    monkeypatch.setattr(project_skills.subprocess, "run", unavailable)
    with pytest.raises(AdapterError, match="skill_git_unavailable"):
        git_output(tmp_path, "rev-parse", "--is-bare-repository", allow_non_git=True)
    assert not (tmp_path / ".durumari").exists()


def test_missing_working_directory_is_not_treated_as_non_git(tmp_path, missing_git):
    with pytest.raises(AdapterError, match="skill_git_unavailable"):
        git_output(tmp_path / "absent", "rev-parse", "--is-bare-repository", allow_non_git=True)


def test_existing_git_executable_does_not_use_missing_git_fallback(
    tmp_path, monkeypatch, missing_git
):
    from durumari_mcp import project_skills

    monkeypatch.setattr(project_skills.shutil, "which", lambda *args, **kwargs: "/synthetic/git")
    with pytest.raises(AdapterError, match="skill_git_unavailable"):
        git_output(tmp_path, "rev-parse", "--is-bare-repository", allow_non_git=True)


@pytest.mark.parametrize("binary_present", [False, True])
def test_relative_git_search_path_is_checked_from_the_workspace(
    tmp_path, monkeypatch, binary_present
):
    from durumari_mcp import project_skills

    workspace = tmp_path / "workspace"
    binaries = workspace / "bin"
    binaries.mkdir(parents=True)
    caller = tmp_path / "caller"
    caller.mkdir()
    monkeypatch.chdir(caller)
    monkeypatch.setenv("PATH", "bin")
    if binary_present:
        executable = binaries / ("git.exe" if project_skills.os.name == "nt" else "git")
        executable.write_bytes(b"synthetic unavailable interpreter")
        executable.chmod(0o700)

    def unavailable(*args, **kwargs):
        raise FileNotFoundError(errno.ENOENT, "Synthetic missing executable/interpreter", "git")

    monkeypatch.setattr(project_skills.subprocess, "run", unavailable)
    if binary_present:
        with pytest.raises(AdapterError, match="skill_git_unavailable"):
            git_output(workspace, "rev-parse", "--is-bare-repository", allow_non_git=True)
    else:
        assert git_output(
            workspace, "rev-parse", "--is-bare-repository", allow_non_git=True
        ) is None


def test_bare_repository_is_rejected_before_creating_installation(tmp_path):
    git(tmp_path, "init", "--bare", "-q")
    with pytest.raises(AdapterError, match="skill_git_bare_repository"):
        install_bundle(tmp_path, "codex", bundle())
    assert not (tmp_path / ".durumari").exists()


def test_host_git_environment_cannot_redirect_the_workspace(tmp_path, monkeypatch):
    root = repo(tmp_path / "workspace")
    other = repo(tmp_path / "other")
    original = exclude_path(other).read_bytes()
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    target = install_bundle(root, "codex", bundle())
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    assert ignored(root, target / "SKILL.md")
    assert exclude_path(other).read_bytes() == original


def test_atomic_exclude_write_failure_preserves_user_rules(tmp_path, monkeypatch):
    from durumari_mcp import project_skills

    root = repo(tmp_path)
    exclude = exclude_path(root)
    original = exclude.read_bytes()

    def fail_replace(*args):
        raise OSError("synthetic failure")

    monkeypatch.setattr(project_skills.os, "replace", fail_replace)
    with pytest.raises(AdapterError, match="skill_git_exclude_failed"):
        install_bundle(root, "codex", bundle())
    assert exclude.read_bytes() == original
    assert not exclude.with_name("exclude.lock").exists()
    assert not (root / ".durumari/skills/durumari-ops").exists()


def test_exclude_junction_is_rejected_before_installation(tmp_path, monkeypatch):
    root = repo(tmp_path)
    exclude = exclude_path(root)
    monkeypatch.setattr(Path, "is_junction", lambda path: path == exclude.parent)
    with pytest.raises(AdapterError, match="skill_symlink_denied"):
        install_bundle(root, "codex", bundle())
    assert not (root / ".durumari").exists()


def test_repository_unignore_rule_returns_a_conflict_without_rewriting_it(tmp_path):
    root = repo(tmp_path)
    user_rules = "!/.durumari/skills/durumari-ops/\n"
    (root / ".gitignore").write_text(user_rules)
    with pytest.raises(AdapterError, match="skill_git_ignore_conflict"):
        install_bundle(root, "codex", bundle())
    assert (root / ".gitignore").read_text() == user_rules
    assert not (root / ".durumari/skills/durumari-ops").exists()
