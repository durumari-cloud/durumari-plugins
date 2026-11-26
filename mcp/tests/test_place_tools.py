"""Execution request tools: asking only, approval on the web."""

from typing import Any

from mcp.types import CallToolResult, TextContent

from durumari_mcp.place_tools import register_place_tools

ID = "11111111-1111-4111-8111-111111111111"
USER = "22222222-2222-4222-8222-222222222222"


def tools() -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[tuple[str, dict[str, Any]]]]:
    calls: list[tuple[str, dict[str, Any]]] = []
    found: dict[str, Any] = {}
    options: dict[str, dict[str, Any]] = {}

    def places(action: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((action, body))
        return {"ok": True}

    def register(fn: Any, **kwargs: Any) -> None:
        found[fn.__name__], options[fn.__name__] = fn, kwargs

    def safe(call: Any) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=str(call()))])

    register_place_tools(places, register, safe)
    return found, options, calls


def test_asking_sends_only_what_was_given_and_reading_is_marked_read_only():
    found, options, calls = tools()
    assert options["durumari_action_status"]["read"] is True
    assert options["durumari_action_request"]["required_scopes"] == [
        "context:read", "context:write"
    ]
    found["durumari_action_request"](request_key="k-1", action="cloud.delete", target_id=ID)
    found["durumari_action_request"](
        request_key="k-2", action="member.role", target_id=USER,
        params={"cloud_id": ID, "role": "member"},
    )
    found["durumari_action_status"](request_id=ID)
    found["durumari_cloud_permissions"](cloud_id=ID)
    found["durumari_cloud_permissions"](cloud_id=ID, view="effective", user_id=USER)
    assert calls == [
        ("request_action", {"request_key": "k-1", "action": "cloud.delete", "target_id": ID}),
        (
            "request_action",
            {"request_key": "k-2", "action": "member.role", "target_id": USER,
             "params": {"cloud_id": ID, "role": "member"}},
        ),
        ("action_status", {"request_id": ID}),
        ("permissions", {"cloud_id": ID}),
        ("permissions", {"cloud_id": ID, "view": "effective", "user_id": USER}),
    ]


def test_descriptions_stay_short():
    found, _options, _calls = tools()
    for name in ("durumari_action_request", "durumari_action_status",
                 "durumari_cloud_permissions"):
        assert len(found[name].__doc__ or "") < 500, name
