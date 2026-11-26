"""`secret run`, `secret env` and the OS-store cache never print a value; synthetic only."""

import base64
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ed25519, x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from durumari_mcp import cli, secret_cache, secret_cli, secret_env
from durumari_mcp.credentials import AdapterError
from durumari_mcp.secret_cache import CACHE, SecretCache, get_blob, put_blob

VALUE = "synthetic-run-token-5c1e9a7f3b"
OTHER = "synthetic-db-password-2d8b6f0a4e"
NEWER = "synthetic-rotated-token-8e4a2c6b0d"
CONNECTION = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
PROJECT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
DEVICE = "dddddddd-dddd-4ddd-8ddd-dddddddddddd"
API = "11111111-1111-4111-8111-111111111111"
DB = "22222222-2222-4222-8222-222222222222"
DOMAIN = b"durumari-agent-secret-v1"


class Store:
    """In-memory OS store; `limit` mimics Windows Credential Manager's blob size."""

    def __init__(self, limit=None):
        self.items = {}
        self.limit = limit

    def get_password(self, service, account):
        return self.items.get((service, account))

    def set_password(self, service, account, value):
        if self.limit is not None and len(value.encode("utf-16-le")) > self.limit:
            raise RuntimeError("credential too large")
        self.items[(service, account)] = value

    def delete_password(self, service, account):
        del self.items[(service, account)]

    def text(self):
        return json.dumps({"|".join(key): value for key, value in self.items.items()})


