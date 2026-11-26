"""Request tools share native/server definitions and separate notes from instructions."""

import json
from types import SimpleNamespace

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.context_client import ContextClient
from durumari_mcp.credentials import AdapterError

REQUEST = "11111111-1111-1111-1111-111111111111"
ITEM = "22222222-2222-2222-2222-222222222222"
PERSON = "33333333-3333-3333-3333-333333333333"
CONTEXT = "44444444-4444-4444-4444-444444444444"
AGREER = "55555555-5555-5555-5555-555555555555"
OBSERVER = "66666666-6666-6666-6666-666666666666"
INSTRUCTION = {"source": "explicit_user_instruction", "message_ref": "message-17"}
LINE = [
    {"kind": "agreement", "user_ids": [AGREER, CONTEXT]},
    {"kind": "approval", "user_ids": [PERSON]},
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


class Operations:
    def __init__(self):
        self.calls = []

    def requests(self, action, body):
        self.calls.append((action, body))
        return {"action": action, "applied_revision": 2}


@pytest.mark.anyio
async def test_cancel_tool_is_marked_destructive():
    async with Client(create_context_server(Operations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        assert tools["durumari_request_cancel"].annotations.destructive_hint is True
        assert tools["durumari_request_list"].annotations.destructive_hint is False


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "action", "body"),
    [
        ("durumari_request_participants", {}, "participants", {}),
        ("durumari_request_participants", {"query": "민호님"}, "participants", {"query": "민호님"}),
        ("durumari_request_list", {}, "list", {"scope": "needs_me", "limit": 10}),
        (
            "durumari_request_list",
            {"scope": "sent", "after": "cursor", "limit": 20},
            "list",
            {"scope": "sent", "after": "cursor", "limit": 20},
        ),
        ("durumari_request_read", {"request_id": REQUEST}, "read", {"request_id": REQUEST}),
        ("durumari_request_read", {"request_id": REQUEST, "project_ref": CONTEXT}, "read",
         {"request_id": REQUEST, "project_ref": CONTEXT}),
        (
            "durumari_request_create",
            {
                "request_key": "create-1",
                "kind": "decision",
                "title": "!446 병합",
                "approver_id": PERSON,
                "items": [
                    {
                        "question": "병합할까요?",
                        "details": "head 73b9d1a4",
                        "references": [{"context_id": CONTEXT, "revision": 3}],
                        "reference_url": "https://lab.example/mr/446",
                        "merge_request": {
                            "url": "https://lab.example/team/project/-/merge_requests/446",
                            "iid": 446,
                            "head_sha": "a" * 40,
                            "target_branch": "dev",
                            "action": "merge",
                        },
                    }
                ],
            },
            "create",
            {
                "request_key": "create-1",
                "kind": "decision",
                "title": "!446 병합",
                "approver_id": PERSON,
                "items": [
                    {
                        "question": "병합할까요?",
                        "details": "head 73b9d1a4",
                        "references": [{"context_id": CONTEXT, "revision": 3}],
                        "reference_url": "https://lab.example/mr/446",
                        "merge_request": {
                            "url": "https://lab.example/team/project/-/merge_requests/446",
                            "iid": 446,
                            "head_sha": "a" * 40,
                            "target_branch": "dev",
                            "action": "merge",
                        },
                    }
                ],
            },
        ),
        (
            "durumari_request_note",
            {
                "request_key": "note-1",
                "request_id": REQUEST,
                "base_revision": 4,
                "text": "CI 통과, 위험 없음",
                "recommendation": "approve",
                "basis": {"sha": "a" * 40, "checked_at": "2026-10-23T10:00:00+09:00"},
            },
            "note",
            {
                "request_key": "note-1",
                "request_id": REQUEST,
                "base_revision": 4,
                "text": "CI 통과, 위험 없음",
                "recommendation": "approve",
                "basis": {"sha": "a" * 40, "checked_at": "2026-10-23T10:00:00+09:00"},
            },
        ),
        (
            "durumari_request_revise",
            {
                "request_key": "revise-1",
                "request_id": REQUEST,
                "base_revision": 5,
                "item_id": ITEM,
                "text": "지적 사항 수정, 새 head b1",
            },
            "revise",
            {
                "request_key": "revise-1",
                "request_id": REQUEST,
                "base_revision": 5,
                "item_id": ITEM,
                "text": "지적 사항 수정, 새 head b1",
            },
        ),
    ],
)
async def test_tools_forward_exact_bodies_without_unset_values(name, arguments, action, body):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, arguments)
    assert not result.is_error
    assert operations.calls == [(action, body)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("durumari_request_note", {"request_id": REQUEST, "base_revision": 1, "text": "x"}),
        (
            "durumari_request_note",
            {
                "request_key": "k",
                "request_id": REQUEST,
                "base_revision": 1,
                "text": "x",
                "response": "reviewed",
            },
        ),
        (
            "durumari_request_note",
            {
                "request_key": "k",
                "request_id": REQUEST,
                "base_revision": 1,
                "text": "x",
                "recommendation": "confirmed",
            },
        ),
        ("durumari_request_read", {"request_id": "../../admin"}),
        (
            "durumari_request_note",
            {
                "request_key": "k",
                "request_id": REQUEST,
                "base_revision": 1,
                "text": "x",
                "basis": {"sha": "a" * 40, "checked_at": "2026-10-23T10:00:00"},
            },
        ),
        (
            "durumari_request_revise",
            {
                "request_key": "k",
                "request_id": REQUEST,
                "base_revision": 1,
                "item_id": ITEM,
                "text": "x" * 2001,
            },
        ),
        ("durumari_request_list", {"limit": 21}),
        (
            "durumari_request_create",
            {
                "request_key": "k",
                "kind": "decision",
                "title": "t",
                "items": [],
                "approver_id": PERSON,
            },
        ),
        (
            "durumari_request_revise",
            {
                "request_key": "k",
                "request_id": REQUEST,
                "base_revision": 1,
                "item_id": ITEM,
                "text": "x",
                "reference_url": "javascript:alert(1)",
            },
        ),
    ],
)
async def test_invalid_or_decision_arguments_never_reach_operations(name, arguments):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert not operations.calls


