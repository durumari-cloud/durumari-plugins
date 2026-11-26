"""Web-only feature tools pass the person's arguments through unchanged."""

from typing import Any

from mcp.types import CallToolResult, TextContent

from durumari_mcp.web_surface_tools import TOOLS, register_web_surface_tools

SPACE = "11111111-1111-4111-8111-111111111111"
DOC = "22222222-2222-4222-8222-222222222222"


def tools() -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[tuple[str, str, Any]]]:
    calls: list[tuple[str, str, Any]] = []
    found: dict[str, Any] = {}
    options: dict[str, dict[str, Any]] = {}

    def web_surfaces(tool: str, action: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((tool, action, body))
        return {"ok": True}

    def register(fn: Any, **kwargs: Any) -> None:
        found[fn.__name__], options[fn.__name__] = fn, kwargs

    def safe(call: Any) -> CallToolResult:
        return CallToolResult(content=[TextContent(type="text", text=str(call()))])

    register_web_surface_tools(web_surfaces, register, safe)
    return found, options, calls


def test_every_tool_registers_with_its_units():
    found, options, _calls = tools()
    assert set(found) == set(TOOLS)
    assert options["durumari_secret_admin"]["read"] is True
    assert options["durumari_secret_admin"]["required_scopes"] == ["secrets:list"]
    assert options["durumari_web_publication"]["required_scopes"] == [
        "context:read",
        "context:write",
    ]


def test_calls_send_only_what_was_given():
    found, _options, calls = tools()
    found["durumari_web_publication"](
        action="publish",
        context_id=DOC,
        target_project_id=SPACE,
        request_key="k-1",
        base_revision=3,
        generation=1,
    )
    found["durumari_secret_admin"](action="change", target_project_id=SPACE, change="policy")
    found["durumari_skill_package"](action="recommendations")
    assert calls == [
        (
            "durumari_web_publication",
            "publish",
            {
                "context_id": DOC,
                "target_project_id": SPACE,
                "request_key": "k-1",
                "base_revision": 3,
                "generation": 1,
            },
        ),
        ("durumari_secret_admin", "change", {"target_project_id": SPACE, "change": "policy"}),
        ("durumari_skill_package", "recommendations", {}),
    ]
