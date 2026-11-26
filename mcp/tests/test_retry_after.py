"""A refused request waits out Retry-After with backoff and loses nothing."""

import io
import json
from email.message import Message
from urllib.error import HTTPError

import pytest

from durumari_mcp.project_gateway import MAX_RETRY_WAIT, ContextFailure, ProjectTransport


def refusal(status, code, after="2", unprocessed=True, **extra):
    headers = Message()
    if after is not None:
        headers["Retry-After"] = after
    details = ({"unprocessed": True} if unprocessed else {}) | extra
    body = json.dumps({"error": {"code": code, "message": "busy", "details": details}}).encode()
    return lambda request: HTTPError(request.full_url, status, "busy", headers, io.BytesIO(body))


class Response(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Scripted:
    def __init__(self, *steps):
        self.steps, self.calls = list(steps), []

    def open(self, request, timeout):
        self.calls.append(request.get_method())
        step = self.steps.pop(0)
        if callable(step):
            raise step(request)
        return Response(json.dumps(step).encode())


def transport(*steps):
    value = ProjectTransport("https://durumari.example")
    value.opener = Scripted(*steps)
    value.waits = []
    value.sleep = value.waits.append
    value.jitter = lambda: 1.0
    return value


def test_unprocessed_write_is_resent_after_retry_after_and_succeeds():
    value = transport(refusal(503, "request_busy", "3"), refusal(429, "account_busy"), {"ok": 1})
    assert value.request("/api/v2/agent/context/save", body={"text": "kept"}) == {"ok": 1}
    assert value.opener.calls == ["POST", "POST", "POST"]
    # Retry-After first; exponential backoff when it is shorter than the backoff step.
    assert value.waits == [3.0, 2.0]


def test_backoff_grows_and_jitter_spreads_clients():
    value = transport(*[refusal(503, "request_busy", "0")] * 3, {"ok": 1})
    value.request("/api/v2/agent/context/search", body={})
    assert value.waits == [0.5, 1.0, 2.0]
    spread = transport(refusal(503, "request_busy", "1"), {"ok": 1})
    spread.jitter = lambda: 1.3
    spread.request("/api/v2/agent/context/search", body={})
    assert spread.waits == [1.3]


def test_exhausted_retries_report_the_wait_instead_of_a_bare_failure():
    value = transport(*[refusal(429, "account_busy", "5")] * 4)
    with pytest.raises(ContextFailure) as failure:
        value.request("/api/v2/agent/context/save", body={})
    assert str(failure.value) == "project_rate_limited"
    assert failure.value.error["details"] == {"retry_after_seconds": 5, "reason": "account_busy"}
    assert sum(value.waits) <= MAX_RETRY_WAIT


def test_a_write_the_application_refused_is_never_resent():
    # An application 429 without an admission code may have applied the write.
    value = transport(refusal(429, "quota_exceeded", unprocessed=False), {"ok": 1})
    with pytest.raises(Exception, match="project_rate_limited"):
        value.request("/api/v2/agent/context/save", body={})
    assert value.opener.calls == ["POST"] and value.waits == []


def test_a_handler_request_busy_without_the_marker_resends_reads_only():
    # The app's own request_busy can follow a commit: a write is never resent on it.
    write = transport(refusal(503, "request_busy", "1", unprocessed=False), {"ok": 1})
    with pytest.raises(Exception, match="project_unavailable"):
        write.request("/api/v2/agent/context/save", body={})
    assert write.opener.calls == ["POST"]
    read = transport(refusal(503, "request_busy", "1", unprocessed=False), {"ok": 1})
    assert read.request("/api/v2/agent/context/items", method="GET") == {"ok": 1}


def test_no_retry_after_means_no_automatic_retry():
    value = transport(refusal(503, "request_busy", None), {"ok": 1})
    with pytest.raises(ContextFailure):
        value.request("/api/v2/agent/context/search", body={})
    assert value.waits == []


def test_vapor_empty_is_waited_out_for_a_write_and_resent():
    value = transport(
        refusal(429, "vapor_empty", "4", unprocessed=False, retry_after_seconds=4), {"ok": 1}
    )
    assert value.request("/api/v2/agent/context/save", body={"text": "kept"}) == {"ok": 1}
    assert value.opener.calls == ["POST", "POST"] and value.waits == [4.0]


def test_vapor_empty_without_a_header_uses_the_body_wait():
    value = transport(
        refusal(429, "vapor_empty", None, unprocessed=False, retry_after_seconds=3), {"ok": 1}
    )
    assert value.request("/api/v2/agent/context/save", body={}) == {"ok": 1}
    assert value.waits == [3.0]


def test_a_long_vapor_wait_is_not_slept_and_names_its_reason():
    value = transport(
        refusal(429, "vapor_empty", "3600", unprocessed=False, retry_after_seconds=3600),
        {"ok": 1},
    )
    with pytest.raises(ContextFailure) as failure:
        value.request("/api/v2/agent/context/save", body={})
    assert str(failure.value) == "project_rate_limited"
    assert failure.value.error["details"] == {
        "retry_after_seconds": 3600,
        "reason": "vapor_empty",
        "next_action": "wait_or_upgrade",
    }
    assert value.waits == [] and value.opener.calls == ["POST"]


def test_exhausted_vapor_retries_keep_the_total_wait_bounded():
    value = transport(*[refusal(429, "vapor_empty", "8", unprocessed=False)] * 4)
    with pytest.raises(ContextFailure) as failure:
        value.request("/api/v2/agent/context/save", body={})
    assert failure.value.error["details"]["reason"] == "vapor_empty"
    assert 0 < sum(value.waits) <= MAX_RETRY_WAIT
