"""Read JSON with comments; change owned fields without rewriting other settings."""

import json
from typing import Any, Never


def masked(text: str) -> str:
    chars = list(text)
    index, quoted, escaped = 0, False, False
    while index < len(text):
        char = text[index]
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
            index += 1
        elif char == '"':
            quoted = True
            index += 1
        elif text[index : index + 2] == "//":
            end = text.find("\n", index)
            end = len(text) if end < 0 else end
            chars[index:end] = " " * (end - index)
            index = end
        elif text[index : index + 2] == "/*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ValueError("unterminated comment")
            end += 2
            chars[index:end] = ["\n" if c == "\n" else " " for c in text[index:end]]
            index = end
        else:
            index += 1
    return "".join(chars)


def whitespace(text: str, offset: int) -> int:
    while offset < len(text) and text[offset].isspace():
        offset += 1
    return offset


def clean(text: str) -> str:
    source = masked(text)
    chars = list(source)
    quoted, escaped = False, False
    for index, char in enumerate(chars):
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char == ",":
            after = whitespace(source, index + 1)
            if after < len(chars) and chars[after] in "]}":
                chars[index] = " "
    return "".join(chars)


def pairs(values: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in values:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def invalid_constant(value: str) -> Never:
    raise ValueError("invalid JSON constant")


def load(text: str) -> dict[str, Any]:
    if len(text.encode()) > 2_000_000:
        raise ValueError("configuration too large")
    value = json.loads(clean(text), object_pairs_hook=pairs, parse_constant=invalid_constant)
    if not isinstance(value, dict):
        raise ValueError("object required")
    return value


def render(text: str, desired: dict[str, Any]) -> str:
    text = text or "{}\n"
    previous, source, comments = load(text), clean(text), masked(text)
    decoder = json.JSONDecoder(object_pairs_hook=pairs, parse_constant=invalid_constant)
    edits: list[tuple[int, int, str]] = []

    def visit(start: int, end: int, old: dict[str, Any], new: dict[str, Any]) -> None:
        position, comma = whitespace(source, start + 1), None
        spans: dict[str, tuple[int, int, int, int | None]] = {}
        while source[position] != "}":
            key_start = position
            key, position = decoder.raw_decode(source, position)
            position = whitespace(source, position)
            if source[position] != ":":
                raise ValueError("object required")
            value_start = whitespace(source, position + 1)
            _, value_end = decoder.raw_decode(source, value_start)
            spans[key] = key_start, value_start, value_end, comma
            position = whitespace(source, value_end)
            if source[position] == ",":
                comma, position = position, whitespace(source, position + 1)
        for key, old_value in old.items():
            key_start, value_start, value_end, preceding = spans[key]
            if key not in new:
                following = whitespace(comments, value_end)
                if comments[following] == ",":
                    edits.append((key_start, following + 1, ""))
                else:
                    edits.append((preceding if preceding is not None else key_start, value_end, ""))
            elif old_value != new[key]:
                if isinstance(old_value, dict) and isinstance(new[key], dict):
                    visit(value_start, value_end, old_value, new[key])
                else:
                    edits.append(
                        (value_start, value_end, json.dumps(new[key], ensure_ascii=False, indent=2))
                    )
        added = {key: value for key, value in new.items() if key not in old}
        if added:
            trailing = bool(spans) and "," in comments[list(spans.values())[-1][2] : end - 1]
            content = json.dumps(added, ensure_ascii=False, indent=2)[1:-1]
            retained = any(key in new for key in old)
            edits.append((end - 1, end - 1, ("," if retained and not trailing else "") + content))

    start = whitespace(source, 0)
    _, end = decoder.raw_decode(source, start)
    visit(start, end, previous, desired)
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    if load(text) != desired:
        raise ValueError("configuration edit failed")
    return text
