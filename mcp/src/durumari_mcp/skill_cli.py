"""Human-invoked native operations; no credential method is exposed as an MCP tool."""

import base64
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import quote

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from durumari_mcp.credentials import AdapterError, os_store, profile_name
from durumari_mcp.private_files import protect_directory
from durumari_mcp.private_files import protect_file as protect_private_file
from durumari_mcp.project_skills import (
    INSTALL_CLIENTS,
    install_bundle,
    migrate_legacy_copies,
    safe_parents,
    skill_root,
)
from durumari_mcp.skill_gateway import SkillGateway as ProjectGateway

SERVICE = "DURUMARI.SkillDevice.v1"
VALUES = "DURUMARI.SkillCredentials.v1"
# Delivered secret kinds; an entry without `kind` comes from an older server and is text.
CREDENTIAL_KINDS = {"text", "account", "file"}
FILE_FORMATS = {"generic", "ssh_key", "pkcs12", "docker_tls"}
# Optional file-secret fields, injected as <ENV>_USERNAME, <ENV>_PASSPHRASE, <ENV>_PASSWORD.
FILE_FIELDS = ("username", "passphrase", "password")
MAX_SECRET_FILES = 32  # vault file items hold 1..32 files (file_schemas.FileCredential)
SLOT = re.compile(r"[a-z][a-z0-9_]{0,39}")
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
WINDOWS_DEVICE = re.compile(r"(con|prn|aux|nul|com[0-9]|lpt[0-9])(\..*)?", re.IGNORECASE)


def add_commands(subcommands: Any) -> None:
    for name, actions in [
        (
            "project-skills",
            [
                "list",
                "install",
                "status",
                "sync",
                "sync-status",
                "import-git",
                "import-npm",
                "import-zip",
                "run",
            ],
        ),
        ("project-device", ["enroll", "status", "receive", "forget"]),
    ]:
        parser = subcommands.add_parser(
            name, help="Manage project Skills and local device credentials"
        )
        parser.add_argument("operation", choices=actions)
        parser.add_argument("--profile")
        parser.add_argument(
            "--client",
            choices=[
                "codex",
                "claude",
                "cursor",
                "gemini",
                "antigravity",
                "opencode",
                "copilot",
                "mcp",
            ],
        )
        parser.add_argument("--skill")
        if name == "project-skills":
            parser.add_argument("--version")
            parser.add_argument(
                "--repository", help="HTTPS repository root URL without credentials"
            )
            parser.add_argument(
                "--path", dest="git_path", default="", help="Skill directory in Git"
            )
            parser.add_argument("--ref", dest="git_ref", default="", help="Branch, tag or commit")
            parser.add_argument(
                "--checkout", help="Existing authenticated checkout for private Git"
            )
            parser.add_argument(
                "--package", help="import-npm: public npm package name, e.g. @team/skill"
            )
            parser.add_argument(
                "--package-version", help="import-npm: one exact version; never a range or tag"
            )
            parser.add_argument(
                "--archive", help="import-zip: a local .zip whose folder holds SKILL.md"
            )
            parser.add_argument(
                "--requirement", action="append", default=[], metavar="SLOT:KIND[:optional]"
            )
            parser.add_argument(
                "--enroll", action="store_true", help="Request device access after installation"
            )
            parser.add_argument(
                "child_command",
                nargs="*",
                help="run: the command after --, started without a shell",
            )


