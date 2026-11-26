"""Where the shared skills and sibling hosts are, so these tests run in either checkout.

In the monorepo this plugin is ``plugins/durumari-agent`` and carries ``skills/``. In
the plugin repository it is ``hosts/agent``, the skills sit once at the root and the
release build copies them into the catalog's ``plugins/durumari-agent``.
"""

from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[1]
ROOT = PLUGIN.parents[1]
MONOREPO = (PLUGIN / "skills").is_dir()
SKILLS = PLUGIN / "skills" if MONOREPO else ROOT / "skills"
CLAUDE_PUBLIC = (
    ROOT / "plugins/durumari-claude-public" if MONOREPO else ROOT / "hosts/claude-public"
)
SKILL_VARIANTS = (
    ROOT / "plugins/skill-variants/render.py"
    if MONOREPO
    else ROOT / "tools/skill-variants/render.py"
)
# The path every catalog and marketplace names for this plugin.
CATALOG_SOURCE = "plugins/durumari-agent"
