"""A server without a published native release lets the installed runtime start."""

import pytest

from durumari_mcp.update_policy import PolicyError, parse_policy

ORIGIN = "https://durumari.kr"


def release(latest, minimum, required=None):
    return {"context_api": 2, "native": {
        "latest_version": latest, "supported_min_version": minimum,
        "required_native_version": required, "update_url": "/downloads",
    }}


def test_unpublished_release_requires_no_update():
    policy = parse_policy(release(None, None), ORIGIN)
    assert policy.latest_version is None and policy.required_native_version is None
    assert policy.needs_update("0.4.18") is False
    assert policy.receipt()["latest_version"] is None


@pytest.mark.parametrize(
    "value",
    [release(None, None, "0.4.18"), release("0.4.18", None), release(None, "0.4.17")],
)
def test_partial_or_required_without_release_is_invalid(value):
    with pytest.raises(PolicyError, match="native_update_policy_invalid"):
        parse_policy(value, ORIGIN)


def test_published_release_still_enforces_required_version():
    policy = parse_policy(release("0.4.19", "0.4.18", "0.4.19"), ORIGIN)
    assert policy.needs_update("0.4.18") is True
    assert policy.needs_update("0.4.19") is False
