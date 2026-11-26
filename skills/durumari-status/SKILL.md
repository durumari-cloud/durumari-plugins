---
name: durumari-status
description: Check which Durumari project this folder is bound to, whether the connection works, unconfirmed Context writes, the plugin version and the per-message related Context level. Use when the user asks "두루마리 연결 상태", "연결돼 있어?", "저장 안 된 거 있어?", "관련 문서 안내 지금 어떻게 돼 있어?", before a git init, folder move or detach, or when a hook reported a connection problem. Session start already shows most of this; run it when the user asks or something looks wrong.
---

# Durumari status

Use the status tools of the selected provider. A plugin-provided remote account
connection reports its current account/conversation/project selection through MCP;
inspect its listed schema and keep returned references. A folder binding is not a
prerequisite. Host OAuth sign-in, tool discovery and a stored Context receipt are
separate observations. Do not start local account setup to repair a network error.

If remote and local providers coexist, report that fact and preserve settings and
unconfirmed writes until their verified migration flow completes. Never claim one
provider remains merely because a package or config file exists. Unsupported host
versions can use the explicit local fallback with that limitation recorded.

Local/folder connections use the session start hook to inject their bound project,
Policy index, response language and connection/update notice. Use the checks below
for that selected connection, or when the person asks about the local installation.

## Check

```sh
python3 <plugin>/scripts/durumari_plugin.py status --workspace <approved work folder>
```

On Windows use `python` or `py -3` with Python 3.12+; `python3` may be the Microsoft
Store alias. Inside a connected session `durumari_project_status` gives the same binding
and pending writes without a command.

Report:

| Field | Say |
|---|---|
| server / project | which project this folder is bound to |
| `pending_writes` | unconfirmed writes of the whole connection |
| `pending_writes_this_session` | this run only |
| `update.state` | current, update_available, update_required or unknown |
| reason code | the safe code, in plain words |

Confirm each uncertain write with `durumari_context_operation` using its original key.

## Per-message related Context level

```sh
python3 <plugin>/scripts/durumari_plugin.py prompt-context --workspace <root>
```

It shows the level (끄기·가볍게·보통·깊게) and who set it (`user_project`, `user`,
`project`, `environment` or `default`). Changing it belongs to durumari-context.

## Before a folder change

Before a `git init`, moving the connected folder or detaching it as an independent
repository, add `--change git-init|move|detach`:

```sh
python3 <plugin>/scripts/durumari_plugin.py status --workspace <root> --change move
```

This local check reads no credentials and reports whether browser reconnection will be
needed. Keep the connection files and pending records, make the folder or Git change,
then reconnect if indicated (durumari-connect).

## Reading problems

| Situation | Meaning | Next |
|---|---|---|
| Network failure, `project_delivery_unknown`, `project_unavailable` | not a login failure | keep working; retry later; confirm writes by key |
| `project_login_required`, `project_access_denied` | approval or login needed | durumari-connect after the user agrees |
| `project_repository_mismatch_reconnect`, `workspace_out_of_scope` | folder or remote changed | durumari-connect for this exact folder |
| `project_removed`, `project_client_mismatch`, `invalid_project_credential` | this Agent's settings name a removed connection (another Agent reconnected) | durumari-connect for this Agent; an active folder connection needs no new approval |
| `project_local_check_failed` | a local check failed before any server request; not an outage | durumari-status, then durumari-connect if it persists |
| Missing or copied manifest | cannot authorize a project | durumari-connect |
| `update_required` | runtime below the server minimum | durumari-update |

- Preserve OS credentials, queued work and quarantine. Never delete them to "fix" a
  status.
- A missing or copied manifest cannot authorize a new project. Use connect for explicit
  browser approval when a binding is missing or revoked.

## Removing this project

Removing the connection is a durumari-connect step (confirm first). It reports pending
revocation until the server confirms it, the local files removed and restored, and the
kept items: the server project and its Context, the plugin, the shared runtime, other
folders' connections, the encrypted queue and quarantine, and the team's shared
`durumari.yaml` when present.

## How binding is found

The Context CLI resolves the nearest approved manifest within a bounded parent search
and stops at nested Git boundaries. A public manifest or folder source UUID is not a
credential.

## What hooks do and do not do

- Session start: binding guidance, Policy index, response language, notices; again
  after compaction.
- Each message: a short related-Context list at the chosen level.
- Stop: a reminder of unconfirmed writes.
- None of them capture raw conversations.

Report server connectivity and any explicit stored receipt separately from hook setup.

## Answer shape

One line for the binding, one for pending writes, one for the version, and the next
step only when something needs action. Avoid listing healthy internals.

## Examples

User: "두루마리 연결돼 있어?"
1. Call `durumari_project_status` (or run status).
2. Reply: "이 폴더는 <프로젝트>에 연결되어 있고 확인 대기 저장은 없습니다."

User: "저장 안 된 거 있어?"
1. Read `pending_writes` and `pending_writes_this_session`.
2. For each uncertain key, call `durumari_context_operation` with that key.
3. Report which ones are stored and which still wait.

User: "폴더 옮겨도 돼?"
1. Run status with `--change move`.
2. Say whether a browser reconnection will be needed, and that pending records stay.
