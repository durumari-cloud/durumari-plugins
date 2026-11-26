"""Portable skill files. Binary support is explicit image data, never executable code."""

import base64
import binascii
import re
from typing import Literal, TypedDict

from durumari_mcp.credentials import AdapterError

type ImageMediaType = Literal["image/png", "image/jpeg", "image/gif", "image/webp"]


class BinaryAsset(TypedDict):
    encoding: Literal["base64"]
    media_type: ImageMediaType
    data: str


type SkillFile = str | BinaryAsset
MAX_FILE_BYTES = 128_000
MAX_TOTAL_BYTES = 512_000
RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(10)),
    *(f"LPT{i}" for i in range(10)),
}


def safe_file_path(path: object) -> None:
    if not isinstance(path, str) or not 1 <= len(path) <= 200:
        raise AdapterError("invalid_skill_file")
    if any(
        not re.fullmatch(r"[A-Za-z0-9_.-]+", part)
        or part.startswith(".")
        or part.endswith(".")
        or part.split(".")[0].upper() in RESERVED
        for part in path.split("/")
    ):
        raise AdapterError("invalid_skill_file")


def image_media_type(path: str, data: bytes) -> ImageMediaType:
    if not path.startswith("assets/"):
        raise AdapterError("invalid_skill_file")
    suffix = path.rsplit(".", 1)[-1].lower()
    if suffix == "png" and data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if suffix in {"jpg", "jpeg"} and data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if suffix == "gif" and data[:6] in {b"GIF87a", b"GIF89a"}:
        return "image/gif"
    if suffix == "webp" and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    raise AdapterError("invalid_skill_file")


def file_bytes(path: str, value: object) -> bytes:
    safe_file_path(path)
    try:
        if isinstance(value, str):
            if any(ord(c) < 32 and c not in "\r\n\t" for c in value):
                raise ValueError
            data = value.encode("utf-8")
        elif isinstance(value, dict) and set(value) == {"encoding", "media_type", "data"}:
            encoded = value["data"]
            if (
                value["encoding"] != "base64"
                or not isinstance(encoded, str)
                or len(encoded) > 170_668
            ):
                raise ValueError
            data = base64.b64decode(encoded, validate=True)
            if (
                base64.b64encode(data).decode("ascii") != encoded
                or image_media_type(path, data) != value["media_type"]
            ):
                raise ValueError
        else:
            raise ValueError
        if len(data) > MAX_FILE_BYTES:
            raise ValueError
        return data
    except (ValueError, UnicodeError, binascii.Error):
        raise AdapterError("invalid_skill_file") from None


def encode_file(path: str, data: bytes) -> SkillFile:
    safe_file_path(path)
    if len(data) > MAX_FILE_BYTES:
        raise AdapterError("invalid_skill_file")
    try:
        text = data.decode("utf-8")
        if any(ord(c) < 32 and c not in "\r\n\t" for c in text):
            raise ValueError
        return text
    except (ValueError, UnicodeError):
        value: BinaryAsset = {
            "encoding": "base64",
            "media_type": image_media_type(path, data),
            "data": base64.b64encode(data).decode("ascii"),
        }
        file_bytes(path, value)
        return value


def validated_files(files: object) -> dict[str, bytes]:
    if (
        not isinstance(files, dict)
        or not 1 <= len(files) <= 100
        or not isinstance(files.get("SKILL.md"), str)
    ):
        raise AdapterError("invalid_skill_file")
    raw = {path: file_bytes(path, value) for path, value in files.items()}
    folded = {path.lower() for path in raw}
    if len(folded) != len(raw) or sum(map(len, raw.values())) > MAX_TOTAL_BYTES:
        raise AdapterError("invalid_skill_file")
    if any(
        "/".join(path.split("/")[:index]) in folded
        for path in folded
        for index in range(1, len(path.split("/")))
    ):
        raise AdapterError("invalid_skill_file")
    return raw
