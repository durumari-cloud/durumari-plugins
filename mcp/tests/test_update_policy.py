"""Only an explicit, validated native policy may require an automatic update."""

import io
import json
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from durumari_mcp.project_gateway import ContextFailure, ProjectTransport
from durumari_mcp.update_policy import PolicyError, fetch_policy, parse_policy


def metadata(required=None):
    return {
        "native": {
            "latest_version": "0.4.17",
            "supported_min_version": "0.4.15",
            "required_native_version": required,
            "update_url": "/downloads",
        }
    }


def test_advisory_and_required_versions_are_distinct():
    advisory = parse_policy(metadata(), "https://durumari.kr")
    assert not advisory.needs_update("0.4.16")
    required = parse_policy(metadata("0.4.17"), "https://durumari.kr")
    assert required.needs_update("0.4.16") and not required.needs_update("0.4.17")
    assert required.update_url == "https://durumari.kr/downloads"


def test_recognized_legacy_advisory_is_explicit_and_does_not_enforce_previous_minimum():
    policy = parse_policy(
        {
            "context_api": 2,
            "client": {
                "latest_version": "0.4.17",
                "minimum_version": "0.4.16",
                "status": "update_required",
            },
        },
        "https://durumari.kr",
    )
    assert policy.capability == "legacy_advisory"
    assert not policy.needs_update("0.4.15")


@pytest.mark.parametrize("change", ["missing", "future", "url", "malformed", "unknown"])
def test_unknown_or_malformed_policy_never_silently_allows_launch(change):
    value = metadata("0.4.17")
    if change == "missing":
        value["native"].pop("required_native_version")
    elif change == "future":
        value["native"]["required_native_version"] = "0.4.18"
    elif change == "url":
        value["native"]["update_url"] = "https://attacker.invalid/install"
    elif change == "malformed":
        value["native"] = None
    else:
        value = {"context_api": 2}
    with pytest.raises(PolicyError):
        parse_policy(value, "https://durumari.kr")


def test_unknown_installed_version_never_bypasses_required_policy():
    with pytest.raises(PolicyError, match="native_runtime_version_unknown"):
        parse_policy(metadata("0.4.17"), "https://durumari.kr").needs_update("0+unknown")


def test_offline_policy_requires_recovery_instead_of_assuming_no_mandatory_update():
    def offline(url, limit):
        raise OSError("private network failure")

    with pytest.raises(PolicyError, match="^native_update_policy_unavailable$"):
        fetch_policy("https://durumari.kr", offline)


def test_transport_preserves_update_required_error_and_public_recovery_details():
    details = {"required_version": "0.4.17", "latest_version": "0.4.17", "update_url": "/downloads"}

    def rejected(request, timeout):
        raise HTTPError(
            request.full_url,
            426,
            "Upgrade Required",
            {},
            io.BytesIO(
                json.dumps(
                    {"error": {"code": "client_update_required", "details": details}}
                ).encode()
            ),
        )

    transport = ProjectTransport("https://durumari.kr")
    transport.opener = SimpleNamespace(open=rejected)
    with pytest.raises(ContextFailure) as failure:
        transport.request("/api/v2/agent/projects/start", body={"request_key": "conversation"})
    assert failure.value.status == 426
    assert failure.value.error == {"code": "client_update_required", "details": details}


def test_required_upgrade_keeps_legacy_write_retryable_with_original_key_and_body(
    tmp_path, monkeypatch
):
    from test_continuity import profile

    from durumari_mcp.context_client import ContextClient

    scope = profile(tmp_path)
    attempts = []

    def request(path, body, **kwargs):
        attempts.append((path, body, kwargs["idempotency_key"]))
        if len(attempts) == 1:
            raise ContextFailure(426, {"code": "client_update_required"})
        return {
            "status": "stored",
            "project_id": scope.project_id,
            "context_id": "document",
            "revision": 1,
            "request_key": kwargs["idempotency_key"],
            "replayed": False,
        }

    monkeypatch.chdir(tmp_path)
    gateway = SimpleNamespace(
        profile=scope, root=tmp_path / "state", name="team", context_request=request
    )
    client = ContextClient(gateway, "synthetic-chat")
    body = {"title": "Original", "body": "Preserve the same request"}
    blocked = client.mutate("POST", "", body, "same-key")
    assert blocked["status"] == "pending"
    assert blocked["error"] == {"code": "client_update_required"}
    assert client.queue.pending(None) == 1 and client.queue.rejection("same-key") is None
    assert client.mutate("POST", "", body, "same-key")["status"] == "stored"
    assert attempts[0] == attempts[1]
