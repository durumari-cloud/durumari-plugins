"""Assemble the private Chat plugin around an already registered ChatGPT MCP app."""

import argparse
import hashlib
import io
import json
import re
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[1] / "hosts/chat"
SOURCE_FILES = ("plugin.json", "skills/durumari-context/SKILL.md")


def source_bytes(source: Path, relative: str) -> bytes:
    path = source
    if path.is_symlink():
        raise ValueError("linked_plugin_source")
    for part in Path(relative).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("linked_plugin_source")
    return path.read_bytes()


def json_bytes(item: dict) -> bytes:
    return (json.dumps(item, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def assemble(app_id: str, output: Path, *, source: Path = SOURCE) -> dict:
    """Keep account-specific app references in the archive, never in source files."""
    if not re.fullmatch(r"asdk_app_[A-Za-z0-9][A-Za-z0-9_-]*", app_id):
        raise ValueError("invalid_registered_app_id")
    if not output.is_absolute():
        raise ValueError("absolute_output_required")
    if output.resolve().is_relative_to(source.resolve()):
        raise ValueError("output_inside_plugin_source")
    files = {name: source_bytes(source, name) for name in SOURCE_FILES}
    try:
        plugin = json.loads(files["plugin.json"])
        if not isinstance(plugin, dict) or plugin["name"] != "durumari":
            raise ValueError
        openai = plugin["extensions"]["com.openai"]
        if not isinstance(openai, dict):
            raise TypeError
        openai["apps"] = "./.app.json"
    except (ValueError, KeyError, TypeError):
        raise ValueError("invalid_chat_plugin_manifest") from None
    files["plugin.json"] = json_bytes(plugin)
    files[".app.json"] = json_bytes({"apps": {"durumari": {"id": app_id}}})
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.compress_type = zipfile.ZIP_DEFLATED
            entry.external_attr = 0o100644 << 16
            archive.writestr(entry, data)
    payload = stream.getvalue()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        destination.write(payload)
    return {
        "name": plugin["name"],
        "version": plugin["version"],
        "output": str(output),
        "files": sorted(files),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "size": len(payload),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        receipt = assemble(args.app_id, args.output)
    except ValueError as error:
        parser.error(str(error))
    except OSError:
        parser.error("chat_plugin_filesystem_error")
    else:
        print(json.dumps(receipt, ensure_ascii=False))


if __name__ == "__main__":
    main()
