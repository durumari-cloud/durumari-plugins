"""Resolve secret names to values for local use without printing or logging them.

Online, the value-free listing decides: a name must be visible with the "Agent 사용"
(`use_tool`) permission, and a cached value is reused only while its version and
grant revision match, it has not expired, and this target already received it.
Anything else is fetched by a signed one-use challenge and opened from an envelope
sealed to this connection's device key. Offline, unexpired cached values for an
already used target still work. Errors are fixed codes plus the names involved.
"""

import base64
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from durumari_mcp.credentials import AdapterError
from durumari_mcp.secret_cache import SecretCache

DOMAIN = "durumari-agent-secret-v1"
ALGORITHM = "X25519-HKDF-SHA256-AES256GCM-v1"
LABEL = r"[A-Za-z_][A-Za-z0-9_.-]{0,99}"
ID = r"[a-f0-9-]{36}"
LISTING = "/api/v2/agent/secrets"
DEVICES = "/api/v2/agent/secret-devices"
MAX_PAGES = 20
# The server is unreachable or did not answer; cached values may be used.
OFFLINE = {"project_delivery_unknown", "project_unavailable"}
AGENT_FORBIDDEN_TIERS = frozenset({"recovery"})
ENV_ALLOWED_TIERS = frozenset({"development"})
DELIVERY_DENIED_CODES = frozenset(
    {
        "secret_use_denied",
        "target_approval_required",
        "secret_env_tier_forbidden",
        "secret_tier_forbidden",
        "secret_environment_denied",
    }
)


class SecretUseError(AdapterError):
    """A fixed code plus the secret names it concerns; never a value."""

    def __init__(self, code: str, names: list[str] | None = None) -> None:
        super().__init__(code)
        self.names = sorted(set(names or []))


@dataclass(frozen=True)
class Target:
    """A local destination: `run` (the command name) or `env` (the output file)."""

    kind: str
    fingerprint: str
    label: str

    @classmethod
    def of(cls, kind: str, workspace: str, name: str) -> "Target":
        identity = json.dumps(
            {"kind": kind, "name": name, "workspace": workspace},
            sort_keys=True,
            separators=(",", ":"),
        )
        return cls(kind, hashlib.sha256(identity.encode()).hexdigest(), (kind + ": " + name)[:80])

    def request(self) -> dict[str, str]:
        return {"kind": self.kind, "fingerprint": self.fingerprint, "label": self.label}


@dataclass
class Resolved:
    values: dict[str, str] = field(default_factory=dict, repr=False)
    cached: list[str] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    offline: bool = False

    def __repr__(self) -> str:
        return f"Resolved(names={sorted(self.values)}, offline={self.offline})"


