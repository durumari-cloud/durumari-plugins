# Shared project Skills

Use the current Cloud Context connection in the repository root:

```sh
durumari-mcp context-connect --agents codex claude cursor
durumari-mcp project-skills list --client cursor
durumari-mcp project-skills install --skill example-ops --client cursor --enroll
```

`--client` is optional: an explicit host profile or the Agent environment selects it; a shared profile without a host hint defaults to Codex. It identifies the calling Agent, not a separate installation destination. `--enroll` requests access after installation; it does not approve access.

The shared profile is discovered from the approved local binding. `--profile` remains available for an explicit profile. Each installed project Skill has one managed copy in `.durumari/skills/<name>`, shared by local Agents. Installation does not also populate `.agents/skills`, `.claude/skills` or `.cursor/skills`, and does not rely on host discovery or symlinks. Generated instructions direct Agents to MCP Skill reads. Host configuration files and `AGENTS.md`/`CLAUDE.md`/`GEMINI.md` remain at their host-required paths; the versioned `durumari.yaml` remains the shared project declaration.

Generated copies, temporary files, installation records and backups are local state under the ignored `.durumari/` directory. Authored Skill sources in their source repository remain versioned. Do not ignore whole host Skill folders or remove existing host copies that the installer cannot prove it owns. Existing edits, unowned directories, symlinks and Windows junctions are protected. A conflict requires review, not a forced overwrite.

## Verification and safe updates

A project can mark Skills as required and select their immutable versions. At session start and before dependent work, native Agents call `durumari_skill_sync_status` to compare required versions, bundle digests and actual installed files, then call `durumari_skill_sync` when updates are needed. Safe synchronization installs missing copies and updates unchanged managed copies. It preserves local modifications and reports conflicts, access failures or unavailable verification. It never executes downloaded code, requests device enrollment or changes secret permissions. Unrelated MCP work remains available; do not claim a required Skill is current when its verification failed.

Check legacy-copy warnings even when the central copy reports `current`. Status includes `legacy_copies` and `legacy_conflicts`: `legacy_skill_copy_pending` means a previous host-specific copy still needs safe reconciliation, while `legacy_skill_copy_conflict` means local modifications, tracked files or unsafe links were preserved for review. Run `durumari_skill_sync` for pending legacy copies as well as missing or outdated central copies; synchronization revalidates and retries safe migration even for a current release. Report preserved conflicts and continue unrelated work. Do not repeatedly retry unchanged conflicts or force their removal.

Use `durumari_skill_list` to find Skills relevant to the task and `durumari_skill_read` to read the selected release. Do not load every Skill's complete contents into each session. Remote Chat uses these server MCP operations without local installation or synchronization. A required Skill is a project expectation, not authority to execute its instructions or access credentials. Device approval and secret access are independent checks.

## Git source registration

The recommendation catalog is curated separately by operators. A recommendation's **Add to shared Skills** button imports its GitHub/GitLab repository, ref and folder into the current project using the same publishing permissions. The original-source link stays available. After registration, the card opens the shared Skill; it does not install it on an Agent or automatically republish upstream changes. Provider limits and import failures leave a retry available. An existing different release with the same name/version requires the regular Git import flow and a new version.

Portable bundles preserve `SKILL.md`, additional safe relative paths (including licenses, `agents/`, `rules/` and examples), and PNG/JPEG/GIF/WebP files under `assets/`. Text stays UTF-8; image bytes use an explicit canonical base64 object with a verified media type. Existing text bundle hashes remain unchanged. Limits remain 100 files, 128,000 bytes per file and 512,000 bytes of original file content; traversal, hidden paths, reserved device names, file/directory or case collisions, links and credential-bearing content are rejected. Public GitHub content comes from the fixed raw-content origin at the resolved commit, and each file must match its pinned Git object id. Imported content is stored and previewed, never executed.

Extra paths and image objects require native SDK 0.4.36 or later. Older SDKs reject these bundles before writing any files; they do not install an incomplete copy. Update the native plugin through its normal update flow before installing or synchronizing a newly supported bundle. Project registration itself does not update the user's installed plugin.

The Skills page and `durumari_skill_import_git` can import a public HTTPS repository URL from GitHub, GitLab.com or the supported GitLab host, an optional Git ref and a Skill subdirectory. Other hosts use the native checkout flow below. Registration resolves the ref to a commit and publishes an immutable release with source provenance; later repository changes do not silently replace that release. Select a newly published version to update the project.

