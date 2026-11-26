"""JSON results on stdout that every reader decodes the same way.

Agents (Node, Rust) decode a child's pipe as UTF-8, but a redirected Python stdout on
Windows uses the ANSI code page (cp949, cp1252). Readable text goes only to a UTF-8
stream or to a console that can show it; any other stream gets ASCII escapes, which
every JSON reader decodes to the same value.
"""

import codecs
import json
import sys
from typing import Any


def _utf8(encoding: str | None) -> bool:
    try:
        return codecs.lookup(encoding or "utf-8").name == "utf-8"
    except LookupError:
        return False


def emit_json(value: Any) -> None:
    text = json.dumps(value, ensure_ascii=False)
    stream = sys.stdout
    readable = _utf8(stream.encoding)
    if not readable and stream.isatty():
        try:
            text.encode(stream.encoding or "utf-8")
            readable = True
        except (LookupError, UnicodeError):
            pass
    print(text if readable else json.dumps(value))
