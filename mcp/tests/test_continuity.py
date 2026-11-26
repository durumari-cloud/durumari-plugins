"""Scoped credentials, encrypted recovery, and synthetic lifecycle boundary tests."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from durumari_mcp.capture import normalize_transcript
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_credentials import ProjectProfile


@pytest.mark.parametrize("tool", ["durumari_context_read", "durumari_project_handoff"])
def test_simple_codex_read_wrapper_waits_for_attestation(tool):
    source = f'text(await tools.mcp__project__{tool}({{"context_id":"ordinary"}}));'
    call = {"type": "response_item", "payload": {
        "type": "custom_tool_call", "name": "exec", "call_id": "read1", "input": source,
    }}
    result = normalize_transcript("codex", [json.dumps(call)], "synthetic")
    assert "context_read_pending" in result.omissions
    assert "sensitive_session_quarantined" not in result.omissions
    completed = {"type": "response_item", "payload": {
        "type": "custom_tool_call_output", "call_id": "read1",
        "output": json.dumps({"project_id": "p", "connection_id": "c"}),
    }}
    result = normalize_transcript(
        "codex", [json.dumps(call), json.dumps(completed)], "synthetic", scope=("p", "c")
    )
    assert len(result.events) == 1
    assert "sensitive_session_quarantined" not in result.omissions


@pytest.mark.parametrize("source", [
    'text(await tools.mcp__project__durumari_context_read({})); await tools.read_secret({});',
    'text(await tools.mcp__project__durumari_context_read({context_id: getSecret()}));',
    'text(await tools.mcp__project__durumari_context_read({"password":"synthetic"}));',
    'text(await tools.mcp__project__durumari_context_read({...secret}));',
])
def test_unknown_or_sensitive_read_wrappers_remain_quarantined(source):
    from durumari_mcp.capture import sensitive_operation

    assert sensitive_operation("exec", source)


@pytest.mark.parametrize(
    "variant",
    ["valid", "foreign_connection", "foreign_nested_project", "error", "malformed",
     "unknown_header", "extra_unscoped_result", "extra_foreign_result", "non_text_block",
     "deferred_result"],
)
def test_codex_project_read_attests_each_framed_result(variant):
    # Observed Codex 0.153.4 exec output: a status block followed by MCP JSON blocks.
    scope = {"project_id": "project-1", "connection_id": "conn-1"}
    if variant == "foreign_connection":
        scope["connection_id"] = "conn-other"
    if variant == "foreign_nested_project":
        scope["nested"] = {"project_id": "project-other"}
    result = {
        "content": [{"type": "text", "text": json.dumps(scope)}],
        "isError": variant == "error",
    }
    if variant == "deferred_result":
        result["resultType"] = "task"
    blocks = [
        {"type": "input_text", "text": "Script completed\nWall time 0.4 seconds\nOutput:\n"},
        {"type": "input_text", "text": json.dumps(result)},
    ]
    if variant == "malformed":
        blocks[-1]["text"] += "invalid trailer"
    elif variant == "unknown_header":
        blocks[0]["text"] = "Unknown host frame\nOutput:\n"
    elif variant == "extra_unscoped_result":
        blocks.append({"type": "input_text", "text": json.dumps({"content": []})})
    elif variant == "extra_foreign_result":
        blocks.append({"type": "input_text", "text": json.dumps({
            "content": [{"type": "text", "text": json.dumps({
                "project_id": "project-other", "connection_id": "conn-other",
            })}],
        })})
    elif variant == "non_text_block":
        blocks.append({"type": "input_image", "image_url": "untrusted"})
    rows = [
        {"type": "response_item", "payload": {
            "type": "custom_tool_call", "call_id": "read1", "name": "exec",
            "input": "text(await tools.mcp__project__durumari_project_handoff({}));",
        }},
        {"type": "response_item", "payload": {
            "type": "custom_tool_call_output", "call_id": "read1", "output": blocks,
        }},
        {"type": "response_item", "payload": {
            "type": "message", "role": "assistant", "content": "SHARED_CONTEXT_ANSWER",
        }},
    ]
    captured = normalize_transcript(
        "codex", [json.dumps(row) for row in rows], "synthetic", scope=("project-1", "conn-1")
    )
    if variant == "valid":
        assert any(event["text"] == "SHARED_CONTEXT_ANSWER" for event in captured.events)
        assert "sensitive_session_quarantined" not in captured.omissions
    else:
        assert not captured.events
        assert "unattested_context_read_quarantined" in captured.omissions


def test_claude_project_tool_discovery_does_not_execute_a_context_read():
    from durumari_mcp.capture import sensitive_operation

    name = "mcp__durumari_project__durumari_context_read"
    arguments = {"query": "select:" + name, "max_results": 2}
    rows = [
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "discover", "name": "ToolSearch", "input": arguments}
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "discover",
                        "content": [{"type": "tool_reference", "tool_name": name}],
                    }
                ]
            },
        },
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "SAFE_MARKER"}]}},
    ]
    assert not sensitive_operation("ToolSearch", arguments)
    result = normalize_transcript("claude", [json.dumps(row) for row in rows], "discovery")
    assert [event["text"] for event in result.events] == ["SAFE_MARKER"]
    assert "sensitive_session_quarantined" not in result.omissions
    assert "context_read_pending" not in result.omissions
    assert sensitive_operation("ToolSearch", {"query": "durumari_context_read password"})


def profile(tmp_path):
    return ProjectProfile(
        gateway_origin="https://durumari.example.com",
        connection_id="conn-1",
        project_id="project-1",
        source_id="source-1",
        actor_id="actor-1",
        client="claude",
        workspace=str(tmp_path),
        access_token="mpa_" + "a" * 43,
        refresh_token="mpr_" + "b" * 43,
        access_expires_at=datetime.now(UTC) + timedelta(minutes=15),
        outbox_key=Fernet.generate_key().decode(),
    )


def test_changed_manifest_cannot_redirect_existing_credentials(tmp_path):
    import yaml
    row = profile(tmp_path)
    (tmp_path / "durumari.yaml").write_text(yaml.safe_dump({
        "schema_version": 1, "server": row.gateway_origin,
        "project_id": "different-project", "source_id": row.source_id,
        "context_source": "server",
    }))
    with pytest.raises(AdapterError, match="project_connection_scope_changed"):
        row.check_workspace(str(tmp_path))


def test_project_profile_rejects_native_token_and_insecure_origin(tmp_path):
    value = profile(tmp_path).protected_json()
    assert "mpa_" in value and "********" not in value
    for field, token in [
        ("access_token", "Bearer_native_secret"),
        ("refresh_token", "native_refresh_secret"),
        ("gateway_origin", "http://durumari.example.com"),
    ]:
        data = json.loads(value)
        data[field] = token
        with pytest.raises(ValueError):
            ProjectProfile.model_validate(data)


@pytest.mark.parametrize("connection", ["conn-1", "other-connection", None])
def test_general_context_read_requires_attestation_without_secret_false_positive(connection):
    timestamp = datetime.now(UTC).isoformat()
    output = {"project_id": "project-1", "body": "Approved ordinary project document."}
    if connection:
        output["connection_id"] = connection
    records = [
        {"type": "user", "timestamp": timestamp, "message": {"content": "Read our context."}},
        {
            "type": "assistant",
            "timestamp": timestamp,
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "read-1",
                        "name": "mcp__durumari_project__durumari_context_read",
                        "input": {"context_id": "ordinary-context"},
                    }
                ]
            },
        },
        {
            "type": "user",
            "timestamp": timestamp,
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "read-1", "content": json.dumps(output)}
                ]
            },
        },
        {"type": "assistant", "timestamp": timestamp, "message": {"content": "SAFE_MARKER"}},
    ]
    result = normalize_transcript(
        "claude", [json.dumps(row) for row in records], "session-1", scope=("project-1", "conn-1")
    )
    if connection == "conn-1":
        assert any(row["text"] == "SAFE_MARKER" for row in result.events)
        assert "sensitive_session_quarantined" not in result.omissions
    else:
        assert not any(row["kind"] in {"assistant", "tool"} for row in result.events)
        assert "unattested_context_read_quarantined" in result.omissions


def test_normalizes_claude_raw_sources_without_reasoning():
    rows = [
        {"type": "user", "message": {"role": "user", "content": "Keep project scope"}},
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "DO_NOT_CAPTURE_REASONING"},
                    {"type": "text", "text": "Implemented"},
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {}},
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "Synthetic result"},
                ],
            },
        },
    ]
    result = normalize_transcript("claude", [json.dumps(r) for r in rows], "session-1")
    assert [r["kind"] for r in result.events] == ["user", "assistant", "tool"]
    assert "DO_NOT_CAPTURE_REASONING" not in json.dumps(result.events)
    assert result.coverage == "partial_host_transcript"


def test_secret_use_quarantines_subsequent_outputs():
    rows = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "secret",
                        "name": "durumari_use_secret_in_model",
                        "input": {},
                    },
                ],
            },
        },
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "secret", "content": "FAKE_SECRET"},
                ],
            },
        },
        {"type": "assistant", "message": {"role": "assistant", "content": "derived FAKE_SECRET"}},
    ]
    result = normalize_transcript("claude", [json.dumps(r) for r in rows], "session-1")
    assert not result.events
    assert "sensitive_session_quarantined" in result.omissions


def test_unknown_or_truncated_format_never_claims_full_capture():
    result = normalize_transcript("codex", ['{"type":"unknown"}', '{"type":'], "session")
    assert not result.events
    assert "unsupported_record" in result.omissions
    assert "incomplete_or_invalid_record" in result.omissions


def test_codex_exec_nested_secret_call_taints_derived_output():
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "functions.exec",
                "arguments": "await tools.mcp__durumari__durumari_use_secret_in_model({})",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "output": "unrecognizable-sensitive-derived-text",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "later-derived-output"}],
            },
        },
    ]
    result = normalize_transcript("codex", [json.dumps(r) for r in rows], "session")
    assert not result.events
    assert "sensitive_session_quarantined" in result.omissions


def test_legacy_analysis_channel_is_never_captured():
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "channel": "analysis",
                "content": [{"type": "output_text", "text": "PRIVATE_REASONING"}],
            },
        }
    ]
    result = normalize_transcript("codex", [json.dumps(row) for row in rows], "session")
    assert not result.events


class MemoryStore:
    def __init__(self):
        self.values = {}

    def set_password(self, service, name, value):
        self.values[(service, name)] = value

    def get_password(self, service, name):
        return self.values.get((service, name))

    def delete_password(self, service, name):
        del self.values[(service, name)]


@pytest.fixture
def project_store(monkeypatch):
    from durumari_mcp import project_credentials

    store = MemoryStore()
    monkeypatch.setattr(project_credentials, "os_store", lambda: store)
    return store


def connection(scope):
    return {
        "id": scope.connection_id,
        "project_id": scope.project_id,
        "source_id": scope.source_id,
        "actor_id": scope.actor_id,
        "host": scope.client,
        "provider": "anthropic",
        "environment_name": "Synthetic environment",
        "actions": ["read", "capture"],
        "revision": 1,
        "state": "active",
        "created_at": datetime.now(UTC).isoformat(),
        "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "last_received_at": None,
        "capture_status": "host_capture_not_verified",
    }


def test_refresh_rotates_only_scoped_credentials(project_store, tmp_path):
    from durumari_mcp.project_credentials import load_project_profile, save_project_profile
    from durumari_mcp.project_gateway import ProjectGateway

    scope = profile(tmp_path).model_copy(
        update={
            "access_expires_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "claude", root=tmp_path / "queue")
    calls = []

    def request(path, **kwargs):
        if path.endswith("/refresh/prepare"):
            raise AdapterError("project_refresh_not_supported")
        calls.append((path, kwargs))
        if path.endswith("/refresh"):
            return {
                "connection": connection(scope),
                "access_token": "mpa_" + "c" * 43,
                "refresh_token": "mpr_" + "d" * 43,
                "access_expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
            }
        return connection(scope)

    gateway.transport.request = request
    gateway.request("/api/v1/project-agent/connection")
    assert load_project_profile("team").access_token.get_secret_value() == "mpa_" + "c" * 43
    assert calls[0][1]["body"] == {"refresh_token": "mpr_" + "b" * 43}
    assert calls[1][1]["token"].get_secret_value() == "mpa_" + "c" * 43
    assert "native" not in str(project_store.values)


@pytest.mark.parametrize(
    "failure", ["project_delivery_unknown", "project_unavailable", "project_response_too_large"]
)
def test_unknown_refresh_never_reuses_consumed_token(project_store, tmp_path, failure):
    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.project_gateway import ProjectGateway

    scope = profile(tmp_path).model_copy(
        update={
            "access_expires_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "claude", root=tmp_path / "queue")
    calls = []

    def failed(*args, **kwargs):
        if args[0].endswith("/refresh/prepare"):
            raise AdapterError("project_refresh_not_supported")
        calls.append(args)
        raise AdapterError(failure)

    gateway.transport.request = failed
    with pytest.raises(AdapterError, match=failure):
        gateway.request("/api/v1/project-agent/connection")
    with pytest.raises(AdapterError, match="project_login_required"):
        gateway.request("/api/v1/project-agent/connection")
    assert len(calls) == 1


def test_workspace_scope_is_explicit_and_not_name_based(tmp_path):
    scope = profile(tmp_path)
    nested = tmp_path / "repo"
    nested.mkdir()
    scope.check_workspace(str(nested))
    with pytest.raises(AdapterError, match="workspace_out_of_scope"):
        scope.check_workspace(str(tmp_path.parent))


@pytest.mark.parametrize("returned_connection", ["conn-1", "conn-other"])
def test_context_read_requires_same_attested_connection(returned_connection):
    rows = [
        {
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "functions.exec",
                "call_id": "read1",
                "arguments": "await tools.durumari_project_handoff({})",
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "read1",
                "output": json.dumps(
                    {
                        "project_id": "project-1",
                        "connection_id": returned_connection,
                        "contexts": [{"body": "restricted source material"}],
                    }
                ),
            },
        },
        {
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Derived implementation"}],
            },
        },
    ]
    result = normalize_transcript(
        "codex", [json.dumps(r) for r in rows], "session", scope=("project-1", "conn-1")
    )
    if returned_connection == "conn-1":
        assert len(result.events) == 2
    else:
        assert not result.events
        assert "unattested_context_read_quarantined" in result.omissions


@pytest.mark.parametrize("client", ["codex", "claude"])
def test_in_flight_project_read_waits_without_permanent_quarantine(client):
    result_text = json.dumps({"project_id": "project-1", "connection_id": "conn-1"})
    if client == "codex":
        rows = [
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "durumari_project_handoff",
                    "call_id": "read1",
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call_output",
                    "call_id": "read1",
                    "output": result_text,
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": "Current handoff read",
                },
            },
        ]
    else:
        rows = [
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "durumari_project_handoff",
                            "id": "read1",
                            "input": {},
                        }
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [
                        {"type": "tool_result", "tool_use_id": "read1", "content": result_text}
                    ]
                },
            },
            {"type": "assistant", "message": {"content": "Current handoff read"}},
        ]
    pending = normalize_transcript(
        client, [json.dumps(rows[0])], "session", scope=("project-1", "conn-1")
    )
    assert not pending.events
    assert "context_read_pending" in pending.omissions
    assert "sensitive_session_quarantined" not in pending.omissions
    finished = normalize_transcript(
        client, [json.dumps(r) for r in rows], "session", scope=("project-1", "conn-1")
    )
    assert len(finished.events) == 2
    assert "context_read_pending" not in finished.omissions
    assert "sensitive_session_quarantined" not in finished.omissions


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_handoff_missing_server_attestation_fails_closed(project_store, tmp_path):
    from durumari_mcp.project_credentials import save_project_profile
    from durumari_mcp.project_gateway import ProjectGateway

    scope = profile(tmp_path)
    save_project_profile("team", scope)
    gateway = ProjectGateway("team", "claude", root=tmp_path / "queue")
    response = {
        "project_id": scope.project_id,
        "server_time": datetime.now(UTC).isoformat(),
        "from_cursor": 0,
        "through_cursor": 0,
        "next_cursor": 0,
        "has_more": False,
        "coverage": "bounded_project_handoff",
        "capture_status": "host_capture_not_verified",
        "decisions": [],
        "actions": [],
        "contexts": [],
        "records": [],
        "omissions": [],
        "work": [],
        "work_from_after": "",
        "work_next_after": None,
    }
    gateway.request = lambda *args, **kwargs: response
    with pytest.raises(AdapterError, match="invalid_handoff_response"):
        gateway.handoff()
    response["connection_id"] = "other-connection"
    with pytest.raises(AdapterError, match="handoff_connection_mismatch"):
        gateway.handoff()
    response["connection_id"] = scope.connection_id
    assert gateway.handoff()["connection_id"] == scope.connection_id


@pytest.mark.parametrize(
    "value",
    [
        '{"password": "synthetic_do_not_capture"}',
        json.dumps('{"password": "synthetic_do_not_capture"}'),
        {"progress": '{"api_key": "synthetic_do_not_capture"}'},
        "ghp_" + "a" * 36,
        "mpg_" + "b" * 43,
    ],
)
def test_sensitive_quoted_json_wrappers_and_token_prefixes_are_excluded(value):
    from durumari_mcp.capture import contains_sensitive

    assert contains_sensitive(value)


def test_refresh_persistence_failure_journal_blocks_new_process(
    project_store, monkeypatch, tmp_path
):
    from durumari_mcp import project_gateway
    from durumari_mcp.project_credentials import save_project_profile

    scope = profile(tmp_path).model_copy(
        update={
            "access_expires_at": datetime.now(UTC) - timedelta(seconds=1),
        }
    )
    save_project_profile("team", scope)
    gateway = project_gateway.ProjectGateway("team", "claude", root=tmp_path / "queue")
    calls = []

    def refresh(path, **kwargs):
        if path.endswith("/refresh/prepare"):
            raise AdapterError("project_refresh_not_supported")
        calls.append(path)
        return {
            "connection": connection(scope),
            "access_token": "mpa_" + "c" * 43,
            "refresh_token": "mpr_" + "d" * 43,
            "access_expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        }

    def failed_store(*args):
        raise AdapterError("protected_project_store_write_failed")

    gateway.transport.request = refresh
    monkeypatch.setattr(project_gateway, "save_project_profile", failed_store)
    with pytest.raises(AdapterError, match="protected_project_store_write_failed"):
        gateway.request("/api/v1/project-agent/connection")
    replacement = project_gateway.ProjectGateway("team", "claude", root=tmp_path / "queue")
    replacement.transport.request = refresh
    with pytest.raises(AdapterError, match="project_login_required"):
        replacement.request("/api/v1/project-agent/connection")
    assert calls == ["/api/v1/project-agent/refresh"]
    ciphertext = (tmp_path / "queue/team.credentials/refresh.enc").read_bytes()
    assert scope.refresh_token.get_secret_value().encode() not in ciphertext


