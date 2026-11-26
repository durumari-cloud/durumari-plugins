"""Tests never write backups into the real user data folder."""

import os

import pytest

from durumari_mcp import local_upgrade

# Git exports GIT_DIR (and GIT_INDEX_FILE, ...) into hooks. A run started inside a hook
# would send a temp repository's `git init`/`git config` to the invoking repository.
# This package is tested on its own, so it keeps its own list of these variables;
# drop them before any fixture runs git.
for _name in (
    "GIT_DIR",
    "GIT_COMMON_DIR",
    "GIT_WORK_TREE",
    "GIT_IMPLICIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_PREFIX",
    "GIT_INTERNAL_SUPER_PREFIX",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_SHALLOW_FILE",
    "GIT_GRAFT_FILE",
    "GIT_NO_REPLACE_OBJECTS",
    "GIT_REPLACE_REF_BASE",
    "GIT_CONFIG",
):
    os.environ.pop(_name, None)


@pytest.fixture(autouse=True)
def isolated_user_data(tmp_path_factory, monkeypatch):
    data = tmp_path_factory.mktemp("user-data")
    monkeypatch.setattr(local_upgrade, "user_data_directory", lambda *args, **kwargs: data)
    return data


@pytest.fixture(autouse=True)
def no_bundled_claude(monkeypatch):
    # The developer's own Claude app copy must not count as an installed Agent in tests.
    from durumari_mcp import releases

    monkeypatch.setattr(releases, "bundled_claude", lambda: None)


@pytest.fixture(autouse=True)
def no_server_announcement(tmp_path_factory, monkeypatch):
    # Requests keep the stored host unless a test announces a move itself.
    from durumari_mcp import releases, server_aliases

    cache = tmp_path_factory.mktemp("server-hosts") / "server-hosts.json"
    monkeypatch.setattr(server_aliases, "_announced", {})
    monkeypatch.setattr(server_aliases, "_cache_path", lambda: cache)
    monkeypatch.setattr(server_aliases, "fetch_app_origin", lambda origin: None)
    monkeypatch.setattr(releases, "announced_app_origin", lambda host: None)
