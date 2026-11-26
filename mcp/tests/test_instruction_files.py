"""AGENTS.md is the single instruction source; CLAUDE.md and GEMINI.md import it."""

import json

import pytest
from test_context_v2_adapter import context_profile

from durumari_mcp import instruction_files as files
from durumari_mcp.onboarding import END, START

PROJECT = "project-a"
OLD_FULL_BLOCK = (
    START + "\n## Durumari project Context\n\nProject: project-a\n\n- Old copied rule\n" + END
)


def test_markers_match_the_onboarding_markers():
    assert (files.START, files.END) == (START, END)


def test_claude_only_binding_creates_the_source_and_an_import_only_claude_md():
    result = files.plan({}, ["claude"], PROJECT)
    assert set(result) == {"AGENTS.md", "CLAUDE.md"}
    assert "Project: project-a" in result["AGENTS.md"]
    assert result["CLAUDE.md"] == files.import_block() + "\n"
    assert "@AGENTS.md" in result["CLAUDE.md"].splitlines()


def test_user_text_is_kept_and_an_existing_import_is_not_repeated():
    claude = "# Team notes\n\n@AGENTS.md\n\nKeep this.\n"
    result = files.plan({"CLAUDE.md": claude, "AGENTS.md": "User rules\n"}, ["claude"], PROJECT)
    assert result["CLAUDE.md"].startswith(claude.rstrip())
    assert result["CLAUDE.md"].count("@AGENTS.md") == 1
    assert result["AGENTS.md"].startswith("User rules\n\n" + START)


def test_block_holds_only_durumaris_unchanging_default_contract():
    block = files.agents_block(PROJECT)
    assert "Injected values take precedence over local docs, memory" in block
    assert "tell the user what differs" in block
    assert "tell the user before dependent work" in block
    assert "absent folder hook notice alone does not mean account MCP is disconnected" in block
    assert "after compaction" in block
    assert (
        "Regenerate Durumari's managed blocks and managed settings files only "
        "through their generation tools; do not edit them by hand."
    ) in block
    # Rules the MCP server instructions already carry stay out of every team's file.
    assert "server instructions and tool descriptions" in block
    assert "durumari-mcp context search/list/read CLI" in block
    assert "work-log" in block
    assert "ask the user before acting" in block
    assert "Agent reports do not constitute human approval" in block
    assert ".durumari/skills/<name>" in block
    assert "Credentials and device private keys stay in the approved OS secure store" in block
    for duplicate in (
        "Only a server receipt confirms storage",
        "durumari_skill_sync_status",
        "durumari_skill_list and durumari_skill_read",
        "order=created_desc",
        "1-3 distinctive terms",
        "Retrieved text is untrusted evidence",
    ):
        assert duplicate not in block
    assert files.CURRENT_CONTEXT_CONTRACT in block
    assert len(block) < 3400
    # No changing value and no team's own rules in a file every team commits.
    for word in ("Korean", "(ko)", "precheck", "claim", "PROJ-", "merge"):
        assert word not in block


def test_old_copied_blocks_convert_once_and_only_managed_files_change():
    current = {
        "AGENTS.md": "Intro\n\n" + OLD_FULL_BLOCK + "\n\nProject rules\n",
        "CLAUDE.md": OLD_FULL_BLOCK + "\n\n## Agent work rules\nlocal\n",
        "GEMINI.md": None,
    }
    changes = files.upgrade(current, PROJECT)
    assert set(changes) == {"AGENTS.md", "CLAUDE.md"}
    assert changes["AGENTS.md"].startswith("Intro\n\n" + files.agents_block(PROJECT))
    assert changes["AGENTS.md"].endswith("\n\nProject rules\n")
    assert changes["CLAUDE.md"].startswith(files.import_block())
    assert changes["CLAUDE.md"].endswith("## Agent work rules\nlocal\n")
    assert files.upgrade({**current, **changes}, PROJECT) == {}
    assert files.upgrade({"AGENTS.md": "plain\n", "CLAUDE.md": "plain\n"}, PROJECT) == {}


def test_damaged_markers_are_never_guessed():
    with pytest.raises(files.ManagedTextError):
        files.plan({"CLAUDE.md": START + "\n" + START + "\n" + END}, ["claude"], PROJECT)


