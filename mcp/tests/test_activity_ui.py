"""Result cards describe server results without upgrading evidence or leaking bodies."""

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from mcp import Client
from mcp.types import CallToolResult, ResourceLink, TextContent
from plugin_layout import AGENT
from test_cloud_protocol import SyntheticOperations

from durumari_mcp.activity_ui import activity_result
from durumari_mcp.cloud_protocol import create_context_server


@pytest.mark.parametrize("text", ["not-json", "[]", "null", '{"status":"stored"}'])
def test_presentation_failure_never_replaces_the_original_committed_receipt(text, monkeypatch):
    from durumari_mcp import activity_ui

    receipt = CallToolResult(content=[TextContent(type="text", text=text)])
    writes = []

    def committed(**arguments):
        writes.append(arguments)
        return receipt

    def broken_projection(*_arguments):
        raise RuntimeError("presentation unavailable")

    monkeypatch.setattr(activity_ui, "activity_result", broken_projection)
    tool = SimpleNamespace(name="durumari_context_save", fn=committed, meta=None)
    activity_ui.decorate_tool(tool)
    result = tool.fn(request_key="one-commit")
    assert result is receipt and len(writes) == 1
    assert result.content[0].text == text and not result.is_error


@pytest.mark.anyio
async def test_real_sdk_registers_navigation_and_serves_self_contained_ui():
    async with Client(create_context_server(SyntheticOperations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        entry = tools["durumari_open_context_workspace"]
        assert entry.title == "Open the Context workspace"
        for name in ("durumari_context_read", "durumari_context_save", "durumari_context_revise"):
            assert tools[name].meta["ui"]["resourceUri"] == entry.meta["ui"]["resourceUri"]
        assert entry.meta["openai/ui"]["entrypoints"] == [
            {"type": "global"},
            {"type": "thread"},
        ]
        assert not entry.input_schema.get("required")
        opened = await client.call_tool("durumari_open_context_workspace", {})
        assert opened.structured_content["activity"]["outcome"] == "ready"
        resource = await client.read_resource(entry.meta["ui"]["resourceUri"])
        assert resource.contents[0].meta["openai/ui"] == {
            "preferredDisplayMode": "inline",
            "availableDisplayModes": ["inline", "fullscreen"],
        }
        # The shared view names itself, so the host never borrows one tool's description.
        description = resource.contents[0].meta["openai/widgetDescription"]
        assert "secret value" in description and "glossary" in description
        html = resource.contents[0].text
        assert "ui/initialize" in html
        assert "<script src=" not in html
        assert "__DURUMARI_ICON_" not in html
        assert "data:font/woff2;base64," in html
        assert "data:image/svg+xml;base64," in html
        assert "url('./wanted-" not in html


def test_widget_bundles_original_figma_assets_and_generated_font_subsets():
    import base64
    from pathlib import Path

    from durumari_mcp.activity_assets import render_activity_html

    root = Path(__file__).parents[1] / "src" / "durumari_mcp" / "activity_assets"
    html = render_activity_html()
    for name in ("doc", "doc-selected", "space", "back"):
        original = (root / "icons" / f"{name}.svg").read_bytes()
        assert f"data:image/svg+xml;base64,{base64.b64encode(original).decode('ascii')}" in html
    for name in ("basic", "ui", "extended"):
        original = (root / "fonts" / f"wanted-{name}.woff2").read_bytes()
        assert f"data:font/woff2;base64,{base64.b64encode(original).decode('ascii')}" in html


def test_widget_takes_colours_from_generated_product_tokens_and_follows_host_theme():
    import re
    from pathlib import Path

    from durumari_mcp.activity_assets import render_activity_html

    source = Path(__file__).parents[1] / "src" / "durumari_mcp"
    for path in [source / "activity.html", *source.glob("activity_*.js")]:
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(|:\s*(?:white|black)\b", text), (
            f"hard-coded colour in {path.name}"
        )
    html = render_activity_html()
    tokens = (source / "activity_assets" / "tokens.css").read_text(encoding="utf-8")
    assert tokens in html
    assert "--radius-md: 10px;" in tokens and "--space-4: 16px;" in tokens
    assert ':root[data-theme="dark"]' in tokens and "prefers-color-scheme: dark" in tokens
    assert "host-context-changed" in html and "dataset.theme" in html


def test_widget_and_plugin_text_say_space_never_project():
    """people read 공간, never 프로젝트. Identifiers such as project_ref
    and tool names stay for compatibility and are not Korean text."""
    from durumari_mcp.activity_assets import render_activity_html

    source = Path(__file__).parents[1] / "src" / "durumari_mcp"
    agent = AGENT
    paths = [
        source / "activity.html",
        *source.glob("activity_*.js"),
        agent / "plugin.json",
        agent / ".codex-plugin" / "plugin.json",
    ]
    found = [
        f"{path.name}:{number}"
        for path in paths
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "프로젝트" in line
    ]
    assert found == []
    html = render_activity_html()
    assert "프로젝트" not in html and "공간 선택" in html and "공간 찾기" in html


def test_ui_configuration_uses_current_namespace_without_patching_javascript():
    from durumari_mcp.activity_assets import render_activity_html

    html = render_activity_html(
        tool_prefix="durumari_workspace_live_", connection_label="내 계정 </script><script>"
    )
    config = html.split('<script id="durumari-ui-config" type="application/json">')[1]
    value = json.loads(config.split("</script>", 1)[0])
    assert value["toolPrefix"] == "durumari_workspace_live_"
    assert value["connectionLabel"] == "내 계정 </script><script>"
    assert "local_origin" not in html
    with pytest.raises(ValueError, match="invalid UI tool prefix"):
        render_activity_html(tool_prefix="unrelated_service_")


@pytest.mark.anyio
async def test_card_does_not_change_the_original_text_receipt_or_write_twice():
    operations = SyntheticOperations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(
            "durumari_context_save",
            {
                "request_key": "one-write",
                "title": "Result",
                "body": "Only original text",
            },
        )
        assert len(operations.writes) == 1
        assert result.meta["activity"]["request_key"] == "one-write"
        assert "Only original text" not in str(result.meta)


@pytest.mark.anyio
async def test_model_keeps_original_read_once_and_view_metadata_has_no_body():
    class WithDocument(SyntheticOperations):
        def read(self, suffix="", **query):
            return {
                "id": "document",
                "project_id": "project",
                "revision": 1,
                "sections": [{"id": "section", "text": "The original Context body"}],
            }

    async with Client(create_context_server(WithDocument())) as client:
        result = await client.call_tool("durumari_context_read", {"context_id": "document"})
        original = json.loads(result.content[0].text)
        assert result.structured_content is None
        assert original["sections"][0]["text"] == "The original Context body"
        assert "sections" not in result.meta["activity"]
        assert result.model_dump_json().count("The original Context body") == 1


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"project_ref": "ref"}, {"project_ref": "ref"}),
        ({"target_project_id": "project"}, {"target_project_id": "project"}),
        ({}, {}),
    ],
)
def test_read_card_keeps_the_original_target_for_continuation(arguments, expected):
    body = '{"id":"document","project_id":"project","revision":1,"sections":[]}'
    tool = SimpleNamespace(
        name="durumari_context_read",
        fn=lambda **_: CallToolResult(content=[TextContent(type="text", text=body)]),
        meta=None,
    )
    from durumari_mcp import activity_ui

    activity_ui.decorate_tool(tool)
    card = tool.fn(context_id="document", **arguments).meta["activity"]
    keys = ("project_ref", "target_project_id")
    assert {key: card[key] for key in keys if key in card} == expected


