"""Gemini CLI installs the same verified plugin folder as an extension."""

import hashlib
import json
import os
import shutil
import sys
from argparse import Namespace

import pytest
from test_releases import bundle, manifest

from durumari_mcp import releases
from durumari_mcp.releases import ReleaseError, extract_catalog

EXTENSION = "durumari-catalog/plugins/durumari-agent/gemini-extension.json"


def extension(**changes):
    item = {"name": "durumari", "version": "0.2.0", "description": "Cloud Context"}
    item.update(changes)
    return {EXTENSION: json.dumps({k: v for k, v in item.items() if v is not None}).encode()}


def test_catalog_accepts_exact_gemini_extension(tmp_path):
    root = extract_catalog(bundle(extension()), tmp_path, "0.2.0")
    assert (root / "plugins/durumari-agent/gemini-extension.json").is_file()


@pytest.mark.parametrize(
    "files",
    [
        extension(version="0.1.0"),
        extension(name="durumari-fork"),
        # No fixed MCP server, secret settings, hidden tools or extra context in the package.
        extension(mcpServers={"durumari_project": {"command": "durumari-mcp"}}),
        extension(settings=[{"name": "token", "envVar": "TOKEN", "sensitive": True}]),
        extension(excludeTools=["run_shell_command"]),
        extension(contextFileName="GEMINI.md"),
        {"durumari-catalog/plugins/durumari-agent/GEMINI.md": b"instructions"},
        {"durumari-catalog/plugins/durumari-agent/hooks/hooks.json": b"{}"},
    ],
)
def test_catalog_rejects_unexpected_gemini_contents(tmp_path, files):
    with pytest.raises(ReleaseError, match="invalid_release_archive"):
        extract_catalog(bundle(files), tmp_path, "0.2.0")


def catalog(tmp_path, version):
    plugin = tmp_path / f"catalog-{version}/plugins/durumari-agent"
    (plugin / "skills/context").mkdir(parents=True)
    (plugin / "skills/context/SKILL.md").write_text(version)
    (plugin / "gemini-extension.json").write_text(
        json.dumps({"name": "durumari", "version": version, "description": "x"})
    )
    return plugin.parents[1]


class FakeGemini:
    """Mirror Gemini CLI's documented copy-on-install and local-source update."""

    def __init__(self, target, *, ignore_update=False):
        self.target = target
        self.calls = []
        self.ignore_update = ignore_update

    def __call__(self, command):
        self.calls.append(command[:3])
        if command[2] == "install":
            source = command[3]
        else:
            if self.ignore_update:
                return
            source = json.loads((self.target / ".gemini-extension-install.json").read_text())[
                "source"
            ]
            shutil.rmtree(self.target)
        shutil.copytree(source, self.target)
        (self.target / ".gemini-extension-install.json").write_text(
            json.dumps({"source": source, "type": "local"})
        )


@pytest.fixture
def gemini(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_CLI_HOME", str(tmp_path / "gemini-home"))
    target = tmp_path / "gemini-home/.gemini/extensions/durumari"
    assert releases.gemini_extension_directory() == target
    fake = FakeGemini(target)
    monkeypatch.setattr(releases, "run_with_host_consent", fake)
    monkeypatch.setattr(releases, "run", lambda *a: pytest.fail("captured host command"))
    return fake


def test_gemini_install_update_rollback_and_noop(tmp_path, gemini):
    other = gemini.target.parent / "other-extension"
    other.mkdir(parents=True)
    (other / "gemini-extension.json").write_text('{"name": "other-extension"}')
    first, second = catalog(tmp_path, "0.2.0"), catalog(tmp_path, "0.3.0")
    # The updater keeps one stable catalog path and swaps its contents.
    stable = tmp_path / "releases/catalog"
    shutil.copytree(first, stable)
    releases.install_host("gemini", stable, "0.2.0")
    assert gemini.calls == [["gemini", "extensions", "install"]]
    releases.install_host("gemini", stable, "0.2.0")
    assert len(gemini.calls) == 1
    shutil.rmtree(stable)
    shutil.copytree(second, stable)
    releases.install_host("gemini", stable, "0.3.0")
    assert gemini.calls[-1] == ["gemini", "extensions", "update"]
    assert (gemini.target / "skills/context/SKILL.md").read_text() == "0.3.0"
    shutil.rmtree(stable)
    shutil.copytree(first, stable)
    releases.install_host("gemini", stable, "0.2.0")
    assert releases.gemini_extension_version(gemini.target) == "0.2.0"
    assert (other / "gemini-extension.json").read_text() == '{"name": "other-extension"}'


def test_gemini_update_without_change_is_not_reported_as_success(tmp_path, gemini):
    stable = tmp_path / "releases/catalog"
    shutil.copytree(catalog(tmp_path, "0.2.0"), stable)
    releases.install_host("gemini", stable, "0.2.0")
    gemini.ignore_update = True
    shutil.rmtree(stable)
    shutil.copytree(catalog(tmp_path, "0.3.0"), stable)
    with pytest.raises(ReleaseError, match="native_plugin_version_not_verified"):
        releases.install_host("gemini", stable, "0.3.0")


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        ("foreign", "gemini_extension_directory_not_owned"),
        ("symlink", "gemini_extension_directory_not_owned"),
        ("git", "gemini_extension_installed_from_other_source"),
        ("other-path", "gemini_extension_installed_from_other_source"),
    ],
)
def test_gemini_install_refuses_extensions_it_does_not_own(tmp_path, gemini, setup, code):
    source = catalog(tmp_path, "0.2.0")
    target = gemini.target
    target.parent.mkdir(parents=True)
    if setup == "foreign":
        target.mkdir()
        (target / "gemini-extension.json").write_text('{"name": "someone", "version": "1.0.0"}')
    elif setup == "symlink":
        target.symlink_to(source / "plugins/durumari-agent", target_is_directory=True)
    else:
        shutil.copytree(source / "plugins/durumari-agent", target)
        metadata = (
            {"source": "https://github.com/example/durumari", "type": "git"}
            if setup == "git"
            else {"source": str(tmp_path / "elsewhere"), "type": "local"}
        )
        (target / ".gemini-extension-install.json").write_text(json.dumps(metadata))
    before = sorted(str(path) for path in target.parent.rglob("*"))
    with pytest.raises(ReleaseError, match=code):
        releases.install_host("gemini", source, "0.2.0")
    assert gemini.calls == []
    assert sorted(str(path) for path in target.parent.rglob("*")) == before


