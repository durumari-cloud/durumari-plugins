"""Where the sibling plugin folders are, so these tests run in either checkout.

The monorepo keeps them side by side under ``plugins/`` (``durumari-agent`` with its
own ``skills/``). The plugin repository keeps this package in ``mcp/``, the host
manifests in ``hosts/`` and one ``skills/`` folder at the root. Paths that a checkout
does not have (``backend/`` outside the monorepo, the private ACP adapter) do not
exist, and the tests that need them skip.
"""

from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]
MONOREPO = (PACKAGE.parent / "durumari-agent").is_dir()

if MONOREPO:
    REPOSITORY = PACKAGE.parents[1]
    AGENT = PACKAGE.parent / "durumari-agent"
    SKILLS = AGENT / "skills"
    CHAT = PACKAGE.parent / "durumari-chat"
    ACP = PACKAGE.parent / "durumari-acp"
else:
    REPOSITORY = PACKAGE.parent
    AGENT = REPOSITORY / "hosts/agent"
    SKILLS = REPOSITORY / "skills"
    CHAT = REPOSITORY / "hosts/chat"
    ACP = REPOSITORY / "hosts/acp"  # private; absent from the plugin repository

BACKEND_LOCK = REPOSITORY / "backend/uv.lock"
PLUGIN_SCRIPT = AGENT / "scripts/durumari_plugin.py"
