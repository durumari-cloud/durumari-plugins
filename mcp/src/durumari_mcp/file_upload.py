"""Native-only local file input. No byte payload or host path enters a receipt/log."""

import base64
import os
import stat
from collections.abc import Callable
from pathlib import Path
from typing import Any

from durumari_mcp.credentials import AdapterError

MAX_SOURCE_BYTES = 2_097_152


def prepare(body: dict[str, Any], action: str) -> dict[str, Any]:
    if action not in {"upload", "status"}:
        raise AdapterError("invalid_upload_operation")
    result = dict(body)
    path = result.pop("file_path", None)
    if path is not None:
        if action != "upload" or result.get("data_base64") is not None:
            raise AdapterError("upload_input_ambiguous")
        if not Path(path).is_absolute():
            raise AdapterError("upload_path_not_absolute")
        try:
            before = os.lstat(path)
            if stat.S_ISLNK(before.st_mode):
                raise AdapterError("upload_file_unreadable")
            if not stat.S_ISREG(before.st_mode):
                raise AdapterError("upload_not_regular_file")
            # Binary mode preserves CRLF/Ctrl-Z on Windows; Unix flags are optional.
            flags = (
                os.O_RDONLY
                | getattr(os, "O_NONBLOCK", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_BINARY", 0)
            )
            fd = os.open(path, flags)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino):
                    raise AdapterError("upload_file_changed")
                if not stat.S_ISREG(info.st_mode):
                    raise AdapterError("upload_not_regular_file")
                if info.st_size > MAX_SOURCE_BYTES:
                    raise AdapterError("upload_source_limit")
                data = stream.read(MAX_SOURCE_BYTES + 1)
            if len(data) > MAX_SOURCE_BYTES:
                raise AdapterError("upload_source_limit")
        except FileNotFoundError:
            raise AdapterError("upload_file_not_found") from None
        except PermissionError:
            raise AdapterError("upload_file_access_denied") from None
        except OSError:
            raise AdapterError("upload_file_unreadable") from None
        result.setdefault("filename", Path(path).name)
        result["data_base64"] = base64.b64encode(data).decode("ascii")
    if action == "upload" and not {"filename", "data_base64"} <= result.keys():
        raise AdapterError("upload_input_missing")
    return result


def deliver(
    send: Callable[[dict[str, Any]], dict[str, Any]], body: dict[str, Any]
) -> dict[str, Any]:
    try:
        return send(body)
    except AdapterError as error:
        if str(error) in {
            "project_delivery_unknown",
            "project_unavailable",
            "project_rate_limited",
        }:
            return {
                "status": "unknown",
                "request_key": body["request_key"],
                "error": {
                    "code": str(error),
                    "retryable": True,
                    "next_action": "check_upload_status_then_retry_same_key",
                },
            }
        raise
