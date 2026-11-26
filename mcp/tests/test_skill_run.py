"""`project-skills run` injects fresh device deliveries; synthetic values only."""

import base64
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pydantic import ValidationError

from durumari_mcp import skill_cli
from durumari_mcp.cli import build_parser
from durumari_mcp.credentials import AdapterError
from durumari_mcp.skill_tools import Requirement, register_skill_tools

PROJECT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
CONNECTION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
SKILL = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
DEVICE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
TOKEN = "synthetic-skill-token-7a3f9c1e"
PASSWORD = "synthetic-account-password-4b8d2e"
PEM = b"-----BEGIN SYNTHETIC KEY-----\nAAAA\n-----END SYNTHETIC KEY-----\n"

# The child reports what it saw into a file; the parent never prints a value.
CHILD = """
import json, os, sys
from pathlib import Path
seen = {}
for name in sys.argv[2].split(","):
    value = os.environ.get(name)
    seen[name] = value
    if value and Path(value).is_file():
        seen[name + ":content"] = Path(value).read_bytes().decode()
    if value and Path(value).is_dir():
        seen[name + ":files"] = sorted(p.name for p in Path(value).iterdir())
Path(sys.argv[1]).write_text(json.dumps(seen))
sys.exit(int(sys.argv[3]))
"""


def file_value(files, **extra):
    return json.dumps(
        {
            "kind": "file",
            "format": extra.pop("format", "generic"),
            "files": [
                {"filename": name, "content_b64": base64.b64encode(data).decode()}
                for name, data in files
            ],
            **extra,
        }
    )


class Store:
    def __init__(self):
        self.items = {}

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        self.items[(service, account)] = value


class Server:
    """Skill list, device list and the signed-challenge, sealed delivery contract."""

    def __init__(self, workspace, entries):
        self.entries = entries
        self.profile = SimpleNamespace(
            project_id=PROJECT,
            connection_id=CONNECTION,
            workspace=str(workspace),
            check_workspace=lambda cwd: None,
        )
        self.signing = ed25519.Ed25519PrivateKey.generate()
        self.encryption = x25519.X25519PrivateKey.generate()
        self.deliveries = 0
        self.device = {
            "id": DEVICE,
            "skill_id": SKILL,
            "state": "approved",
            "revision": 3,
            "version": "1.0.0",
            "bindings": [
                {k: e[k] for k in ("slot", "secret_id", "revision", "policy_revision")}
                for e in entries
            ],
        }

    def keys(self):
        return {
            "connection_id": CONNECTION,
            "skill_id": SKILL,
            "signing_private": base64.b64encode(self.signing.private_bytes_raw()).decode(),
            "encryption_private": base64.b64encode(self.encryption.private_bytes_raw()).decode(),
            "ssh_public_key": "ssh-ed25519 AAAA",
            "ssh_key_path": "unused",
        }

    def request(self, path, body=None, *, method="GET"):
        if path == "/api/v2/agent/skills":
            return {"items": [{"id": SKILL, "name": "team-ops", "project_id": PROJECT}]}
        if path == "/api/v2/agent/skills/devices":
            return {"items": [self.device]}
        base = "/api/v2/agent/skills/devices/" + DEVICE
        if path == base + "/challenges":
            message = {
                "domain": "durumari-skill-device-v1",
                "device_id": DEVICE,
                "challenge_id": "challenge",
                "revision": 3,
            }
            return {"id": "challenge", "message": json.dumps(message)}
        assert path == base + "/deliveries" and method == "POST"
        self.deliveries += 1
        binding = {
            "project_id": PROJECT,
            "connection_id": CONNECTION,
            "device_id": DEVICE,
            "challenge_id": "challenge",
            "revision": 3,
        }
        aad = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ephemeral = x25519.X25519PrivateKey.generate()
        shared = ephemeral.exchange(self.encryption.public_key())
        key = HKDF(
            algorithm=hashes.SHA256(), length=32, salt=None, info=b"durumari-skill-device-v1" + aad
        ).derive(shared)
        nonce = os.urandom(12)
        plain = json.dumps({"credentials": [dict(e) for e in self.entries]}).encode()
        return {
            "algorithm": "X25519-HKDF-SHA256-AES256GCM-v1",
            "binding": binding,
            "ephemeral_key": base64.b64encode(ephemeral.public_key().public_bytes_raw()).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, plain, aad)).decode(),
        }


def entry(slot, value, **extra):
    return {
        "slot": slot,
        "secret_id": "secret-" + slot,
        "revision": 1,
        "policy_revision": 1,
        "receipt_id": "receipt-" + slot,
        "value": value,
        **extra,
    }


