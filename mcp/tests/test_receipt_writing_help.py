"""Writing-rule help on Context receipts."""

from durumari_mcp.context_client import Receipt

BASE = {
    "status": "stored",
    "project_id": "project-1",
    "context_id": "context-1",
    "revision": 1,
    "request_key": "APP-1-result",
    "replayed": False,
}


def test_receipts_from_older_servers_keep_their_shape():
    assert Receipt.model_validate(BASE).model_dump(exclude_none=True) == BASE


def test_added_tags_and_warnings_reach_the_agent():
    value = {
        **BASE,
        "added_tags": ["APP-12"],
        "warnings": [{"code": "summary_missing", "message": "요약이 비어 있습니다."}],
    }
    assert Receipt.model_validate(value).model_dump(exclude_none=True) == value


def test_malformed_help_never_rejects_a_stored_write():
    value = {
        **BASE,
        "added_tags": ["APP-12", 7, "bad tag with spaces", "x" * 41, "APP-13\n"],
        "warnings": [
            {"code": "title_length", "message": "m" * 400},
            {"code": "Not A Code", "message": "dropped"},
            {"code": "summary_missing\n", "message": "dropped"},
            "not an object",
        ],
    }
    saved = Receipt.model_validate(value).model_dump(exclude_none=True)
    assert saved["added_tags"] == ["APP-12"]
    assert saved["warnings"] == [{"code": "title_length", "message": "m" * 300}]
    assert Receipt.model_validate({**BASE, "warnings": "oops"}).warnings is None
