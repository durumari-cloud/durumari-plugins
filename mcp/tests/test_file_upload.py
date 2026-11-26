import base64
import json

import pytest
from mcp import Client
from test_cloud_protocol import SyntheticOperations

from durumari_mcp.cloud_protocol import create_context_server
from durumari_mcp.credentials import AdapterError
from durumari_mcp.file_upload import deliver, prepare


def test_local_file_is_read_without_mutation_and_path_is_not_transmitted(tmp_path):
    source = tmp_path / "한글.txt"
    source.write_bytes(b"synthetic notes")
    value = prepare({"request_key": "one", "file_path": str(source)}, "upload")
    assert value["filename"] == source.name and "file_path" not in value
    assert base64.b64decode(value["data_base64"]) == source.read_bytes()
    assert source.read_bytes() == b"synthetic notes"
    link = tmp_path / "link.txt"
    link.symlink_to(source)
    with pytest.raises(AdapterError, match="upload_file_unreadable"):
        prepare({"file_path": str(link)}, "upload")


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({}, "upload_input_missing"),
        ({"file_path": "relative.txt"}, "upload_path_not_absolute"),
        ({"file_path": "/missing-file", "data_base64": ""}, "upload_input_ambiguous"),
        ({"file_path": "/missing-durumari-test-file"}, "upload_file_not_found"),
    ],
)
def test_native_input_errors_are_distinct(body, code):
    with pytest.raises(AdapterError, match=code):
        prepare(body, "upload")


def test_lost_reply_is_unknown_not_stored_and_keeps_recovery_key():
    def lost(body):
        raise AdapterError("project_delivery_unknown")

    result = deliver(lost, {"request_key": "stable"})
    assert result["status"] == "unknown" and result["request_key"] == "stable"
    assert result["error"]["retryable"] is True


@pytest.mark.anyio
async def test_mcp_input_and_rejected_status_are_machine_readable():
    class Files(SyntheticOperations):
        def files(self, action, body):
            return {
                "status": "rejected",
                "request_key": body["request_key"],
                "error": {
                    "code": "upload_file_limit",
                    "retryable": False,
                    "next_action": "choose_smaller_file_no_lossy_fallback",
                },
            }

    async with Client(create_context_server(Files())) as client:
        result = await client.call_tool(
            "durumari_file_upload",
            {"request_key": "stable", "filename": "notes.txt", "data_base64": "eA=="},
        )
        assert result.is_error
        value = json.loads(result.content[0].text)
        assert value["error"]["code"] == "upload_file_limit"
        recovered = await client.call_tool("durumari_file_upload_status", {"request_key": "stable"})
        assert recovered.is_error


@pytest.mark.parametrize(
    "payload",
    [
        b"<html>limited</html>",
        json.dumps(
            {
                "error": {
                    "code": "upload_compressor_busy",
                    "details": {"retryable": True, "next_action": "wait_then_retry_same_key"},
                }
            }
        ).encode(),
    ],
)
def test_native_upload_preserves_structured_throttle_and_handles_ingress_html(payload):
    import io
    from urllib.error import HTTPError

    from durumari_mcp.project_gateway import ContextFailure, ProjectTransport

    class Limited:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 429, "limited", {}, io.BytesIO(payload))

    transport = ProjectTransport("https://durumari.example")
    transport.opener = Limited()
    if payload.startswith(b"<"):
        with pytest.raises(AdapterError, match="project_rate_limited"):
            transport.request("/api/v2/agent/file-uploads", body={})
    else:
        with pytest.raises(ContextFailure) as error:
            transport.request("/api/v2/agent/file-uploads", body={})
        assert error.value.error["code"] == "upload_compressor_busy"


def test_platform_file_flags_preserve_binary_bytes_without_unix_constants(tmp_path, monkeypatch):
    import os

    from durumari_mcp import file_upload

    source = tmp_path / "binary.txt"
    source.write_bytes(b"line\r\n\x1aend\r\n")
    monkeypatch.delattr(os, "O_NONBLOCK", raising=False)
    binary = 0x80000000
    monkeypatch.setattr(os, "O_BINARY", binary, raising=False)
    original = os.open
    flags_used = []

    def opened(path, flags):
        flags_used.append(flags)
        return original(path, flags & ~binary)

    monkeypatch.setattr(file_upload.os, "open", opened)
    prepared = prepare({"file_path": str(source)}, "upload")
    assert flags_used[0] & binary
    assert base64.b64decode(prepared["data_base64"]) == source.read_bytes()
