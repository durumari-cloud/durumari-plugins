"""A release's sha256 covers its requirements as stored, not the server's normalized view."""

import pytest

from durumari_mcp.credentials import AdapterError
from durumari_mcp.project_skills import bundle_digest, validate_bundle

SKILL = "---\nname: durumari-ops\ndescription: Dedicated operations\n---\n"


def served_release():
    """A 0.2.x-like release as the server serves it: legacy rows hashed as stored."""
    stored = [{"name": "ops_api", "kind": "api_token", "required": True}]
    row = {
        "id": "skill",
        "project_id": "project",
        "name": "durumari-ops",
        "version": "0.2.0",
        "requirements": stored,
        "files": {"SKILL.md": SKILL},
    }
    row["sha256"] = bundle_digest(row)
    row["stored_requirements"] = stored
    row["requirements"] = [
        {
            "name": "ops_api",
            "kind": "text",
            "required": True,
            "description": None,
            "env": "OPS_API",
            "legacy_kind": "api_token",
        }
    ]
    row["device_keys"] = ["ssh"]
    return row


def test_published_legacy_bundle_verifies_over_stored_requirements():
    validate_bundle(served_release())


def test_digest_still_rejects_tampered_stored_requirements():
    row = served_release()
    row["stored_requirements"] = [{"name": "ops_api", "kind": "text", "required": True}]
    with pytest.raises(AdapterError, match="invalid_skill_bundle"):
        validate_bundle(row)


@pytest.mark.parametrize(
    "change", [{"env": "EVIL"}, {"name": "other"}, {"required": False}, {"kind": "account"}]
)
def test_tampered_normalized_requirements_are_rejected(change):
    row = served_release()
    row["requirements"][0].update(change)
    with pytest.raises(AdapterError, match="invalid_skill_bundle"):
        validate_bundle(row)


def test_digest_still_rejects_tampered_files():
    row = served_release()
    row["files"]["SKILL.md"] += "changed\n"
    with pytest.raises(AdapterError, match="invalid_skill_bundle"):
        validate_bundle(row)
