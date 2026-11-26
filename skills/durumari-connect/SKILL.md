---
name: durumari-connect
description: Connect this Agent to a Durumari account for use across work folders, safely transition an existing folder connection, or manage an explicitly requested folder binding. Use when the user says "두루마리 연결해 줘", "계정 연결", "계정 연결로 전환", "이 폴더 연결", "다른 컴퓨터에서도 쓰고 싶어", "권한 추가", "연결 해제", or status reports a missing or revoked connection. Explain the scope and let the human approve in the browser; never approve for them.
---

# Connect an Agent to Durumari

For Codex, Claude Code and Cursor, account setup is the default. The person approves
their account scope once in the browser, and the Agent can use authorized Context
from any work folder. Git, origin, AGENTS.md and folder approval are not prerequisites.
The server checks current permissions on every operation. An account connection does
not grant device, secret, filesystem or execution access.

## Default: plugin-provided remote account connection

Current Codex and Cursor discover the portable `mcp.json`; Claude Code reads
`mcp-claude.json` through its manifest. Both name the same remote account service.
Use the host's sign-in action when OAuth is required. The person completes login
and approves the displayed scope; never approve or enter credentials for them.

Inspect the available tools and their schemas first. When the plugin's remote
account tools are available, use their status and conversation/project selection
flow. Start a conversation only when the corresponding tool is listed, keep the
returned references, and let the person select a permitted space. Do not require
a Git repository, folder binding, or a local Python process for ordinary Context.

Do not add a second `durumari_account` entry just because remote sign-in is pending
or a request failed. If remote and local/folder providers coexist, report the
duplicate, preserve pending writes and settings, and use only the selected provider.
Removing an older provider requires its verified migration/recovery flow; package
installation is not proof that the new host discovered or authorized its tools.

If this host/version cannot load the remote plugin configuration, record that
limitation and offer the existing local connection below. Actual host OAuth,
two-folder use and duplicate-provider migration need separate acceptance checks.

## Optional local account connection

Explain that one `durumari_account` entry is added to this host's user MCP settings,
and the person approves the displayed scope in the browser. The user's explicit
request to connect already authorizes starting this flow; do not ask the same question
again. The person, never the Agent, completes OAuth/permission approval.

```sh
python3 <plugin>/scripts/durumari_plugin.py connect --client <codex|claude|cursor>
```

`claude` maps to native account client `claude-code`. The explicit `account-setup`
command below remains supported. An update alone never converts an old folder grant.
Other hosts retain explicit folder setup until account registration is verified for
that host; never claim it works by copying another host's configuration.

## Transition an existing folder

Keep the old connection until the new account's tools and permitted read work in the
actual host. Do not remove entries manually or scan for unknown working folders.
When `durumari_project_status` on the account connection reports `legacy_folder`, that
folder still serves duplicate folder tools; offer its `next_action` plan. Planning and
applying stop with `account_migration_pending_writes` while the folder has unconfirmed
Context writes: let the folder connection deliver or recover them first.
After account setup and host restart, plan only the person's selected absolute root:

```sh
python3 <plugin>/scripts/durumari_plugin.py account-migrate plan --client <codex|claude-code|cursor> --profile <account-profile> --workspace <absolute-root>
```

Show the returned file paths and before/after hashes. Call the account host's
`durumari_account_migration_verify` with that migration_id; it checks the account,
identity, global registration and an authorized read of the original project. A CLI
probe alone is not proof the person's host discovered its tools. After verification,
apply the reviewed plan using `account-migrate apply --client ... --profile ...
--migration-id ...` and restart the host. Confirm that one Context provider remains.

`account-migrate status` reports the saved stage, and `account-migrate rollback`
restores only unchanged entries this migration modified. A changed file stops recovery
rather than overwriting user edits. The journal is encrypted with the account's OS
stored key. Credentials, pending queues and other hosts remain untouched. Existing
AGENTS/CLAUDE/GEMINI managed blocks use the common instruction generator; user text
and manual imports stay. Review any tracked shared-format changes normally and never
commit personal migration state. Damaged markers or a missing shared source defer
the transition until the normal generator/review repairs it. A missing/changed identity,
unknown ownership, shared host file, expired proof or concurrent edit requires recovery,
not forced cleanup. Do not claim the transition finished while shared guidance or
duplicate host discovery remains unresolved.

## Explicit folder connection

Use the following only when the user explicitly requests a folder-bound connection,
or the host does not yet support account registration. This keeps the old contract.

## Before running: confirm

Tell the user, in one or two lines, what will happen and wait for "응":

> 이 폴더(`<root>`)를 두루마리 공간에 연결합니다. 브라우저에서 구름·공간·
> 저장소·폴더를 확인하고 승인하면 이 폴더의 Agent 설정 파일에 연결 항목이 추가됩니다.
> 진행할까요?