class Server:
    """Value-free listing plus the signed-challenge, sealed-envelope delivery contract."""

    def __init__(self):
        self.secrets = {
            API: {"label": "API_KEY", "value": VALUE, "revision": 1, "policy_revision": 1},
            DB: {"label": "DB_PASSWORD", "value": OTHER, "revision": 1, "policy_revision": 1},
        }
        self.permissions = {API: ["metadata", "use_tool"], DB: ["metadata", "use_tool"]}
        self.tiers: dict[str, str] = {}
        self.env_allowed: dict[str, bool] = {}
        self.cache_seconds = 3600
        self.calls = []
        self.offline = False
        self.keys = None
        self.challenges = {}
        self.tamper = False
        self.deny = None
        self.profile = SimpleNamespace(connection_id=CONNECTION, project_id=PROJECT, workspace="")

    def context_request(self, path, body=None, *, method="GET", idempotency_key=None):
        route = path.split("?", 1)[0]
        self.calls.append((method, route))
        if self.offline:
            raise AdapterError("project_delivery_unknown")
        if route == "/api/v2/agent/secrets":
            items = []
            for secret_id, row in self.secrets.items():
                item = {
                    "id": secret_id,
                    "label": row["label"],
                    "kind": "text",
                    "revision": row["revision"],
                    "policy_revision": row["policy_revision"],
                    "owned": True,
                    "permissions": self.permissions[secret_id],
                    "expires_on": None,
                }
                if secret_id in self.tiers:
                    item["tier"] = self.tiers[secret_id]
                if secret_id in self.env_allowed:
                    item["env_allowed"] = self.env_allowed[secret_id]
                items.append(item)
            return {"items": items, "next_after": None}
        if route == "/api/v2/agent/secret-devices":
            self.keys = body
            return {"id": DEVICE, "connection_id": CONNECTION, "state": "active", "revision": 1}
        device = "/api/v2/agent/secret-devices/" + DEVICE
        if route == device + "/challenges":
            challenge_id = f"eeeeeeee-eeee-4eee-8eee-{len(self.challenges):012d}"
            message = json.dumps(
                {
                    "challenge_id": challenge_id,
                    "connection_id": CONNECTION,
                    "device_id": DEVICE,
                    "domain": DOMAIN.decode(),
                    "nonce": "n",
                    "revision": 1,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            self.challenges[challenge_id] = message
            return {"id": challenge_id, "message": message, "revision": 1}
        if route == device + "/deliveries":
            return self.deliver(body)
        raise AssertionError(route)

    def deliver(self, body):
        message = self.challenges.pop(body["challenge_id"])
        request = json.dumps(
            body["request"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        ed25519.Ed25519PublicKey.from_public_bytes(
            base64.b64decode(self.keys["signing_key"])
        ).verify(base64.b64decode(body["signature"]), message.encode() + b"\n" + request)
        if self.deny:
            raise AdapterError(self.deny)
        entries = []
        for ref in body["request"]["secrets"]:
            row = self.secrets[ref["secret_id"]]
            assert (ref["revision"], ref["policy_revision"]) == (
                row["revision"],
                row["policy_revision"],
            )
            entries.append(
                {
                    "secret_id": ref["secret_id"],
                    "label": row["label"],
                    "revision": row["revision"],
                    "policy_revision": row["policy_revision"],
                    "value": row["value"],
                    "receipt_id": "r",
                }
            )
        binding = {
            "challenge_id": body["challenge_id"],
            "connection_id": CONNECTION,
            "device_id": DEVICE,
            "project_id": PROJECT,
            "request_sha256": hashlib.sha256(request).hexdigest(),
            "revision": 1,
        }
        if self.tamper:
            binding["device_id"] = API
        aad = json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()
        ephemeral = x25519.X25519PrivateKey.generate()
        shared = ephemeral.exchange(
            x25519.X25519PublicKey.from_public_bytes(base64.b64decode(self.keys["encryption_key"]))
        )
        key = HKDF(hashes.SHA256(), 32, None, DOMAIN + aad).derive(shared)
        nonce = os.urandom(12)
        payload = json.dumps({"secrets": entries, "cache_seconds": self.cache_seconds}).encode()
        return {
            "algorithm": "X25519-HKDF-SHA256-AES256GCM-v1",
            "binding": binding,
            "ephemeral_key": base64.b64encode(ephemeral.public_key().public_bytes_raw()).decode(),
            "nonce": base64.b64encode(nonce).decode(),
            "ciphertext": base64.b64encode(AESGCM(key).encrypt(nonce, payload, aad)).decode(),
        }

    def deliveries(self):
        return sum(1 for _, route in self.calls if route.endswith("/deliveries"))


@pytest.fixture
def env(monkeypatch, tmp_path):
    store = Store()
    server = Server()
    server.profile.workspace = str(tmp_path)
    monkeypatch.setattr(secret_cache, "os_store", lambda: store)
    monkeypatch.setattr(secret_cli, "gateway_for", lambda args: server)
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(store=store, server=server, root=tmp_path)


def main(monkeypatch, capfd, argv):
    monkeypatch.setattr(sys, "argv", ["durumari-mcp", *argv])
    code = 0
    try:
        cli.main()
    except SystemExit as exit:
        code = exit.code or 0
    out, err = capfd.readouterr()
    return code, out, err


def probe(**expected):
    """A child that exits 0 only when each variable has the expected value's digest."""
    digests = {k: hashlib.sha256(v.encode()).hexdigest() for k, v in expected.items()}
    script = (
        "import hashlib, json, os, sys\n"
        "want = json.loads(sys.argv[1])\n"
        "ok = all(hashlib.sha256(os.environ.get(k, '').encode()).hexdigest() == v "
        "for k, v in want.items())\n"
        "sys.exit(0 if ok else 9)"
    )
    return [sys.executable, "-c", script, json.dumps(digests)]


def assert_value_free(data, *texts):
    for value in (VALUE, OTHER, NEWER):
        assert all(value not in text for text in texts)
        for path in data.root.rglob("*"):
            if path.is_file() and path.name != ".env":
                assert value.encode() not in path.read_bytes()


# run --------------------------------------------------------------------------------


def test_run_injects_only_into_the_child_and_prints_nothing(env, monkeypatch, capfd):
    code, out, err = main(
        monkeypatch,
        capfd,
        ["secret", "run", "--env", "API_KEY", "--env", "PGPASSWORD=DB_PASSWORD", "--"]
        + probe(API_KEY=VALUE, PGPASSWORD=OTHER),
    )
    assert code == 0, err
    assert out == err == ""
    assert env.server.deliveries() == 1
    assert "API_KEY" not in os.environ and "PGPASSWORD" not in os.environ
    assert_value_free(env, out, err)
    # Device keys and cached values live only in the OS store, bound to the connection.
    services = {service for service, _ in env.store.items}
    assert services == {"DURUMARI.AgentSecretDevice.v1", CACHE}


def test_run_passes_the_child_exit_code_and_its_own_output(env, monkeypatch, capfd):
    script = "import sys; print('child output'); sys.exit(7)"
    code, out, err = main(
        monkeypatch,
        capfd,
        ["secret", "run", "--env", "API_KEY", "--", sys.executable, "-c", script],
    )
    assert code == 7
    assert out == "child output\n" and VALUE not in out + err


def test_second_run_uses_the_cache_and_a_new_version_refetches(env, monkeypatch, capfd):
    argv = ["secret", "run", "--env", "API_KEY", "--"]
    assert main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))[0] == 0
    assert main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))[0] == 0
    assert env.server.deliveries() == 1
    env.server.secrets[API].update(value=NEWER, revision=2)
    code, out, err = main(monkeypatch, capfd, argv + probe(API_KEY=NEWER))
    assert code == 0, err
    assert env.server.deliveries() == 2
    assert VALUE not in env.store.text()
    assert_value_free(env, out, err)


