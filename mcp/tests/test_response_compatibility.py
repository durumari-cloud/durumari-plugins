"""Released clients accept additive server response and receipt fields and still check the rest."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from test_account_skill_gateway import REF_A, REF_B, ROOT, SKILL, Account
from test_continuity import connection, profile

from durumari_mcp.account_credentials import profile_from_response
from durumari_mcp.account_skill_gateway import AccountSkillGateway
from durumari_mcp.context_client import ContextClient
from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_gateway import validate_connection, validate_receipt
from durumari_mcp.project_rotation import CommitReceipt, replacement

ADDED = {"scope_kind": "project", "future_field": {"nested": [1, 2]}}


def test_connection_with_added_server_fields_is_accepted(tmp_path):
    scope = profile(tmp_path)
    row = validate_connection({**connection(scope), **ADDED}, scope)
    assert row.id == scope.connection_id
    assert not hasattr(row, "scope_kind")
    assert "future_field" not in row.model_dump()


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("id", "conn-2", "project_connection_scope_changed"),
        ("project_id", "project-2", "project_connection_scope_changed"),
        ("source_id", "source-2", "project_connection_scope_changed"),
        ("actor_id", "actor-2", "project_connection_scope_changed"),
        ("host", "codex", "project_connection_scope_changed"),
        ("provider", "openai", "project_provider_mismatch"),
        ("state", "revoked", "project_connection_unavailable"),
        (
            "expires_at",
            (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            "project_connection_unavailable",
        ),
        ("actions", ["read", "admin"], "invalid_project_connection"),
        ("host", "unknown", "invalid_project_connection"),
        ("revision", 0, "invalid_project_connection"),
        ("capture_status", "verified", "invalid_project_connection"),
    ],
)
def test_declared_connection_fields_are_still_checked(tmp_path, field, value, code):
    scope = profile(tmp_path)
    with pytest.raises(AdapterError, match=code):
        validate_connection({**connection(scope), **ADDED, field: value}, scope)


@pytest.mark.parametrize("missing", ["id", "project_id", "actions", "state", "expires_at"])
def test_missing_declared_connection_field_is_rejected(tmp_path, missing):
    scope = profile(tmp_path)
    row = {**connection(scope), **ADDED}
    del row[missing]
    with pytest.raises(AdapterError, match="invalid_project_connection"):
        validate_connection(row, scope)


def prepared(scope, **changes):
    return {
        "connection": {**connection(scope), **ADDED},
        "access_token": "mpa_" + "c" * 43,
        "refresh_token": "mpr_" + "d" * 43,
        "access_expires_at": (datetime.now(UTC) + timedelta(minutes=15)).isoformat(),
        "future_field": "ignored",
        **changes,
    }


def test_refresh_prepare_with_added_fields_rotates_credentials(tmp_path):
    scope = profile(tmp_path)
    candidate, _ = replacement(scope, prepared(scope))
    assert candidate.refresh_token.get_secret_value() == "mpr_" + "d" * 43


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        ({"access_token": "plain-token"}, "invalid_project_response"),
        ({"access_expires_at": "2026-10-20T00:00:00"}, "invalid_project_response"),
        ({"refresh_token": "mpa_" + "d" * 43}, "invalid_project_credential"),
    ],
)
def test_refresh_prepare_still_checks_token_fields(tmp_path, changes, code):
    scope = profile(tmp_path)
    with pytest.raises(AdapterError, match=code):
        replacement(scope, prepared(scope, **changes))


def test_refresh_prepare_still_checks_the_connection_scope(tmp_path):
    scope = profile(tmp_path)
    changed = {**connection(scope), **ADDED, "project_id": "project-2"}
    with pytest.raises(AdapterError, match="project_connection_scope_changed"):
        replacement(scope, prepared(scope, connection=changed))


def test_commit_receipt_ignores_added_fields_but_checks_status():
    base = {
        "status": "committed",
        "connection": {},
        "access_expires_at": datetime.now(UTC).isoformat(),
        "future_field": True,
    }
    assert CommitReceipt.model_validate(base).status == "committed"
    with pytest.raises(ValueError):
        CommitReceipt.model_validate({**base, "status": "accepted"})


ACCOUNT = "11111111-1111-1111-1111-111111111111"
ACTOR = "22222222-2222-2222-2222-222222222222"


def account_reply(connection_changes=None, tokens_changes=None):
    return {
        "status": "approved",
        "connection": {
            "id": ACCOUNT,
            "actor_id": ACTOR,
            "client": "codex",
            "provider_family": "openai",
            "scope_kind": "account",
            "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "future_field": "ignored",
            **(connection_changes or {}),
        },
        "tokens": {
            "access_token": "mpa_" + "A" * 43,
            "refresh_token": "mpr_" + "B" * 43,
            "access_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
            **(tokens_changes or {}),
        },
    }


def account_profile(reply):
    return profile_from_response(
        reply, "https://durumari.kr", "codex", Fernet.generate_key().decode()
    )


def test_account_connection_with_added_fields_is_accepted():
    assert account_profile(account_reply()).connection_id == ACCOUNT


@pytest.mark.parametrize(
    ("connection_changes", "tokens_changes"),
    [
        ({"scope_kind": "project"}, None),
        ({"id": "not-a-uuid"}, None),
        ({"actor_id": "not-a-uuid"}, None),
        ({"client": "claude"}, None),
        (None, {"extra_token": "mpa_" + "C" * 43}),
    ],
)
def test_account_reply_still_checks_scope_identity_and_token_set(
    connection_changes, tokens_changes
):
    with pytest.raises(AdapterError, match="invalid_account_response"):
        account_profile(account_reply(connection_changes, tokens_changes))


EVENT = {"event_id": "event-1", "session_id": "session-1", "sequence": 3}


def event_receipt(**changes):
    return {
        "id": "receipt-1",
        **EVENT,
        "received_at": datetime.now(UTC).isoformat(),
        "cursor": 7,
        "replayed": False,
        "future_field": "ignored",
        **changes,
    }


def test_event_receipt_ignores_added_fields():
    assert "future_field" not in validate_receipt(event_receipt(), EVENT)


@pytest.mark.parametrize(
    "changes",
    [{"event_id": "event-2"}, {"session_id": "session-2"}, {"sequence": 4}, {"replayed": "no"}],
)
def test_event_receipt_still_matches_the_sent_event(changes):
    with pytest.raises(AdapterError, match="invalid_event_receipt"):
        validate_receipt(event_receipt(**changes), EVENT)


def context_receipt(client, **changes):
    value = {
        "status": "stored",
        "project_id": "project-1",
        "context_id": "context-1",
        "revision": 2,
        "request_key": "save-1",
        "replayed": False,
        "future_field": "ignored",
        **changes,
    }
    return ContextClient.receipt(client, value, "save-1")


def test_context_receipt_ignores_added_fields_but_checks_project_and_key():
    client = SimpleNamespace(
        gateway=SimpleNamespace(profile=SimpleNamespace(project_id="project-1"))
    )
    assert context_receipt(client)["context_id"] == "context-1"
    for changes in ({"project_id": "project-2"}, {"request_key": "save-2"}, {"status": "queued"}):
        with pytest.raises(AdapterError, match="invalid_context_receipt"):
            context_receipt(client, **changes)


def skill_edit(account):
    return AccountSkillGateway(account, REF_A).request(
        ROOT + "/" + SKILL + "/metadata",
        {"base_revision": 1, "label": "Team", "tags": []},
        method="PATCH",
        request_key="edit",
    )


@pytest.mark.parametrize(
    ("added", "accepted"),
    [
        ({"future_field": "ignored"}, True),
        ({"project_ref": REF_B}, False),
        ({"files": {"SKILL.md": "bundle"}}, False),
        ({"content": "bundle"}, False),
    ],
)
def test_skill_receipt_ignores_added_fields_but_rejects_scope_change_and_content(
    tmp_path, added, accepted
):
    account = Account(tmp_path)
    original = account.request
    account.request = lambda path, **kwargs: {**original(path, **kwargs), **added}
    if accepted:
        assert "future_field" not in skill_edit(account)
    else:
        with pytest.raises(AdapterError, match="invalid_skill_receipt"):
            skill_edit(account)
