"""Cursor reuses the shared skills and runtime; the manifest adds no fixed project access."""

import json
import re

from agent_layout import CATALOG_SOURCE, MONOREPO, PLUGIN, ROOT
from agent_layout import SKILLS as SKILLS_DIR

SKILLS = {
    "durumari-connect",
    "durumari-context",
    "durumari-secrets",
    "durumari-status",
    "durumari-update",
    "durumari-migrate",
    "durumari-share-skill",
    "durumari-work-logs",
}


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_cursor_manifest_matches_portable_plugin_and_uses_shared_skills():
    cursor = load(PLUGIN / ".cursor-plugin/plugin.json")
    portable = load(PLUGIN / "plugin.json")
    # https://cursor.com/docs/reference/plugins: lowercase kebab-case name.
    assert re.fullmatch(r"[a-z0-9]([a-z0-9.-]*[a-z0-9])?", cursor["name"])
    for field in ("name", "version", "description", "author"):
        assert cursor[field] == portable[field]
    assert cursor["skills"] == "./skills/"
    # Project MCP settings are written only after exact repository approval.
    assert not {"mcpServers", "hooks", "variables", "rules"} & set(cursor)


def test_shared_skills_satisfy_cursor_skill_frontmatter():
    assert {path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md")} == SKILLS
    for name in SKILLS:
        text = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        front = text.split("---", 2)[1]
        assert re.search(rf"^name: {name}$", front, re.MULTILINE)
        assert re.search(r"^description: \S", front, re.MULTILINE)


def test_cursor_marketplace_points_to_the_same_plugin_directory():
    marketplace = load(ROOT / ".cursor-plugin/marketplace.json")
    assert marketplace["owner"]["name"]
    [entry] = marketplace["plugins"]
    assert entry["name"] == "durumari"
    assert entry["source"] == CATALOG_SOURCE
    if MONOREPO:
        assert (ROOT / entry["source"]).resolve() == PLUGIN.resolve()


def test_connect_wrapper_accepts_cursor_client():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "durumari_plugin_cursor", PLUGIN / "scripts/durumari_plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.parser().parse_args(
        ["connect", "--workspace", ".", "--client", "cursor"]
    )
    assert args.client == "cursor"