def import_package(gateway: ProjectGateway, args: Any) -> dict[str, Any]:
    """npm or zip Skill sources: the server reads and verifies the files.

    npm: the server fetches one exact version from the public registry and checks its
    sha512 integrity. zip: this command sends the archive bytes only; the server hashes
    and unpacks them. Nothing from either is installed or executed.
    """
    from pydantic import TypeAdapter

    from durumari_mcp.skill_tools import Requirement, Version

    try:
        version = TypeAdapter(Version).validate_python(args.version)
        requirements = []
        for raw in args.requirement:
            parts = raw.split(":")
            if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] != "optional"):
                raise ValueError
            requirements.append(
                Requirement.model_validate(
                    {"name": parts[0], "kind": parts[1], "required": len(parts) == 2}
                ).model_dump(exclude_none=True)
            )
    except ValueError:
        raise AdapterError("invalid_command_arguments") from None
    body: dict[str, Any] = {"version": version, "requirements": requirements, "path": args.git_path}
    kind = args.operation.removeprefix("import-")
    if kind == "npm":
        if not args.package or not args.package_version:
            raise AdapterError("invalid_command_arguments")
        body.update(package=args.package, package_version=args.package_version)
    else:
        if not args.archive:
            raise AdapterError("invalid_command_arguments")
        archive = Path(args.archive)
        try:
            if archive.is_symlink() or not archive.is_file():
                raise AdapterError("invalid_command_arguments")
            if archive.stat().st_size > 2_000_000:
                raise AdapterError("skill_archive_limit")
            data = archive.read_bytes()
        except OSError:
            raise AdapterError("invalid_command_arguments") from None
        body["archive_base64"] = base64.b64encode(data).decode("ascii")
    endpoint = "/api/v2/agent/skills/import-" + kind
    if args.skill:
        row = selected(gateway, args.skill)
        endpoint = "/api/v2/agent/skills/" + quote(row["id"], safe="") + "/import-" + kind
    return gateway.request(endpoint, body, method="POST")


def selected(gateway: ProjectGateway, name: str | None) -> dict[str, Any]:
    rows = gateway.request("/api/v2/agent/skills")["items"]
    found = [row for row in rows if row["name"] == name]
    if len(found) != 1:
        raise AdapterError("project_skill_required")
    result: dict[str, Any] = found[0]
    if result.get("project_id") != gateway.profile.project_id:
        raise AdapterError("skill_scope_mismatch")
    return result


def store_id(gateway: ProjectGateway, skill_id: str) -> str:
    return hashlib.sha256((gateway.profile.connection_id + ":" + skill_id).encode()).hexdigest()


def local_keys(gateway: ProjectGateway, skill_id: str, *, create: bool) -> dict[str, str]:
    store = os_store()
    identity = store_id(gateway, skill_id)
    raw = store.get_password(SERVICE, identity)
    if raw:
        try:
            keys: dict[str, str] = json.loads(raw)
            if (
                keys["connection_id"] != gateway.profile.connection_id
                or keys["skill_id"] != skill_id
            ):
                raise ValueError
            return keys
        except (ValueError, KeyError, TypeError):
            raise AdapterError("invalid_device_store") from None
    if not create:
        raise AdapterError("device_enrollment_required")
    signing = ed25519.Ed25519PrivateKey.generate()
    encryption = x25519.X25519PrivateKey.generate()
    ssh = ed25519.Ed25519PrivateKey.generate()
    path = Path.home() / ".ssh" / ("durumari-" + identity[:24])
    safe_parents(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        raise AdapterError("device_key_file_collision")
    fd = os.open(path, os.O_EXCL | os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "wb") as out:
            # Apply the ACL while the new file is still empty, before key material.
            protect_private_file(path)
            out.write(
                ssh.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.OpenSSH,
                    serialization.NoEncryption(),
                )
            )
    except Exception:
        path.unlink()
        raise AdapterError("device_key_creation_failed") from None
    keys = {
        "connection_id": gateway.profile.connection_id,
        "skill_id": skill_id,
        "signing_private": base64.b64encode(signing.private_bytes_raw()).decode(),
        "encryption_private": base64.b64encode(encryption.private_bytes_raw()).decode(),
        "ssh_public_key": ssh.public_key()
        .public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH)
        .decode(),
        "ssh_key_path": str(path),
    }
    try:
        store.set_password(SERVICE, identity, json.dumps(keys))
    except Exception:
        # The newly generated key was never registered; remove this attempt only.
        path.unlink()
        raise AdapterError("device_store_unavailable") from None
    return keys


