---
name: durumari-migrate
description: Help a person move selected archived documents (보관 문서) into the project's shared Durumari Cloud Context, or turn user-selected local notes into reviewed Context. The web 보관 문서 view is the main path; use this skill only when the user explicitly asks for migration help.
disable-model-invocation: true
---

# Migrate archived documents

Migration moves only what a person selects. The web app's 보관 문서 view is the main
path; this skill guides the steps and prepares local material for review.

## Archived documents on the server

1. Open the bound project's 보관 문서 view in the web app.
2. The human selects each document and approves the exact current version and
   fingerprint before it becomes shared Cloud Context.
3. Original ID, versions, provenance, keys and history are preserved.
4. Private or restricted documents are never published automatically.

An Agent cannot approve these steps. Explain what the person will see and what each
approval shares.

## Local notes the user selected

1. Ask which files or notes are in scope. Do not scan or upload the repository, raw
   transcripts, secrets or attachments as an automatic connection step.
2. Prepare a concise document with provenance for each selected item and show it to the
   user for review.
3. After the user agrees, save with the normal Context save contract (durumari-context):
   stable request key, provenance, receipt.
4. Keep the originals.

Historical bulk import and model-driven enrichment are not current runtime features.

## Not part of migration

| The user wants to | Use |
|---|---|
| Share their own Skill with the team | durumari-share-skill (공용 스킬 view) |
| Move tokens, keys or `.env` values | durumari-secrets (비밀값 view) |
| Connect this folder | durumari-connect |

The automatic-upload restriction above is a migration scope limit, not a statement that
Durumari cannot store secrets or Skills. Never infer a missing product capability from
a skill restriction or from the absence of a model-facing tool; check the product
documentation or implementation, and distinguish implementation evidence from
availability in the user's deployed service.

## Answer shape

List what was selected, what was saved (title, id, revision, receipt), what still waits
for the person's approval in the web app, and what was left out.

## Examples

User: "보관 문서 옮기는 거 도와줘."
1. Explain that the 보관 문서 view lists archived documents and that each one is
   approved by version and fingerprint.
2. Offer to summarize what a selected document contains before they approve it.

User: "이 폴더의 회의록 세 개를 Context로 올려 줘."
1. Confirm the three files.
2. Prepare one concise document each with provenance and show them.
3. After "응", save each and report the receipts; keep the originals.
