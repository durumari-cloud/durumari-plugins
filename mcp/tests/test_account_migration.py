"""Account migration never retires a folder before an observed MCP read."""

import json
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from durumari_mcp import account_migration as migration
from durumari_mcp.credentials import AdapterError


@pytest.fixture
def environment(tmp_path, monkeypatch):
    root, home, data = (tmp_path / name for name in ("work", "home", "data"))
    root.mkdir()
    home.mkdir()
    account = SimpleNamespace(
        connection_id="account-id",
        actor_id="actor",
        client="codex",
        gateway_origin="https://durumari.kr",
        outbox_key=SecretStr(Fernet.generate_key().decode()),
    )
    legacy = SimpleNamespace(
        connection_id="old-id",
        actor_id="actor",
        project_id="project-id",
        workspace=str(root),
        gateway_origin="https://durumari.kr",
    )
    monkeypatch.setattr(migration, "load_account_profile", lambda name: account)
    monkeypatch.setattr(migration, "load_project_profile", lambda name: legacy)
    monkeypatch.setattr(migration, "AccountContextClient", lambda name, client: Account(account))
    monkeypatch.setattr(migration, "user_data_directory", lambda: data)
    monkeypatch.setattr(
        migration, "pending_writes", lambda name: SimpleNamespace(count=0, recovery_required=False)
    )
    monkeypatch.setattr(migration.Path, "home", lambda: home)
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text('[mcp_servers.durumari_account]\ncommand="account"\n')
    installation = data / "account-setup/codex/installed.json"
    installation.parent.mkdir(parents=True)
    installation.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "client": "codex",
                "profile": "personal",
                "server": "https://durumari.kr",
                "entry": {"command": "account"},
            }
        )
    )
    (root / ".durumari").mkdir()
    (root / ".durumari/context-binding.json").write_text(
        json.dumps({"profile": "legacy", "agents": ["codex"], "created_files": []})
    )
    (root / ".codex").mkdir()
    (root / ".codex/config.toml").write_text(
        '# user preference\nmodel="keep"\n[mcp_servers.durumari_project]\ncommand="python"\n'
        'args=["-m", "durumari_mcp", "context-serve", "--profile", "legacy"]\n'
        '# personal comment\n[mcp_servers.other]\ncommand="other"\n'
    )
    (root / ".codex/hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "python -m durumari_mcp project-hook --profile legacy"
                                    ),
                                },
                                {"type": "command", "command": "other"},
                            ]
                        }
                    ]
                }
            }
        )
    )
    (root / "AGENTS.md").write_text("User instructions: keep exactly\n")
    (root / "pending.bin").write_bytes(b"opaque-pending-request")
    return root, account, legacy


def planned(environment):
    root, _, _ = environment
    return migration.plan(root, "codex", "personal")["migration_id"]


class Account:
    name, client = "personal", "codex"

    def __init__(self, account):
        self.profile = account
        self.calls = []

    def status(self):
        return {"state": "active"}

    def projects(self, action, body):
        self.calls.append(action)
        return (
            {"conversation_ref": "conversation", "revision": 0}
            if action == "start"
            else {"project_ref": "selected"}
        )

    def targeted(self, ref):
        assert ref == "selected"
        return self

    def direct_target(self, project):
        assert project == "project-id"
        return self

    def read(self, **kwargs):
        self.calls.append("read")
        return {"items": []}


def verified(environment):
    mid = planned(environment)
    migration.verify(mid, Account(environment[1]))
    return mid


def test_plan_is_read_only_and_apply_needs_host_read(environment):
    root, _, _ = environment
    before = (root / ".codex/config.toml").read_bytes()
    mid = planned(environment)
    assert (root / ".codex/config.toml").read_bytes() == before
    with pytest.raises(AdapterError, match="account_migration_host_verification_required"):
        migration.apply(mid, "codex", "personal")
    assert (root / ".codex/config.toml").read_bytes() == before


def test_crlf_bytes_survive_plan_apply_and_rollback(environment):
    root, _, _ = environment
    originals = {}
    for name in (".codex/config.toml", ".codex/hooks.json", ".durumari/context-binding.json"):
        path = root / name
        value = path.read_bytes().replace(b"\n", b"\r\n")
        path.write_bytes(value)
        originals[name] = value
    mid = verified(environment)
    migration.apply(mid, "codex", "personal")
    result = (root / ".codex/config.toml").read_bytes()
    assert b"\r\n" in result and b"\n" not in result.replace(b"\r\n", b"")
    migration.rollback(mid, "codex", "personal")
    assert all((root / name).read_bytes() == value for name, value in originals.items())


