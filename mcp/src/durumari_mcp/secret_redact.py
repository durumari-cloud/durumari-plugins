"""Real-time byte-stream redaction for secrets injected into `secret run` children.

Masks each injected secret value and its common encodings (standard/URL-safe base64
with or without padding, URL percent-encoding in upper/lower hex, JSON string escapes,
and non-trivial lines of multiline values) across arbitrary pipe chunk boundaries.
"""

from __future__ import annotations

import base64
import json
import os
import re
from collections.abc import Iterable
from typing import IO, Any
from urllib.parse import quote, quote_plus

PLACEHOLDER = b"[REDACTED]"
MIN_REDACT_LEN = 4
MIN_LINE_REDACT_LEN = 8
READ_SIZE = 8192
_PERCENT_HEX = re.compile(r"%[0-9A-F]{2}")


def _lowercase_percent(encoded: str) -> str:
    return _PERCENT_HEX.sub(lambda match: match.group(0).lower(), encoded)


def _add_encodings(value: str, out: set[bytes]) -> None:
    raw = value.encode("utf-8")
    out.add(raw)
    for encoder in (base64.b64encode, base64.urlsafe_b64encode):
        encoded = encoder(raw)
        if len(encoded) >= MIN_REDACT_LEN:
            out.add(encoded)
            unpadded = encoded.rstrip(b"=")
            if len(unpadded) >= MIN_REDACT_LEN:
                out.add(unpadded)
    for url_encoded in (quote(value, safe=""), quote_plus(value)):
        if len(url_encoded) >= MIN_REDACT_LEN:
            out.add(url_encoded.encode("utf-8"))
            lowered = _lowercase_percent(url_encoded)
            if lowered != url_encoded:
                out.add(lowered.encode("utf-8"))
    json_escaped = json.dumps(value, ensure_ascii=False)[1:-1]
    if len(json_escaped) >= MIN_REDACT_LEN:
        out.add(json_escaped.encode("utf-8"))
    json_ascii = json.dumps(value, ensure_ascii=True)[1:-1]
    if json_ascii != json_escaped and len(json_ascii) >= MIN_REDACT_LEN:
        out.add(json_ascii.encode("utf-8"))


def variants(values: Iterable[str]) -> tuple[bytes, ...]:
    """Return deduplicated byte patterns for `values`, sorted longest-first."""
    patterns: set[bytes] = set()
    for value in values:
        if not isinstance(value, str) or len(value) < MIN_REDACT_LEN:
            continue
        _add_encodings(value, patterns)
        stripped = value.strip()
        if stripped != value and len(stripped) >= MIN_REDACT_LEN:
            _add_encodings(stripped, patterns)
        if "\n" in value or "\r" in value:
            for line in value.splitlines():
                for candidate in (line, line.strip()):
                    if len(candidate) >= MIN_LINE_REDACT_LEN:
                        patterns.add(candidate.encode("utf-8"))
                        url_line = quote(candidate, safe="")
                        if url_line != candidate:
                            patterns.add(url_line.encode("utf-8"))
                            lowered = _lowercase_percent(url_line)
                            if lowered != url_line:
                                patterns.add(lowered.encode("utf-8"))
    return tuple(sorted(patterns, key=len, reverse=True))


class StreamRedactor:
    """Chunk-boundary-safe streaming byte redactor with minimal prefix buffering."""

    def __init__(self, values: Iterable[str], *, placeholder: bytes = PLACEHOLDER) -> None:
        self._placeholder = placeholder
        self._patterns = variants(values)
        self._pending = b""
        if not self._patterns:
            self._max_len = 0
            self._pattern_re: re.Pattern[bytes] | None = None
            self._first1: set[int] = set()
            self._by_first2: dict[bytes, tuple[bytes, ...]] = {}
            self._start2_re: re.Pattern[bytes] | None = None
            return
        self._max_len = len(self._patterns[0])
        self._pattern_re = re.compile(b"|".join(re.escape(p) for p in self._patterns))
        self._first1 = {p[0] for p in self._patterns if len(p) >= 2}
        grouped: dict[bytes, list[bytes]] = {}
        for pattern in self._patterns:
            if len(pattern) >= 3:
                grouped.setdefault(pattern[:2], []).append(pattern)
        self._by_first2 = {key: tuple(items) for key, items in grouped.items()}
        self._start2_re = (
            re.compile(b"(?=(" + b"|".join(re.escape(k) for k in self._by_first2) + b"))")
            if self._by_first2
            else None
        )

    @property
    def active(self) -> bool:
        return self._pattern_re is not None

    def _hold_start(self, buf: bytes) -> int:
        """Earliest index `i` where `buf[i:]` is a proper prefix of any pattern."""
        length = len(buf)
        if length == 0 or self._max_len <= 1:
            return length
        tail_start = max(0, length - (self._max_len - 1))
        if self._start2_re is not None and length - tail_start >= 2:
            for match in self._start2_re.finditer(buf, tail_start):
                idx = match.start()
                rem = length - idx
                prefix = buf[idx:]
                for pattern in self._by_first2[match.group(1)]:
                    if len(pattern) > rem and pattern.startswith(prefix):
                        return idx
        if buf[-1] in self._first1:
            return length - 1
        return length

    def feed(self, chunk: bytes) -> bytes:
        if not chunk:
            return b""
        if self._pattern_re is None:
            return chunk
        buf = self._pending + chunk
        out = bytearray()
        while buf:
            hold = self._hold_start(buf)
            pos = 0
            restarted = False
            for match in self._pattern_re.finditer(buf):
                start, end = match.span()
                if start >= hold:
                    break
                out.extend(buf[pos:start])
                out.extend(self._placeholder)
                if end > hold:
                    buf = buf[end:]
                    restarted = True
                    break
                pos = end
            if restarted:
                continue
            out.extend(buf[pos:hold])
            self._pending = buf[hold:]
            return bytes(out)
        self._pending = b""
        return bytes(out)

    def flush(self) -> bytes:
        if not self._pending:
            return b""
        tail = self._pending
        self._pending = b""
        if self._pattern_re is None:
            return tail
        return self._pattern_re.sub(self._placeholder, tail)

    def clear(self) -> None:
        self._pending = b""


def _write_sink(sink: Any, data: bytes) -> bool:
    if not data:
        return True
    try:
        buffer = getattr(sink, "buffer", None)
        if buffer is not None:
            buffer.write(data)
            buffer.flush()
        else:
            sink.write(data.decode("utf-8", errors="replace"))
            sink.flush()
        return True
    except (BrokenPipeError, OSError, ValueError):
        return False


def _read_chunk(source: IO[bytes]) -> bytes:
    try:
        fd = source.fileno()
    except (AttributeError, OSError, ValueError):
        return source.read(READ_SIZE)
    return os.read(fd, READ_SIZE)


def pump_stream(source: IO[bytes], sink: Any, redactor: StreamRedactor) -> None:
    """Read raw chunks from `source`, redact them, and write immediately to `sink`."""
    sink_open = True
    try:
        while True:
            try:
                chunk = _read_chunk(source)
            except OSError:
                break
            if not chunk:
                break
            redacted = redactor.feed(chunk)
            if sink_open and redacted:
                sink_open = _write_sink(sink, redacted)
        tail = redactor.flush()
        if sink_open and tail:
            _write_sink(sink, tail)
    finally:
        redactor.clear()
        try:
            source.close()
        except OSError:
            pass
