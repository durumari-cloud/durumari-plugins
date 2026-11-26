---
name: durumari-secrets
description: Register, deploy and use secret values (API tokens, keys, passwords, connection strings, .env values) through Durumari without the value reaching chat, files, logs or command lines. Use when the user mentions 토큰, API 키, 비밀값, 시크릿, 비밀번호, 인증 정보, .env, 환경변수, 키체인, "토큰 발급해서 올려 줘", "GitHub 시크릿에 넣어 줘", "환경변수로 넣어서 실행해 줘", "공용 스킬에 필요한 값 등록"; and before any command that would print, read or pass a credential. Not for ordinary Context documents (durumari-context).
---

# Durumari secrets

Durumari's project vault stores secret values encrypted. Values move only through
pipes into the local `durumari-mcp secret` command, the web 비밀값 secure screen, or the
approved device delivery flow. The model never needs to see a value.

## Ground rules

1. Never print, echo, `cat`, open or summarize a secret value, `.env` file, key file,
   token cache or credential store. Never ask the user to paste a value into chat.
2. Never put a value on a command line (`--token abc...`): process lists and shell
   history keep it. Use a pipe, `--from-cmd`, `secret run` or the secure screen.
3. Never write a value into Context, SKILL.md, references, commits, logs or reports.
   Names, versions and ids are fine.
4. Say what will happen and get a "응" before registering, deploying, writing `.env` or
   injecting a value into a command. Reading names and status needs no confirmation.
5. Be honest about the limit: this is "no exposure on Durumari's path", not isolation
   from the Agent. A command run without a pipe prints its value, and `.env` files and
   some OS key stores are readable by the same user. Prefer `secret run`.
6. Never collect passwords. Replacing, deleting, sharing, viewing a value or importing
   from a key store needs the person on the secure screen with their password.
7. Check the project Policy index first when a Policy covers secrets or a provider.

## What exists where

| Step | How | Notes |
|---|---|---|
| List names, check a request | `durumari_secret_list`, `durumari_secret_status` tools, or `durumari-mcp secret list` / `secret status <operation_id>` | value-free; hidden in Chat (OAuth) connections |
| Register an issued token | `durumari-mcp secret put NAME --from-cmd -- <issuing command>` | captures only the command's stdout |
| Replace a same-named secret | `<producer> \| durumari-mcp secret put NAME --stdin --replace` | only stages it; the person approves on the secure screen |
| Register a piped value | `<producer> \| durumari-mcp secret put NAME --stdin` | refuses a terminal; needs a pipe |
| Value the person holds | web 비밀값 view (secure screen) | the Agent never receives it |
| Use in one command | `durumari-mcp secret run --env VAR[=NAME] -- <command>` | env only, nothing printed |
| `.env` for a tool | `.env.durumari` template + `durumari-mcp secret env` | gitignored, owner-only |
| Offline use | `secret run --offline`, `secret cache status\|clear` | OS key store cache |
| Deploy to a provider | provider's stdin command inside `secret run` | see providers below |

Commands appear as the installed runtime gains them. Check with
`durumari-mcp secret --help` before proposing one; if a command is missing, say the
runtime needs an update (durumari-update) instead of inventing a workaround.

## Approval

- A new secret owned by the user, shared with nobody: allowed from an approved device
  unless the project setting asks for approval. Then `put` returns an operation id;
  check it with `secret status`.
- Deploying or injecting (run, env, provider push): needs that secret's "Agent 사용"
  permission; the first use for each target asks for browser approval once.
- Replace, delete, share, view, key store import: the secure screen with the person's
  password. Open or point to the 비밀값 view; do not attempt it from the CLI.

## Flows

### A. "토큰 발급해서 두루마리에 올려 줘"

1. Find how the provider issues it (see [providers](references/providers/github.md)).
2. Tell the user: "<제공자> 토큰을 발급해 두루마리에 `NAME`으로 저장합니다. 값은
   화면에 나오지 않습니다. 진행할까요?"
3. After "응": `durumari-mcp secret put NAME --from-cmd -- <issuing command>`.
4. Report name, version and id only. If an operation id came back, report that it waits
   for approval and how to check it.

If the provider issues tokens only in its web page, the person copies the token there.
Then, after confirming, pipe the clipboard without displaying it
(`pbpaste | durumari-mcp secret put NAME --stdin` on macOS,
`Get-Clipboard | durumari-mcp secret put NAME --stdin` in PowerShell), and suggest
clearing the clipboard. Or send them to the 비밀값 view.

### B. "이 값으로 실행해 줘" / "환경변수로 넣어 줘"

1. Confirm the secret name and the variable name.
2. `durumari-mcp secret run --env VAR=NAME -- <command>`; repeat `--env` for more.
3. The command's own output is shown as usual; never add `env`, `printenv` or `echo`.

### C. ".env 만들어 줘"

Write `.env.durumari` with references instead of values, then render it. Details:
[references/env.md](references/env.md).

### D. "GitHub(Vercel, Cloudflare) 시크릿에 넣어 줘"

Run the provider's stdin-reading command inside `secret run` so the value goes from
Durumari to the provider without a terminal or file:
[GitHub](references/providers/github.md), [Vercel](references/providers/vercel.md),
[Cloudflare](references/providers/cloudflare.md).

### E. Key store (키체인)

Local OS key stores, the cache used by `secret run`, and import rules:
[references/keychain.md](references/keychain.md).

### F. Values a shared Skill needs

When a user shares their own Skill, keep values out of every Skill file. Guide them to
register each value in the project's 비밀값 view (encrypted, never shown again). Secret
delivery needs the secret owner's use grant and the cloud owner's device approval for
the pinned Skill version, resource scope and secret references after provider
registration. Repository membership alone does not authorize use. The approved
`durumari-mcp project-device receive --client <agent> --skill <name>` flow writes
credentials to the device's OS credential store without exposing them to the model.
Generate SSH private keys on the device and register only public keys. Durumari
revocation does not recall delivered values or provider sessions; revoke at the
provider too. Registering the Skill itself: durumari-share-skill.

## Commands to refuse or rewrite

| Instead of | Do |
|---|---|
| `cat .env`, `grep TOKEN .env`, opening a key file | check names with `secret list`, or ask what is missing |
| `echo $TOKEN`, `printenv`, `env` | `secret run --verbose` prints injected names only |
| `gh auth token` alone, `security find-generic-password -w` alone | pipe into `secret put` or `secret run` |
| `curl -H "Authorization: Bearer abc..."` | `secret run --env TOKEN=NAME -- curl -H "Authorization: Bearer $TOKEN"` via a shell that expands it |
| Writing a value into a config file | `.env.durumari` + `secret env`, or the provider's secret store |

When a variable must be expanded by a shell, use `secret run --env TOKEN=NAME -- sh -c
'<command using "$TOKEN">'` (PowerShell: `pwsh -c`). The value stays inside the child.

## Absence is not proof

A missing tool or command in this session does not mean the product lacks the feature.
Check the installed runtime (`durumari-mcp secret --help`), the plugin version and the
service before saying something is unavailable, and separate "implemented" from
"available in this deployment".

## Answer shape

Report what was stored or deployed by name, version, target and id; what still waits
for approval; and what the person must do in the browser. Never include a value or a
partial value.