def test_old_shared_blocks_become_generic_without_changing_user_imports(environment):
    root, _, _ = environment
    start, end = migration.instruction_files.START, migration.instruction_files.END
    source = (
        "User source\n"
        + start
        + "\nProject: project-id\nOld fixed guidance\n"
        + end
        + "\nEnd user source\n"
    )
    importer = "@AGENTS.md\nUser importer\n" + start + "\nOld copied rules\n" + end + "\n"
    (root / "AGENTS.md").write_text(source)
    (root / "CLAUDE.md").write_text(importer)
    mid = verified(environment)
    migration.apply(mid, "codex", "personal")
    updated = (root / "AGENTS.md").read_text()
    assert updated.startswith("User source\n") and updated.endswith("\nEnd user source\n")
    assert "target_project_id" in updated and "personal" not in updated
    assert (root / "CLAUDE.md").read_text().count("@AGENTS.md") == 1
    migration.rollback(mid, "codex", "personal")
    assert (root / "AGENTS.md").read_text() == source
    assert (root / "CLAUDE.md").read_text() == importer


def test_damaged_markers_defer_retirement_without_writing_files(environment):
    root, _, _ = environment
    (root / "AGENTS.md").write_text(migration.instruction_files.START + "\nDamaged")
    before = (root / ".codex/config.toml").read_bytes()
    with pytest.raises(AdapterError, match="account_migration_instruction_repair_required"):
        planned(environment)
    assert (root / ".codex/config.toml").read_bytes() == before


def test_foreign_instruction_owner_is_not_rewritten(environment):
    root, _, _ = environment
    text = migration.instruction_files.agents_block("another-project")
    (root / "AGENTS.md").write_text(text)
    with pytest.raises(AdapterError, match="account_migration_instruction_ownership_required"):
        planned(environment)
    assert (root / "AGENTS.md").read_text() == text


def test_reverification_after_partial_apply_keeps_rollback_available(environment, monkeypatch):
    root, _, _ = environment
    mid = verified(environment)
    original = migration.atomic
    interrupted = False

    def replace_then_stop(path, data, **kwargs):
        nonlocal interrupted
        original(path, data, **kwargs)
        if path == root / ".codex/config.toml" and not interrupted:
            interrupted = True
            raise OSError("interrupted")

    monkeypatch.setattr(migration, "atomic", replace_then_stop)
    with pytest.raises(OSError):
        migration.apply(mid, "codex", "personal")
    assert migration.verify(mid, Account(environment[1]))["state"] == "applying"
    assert migration.rollback(mid, "codex", "personal")["state"] == "rolled_back"
    assert "durumari_project" in (root / ".codex/config.toml").read_text()


def test_apply_keeps_user_settings_instructions_credentials_and_pending(environment):
    root, _, _ = environment
    mid = verified(environment)
    assert migration.apply(mid, "codex", "personal")["state"] == "completed"
    text = (root / ".codex/config.toml").read_text()
    assert "durumari_project" not in text and "mcp_servers.other" in text
    assert "# personal comment" in text and 'model="keep"' in text
    assert (root / "AGENTS.md").read_text() == "User instructions: keep exactly\n"
    assert (root / "pending.bin").read_bytes() == b"opaque-pending-request"
    hooks = (root / ".codex/hooks.json").read_text()
    assert "project-hook" not in hooks and "other" in hooks
    assert migration.apply(mid, "codex", "personal")["state"] == "completed"
    assert migration.rollback(mid, "codex", "personal")["state"] == "rolled_back"
    assert "durumari_project" in (root / ".codex/config.toml").read_text()


def test_account_switch_cannot_retire_old_identity(environment):
    mid = planned(environment)
    environment[1].actor_id = "different-user"
    with pytest.raises(AdapterError, match="account_migration_identity_changed"):
        migration.verify(mid, Account(environment[1]))


@pytest.mark.parametrize("state", ["rolled_back", "rolling_back"])
def test_rollback_cannot_be_reversed_using_old_host_proof(environment, state):
    root, _, _ = environment
    mid = verified(environment)
    migration.apply(mid, "codex", "personal")
    migration.rollback(mid, "codex", "personal")
    row = migration._load(mid, "codex", "personal")
    row["state"] = state
    migration._save(row)
    before = (root / ".codex/config.toml").read_bytes()
    with pytest.raises(AdapterError, match="account_migration_host_verification_required"):
        migration.apply(mid, "codex", "personal")
    assert (root / ".codex/config.toml").read_bytes() == before