def canonical(value: dict[str, Any]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def visible(gateway: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    after = ""
    for _ in range(MAX_PAGES):
        page = gateway.context_request(LISTING + "?limit=50" + ("&after=" + after if after else ""))
        rows = page.get("items")
        if not isinstance(rows, list):
            raise AdapterError("invalid_project_response")
        items.extend(row for row in rows if isinstance(row, dict))
        after = page.get("next_after") or ""
        if not after:
            return items
        if not re.fullmatch(ID, after):
            raise AdapterError("invalid_project_response")
    raise SecretUseError("secret_listing_too_large")


def choose(items: list[dict[str, Any]], name: str) -> dict[str, Any]:
    """Names are unique per owner: one's own secret wins, otherwise exactly one shared."""
    matches = [row for row in items if row.get("label") == name]
    owned = [row for row in matches if row.get("owned") is True]
    if len(owned) == 1:
        return owned[0]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise SecretUseError("secret_not_found", [name])
    raise SecretUseError("secret_name_ambiguous", [name])


def device(gateway: Any, cache: SecretCache, *, refresh: bool = False) -> tuple[str, Any]:
    keys = cache.device_keys(create=True)
    assert keys is not None
    if keys.device_id and not refresh:
        return keys.device_id, keys
    platform = (
        "macos"
        if sys.platform == "darwin"
        else "windows"
        if sys.platform == "win32"
        else "linux"
        if sys.platform.startswith("linux")
        else "other"
    )
    enrolled = gateway.context_request(
        DEVICES, {"platform": platform, **keys.public()}, method="POST"
    )
    device_id = enrolled.get("id")
    if (
        not isinstance(device_id, str)
        or not re.fullmatch(ID, device_id)
        or enrolled.get("connection_id") != cache.connection_id
        or enrolled.get("state") != "active"
    ):
        raise AdapterError("invalid_device_receipt")
    cache.save_keys({**keys.data, "device_id": device_id})
    return device_id, keys


def fetch(
    gateway: Any, cache: SecretCache, refs: list[dict[str, Any]], target: Target
) -> tuple[list[dict[str, Any]], int]:
    device_id, keys = device(gateway, cache)
    path = DEVICES + "/" + quote(device_id, safe="")
    try:
        proof = gateway.context_request(path + "/challenges", {}, method="POST")
    except AdapterError as error:
        if str(error) != "device_access_denied":
            raise
        # The server no longer knows this ID; the same keys re-enroll once (a key
        # change is refused there and needs the connection approved again).
        device_id, keys = device(gateway, cache, refresh=True)
        path = DEVICES + "/" + quote(device_id, safe="")
        proof = gateway.context_request(path + "/challenges", {}, method="POST")
    request = {
        "secrets": [
            {k: ref[k] for k in ("secret_id", "revision", "policy_revision")} for ref in refs
        ],
        "target": target.request(),
    }
    request_bytes = canonical(request)
    try:
        message = json.loads(proof["message"])
        if (
            message["domain"] != DOMAIN
            or message["device_id"] != device_id
            or message["connection_id"] != cache.connection_id
            or message["challenge_id"] != proof["id"]
        ):
            raise ValueError
        signature = keys.signing().sign(proof["message"].encode() + b"\n" + request_bytes)
    except (KeyError, ValueError, TypeError):
        raise AdapterError("invalid_device_challenge") from None
    envelope = gateway.context_request(
        path + "/deliveries",
        {
            "challenge_id": proof["id"],
            "signature": base64.b64encode(signature).decode(),
            "request": request,
        },
        method="POST",
    )
    return open_delivery(envelope, keys, cache, device_id, proof, request_bytes, refs)


def open_delivery(
    envelope: dict[str, Any],
    keys: Any,
    cache: SecretCache,
    device_id: str,
    proof: dict[str, Any],
    request_bytes: bytes,
    refs: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], int]:
    binding = {
        "challenge_id": proof["id"],
        "connection_id": cache.connection_id,
        "device_id": device_id,
        "project_id": cache.project_id,
        "request_sha256": hashlib.sha256(request_bytes).hexdigest(),
        "revision": proof.get("revision"),
    }
    try:
        if envelope["algorithm"] != ALGORITHM or envelope["binding"] != binding:
            raise ValueError
        aad = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        shared = keys.encryption().exchange(
            x25519.X25519PublicKey.from_public_bytes(
                base64.b64decode(envelope["ephemeral_key"], validate=True)
            )
        )
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=DOMAIN.encode() + aad
        ).derive(shared)
        payload = json.loads(
            AESGCM(key).decrypt(
                base64.b64decode(envelope["nonce"], validate=True),
                base64.b64decode(envelope["ciphertext"], validate=True),
                aad,
            )
        )
        entries = payload["secrets"]
        seconds = payload["cache_seconds"]
        if (
            set(payload) != {"secrets", "cache_seconds"}
            or not isinstance(entries, list)
            or not isinstance(seconds, int)
            or isinstance(seconds, bool)
        ):
            raise ValueError
        expected = {ref["secret_id"]: (ref["revision"], ref["policy_revision"]) for ref in refs}
        for entry in entries:
            pinned = expected.pop(entry["secret_id"], None)
            if pinned != (entry["revision"], entry["policy_revision"]) or not isinstance(
                entry["value"], str
            ):
                raise ValueError
        if expected:
            raise ValueError
    except (KeyError, ValueError, TypeError, InvalidTag):
        raise AdapterError("invalid_device_envelope") from None
    return entries, seconds


def _env_forbidden(meta: dict[str, Any]) -> bool:
    if meta.get("env_allowed") is False:
        return True
    tier = meta.get("tier")
    return tier is not None and tier not in ENV_ALLOWED_TIERS


def _check_policy(
    chosen: dict[str, dict[str, Any]], target: Target, cache: SecretCache
) -> None:
    tier_denied = [
        name for name, row in chosen.items() if row.get("tier") in AGENT_FORBIDDEN_TIERS
    ]
    for name in tier_denied:
        cache.forget(chosen[name]["id"])
    if tier_denied:
        raise SecretUseError("secret_tier_forbidden", tier_denied)
    if target.kind == "env":
        env_denied = [name for name, row in chosen.items() if _env_forbidden(row)]
        if env_denied:
            raise SecretUseError("secret_env_tier_forbidden", env_denied)