def fetch_credentials(
    gateway: ProjectGateway, device: dict[str, Any], keys: dict[str, str]
) -> list[dict[str, Any]]:
    """Fresh signed delivery; callers never print the returned values."""
    path = "/api/v2/agent/skills/devices/" + quote(device["id"], safe="")
    proof = gateway.request(path + "/challenges", {}, method="POST")
    try:
        message = json.loads(proof["message"])
        if (
            message["domain"] != "durumari-skill-device-v1"
            or message["device_id"] != device["id"]
            or message["challenge_id"] != proof["id"]
            or message["revision"] != device["revision"]
        ):
            raise ValueError
        signing = ed25519.Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(keys["signing_private"], validate=True)
        )
        signature = base64.b64encode(signing.sign(proof["message"].encode())).decode()
        envelope = gateway.request(
            path + "/deliveries",
            {"challenge_id": proof["id"], "signature": signature},
            method="POST",
        )
        binding = {
            "project_id": gateway.profile.project_id,
            "connection_id": gateway.profile.connection_id,
            "device_id": device["id"],
            "challenge_id": proof["id"],
            "revision": device["revision"],
        }
        if (
            envelope["algorithm"] != "X25519-HKDF-SHA256-AES256GCM-v1"
            or envelope["binding"] != binding
        ):
            raise ValueError
        aad = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        box = x25519.X25519PrivateKey.from_private_bytes(
            base64.b64decode(keys["encryption_private"], validate=True)
        )
        shared = box.exchange(
            x25519.X25519PublicKey.from_public_bytes(
                base64.b64decode(envelope["ephemeral_key"], validate=True)
            )
        )
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"durumari-skill-device-v1" + aad
        ).derive(shared)
        payload = json.loads(
            AESGCM(key).decrypt(
                base64.b64decode(envelope["nonce"], validate=True),
                base64.b64decode(envelope["ciphertext"], validate=True),
                aad,
            )
        )
        credentials = payload["credentials"]
        if (
            set(payload) != {"credentials"}
            or not isinstance(credentials, list)
            or len(credentials) != len(device["bindings"])
        ):
            raise ValueError
        expected = {b["slot"]: b for b in device["bindings"]}
        for entry in credentials:
            ref = {k: entry[k] for k in ("slot", "secret_id", "revision", "policy_revision")}
            if expected.pop(entry["slot"], None) != ref or not isinstance(entry["value"], str):
                raise ValueError
            kind = entry.get("kind", "text")
            if kind not in CREDENTIAL_KINDS:
                raise ValueError
            credential_env(entry)
            if kind == "file":
                file_payload(entry["value"])
        if expected:
            raise ValueError
    except (KeyError, ValueError, TypeError, InvalidTag):
        raise AdapterError("invalid_device_envelope") from None
    return credentials


def credential_env(entry: dict[str, Any]) -> str:
    """The variable a slot is injected as: its `env`, or the slot name upper-cased."""
    from durumari_mcp.secret_env import reserved

    slot = entry["slot"]
    if not isinstance(slot, str) or not SLOT.fullmatch(slot):
        raise ValueError
    name = entry.get("env") or slot.upper()
    if not isinstance(name, str) or not ENV_NAME.fullmatch(name) or reserved(name):
        raise ValueError
    return name


def file_payload(value: str) -> dict[str, Any]:
    """Strictly parse a `kind: file` value; raises ValueError for any other shape."""
    payload = json.loads(value)
    if (
        not isinstance(payload, dict)
        or payload.get("kind") != "file"
        or payload.get("format") not in FILE_FORMATS
        or set(payload) - {"kind", "format", "files", *FILE_FIELDS}
        or any(not isinstance(payload[field], str) for field in FILE_FIELDS if field in payload)
    ):
        raise ValueError
    files = payload.get("files")
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_SECRET_FILES:
        raise ValueError
    seen: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or set(item) != {"filename", "content_b64"}:
            raise ValueError
        name, content = item["filename"], item["content_b64"]
        if (
            not isinstance(name, str)
            or not FILENAME.fullmatch(name)
            or name.endswith(".")
            or WINDOWS_DEVICE.fullmatch(name)
            or name.lower() in seen
            or not isinstance(content, str)
        ):
            raise ValueError
        seen.add(name.lower())
        base64.b64decode(content, validate=True)  # binascii.Error is a ValueError
    return payload


