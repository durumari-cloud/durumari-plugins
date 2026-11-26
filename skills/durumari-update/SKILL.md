---
name: durumari-update
description: Check for and install a Durumari plugin and runtime update while keeping every project's connection, credentials and pending writes. Use when the user says "두루마리 업데이트", "새 버전 있어?", "플러그인 업데이트해 줘", or when session start or status reports update_available, update_required or client_update_required. Checking is safe; installing needs the user's yes first.
---

# Update the Durumari plugin

Resolve this plugin root from this file.

## 1. Check (no confirmation needed)

```sh
python3 <plugin>/scripts/durumari_plugin.py update --check
```

On Windows use `python` or `py -3` with Python 3.12+; `python3` may be the Microsoft
Store alias. Report the installed and latest versions and whether an update is
required (`update_required`) or optional (`update_available`).

`blocked: true` with `required_version` means the development team set a required
version and the server now refuses this plugin's Agent requests (`426
client_update_required`, shown locally as `server_requires_client_update`). Say so
plainly: nothing was saved by the refused request, and the connection, login and
queued writes stay as they are. After the update and host restart, retry the same
request key; the queued write is delivered once with the original body.

Account setup registers a launcher that checks the server policy before each MCP start.
When the installed runtime is below an explicit required version, the launcher installs
the verified release into a separate runtime and keeps profiles, credentials and queued
writes. If it reports a recovery code (policy unavailable, update busy, verification
failed), report the code and stop; retry the same account-setup later. Never delete
locks or snapshots to get past it. An update never adds a permission.

## 2. Confirm

Before installing, tell the user what will happen and wait for "응":

> 두루마리 <현재> → <새 버전>으로 업데이트합니다. 프로젝트 연결·로그인·미확정 저장은
> 그대로 두고, 끝나면 이 호스트를 다시 시작해야 합니다. 진행할까요?

Mention any host that must be closed first if the check reported one.

## 3. Install

```sh
python3 <plugin>/scripts/durumari_plugin.py update --client <current supported host>
```

- A Git catalog installation uses the host marketplace update flow to obtain the new
  native plugin first.
- This loads the safe updater from the verified bundled wheel, so an older or missing
  installed runtime cannot bypass its preservation checks.
- If `safe_runtime_installer_missing_update_plugin` is reported, update the native
  plugin through the host's marketplace flow first; do not retry an older runtime's
  in-place installer.
- Never edit private plugin caches.

## 4. Windows and long installs

On Windows, check the returned independent status until completed:

```sh
python3 <plugin>/scripts/durumari_plugin.py update --status
```

This also works when the installed module is unavailable. Starting an installer is not
completion.

## Result codes

| Code | Meaning | What to do |
|---|---|---|
| `server_requires_client_update` | The server refused this version; nothing was applied | Update, restart the host, then retry the same request key |
| `runtime_in_use_close_hosts_and_retry` | No replacement was attempted | Tell the human which hosts must exit; retry after they have exited |
| `runtime_update_must_run_outside_runtime` | Started from the runtime itself | Rerun this wrapper's `update` |
| `runtime_rollback_requires_recovery` | An owned snapshot was kept; not a success | Report it; do not delete snapshots |
| `safe_runtime_installer_missing_update_plugin` | Native plugin too old | Update through the host marketplace first |
| `invalid_release_archive` | The installed updater predates the release layout (0.4.0–0.4.4 vs 0.4.6+); nothing was replaced | Ask the user, then reinstall with the same service's latest installer (below) |

- Never kill ongoing MCP processes merely to unblock an update.
- Installation verifies module imports, the runtime CLI and launcher versions; a failed
  replacement restores the preserved environment when possible.
- Do not delete snapshots or stale locks to hide a failure.

## Updater too old

An older updater cannot read a newer release layout and stops with
`invalid_release_archive` before replacing anything. Retrying will not help; the server
cannot offer an intermediate version. With the user's yes, reinstall from the same
service the installation uses (`https://app.durumari.cloud`, or `https://preview.durumari.cloud`
for Preview):

```sh
curl -fsSLO https://app.durumari.cloud/downloads/install.py && python3 install.py install
```

The installer is the current updater. It reuses the recorded service and hosts,
verifies the archive against `latest.json` and keeps the same things as an update.
Then continue with "After the update".

## What is kept

Profiles, OS credentials, encrypted pending requests, quarantines, the per-message
Context depth preference and other projects' settings. Never bypass host trust.

## After the update

1. Restart the host or begin a new session after replacement.
2. Check the installed version and the exact project's status (durumari-status) before
   claiming success.
3. Context access and transcript capture are different; this update does not enable
   automatic capture.
4. A new runtime may convert older local settings when it first runs (for example the
   managed block of `AGENTS.md`, or adding the per-message Context hook); session start
   reports what changed.

## Examples

Session start said an update is available; the user says "업데이트해 줘".
1. Run the check and report the versions.
2. Ask the confirmation question above.
3. After "응", install, follow the status until completed, then ask the user to restart
   the host.

The user asks "새 버전 있어?".
1. Run only the check.
2. Answer with the versions; offer the update without installing it.

The update reports `runtime_in_use_close_hosts_and_retry`.
1. Name the hosts that must exit.
2. Wait for the user to close them, then retry once.