def test_gemini_consent_is_never_attempted_without_a_terminal(monkeypatch):
    monkeypatch.setattr(releases.sys.stdin, "isatty", lambda: False, raising=False)
    monkeypatch.setattr(
        releases.subprocess, "run", lambda *a, **kw: pytest.fail("host prompt started")
    )
    with pytest.raises(ReleaseError, match="gemini_consent_requires_interactive_terminal"):
        releases.run_with_host_consent(["gemini", "extensions", "install", "path"])


def test_null_device_stdin_is_not_a_terminal(monkeypatch):
    # The detached runner starts with stdin=DEVNULL; Python 3.12 on Windows answers isatty() True
    # for NUL, which let Gemini CLI start a consent prompt nobody could answer (the update hung).
    # No skip: the CI receipt rejects any skipped test outside its platform list.
    with open(os.devnull, encoding="utf-8") as nul:
        monkeypatch.setattr(releases.sys, "stdin", nul)
        assert releases.stdin_is_terminal() is False
        monkeypatch.setattr(
            releases.subprocess, "run", lambda *a, **kw: pytest.fail("host prompt started")
        )
        with pytest.raises(ReleaseError, match="gemini_consent_requires_interactive_terminal"):
            releases.run_with_host_consent(["gemini", "extensions", "update", "durumari"])


def test_gemini_host_output_and_prompt_stay_off_json_stdout(capfd, monkeypatch):
    monkeypatch.setattr(releases.sys, "stdin", type("Tty", (), {"isatty": lambda self: True})())
    releases.run_with_host_consent(
        [sys.executable, "-c", "print('Do you want to continue? [Y/n]:')"]
    )
    output = capfd.readouterr()
    assert output.out == ""
    assert "Do you want to continue" in output.err
    with pytest.raises(ReleaseError, match="release_install_command_failed"):
        releases.run_with_host_consent([sys.executable, "-c", "raise SystemExit(3)"])


def test_gemini_release_skips_legacy_cleanup_and_records_client(monkeypatch, tmp_path):
    data = bundle(extension())
    metadata = manifest()
    metadata["archive"].update(size=len(data), sha256=hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(releases, "state_directory", lambda: tmp_path / "releases")
    checked = []
    monkeypatch.setattr(releases.shutil, "which", lambda name: checked.append(name) or name)
    monkeypatch.setattr(
        releases,
        "download",
        lambda url, limit: json.dumps(metadata).encode() if url.endswith("latest.json") else data,
    )
    monkeypatch.setattr(releases, "install_runtime", lambda *args, **kwargs: None)
    monkeypatch.setattr(releases, "preflight_runtime", lambda: None)
    monkeypatch.setattr(releases, "install_host", lambda *args: None)
    monkeypatch.setattr(
        releases, "retire_legacy_host", lambda host: pytest.fail("legacy cleanup for " + host)
    )
    args = Namespace(server="https://durumari.example", client="gemini", check=False)
    assert releases.execute_release(args)["clients"] == ["gemini"]
    assert checked == ["uv", "gemini"]
    assert releases.load_state(tmp_path / "releases")["clients"] == ["gemini"]


def test_gemini_project_binding_preserves_user_settings_and_instructions(tmp_path):
    from test_context_v2_adapter import context_profile

    from durumari_mcp.context_binding import install_binding, remove_binding

    scope = context_profile(tmp_path)
    (tmp_path / ".gemini").mkdir()
    settings = {
        "general": {"vimMode": True},
        "mcpServers": {"other": {"command": "other"}},
        "extensions": {"disabled": ["someone"]},
    }
    (tmp_path / ".gemini/settings.json").write_text(json.dumps(settings))
    (tmp_path / "GEMINI.md").write_text("User Gemini instructions\n")
    for _ in range(2):
        install_binding(scope, "shared", ["gemini"])
        current = json.loads((tmp_path / ".gemini/settings.json").read_text())
        assert current["general"] == settings["general"]
        assert current["extensions"] == settings["extensions"]
        assert current["mcpServers"]["other"] == {"command": "other"}
        entry = current["mcpServers"]["durumari_project"]
        assert "context-serve" in entry["args"]
        assert not entry.get("env")
        assert "User Gemini instructions" in (tmp_path / "GEMINI.md").read_text()
        remove_binding(scope, "shared")
        assert json.loads((tmp_path / ".gemini/settings.json").read_text()) == settings
        assert (tmp_path / "GEMINI.md").read_text().strip() == "User Gemini instructions"
