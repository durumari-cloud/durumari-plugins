# Saving Context

## When

Save:

- a decision the user made ("응", "그렇게 하자") or confirmed;
- a verified result: what changed, where (branch, commit, file), what was checked and
  what was not;
- a checkpoint before a long pause, a hand-over or the end of the task;
- requirements and open questions the user wants kept.

Do not save read-only lookups, routine progress, unchanged facts, raw logs, transcripts,
credentials, secret values or internal reasoning.

## Shape

| Field | Guidance |
|---|---|
| `title` | `<topic> — <conclusion>`, 8-80 characters; no kind prefix, issue key or date |
| `summary` | One or two sentences; the first states the conclusion |
| `kind` | decision, spec, plan, `action` (work logs), report (finished result), issue, request, note, ... |
| `body` | Selected facts with short excerpts; `#` headings for sections |
| `tags` | Issue keys, an area tag, reserved `work-log` / `done` / `policy` / `qa` |
| `provenance` | user / document (`source_id`) / agent entries, excerpts up to 800 chars |
| `supersedes` | Earlier documents this one replaces, with scopes when partial |

Provenance with `kind=document` and `source_id` links the cited document in the project
graph. A URL alone or an id written in the body does not create that link.

What goes after `—` depends on the kind: the decision itself, an issue's symptom, a
report's result, a plan's scope. Ask whether the title alone tells a teammate if they
need to open the document. Exceptions: team rules are `Policy — <subject>`, and work
logs are `작업 기록 — <task>(<issue key>)`.

## Receipt help

Servers that check the writing rules add two fields to a `stored` receipt:

- `added_tags`: issue keys the server found in the text and added as tags. Only
  prefixes the project already uses as tags count.
- `warnings`: `[{code, message}]` for rules the document misses, such as
  `summary_missing`, `title_kind_prefix` or `title_duplicate`.

The document is saved either way. Tell the user in one line and offer a revise; do not
retry the save. Older servers send neither field.

## Request keys

- One stable key per intended write: `<issue>-<topic>-<purpose>`, letters, digits and
  `_.:-` only, up to 128 characters.
- Reuse the same key when retrying the same write.
- After a lost reply, call `durumari_context_operation` with that key. Only `stored`
  confirms persistence.

## Revising

1. Read the latest outline or section; keep `revision` and section `sha256`.
2. Call `durumari_context_revise` with `base_revision` and
   `sections=[{id, before_sha256, text}]`, or changed fields. A section replacement
   replaces the whole section: read it to the end first (`truncated` -> `next_after`);
   a cut to half or less of a 1,000+ character section needs `allow_shrink`.
   To add a line at the end of a section (work-log progress), send
   `section_appends=[{id, text}]` alone: no read, no sha256, no current `base_revision`.
3. On conflict: read the latest section again, reconcile, and use a new request key.

## Example

```json
{
  "request_key": "APP-243-prompt-context-result",
  "title": "질문별 관련 문서 자동 주입 — 훅과 깊이 4단계 구현",
  "kind": "report",
  "summary": "UserPromptSubmit 훅과 깊이 4단계를 구현했다. 통합 브랜치 검증 전.",
  "body": "# 바뀐 것\n- ...\n# 검증하지 않은 것\n- ...",
  "tags": ["APP-243"],
  "provenance": [
    {"kind": "user", "excerpt": "C2 깊이 설정을 4단계로 넣어 줘"},
    {"kind": "document", "source_id": "<decision id>"}
  ]
}
```
