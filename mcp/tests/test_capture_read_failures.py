"""A content-free failed read ends its wait, without claiming a successful read."""

import json

import pytest

from durumari_mcp.capture import normalize_transcript


def transcript(client, response, *, wrapped=False, call_id="read1"):
    tool = "durumari_context_read"
    if client == "claude":
        return [
            {"type": "assistant", "message": {"content": [{
                "type": "tool_use", "id": "read1", "name": tool, "input": {},
            }]}},
            {"type": "user", "message": {"content": [{
                "type": "tool_result", "tool_use_id": call_id, "is_error": True,
                "content": response,
            }]}},
            {"type": "assistant", "message": {"content": "The read failed."}},
        ]
    return [
        {"type": "response_item", "payload": {
            "type": "custom_tool_call" if wrapped else "function_call",
            "name": "exec" if wrapped else tool, "call_id": "read1",
            "input": f"text(await tools.mcp__project__{tool}({{}}));" if wrapped else "{}",
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output" if wrapped else "function_call_output",
            "call_id": call_id, "output": response,
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": "The read failed.",
        }},
    ]


ERROR = "Error executing tool durumari_context_read: project_rate_limited"


@pytest.mark.parametrize("client,wrapped", [("codex", False), ("codex", True), ("claude", False)])
def test_fixed_read_failure_ends_only_its_wait(client, wrapped):
    content = [{"type": "text", "text": ERROR}]
    envelope = {"content": content, "isError": True}
    response = content if client == "claude" else json.dumps(envelope)
    if wrapped:
        response = [
            {"type": "input_text", "text": "Script completed\nWall time 0.2 seconds\nOutput:\n"},
            {"type": "input_text", "text": json.dumps(envelope)},
        ]
    rows = transcript(client, response, wrapped=wrapped)
    result = normalize_transcript(client, [json.dumps(r) for r in rows], "synthetic")
    assert not result.pending_read_ids
    assert len(result.resolved_read_ids) == 1
    assert "sensitive_session_quarantined" not in result.omissions
    assert "context_read_failed_without_content" in result.omissions
    assert [e["text"] for e in result.events] == ["The read failed."]


@pytest.mark.parametrize("response", [
    ERROR + "\nprivate source content",
    ERROR.replace("durumari_context_read", "durumari_project_handoff"),
    ERROR.replace("project_rate_limited", "unknown_failure"),
    json.dumps({"content": [{"type": "text", "text": ERROR}], "isError": False}),
    json.dumps({"content": [{"type": "text", "text": ERROR}], "isError": True,
                "resultType": "task"}),
    json.dumps({"content": [{"type": "text", "text": ERROR}], "isError": True,
                "structuredContent": {"body": "private source content"}}),
    json.dumps({"content": [{"type": "text", "text": ERROR},
                            {"type": "image", "data": "private"}], "isError": True}),
    json.dumps({"content": [{"type": "text", "text": ERROR}], "isError": True,
                "_meta": {"private": "source content"}}),
    '{"content":[],"content":[{"type":"text","text":' + json.dumps(ERROR)
    + '}],"isError":true}',
])
def test_unrecognized_or_mixed_read_errors_remain_quarantined(response):
    rows = transcript("codex", response)
    result = normalize_transcript("codex", [json.dumps(r) for r in rows], "synthetic")
    assert "unattested_context_read_quarantined" in result.omissions
    assert not result.events


def test_fixed_error_for_another_call_does_not_complete_pending_read():
    rows = transcript("codex", ERROR, call_id="another-call")
    result = normalize_transcript("codex", [json.dumps(r) for r in rows], "synthetic")
    assert "context_read_pending" in result.omissions
    assert not result.resolved_read_ids
    assert not result.events




