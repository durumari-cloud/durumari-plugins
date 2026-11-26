"""Git imports use pinned objects, bounded provider APIs and no checkout execution."""

import base64
import hashlib
import io
import json
import os
import subprocess
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import pytest

from durumari_mcp import skill_git
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_skills import bundle_digest, validate_bundle

REPOSITORY = "https://github.com/team/skill"
COMMIT = "a" * 40
TREE = "b" * 40
SKILL = b"---\nname: imported-skill\ndescription: Imported instructions\n---\nNo execution.\n"


def oid(value):
    return hashlib.sha1(b"blob " + str(len(value)).encode() + b"\0" + value).hexdigest()


@pytest.mark.parametrize(
    "source,expected",
    [
        ("https://GitHub.com/Team/Skill.git/", "https://github.com/team/skill"),
        ("https://gitlab.com/team/nested/skill.git", "https://gitlab.com/team/nested/skill"),
        ("https://git.corp.example:443/team/skill", "https://git.corp.example/team/skill"),
    ],
)
def test_normalize_public_repository_url(source, expected):
    assert skill_git.normalize_repository_url(source) == expected


def test_default_public_ref_is_absent_provenance_not_an_empty_ref(monkeypatch):
    provider = PublicProvider(monkeypatch)
    result = skill_git.fetch_public(REPOSITORY)
    assert result["source"]["requested_ref"] is None
    assert result["source"]["commit"] == COMMIT
    assert provider.calls[0].endswith("/commits/HEAD")


@pytest.mark.parametrize(
    "source",
    [
        "http://github.com/team/skill",
        "https://token@github.com/team/skill",
        "https://u:p@github.com/team/skill",
        "https://github.com:8443/team/skill",
        "https://github.com/team/skill?token=private",
        "https://github.com/team/skill#main",
        "https://github.com/team/skill?",
        "https://github.com/team/skill#",
        "https://github.com/team/skill/tree/main",
        "https://github.com/team/../skill",
        "https://github.com/team%2fother/skill",
        "https://github.com/team\\other/skill",
        "https://github.com/team/%2e%2e",
        "https://github.com//team/skill",
    ],
)
def test_url_rejects_credentials_repository_views_and_ambiguous_paths(source):
    with pytest.raises(AdapterError, match="^invalid_git_repository_url$"):
        skill_git.normalize_repository_url(source)


class Response(io.BytesIO):
    status = 200

    def __init__(self, value, headers=None):
        super().__init__(value if isinstance(value, bytes) else json.dumps(value).encode())
        self.headers = headers or {}


class PublicProvider:
    def __init__(self, monkeypatch, files=None):
        self.calls = []
        self.handlers = []
        self.files = files or {"SKILL.md": SKILL}
        self.truncated = False
        self.bad_mode = None
        self.duplicate = False
        self.raise_error = None
        self.responses = None
        monkeypatch.setattr(skill_git, "build_opener", self.build)

    def build(self, *handlers):
        self.handlers.extend(handlers)
        return self

    def open(self, request, timeout):
        assert 0 < timeout <= 10
        assert request.method == "GET"
        assert "Authorization" not in request.headers
        self.calls.append(request.full_url)
        if self.raise_error:
            raise self.raise_error
        if self.responses:
            return self.responses(request)
        path = urlsplit(request.full_url).path
        if urlsplit(request.full_url).hostname == "raw.githubusercontent.com":
            prefix = "/team/skill/" + COMMIT + "/"
            assert path.startswith(prefix)
            return Response(self.files[path.removeprefix(prefix)])
        if path.endswith("/commits/main") or path.endswith("/commits/HEAD"):
            return Response(
                {
                    "sha": COMMIT,
                    "commit": {"tree": {"sha": TREE}},
                    "url": "https://malicious.invalid/never-follow",
                }
            )
        if path.endswith("/git/trees/" + TREE):
            entries = [
                {
                    "path": name,
                    "mode": self.bad_mode or "100644",
                    "type": "blob",
                    "sha": oid(content),
                    "size": len(content),
                    "url": "https://malicious.invalid/never-follow",
                }
                for name, content in self.files.items()
            ]
            if self.duplicate:
                entries.append(entries[0])
            return Response({"sha": TREE, "tree": entries, "truncated": self.truncated})
        for value in self.files.values():
            if path.endswith("/git/blobs/" + oid(value)):
                return Response(
                    {
                        "sha": oid(value),
                        "size": len(value),
                        "encoding": "base64",
                        "content": base64.b64encode(value).decode(),
                        "download_url": "https://malicious.invalid/never-follow",
                    }
                )
        raise AssertionError("Unexpected API route")