def test_stored_receipt_has_exact_target_without_payload():
    value = dict(
        status="stored",
        project_id="project",
        context_id="document",
        revision=3,
        request_key="original",
        body="private source",
        token="credential",
    )
    card = activity_result(value, "create")
    assert card["outcome"] == "stored"
    assert card["revision"] == 3
    assert card["context_id"] == "document"
    assert card["request_key"] == "original"
    assert "private source" not in str(card) and "credential" not in str(card)


def test_unknown_operation_is_not_a_failed_or_successful_save():
    card = activity_result({"error": {"code": "operation_unknown"}}, "create")
    assert card["outcome"] == "unknown"
    assert "revision" not in card


def test_partial_read_and_outline_never_claim_full_document_consumption():
    card = activity_result(
        dict(
            id="doc",
            project_id="project",
            revision=2,
            coverage="outline",
            sections=[],
            truncated=False,
        ),
        "read",
    )
    assert card["outcome"] == "retrieved" and card["coverage"] == "outline"
    assert "understood" not in str(card)
    partial = activity_result(
        dict(
            id="doc", project_id="project", revision=2, coverage="selected_sections", truncated=True
        ),
        "read",
    )
    assert partial["partial"] is True


def test_malformed_stored_result_cannot_become_success():
    assert activity_result({"status": "stored", "revision": True}, "create")["outcome"] == "unknown"