def test_after_a_replace_offline_keeps_the_old_value_only_until_online(env, monkeypatch, capfd):
    # a replace approved on the secure screen raises the revision.
    # Offline, the device cannot learn that, so the unexpired cached value is still used
    # (bounded by the cache lifetime); the first online use refetches and drops it.
    argv = ["secret", "run", "--env", "API_KEY", "--"]
    assert main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))[0] == 0
    env.server.secrets[API].update(value=NEWER, revision=2)
    env.server.offline = True
    code, out, err = main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))
    assert code == 0, err
    env.server.offline = False
    code, out, err = main(monkeypatch, capfd, argv + probe(API_KEY=NEWER))
    assert code == 0, err
    assert VALUE not in env.store.text()
    assert_value_free(env, out, err)


def test_grant_change_and_lost_permission_invalidate_the_cache(env, monkeypatch, capfd):
    argv = ["secret", "run", "--env", "API_KEY", "--"]
    assert main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))[0] == 0
    env.server.secrets[API]["policy_revision"] = 2
    assert main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))[0] == 0
    assert env.server.deliveries() == 2
    env.server.permissions[API] = ["metadata"]
    code, out, err = main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))
    assert code == 2
    assert "secret_use_denied" in err and "API_KEY" in err
    assert VALUE not in env.store.text()
    assert_value_free(env, out, err)


def test_offline_uses_unexpired_cache_for_a_known_target_only(env, monkeypatch, capfd):
    argv = ["secret", "run", "--env", "API_KEY", "--"]
    assert main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))[0] == 0
    env.server.offline = True
    code, out, err = main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))
    assert code == 0, err
    # Another command is another target: its first use needs the server.
    other = ["secret", "run", "--env", "API_KEY", "--", "git", "--version"]
    code, out, err = main(monkeypatch, capfd, other)
    assert code == 2 and "secret_offline_unavailable" in err
    # Expired entries are removed, not used.
    cache = SecretCache(CONNECTION, PROJECT, store=env.store)
    index = cache.index()
    index[API]["expires_at"] = int(time.time()) - 1
    cache._save_index(index)
    code, out, err = main(monkeypatch, capfd, argv + probe(API_KEY=VALUE))
    assert code == 2 and "secret_offline_unavailable" in err
    assert VALUE not in env.store.text()
    assert_value_free(env, out, err)


def test_explicit_offline_never_calls_the_server(env, monkeypatch, capfd):
    code, out, err = main(
        monkeypatch, capfd, ["secret", "run", "--offline", "--env", "API_KEY", "--", "true"]
    )
    assert code == 2 and "secret_offline_unavailable" in err
    assert env.server.calls == []


def test_server_refusals_and_tampered_envelopes_fail_closed(env, monkeypatch, capfd):
    argv = ["secret", "run", "--env", "API_KEY", "--"] + probe(API_KEY=VALUE)
    env.server.deny = "target_approval_required"
    code, out, err = main(monkeypatch, capfd, argv)
    assert code == 2 and "target_approval_required" in err and "API_KEY" in err
    env.server.deny = None
    env.server.tamper = True
    code, out, err = main(monkeypatch, capfd, argv)
    assert code == 2 and "invalid_device_envelope" in err
    assert VALUE not in env.store.text()
    assert_value_free(env, out, err)