def test_github_import_pins_ref_and_ignores_server_urls(monkeypatch):
    provider = PublicProvider(
        monkeypatch, {"SKILL.md": SKILL, "scripts/a.py": b"raise SystemExit()\n"}
    )
    imported = skill_git.fetch_public(REPOSITORY, ref="main")
    assert imported["files"]["SKILL.md"] == SKILL.decode()
    assert imported["source"] == {
        "repository_url": REPOSITORY,
        "path": "",
        "requested_ref": "main",
        "commit": COMMIT,
        "transport": "public_api",
    }
    assert provider.calls[0] == "https://api.github.com/repos/team/skill/commits/main"
    assert {urlsplit(url).hostname for url in provider.calls} == {
        "api.github.com",
        "raw.githubusercontent.com",
    }
    assert all(
        COMMIT in url
        for url in provider.calls
        if urlsplit(url).hostname == "raw.githubusercontent.com"
    )
    assert any(isinstance(h, skill_git.NoRedirect) for h in provider.handlers)
    assert any(getattr(h, "proxies", None) == {} for h in provider.handlers)
    row = {
        "name": "imported-skill",
        "version": "1.0.0",
        "requirements": [],
        "files": imported["files"],
        "source": imported["source"],
    }
    row["sha256"] = bundle_digest(row)
    validate_bundle(row)
    row["source"]["requested_ref"] = "different"
    assert bundle_digest(row) == row["sha256"]


def test_public_external_files_include_licenses_agent_guides_and_images(monkeypatch):
    from durumari_mcp.skill_files import file_bytes

    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH8sAAAAASUVORK5CYII="
    )
    files = {
        "SKILL.md": SKILL,
        "LICENSE.txt": b"original license",
        "AGENTS.md": b"agent guide",
        "agents/review.md": b"review",
        "eval-viewer/viewer.html": b"<html>data</html>",
        "assets/icon.png": image,
    }
    provider = PublicProvider(monkeypatch, files)
    imported = skill_git.fetch_public(REPOSITORY, ref="main")
    assert all(file_bytes(name, imported["files"][name]) == value for name, value in files.items())
    assert len([url for url in provider.calls if urlsplit(url).hostname == "api.github.com"]) == 2
    assert len(
        [url for url in provider.calls if urlsplit(url).hostname == "raw.githubusercontent.com"]
    ) == len(files)


def test_raw_content_is_still_checked_against_the_pinned_git_object(monkeypatch):
    provider = PublicProvider(monkeypatch)
    original = provider.open

    def changed(request, timeout):
        if urlsplit(request.full_url).hostname == "raw.githubusercontent.com":
            return Response(SKILL.replace(b"No execution.", b"Wrong object."))
        return original(request, timeout)

    provider.open = changed
    with pytest.raises(AdapterError, match="invalid_git_skill"):
        skill_git.fetch_public(REPOSITORY, ref="main")


@pytest.mark.parametrize(
    "repository,path,name",
    [
        ("https://gitlab.com/team/skill", "", "SKILL.md"),
        ("https://github.com.evil.invalid/team/skill", "", "SKILL.md"),
        (REPOSITORY, "../other", "SKILL.md"),
        (REPOSITORY, "", "../outside"),
    ],
)
def test_raw_content_does_not_accept_arbitrary_hosts_or_paths(monkeypatch, repository, path, name):
    provider = PublicProvider(monkeypatch)
    api = skill_git.PublicAPI("https://api.github.com")
    with pytest.raises(AdapterError):
        api.github_file(repository, COMMIT, path, name)
    assert provider.calls == []


