"""Gemini CLI loads the shared plugin folder as an extension without a fixed project server."""

import importlib.util
import json
import re

from agent_layout import PLUGIN
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


def test_extension_manifest_matches_portable_plugin_without_mcp_or_secrets():
    item = json.loads((PLUGIN / "gemini-extension.json").read_text(encoding="utf-8"))
    portable = json.loads((PLUGIN / "plugin.json").read_text(encoding="utf-8"))
    # https://geminicli.com/docs/extensions/reference/: letters, numbers and dashes.
    assert re.fullmatch(r"[a-z0-9-]+", item["name"])
    assert set(item) == {"name", "version", "description"}
    assert (item["name"], item["version"], item["description"]) == (
        portable["name"],
        portable["version"],
        portable["description"],
    )


def test_extension_discovers_the_shared_skills_only():
    assert {path.parent.name for path in SKILLS_DIR.glob("*/SKILL.md")} == SKILLS
    for name in SKILLS:
        front = (SKILLS_DIR / name / "SKILL.md").read_text(encoding="utf-8")
        front = front.split("---", 2)[1]
        assert re.search(rf"^name: {name}$", front, re.MULTILINE)
        assert re.search(r"^description: \S", front, re.MULTILINE)
    # These would add host-wide context, hooks, commands or policies on install.
    for extra in ("GEMINI.md", "hooks", "commands", "policies", "agents", ".env"):
        assert not (PLUGIN / extra).exists()


def test_connect_wrapper_accepts_gemini_client():
    spec = importlib.util.spec_from_file_location(
        "durumari_plugin_gemini", PLUGIN / "scripts/durumari_plugin.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.parser().parse_args(
        ["connect", "--workspace", ".", "--client", "gemini"]
    )
    assert args.client == "gemini"