def resolve(
    gateway: Any, cache: SecretCache, names: list[str], target: Target, *, offline: bool = False
) -> Resolved:
    """Return {name: value} for the requested names, using the cache where valid."""
    for name in names:
        if not re.fullmatch(LABEL, name):
            raise SecretUseError("invalid_secret_name", [name])
    wanted = sorted(set(names))
    result = Resolved()
    items: list[dict[str, Any]] | None = None
    if not offline:
        try:
            items = visible(gateway)
        except AdapterError as error:
            if str(error) not in OFFLINE:
                raise
    if items is None:
        return from_cache(cache, wanted, target, result)
    chosen = {name: choose(items, name) for name in wanted}
    _check_policy(chosen, target, cache)
    denied = [n for n, row in chosen.items() if "use_tool" not in (row.get("permissions") or [])]
    for name in denied:
        cache.forget(chosen[name]["id"])
    if denied:
        raise SecretUseError("secret_use_denied", denied)
    for row in chosen.values():
        if row.get("kind") not in {"text", "token"}:
            raise SecretUseError("secret_kind_not_usable", [row["label"]])
    missing: list[dict[str, Any]] = []
    for name, row in chosen.items():
        entry = cache.get(row["id"])
        if (
            entry is not None
            and (entry["revision"], entry["policy_revision"])
            == (row["revision"], row["policy_revision"])
            and target.fingerprint in entry["targets"]
        ):
            result.values[name] = entry["value"]
            result.cached.append(name)
        else:
            if entry is not None and (entry["revision"], entry["policy_revision"]) != (
                row["revision"],
                row["policy_revision"],
            ):
                cache.forget(row["id"])
            missing.append({"secret_id": row["id"], "name": name, **row})
    if missing:
        try:
            entries, seconds = fetch(gateway, cache, missing, target)
        except AdapterError as error:
            if str(error) in DELIVERY_DENIED_CODES:
                raise SecretUseError(str(error), [ref["name"] for ref in missing]) from None
            raise
        by_id = {ref["secret_id"]: ref["name"] for ref in missing}
        for entry in entries:
            name = by_id[entry["secret_id"]]
            row = chosen[name]
            if "tier" not in entry and isinstance(row.get("tier"), str):
                entry["tier"] = row["tier"]
            if "env_allowed" not in entry and isinstance(row.get("env_allowed"), bool):
                entry["env_allowed"] = row["env_allowed"]
            entry_seconds = entry.get("cache_seconds", seconds)
            if not isinstance(entry_seconds, int) or isinstance(entry_seconds, bool):
                entry_seconds = seconds
            result.values[name] = entry["value"]
            result.fetched.append(name)
            cache.put(entry, target.fingerprint, min(seconds, entry_seconds))
            entry.pop("value", None)
    return result


def from_cache(cache: SecretCache, wanted: list[str], target: Target, result: Resolved) -> Resolved:
    result.offline = True
    index = cache.index()
    by_label: dict[str, list[str]] = {}
    for secret_id, meta in index.items():
        by_label.setdefault(str(meta.get("label")), []).append(secret_id)
    tier_denied = []
    env_denied = []
    unavailable = []
    for name in wanted:
        ids = by_label.get(name, [])
        if len(ids) != 1:
            unavailable.append(name)
            continue
        secret_id = ids[0]
        meta = index[secret_id]
        if meta.get("tier") in AGENT_FORBIDDEN_TIERS:
            cache.forget(secret_id)
            tier_denied.append(name)
            continue
        if target.kind == "env" and _env_forbidden(meta):
            env_denied.append(name)
            continue
        entry = cache.get(secret_id)
        if entry is None or target.fingerprint not in entry["targets"]:
            unavailable.append(name)
            continue
        result.values[name] = entry["value"]
        result.cached.append(name)
    if tier_denied:
        result.values.clear()
        raise SecretUseError("secret_tier_forbidden", tier_denied)
    if env_denied:
        result.values.clear()
        raise SecretUseError("secret_env_tier_forbidden", env_denied)
    if unavailable:
        result.values.clear()
        raise SecretUseError("secret_offline_unavailable", unavailable)
    return result