@pytest.mark.parametrize(
    "change",
    [
        lambda p: setattr(p, "truncated", True),
        lambda p: setattr(p, "bad_mode", "120000"),
        lambda p: setattr(p, "bad_mode", "160000"),
        lambda p: setattr(p, "duplicate", True),
        lambda p: p.files.update({"references/../escape": b"unsafe"}),
        lambda p: p.files.update({"references/CON": b"unsafe"}),
        lambda p: p.files.update({"references/a": b"\xff"}),
        lambda p: p.files.update({"references/a": b"x" * 128001}),
        lambda p: p.files.update(
            {"references/a": b"version https://git-lfs.github.com/spec/v1\noid sha256:x\n"}
        ),
        lambda p: p.files.update({f"references/{i}": b"x" for i in range(100)}),
    ],
)
def test_public_import_rejects_incomplete_unsafe_or_oversized_bundle(monkeypatch, change):
    provider = PublicProvider(monkeypatch)
    change(provider)
    with pytest.raises(AdapterError):
        skill_git.fetch_public(REPOSITORY)


def test_public_only_contacts_fixed_hosts(monkeypatch):
    provider = PublicProvider(monkeypatch)
    with pytest.raises(AdapterError, match="checkout_required"):
        skill_git.fetch_public("https://git.corp.example/team/skill")
    assert provider.calls == []


@pytest.mark.parametrize(
    "status,code",
    [
        (302, "git_redirect_denied"),
        (401, "checkout_required"),
        (403, "checkout_required"),
        (404, "checkout_required"),
        (429, "git_fetch_rate_limited"),
        (500, "git_fetch_failed"),
    ],
)
def test_http_errors_never_disclose_credentials_or_upstream_body(monkeypatch, status, code, capsys):
    provider = PublicProvider(monkeypatch)
    provider.raise_error = HTTPError(
        "https://token@secret.invalid/private",
        status,
        "private-token-value",
        {},
        io.BytesIO(b"private-body"),
    )
    with pytest.raises(AdapterError, match="^" + code + "$") as error:
        skill_git.fetch_public(REPOSITORY)
    captured = capsys.readouterr()
    assert "private" not in str(error.value) + captured.out + captured.err


@pytest.mark.parametrize(
    "repository,status,remaining,expected",
    [
        (REPOSITORY, 403, "0", "git_fetch_rate_limited"),
        (REPOSITORY, 403, "1", "checkout_required"),
        (REPOSITORY, 403, None, "checkout_required"),
        ("https://gitlab.com/team/skill", 403, "0", "checkout_required"),
        ("https://gitlab.com/team/skill", 429, None, "git_fetch_rate_limited"),
    ],
)
def test_public_api_classifies_rate_limits_without_reading_provider_errors(
    monkeypatch, capsys, repository, status, remaining, expected
):
    provider = PublicProvider(monkeypatch)
    body = io.BytesIO(b"private-provider-body")
    headers = {} if remaining is None else {"X-RateLimit-Remaining": remaining}
    provider.raise_error = HTTPError(
        "https://token@secret.invalid/private", status, "private-token-value", headers, body
    )
    with pytest.raises(AdapterError, match="^" + expected + "$") as error:
        skill_git.fetch_public(repository)
    assert body.tell() == 0
    captured = capsys.readouterr()
    assert "private" not in str(error.value) + captured.out + captured.err


def run_git(repository, *args, input=None):
    return subprocess.run(
        ["git", "-C", str(repository), *args],
        input=input,
        check=True,
        capture_output=True,
    ).stdout


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    run_git(root, "init", "-b", "main")
    run_git(root, "config", "user.name", "Synthetic User")
    run_git(root, "config", "user.email", "synthetic@example.invalid")
    run_git(root, "remote", "add", "origin", "git@github.com:team/skill.git")
    (root / "skills/team/scripts").mkdir(parents=True)
    (root / "skills/team/SKILL.md").write_bytes(SKILL)
    (root / "skills/team/scripts/test.py").write_text("raise RuntimeError('must never run')\n")
    run_git(root, "add", ".")
    # Synthetic fixture only: plumbing creates no hooks, worktree switch or remote action.
    tree = run_git(root, "write-tree").strip().decode()
    commit = run_git(root, "commit-tree", tree, input=b"Synthetic fixture\n").strip().decode()
    run_git(root, "update-ref", "refs/heads/main", commit)
    return root, commit


