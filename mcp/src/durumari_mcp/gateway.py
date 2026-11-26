"""Bounded requests to a bootstrap-pinned Durumari origin; redirects/proxies disabled."""

import json
import ssl
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from durumari_mcp.credentials import AdapterError, Profile

MAX_RESPONSE_BYTES = 262_144


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Gateway:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.opener = build_opener(
            ProxyHandler({}), NoRedirect(), HTTPSHandler(context=ssl.create_default_context())
        )

    def request(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        # Paths come exclusively from this adapter's static operation handlers.
        if not path.startswith("/api/v1/plugin-gateway/") or any(c in path for c in "?#"):
            raise AdapterError("invalid_gateway_operation")
        request = Request(
            self.profile.gateway_origin + path,
            data=json.dumps(body).encode("utf-8") if body is not None else None,
            headers={
                "Authorization": "Plugin " + self.profile.plugin_token.get_secret_value(),
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST" if body is not None else "GET",
        )
        try:
            with self.opener.open(request, timeout=15) as response:
                data = response.read(MAX_RESPONSE_BYTES + 1)
                if len(data) > MAX_RESPONSE_BYTES:
                    raise AdapterError("gateway_response_too_large")
                value = json.loads(data)
                if not isinstance(value, dict):
                    raise AdapterError("invalid_gateway_response")
                return value
        except HTTPError as error:
            # Do not read or reflect backend error bodies, headers, or URLs.
            if error.code in {400, 401, 403, 404, 409, 410, 422, 429}:
                raise AdapterError("gateway_denied") from None
            raise AdapterError("gateway_unavailable") from None
        except (URLError, TimeoutError, OSError):
            # POST delivery may have happened; never retry grants/consume.
            raise AdapterError("gateway_delivery_unknown") from None
        except (UnicodeError, ValueError):
            raise AdapterError("invalid_gateway_response") from None
