"""A Git root with non-ASCII characters is the same workspace on Windows.

Windows decodes text subprocess output with its ANSI code page unless told otherwise.
The fixture reproduces that on every OS: a text call without an explicit encoding is
decoded as cp949, the code page of the reported Korean Windows machine.
"""

import io
import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest
from test_context_v2_adapter import context_profile

from durumari_mcp import cli, context_cli
from durumari_mcp.context_binding import install_binding

REAL_CHECK_OUTPUT = subprocess.check_output


def windows_code_page(*args, **kwargs):
    if kwargs.get("text") and not kwargs.get("encoding"):
        kwargs = {**kwargs, "encoding": "cp949"}
    return REAL_CHECK_OUTPUT(*args, **kwargs)


@pytest.fixture
def code_page(monkeypatch):
    monkeypatch.setattr(subprocess, "check_output", windows_code_page)


@pytest.mark.parametrize(
    "name", ["SSAFY 창업 _ Durumari", "プロジェクト", "rocket 🚀", "with space"]
)
def test_non_ascii_git_root_stays_in_scope(tmp_path, code_page, name):
    root = tmp_path / name
    root.mkdir()
    profile = context_profile(root)
    install_binding(profile, "member-a", ["claude"])
    profile.check_workspace(str(root))
    nested = root / "src" / "모듈"
    nested.mkdir(parents=True)
    profile.check_workspace(str(nested))


def test_local_repository_identity_is_stable_for_non_ascii_paths(tmp_path, code_page):
    from durumari_mcp.onboarding import repository_identity

    main = tmp_path / "창업"
    subprocess.run(["git", "init", "-q", str(main)], check=True)
    subprocess.run(
        ["git", "-C", str(main), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.com",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.com",
            "PATH": os.environ["PATH"],
        },
    )
    linked = tmp_path / "작업트리"
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", str(linked)], check=True)
    # A linked worktree reports the main repository's absolute .git path.
    assert repository_identity(linked) == repository_identity(main)


class KoreanContext:
    def read(self, path="", **query):
        return {"items": [{"title": "결정 — 한글 제목 🚀", "path": path}]}


def cp1252_pipe(monkeypatch):
    # A redirected Python stdout on an en-US Windows uses the ANSI code page strictly.
    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    return stream


@pytest.mark.parametrize("command", [["read", "--id", "context"], ["search", "--query", "결정"]])
def test_context_cli_prints_korean_results_through_a_cp1252_pipe(
    tmp_path, monkeypatch, capsys, command
):
    # the CLI failed here and blamed the project files as non-UTF-8.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        context_cli,
        "load_project_profile",
        lambda name: SimpleNamespace(check_workspace=lambda path: None, workspace_identity=None),
    )
    monkeypatch.setattr(context_cli, "ProjectGateway", lambda name, client: object())
    monkeypatch.setattr(context_cli, "ContextClient", lambda gateway, session: KoreanContext())
    argv = ["durumari-mcp", "context", *command, "--workspace", str(tmp_path), "--profile", "team"]
    monkeypatch.setattr(sys, "argv", argv)
    stream = cp1252_pipe(monkeypatch)
    cli.main()
    stream.flush()
    printed = stream.buffer.getvalue()
    assert json.loads(printed)["items"][0]["title"] == "결정 — 한글 제목 🚀"
    assert "workspace_text_invalid_utf8" not in capsys.readouterr().err


@pytest.mark.parametrize("encoding", ["cp949", "cp1252"])
def test_json_on_a_legacy_code_page_pipe_is_ascii_for_utf8_readers(monkeypatch, encoding):
    # Agents decode a child's pipe as UTF-8. Korean text cp949 can encode still has to
    # arrive as ASCII escapes, not cp949 bytes.
    stream = io.TextIOWrapper(io.BytesIO(), encoding=encoding, errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    context_cli._emit_json({"title": "회귀 테스트"})
    stream.flush()
    printed = stream.buffer.getvalue()
    assert printed.isascii()
    assert json.loads(printed.decode("utf-8"))["title"] == "회귀 테스트"


def test_secret_receipt_on_a_cp1252_pipe_is_ascii_json(monkeypatch):
    from durumari_mcp.json_output import emit_json

    stream = io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    emit_json({"items": [{"label": "배포 토큰"}]})
    stream.flush()
    assert json.loads(stream.buffer.getvalue())["items"][0]["label"] == "배포 토큰"


def test_context_cli_keeps_readable_text_when_stdout_is_utf8(monkeypatch):
    stream = io.TextIOWrapper(io.BytesIO(), encoding="utf-8", errors="strict")
    monkeypatch.setattr(sys, "stdout", stream)
    context_cli._emit_json({"title": "한글"})
    stream.flush()
    assert stream.buffer.getvalue().decode("utf-8").strip() == '{"title": "한글"}'


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UnicodeEncodeError("cp1252", "한", 0, 1, "undefined"), "operation_failed"),
        (UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), "workspace_text_invalid_utf8"),
    ],
)
def test_only_decoding_failures_blame_workspace_text(
    tmp_path, monkeypatch, capsys, error, expected
):
    def fail(args):
        raise error

    monkeypatch.setattr(cli, "execute", fail)
    monkeypatch.setattr(
        sys, "argv", ["durumari-mcp", "context-status", "--workspace", str(tmp_path)]
    )
    with pytest.raises(SystemExit) as exited:
        cli.main()
    assert exited.value.code == 2
    message = capsys.readouterr().err
    assert expected in message
    assert ("UTF-8로 저장" in message) == (expected == "workspace_text_invalid_utf8")