def test_checkout_reads_only_pinned_committed_objects_and_matches_origin(checkout):
    root, commit = checkout
    (root / "skills/team/SKILL.md").write_text("dirty content")
    (root / "skills/team/references").mkdir()
    (root / "skills/team/references/untracked.md").write_text("untracked private content")
    imported = skill_git.read_checkout(REPOSITORY, root, path="skills/team")
    assert imported["files"]["SKILL.md"] == SKILL.decode()
    assert "references/untracked.md" not in imported["files"]
    assert imported["source"]["commit"] == commit
    assert imported["source"]["transport"] == "agent_checkout"
    assert imported["source"]["path"] == "skills/team"
    assert imported["source"]["requested_ref"] is None


def test_checkout_origin_mismatch_fails_without_leaking_actual_remote(checkout, capsys):
    root, _ = checkout
    run_git(root, "config", "remote.origin.url", "https://token@private.invalid/team/other.git")
    with pytest.raises(AdapterError) as error:
        skill_git.read_checkout(REPOSITORY, root, path="skills/team")
    captured = capsys.readouterr()
    assert "token" not in str(error.value) + captured.out + captured.err
    assert "private.invalid" not in str(error.value)


@pytest.mark.parametrize(
    "ref,path",
    [
        ("--help", "skills/team"),
        ("main:SKILL.md", "skills/team"),
        ("main", "../skills"),
        ("main", "/skills/team"),
    ],
)
def test_checkout_ref_and_path_cannot_be_git_options_or_revision_expressions(checkout, ref, path):
    root, _ = checkout
    with pytest.raises(AdapterError):
        skill_git.read_checkout(REPOSITORY, root, ref=ref, path=path)


def test_missing_checkout_requires_agent_preparation(tmp_path):
    with pytest.raises(AdapterError, match="checkout_required"):
        skill_git.read_checkout(REPOSITORY, tmp_path / "missing")


def test_github_selected_folder_descends_by_tree_sha_before_recursive_read(monkeypatch):
    provider = PublicProvider(monkeypatch)
    subtree, selected = "c" * 40, "d" * 40

    def response(request):
        url = urlsplit(request.full_url)
        if url.path.endswith("/commits/HEAD"):
            return Response({"sha": COMMIT, "commit": {"tree": {"sha": TREE}}})
        if url.path.endswith("/git/trees/" + TREE):
            assert not url.query
            return Response(
                {
                    "sha": TREE,
                    "truncated": False,
                    "tree": [
                        {"path": "skills", "mode": "040000", "type": "tree", "sha": subtree},
                        {"path": ".gitignore", "mode": "100644", "type": "blob", "sha": "e" * 40},
                    ],
                }
            )
        if url.path.endswith("/git/trees/" + subtree):
            assert not url.query
            return Response(
                {
                    "sha": subtree,
                    "truncated": False,
                    "tree": [
                        {"path": "team", "mode": "040000", "type": "tree", "sha": selected},
                    ],
                }
            )
        if url.path.endswith("/git/trees/" + selected):
            assert parse_qs(url.query) == {"recursive": ["1"]}
            return Response(
                {
                    "sha": selected,
                    "truncated": False,
                    "tree": [
                        {
                            "path": "SKILL.md",
                            "mode": "100644",
                            "type": "blob",
                            "sha": oid(SKILL),
                            "size": len(SKILL),
                        },
                    ],
                }
            )
        if url.hostname == "raw.githubusercontent.com":
            assert url.path == "/team/skill/" + COMMIT + "/skills/team/SKILL.md"
            return Response(SKILL)
        raise AssertionError("Unexpected API route")

    provider.responses = response
    imported = skill_git.fetch_public(REPOSITORY, path="skills/team")
    assert imported["files"] == {"SKILL.md": SKILL.decode()}
    assert imported["source"]["commit"] == COMMIT
    assert len(provider.calls) == 5


