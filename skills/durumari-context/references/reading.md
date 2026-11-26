# Reading Context

## Search terms

The server matches a whole query against titles, tags, summaries and text, and also
accepts substring and fuzzy title matches. A long sentence rarely matches; a short,
distinctive term does.

| Request | First query | Then |
|---|---|---|
| "APP-240 결과 알려 줘" | `APP-240` | the feature name |
| "응답 언어 정한 거" | `응답 언어` | `언어 설정`, `response language` |
| "플러그인 스킬 재정의 결정" | `스킬 재정의` | `플러그인 스킬` |
| "project_cli.py 바꾼 이유" | `project_cli` | the hook or feature name |

- Drop particles and request words: search `스킬`, not `스킬을`; not `찾아줘`.
- Korean and English names of the same thing are separate searches.
- A Context id (`1a2b3c4d-...`) is read directly with `durumari_context_read`; a short
  id prefix is not a full id, so search the title instead.

## Results

Each item has `id`, `revision`, `title`, `section_id`, a short `snippet`, `trust` and
`efficacy`. `origin: legacy_archive` marks text imported from the older store that
nobody has revised since.

- Prefer active, current documents. `efficacy.status` tells whether a document is in
  force today; `state` is the snapshot state of that revision.
- `trust: reported` means the server stored what someone reported; it is not proof.
- `tier` comes from real reads and edits (bulk scans excluded): `hot` is current,
  `warm` was not really read for weeks and ranks lower, `cold` is archived and only
  returned with `include_archived=true`. A tier says nothing about truth: an archived
  decision can still be in force, so check `efficacy` before relying on any result.

## Reading

1. Long document: `view=outline` first, then `section_id`.
2. Need the exact text that a later revision changed: pass `revision`.
3. Need where a statement came from: `view=provenance`.
4. Need why a document was replaced: `view=lifecycle`.
5. Keep `revision` and section `sha256` for any later revise.

Follow an explicit continuation (`after`) only when the needed part was cut.

## Neighbours

When a read result includes related documents (relation, title, id, revision), read
only those that decide the answer: usually the decision a spec implements, or the
evidence a decision cites. One or two are enough; keep within the token budget.

## Policies

Before using an external tool, plugin or skill, check the Policy index shown at session
start (or `durumari_policy_index`). When a Policy's when-line matches the work, read
that Policy first and follow it. If it conflicts with the user's request or the tool's
own rules, ask the user before acting.

## New reports and recent issues

For "버그 들어왔어 확인해봐" and similar requests, search the symptom and list
with `order=created_desc`, then read the matching current section before Jira.
This sorts by original creation time with stable id tie-breaking; editing an old
document does not make it a new report. The default catalog remains in id order.
Use the same order with `next_after` when paging. If MCP fails, use the approved
workspace CLI (`durumari-mcp context list --order created_desc` and search/read).
Do not reconnect or infer a missing report from a failed read.