def run(monkeypatch, tmp_path, entries, names, exit_code=0):
    server = Server(tmp_path, entries)
    store = Store()
    store.set_password(
        skill_cli.SERVICE, skill_cli.store_id(server, SKILL), json.dumps(server.keys())
    )
    monkeypatch.setattr(skill_cli, "os_store", lambda: store)
    monkeypatch.setattr(skill_cli, "ProjectGateway", lambda name, client: server)
    report = tmp_path / "seen.json"
    args = build_parser().parse_args(
        [
            "project-skills",
            "run",
            "--profile",
            "synthetic",
            "--client",
            "claude",
            "--skill",
            "team-ops",
            "--",
            sys.executable,
            "-c",
            CHILD,
            str(report),
            ",".join(names),
            str(exit_code),
        ]
    )
    with pytest.raises(SystemExit) as stopped:
        skill_cli.execute(args)
    seen = json.loads(report.read_text()) if report.exists() else None
    return stopped.value.code, seen, server


def test_run_injects_text_and_account_values_and_prints_nothing(monkeypatch, tmp_path, capfd):
    code, seen, server = run(
        monkeypatch,
        tmp_path,
        [
            entry("api_token", TOKEN, kind="text", env="SERVICE_TOKEN"),
            entry("deploy_account", PASSWORD, kind="account", env="DEPLOY_PASSWORD"),
        ],
        ["SERVICE_TOKEN", "DEPLOY_PASSWORD", "API_TOKEN"],
    )
    assert code == 0 and server.deliveries == 1
    assert seen == {"SERVICE_TOKEN": TOKEN, "DEPLOY_PASSWORD": PASSWORD, "API_TOKEN": None}
    out, err = capfd.readouterr()
    assert TOKEN not in out + err and PASSWORD not in out + err


def test_legacy_entry_without_kind_or_env_is_text_named_after_the_slot(monkeypatch, tmp_path):
    code, seen, _ = run(monkeypatch, tmp_path, [entry("api_token", TOKEN)], ["API_TOKEN"])
    assert code == 0 and seen == {"API_TOKEN": TOKEN}


@pytest.mark.parametrize("exit_code", [0, 3])
def test_file_secret_exists_only_while_the_child_runs(monkeypatch, tmp_path, exit_code):
    code, seen, _ = run(
        monkeypatch,
        tmp_path,
        [
            entry(
                "deploy_key",
                file_value([("id_ed25519", PEM)], format="ssh_key", username="deploy"),
                kind="file",
                env="DEPLOY_KEY",
            )
        ],
        ["DEPLOY_KEY", "DEPLOY_KEY_USERNAME", "DEPLOY_KEY_PASSPHRASE"],
        exit_code,
    )
    assert code == exit_code
    path = Path(seen["DEPLOY_KEY"])
    assert path.name == "id_ed25519" and seen["DEPLOY_KEY:content"] == PEM.decode()
    assert seen["DEPLOY_KEY_USERNAME"] == "deploy" and seen["DEPLOY_KEY_PASSPHRASE"] is None
    assert not path.exists() and not path.parent.parent.exists()


def test_multi_file_secret_points_at_its_folder(monkeypatch, tmp_path):
    value = file_value([("ca.pem", b"ca"), ("cert.pem", b"cert"), ("key.pem", b"key")])
    code, seen, _ = run(
        monkeypatch, tmp_path, [entry("docker_tls", value, kind="file")], ["DOCKER_TLS"]
    )
    assert code == 0 and seen["DOCKER_TLS:files"] == ["ca.pem", "cert.pem", "key.pem"]
    assert not Path(seen["DOCKER_TLS"]).exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX modes")
def test_file_secret_modes_are_owner_only(monkeypatch, tmp_path):
    modes = {}
    write = skill_cli.write_secret_file

    def record(path, data):
        result = write(path, data)
        modes["file"] = path.stat().st_mode & 0o777
        modes["dir"] = path.parent.parent.stat().st_mode & 0o777
        return result

    monkeypatch.setattr(skill_cli, "write_secret_file", record)
    run(monkeypatch, tmp_path, [entry("cert", file_value([("c.pem", b"x")]), kind="file")], [])
    assert modes == {"file": 0o600, "dir": 0o700}


def test_temporary_files_are_removed_when_the_command_cannot_start(monkeypatch, tmp_path):
    created = []
    original = skill_cli.tempfile.mkdtemp

    def mkdtemp(**kwargs):
        created.append(original(**kwargs))
        return created[-1]

    monkeypatch.setattr(skill_cli.tempfile, "mkdtemp", mkdtemp)
    credentials = [entry("cert", file_value([("c.pem", b"x")]), kind="file")]
    with pytest.raises(AdapterError, match="run_command_not_found"):
        skill_cli.run_skill(credentials, ["durumari-command-that-does-not-exist-91c2"])
    assert created and not Path(created[0]).exists()
    assert credentials == [{}]


