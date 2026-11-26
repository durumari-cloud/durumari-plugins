"""Folder approval keeps repository, path and pending-write boundaries intact."""

import subprocess
from types import SimpleNamespace
from uuid import uuid4

import pytest
from test_context_connect import approval, connection, enrollment
from test_continuity import profile
from test_continuity import project_store as store_fixture

from durumari_mcp import context_cli, context_removal
from durumari_mcp.context_binding import install_binding, workspace_identity
from durumari_mcp.context_outbox import ContextOutbox
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import ProjectProfile, load_project_profile
from durumari_mcp.project_gateway import ProjectTransport
from durumari_mcp.workspace_binding import binding_root, folder_identity

project_store = store_fixture


def folder_profile(root, identifier=None):
    identifier = identifier or str(uuid4())
    values = profile(root).model_dump()
    values.update(
        client="mcp",
        context_only=True,
        capture_enabled=False,
        folder_source_id=identifier,
        repository_id=folder_identity(identifier),
        workspace_identity=workspace_identity(root),
    )
    return ProjectProfile.model_validate(values)


@pytest.mark.parametrize("git", [False, True])
def test_connect_folder_without_origin_and_reuse_approval(
    project_store, tmp_path, monkeypatch, git
):
    if git:
        subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    browsers = []
    scopes = []
    requests = []
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(context_cli, "os_store", lambda: project_store)
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(context_cli.webbrowser, "open", browsers.append)

    def request(self, path, **kwargs):
        requests.append(path)
        if path.endswith("/config"):
            return {"browser_origin": "https://api.example.invalid", "folder_sources": True}
        if path.endswith("/enrollments"):
            body = kwargs["body"]
            assert "repository_remote" not in body
            scope = folder_profile(tmp_path, body["folder_source_id"])
            scopes.append(scope)
            return enrollment(scope)
        if path.endswith("/poll"):
            return approval(scopes[-1])
        if path.endswith("/finish"):
            return {}
        if path.endswith("/connection"):
            return connection(scopes[-1])
        if path == "/api/v2/agent/skills/required":
            assert kwargs.get("method", "GET") == "GET"
            assert kwargs.get("body") is None
            return {"items": []}
        pytest.fail("Unexpected route")

    monkeypatch.setattr(ProjectTransport, "request", request)
    args = SimpleNamespace(
        workspace=str(tmp_path),
        server="https://api.example.invalid",
        project=None,
        agents=["codex", "claude"],
    )
    result = context_cli.connect(args)
    bound = load_project_profile(result["profile"])
    bound.check_workspace(str(tmp_path))
    assert bound.folder_source_id == scopes[-1].folder_source_id
    assert not bound.capture_enabled
    assert not bound.disabled
    assert "context-serve" in (tmp_path / ".mcp.json").read_text()
    assert [item["client"] for item in result["required_skills"]["clients"]] == args.agents
    assert all(item["status"] == "current" for item in result["required_skills"]["clients"])
    required_checks = requests.count("/api/v2/agent/skills/required")
    assert required_checks >= len(args.agents)
    again = context_cli.connect(args)
    assert again["profile"] == result["profile"]
    assert load_project_profile(again["profile"]).connection_id == bound.connection_id
    assert not load_project_profile(again["profile"]).disabled
    assert all(item["status"] == "current" for item in again["required_skills"]["clients"])
    assert requests.count("/api/v2/agent/skills/required") > required_checks
    assert len(browsers) == 1
    assert len(scopes) == 1


