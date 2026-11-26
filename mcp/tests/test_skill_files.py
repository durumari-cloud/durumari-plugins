"""External skill layout and binary assets preserve bytes without executable paths."""

import base64
import hashlib
import json

import pytest

from durumari_mcp.credentials import AdapterError
from durumari_mcp.skill_files import encode_file, file_bytes, safe_file_path, validated_files

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aH8sAAAAASUVORK5CYII="
)


@pytest.mark.parametrize(
    "path",
    [
        "SKILL.md",
        "LICENSE.txt",
        "AGENTS.md",
        "metadata.json",
        "agents/analyzer.md",
        "rules/server.md",
        "eval-viewer/viewer.html",
        "examples/test.py",
    ],
)
def test_external_safe_paths_are_preserved(path):
    safe_file_path(path)
    assert file_bytes(path, encode_file(path, b"content\r\n")) == b"content\r\n"


@pytest.mark.parametrize(
    "path",
    [
        "../outside",
        "/absolute",
        "a/../b",
        ".git/config",
        "a/.hidden",
        "C:/file",
        "a\\b",
        "CON",
        "dir/nul.txt",
        "a//b",
        "a/",
        "",
    ],
)
def test_unsafe_portable_paths_remain_denied(path):
    with pytest.raises(AdapterError, match="invalid_skill_file"):
        safe_file_path(path)


def test_image_roundtrip_has_an_explicit_bounded_encoding():
    value = encode_file("assets/icon.png", PNG)
    assert value == {
        "encoding": "base64",
        "media_type": "image/png",
        "data": base64.b64encode(PNG).decode(),
    }
    assert file_bytes("assets/icon.png", json.loads(json.dumps(value))) == PNG
    assert (
        hashlib.sha256(file_bytes("assets/icon.png", value)).digest()
        == hashlib.sha256(PNG).digest()
    )


@pytest.mark.parametrize(
    "path,data",
    [
        ("scripts/icon.png", PNG),
        ("assets/icon.jpg", PNG),
        ("SKILL.md", PNG),
        ("assets/program.exe", b"\x00MZ"),
        ("assets/icon.png", b"\x00not-an-image"),
        ("SKILL.md", b"\0bad"),
    ],
)
def test_binary_is_limited_to_recognized_image_assets(path, data):
    with pytest.raises(AdapterError, match="invalid_skill_file"):
        encode_file(path, data)


@pytest.mark.parametrize(
    "patch",
    [
        {"encoding": "hex"},
        {"media_type": "text/html"},
        {"data": "not base64"},
        {"data": base64.b64encode(PNG).decode() + "\n"},
        {"extra": "field"},
    ],
)
def test_binary_metadata_and_bytes_are_validated(patch):
    value = {**encode_file("assets/icon.png", PNG), **patch}
    with pytest.raises(AdapterError, match="invalid_skill_file"):
        file_bytes("assets/icon.png", value)


def test_each_file_remains_bounded():
    with pytest.raises(AdapterError, match="invalid_skill_file"):
        encode_file("LICENSE.txt", b"x" * 128_001)


@pytest.mark.parametrize(
    "extra",
    [
        {"AGENTS.md": "file", "agents.MD/rule": "collision"},
        {"a": "file", "A/nested.txt": "collision"},
        {"SKILL.md/child": "collision"},
    ],
)
def test_file_directory_collisions_remain_denied_on_case_insensitive_hosts(extra):
    with pytest.raises(AdapterError, match="invalid_skill_file"):
        validated_files({"SKILL.md": "text", **extra})


def test_global_limits_use_decoded_bytes_and_file_count():
    with pytest.raises(AdapterError, match="invalid_skill_file"):
        validated_files({"SKILL.md": "text", **{f"assets/{i}.txt": "x" for i in range(100)}})
    with pytest.raises(AdapterError, match="invalid_skill_file"):
        validated_files(
            {"SKILL.md": "text", **{f"assets/{i}.txt": "x" * 128_000 for i in range(4)}}
        )