def test_browser_selection_completion_is_not_document_storage():
    assert (
        activity_result({"state": "completed", "result": {"resources": []}}, "view")["outcome"]
        == "displayed"
    )


def test_delivered_widget_keeps_report_decision_and_import_boundaries():
    from durumari_mcp.activity_assets import render_activity_html

    html = render_activity_html()
    # Reported stages are separate and never become independent verification.
    for text in ("문서의 보고 · 독립 검증 아님", "보고 기록 없음", "단계별 상태로 나누지 않음"):
        assert text in html
    # A past, replaced or unknown decision is not labelled as current.
    assert "대체 관계 있음 · 효력 확인 필요" in html
    # Section moves verify the cited hash; a stored import needs a receipt.
    assert "section.sha256!==source.sha256" in html
    assert "가져오기가 완료되지는 않습니다" in html
    # Widgets only use values the server sends: no invented import states or candidate kinds.
    for invented in ("'previewed'", "homonym", "자동으로 합치지 않음"):
        assert invented not in html
    assert "반복 관찰된 표현" in html


def test_widget_scripts_stay_offline_and_do_not_start_meeting_features():
    from pathlib import Path

    root = Path(__file__).parents[1] / "src" / "durumari_mcp"
    for name in ("explorer", "glossary", "knowledge", "collaboration", "materials", "domains"):
        source = (root / f"activity_{name}.js").read_text(encoding="utf-8")
        for forbidden in (
            "fetch(",
            "XMLHttpRequest",
            "localStorage",
            "sessionStorage",
            "meeting-stt",
            "durumari_meeting_transcript",
        ):
            assert forbidden not in source, (name, forbidden)


def test_delivered_widget_reports_content_height_without_viewport_units():
    import re
    from pathlib import Path

    from durumari_mcp.activity_assets import render_activity_html

    html = render_activity_html()
    # One observer on <main> covers every view, including the ones that return early from show().
    assert "new ResizeObserver(()=>reportSize()).observe(sizeRoot)" in html
    assert html.count("notify('ui/notifications/size-changed'") == 1
    # The reported height must not depend on the iframe's own height.
    assert "documentElement.scrollHeight" not in html
    root = Path(__file__).parents[1] / "src" / "durumari_mcp"
    for name in ("activity.html", "activity_explorer.js", "activity_domains.js"):
        source = (root / name).read_text(encoding="utf-8")
        assert not re.search(r"\d(?:vh|dvh|svh|lvh)\b", source), name