@pytest.mark.anyio
async def test_request_tools_have_declared_scopes():
    async with Client(create_context_server(Operations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    requests = {name for name in tools if name.startswith("durumari_request_")}
    assert requests == {
        "durumari_request_participants",
        "durumari_request_list",
        "durumari_request_read",
        "durumari_request_create",
        "durumari_request_note",
        "durumari_request_revise",
        "durumari_request_respond",
        "durumari_request_decide",
        "durumari_request_cancel",
    }
    for name in requests:
        read = name.rsplit("_", 1)[1] in {"participants", "list", "read"}
        scheme = tools[name].meta["securitySchemes"]
        assert scheme == [
            {"type": "oauth2", "scopes": ["requests:read" if read else "requests:write"]}
        ]
        assert tools[name].annotations.read_only_hint is read


@pytest.mark.anyio
async def test_clients_without_requests_list_no_request_tools():
    class ContextOnly:
        def read(self, suffix="", **query):
            return {}

    async with Client(create_context_server(ContextOnly())) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert not any(name.startswith("durumari_request_") for name in names)


def test_stdio_client_uses_the_agent_route_with_the_request_key():
    calls = []

    def context_request(path, body=None, *, method="GET", idempotency_key=None):
        calls.append((path, body, method, idempotency_key))
        return {"ok": True}

    client = ContextClient.__new__(ContextClient)
    client.gateway = SimpleNamespace(context_request=context_request)
    client.requests("note", {"request_key": "note-1", "text": "x"})
    client.requests("read", {"request_id": REQUEST})
    assert calls == [
        (
            "/api/v2/agent/requests/note",
            {"request_key": "note-1", "text": "x"},
            "POST",
            "note-1",
        ),
        ("/api/v2/agent/requests/read", {"request_id": REQUEST}, "POST", None),
    ]
    with pytest.raises(AdapterError, match="invalid_request_operation"):
        client.requests("resolve", {})


def test_gateway_accepts_only_the_declared_request_routes():
    import re

    from durumari_mcp import project_gateway

    source = open(project_gateway.__file__, encoding="utf-8").read()
    found = re.search(r'r"(/api/v2/agent/requests/)"\s*r"([^"]+)"', source)
    pattern = found.group(1) + found.group(2)
    for action in ("participants", "list", "read", "create", "note", "revise", "respond", "decide"):
        assert re.fullmatch(pattern, "/api/v2/agent/requests/" + action)
    for action in ("resolve", "cancel", "reassign", "note/../respond"):
        assert not re.fullmatch(pattern, "/api/v2/agent/requests/" + action)


def create(**values):
    return {"request_key": "line-1", "title": "Agent 연결 정책",
            "items": [{"question": "이대로 정할까요?"}], **values}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "body"),
    [
        # An approval line and cc go as they are; kind defaults to decision.
        (create(approval_line=LINE, cc_ids=[OBSERVER]),
         create(kind="decision", approval_line=LINE, cc_ids=[OBSERVER])),
        # One approver alone, the requester included, is a whole line.
        (create(approval_line=[{"kind": "approval", "user_ids": [PERSON]}]),
         create(kind="decision", approval_line=[{"kind": "approval", "user_ids": [PERSON]}])),
        # Earlier clients: the server converts approver_id and reviewers.
        (create(approver_id=PERSON, reviewers=[{"user_id": AGREER, "required": True}]),
         create(kind="decision", approver_id=PERSON,
                reviewers=[{"user_id": AGREER, "required": True}])),
        (create(approver_id=PERSON), create(kind="decision", approver_id=PERSON)),
        # A retired review still reaches the server, which refuses it after a receipt replay.
        (create(kind="review", reviewers=[{"user_id": AGREER, "required": True}]),
         create(kind="review", reviewers=[{"user_id": AGREER, "required": True}])),
    ],
)
async def test_create_sends_an_approval_line_or_earlier_inputs(arguments, body):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_request_create", arguments)
    assert not result.is_error
    assert operations.calls == [("create", body)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "code"),
    [
        (create(approval_line=[{"kind": "agreement", "user_ids": [AGREER]}]),
         "approval_line_invalid"),
        (create(approval_line=[*LINE, {"kind": "agreement", "user_ids": [OBSERVER]}]),
         "approval_line_invalid"),
        (create(approval_line=[{"kind": "agreement", "user_ids": [PERSON]}, LINE[1]]),
         "approval_line_invalid"),
        (create(approval_line=LINE, cc_ids=[AGREER]), "approval_line_invalid"),
        (create(approval_line=LINE, cc_ids=[OBSERVER, OBSERVER]), "approval_line_invalid"),
        (create(approval_line=LINE, approver_id=PERSON), "approval_line_invalid"),
        (create(approval_line=LINE, reviewers=[{"user_id": AGREER}]), "approval_line_invalid"),
        (create(approver_id=PERSON, cc_ids=[OBSERVER]), "approval_line_invalid"),
        (create(), "approval_line_required"),
        (create(approval_line=[{"kind": "approval", "user_ids": [PERSON, AGREER]}]), None),
        (create(approval_line=[{"kind": "approval", "user_ids": []}]), None),
        (create(approval_line=[]), None),
        (create(approval_line=[LINE[0]] * 10 + [LINE[1]]), None),
        (create(approval_line=[{"kind": "consult", "user_ids": [PERSON]}]), None),
    ],
)
async def test_malformed_approval_lines_never_reach_the_server(arguments, code):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_request_create", arguments)
    assert result.is_error
    assert operations.calls == []
    if code:
        assert json.loads(result.content[0].text)["error"]["code"] == code


