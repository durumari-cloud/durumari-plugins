"""Native direct targets never follow conversation selection or retarget retries."""

import pytest
from test_account_native import (
    PROJECT_A,
    PROJECT_B,
    REF_A,
    Transport,
    client,
    receipt,
)
from test_account_native import store as store_fixture

from durumari_mcp.credentials import AdapterError

store = store_fixture


class DirectTransport(Transport):
    def request(self, path, **kwargs):
        self.calls.append((path, kwargs))
        body = kwargs.get("body", {})
        if path == "/api/v2/agent/context/target":
            return {"project_id": body["target_project_id"]}
        if path == "/api/v2/agent/context/mutations":
            assert "project_ref" not in body
            if self.fail_write:
                raise AdapterError("project_delivery_unknown")
            return receipt(body["target_project_id"], body["request_key"])
        if path == "/api/v2/agent/context/operation":
            assert "project_ref" not in body
            return receipt(body["target_project_id"], body["request_key"])
        if path == "/api/v2/agent/projects/resolve":
            return {"project_id": PROJECT_B}
        return {"items": []}


def test_direct_read_write_and_recovery_keep_explicit_project(tmp_path, store):
    transport = DirectTransport()
    account = client(tmp_path, store, transport)
    view = account.direct_target(PROJECT_A)
    view.read("/search", q="Synthetic")
    assert transport.calls[-1][1]["target_project_id"] == PROJECT_A
    transport.fail_write = True
    body = {"title": "Direct", "body": "# Synthetic\nFixture"}
    assert view.mutate("POST", "", body, "same-key")["status"] == "unknown"
    account.targeted(REF_A).read()  # Navigation resolves to another project.
    assert view.operation("same-key")["project_id"] == PROJECT_A
    assert all("conversation" not in path for path, _ in transport.calls)
    assert (
        view._queue(PROJECT_A).directory != account.targeted(PROJECT_A)._queue(PROJECT_A).directory
    )


def test_server_cannot_redirect_direct_project(tmp_path, store):
    class Redirect(DirectTransport):
        def request(self, path, **kwargs):
            return {"project_id": PROJECT_B}

    account = client(tmp_path, store, Redirect())
    with pytest.raises(AdapterError, match="invalid_project_response"):
        account.direct_target(PROJECT_A).read()


def test_document_types_read_the_resolved_space_with_the_callers_own_target(tmp_path, store):
    transport = DirectTransport()
    account = client(tmp_path, store, transport)
    account.direct_target(PROJECT_A).document_types()
    path, kwargs = transport.calls[-1]
    assert path == f"/api/v2/projects/{PROJECT_A}/document-types"
    assert kwargs["target_project_id"] == PROJECT_A and "body" not in kwargs
    account.targeted(REF_A).document_types()
    path, kwargs = transport.calls[-1]
    assert path == f"/api/v2/projects/{PROJECT_B}/document-types"
    assert kwargs["project_ref"] == REF_A and "target_project_id" not in kwargs
    with pytest.raises(AdapterError, match="project_selection_required"):
        account.document_types()
