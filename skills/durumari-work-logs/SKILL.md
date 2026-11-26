---
name: durumari-work-logs
description: Find, create, update and hand off a project's Agent-authored work record using the Durumari work-log tools. Use when starting, resuming, checkpointing or handing off project work, or when asked to check a teammate's work.
---

# Durumari work records

Agents keep a durable record for each task; people primarily read it and can correct it.
Use the current authorized project target and the work-log tools when available. A
project reference identifies a target, not a grant of permission.

1. Find the existing record by task or issue. Read it before continuing; distinguish
   work status from the current Agent session and compare branch and commit with Git.
2. If none exists, create one with a concise title, current summary and optional
   reported Agent name. Leave unknown owner, session and status unknown; do not infer
   them from a last editor, a name in text or a local path. A legacy Context work log
   can be linked with `durumari_work_log_import` using its verified current revision;
   the original is preserved. On a source change, re-read and reconcile rather than
   assuming either version is current. Do not create a duplicate.
3. At the plan, meaningful checkpoints, interruption and handoff, update completed,
   remaining, next action, blockers, constraints and references as relevant; append a
   short reported event. Preserve previous events. Never include raw sessions, tokens,
   secret values or irrelevant private data.
4. Read the current revision first. Use a stable request key and `base_revision` for
   each update. On an unknown result, query the original key or retry the identical
   request; on conflict, read the latest version and reconcile before a new key.
   Only a `stored` receipt proves persistence. Report test results as Agent reports,
   not as independent verification or human approval.
5. For handoff, include the goal, completed and remaining work, next action,
   restrictions, validation performed and limits, plus relevant source and revision
   links. A record or link does not convey another person's access or execution rights.

Tools: `durumari_work_log_list` accepts server filters `q`, `status`, `assignee_id`,
`last_saved_by`, `agent`, `updated_days`, `after` and `limit`; `durumari_work_log_read` reads a native
record; `durumari_work_log_create` and `durumari_work_log_update` write it;
`durumari_work_log_operation` recovers a receipt. For legacy Context results, read
the source using `durumari_context_read` and retain its ID and revision. Never claim
the new tool exists or a legacy record was migrated if the server did not confirm it.
`durumari_work_log_revision` reads an immutable retained snapshot when available.

`last_saved_by` filters the account recorded on the latest event, or on
the latest original Context revision for legacy records. It may be a person's Agent
connection and is not evidence of the current assignee. Use `__unknown__` to find
records without that account; never infer an assignee from the saving account.

For migration, use `durumari_work_log_migration_preview` as a dry run. Follow every
`next_after`, record candidate IDs and source revisions, and account for unavailable
sources before acting. The inventory covers active, project-visible tagged sources,
including already imported and archived-tier records. A matching title without a
work-log tag is only a review candidate, never an automatic import. Private, restricted, redacted,
inactive, empty, fully superseded, quarantined, and untagged sources without a matching
title are excluded; do not infer their number. Titles or summaries containing recognizable
credentials, transcripts, code fences or
invalid lengths require review and are never automatically copied. An unsafe title is
withheld from the inventory; open the original only if authorized and necessary.
This is a live scan, not a snapshot: rescan and reconcile changes before a
cutover. `source_changed` and `requires_review` need source review and are not batch
import candidates. `preview_status=ready` lists field names that would be copied
and those left unknown for that exact source revision; it does not include the field
contents or guarantee a later write. Re-read an unavailable preview before importing;
do not treat `already_imported` as a request to overwrite the existing record. It
inspects at most 20 records per call. Import checked
candidates with `durumari_work_log_migration_batch` in groups of at most 20, passing
only `{context_id, source_revision}` and a stable batch request key. Inspect every
item: `stored` confirms that item, `conflict` requires re-reading the changed source,
and `unknown` requires retrying the same batch key and inputs. Imported records retain
the source; missing fields remain unknown. A batch can partially succeed. Do not delete
or overwrite original Context records, and do not assume a dry run changes anything.
For a changed source, read both the saved source revision and the current source,
compare them, and update any affected fields. Only after review, include
`acknowledge_source_revision` with the exact current source revision in a normal
work-log update, together with its `base_revision` and a reported event. A stale
source or work-log revision conflicts; re-read rather than guessing. Acknowledging a
source does not automatically copy its text or imply independent verification.

Create `body` requires `title`; updates require `base_revision` and at least one
changed field. Optional fields are `status` (`unknown`, `in_progress`, `waiting`,
`complete`, `stopped`), `summary`, `completed`, `remaining`, `next_action`,
`waiting_reason`, `constraints`, verified human `assignee_id`, reported `agent`,
`session_ref`, and `evidence` (short strings or references). Each update may include
`event: {"text": "...", "kind": "reported"}`. Only use a session reference if
actually known; never paste its transcript. Set work `complete` only when the work
itself is complete, not merely because an Agent run has ended.

To take over an unfinished record, read it with `durumari_work_log_read` and
`view="handoff"`: a bounded bundle of the goal, what is done and remaining, constraints,
evidence, the pinned source and whether it changed, and the latest events with who
reported them. Everything in it is a report. Recheck the project binding, the local
Git root, branch, HEAD and working tree, the source revision and your permissions, and
tell the person about any difference before acting. Then take over with
`durumari_work_log_update` and `{"accept_handoff": true, "agent": ..., "session_ref": ...,
"base_revision": <revision read>}` plus a new request key. The server refuses a
completed record and a record another session reported progress on within the last 30
minutes (`work_log_active_elsewhere`); a revision conflict means someone else got there
first. Re-read instead of forcing it. `acceptable` says whether an accept sent now can
succeed; while another session holds the record it is false and `active_until` says when
the hold ends. The holding session can end it at once by updating the record's status to
`waiting` (`release_with`); when you hand a task over, do that instead of making the
receiver wait. The receipt it records is a report, not
verification, and sharing a record never approves running anything on another device.