def write_secret_file(path: Path, data: bytes) -> Path:
    flags = (
        os.O_EXCL
        | os.O_CREAT
        | os.O_WRONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as out:
        # Owner-only access is applied while the file is still empty.
        protect_private_file(path)
        out.write(data)
    return path


def remove_secret_files(directory: Path) -> None:
    try:
        shutil.rmtree(directory)
    except OSError:
        # Paths are not secret; the operator must know files were left behind.
        print(
            "durumari: temporary secret files could not be removed: " + str(directory),
            file=sys.stderr,
        )


def run_skill(credentials: list[dict[str, Any]], command: list[str]) -> int:
    """Run one child with freshly delivered values; files exist only while it runs.

    text/account slots become `ENV=value`. A file slot is written under a private
    temporary directory (0700/0600, owner-only ACL on Windows); its variable holds
    the path of its single file, or of its folder when it has several files. The
    directory is removed when the child exits, fails to start or is interrupted.
    Nothing is printed; the child's stdin, stdout and stderr stay attached.
    """
    from durumari_mcp.secret_env import reserved, run_child

    environment = dict(os.environ)
    injected: set[str] = set()
    workdir: Path | None = None

    def put(name: str, value: str) -> None:
        if name in injected or reserved(name):
            raise AdapterError("skill_env_conflict")
        injected.add(name)
        environment[name] = value

    try:
        for entry in credentials:
            name = credential_env(entry)
            if entry.get("kind", "text") != "file":
                put(name, entry["value"])
                continue
            payload = file_payload(entry["value"])
            if workdir is None:
                workdir = Path(tempfile.mkdtemp(prefix="durumari-skill-")).resolve()
                protect_directory(workdir)
            folder = workdir / entry["slot"]
            protect_directory(folder)
            paths = [
                write_secret_file(folder / item["filename"], base64.b64decode(item["content_b64"]))
                for item in payload["files"]
            ]
            put(name, str(paths[0] if len(paths) == 1 else folder))
            for field in FILE_FIELDS:
                if field in payload:
                    put(name + "_" + field.upper(), payload[field])
            payload.clear()
        return run_child(command, environment)
    except ValueError:
        raise AdapterError("invalid_device_envelope") from None
    finally:
        environment.clear()
        for entry in credentials:
            entry.clear()
        if workdir is not None:
            remove_secret_files(workdir)


def skill_device(gateway: ProjectGateway, row: dict[str, Any]) -> dict[str, Any]:
    devices = gateway.request("/api/v2/agent/skills/devices")["items"]
    matching = [d for d in devices if d["skill_id"] == row["id"]]
    if len(matching) != 1:
        raise AdapterError("device_enrollment_required")
    device: dict[str, Any] = matching[0]
    return device


def receive(gateway: ProjectGateway, device: dict[str, Any], keys: dict[str, str]) -> int:
    credentials = fetch_credentials(gateway, device, keys)
    # Values never appear in stdout, the project files, hooks or MCP responses.
    try:
        os_store().set_password(
            VALUES,
            store_id(gateway, device["skill_id"]),
            json.dumps(
                {
                    "device_id": device["id"],
                    "revision": device["revision"],
                    "skill_id": device["skill_id"],
                    "skill_version": device["version"],
                    "resource_scope": device["resource_scope"],
                    "credentials": credentials,
                }
            ),
        )
    except Exception:
        raise AdapterError("device_store_unavailable") from None
    return len(credentials)


def infer_client(profile: str, requested: str | None = None) -> str:
    from durumari_mcp.project_credentials import load_project_profile

    if requested:
        if requested not in INSTALL_CLIENTS:
            raise AdapterError("project_client_mismatch")
        return requested
    client = load_project_profile(profile).client
    if client in INSTALL_CLIENTS:
        return client
    if os.environ.get("CLAUDECODE"):
        return "claude"
    if os.environ.get("CURSOR_AGENT"):
        return "cursor"
    return "mcp"


def enroll_device(gateway: ProjectGateway, row: dict[str, Any]) -> dict[str, Any]:
    keys = local_keys(gateway, row["id"], create=True)
    signing = ed25519.Ed25519PrivateKey.from_private_bytes(
        base64.b64decode(keys["signing_private"], validate=True)
    )
    encryption = x25519.X25519PrivateKey.from_private_bytes(
        base64.b64decode(keys["encryption_private"], validate=True)
    )
    body = {
        "skill_id": row["id"],
        "signing_key": base64.b64encode(signing.public_key().public_bytes_raw()).decode(),
        "encryption_key": base64.b64encode(encryption.public_key().public_bytes_raw()).decode(),
        "ssh_public_key": keys["ssh_public_key"],
    }
    try:
        os_store().set_password(
            SERVICE,
            "index:" + profile_name(gateway.name) + ":" + row["name"],
            json.dumps({"credential_store_id": store_id(gateway, row["id"])}),
        )
    except Exception:
        raise AdapterError("device_store_unavailable") from None
    enrolled = gateway.request("/api/v2/agent/skills/devices", body, method="POST")
    return {
        "device_id": enrolled["id"],
        "state": enrolled["state"],
        "fingerprint": enrolled["fingerprint"],
        "ssh_key_path": keys["ssh_key_path"],
    }


def install_skill(
    gateway: ProjectGateway,
    row: dict[str, Any],
    client: str,
    version: str | None = None,
    *,
    enroll: bool | None = False,
) -> dict[str, Any]:
    """`enroll=None` requests device access only when this version names secret slots."""
    version = version or row["selected_version"]
    path = "/api/v2/agent/skills/" + quote(row["id"], safe="")
    bundle = gateway.request(path + "/versions/" + quote(version, safe=""))
    if (
        (bundle.get("id"), bundle.get("project_id"), bundle.get("name"), bundle.get("version"))
        != (row["id"], gateway.profile.project_id, row["name"], version)
        or version == row["selected_version"]
        and bundle.get("sha256") != row["sha256"]
    ):
        raise AdapterError("skill_scope_mismatch")
    installed = install_bundle(Path(gateway.profile.workspace), client, bundle)
    gateway.request(
        path + "/installations",
        {
            "client": client,
            "version": version,
            "sha256": bundle["sha256"],
        },
        method="POST",
    )

    def previous_release(previous_version: str) -> dict[str, Any]:
        try:
            previous = gateway.request(path + "/versions/" + quote(previous_version, safe=""))
        except Exception:
            raise AdapterError("skill_legacy_release_unavailable") from None
        return previous

    result: dict[str, Any] = {
        "status": "installed",
        "path": str(installed),
        "version": version,
        "recognition": "available_via_durumari_mcp",
        "legacy_copies": migrate_legacy_copies(
            Path(gateway.profile.workspace), client, bundle, previous_release
        ),
    }
    if enroll is None:
        enroll = bool(bundle.get("requirements"))
    if enroll:
        result["device"] = enroll_device(gateway, row)
    return result


def execute(args: Any) -> None:
    from durumari_mcp.context_removal import local_profile

    args.profile = args.profile or local_profile(Path.cwd().resolve())
    if not args.profile:
        raise AdapterError("project_credential_missing")
    command: list[str] = getattr(args, "child_command", None) or []
    command = command[1:] if command[:1] == ["--"] else command
    if args.command == "project-skills" and args.operation == "run" and not command:
        raise AdapterError("run_command_required")
    if command and args.operation != "run":
        raise AdapterError("invalid_command_arguments")
    if args.command == "project-device" and args.operation == "forget":
        name = profile_name(args.profile)
        if not args.skill or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.skill):
            raise AdapterError("project_skill_required")
        try:
            store = os_store()
            index = store.get_password(SERVICE, "index:" + name + ":" + args.skill)
            identity = json.loads(index)["credential_store_id"] if index else None
            if identity and store.get_password(VALUES, identity) is not None:
                store.delete_password(VALUES, identity)
        except Exception:
            raise AdapterError("device_store_unavailable") from None
        print(
            json.dumps(
                {
                    "status": "local_credentials_removed",
                    "provider_revocation": "still_required",
                    "ssh_key": "retained_locally",
                }
            )
        )
        return
    args.client = infer_client(args.profile, args.client)
    gateway = ProjectGateway(args.profile, args.client)
    gateway.profile.check_workspace(os.getcwd())
    if args.command == "project-skills" and args.operation == "list":
        print(json.dumps(gateway.request("/api/v2/agent/skills")))
        return
    if args.command == "project-skills" and args.operation in {"sync", "sync-status"}:
        from durumari_mcp.required_skills import status, sync

        operation = sync if args.operation == "sync" else status
        print(json.dumps(operation(gateway, args.client)))
        return
    if args.command == "project-skills" and args.operation in {"import-npm", "import-zip"}:
        print(json.dumps(import_package(gateway, args)))
        return
    if args.command == "project-skills" and args.operation == "import-git":
        from pydantic import TypeAdapter

        from durumari_mcp.skill_git import normalize_repository_url, read_checkout, skill_name
        from durumari_mcp.skill_tools import Requirement, Version

        try:
            version = TypeAdapter(Version).validate_python(args.version)
            requirements = []
            for raw in args.requirement:
                parts = raw.split(":")
                if len(parts) not in {2, 3} or (len(parts) == 3 and parts[2] != "optional"):
                    raise ValueError
                requirements.append(
                    Requirement.model_validate(
                        {"name": parts[0], "kind": parts[1], "required": len(parts) == 2}
                    ).model_dump(exclude_none=True)
                )
            repository = normalize_repository_url(args.repository or "")
        except ValueError:
            raise AdapterError("invalid_command_arguments") from None
        body: dict[str, Any] = {
            "version": version,
            "requirements": requirements,
        }
        endpoint = "/api/v2/agent/skills/import-git"
        if args.checkout:
            imported = read_checkout(repository, Path(args.checkout), args.git_ref, args.git_path)
            body.update(
                name=skill_name(imported["files"]),
                files=imported["files"],
                source=imported["source"],
            )
            endpoint = "/api/v2/agent/skills"
        else:
            body.update(repository_url=repository, ref=args.git_ref, path=args.git_path)
            if args.skill:
                row = selected(gateway, args.skill)
                endpoint = "/api/v2/agent/skills/" + quote(row["id"], safe="") + "/import-git"
        print(json.dumps(gateway.request(endpoint, body, method="POST")))
        return
    row = selected(gateway, args.skill)
    if args.command == "project-skills" and args.operation == "run":
        # Always a fresh signed delivery; the OS-store copy from `receive` is never read.
        keys = local_keys(gateway, row["id"], create=False)
        device = skill_device(gateway, row)
        if device["state"] != "approved":
            raise AdapterError("device_approval_required")
        raise SystemExit(run_skill(fetch_credentials(gateway, device, keys), command))
    if args.command == "project-skills":
        version = args.version or row["selected_version"]
        if args.operation == "status":
            root = skill_root(Path(gateway.profile.workspace), args.client) / row["name"]
            try:
                marker = json.loads((root / ".durumari-release.json").read_text())
            except (ValueError, OSError):
                marker = None
            print(
                json.dumps(
                    {
                        "selected_version": row["selected_version"],
                        "installed_version": marker["version"]
                        if marker and marker.get("project_id") == row["project_id"]
                        else None,
                        "recognition": "available_via_durumari_mcp",
                        "verification": "marker_only",
                    }
                )
            )
            return
        print(
            json.dumps(
                install_skill(
                    gateway,
                    row,
                    args.client,
                    version,
                    enroll=getattr(args, "enroll", False),
                )
            )
        )
        return
    keys = local_keys(gateway, row["id"], create=args.operation == "enroll")
    if args.operation == "enroll":
        print(json.dumps(enroll_device(gateway, row)))
        return
    device = skill_device(gateway, row)
    if args.operation == "status":
        print(
            json.dumps(
                {
                    "device_id": device["id"],
                    "state": device["state"],
                    "approved_version": device["version"],
                    "provider_registration": device["provider_registration"],
                }
            )
        )
        return
    if device["state"] != "approved":
        raise AdapterError("device_approval_required")
    print(
        json.dumps(
            {
                "status": "stored_in_os_credentials",
                "credential_count": receive(gateway, device, keys),
            }
        )
    )