@pytest.mark.parametrize(
    "value",
    [
        "not json",
        json.dumps({"kind": "file", "format": "generic", "files": []}),
        json.dumps({"kind": "text", "format": "generic", "files": [{}]}),
        file_value([("c.pem", b"x")], format="zip"),
        file_value([("../escape", b"x")]),
        file_value([("nul", b"x")]),
        file_value([("a.pem", b"x"), ("A.PEM", b"y")]),
        file_value([("c.pem", b"x")], token="extra"),
        file_value([("c.pem", b"x")], password=1),
        json.dumps(
            {
                "kind": "file",
                "format": "generic",
                "files": [{"filename": "c.pem", "content_b64": "***"}],
            }
        ),
    ],
)
def test_malformed_file_payload_is_rejected_before_anything_runs(monkeypatch, tmp_path, value):
    with pytest.raises(AdapterError, match="invalid_device_envelope"):
        run(monkeypatch, tmp_path, [entry("cert", value, kind="file")], ["CERT"])
    assert not (tmp_path / "seen.json").exists()


@pytest.mark.parametrize(
    "extra", [{"kind": "blob"}, {"env": "PATH"}, {"env": "DURUMARI_MCP_X"}, {"env": "1BAD"}]
)
def test_unknown_kind_or_unusable_env_is_rejected(monkeypatch, tmp_path, extra):
    with pytest.raises(AdapterError, match="invalid_device_envelope"):
        run(monkeypatch, tmp_path, [entry("api_token", TOKEN, **extra)], ["API_TOKEN"])


def test_duplicate_variables_are_refused(monkeypatch, tmp_path):
    with pytest.raises(AdapterError, match="skill_env_conflict"):
        run(
            monkeypatch,
            tmp_path,
            [entry("one", TOKEN, env="SAME"), entry("two", PASSWORD, env="SAME")],
            ["SAME"],
        )


def test_run_requires_a_command_before_any_delivery(monkeypatch, tmp_path):
    server = Server(tmp_path, [entry("api_token", TOKEN)])
    monkeypatch.setattr(skill_cli, "ProjectGateway", lambda name, client: server)
    args = build_parser().parse_args(
        ["project-skills", "run", "--profile", "p", "--client", "claude", "--skill", "team-ops"]
    )
    with pytest.raises(AdapterError, match="run_command_required"):
        skill_cli.execute(args)
    assert server.deliveries == 0


def test_requirement_accepts_new_and_legacy_kinds_with_optional_env():
    for kind in ["text", "account", "file", "api_token", "ssh_key", "dns_credential", "password"]:
        Requirement(name="slot", kind=kind)
    for bad in [{"kind": "blob"}, {"kind": "text", "env": "PATH"}, {"kind": "text", "env": "a-b"}]:
        with pytest.raises(ValidationError):
            Requirement.model_validate({"name": "slot", **bad})
    calls = []

    def request(path, body=None, *, method="GET"):
        calls.append(body)
        return {"id": SKILL, "project_id": "project"}

    tools = {}
    register_skill_tools(
        SimpleNamespace(profile=SimpleNamespace(project_id="project"), request=request),
        lambda fn, **kw: tools.update({fn.__name__: fn}),
        ["read", "write"],
    )
    assert "durumari.json" in tools["durumari_skill_publish"].__doc__
    tools["durumari_skill_publish"](
        "team",
        "1.0.0",
        {"SKILL.md": "public"},
        [
            Requirement(name="cert", kind="file", description="Client cert", env="CLIENT_CERT"),
            Requirement(name="api_token", kind="api_token"),
        ],
    )
    assert calls[0]["requirements"] == [
        {
            "name": "cert",
            "kind": "file",
            "required": True,
            "description": "Client cert",
            "env": "CLIENT_CERT",
        },
        {"name": "api_token", "kind": "api_token", "required": True},
    ]


def test_cli_requirement_flag_accepts_new_kinds(monkeypatch, tmp_path):
    sent = []
    gateway = SimpleNamespace(
        request=lambda path, body=None, method="GET": sent.append(body) or {"status": "ok"}
    )
    archive = tmp_path / "skill.zip"
    archive.write_bytes(b"PK")
    args = SimpleNamespace(
        version="1.0.0",
        requirement=["cert:file:optional", "login:account"],
        git_path="",
        operation="import-zip",
        archive=str(archive),
        skill=None,
    )
    skill_cli.import_package(gateway, args)
    assert sent[0]["requirements"] == [
        {"name": "cert", "kind": "file", "required": False},
        {"name": "login", "kind": "account", "required": True},
    ]
