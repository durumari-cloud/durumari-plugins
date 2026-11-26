"""Unit tests for streaming `secret run` stdout/stderr redaction; synthetic values only."""

from __future__ import annotations

import base64
import io
import json
from urllib.parse import quote, quote_plus

import pytest

from durumari_mcp.secret_redact import PLACEHOLDER, StreamRedactor, pump_stream

TOKEN = "synth/token+with space=1&2"
MULTILINE = "first-secret-line-01\nsecond-secret-line-02\nshort"


def redact_all(redactor: StreamRedactor, data: bytes, chunk_size: int) -> bytes:
    out = bytearray()
    for index in range(0, len(data), chunk_size):
        out.extend(redactor.feed(data[index : index + chunk_size]))
    out.extend(redactor.flush())
    return bytes(out)


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 5, 7, 13, 64, 4096])
def test_stream_redactor_masks_raw_and_encoded_variants_across_chunk_boundaries(chunk_size):
    b64 = base64.b64encode(TOKEN.encode("utf-8")).decode("ascii")
    b64_unpadded = b64.rstrip("=")
    b64_url = base64.urlsafe_b64encode(TOKEN.encode("utf-8")).decode("ascii")
    b64_url_unpadded = b64_url.rstrip("=")
    url_upper = quote(TOKEN, safe="")
    url_lower = (
        url_upper.replace("%2F", "%2f")
        .replace("%2B", "%2b")
        .replace("%3D", "%3d")
    )
    url_plus = quote_plus(TOKEN)
    json_body = json.dumps(TOKEN)[1:-1]

    payload = (
        f"raw={TOKEN}\n"
        f"b64={b64}\n"
        f"b64_nopad={b64_unpadded}\n"
        f"b64url={b64_url}\n"
        f"b64url_nopad={b64_url_unpadded}\n"
        f"url={url_upper}\n"
        f"url_low={url_lower}\n"
        f"url_plus={url_plus}\n"
        f"json={json_body}\n"
        "safe=visible-output\n"
    ).encode()

    redactor = StreamRedactor([TOKEN])
    result = redact_all(redactor, payload, chunk_size).decode("utf-8")

    for forbidden in (
        TOKEN,
        b64,
        b64_unpadded,
        b64_url,
        b64_url_unpadded,
        url_upper,
        url_lower,
        url_plus,
        json_body,
    ):
        assert forbidden not in result
    assert "safe=visible-output\n" in result
    assert result.count("[REDACTED]") == 9


def test_stream_redactor_handles_overlapping_and_prefix_collision_patterns():
    # Self-overlapping secret ("abab") at chunk end must still be redacted cleanly.
    r1 = StreamRedactor(["abab"])
    assert r1.feed(b"XXabab") + r1.flush() == b"XX[REDACTED]"
    assert r1.feed(b"ababab") + r1.flush() == b"[REDACTED]ab"

    # Overlapping shorter/longer patterns: wait for longer match when prefix matches.
    r2 = StreamRedactor(["abcd", "abcdef"])
    first = r2.feed(b"1abcd")
    assert first == b"1"
    second = r2.feed(b"ef2")
    assert second + r2.flush() == b"[REDACTED]2"

    # When EOF arrives before the longer match finishes, flush redacts the shorter match.
    r3 = StreamRedactor(["abcd", "abcdef"])
    assert r3.feed(b"1abcd") == b"1"
    assert r3.flush() == b"[REDACTED]"


def test_stream_redactor_flushes_non_matching_output_immediately():
    redactor = StreamRedactor(["synthetic-very-long-secret-value-9999"])
    # Output that does not end with any pattern prefix must stream without buffering.
    assert redactor.feed(b"progress: step 1\n") == b"progress: step 1\n"
    assert redactor.feed(b"progress: step 2\n") == b"progress: step 2\n"
    assert redactor.flush() == b""


def test_stream_redactor_masks_multiline_lines_and_binary_output():
    redactor = StreamRedactor([MULTILINE])
    raw = b"\xff\xfeheader\n" + b"first-secret-line-01\n" + b"body\nsecond-secret-line-02\x00"
    result = redact_all(redactor, raw, 4)
    assert b"first-secret-line-01" not in result
    assert b"second-secret-line-02" not in result
    assert result == b"\xff\xfeheader\n[REDACTED]\nbody\n[REDACTED]\x00"


def test_stream_redactor_ignores_empty_or_too_short_values():
    redactor = StreamRedactor(["", "a", "123"])
    assert not redactor.active
    assert redactor.feed(b"123 a") + redactor.flush() == b"123 a"


def test_pump_stream_copies_and_redacts_in_memory_streams():
    sink = io.TextIOWrapper(io.BytesIO(), encoding="utf-8")
    source = io.BytesIO(f"before {TOKEN} after\n".encode())
    pump_stream(source, sink, StreamRedactor([TOKEN]))
    output = sink.buffer.getvalue()
    assert TOKEN.encode("utf-8") not in output
    assert output == b"before " + PLACEHOLDER + b" after\n"