def connect_folder(project_store, monkeypatch, tmp_path, root, folder=None):
    """Run connect for exactly `root` against a fake server; return result and request."""
    monkeypatch.setattr(context_cli, "os_store", lambda: project_store)
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(context_cli.webbrowser, "open", lambda url: None)
    requests, scopes = [], []

    def request(self, path, **kwargs):
        if path.endswith("/config"):
            return {"browser_origin": "https://api.example.invalid", "folder_sources": True}
        if path.endswith("/enrollments"):
            requests.append(kwargs["body"])
            scope = folder_profile(root, kwargs["body"]["folder_source_id"])
            scopes.append(scope)
            return enrollment(scope)
        if path.endswith("/poll"):
            return approval(scopes[-1])
        if path.endswith("/finish"):
            return {}
        if path.endswith("/connection"):
            return connection(scopes[-1])
        if path == "/api/v2/agent/skills/required":
            assert kwargs.get("method", "GET") == "GET"
            assert kwargs.get("body") is None
            return {"items": []}
        pytest.fail("Unexpected route")

    monkeypatch.setattr(ProjectTransport, "request", request)
    args = SimpleNamespace(
        workspace=str(root),
        server="https://api.example.invalid",
        project=None,
        agents=["claude"],
        folder_source=folder,
    )
    return context_cli.connect(args), requests


def test_connect_in_plain_subfolder_binds_it_without_touching_the_parent(
    project_store, tmp_path, monkeypatch
):
    parent_root = tmp_path / "work"
    child = parent_root / "notes"
    child.mkdir(parents=True)
    parent, _ = connect_folder(project_store, monkeypatch, tmp_path, parent_root)
    parent_manifest = (parent_root / "durumari.yaml").read_text()
    # The plain child would discover the parent binding, but connect never rebinds it.
    assert binding_root(child) == parent_root
    selected = str(uuid4())
    result, requests = connect_folder(project_store, monkeypatch, tmp_path, child, folder=selected)
    assert requests[-1]["workspace_identity"] == workspace_identity(child)
    assert requests[-1]["folder_source_id"] == selected
    assert (parent_root / "durumari.yaml").read_text() == parent_manifest
    assert load_project_profile(parent["profile"]).workspace == str(parent_root)
    child_profile = load_project_profile(result["profile"])
    assert child_profile.workspace == str(child)
    assert child_profile.folder_source_id == selected
    # The child's own manifest now stops inheritance from the parent.
    assert binding_root(child) == child


def test_remove_in_subfolder_never_revokes_the_parent_binding(project_store, tmp_path, monkeypatch):
    parent_root = tmp_path / "work"
    child = parent_root / "notes"
    child.mkdir(parents=True)
    parent, _ = connect_folder(project_store, monkeypatch, tmp_path, parent_root)
    monkeypatch.setattr(ProjectTransport, "request", lambda *a, **kw: pytest.fail("No revocation"))
    args = SimpleNamespace(command="context-remove", workspace=str(child), profile=None)
    with pytest.raises(AdapterError, match="remove_requires_binding_root"):
        context_cli.execute(args)
    assert (parent_root / "durumari.yaml").exists()
    assert not load_project_profile(parent["profile"]).disabled


def test_folder_source_requires_server_capability_before_browser(
    project_store, tmp_path, monkeypatch
):
    monkeypatch.setattr(context_cli, "os_store", lambda: project_store)
    monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
    monkeypatch.setattr(ProjectTransport, "request", lambda *a, **kw: {})
    monkeypatch.setattr(context_cli.webbrowser, "open", lambda url: pytest.fail("No approval"))
    args = SimpleNamespace(
        workspace=str(tmp_path), server="https://api.example.invalid", project=None
    )
    with pytest.raises(AdapterError, match="server_upgrade_required_for_folder_sources"):
        context_cli.connect(args)


def test_nested_manifest_repository_and_path_escape_cannot_inherit(tmp_path):
    scope = folder_profile(tmp_path)
    install_binding(scope, "folder", ["claude"])
    child = tmp_path / "docs"
    child.mkdir()
    scope.check_workspace(str(child))
    assert binding_root(child) == tmp_path
    from durumari_mcp.onboarding import repository_diagnostics

    assert repository_diagnostics(tmp_path)["repository_matches"]
    (child / "durumari.yaml").write_text("untrusted")
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(child))
    (child / "durumari.yaml").unlink()
    subprocess.run(["git", "init", "-q", str(child)], check=True)
    assert binding_root(child) == child
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(child))
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(tmp_path.parent))


