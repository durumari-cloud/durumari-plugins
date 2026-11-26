---
name: durumari-share-skill
description: Help register a user-selected personal or Git Skill as a project shared Skill (공용 스킬), publish a new version, link secret references by name, point to the web settings for who may use it, or install a shared Skill and enroll this device for its secrets. Use only when the user explicitly asks; the web 공용 스킬 view offers the same steps.
disable-model-invocation: true
---

# Share a Skill with the project

Shared Skills (공용 스킬) are versioned, credential-free instructions stored once per
repository in `.durumari/skills/<name>`, which every supported Agent (Codex, Claude
Code, Cursor, Gemini CLI, OpenCode, GitHub Copilot and other MCP hosts) reads through
Durumari MCP. Each step below is a Durumari MCP tool call; the web 공용 스킬 view and
the `durumari-mcp project-skills` CLI do the same. Do not drive the web screen for
these steps.

Before any change, read the current state with `durumari_skill_get` (or
`durumari_skill_list`) and pass its `revision` and `policy_revision`. After a lost
reply, read again before retrying; never guess a revision.

## Register or publish a version

1. Ask which Skill to share and review only the selected files.
2. Keep secret values out of SKILL.md, references, scripts, Context, chat, logs and
   reports. Replace each value with a named slot (for example `api_token`).
3. Choose the source:
   - Local files: `durumari_skill_publish(name, version, files, requirements)`.
   - Public Git (GitHub, GitLab): `durumari_skill_import_git(repository_url, version,
     path, ref, requirements)` with the HTTPS repository root, the Skill folder and a
     branch, tag or commit. The server pins the commit and records the source.
   - Private Git: the user prepares a checkout with their own Git login, then
     `durumari_skill_import_git_checkout(repository_url, version, path, ref,
     checkout_path)`. Only committed files are read; hooks and scripts never run.
   Never put credentials in a URL, argument or file.
4. Versions are immutable. Repeating the exact bundle is safe; changed content needs a
   new version. To make a new version current, call `durumari_skill_select`.
5. Label and tags: `durumari_skill_update`. Published versions:
   `durumari_skill_versions`.

## Permissions and required Skills

Agents never change who may use a Skill. Permission and required changes need the
person's password in the browser, so a prompt cannot widen access.

1. `durumari_skill_principals` lists project members and groups by id and name.
2. `durumari_skill_share(skill_id, principal_type, principal_id, permissions)` (or
   `read_policy`) and `durumari_skill_set_required(skill_id, required)` change nothing.
   They return the Skill's settings link (`url`) with the requested change.
3. Give the user that link and the requested change. They complete it in the browser
   after re-entering their password; only the project owner can mark a Skill required.
4. Afterwards, read `durumari_skill_get` again to confirm the new state.

Skill permissions never grant use of a secret value. Secret use needs the secret
owner's grant and an approved device.

## Secret references (no values)

1. The value is registered in the 비밀값 view or with durumari-secrets, encrypted and
   never shown again. Never ask for or paste the value here.
2. `durumari_secret_list` finds the secret's id and revisions.
3. `durumari_skill_link(skill_id, secret_id, base_revision, secret_revision,
   secret_policy_revision, slot)` links it to a slot. `durumari_skill_secret_links`
   shows links; `durumari_skill_unlink` removes one without deleting the value.

## Install on this device

After the user agrees, install the selected version into the current repository:
`durumari_skill_install(skill_id)`. It writes `.durumari/skills/<name>`, keeps the
copy out of Git, preserves local edits and requests device access only when the
Skill names secret slots. The same from a shell:

```sh
durumari-mcp project-skills install --skill <skill-name>
```

Required Skills: `durumari_skill_sync_status` reports missing, outdated or locally
modified copies; `durumari_skill_sync` updates them safely and reports conflicts
instead of overwriting.

When the Skill needs secrets, the secret owner grants use and the cloud owner confirms
the provider registration of dedicated accounts, tokens and SSH public keys before
approving the device. After that:

```sh
durumari-mcp project-device receive --skill <skill-name>
```

stores the credentials in the OS credential store without exposing them to the model.
Generate SSH private keys on the device and register only public keys; never copy a
personal key.

## Removing

- A Skill: `durumari_skill_delete(skill_id, base_revision)` after the user confirms.
- Local credentials: `durumari-mcp project-device forget --skill <skill-name>`.
  Revoking provider tokens and SSH registrations is separate; Durumari revocation does
  not recall values already delivered or provider sessions.

## Boundaries

- Repository membership alone does not authorize secret use; the owner's grant and the
  device approval do.
- Installing, syncing or linking never executes downloaded code, approves a device or
  reveals a value.
- A template's scope and steps are not an executed setup; real commands run only after
  the dedicated account and resources are configured.
- Changes to the original Git repository are not pushed back.

## Answer shape

Say which Skill and version is shared or installed, who can use it, which secrets it
references by slot name, what the owners must still approve, and what was
intentionally left out.

## Examples

User: "내 배포 스킬을 팀에 공유하고 싶어."
1. Review the selected Skill folder; list any values that must become project secrets.
2. After "응", publish the credential-free bundle and report its version and sha256.
3. Point to the 비밀값 view for each required value, then link it by slot.

User: "이 GitHub 저장소의 skills/deploy를 공용 스킬로 등록하고 백엔드 그룹만 쓰게 해 줘."
1. Import with the HTTPS root, path `skills/deploy` and the ref the user names.
2. Find the group with `durumari_skill_principals`, then give the user the settings
   link from `durumari_skill_share` to grant `read` and restrict the read policy.

User: "팀 공용 스킬 설치해 줘."
1. Ask which Skill.
2. After "응", install it; say whether device access was requested and which approvals
   the owners must give.
3. After approval, run receive and report that credentials are in the OS store.