def test_run_reads_the_template_and_rejects_reserved_names(env, monkeypatch, capfd):
    (env.root / ".env.durumari").write_text(
        "# synthetic\nAPI_KEY=durumari://API_KEY\nMODE=development\n", encoding="utf-8"
    )
    code, out, err = main(
        monkeypatch, capfd, ["secret", "run", "--"] + probe(API_KEY=VALUE, MODE="development")
    )
    assert code == 0, err
    for name in (
        "PATH=API_KEY",
        "LD_PRELOAD=API_KEY",
        "DURUMARI_MCP_X=API_KEY",
        "DURUMARI_UV_EXECUTABLE=API_KEY",
        "1BAD",
    ):
        code, out, err = main(monkeypatch, capfd, ["secret", "run", "--env", name, "--", "true"])
        assert code == 2 and "invalid_env_name" in err
    assert_value_free(env, out, err)


def test_unknown_names_and_missing_command_are_fixed_errors(env, monkeypatch, capfd):
    code, out, err = main(monkeypatch, capfd, ["secret", "run", "--env", "NOPE", "--", "true"])
    assert code == 2 and "secret_not_found" in err and "NOPE" in err
    code, out, err = main(monkeypatch, capfd, ["secret", "run", "--env", "API_KEY"])
    assert code == 2 and "run_command_required" in err
    assert env.server.deliveries() == 0


# cache ------------------------------------------------------------------------------


def test_long_values_are_split_under_the_windows_credential_limit():
    store = Store(limit=2560)
    text = "w" * 5000 + "한글" * 300
    put_blob(store, CACHE, "value:x", text)
    assert get_blob(store, CACHE, "value:x") == text
    assert all(len(v.encode("utf-16-le")) <= 2560 for v in store.items.values())
    # A torn write (one part replaced) reads as a miss, never as another value.
    store.items[(CACHE, "value:x#1")] = "z" * 1000
    assert get_blob(store, CACHE, "value:x") is None


def test_cache_status_and_clear_show_names_only(env, monkeypatch, capfd):
    assert main(monkeypatch, capfd, ["secret", "run", "--env", "API_KEY", "--", "true"])[0] == 0
    code, out, err = main(monkeypatch, capfd, ["secret", "cache", "status"])
    assert code == 0, err
    status = json.loads(out)
    assert status["device_id"] == DEVICE
    assert [(i["label"], i["revision"], i["targets"]) for i in status["items"]] == [
        ("API_KEY", 1, 1)
    ]
    code, out, err = main(monkeypatch, capfd, ["secret", "cache", "clear", "--json"])
    assert json.loads(out) == {"status": "cleared", "removed": 1}
    assert VALUE not in env.store.text()
    assert_value_free(env, out, err)


@pytest.mark.parametrize(
    "platform,module,name",
    [
        ("win32", "keyring.backends.Windows", "WinVaultKeyring"),
        ("darwin", "keyring.backends.macOS", "Keyring"),
        ("linux", "keyring.backends.SecretService", "Keyring"),
    ],
)
def test_cache_uses_only_the_platform_os_store(monkeypatch, platform, module, name):
    import importlib
    import importlib.util

    created = []

    class Backend(Store):
        priority = 5

        def __init__(self):
            super().__init__()
            created.append(self)

    monkeypatch.setattr(sys, "platform", platform)
    if platform == "darwin" and importlib.util.find_spec("durumari_mcp.macos_credentials"):
        module = "durumari_mcp.macos_credentials"
    monkeypatch.setattr(importlib.import_module(module), name, Backend)
    SecretCache(CONNECTION, PROJECT).device_keys(create=True)
    [store] = created
    assert [service for service, _ in store.items] == ["DURUMARI.AgentSecretDevice.v1"]
    monkeypatch.setattr(sys, "platform", "freebsd")
    with pytest.raises(AdapterError, match="protected_store_unavailable"):
        SecretCache(CONNECTION, PROJECT)


