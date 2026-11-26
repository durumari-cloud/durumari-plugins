# Runtime update preservation

Version 0.4.7 adds a safe update entry point to the native plugin wrapper. Use
`python3 <plugin>/scripts/durumari_plugin.py update --check`, then `update` when
requested, and `update --status` to read the independent Windows result. The
wrapper verifies the bundled wheel and loads its stdlib-only updater without
importing the installed runtime. This also allows a verified `install` to repair
a missing installed module after its hosts have exited. Older bundled wheels
without the preservation contract are refused by this wrapper. The updater needs
Python 3.12; when `python3` is older, the wrapper reruns the same command under
`uv run --no-project --python 3.12` instead of loading it. It passes the uv it
resolved to the updater, and prints only the error code plus recovery facts
(`snapshot_id`, `runtime_rollback`, diagnostic path), never command output.

An older runtime cannot acquire the new safeguards simply by downloading a new
wheel: its already-running updater owns that first replacement. Update the native
plugin through the host's supported marketplace flow first, then use the new
wrapper. Do not retry the old in-place installer or modify private host caches.
Publish the new native bundle/version before recommending this recovery path.

Before switching a catalog, and again before uv changes files, Windows checks
runtime process image paths and private file usage. Shared uv cache hardlinks
loaded by another environment do not establish that this runtime is running.
`runtime_in_use_close_hosts_and_retry` leaves the installation unchanged. The
updater's own process and its ancestors are not exempt: a running image cannot be
replaced, so an updater started from this runtime gets
`runtime_update_must_run_outside_runtime` and should be rerun through the plugin
wrapper. Processes that exited or cannot be opened (access denied) are left to
the Restart Manager file check. The updater never closes a host or stops an MCP
process. Restart Manager is queried
only for usage, never for shutdown/restart; image paths and command lines are
not written to diagnostics. API errors stop replacement.

The installer serializes runtime changes with an owned `update.lock` in
`durumari-runtime-update/`, a sibling of `uv tool dir` (uv treats every entry of
the tool directory as a tool, and same-filesystem renames keep rollback atomic).
If that parent is not writable or is another filesystem, the Durumari release
state directory's `runtime-update/` is used when it shares the tool directory's
filesystem; otherwise `runtime_update_directory_unavailable_same_filesystem_required`
refuses before any change. It snapshots only the `durumari-mcp` environment and exact launcher into an owned
`durumari-runtime-update/snapshots/<id>` directory. Python caches are regenerated; long
Windows paths are preserved, and the usage check walks the same long-path form. OS credentials, project bindings, queues and other
host settings are outside the snapshot and are never read or copied.

After uv returns, module imports, metadata version, runtime CLI version and
launcher version must agree before installation can complete. An older runtime (the previous version reinstalled by a release rollback, or the old environment kept in a snapshot) is checked only by what every release provides: interpreter, top-level package import, package metadata version and launcher file. A command failure,
timeout or verification failure moves partial files aside, restores the old
environment/launcher at their original paths and verifies the old runtime when
it was previously usable. A previously broken environment is identified as an
unavailable snapshot; restoring it is not reported as healthy. The release
rollback does not invoke uv again when runtime installation already restored its
snapshot, nor reinstall hosts it had not reached. An existing `failed` catalog
is retained while the current rollback uses a unique path.

A snapshot is retained only when recovery may still need it: the restored
environment could not be verified (`restored_unavailable_snapshot`) or rollback
failed (`runtime_rollback_requires_recovery`). A refusal before any change, a
completed install and a restore that passed verification (`restored`) remove the
owned copy, so repeated failures do not accumulate environment copies. A failed first install (no previous runtime) removes its partial files, reports `runtime_rollback: not_installed` and keeps no snapshot. `runtime_rollback_requires_recovery` includes the
owned snapshot ID and never reports completed. An OS hard kill can leave the
runtime lock and snapshot in place; automatic recovery of a stale lock is not
implemented. Do not delete them or blindly reinstall. Inspect the retained
snapshot and diagnostic, ensure the updater and runtime hosts have exited, then
restore the owned environment/launcher or use an independently verified recovery
installation. Production-host recovery and cross-platform acceptance remain
separate from the isolated Windows tests and are tracked separately.

The archive manifest, path, hash, size and link checks from the two-stage release remain
unchanged. This release neither broadens Context permissions nor adds capture.
