# OS key stores (키체인)

Durumari's local client keeps its own credentials only in the OS store: macOS
Keychain, Windows Credential Manager, Linux Secret Service. There is no plaintext
fallback.

## Cache for `secret run`

- `secret run` and `secret env` can use a local cache in the OS store, so a value works
  offline (`--offline`) without a plaintext copy on disk.
- `durumari-mcp secret cache status` shows what is cached by name; `secret cache clear`
  removes it. Neither prints values.

## Importing an existing item

Importing a value the user already keeps in a key store is a secure-screen action:
the person selects one service and account, approves in the browser and enters their
password; the client reads only that item locally and uploads it encrypted. macOS also
asks for each item. Never scan, list or read the whole key store, and never run
`security find-generic-password -w`, `cmdkey` or `secret-tool lookup` to print a value.
If the import command is not in `durumari-mcp secret --help` yet, use the 비밀값 view.

## Sending to a key store

Delivering a secret into a local key store for another program is a deploy target and
needs the same "Agent 사용" permission and first-use approval as other targets. Keep a
record of where it was delivered so replacing or deleting the secret updates it.

## Limits

Windows Credential Manager and Linux Secret Service items are readable by other
programs of the same user, including Agents. Tell the user when a workflow relies on
that, and prefer `secret run` for one-off use.