# env --------------------------------------------------------------------------------


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="git required")


def template(root, text=None):
    (root / ".env.durumari").write_text(
        text
        or "# synthetic\nexport API_KEY=durumari://API_KEY\n"
        "DB_PASSWORD='durumari://DB_PASSWORD'  # db\nMODE=development\n",
        encoding="utf-8",
    )


@needs_git
def test_env_writes_a_gitignored_owner_only_file(env, monkeypatch, capfd):
    git(env.root, "init", "-q")
    (env.root / ".gitignore").write_text(".env\n")
    template(env.root)
    code, out, err = main(monkeypatch, capfd, ["secret", "env", "--json"])
    assert code == 0, err
    receipt = json.loads(out)
    assert receipt["names"] == ["API_KEY", "DB_PASSWORD"] and receipt["gitignore"] == "ignored"
    written = (env.root / ".env").read_text(encoding="utf-8")
    assert written == (
        f"# synthetic\nexport API_KEY={VALUE}\nDB_PASSWORD={OTHER}\nMODE=development\n"
    )
    if sys.platform != "win32":
        assert stat.S_IMODE((env.root / ".env").stat().st_mode) == 0o600
    assert list(env.root.glob(".*.tmp")) == []
    assert_value_free(env, out, err)


@needs_git
def test_env_refuses_a_file_git_would_commit(env, monkeypatch, capfd):
    git(env.root, "init", "-q")
    template(env.root)
    code, out, err = main(monkeypatch, capfd, ["secret", "env"])
    assert code == 2 and "env_not_gitignored" in err
    assert not (env.root / ".env").exists()
    (env.root / ".env").write_text("TRACKED=1\n")
    git(env.root, "add", ".env")
    (env.root / ".gitignore").write_text(".env\n")
    code, out, err = main(monkeypatch, capfd, ["secret", "env", "--force"])
    assert code == 2 and "env_tracked_by_git" in err
    assert (env.root / ".env").read_text() == "TRACKED=1\n"
    assert env.server.deliveries() == 0


@needs_git
def test_env_asks_before_overwriting_and_never_follows_links(env, monkeypatch, capfd):
    git(env.root, "init", "-q")
    (env.root / ".gitignore").write_text(".env\n")
    template(env.root)
    (env.root / ".env").write_text("OLD=1\n")
    code, out, err = main(monkeypatch, capfd, ["secret", "env"])
    assert code == 2 and "env_exists_confirm_required" in err
    assert (env.root / ".env").read_text() == "OLD=1\n"
    code, out, err = main(monkeypatch, capfd, ["secret", "env", "--force"])
    assert code == 0, err
    assert VALUE in (env.root / ".env").read_text()
    if sys.platform != "win32":
        (env.root / ".env").unlink()
        (env.root / "elsewhere").write_text("keep\n")
        (env.root / ".env").symlink_to(env.root / "elsewhere")
        code, out, err = main(monkeypatch, capfd, ["secret", "env", "--force"])
        assert code == 2 and "env_output_unsafe" in err
        assert (env.root / "elsewhere").read_text() == "keep\n"
    assert_value_free(env, out, err)


@pytest.mark.parametrize(
    "text",
    [
        "URL=https://x@durumari://API_KEY\n",
        "API_KEY=durumari://API_KEY extra\n",
        "API_KEY=\"durumari://API_KEY'\n",
    ],
)
def test_partial_references_are_rejected(env, monkeypatch, capfd, text):
    template(env.root, text)
    code, out, err = main(monkeypatch, capfd, ["secret", "env", "--output", "out.env"])
    assert code == 2 and "invalid_template_line" in err and "line 1" in err
    assert env.server.calls == []


def test_dotenv_quoting_keeps_values_literal():
    assert secret_env.quote("abc-123_./:@+=") == "abc-123_./:@+="
    assert secret_env.quote("a b$HOME") == "'a b$HOME'"
    assert secret_env.quote("it's\nmulti") == '"it\'s\\nmulti"'
    assert secret_env.quote('q"\\') == "'q\"\\'"