def test_copied_manifest_and_mutated_source_do_not_grant_access(tmp_path):
    import shutil

    scope = folder_profile(tmp_path)
    install_binding(scope, "folder", ["claude"])
    copy = tmp_path.parent / (tmp_path.name + "-copy")
    shutil.copytree(tmp_path, copy)
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(copy))
    copied = scope.model_copy(update={"workspace": str(copy)})
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        copied.check_binding()
    manifest = tmp_path / "durumari.yaml"
    import yaml

    data = yaml.safe_load(manifest.read_text())
    data["folder_source_id"] = str(uuid4())
    data["repository_id"] = folder_identity(data["folder_source_id"])
    manifest.write_text(yaml.safe_dump(data))
    with pytest.raises(AdapterError, match="project_connection_scope_changed"):
        scope.check_binding()


def test_git_initialization_does_not_change_folder_grant_or_pending_scope(tmp_path):
    scope = folder_profile(tmp_path)
    install_binding(scope, "folder", ["claude"])
    box = ContextOutbox(tmp_path / "state", "folder", scope)
    path = f"/api/v2/projects/{scope.project_id}/contexts"
    box.enqueue("pending-original", "session", "POST", path, {"title": "synthetic"})
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "remote",
            "add",
            "origin",
            "https://git.example.invalid/team/new.git",
        ],
        check=True,
    )
    scope.check_workspace(str(tmp_path))
    assert box.pending("session") == 1
    other = scope.model_copy(update={"connection_id": "new-approved-connection"})
    with pytest.raises(AdapterError, match="context_outbox_scope_mismatch"):
        ContextOutbox(tmp_path / "state", "folder", other).pending("session")
    assert box.pending("session") == 1


def test_plugin_wrapper_accepts_public_folder_source():
    import importlib.util

    from plugin_layout import PLUGIN_SCRIPT as path
    spec = importlib.util.spec_from_file_location("folder_plugin", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = str(uuid4())
    args = module.parser().parse_args(
        ["connect", "--workspace", str(path.parent), "--folder-source", source]
    )
    assert args.folder_source == source


def test_directory_link_cannot_escape_folder_approval(tmp_path):
    import os

    scope = folder_profile(tmp_path)
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    link = tmp_path / "outside-link"
    if os.name == "nt":
        # Junction creation is available without Windows symlink privileges.
        source = str(link).replace("'", "''")
        target = str(outside).replace("'", "''")
        subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"New-Item -ItemType Junction -Path '{source}' -Target '{target}' | Out-Null",
            ],
            capture_output=True,
            check=True,
        )
    else:
        link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(link))
    assert outside.is_dir()


def test_equivalent_git_urls_keep_the_existing_identity(tmp_path):
    from durumari_mcp.onboarding import repository_identity

    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    urls = [
        "git@git.example.invalid:team/Repo.git",
        "https://git.example.invalid/team/Repo",
        "ssh://git@git.example.invalid/team/Repo.git",
    ]
    identities = []
    for remote in urls:
        subprocess.run(
            ["git", "-C", str(tmp_path), "config", "remote.origin.url", remote], check=True
        )
        identities.append(repository_identity(tmp_path))
    assert len(set(identities)) == 1
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "config",
            "remote.origin.url",
            "https://git.example.invalid/team/repo",
        ],
        check=True,
    )
    assert repository_identity(tmp_path) != identities[0]