def test_gitlab_pagination_is_constructed_locally_and_pinned_before_raw_blobs(monkeypatch):
    provider = PublicProvider(monkeypatch)
    script = b"raise RuntimeError('never execute')\n"
    values = {oid(SKILL): SKILL, oid(script): script}
    first = [{"path": "skills/team/SKILL.md", "mode": "100644", "type": "blob", "id": oid(SKILL)}]
    first.extend(
        {"path": f"skills/team/references/{i}", "mode": "040000", "type": "tree", "id": "e" * 40}
        for i in range(99)
    )

    def response(request):
        url = urlsplit(request.full_url)
        assert url.hostname == "gitlab.com"
        assert url.path.startswith("/api/v4/projects/team%2Fnested%2Frepo/repository/")
        if url.path.endswith("/commits/release%2Fv1"):
            return Response({"id": COMMIT, "web_url": "https://evil.invalid/never"})
        if url.path.endswith("/tree"):
            query = parse_qs(url.query)
            assert query["ref"] == [COMMIT] and query["path"] == ["skills/team"]
            assert query["per_page"] == ["100"]
            if query["page"] == ["1"]:
                return Response(
                    first,
                    {
                        "X-Next-Page": "2",
                        "X-Total": "101",
                        "Link": '<https://evil.invalid/never?page=2>; rel="next"',
                    },
                )
            assert query["page"] == ["2"]
            return Response(
                [
                    {
                        "path": "skills/team/scripts/run.py",
                        "mode": "100755",
                        "type": "blob",
                        "id": oid(script),
                    }
                ],
                {"X-Total": "101"},
            )
        for sha, value in values.items():
            if url.path.endswith("/blobs/" + sha + "/raw"):
                return Response(value)
        raise AssertionError("Unexpected API route")

    provider.responses = response
    imported = skill_git.fetch_public(
        "https://gitlab.com/team/nested/repo.git", ref="release/v1", path="skills/team"
    )
    assert imported["files"] == {"SKILL.md": SKILL.decode(), "scripts/run.py": script.decode()}
    assert imported["source"]["commit"] == COMMIT
    assert not any("evil.invalid" in url for url in provider.calls)


def test_gitlab_truncated_page_or_wrong_scope_fails_before_any_blob(monkeypatch):
    provider = PublicProvider(monkeypatch)

    def response(request):
        if "/commits/" in request.full_url:
            return Response({"id": COMMIT})
        assert "/tree?" in request.full_url
        return Response(
            [{"path": "SKILL.md", "type": "blob", "mode": "100644", "id": oid(SKILL)}],
            {"X-Total": "2"},
        )

    provider.responses = response
    with pytest.raises(AdapterError, match="git_tree_invalid"):
        skill_git.fetch_public("https://gitlab.com/team/repo")
    assert len(provider.calls) == 2


def test_public_total_content_limit_is_checked_before_partial_result(monkeypatch):
    provider = PublicProvider(
        monkeypatch, {"SKILL.md": SKILL, **{f"references/{i}": b"x" * 128000 for i in range(4)}}
    )
    with pytest.raises(AdapterError, match="git_import_limit"):
        skill_git.fetch_public(REPOSITORY)
    assert len(provider.calls) == 2


@pytest.mark.parametrize("limit", ["MAX_HTTP_BYTES", "MAX_REQUESTS", "MAX_SECONDS"])
def test_public_global_transfer_request_and_time_budgets_are_enforced(monkeypatch, limit):
    provider = PublicProvider(monkeypatch)
    monkeypatch.setattr(skill_git, limit, 0)
    with pytest.raises(AdapterError, match="git_import_limit"):
        skill_git.fetch_public(REPOSITORY)
    assert len(provider.calls) <= 1


def synthetic_commit(root):
    tree = run_git(root, "write-tree").strip().decode()
    commit = run_git(root, "commit-tree", tree, input=b"Synthetic next fixture\n").strip().decode()
    run_git(root, "update-ref", "refs/heads/main", commit)
    return commit


@pytest.mark.parametrize("mode", ["120000", "160000"])
def test_private_symlinks_and_gitlinks_are_rejected_without_following(checkout, mode):
    root, commit = checkout
    if mode == "120000":
        blob = (
            run_git(root, "hash-object", "-w", "--stdin", input=b"/never/read/private/key")
            .strip()
            .decode()
        )
    else:
        blob = commit
    run_git(root, "update-index", "--add", "--cacheinfo", mode, blob, "skills/team/assets/link")
    synthetic_commit(root)
    with pytest.raises(AdapterError, match="git_tree_invalid"):
        skill_git.read_checkout(REPOSITORY, root, path="skills/team")