def test_env_outside_a_repository_is_allowed_and_noted(env, monkeypatch, capfd, tmp_path):
    monkeypatch.setattr(secret_env, "inside_repository", lambda directory: False)
    monkeypatch.setattr(secret_env, "git", lambda directory, *args: None)
    template(env.root, "API_KEY=durumari://API_KEY\n")
    code, out, err = main(monkeypatch, capfd, ["secret", "env"])
    assert code == 0, err
    assert "gitignore" in err
    assert (env.root / ".env").read_text() == f"API_KEY={VALUE}\n"
    # git missing inside a repository cannot prove the file is ignored.
    monkeypatch.setattr(secret_env, "inside_repository", lambda directory: True)
    code, out, err = main(monkeypatch, capfd, ["secret", "env", "--force"])
    assert code == 2 and "gitignore_check_unavailable" in err


def test_target_identity_hides_command_arguments(tmp_path):
    first = secret_env.run_target(tmp_path, ["--", "npm", "run", "deploy", "--token=x"])
    second = secret_env.run_target(tmp_path, ["npm", "test"])
    assert first == second and first.label == "run: npm"
    assert secret_env.env_target(tmp_path, tmp_path / ".env").label == "env: .env"
    assert (
        len({first.fingerprint, secret_env.env_target(tmp_path, tmp_path / ".env").fingerprint})
        == 2
    )


def test_resolved_repr_never_contains_values():
    from durumari_mcp.secret_use import Resolved

    resolved = Resolved(values={"API_KEY": VALUE})
    assert VALUE not in repr(resolved) and VALUE not in str(resolved)


def test_devices_route_is_allowed_for_the_gateway():
    import re

    from durumari_mcp import project_gateway

    source = Path(project_gateway.__file__).read_text()
    assert "secret-devices" in source
    assert re.fullmatch(
        r"/api/v2/agent/secret-devices(?:/[a-f0-9-]{36}/(?:challenges|deliveries))?",
        "/api/v2/agent/secret-devices/" + DEVICE + "/deliveries",
    )


def test_a_forgotten_device_id_re_enrolls_the_same_keys_once(env, monkeypatch, capfd):
    argv = ["secret", "run", "--env", "API_KEY", "--"] + probe(API_KEY=VALUE)
    assert main(monkeypatch, capfd, argv)[0] == 0
    first = dict(env.server.keys)
    real = env.server.context_request
    lost = {"done": False}

    def forget_once(path, body=None, *, method="GET", idempotency_key=None):
        if path.endswith("/challenges") and not lost["done"]:
            lost["done"] = True
            raise AdapterError("device_access_denied")
        return real(path, body, method=method, idempotency_key=idempotency_key)

    env.server.context_request = forget_once
    SecretCache(CONNECTION, PROJECT, store=env.store).clear()
    code, out, err = main(monkeypatch, capfd, argv)
    assert code == 0, err
    assert env.server.keys == first
    assert_value_free(env, out, err)


# redaction & tier restrictions (#30) ------------------------------------------------


def test_run_redacts_secret_raw_base64_and_url_from_stdout_and_stderr(env, monkeypatch, capfd):
    special = "synth/run+secret=value 99"
    env.server.secrets[API]["value"] = special
    script = (
        "import base64, os, sys, urllib.parse\n"
        "v = os.environ['API_KEY']\n"
        "print('raw:' + v)\n"
        "print('b64:' + base64.b64encode(v.encode()).decode())\n"
        "print('url:' + urllib.parse.quote(v, safe=''))\n"
        "print('safe:ok')\n"
        "sys.stderr.write('err:' + v + '\\n')\n"
        "u = base64.urlsafe_b64encode(v.encode()).decode().rstrip('=')\n"
        "sys.stderr.write('err_b64:' + u + '\\n')\n"
    )
    code, out, err = main(
        monkeypatch,
        capfd,
        ["secret", "run", "--env", "API_KEY", "--", sys.executable, "-c", script],
    )
    assert code == 0
    assert out == "raw:[REDACTED]\nb64:[REDACTED]\nurl:[REDACTED]\nsafe:ok\n"
    assert err == "err:[REDACTED]\nerr_b64:[REDACTED]\n"
    assert special not in out + err
    assert base64.b64encode(special.encode()).decode() not in out + err