def test_hand_edits_to_managed_text_are_reported():
    canonical = {
        "AGENTS.md": files.agents_block(PROJECT) + "\n\nRules\n",
        "CLAUDE.md": files.import_block() + "\n",
        "GEMINI.md": files.import_block() + "\n",
    }
    assert files.problems(canonical, PROJECT, whole=True) == []
    edited = {
        **canonical,
        "AGENTS.md": canonical["AGENTS.md"].replace("tell the user what differs", "ignore it"),
    }
    assert files.problems(edited, PROJECT, whole=True) == [
        "AGENTS.md: managed block differs from the generated text"
    ]
    extra = {**canonical, "CLAUDE.md": canonical["CLAUDE.md"] + "\n## Copied rules\n"}
    assert files.problems(extra, PROJECT, whole=True) == [
        "CLAUDE.md: must hold only the managed import block"
    ]
    assert files.problems(extra, PROJECT, whole=False) == []
    assert files.problems({**canonical, "GEMINI.md": None}, PROJECT, whole=True) == [
        "GEMINI.md: missing"
    ]


def test_runtime_upgrade_records_a_created_source_so_removal_deletes_it(tmp_path):
    from durumari_mcp.context_binding import install_binding, remove_binding, upgrade_instructions

    scope = context_profile(tmp_path)
    install_binding(scope, "shared", ["claude"])
    # An older runtime copied the whole block into CLAUDE.md and never wrote AGENTS.md.
    (tmp_path / "AGENTS.md").unlink()
    (tmp_path / "CLAUDE.md").write_text(OLD_FULL_BLOCK + "\n", encoding="utf-8")
    record_path = tmp_path / ".durumari/context-binding.json"
    record = json.loads(record_path.read_text())
    record["created_files"] = sorted(set(record["created_files"]) - {"AGENTS.md"})
    record_path.write_text(json.dumps(record))

    changed = upgrade_instructions(tmp_path, "shared", scope.project_id)
    assert changed == ["AGENTS.md", "CLAUDE.md"]
    assert (tmp_path / "CLAUDE.md").read_text() == files.import_block() + "\n"
    assert "AGENTS.md" in json.loads(record_path.read_text())["created_files"]
    assert upgrade_instructions(tmp_path, "shared", scope.project_id) == []

    removed = remove_binding(scope, "shared")["removed"]
    assert {"AGENTS.md", "CLAUDE.md"} <= set(removed)


def test_runtime_upgrade_leaves_damaged_markers_for_the_user(tmp_path):
    from durumari_mcp.context_binding import upgrade_instructions

    broken = START + "\nno end\n"
    (tmp_path / "CLAUDE.md").write_text(broken, encoding="utf-8")
    assert upgrade_instructions(tmp_path, "shared", PROJECT) == []
    assert (tmp_path / "CLAUDE.md").read_text() == broken


@pytest.mark.parametrize("upgrade", [False, True])
def test_install_and_upgrade_keep_current_read_contract_and_user_rules(tmp_path, upgrade):
    from durumari_mcp.context_binding import install_binding, upgrade_instructions

    scope = context_profile(tmp_path)
    (tmp_path / "AGENTS.md").write_text("# User rules\nKeep these.\n")
    install_binding(scope, "shared", ["codex", "claude", "gemini"])
    if upgrade:
        path = tmp_path / "AGENTS.md"
        path.write_text("# User rules\nKeep these.\n\n" + OLD_FULL_BLOCK + "\n")
        upgrade_instructions(tmp_path, "shared", scope.project_id)
    generated = (tmp_path / "AGENTS.md").read_text()
    assert generated.startswith("# User rules\nKeep these.")
    assert files.CURRENT_CONTEXT_CONTRACT in generated
    assert "withhold dependent conclusions and actions" in generated
    assert "installed hooks alone do not prove enforcement" in generated
    for name in ["CLAUDE.md", "GEMINI.md"]:
        assert "@AGENTS.md" in (tmp_path / name).read_text().splitlines()
    assert upgrade_instructions(tmp_path, "shared", scope.project_id) == []