@pytest.mark.parametrize("bad_selection", [None, "not-a-uuid", str(uuid4())])
def test_granted_folder_must_match_server_source_hash(
    project_store, tmp_path, monkeypatch, bad_selection
):
    monkeypatch.setattr(context_cli, "os_store", lambda: project_store)
    monkeypatch.setattr(context_cli.webbrowser, "open", lambda url: None)
    scope = folder_profile(tmp_path)

    def request(self, path, **kwargs):
        if path.endswith("/config"):
            return {"browser_origin": "https://api.example.invalid", "folder_sources": True}
        if path.endswith("/enrollments"):
            return enrollment(scope)
        if path.endswith("/poll"):
            result = approval(scope)
            result["folder_source_id"] = bad_selection
            return result
        pytest.fail("Invalid grant must not finish")

    monkeypatch.setattr(ProjectTransport, "request", request)
    args = SimpleNamespace(
        workspace=str(tmp_path),
        server="https://api.example.invalid",
        project=None,
        agents=["claude"],
    )
    with pytest.raises(
        AdapterError,
        match="enrollment_scope_changed|project_connection_scope_changed|invalid_folder_source",
    ):
        context_cli.connect(args)
    assert not (tmp_path / "durumari.yaml").exists()


@pytest.mark.parametrize("explicit", [False, True])
def test_existing_git_binding_never_silently_becomes_a_folder_source(
    project_store, tmp_path, monkeypatch, explicit
):
    import yaml

    git_manifest = {
        "schema_version": 1,
        "server": "https://api.example.invalid",
        "project_id": "project-1",
        "source_id": "source-1",
        "context_source": "server",
        "repository_id": "a" * 64,
    }
    path = tmp_path / "durumari.yaml"
    path.write_text(yaml.safe_dump(git_manifest), encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    if not explicit:
        # A failed Git lookup (no git, timeout, origin removed) keeps the Git contract.
        monkeypatch.setattr(context_cli, "os_store", lambda: project_store)
        monkeypatch.setattr(context_removal, "state_root", lambda: tmp_path / "state")
        monkeypatch.setattr(ProjectTransport, "request", lambda *a, **kw: pytest.fail("No call"))
        args = SimpleNamespace(workspace=str(tmp_path), server=None, project=None, agents=[])
        with pytest.raises(AdapterError, match="repository_root_and_remote_required"):
            context_cli.connect(args)
        assert path.read_text(encoding="utf-8") == before
        return
    # Switching to a folder source is only an explicit --folder-source choice.
    selected = str(uuid4())
    result, requests = connect_folder(
        project_store, monkeypatch, tmp_path, tmp_path, folder=selected
    )
    assert requests[-1]["folder_source_id"] == selected
    assert load_project_profile(result["profile"]).folder_source_id == selected


@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError("git"),
        subprocess.TimeoutExpired("git", 2),
        UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"),
        subprocess.CalledProcessError(128, "git"),
    ],
)
def test_failed_git_lookup_on_a_git_root_is_an_error_not_a_folder(tmp_path, monkeypatch, failure):
    from durumari_mcp import workspace_binding

    def fail(*args, **kwargs):
        raise failure

    monkeypatch.setattr(workspace_binding.subprocess, "check_output", fail)
    # Without its own .git the root cannot be a Git root: a plain folder.
    assert workspace_binding.git_remote(tmp_path) is None
    (tmp_path / ".git").mkdir()
    with pytest.raises(AdapterError, match="repository_root_and_remote_required"):
        workspace_binding.git_remote(tmp_path)


def test_git_root_without_origin_is_a_folder_and_origin_is_a_remote(tmp_path):
    from durumari_mcp.workspace_binding import git_remote

    assert git_remote(tmp_path) is None
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert git_remote(tmp_path) is None
    remote = "https://git.example.invalid/team/repo.git"
    subprocess.run(["git", "-C", str(tmp_path), "config", "remote.origin.url", remote], check=True)
    assert git_remote(tmp_path) == remote


def test_explicit_folder_source_skips_the_git_lookup(project_store, tmp_path, monkeypatch):
    monkeypatch.setattr(context_cli, "git_remote", lambda root: pytest.fail("No Git lookup"))
    selected = str(uuid4())
    _, requests = connect_folder(project_store, monkeypatch, tmp_path, tmp_path, folder=selected)
    assert requests[-1]["folder_source_id"] == selected
