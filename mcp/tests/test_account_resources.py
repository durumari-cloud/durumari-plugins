"""A required update must not permanently reject the original immutable write."""

import pytest
from test_account_native import REF_A, Transport, client
from test_account_native import store as store_fixture

from durumari_mcp.project_gateway import ContextFailure

store = store_fixture


def test_update_required_preserves_exact_queued_target_key_and_body(tmp_path, store):
    class UpdateRequiredOnce(Transport):
        def __init__(self):
            super().__init__()
            self.update_required = True
            self.deliveries = []

        def request(self, path, **kwargs):
            if path == "/api/v2/agent/context/mutations":
                self.deliveries.append(kwargs["body"])
                if self.update_required:
                    raise ContextFailure(426, {"code": "client_update_required"})
            return super().request(path, **kwargs)

    transport = UpdateRequiredOnce()
    account = client(tmp_path, store, transport)
    target = account.targeted(REF_A)
    body = {"title": "Synthetic update retry", "body": "Original queued source"}
    with pytest.raises(ContextFailure) as error:
        target.mutate("POST", "", body, "same-original-key")
    assert error.value.status == 426
    transport.update_required = False
    receipt = account.targeted(REF_A).mutate("POST", "", body, "same-original-key")
    assert receipt["status"] == "stored"
    assert len(transport.deliveries) == 2
    assert (
        transport.deliveries[0]
        == transport.deliveries[1]
        == {
            "project_ref": REF_A,
            "request_key": "same-original-key",
            "action": "create",
            "body": body,
        }
    )