def test_private_git_uses_only_read_commands_and_blocks_lazy_fetch_hooks_and_config(
    checkout, monkeypatch
):
    root, _ = checkout
    real = skill_git.subprocess.Popen
    calls = []
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.sshCommand")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "untrusted-command")

    def guarded(command, **kwargs):
        env = kwargs["env"]
        assert "GIT_CONFIG_COUNT" not in env
        assert env["GIT_NO_LAZY_FETCH"] == "1" and env["GIT_ALLOW_PROTOCOL"] == ""
        assert env["GIT_CONFIG_GLOBAL"] == os.devnull
        assert env["GIT_NO_REPLACE_OBJECTS"] == "1" and env["GIT_TERMINAL_PROMPT"] == "0"
        assert kwargs["stderr"] == subprocess.DEVNULL and kwargs["shell"] is False
        assert not any(arg in {"clone", "fetch", "checkout", "submodule", "lfs"} for arg in command)
        calls.append(command)
        return real(command, **kwargs)

    monkeypatch.setattr(skill_git.subprocess, "Popen", guarded)
    imported = skill_git.read_checkout(REPOSITORY, root, path="skills/team")
    assert imported["files"]["scripts/test.py"].startswith("raise RuntimeError")
    assert len(calls) == 7


@pytest.mark.parametrize(
    "header",
    [
        "---\nname: &a imported-skill\ndescription: *a\n---\n",
        "---\nname: Wrong_Name\ndescription: example\n---\n",
        "---\nname: imported-skill\ndescription: " + "x" * 8192 + "\n---\n",
    ],
)
def test_name_parser_rejects_anchors_invalid_names_and_unbounded_frontmatter(header):
    with pytest.raises(AdapterError, match="invalid_git_skill"):
        skill_git.skill_name({"SKILL.md": header})


def test_name_parser_is_pure_and_accepts_quoted_name():
    assert (
        skill_git.skill_name({"SKILL.md": '---\nname: "imported-skill"\ndescription: text\n---\n'})
        == "imported-skill"
    )


def test_gitlab_short_page_with_next_link_is_rejected_not_silently_truncated(monkeypatch):
    provider = PublicProvider(monkeypatch)

    def response(request):
        if "/commits/" in request.full_url:
            return Response({"id": COMMIT})
        return Response(
            [{"path": "SKILL.md", "mode": "100644", "type": "blob", "id": oid(SKILL)}],
            {"Link": '<https://evil.invalid/next>; rel="next"'},
        )

    provider.responses = response
    with pytest.raises(AdapterError, match="git_tree_invalid"):
        skill_git.fetch_public("https://gitlab.com/team/repo")
    assert len(provider.calls) == 2


def test_malformed_tree_mode_is_a_structured_error(monkeypatch):
    provider = PublicProvider(monkeypatch)
    provider.bad_mode = ["100644"]
    with pytest.raises(AdapterError, match="git_tree_invalid"):
        skill_git.fetch_public(REPOSITORY)


def test_checkout_rejects_multiple_origin_urls_instead_of_only_comparing_last(checkout):
    root, _ = checkout
    run_git(root, "config", "--unset-all", "remote.origin.url")
    run_git(root, "config", "--add", "remote.origin.url", "https://github.com/other/source.git")
    run_git(root, "config", "--add", "remote.origin.url", REPOSITORY)
    with pytest.raises(AdapterError, match="git_origin_mismatch"):
        skill_git.read_checkout(REPOSITORY, root, path="skills/team")


def test_checkout_rejects_local_url_rewrite_to_another_effective_origin(checkout):
    root, _ = checkout
    run_git(root, "config", "remote.origin.url", REPOSITORY)
    run_git(root, "config", "url.https://github.com/other/.insteadOf", "https://github.com/team/")
    with pytest.raises(AdapterError, match="git_origin_mismatch"):
        skill_git.read_checkout(REPOSITORY, root, path="skills/team")


def test_checkout_does_not_run_git_filters_or_hooks(checkout):
    root, _ = checkout
    (root / ".gitattributes").write_text("skills/team/SKILL.md filter=unsafe\n")
    run_git(root, "config", "filter.unsafe.smudge", "touch filter-ran")
    hook = root / ".git/hooks/post-checkout"
    hook.write_text("#!/bin/sh\ntouch hook-ran\n")
    hook.chmod(0o755)
    run_git(root, "add", ".gitattributes")
    synthetic_commit(root)
    imported = skill_git.read_checkout(REPOSITORY, root, path="skills/team")
    assert imported["files"]["SKILL.md"] == SKILL.decode()
    assert not (root / "filter-ran").exists() and not (root / "hook-ran").exists()
