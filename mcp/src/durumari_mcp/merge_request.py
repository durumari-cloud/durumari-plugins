"""Pinned merge request data shared by MCP and REST. No credentials or remote execution."""

import argparse
import json
import re
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MergeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    url: str = Field(min_length=1, max_length=2000)
    iid: int = Field(ge=1, strict=True)
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    target_branch: str = Field(min_length=1, max_length=200)
    action: Literal["merge"] = "merge"

    @model_validator(mode="after")
    def valid(self) -> "MergeRequest":
        url = urlsplit(self.url)
        _ = url.port  # Reject malformed ports before storing a clickable URL.
        path = re.fullmatch(r"/([^\s%?#]+)/-/merge_requests/([1-9][0-9]*)", url.path)
        if (
            url.scheme != "https"
            or not url.hostname
            or "@" in url.netloc
            or url.username
            or url.password
            or url.query
            or url.fragment
            or any(ord(c) <= 32 or ord(c) == 127 or c in '<>"`' for c in self.url)
            or "\\" in self.url
            or not path
            or int(path[2]) != self.iid
            or any(p in {"", ".", ".."} for p in path[1].split("/"))
        ):
            raise ValueError("invalid_merge_request_url")
        # Validate an ordinary Git branch, never an option, ref expression or command.
        branch = self.target_branch
        if (
            branch.startswith(("-", "/"))
            or branch.endswith(("/", "."))
            or ".." in branch
            or "@{" in branch
            or branch == "@"
            or re.search(r"[\x00-\x20\x7f~^:?*\[\\]", branch)
            or any(not p or p.startswith(".") or p.endswith(".lock") for p in branch.split("/"))
        ):
            raise ValueError("invalid_merge_target_branch")
        return self


def require_confirmed_merge(item: dict[str, Any], live_mr: dict[str, Any]) -> MergeRequest:
    """Compare freshly read request and GitLab evidence before running merge_guard.

    This comparison neither authenticates supplied JSON nor substitutes for the
    guard's current CI, approval, branch-protection and fast-forward checks.
    """
    decision = item.get("decision") or {}
    if (
        decision.get("outcome") != "confirmed"
        or decision.get("round") != item.get("round")
        or not decision.get("context_id")
        or not decision.get("context_revision")
        or decision.get("via") == "agent"
        or decision.get("user_instruction") is not None
    ):
        raise ValueError("human_decision_required")
    target = MergeRequest.model_validate(item.get("merge_request"))
    if (
        live_mr.get("web_url") != target.url
        or live_mr.get("iid") != target.iid
        or live_mr.get("sha") != target.head_sha
        or live_mr.get("target_branch") != target.target_branch
        or live_mr.get("state") != "opened"
        or live_mr.get("draft") is not False
    ):
        raise ValueError("merge_target_changed")
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare fresh request and MR JSON; never merge.")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--item", required=True)
    parser.add_argument("--mr", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text())
        if request.get("kind") != "decision" or request.get("state") == "cancelled":
            raise ValueError("human_decision_required")
        item = next(item for item in request["items"] if item["id"] == args.item)
        target = require_confirmed_merge(item, json.loads(args.mr.read_text()))
    except (OSError, ValueError, KeyError, TypeError, StopIteration):
        parser.exit(
            1, "Merge evidence unavailable, unapproved or changed; reread and leave a note.\n"
        )
    print(target.model_dump_json())


if __name__ == "__main__":
    main()
