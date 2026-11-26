"""Official app hosts that name one Durumari server while it moves.

Bindings, OS credentials and committed durumari.yaml files keep the origin they were
made with; only these fixed official pairs compare equal. Arbitrary hosts never do.
Requests go to the host the server announces as its app origin, so
moving to durumari.cloud and back is a server setting, not a plugin release.
"""

import json
import os
import threading
import time
import urllib.request
from typing import Any

MOVED = {
    "https://durumari.kr": "https://app.durumari.cloud",
    "https://preview.durumari.kr": "https://preview.durumari.cloud",
}
PREVIOUS = {moved: previous for previous, moved in MOVED.items()}
# A process asks once; other processes reuse the answer for this long.
ANNOUNCEMENT_TTL = 600
# Upper bound for the whole lookup (all hosts, DNS included), not per request.
ANNOUNCEMENT_TIMEOUT = 3
# Offline answers are reused this long so short-lived processes do not keep waiting.
ANNOUNCEMENT_FAILURE_TTL = 300
_announced: dict[str, str] = {}


def current(origin: str) -> str:
    return MOVED.get(origin, origin)


def identity(origin: str) -> str:
    """Name the server by its previous host so profile names made before the move hold."""
    return PREVIOUS.get(origin, origin)


def same_server(left: str, right: str) -> bool:
    return identity(left) == identity(right)


def request_origin(origin: str) -> str:
    """The pair host the server announces; any other origin is used as stored."""
    key = identity(origin)
    if key not in MOVED:
        return origin
    if key not in _announced:
        _announced[key] = announced(origin)
    return _announced[key]


def fetch_app_origin(origin: str) -> str | None:
    request = urllib.request.Request(
        origin + "/api/v2/release", headers={"User-Agent": "DURUMARI-Agent/1"}
    )
    with urllib.request.urlopen(request, timeout=ANNOUNCEMENT_TIMEOUT) as response:
        value = json.loads(response.read(32769)[:32768])
    service = value.get("service") if isinstance(value, dict) else None
    app = service.get("app_origin") if isinstance(service, dict) else None
    return app if isinstance(app, str) else None


def _lookup(origin: str) -> str | None:
    """Ask the hosts in a daemon thread so DNS and slow hosts share one deadline."""
    key = identity(origin)
    found: list[str] = []

    def run() -> None:
        # Ask the stored host first, then its pair, so either side being down still answers.
        for host in dict.fromkeys((origin, key, MOVED[key])):
            try:
                app = fetch_app_origin(host)
            except Exception:
                continue
            if app is not None and app in (key, MOVED[key]):
                found.append(app)
                return

    worker = threading.Thread(target=run, name="durumari-host-lookup", daemon=True)
    worker.start()
    worker.join(ANNOUNCEMENT_TIMEOUT)
    return found[0] if found else None


def announced(origin: str) -> str:
    key = identity(origin)
    cache = _read_cache()
    saved = cache.get(key)
    if (
        isinstance(saved, dict)
        and isinstance(saved.get("origin"), str)
        and same_server(saved["origin"], origin)
        and isinstance(saved.get("checked_at"), int | float)
        and 0 <= time.time() - saved["checked_at"] < ANNOUNCEMENT_TTL
    ):
        return str(saved["origin"])
    app = _lookup(origin)
    if app is not None:
        cache[key] = {"origin": app, "checked_at": time.time()}
        _write_cache(cache)
        return app
    fallback = origin
    if isinstance(saved, dict) and isinstance(saved.get("origin"), str):
        if same_server(saved["origin"], origin):
            fallback = str(saved["origin"])
    # Offline: keep the last answer and let other processes skip the lookup for a while.
    cache[key] = {
        "origin": fallback,
        "checked_at": time.time() - ANNOUNCEMENT_TTL + ANNOUNCEMENT_FAILURE_TTL,
    }
    _write_cache(cache)
    return fallback


def _cache_path() -> Any:
    from durumari_mcp.project_outbox import state_root

    return state_root() / "server-hosts.json"


def _read_cache() -> dict[str, Any]:
    try:
        value = json.loads(_cache_path().read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_cache(value: dict[str, Any]) -> None:
    try:
        path = _cache_path()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staged = path.with_name(path.name + "." + str(os.getpid()))
        staged.write_text(json.dumps(value), encoding="utf-8")
        os.replace(staged, path)
    except OSError:
        pass
