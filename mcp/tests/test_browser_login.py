from types import SimpleNamespace

import pytest

from durumari_mcp import browser_login as module
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def flow(monkeypatch):
    calls, opened = [], []
    response = {"status": "pending"}
    request = {
        "device_token": "s" * 43,
        "request_id": "12345678-1234-1234-1234-123456789abc",
        "confirmation_code": "1234ABCD",
        "verification_path": "/connect?client=codex&request=12345678-1234-1234-1234-123456789abc",
    }

    def transport(path, **kwargs):
        calls.append((path, kwargs))
        if path.endswith("/config"):
            return {"browser_origin": "https://durumari.example"}
        if path.endswith("/requests"):
            return request
        if path.endswith("/exchange"):
            return response
        return {}

    monkeypatch.setattr(module.webbrowser, "open", lambda url: opened.append(url))
    return SimpleNamespace(
        request=request,
        response=response,
        calls=calls,
        opened=opened,
        transport=SimpleNamespace(request=transport, origin="https://durumari.example"),
    )


def test_browser_receives_no_secret_and_temporary_session_closes(flow, monkeypatch, capsys):
    def ready(_):
        flow.response.update(status="approved", tokens={"access_token": "a" * 43})

    monkeypatch.setattr(module.time, "sleep", ready)
    with module.browser_login(flow.transport, "codex") as token:
        assert token.get_secret_value() == "a" * 43
    assert flow.calls[-1][0].endswith("/cancel")
    output = capsys.readouterr().out
    assert "1234ABCD" in output
    assert "s" * 43 not in output and "a" * 43 not in output
    assert all("s" * 43 not in url for url in flow.opened)


def test_interruption_cancels_the_request(flow, monkeypatch):
    def interrupt(_):
        raise KeyboardInterrupt

    monkeypatch.setattr(module.time, "sleep", interrupt)
    with pytest.raises(AdapterError, match="project_connection_cancelled"):
        with module.browser_login(flow.transport, "codex"):
            pytest.fail("issued login")
    assert flow.calls[-1][0].endswith("/cancel")


def test_arbitrary_browser_return_path_is_rejected(flow):
    flow.request["verification_path"] = "//external.invalid"
    with pytest.raises(AdapterError, match="invalid_browser_login_request"):
        with module.browser_login(flow.transport, "codex"):
            pytest.fail("issued login")
    assert not flow.opened
