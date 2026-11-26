# Durumari plugins

Agent and Chat plugins for [Durumari](https://app.durumari.cloud): shared project
Context, decisions, work logs, Skills and secret references for coding Agents and Chat
hosts, over standard MCP and a local CLI.

One plugin, several hosts. The same skills and runtime serve Claude Code, Codex, Cursor,
Gemini CLI, GitHub Copilot CLI and OpenCode; Chat hosts (Claude, ChatGPT) use the remote
MCP server with OAuth.

## Install

You need a Durumari account at https://app.durumari.cloud and Python 3.12 or newer.
The one-line installer sets up every supported Agent it finds on this computer.

macOS / Linux:

```sh
curl -fsSL https://app.durumari.cloud/downloads/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://app.durumari.cloud/downloads/install.ps1 | iex
```

To set up one Agent only, name it: `sh -s -- --client codex` on macOS/Linux, or
`$env:DURUMARI_MCP_CLIENT = 'codex'` before the PowerShell command.

Then, in your project folder, ask the Agent to connect Durumari (the `durumari-connect`
skill). Sign-in and project approval always happen in your browser; the plugin never
asks you to paste a password or token into the chat.

Chat hosts do not install anything locally: add the remote MCP server
`https://mcp.durumari.cloud/mcp` as a connector and approve it with OAuth.

## Update

Ask the Agent to update Durumari (the `durumari-update` skill), or run:

```sh
python3 <plugin>/scripts/durumari_plugin.py update --check
python3 <plugin>/scripts/durumari_plugin.py update --client <claude|codex|cursor|gemini>
```

An update keeps every project connection, login and pending write. Restart the host
afterwards. Installations made from a Git catalog update through the host's own
marketplace flow first.

## Repository layout

| Path | Content |
| --- | --- |
| `skills/` | The one copy of every skill the Agent plugin ships |
| `mcp/` | `durumari-mcp`: the Python MCP server, CLI, updater and installers (`mcp/bootstrap/`) |
| `hosts/agent/` | Claude Code, Codex, Cursor, Gemini and Copilot manifests plus the plugin launcher |
| `hosts/claude-public/` | The Claude remote-MCP plugin |
| `hosts/chat/` | The ChatGPT plugin (the registered app ID is a build input, never committed) |
| `tools/` | Release build, Chat plugin build and the skill-variant generator |
| `.claude-plugin/`, `.agents/plugins/`, `.cursor-plugin/` | Marketplace files of the release catalog |

Host copies of `durumari-context` (Chat, public Claude and the copy packaged in
`durumari-mcp`) are generated from `skills/durumari-context` by
`python tools/skill-variants/render.py`; CI fails when they drift.

## Develop

```sh
uv sync --project mcp
uv run --project mcp pytest mcp/tests hosts/agent/tests -q
uv run --project mcp ruff check .
python tools/skill-variants/render.py --check
```

The credential-store tests use the real OS store. On Linux without a desktop session,
run them inside a disposable Secret Service (needs `dbus` and `gnome-keyring`):
`sh tools/linux-keyring.sh uv run --project mcp pytest mcp/tests hosts/agent/tests -q`.

Build a release bound to one service origin (needs `uv`):

```sh
uv run --project mcp python tools/build_release.py \
  --service-origin https://app.durumari.cloud --output dist/release --catalog-output dist/catalog
```

Build the ChatGPT plugin for an already registered app (the ID is passed in, not stored):

```sh
python tools/build_chat_plugin.py --app-id "$CHATGPT_APP_ID" --output "$PWD/dist/chat-plugin.zip"
```

## License

The code in this repository is licensed under the [Apache License 2.0](LICENSE).
See [NOTICE](NOTICE).

The name "Durumari" (두루마리) and the Durumari logo are trademarks of the Durumari
project and are not licensed under the Apache License. You may not use them to name or
promote a modified version or a derived product, other than to state truthfully that it
is based on this code.
