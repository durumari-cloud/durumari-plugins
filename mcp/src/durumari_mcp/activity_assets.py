"""Bundle the current design's original assets into an offline MCP resource."""

import base64
import json
import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=8)
def render_activity_html(
    *, tool_prefix: str = "durumari_", connection_label: str = "Context 작업 공간"
) -> str:
    if not re.fullmatch(r"durumari_(?:[a-z0-9]+_)*", tool_prefix):
        raise ValueError("invalid UI tool prefix")
    root = Path(__file__).parent
    assets = root / "activity_assets"
    html = (root / "activity.html").read_text(encoding="utf-8")
    html = html.replace(
        "/*__DURUMARI_MATERIALS__*/", (root / "activity_materials.js").read_text(encoding="utf-8")
    )
    html = html.replace(
        "/*__DURUMARI_COLLABORATION__*/",
        (root / "activity_collaboration.js").read_text(encoding="utf-8"),
    )
    html = html.replace(
        "/*__DURUMARI_KNOWLEDGE__*/", (root / "activity_knowledge.js").read_text(encoding="utf-8")
    )
    html = html.replace(
        "/*__DURUMARI_GLOSSARY__*/", (root / "activity_glossary.js").read_text(encoding="utf-8")
    )
    html = html.replace(
        "/*__DURUMARI_DOMAINS__*/", (root / "activity_domains.js").read_text(encoding="utf-8")
    )
    html = html.replace(
        "/*__DURUMARI_EXPLORER__*/", (root / "activity_explorer.js").read_text(encoding="utf-8")
    )
    html = html.replace(
        "/*__DURUMARI_SECRETS__*/", (root / "activity_secrets.js").read_text(encoding="utf-8")
    )
    config = json.dumps(
        {"toolPrefix": tool_prefix, "connectionLabel": connection_label}, ensure_ascii=False
    ).replace("<", "\\u003c")
    html = html.replace(
        '<script id="durumari-ui-config" type="application/json">{}</script>',
        f'<script id="durumari-ui-config" type="application/json">{config}</script>',
    )
    for name in ("doc", "doc-selected", "space", "back"):
        encoded = base64.b64encode((assets / "icons" / f"{name}.svg").read_bytes()).decode("ascii")
        html = html.replace(
            f"__DURUMARI_ICON_{name.upper().replace('-', '_')}__",
            f"data:image/svg+xml;base64,{encoded}",
        )
    css = (assets / "tokens.css").read_text(encoding="utf-8")
    css += (assets / "fonts" / "wanted.css").read_text(encoding="utf-8")
    # Shared widget surfaces (cards, chips, lines, states) that every view builds on.
    css += (assets / "widget.css").read_text(encoding="utf-8")
    for name in ("basic", "ui", "extended"):
        filename = f"wanted-{name}.woff2"
        encoded = base64.b64encode((assets / "fonts" / filename).read_bytes()).decode("ascii")
        css = css.replace(f"./{filename}", f"data:font/woff2;base64,{encoded}")
    return html.replace("<style>", f"<style>\n{css}\n", 1)