def test_changed_settings_are_not_overwritten(environment):
    root, _, _ = environment
    mid = verified(environment)
    path = root / ".codex/hooks.json"
    path.write_text('{"user":"concurrent"}')
    with pytest.raises(AdapterError, match="account_migration_configuration_changed"):
        migration.apply(mid, "codex", "personal")
    assert "durumari_project" in (root / ".codex/config.toml").read_text()
    assert path.read_text() == '{"user":"concurrent"}'


def test_rollback_preserves_edits_made_after_migration(environment):
    root, _, _ = environment
    mid = verified(environment)
    migration.apply(mid, "codex", "personal")
    path = root / ".codex/config.toml"
    path.write_text(path.read_text() + "# new user setting\n")
    with pytest.raises(AdapterError, match="account_migration_configuration_changed"):
        migration.rollback(mid, "codex", "personal")
    assert path.read_text().endswith("# new user setting\n")


def test_journal_is_encrypted_and_interrupted_apply_resumes(environment, monkeypatch):
    root, _, _ = environment
    mid = verified(environment)
    original = migration.atomic
    failed = False

    def interrupted(path, data, **kwargs):
        nonlocal failed
        original(path, data, **kwargs)
        if path == root / ".codex/config.toml" and not failed:
            failed = True
            raise OSError("interrupted after replace")

    monkeypatch.setattr(migration, "atomic", interrupted)
    with pytest.raises(OSError):
        migration.apply(mid, "codex", "personal")
    assert migration.apply(mid, "codex", "personal")["state"] == "completed"
    assert not any(
        b"personal comment" in p.read_bytes()
        for p in migration.user_data_directory().rglob("*.journal")
    )


def test_unowned_global_entry_is_not_accepted(environment):
    path = migration.Path.home() / ".codex/config.toml"
    path.write_text('[mcp_servers.durumari_account]\ncommand="foreign"\n')
    with pytest.raises(AdapterError, match="account_migration_account_setup_required"):
        planned(environment)


def test_unrelated_host_state_does_not_invalidate_account_proof(environment):
    mid = planned(environment)
    path = migration.Path.home() / ".codex/config.toml"
    path.write_text(path.read_text() + '\n[projects."/tmp/synthetic"]\ntrust_level="trusted"\n')
    migration.verify(mid, Account(environment[1]))
    path.write_text('model="synthetic-model"\n' + path.read_text())
    assert migration.apply(mid, "codex", "personal")["state"] == "completed"
    assert 'model="synthetic-model"' in path.read_text()


def test_changed_account_entry_still_invalidates_the_plan(environment):
    mid = verified(environment)
    path = migration.Path.home() / ".codex/config.toml"
    path.write_text('[mcp_servers.durumari_account]\ncommand="new-account"\n')
    installed = migration.user_data_directory() / "account-setup/codex/installed.json"
    value = json.loads(installed.read_text())
    value["entry"]["command"] = "new-account"
    installed.write_text(json.dumps(value))
    with pytest.raises(AdapterError, match="account_migration_configuration_changed"):
        migration.apply(mid, "codex", "personal")


@pytest.mark.parametrize(
    "client,config", [("claude-code", ".mcp.json"), ("cursor", ".cursor/mcp.json")]
)
def test_json_hosts_keep_comments_and_foreign_servers(environment, client, config):
    root, account, _ = environment
    account.client = client
    home = migration.Path.home()
    path = home / (".claude.json" if client == "claude-code" else ".cursor/mcp.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"mcpServers":{"durumari_account":{"command":"account"}}}')
    installation = migration.user_data_directory() / "account-setup" / client / "installed.json"
    installation.parent.mkdir(parents=True)
    installation.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "client": client,
                "profile": "personal",
                "server": "https://durumari.kr",
                "entry": {"command": "account"},
            }
        )
    )
    local = root / config
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text(
        '{/* personal comment */"mcpServers":{"other":{"command":"keep"},'
        '"durumari_project":{"command":"python","args":["-m","durumari_mcp",'
        '"context-serve","--profile","legacy"]}}}'
    )
    plan = migration.plan(root, client, "personal")
    host_state = json.loads(path.read_text())
    host_state["numStartups"] = 2
    path.write_text(json.dumps(host_state))
    operations = Account(account)
    operations.client = client
    migration.verify(plan["migration_id"], operations)
    host_state["numStartups"] = 3
    path.write_text(json.dumps(host_state))
    migration.apply(plan["migration_id"], client, "personal")
    assert "personal comment" in local.read_text() and '"keep"' in local.read_text()
    assert "durumari_project" not in local.read_text()
    assert json.loads(path.read_text())["numStartups"] == 3