def test_delivered_widget_draws_document_bodies_through_one_safe_markdown_renderer():
    from pathlib import Path

    from durumari_mcp.activity_assets import render_activity_html

    html = render_activity_html()
    renderer = html.split('<script id="durumari-markdown">')[1].split("</script>", 1)[0]
    # Document text becomes text nodes only, and only these schemes become links.
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert forbidden not in renderer, forbidden
    assert "['http:','https:','mailto:'].includes(parsed.protocol)" in renderer
    # Every body display path goes through the renderer instead of a raw pre-wrap paragraph.
    root = Path(__file__).parents[1] / "src" / "durumari_mcp"
    sources = {path.name: path.read_text(encoding="utf-8") for path in root.glob("activity*.*")}
    section = "durumariMarkdown.section(section.text,section.title)"
    assert f"block.append({section})" in sources["activity.html"]
    assert "reader.append(durumariMarkdown.render(text))" in sources["activity.html"]
    assert section in sources["activity_explorer.js"]
    for name, text in sources.items():
        for raw in (
            "el('p',section.text",
            "element('p',section.text",
            "node('p',section.text",
            "el('p',documentBody(",
            "el('pre',documentBody(",
        ):
            assert raw not in text, (name, raw)


def test_secret_error_keeps_what_the_agent_needs_to_act_and_nothing_else():
    from durumari_mcp.activity_ui import scrub_secret_values

    limited = {
        "error": {
            "code": "project_rate_limited",
            "message": "잠시 뒤 다시 시도하세요",
            "details": {
                "retry_after_seconds": 42,
                "reason": "burst",
                "cause": "x" * 500,
                "next_action": "ask_owner",
                "invalid_fields": ["label", {"value": "nested-leak"}, "y" * 500],
                "value": "detail-leak",
                "echo": {"secret_value": "detail-leak"},
            },
        },
        "details": {"reason": "top-level-leak"},
    }
    shown = scrub_secret_values(limited)
    assert shown == {
        "error": {
            "code": "project_rate_limited",
            "message": "잠시 뒤 다시 시도하세요",
            "details": {
                "retry_after_seconds": 42,
                "reason": "burst",
                "cause": "x" * 200,
                "next_action": "ask_owner",
                "invalid_fields": ["label", "y" * 200],
            },
        }
    }
    # No details, or none of the allowed fields: the key is left out as before.
    assert scrub_secret_values(
        {"error": {"code": "access_denied", "details": {"value": "leak"}}}
    ) == {"error": {"code": "access_denied"}}
    assert scrub_secret_values({"error": {"code": "x", "details": "leak"}}) == {
        "error": {"code": "x"}
    }


def test_secret_card_projection_carries_names_and_states_but_never_values():
    from pathlib import Path
    from types import SimpleNamespace

    from durumari_mcp.activity_assets import render_activity_html
    from durumari_mcp.activity_ui import decorate_tool

    body = {
        "items": [
            {
                "id": "a" * 8,
                "label": "DEPLOY_KEY",
                "revision": 3,
                "value": "hunter2",
                "secret_value": "renamed-leak",
                "api_key": "renamed-leak",
                "nested": {"password": "x"},
            }
        ],
        "next_after": None,
    }

    def secret_list(**_: object) -> CallToolResult:
        return CallToolResult(
            content=[
                TextContent(type="text", text=json.dumps(body)),
                TextContent(type="text", text=json.dumps({"password": "second-leak"})),
                TextContent(type="text", text="not json: hunter2"),
                TextContent(type="text", text='"scalar-leak"'),
                ResourceLink(type="resource_link", name="resource-leak", uri="file:///leak"),
                TextContent(type="text", text='["list-leak"]'),
                TextContent(
                    type="text", text=json.dumps({"items": ["items-leak"], "status": "ok"})
                ),
            ],
            structured_content={"value": "structured-leak", "label": "DEPLOY_KEY"},
        )

    tool = SimpleNamespace(name="durumari_secret_list", fn=secret_list, meta=None)
    decorate_tool(tool)
    result = tool.fn(project_ref="p" * 8)
    shown = json.loads(result.content[0].text)
    assert shown["items"][0]["label"] == "DEPLOY_KEY" and shown["items"][0]["revision"] == 3
    everything = json.dumps([item.text for item in result.content]) + json.dumps(
        result.structured_content
    )
    assert (
        "hunter2" not in everything
        and "second-leak" not in everything
        and "structured-leak" not in everything
    )
    assert "renamed-leak" not in everything and "list-leak" not in everything
    assert "scalar-leak" not in everything and "items-leak" not in everything
    assert "resource-leak" not in everything and all(
        isinstance(item, TextContent) for item in result.content
    )
    assert [item.text for item in result.content[3:6]] == ["값이 포함될 수 있어 생략됨"] * 3
    assert json.loads(result.content[6].text) == {"items": [], "status": "ok"}
    assert result.content[2].text == "값이 포함될 수 있어 생략됨"
    assert result.structured_content == {"label": "DEPLOY_KEY"}
    assert result.meta["domain"]["tool"] == "durumari_secret_list"
    script = (Path(__file__).parents[1] / "src" / "durumari_mcp" / "activity_secrets.js").read_text(
        encoding="utf-8"
    )
    assert not any(
        tag in script for tag in ("<input", "<textarea", "'input'", "'textarea'", "clipboard")
    )
    html = render_activity_html()
    assert "보안 화면 열기" in html and "/*__DURUMARI_SECRETS__*/" not in html


