# Folder sources

## Identity and approval

Git is optional for a new connection. Selecting a Git root with an origin keeps
the existing registered repository contract. A non-Git directory, Git directory
without origin, or separately selected subdirectory uses a folder source UUID.
The UUID is generated in the OS-protected enrollment journal before the request,
so interrupted enrollment retries retain the same request. Users select an existing
project and source by name in the approval page; they never need to enter a UUID.
The explicit `--folder-source` option remains for compatibility and automation.

The existing registered-source table stores `folder:UUID` and its SHA-256 source
identity, in a namespace separate from `remote:HOST/PATH`. No migration or token
scope expansion is needed. A logical registered source is shared across devices;
the existing `Source` and connection records remain per device enrollment. Each
new location/device still gets its own browser approval, workspace identity and
OS-protected project profile. A UUID, manifest or Git remote grants no access.

Owners can register a folder source to their project or create a project during
approval, with one explicit scope approval button and no separate consent checkbox.
An owner may register the new source in that same action. Members can approve only sources already registered to a project they
can access, and only the folder's own proposed source or a source they already
hold an active connection for (`folder_source_owner_required` otherwise), so a
member cannot re-point a pending request to a different source in the browser.
This is a merge guard, not proof of folder identity. As with a Git remote, the
server never sees local contents and a proposed folder UUID is a client claim;
the UUID in `durumari.yaml` is public. The access boundary is project membership
plus an owner-registered source: a member proposing a registered folder UUID
(for example via `--folder-source`) is equivalent to a member claiming a
registered Git remote, and gains no access beyond that project's Context.
Only the folder's own source is preselected; choosing another existing source
is always explicit. Existing Context access checks, exact read/write scopes, rotation and
revocation remain in force. The browser shows the source, folder, user and target
project. Source choices are limited to accessible projects and the pinned target,
when present. Multiple sources require an explicit choice; project changes clear
the source selection. The approved source can differ from the locally proposed
UUID, while enrollment replay retains the original request scope. Poll returns
the server-granted UUID and hash, which the client verifies before storing its
profile and public manifest. Enrollment may not claim both a remote and folder source, or neither.

## Workspace boundaries

The protected profile binds the canonical absolute work folder, hashed workspace
identity, folder UUID, registered-source identity, server, actor and connection.
`durumari.yaml` records public metadata only and must match that protected profile.
Changing both public source fields cannot redirect the existing credentials.
Copying or moving the manifest never moves credentials or the encrypted outbox.
Connect permits a fresh browser approval at the new location without disabling
the original location. The original pending queue stays in its original profile.

The Context CLI searches upward at most 64 directories for the nearest manifest,
stopping at a Git boundary. The loaded OS profile and server grant authenticate
the result. Inside an approved folder, a descendant without another manifest or
Git root is permitted. A different manifest or nested `.git` file/directory stops
inheritance. Canonical path resolution rejects junction/symlink escapes outside
the approved root. Host configuration discovery is a separate host capability;
finding metadata is not proof that the host has loaded MCP or its advisory hooks.
Upward discovery applies only to using an existing binding (serve, status and
Context commands). `context-connect` always binds exactly the selected folder:
in a plain subfolder it creates that subfolder's own manifest and source and never
re-enrolls or overwrites the parent binding. `context-remove` is destructive and
refuses with `remove_requires_binding_root` unless the selected folder is itself
the binding root.

For a multi-repository parent folder, explicitly connect the parent folder source
and each selected child repository to the same project. Child enrollment creates
its own source/device grant. Unselected child repositories cannot borrow the
parent profile. There is no automatic child scan, enrollment or conversation
capture. Open a new host session at each approved configuration root if the host
does not inherit its project settings.

## Git transitions and compatibility

A folder source stays the same when Git is initialized or origin is added/removed
at that approved root. Nested Git roots still require separate approval. An
existing Git binding keeps its Git top-level boundary: unrelated `durumari.yaml`
files inside the repository (examples, fixtures) do not block it, and only a
subfolder separately connected as its own folder source stops it. Existing
Git-source profiles retain remote validation: a genuinely different repository
requires reconnect, while the existing SSH/HTTPS spelling and `.git` equivalence remain supported.
Repository path case is preserved. Expanded host-case/default-port canonicalization
is deferred: changing existing registered hashes without a migration would force
reapproval of valid legacy connections.

Legacy manifests and protected profiles without `folder_source_id` keep their
existing Git contract. New folder requests require the server's `folder_sources`
capability before opening approval. Deploy server and approval UI support before
shipping a folder-enabled client; do not bypass older-server rejection. The
public API's existing `repository_id`/repository table names are compatibility
names for registered source identity; they do not imply that all sources use Git.

## Verification and limits

Regression cases cover non-Git and origin-less connect, repeated approved connect,
old-server refusal, copied/mutated manifests, nearest-manifest discovery, nested
repositories, path escape, Git initialization, pending queue isolation, owner
registration, pre-approval withholding of tokens, explicit second-device approval,
Context receipt/read-back and revocation. Approval UI tests require explicit
device consent and folder-specific copy.

Synthetic browser decisions and local PostgreSQL/API tests are automated evidence.
They are not evidence of a human-approved production connection, installed host
settings discovery, OS credential-store enrollment, or a published plugin update.
Record those remaining gates separately; never report transcript capture.