def formal(**values):
    return {"request_key": "choice-1", "request_id": REQUEST, "item_id": ITEM,
            "base_revision": 3, "expected_round": 2, "user_instruction": INSTRUCTION, **values}


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("arguments", "body"),
    [
        (formal(choice="agree"), formal(choice="agree", text="")),
        (formal(choice="approve", text="이대로 정합니다.", followup_url="https://lab.example/1"),
         formal(choice="approve", text="이대로 정합니다.", followup_url="https://lab.example/1")),
        (formal(choice="reject", text="근거를 보완해 주세요.", reason="근거 부족"),
         formal(choice="reject", text="근거를 보완해 주세요.", reason="근거 부족")),
        (formal(outcome="confirmed", text="승인합니다.", reason="검증 완료"),
         formal(outcome="confirmed", text="승인합니다.", reason="검증 완료")),
    ],
)
async def test_decide_sends_a_step_choice_or_an_earlier_outcome(arguments, body):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_request_decide", arguments)
    assert not result.is_error
    assert operations.calls == [("decide", body)]


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("name", "arguments", "code"),
    [
        ("durumari_request_decide", formal(), "approval_choice_invalid"),
        ("durumari_request_decide", formal(choice="approve", outcome="confirmed", text="x",
                                           reason="y"), "approval_choice_invalid"),
        ("durumari_request_decide", formal(choice="reject"), "approval_opinion_required"),
        ("durumari_request_decide", formal(choice="hold", text=" "), "approval_opinion_required"),
        ("durumari_request_decide", formal(choice="decline"), "approval_opinion_required"),
        ("durumari_request_decide", formal(outcome="deferred", text="보류"),
         "approval_opinion_required"),
        ("durumari_request_decide", formal(choice="review"), None),
        ("durumari_request_respond", formal(response="changes_requested"),
         "approval_opinion_required"),
    ],
)
async def test_step_choices_without_a_choice_or_opinion_never_reach_the_server(
    name, arguments, code
):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool(name, arguments)
    assert result.is_error
    assert operations.calls == []
    if code:
        assert json.loads(result.content[0].text)["error"]["code"] == code