For a private repository, an Agent prepares a local checkout using the user's existing Git login and calls native `durumari_skill_import_git_checkout` with its HTTPS repository URL, checkout directory, ref, subdirectory and release version. The checkout origin must match the repository URL. Only committed Skill files from the selected directory are uploaded; uncommitted edits and Git configuration or credential data are excluded. Importing does not run hooks, filters or repository scripts. Do not put a token in the URL or pass one to an MCP tool. Chat cannot access a user's local checkout; private imports require the connected native Agent. Public or private source access does not grant project publishing or Skill access permissions.

## Devices and secret access

The project Skills page exposes immutable versions and device public keys. An operator registers a dedicated account, per-user/device API token and the device SSH public key with provider-enforced resource access. The secret owner grants use, then the cloud owner approves the pinned version, value/policy revisions and resource references with password verification. Administrator accounts and personal credentials must not be copied into this project.

```sh
durumari-mcp project-device status --client cursor --skill example-ops
durumari-mcp project-device receive --client cursor --skill example-ops
durumari-mcp project-device forget --client cursor --skill example-ops
```

### Declaring and using secrets

A Skill declares its secret slots in `durumari.json` at the bundle root. Explicit `requirements` passed to publish/import tools or `--requirement SLOT:KIND[:optional]` must match it when both are given.

```json
{
  "secrets": {
    "api_token": {"kind": "text", "description": "Service API token", "required": true},
    "deploy_account": {"kind": "account", "env": "DEPLOY_PASSWORD"},
    "client_cert": {"kind": "file", "required": false}
  },
  "device_keys": ["ssh"]
}
```

- `text`: a token or plain string. `account`: only the account's password string is delivered. `file`: one or more files.
- `env` names the variable; it defaults to the slot name upper-cased (`api_token` becomes `API_TOKEN`). `PATH`, `LD_*`, `DYLD_*` and `DURUMARI_*` are reserved.
- `device_keys: ["ssh"]` asks for the device SSH key generated at enrollment. Legacy kinds are still read: `api_token`/`dns_credential` are text, `password` is account, `ssh_key` requests the device SSH key.

Run the Skill's command through the approved device:

```sh
durumari-mcp project-skills run --skill example-ops -- ./scripts/deploy.sh --dry-run
```

Each run fetches a fresh signed delivery; it never reads the `receive` cache. Text and account values become environment variables of one child process started without a shell. File secrets are written to a private temporary directory (0700 directory and 0600 files on POSIX, an owner-only ACL on Windows) only while the child runs; the variable holds the file's path, or the folder's path when the secret has several files. A file secret's optional `username`, `passphrase` and `password` (for example an SSH key's user and passphrase, or a PKCS#12 password) become `<ENV>_USERNAME`, `<ENV>_PASSPHRASE` and `<ENV>_PASSWORD`. The directory is removed after the child exits, including a non-zero exit, a start failure or an interrupt. The command prints nothing itself, passes stdin/stdout/stderr through and exits with the child's code.

Receive decrypts directly into the OS credential store; stdout reports only status and count. Device private keys and credential caches stay in the approved OS secure store, outside Skill files. Forget removes cached credentials offline and retains local keys. Server/provider token and SSH registration revocation remain separate obligations.

Native and remote MCP expose shared Skill search, pinned reads, immutable publishing, metadata updates, version selection, removal and secret-reference linking/unlinking. Every write requires the caller's current resource permissions; revision conflicts require re-reading and reconciling. Repeating the exact immutable publication is safe. Secret values, sharing grants and device approval never become model arguments. Sharing and secret operations that require human approval stay on the existing secure screen. The Agent-only `durumari_secret_request` accepts an operation kind, stable request key and optional versioned secret reference; it returns a secure-screen request and never accepts a value or password. Chat OAuth connections do not expose this request tool.

Native MCP additionally exposes `durumari_skill_install` (install and request access together). Remote Chat has no access to local keys and does not expose it. The product ships no provider-specific execution adapter: a Skill that operates infrastructure carries its own scripts and receives credentials through the standard device delivery above.

Downloaded scripts are not executed during installation. Skills are untrusted source material and confer no execution authority. Cursor uses the standard `.cursor/mcp.json` written by `context-connect`; automatic raw conversation capture remains disabled.
