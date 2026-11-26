import json

import pytest
from mcp import Client

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.credentials import AdapterError
from durumari_mcp.glossary_tools import deliver
from durumari_mcp.project_gateway import ContextFailure


def test_receipts_are_checked_and_uncertain_delivery_never_claims_stored():
    with pytest.raises(AdapterError, match="invalid_glossary_receipt"):
        deliver(
            lambda _: {"status": "stored", "project_id": "wrong", "request_key": "k"},
            "create",
            {"request_key": "k"},
            project_id="expected",
        )

    def lost(_):
        raise TimeoutError()

    assert deliver(lost, "observe", {"request_key": "k"}) == {
        "status": "unknown",
        "request_key": "k",
        "next_action": "read_operation_or_retry_same_key_and_payload",
    }


@pytest.mark.parametrize(
    "error",
    [
        AdapterError("account_reconnect_required"),
        AdapterError("project_rate_limited"),
        ContextFailure(403, {"code": "access_denied"}),
        ContextFailure(409, {"code": "project_ref_stale"}),
        ContextFailure(422, {"code": "invalid_glossary_operation"}),
    ],
)
def test_definitive_rejections_are_not_reported_as_unknown_delivery(error):
    def rejected(_):
        raise error

    with pytest.raises(type(error)):
        deliver(rejected, "create", {"request_key": "k"})


def test_only_transport_uncertainty_uses_the_unknown_delivery_receipt():
    def lost(_):
        raise AdapterError("project_delivery_unknown")

    assert deliver(lost, "create", {"request_key": "k"})["status"] == "unknown"

    def unexpected(_):
        raise RuntimeError("internal details must not be exposed")

    with pytest.raises(AdapterError, match="^glossary_delivery_failed$"):
        deliver(unexpected, "create", {"request_key": "k"})


@pytest.mark.parametrize(
    "action,body",
    [
        ("review", {"request_key": "k"}),
        ("observe", {"request_key": "bad/key"}),
        ("terms", {"project_ref": "caller-overrides-target"}),
    ],
)
def test_invalid_or_privileged_calls_never_reach_transport(action, body):
    sent = []
    with pytest.raises(AdapterError):
        deliver(lambda value: sent.append(value) or {}, action, body)
    assert not sent


@pytest.mark.anyio
async def test_sdk_uses_typed_actions_and_explicit_immutable_target():
    sent = []

    class Target:
        def glossary(self, action, body):
            sent.append((action, body))
            return {"items": []}

    class Operations:
        def targeted(self, ref):
            sent.append(ref)
            return Target()

    async with Client(create_context_server(Operations())) as client:
        output = await client.call_tool(
            "durumari_glossary_read",
            {"project_ref": "00000000-0000-0000-0000-000000000001", "query": "엠씨피"},
        )
        assert not output.is_error and json.loads(output.content[0].text) == {"items": []}
        assert sent[-1] == ("terms", {"q": "엠씨피"})
        invalid = await client.call_tool(
            "durumari_glossary_submit", {"action": "review", "request_key": "k", "body": {}}
        )
        assert invalid.is_error


def test_no_automatic_merge_action_exists_for_same_expression_candidates():
    from durumari_mcp.glossary_tools import READS, WRITES

    assert not [name for name in READS | WRITES if "merge" in name or "homonym" in name]
    sent = []
    for action in ("merge", "merge_candidates", "accept"):
        with pytest.raises(AdapterError, match="invalid_glossary_operation"):
            deliver(lambda value: sent.append(value) or {}, action, {"request_key": "k"})
    assert not sent


@pytest.mark.anyio
async def test_discovery_candidate_is_returned_as_review_state_without_any_write():
    # Shape of the server's deterministic discovery candidate (glossary/observations.py).
    sent = []
    candidate = {
        "items": [
            {
                "id": "candidate",
                "kind": "discovery",
                "status": "pending",
                "origin": "discovery",
                "blocked": True,
                "expression": "바인딩",
                "term": None,
            }
        ]
    }

    class Target:
        def glossary(self, action, body):
            sent.append(action)
            return candidate

    class Operations:
        def targeted(self, ref):
            return Target()

    async with Client(create_context_server(Operations())) as client:
        output = await client.call_tool(
            "durumari_glossary_read",
            {"project_ref": "00000000-0000-0000-0000-000000000001", "view": "candidates"},
        )
    assert json.loads(output.content[0].text) == candidate
    assert sent == ["candidates"]