@pytest.mark.anyio
async def test_tool_guidance_names_the_approval_line_and_my_turn():
    async with Client(create_context_server(Operations())) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
    create_tool = tools["durumari_request_create"]
    assert {"approval_line", "cc_ids"} <= set(create_tool.input_schema["properties"])
    assert "kind" not in create_tool.input_schema.get("required", [])
    assert "review_kind_retired" in create_tool.description
    assert "your turn" in tools["durumari_request_list"].description
    for field in ("waiting_on", "trail", "stage"):
        assert field in tools["durumari_request_read"].description
    decide = tools["durumari_request_decide"].input_schema
    assert set(decide["properties"]["choice"]["anyOf"][0]["enum"]) == {
        "approve", "reject", "hold", "decline", "agree"
    }
    assert "choice" not in decide["required"] and "outcome" not in decide["required"]


def cancelling(**values):
    return {"request_key": "cancel-1", "request_id": REQUEST, "base_revision": 3,
            "reason": "다른 경로로 정리됨", "user_instruction": INSTRUCTION, **values}


@pytest.mark.anyio
async def test_cancel_sends_the_reason_and_instruction_to_the_server():
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_request_cancel", cancelling())
    assert not result.is_error
    assert operations.calls == [("cancel", cancelling())]


@pytest.mark.anyio
@pytest.mark.parametrize(
    "arguments",
    [
        {k: v for k, v in cancelling().items() if k != "user_instruction"},
        {k: v for k, v in cancelling().items() if k != "reason"},
        cancelling(reason=""),
        cancelling(item_id=ITEM),
    ],
)
async def test_cancel_without_instruction_or_reason_never_reaches_the_server(arguments):
    operations = Operations()
    async with Client(create_context_server(operations)) as client:
        result = await client.call_tool("durumari_request_cancel", arguments)
    assert result.is_error
    assert operations.calls == []