def test_revoked_permission_does_not_unlock_apply(environment, monkeypatch):
    mid = planned(environment)
    client = Account(environment[1])

    def denied(**kwargs):
        raise AdapterError("forbidden")

    monkeypatch.setattr(client, "read", denied)
    with pytest.raises(AdapterError, match="forbidden"):
        migration.verify(mid, client)
    assert migration.status(mid, "codex", "personal")["state"] == "planned"


def test_revocation_between_verification_and_apply_keeps_old_connection(environment, monkeypatch):
    mid = verified(environment)

    def revoked(*args):
        raise AdapterError("account_reconnect_required")

    monkeypatch.setattr(migration, "AccountContextClient", revoked)
    with pytest.raises(AdapterError, match="account_reconnect_required"):
        migration.apply(mid, "codex", "personal")
    assert "durumari_project" in (environment[0] / ".codex/config.toml").read_text()


def test_expired_verification_keeps_the_folder(environment, monkeypatch):
    mid = verified(environment)
    now = migration.time.time()
    monkeypatch.setattr(migration.time, "time", lambda: now + 901)
    with pytest.raises(AdapterError, match="host_verification_required"):
        migration.apply(mid, "codex", "personal")
    assert "durumari_project" in (environment[0] / ".codex/config.toml").read_text()


def test_symlink_cannot_redirect_planning(environment, tmp_path):
    root = environment[0]
    path = root / ".codex/config.toml"
    foreign = tmp_path / "foreign"
    foreign.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(foreign)
    with pytest.raises(AdapterError, match="unsafe_account_setup_path"):
        planned(environment)
    assert "durumari_project" in foreign.read_text()


@pytest.mark.anyio
async def test_only_native_account_mcp_invocation_observes_read(environment):
    from mcp import Client
    from mcp.server.mcpserver import MCPServer

    mid = planned(environment)
    operations = Account(environment[1])
    server = MCPServer("migration-fixture")
    migration.register(server, operations)
    async with Client(server) as client:
        assert "durumari_account_migration_verify" in {
            tool.name for tool in (await client.list_tools()).tools
        }
        result = await client.call_tool("durumari_account_migration_verify", {"migration_id": mid})
        assert not result.is_error
        assert json.loads(result.content[0].text)["authorized_read"] == "verified"
    assert "read" in operations.calls
    assert migration.status(mid, "codex", "personal")["state"] == "verified"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize(("count", "recovery"), [(2, False), (0, True)])
def test_unconfirmed_folder_writes_stop_planning_and_applying(
    environment, monkeypatch, count, recovery
):
    root, _, _ = environment
    mid = verified(environment)
    before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
    pending = SimpleNamespace(count=count, recovery_required=recovery)
    monkeypatch.setattr(migration, "pending_writes", lambda name: pending)
    with pytest.raises(AdapterError, match="account_migration_pending_writes"):
        migration.plan(root, "codex", "personal")
    with pytest.raises(AdapterError, match="account_migration_pending_writes"):
        migration.apply(mid, "codex", "personal")
    assert {path: path.read_bytes() for path in root.rglob("*") if path.is_file()} == before


def test_a_folder_bound_before_the_move_migrates_to_an_account_made_after_it(environment):
    root, account, _ = environment
    account.gateway_origin = "https://app.durumari.cloud"
    installation = migration.user_data_directory() / "account-setup/codex/installed.json"
    value = json.loads(installation.read_text())
    installation.write_text(json.dumps({**value, "server": "https://app.durumari.cloud"}))
    assert migration.plan(root, "codex", "personal")["state"] == "planned"
    # Another host is another server, even with an account set up for it.
    account.gateway_origin = "https://durumari.cloud"
    installation.write_text(json.dumps({**value, "server": "https://durumari.cloud"}))
    with pytest.raises(AdapterError, match="account_migration_identity_changed"):
        migration.plan(root, "codex", "personal")
