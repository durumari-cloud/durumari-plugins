"""Build the portable OpenAI draft without account-specific app bindings."""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import io
import json
import zipfile
from pathlib import Path

SOURCE = Path(__file__).resolve().parent
FILES = ("plugin.json", "mcp.json", "skills/durumari-context/SKILL.md", "assets/icon.png.b64")


def _manifest_fields(value: object, allowed: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError("invalid_manifest")
    return value


def build(output: Path) -> dict:
    if not output.is_absolute() or output.resolve().is_relative_to(SOURCE.resolve()):
        raise ValueError("output_must_be_absolute_and_outside_source")
    files = {}
    for name in FILES:
        path = SOURCE
        for part in Path(name).parts:
            path = path / part
            if path.is_symlink():
                raise ValueError("linked_source_not_allowed")
        files[name] = path.read_bytes()
    encoded_icon = b"".join(files.pop("assets/icon.png.b64").split())
    try:
        icon_png = base64.b64decode(encoded_icon, validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("invalid_icon_png") from None
    if not icon_png.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("invalid_icon_png")
    files["assets/icon.png"] = icon_png
    manifest = json.loads(files["plugin.json"])
    server = json.loads(files["mcp.json"])
    if not isinstance(manifest, dict) or not manifest.get("name") or not manifest.get("version"):
        raise ValueError("invalid_manifest")
    extensions = manifest.get("extensions", {})
    if not isinstance(extensions, dict) or not isinstance(extensions.get("com.openai", {}), dict):
        raise ValueError("invalid_manifest")  # noqa: TRY004
    if manifest.get("apps") is not None or extensions.get("com.openai", {}).get("apps") is not None:
        raise ValueError("app_references_not_allowed")
    if manifest.get("hooks") is not None:
        raise ValueError("lifecycle_hooks_not_allowed")
    _manifest_fields(
        manifest, {"$schema", "name", "version", "description", "author", "extensions"}
    )
    author = _manifest_fields(manifest.get("author", {}), {"name"})
    _manifest_fields(extensions, {"com.openai"})
    openai = _manifest_fields(
        extensions.get("com.openai", {}), {"review", "publication", "interface"}
    )
    review = _manifest_fields(openai.get("review", {}), {"commerce", "test_cases", "demo_recording_url"})
    publication = _manifest_fields(
        openai.get("publication", {}), {"countries", "release_notes", "translations"}
    )
    interface = _manifest_fields(openai.get("interface", {}), {
        "displayName", "shortDescription", "longDescription", "developerName", "category",
        "logo", "composerIcon", "defaultPrompt", "capabilities",
        "websiteURL", "supportURL", "privacyPolicyURL", "termsOfServiceURL",
    })
    for fields in (manifest, author, publication, interface):
        for name, value in fields.items():
            if name in {"author", "extensions", "translations"}:
                continue
            if name in {"countries", "defaultPrompt", "capabilities"}:
                if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                    raise ValueError("invalid_manifest")
            elif not isinstance(value, str):
                raise ValueError("invalid_manifest")
    translations = publication.get("translations", {})
    if not isinstance(translations, dict):
        raise ValueError("invalid_manifest")  # noqa: TRY004 - package schema validation
    for locale, translated in translations.items():
        if not isinstance(locale, str) or not locale.strip():
            raise ValueError("invalid_manifest")
        translated = _manifest_fields(translated, {"subtitle", "description"})
        for name, value in translated.items():
            if not isinstance(value, str) or not value.strip():
                raise ValueError("invalid_manifest")
            if name == "subtitle" and (len(value) > 30 or "\n" in value):
                raise ValueError("invalid_manifest")
            if name == "description" and len(value) > 4000:
                raise ValueError("invalid_manifest")
    if "commerce" in review and type(review["commerce"]) is not bool:
        raise ValueError("invalid_manifest")
    if "demo_recording_url" in review:
        from urllib.parse import urlparse

        demo_url = review["demo_recording_url"]
        parsed_demo_url = urlparse(demo_url) if isinstance(demo_url, str) else None
        if (
            parsed_demo_url is None
            or parsed_demo_url.scheme != "https"
            or not parsed_demo_url.netloc
            or parsed_demo_url.username
            or parsed_demo_url.password
        ):
            raise ValueError("invalid_manifest")
    if "test_cases" in review:
        test_cases = _manifest_fields(review["test_cases"], {"positive", "negative"})
        positive = test_cases.get("positive")
        negative = test_cases.get("negative")
        if (
            not isinstance(positive, list)
            or len(positive) != 5
            or not isinstance(negative, list)
            or len(negative) != 3
        ):
            raise ValueError("invalid_manifest")
        for case in positive:
            case = _manifest_fields(
                case, {"description", "prompt", "tools_triggered", "expected_behavior"}
            )
            if set(case) != {"description", "prompt", "tools_triggered", "expected_behavior"}:
                raise ValueError("invalid_manifest")
            if any(not isinstance(value, str) or not value.strip() for value in case.values()):
                raise ValueError("invalid_manifest")
        for case in negative:
            case = _manifest_fields(case, {"description", "prompt"})
            if set(case) != {"description", "prompt"}:
                raise ValueError("invalid_manifest")
            if any(not isinstance(value, str) or not value.strip() for value in case.values()):
                raise ValueError("invalid_manifest")
    servers = server.get("mcpServers", {}) if isinstance(server, dict) else None
    endpoint = servers.get("durumari", {}) if isinstance(servers, dict) else None
    if (
        not isinstance(server, dict)
        or set(server) - {"$schema", "mcpServers"}
        or ("$schema" in server and server["$schema"] != "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json")
        or not isinstance(servers, dict)
        or set(servers) != {"durumari"}
        or not isinstance(endpoint, dict)
        or endpoint != {
            "type": "streamable-http",
            "url": "https://mcp.durumari.cloud/mcp",
            "extensions": {"com.openai": {"auth": {"type": "oauth", "client": {"mode": "dcr"}}}},
        }
    ):
        raise ValueError("unexpected_mcp_endpoint")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            entry = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            entry.create_system = 3
            entry.external_attr = 0o100644 << 16
            entry.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(entry, content)
    payload = buffer.getvalue()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as destination:
        destination.write(payload)
    return {
        "output": str(output),
        "files": sorted(files),
        "sha256": hashlib.sha256(payload).hexdigest(),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = build(args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
