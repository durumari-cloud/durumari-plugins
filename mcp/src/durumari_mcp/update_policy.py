"""Public native update policy. Standard-library only for the independent launcher."""

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

VERSION = r"[0-9]{1,5}\.[0-9]{1,5}\.[0-9]{1,6}"


class PolicyError(Exception):
    """Value-free launcher recovery codes."""


def version(value: Any) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(VERSION, value):
        raise PolicyError("native_runtime_version_unknown")
    major, minor, patch = value.split(".")
    return int(major), int(minor), int(patch)


def trusted_origin(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
            or value != value.strip()
            or any(c.isspace() or ord(c) < 32 for c in value)
            or (parsed.port is not None and not 1 <= parsed.port <= 65535)
            or not (
                parsed.scheme == "https"
                or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"})
            )
        ):
            raise ValueError
        return value.rstrip("/")
    except (ValueError, TypeError):
        raise PolicyError("native_update_origin_invalid") from None


@dataclass(frozen=True)
class NativePolicy:
    # None when the server publishes no native release; nothing is then required.
    latest_version: str | None
    supported_min_version: str | None
    required_native_version: str | None
    update_url: str
    capability: str = "native_policy_v1"

    def needs_update(self, installed: str) -> bool:
        current = version(installed)
        return self.required_native_version is not None and current < version(
            self.required_native_version
        )

    def receipt(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "latest_version": self.latest_version,
            "supported_min_version": self.supported_min_version,
            "required_native_version": self.required_native_version,
            "update_url": self.update_url,
        }


def parse_policy(value: Any, origin: str) -> NativePolicy:
    origin = trusted_origin(origin)
    try:
        if not isinstance(value, dict):
            raise ValueError
        if "native" not in value:
            client = value.get("client")
            if (
                value.get("context_api") != 2
                or not isinstance(client, dict)
                or client.get("status") not in {"current", "update_available", "update_required"}
            ):
                raise ValueError
            latest, minimum = client["latest_version"], client["minimum_version"]
            if version(minimum) > version(latest):
                raise ValueError
            return NativePolicy(latest, minimum, None, origin + "/downloads", "legacy_advisory")
        native = value["native"]
        if (
            not isinstance(native, dict)
            or not {
                "latest_version",
                "supported_min_version",
                "required_native_version",
                "update_url",
            }
            <= native.keys()
        ):
            raise ValueError
        latest, minimum = native["latest_version"], native["supported_min_version"]
        required = native["required_native_version"]
        required = None if required in {None, ""} else required
        if latest is None or minimum is None:
            # No published release: run the installed runtime without an update.
            # A required version without a release to install is never valid.
            if required is not None or (latest, minimum) != (None, None):
                raise ValueError
        elif version(minimum) > version(latest) or (
            required is not None and version(required) > version(latest)
        ):
            raise ValueError
        if native["update_url"] not in {"/downloads", origin + "/downloads"}:
            raise ValueError
        return NativePolicy(latest, minimum, required, origin + "/downloads")
    except (ValueError, TypeError, KeyError, PolicyError):
        raise PolicyError("native_update_policy_invalid") from None


def fetch_policy(origin: str, download: Any) -> NativePolicy:
    origin = trusted_origin(origin)
    try:
        value = json.loads(download(origin + "/api/v2/release", 32768))
    except Exception:
        raise PolicyError("native_update_policy_unavailable") from None
    return parse_policy(value, origin)