def test_run_redacts_secret_written_in_tiny_flushed_chunks(env, monkeypatch, capfd):
    script = (
        "import os, sys\n"
        "v = 'prefix-' + os.environ['API_KEY'] + '-suffix\\n'\n"
        "for ch in v:\n"
        "    sys.stdout.write(ch)\n"
        "    sys.stdout.flush()\n"
    )
    code, out, err = main(
        monkeypatch,
        capfd,
        ["secret", "run", "--env", "API_KEY", "--", sys.executable, "-c", script],
    )
    assert code == 0, err
    assert out == "prefix-[REDACTED]-suffix\n"
    assert VALUE not in out + err


@pytest.mark.parametrize("tier", ["recovery", "runtime", "unknown"])
@needs_git
def test_env_refuses_non_development_tiers_without_delivering_or_writing(
    env, monkeypatch, capfd, tier
):
    git(env.root, "init", "-q")
    (env.root / ".gitignore").write_text(".env\n", encoding="utf-8")
    template(env.root, "API_KEY=durumari://API_KEY\n")
    env.server.tiers[API] = tier
    code, out, err = main(monkeypatch, capfd, ["secret", "env"])
    assert code == 2
    expected_code = "secret_tier_forbidden" if tier == "recovery" else "secret_env_tier_forbidden"
    assert expected_code in err and "API_KEY" in err
    assert not (env.root / ".env").exists()
    assert env.server.deliveries() == 0
    assert_value_free(env, out, err)


@needs_git
def test_env_refuses_policy_forbidden_secret_and_allows_development_tier(env, monkeypatch, capfd):
    git(env.root, "init", "-q")
    (env.root / ".gitignore").write_text(".env\n", encoding="utf-8")
    template(env.root, "API_KEY=durumari://API_KEY\n")
    env.server.tiers[API] = "development"
    env.server.env_allowed[API] = False
    code, out, err = main(monkeypatch, capfd, ["secret", "env"])
    assert code == 2 and "secret_env_tier_forbidden" in err and "API_KEY" in err
    assert not (env.root / ".env").exists()
    assert env.server.deliveries() == 0

    env.server.env_allowed[API] = True
    code, out, err = main(monkeypatch, capfd, ["secret", "env", "--json"])
    assert code == 0, err
    assert (env.root / ".env").read_text(encoding="utf-8") == f"API_KEY={VALUE}\n"


@needs_git
def test_env_offline_refuses_cached_runtime_secret(env, monkeypatch, capfd):
    git(env.root, "init", "-q")
    (env.root / ".gitignore").write_text(".env\n", encoding="utf-8")
    template(env.root, "API_KEY=durumari://API_KEY\n")
    env.server.tiers[API] = "runtime"
    # Runtime secrets may still be used via `secret run`, which may populate cache if server allows.
    assert main(monkeypatch, capfd, ["secret", "run", "--env", "API_KEY", "--", "true"])[0] == 0
    code, out, err = main(monkeypatch, capfd, ["secret", "env", "--offline"])
    assert code == 2 and "secret_env_tier_forbidden" in err and "API_KEY" in err
    assert not (env.root / ".env").exists()


def test_run_refuses_recovery_tier_and_clears_cache(env, monkeypatch, capfd):
    assert main(monkeypatch, capfd, ["secret", "run", "--env", "API_KEY", "--", "true"])[0] == 0
    assert VALUE in env.store.text()
    env.server.tiers[API] = "recovery"
    code, out, err = main(monkeypatch, capfd, ["secret", "run", "--env", "API_KEY", "--", "true"])
    assert code == 2 and "secret_tier_forbidden" in err and "API_KEY" in err
    assert VALUE not in env.store.text()
    assert_value_free(env, out, err)


def test_zero_cache_seconds_never_persists_value_in_os_store(env, monkeypatch, capfd):
    env.server.cache_seconds = 0
    code, out, err = main(
        monkeypatch, capfd, ["secret", "run", "--env", "API_KEY", "--"] + probe(API_KEY=VALUE)
    )
    assert code == 0, err
    assert VALUE not in env.store.text()
    assert SecretCache(CONNECTION, PROJECT, store=env.store).index() == {}

