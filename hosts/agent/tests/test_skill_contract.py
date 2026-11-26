"""Goal-named skills: same names for every host, one
reference level, explicit-only auxiliaries, and no team rules in product defaults."""

import re
from pathlib import Path

import pytest
from agent_layout import SKILLS as SKILLS_DIR

SKILLS = sorted(path.parent for path in SKILLS_DIR.glob("*/SKILL.md"))
EXPLICIT_ONLY = {"durumari-migrate", "durumari-share-skill"}


def front_matter(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    block = text.split("---\n", 2)[1]
    return dict(line.split(": ", 1) for line in block.strip().splitlines())


def test_skills_are_discoverable():
    assert SKILLS
    assert EXPLICIT_ONLY <= {skill.name for skill in SKILLS}


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.name)
def test_skill_names_and_invocation_policy(skill):
    front = front_matter(skill / "SKILL.md")
    assert front["name"] == skill.name and skill.name.startswith("durumari-")
    # Claude Code truncates description plus when_to_use at 1,536 characters.
    assert 80 <= len(front["description"]) <= 1536
    lines = (skill / "SKILL.md").read_text(encoding="utf-8").count("\n")
    assert lines <= 300
    codex = (skill / "agents/openai.yaml").read_text(encoding="utf-8")
    implicit = re.search(r"allow_implicit_invocation: (true|false)", codex).group(1)
    assert (implicit == "false") == (skill.name in EXPLICIT_ONLY)
    assert (front.get("disable-model-invocation") == "true") == (skill.name in EXPLICIT_ONLY)


@pytest.mark.parametrize("skill", SKILLS, ids=lambda path: path.name)
def test_references_are_one_level_and_exist(skill):
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    for target in re.findall(r"\]\(((?:references|\.\./)[^)#]*)", text):
        assert (skill / target).resolve().is_file(), target
    for reference in (skill / "references").rglob("*.md") if (skill / "references").exists() else []:
        assert "](references/" not in reference.read_text(encoding="utf-8")


def test_plugin_defaults_hold_no_team_rules_or_values():
    for path in SKILLS_DIR.glob("**/*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        # Team rules (claim, precheck, issue prefixes) are team-registered, not defaults.
        assert not re.search(r"\b[A-Z][A-Z0-9]{5,}-[0-9]+\b|tools\.delivery|precheck", text), path
        assert not re.search(r"(ghp_|github_pat_|sk-[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16})", text)