Name the files that will change when you know them (`.mcp.json`, `.codex/config.toml`,
`.claude/settings.local.json`, `.codex/hooks.json`, `AGENTS.md` managed block,
`durumari.yaml`, `.durumari/context-binding.json`). Do not run before the yes.

## Run

Resolve this plugin root from this file and the selected work folder:

```sh
python3 <plugin>/scripts/durumari_plugin.py connect --workspace <absolute root> --client <current agent>
```

- Python 3.12+ and uv are required; the verified bundle installs a missing runtime.
- On Windows `python3` may be the Microsoft Store alias; use `python` (or `py -3`)
  after confirming it is 3.12+.
- Keep the command running while the human logs in and approves the displayed cloud,
  space, repository and working folder in the browser.
- Never approve for them, and never collect passwords or tokens.

## Explicit account setup command

When no connection authorization is present, explain the scope and ask once:

> 이 Agent(`<host>`)를 두루마리 계정에 연결합니다. 브라우저에서 승인하면 사용자 MCP
> 설정에 `durumari_account` 항목 하나가 추가되고, 다른 설정은 바꾸지 않습니다.
> 진행할까요?

```sh
python3 <plugin>/scripts/durumari_plugin.py account-setup --client <codex|claude-code|cursor>
```

- `state: pending` returns an approval URL; let the human approve, then run the same
  command again. Never approve for them.
- After `configured`, ask the human to restart the host.
- `account_runtime_update_required_then_restart_host`: update the plugin and runtime
  (durumari-update), restart, then repeat account-setup.
- `account_mcp_configuration_conflict` or `invalid_agent_configuration`: report it; do not
  edit the host configuration by hand. `account_setup_configuration_changed_retry`:
  another program changed the file; run the same command again.

## Which server and folder

- Use the bundled server, or this project's existing `durumari.yaml` (shared team values
  only; each user's profile stays in the ignored `.durumari/context-binding.json`). An
  explicit self-hosted server may override it.
- Git and origin are optional. A Git root with a remote keeps its existing repository
  source.
- A non-Git folder, a selected Git subfolder, or a local repository without origin uses
  a public folder source UUID, registered by a cloud owner during browser approval.
- A server that does not advertise `folder_sources` requires an upgrade; do not bypass it.
- Copied files and unrelated or nested repositories grant no access. A copied manifest
  grants no access.

## Another device or location

- Let the human select the space and source name on the approval page. Never ask them
  to copy or type a UUID.
- Owners may register the current folder as a new source and approve its connection in
  the same action. Members can approve only the folder's own registered source, or
  extend a source they already hold an active connection for. Re-pointing a folder to
  another existing source is left to an owner in the browser.
- Like a Git remote, a folder UUID is a public client claim, not proof of folder
  contents; project membership and owner registration are the access boundary.
- The explicit approval button grants the displayed Context scope without an additional
  consent checkbox. Each device and new location still needs browser approval.
- After copying or moving, keep the old profile and pending queue; never forward its
  records into the new location.

## Boundaries

- Existing approved parent bindings are used by status and Context tools only within
  the same source boundary.
- Connect always binds exactly the selected folder and never rebinds its parent.
- Nested Git repositories or another manifest need their own explicit connection.
- For a multi-repository workspace, connect selected child repositories separately to
  the same project; never scan or enroll children automatically.

## Adding a permission

A new kind of permission is never added to an existing connection automatically. When a
tool reports that an action needs a permission this connection lacks, explain which one
and run connect again for the same folder after the user agrees; the browser shows only
the additional permission to approve. Permissions that only split an existing one keep
the same scope without a new approval.

## Removing the connection

When the user asks to disconnect this folder, confirm first, then run the wrapper's
`remove` command in the exact root:

```sh
python3 <plugin>/scripts/durumari_plugin.py remove --workspace <absolute root>
```

Report pending revocation until the server confirms it, list `local_files` removed and
restored, and tell the user each `kept` item (the server project and its Context, the
plugin, the shared runtime, other folders' connections, the encrypted queue and
quarantine, and the team's shared `durumari.yaml` when present) remains. Preserve unrelated project settings.

## After approval

1. Reload the host if needed and run status (durumari-status).
2. Credentials stay in the OS store; ordinary use refreshes them until removal or
   revocation.
3. A transient network failure keeps the connection and pending requests.
4. Keep host trust and disabled hooks intact. Never remove an older queue or quarantine
   to repair a connection.
5. Verify server status, an explicit Context write receipt and read-back separately.
   Host settings discovery and reload must be checked on the actual host; generated
   files alone do not prove an active MCP session.

## Hooks this adds

- Session start: bound project, Policy index, response language, update or connection
  notices; again after compaction.
- Each message: a short list of related Context titles (depth 보통 by default; the
  durumari-context skill changes it).
- Stop: a reminder when writes are still unconfirmed.

## Answer shape

Say which folder is now connected to which space, which files changed, what the user
must still do (reload, approve a hook in the host), and how to check (durumari-status).
