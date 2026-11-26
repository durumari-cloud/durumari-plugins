"""Human browser approval; temporary credentials never reach browser URLs or output."""

import re
import time
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlsplit

from pydantic import SecretStr

from durumari_mcp.credentials import AdapterError


def browser_origin(value: Any, gateway_origin: str) -> str:
    try:
        if not isinstance(value, str):
            raise ValueError
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path
            or parsed.query
            or parsed.fragment
            or parsed.port == 0
        ):
            raise ValueError
        if parsed.scheme != "https" and not (
            parsed.scheme == "http"
            and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
            and urlsplit(gateway_origin).scheme == "http"
            and urlsplit(gateway_origin).hostname in {"127.0.0.1", "::1"}
        ):
            raise ValueError
        return value
    except (TypeError, ValueError):
        raise AdapterError("invalid_browser_login_origin") from None


@contextmanager
def browser_login(transport: Any, client: str) -> Iterator[SecretStr]:
    origin = browser_origin(
        transport.request("/api/v1/auth/plugin/config").get("browser_origin"), transport.origin
    )
    result = transport.request("/api/v1/auth/plugin/requests", body={"client": client})
    try:
        device = result["device_token"]
        request_id = result["request_id"]
        path = result["verification_path"]
        code = result["confirmation_code"]
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{43}", device)
            or not re.fullmatch(r"[0-9a-f-]{36}", request_id)
            or not re.fullmatch(r"[0-9A-F]{8}", code)
        ):
            raise ValueError
        if path != f"/connect?client={client}&request={request_id}":
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise AdapterError("invalid_browser_login_request") from None
    # The browser sees only a public request id and confirmation code. The
    # 256-bit exchange secret remains in this process and is never persisted.
    try:
        print("브라우저에서 두루마리 로그인 또는 회원가입을 완료해 주세요.")
        print("연결 확인 코드: " + code)
        print("로그인 주소: " + origin + path, flush=True)
        webbrowser.open(origin + path)
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            response = transport.request(
                "/api/v1/auth/plugin/exchange", body={"device_token": device}
            )
            if response.get("status") == "approved":
                tokens = response.get("tokens", {})
                token = tokens.get("access_token")
                if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
                    raise AdapterError("invalid_native_session")
                yield SecretStr(token)
                return
            if response != {"status": "pending"}:
                raise AdapterError("invalid_browser_login_response")
            time.sleep(5)
        raise AdapterError("browser_login_expired")
    except KeyboardInterrupt:
        raise AdapterError("project_connection_cancelled") from None
    finally:
        # Also revokes a session whose exchange response was lost. Cancelling
        # this setup cannot log out the human's browser or remove project grants.
        try:
            transport.request("/api/v1/auth/plugin/cancel", body={"device_token": device})
        except AdapterError:
            raise AdapterError("native_bootstrap_logout_not_confirmed") from None
