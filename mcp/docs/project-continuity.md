# 작업 연속성 (현행 안내)

이 문서의 이전 내용(호스트 대화 원문 자동 수집, `project-login`·`project-serve`·`project-sync`, `durumari_project_capture` 도구, 수집 큐·tainted session 규칙)은 0.3.x 이전 방식이며 현행이 아니다. 원문은 Git 이력(커밋 `a3b44a7` 시점 내용)에 보존한다. 현행 동작의 기준은 [연결 계약](connection-contract.md)이다.

## 현행 계약 요약
- **대화 수집 없음.** 연결 프로필은 `context_only=True`, `capture_enabled=False`다. 대화 원문·내부 reasoning·비밀값을 서버에 올리지 않는다.
- **이어받기는 Context로 한다.** Agent가 중요한 결정·결과·체크포인트를 짧은 근거와 함께 `durumari_context_save`·`durumari_context_revise`로 저장하고, 다음 Agent가 `durumari_context_search`·`durumari_context_read`로 읽는다.
- **저장 완료는 서버 영수증만으로 판단한다.** 응답이 끊기면 같은 `request_key`로 `durumari_context_operation`을 조회한다. 미확인 요청은 로컬 암호화 큐에 남고 임의로 폐기하지 않는다.
- **훅은 안내만 한다.** SessionStart는 바인딩 안내와 Policy 색인, UserPromptSubmit은 관련 Context의 제목·요약([prompt-context.md](prompt-context.md)), Stop은 미확인 저장 알림만 준다. 대화를 수집하거나 Agent 작업을 막지 않는다.

## 확인
`context-status`는 연결 상태·runtime·서버 릴리스를 보여 준다. `pending_writes`는 연결 전체의 미확인 쓰기 수이며, `pending_writes_this_session`은 현재 세션만 센다. Stop 훅 알림은 세션 기준이다(계약 7절).
