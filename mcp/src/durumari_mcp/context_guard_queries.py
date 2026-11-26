"""Bounded topic extraction for the mandatory Context guard.

Paths in follow-up operational directions must not crowd out the leading
question. This is a search heuristic, never evidence of semantic relevance;
the mandatory reader still refuses no-result and unrelated matches.
"""

import re
import unicodedata

from durumari_mcp.prompt_context import terms


def guard_queries(prompt: str) -> list[str]:
    visible = unicodedata.normalize("NFC", prompt)[:4000]
    visible = re.sub(r"```.*?```", " ", visible, flags=re.S).strip()
    # Do not split on dots: identifiers and paths may themselves be the topic.
    lead = re.split(r"[?？!！。]|\n", visible, maxsplit=1)[0].strip()
    return terms(lead) or terms(visible)