def test_secret_status_card_names_every_server_operation_state():
    # waiting, approved, declined and expired must read as such, not "확인 필요".
    script = (Path(__file__).parents[1] / "src" / "durumari_mcp" / "activity_secrets.js").read_text(
        encoding="utf-8"
    )
    for state, label in {
        "awaiting_user": "승인 대기",
        "ready_for_review": "승인 대기",
        "completed": "승인됨",
        "cancelled": "거절됨",
        "expired": "만료됨",
        "failed": "실패",
    }.items():
        assert f"{state}:'{label}'" in script
    from durumari_mcp.activity_ui import scrub_secret_values

    assert scrub_secret_values({"processed": 1, "error_code": "x", "ciphertext": "v"}) == {
        "processed": 1,
        "error_code": "x",
    }


def test_secret_card_reads_state_again_after_the_secure_screen():
    # returning from the secure screen re-reads names and states only.
    script = (Path(__file__).parents[1] / "src" / "durumari_mcp" / "activity_secrets.js").read_text(
        encoding="utf-8"
    )
    assert "addEventListener('visibilitychange',wake)" in script
    assert "dataTool('durumari_secret_status',{...args,operation_id:source.operation_id})" in script
    assert "durumari_secret_reveal" not in script and "clipboard" not in script


def test_widget_dark_palette_and_spacing_come_from_figma_tokens():
    # dark values come from the Figma Foundation Dark mode, and on-scale
    # spacing, type and radius values use tokens instead of px.
    root = Path(__file__).parents[1] / "src" / "durumari_mcp"
    tokens = (root / "activity_assets" / "tokens.css").read_text(encoding="utf-8")
    dark = tokens.split(':root[data-theme="dark"]', 1)[1]
    assert "--surface: #161b22;" in dark and "--info-surface: #121d33;" in dark
    assert "--border-subtle: #0e1116;" not in dark and "--surface-app: #0e1116;" in dark
    domains = (root / "activity_domains.js").read_text(encoding="utf-8")
    # UI v3: the shared surfaces live in widget.css; the domain views keep spacing on tokens.
    assert "gap:var(--space-3)" in domains and "padding-left:var(--space-4)" in domains
    assert "gap:16px" not in domains and "gap:12px" not in domains
    widget = (root / "activity_assets" / "widget.css").read_text(encoding="utf-8")
    assert not re.search(r"#[0-9a-fA-F]{3,8}\b|rgba?\(", widget), "hard-coded colour in widget.css"
