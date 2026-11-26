# Examples

User: "지난번에 응답 언어를 어떻게 하기로 했지?"
1. Search `응답 언어`; if empty, search `언어 설정`.
2. Read the outline of the best match, then the decision section.
3. Answer with the decision and `[id@revision]`; note if it was superseded.

User: "이 결정 두루마리에 남겨 줘."
1. Summarize the decision with the user's own words as provenance.
2. Save with `kind=decision` and a stable request key.
3. Report the stored receipt in one line.

User: "관련 문서 안내 너무 길어, 가볍게 해 줘."
1. Run `prompt-context --depth light` in the project root.
2. Reply: "다음 메시지부터 관련 문서를 가볍게(제목·요약 3건) 안내합니다."

User: "응" (after you proposed saving a result)
1. Save with the request key you named in the proposal.
2. Report the receipt; if the reply was lost, check the operation with the same key.
