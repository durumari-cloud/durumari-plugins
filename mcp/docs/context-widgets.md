# Context 작업 위젯

## 사용과 등장 조건

| 작업 | 진입 도구/입력 | 표시 |
|---|---|---|
| 단일 원문 | context_read | 반환된 절·판본·출처, 이어 읽기 |
| 점진 탐색 | context_explore의 context_ids 또는 query | 읽음/발견 노드, 실제 관계와 탐색 경로, 선택 원문 |
| 생성·수정·이력 | context_save/revise/version/operation | 서버 저장 영수증, 실제 전후 판본 비교, 복원과 구별한 이력 조회 |
| 용어 | glossary_read/submit | 정의·별칭·관련 개념, 언어별 확인 상태, 후보·현재 정의 비교 |
| 종합·영향·결정 | context_explore view=synthesis/impact/decision | 실제 근거 절, 연결된 검토 범위, 효력 상태와 원문 |
| 재개·검증 보고 | context_explore view=resume/report | 작업 기록의 목표·남은 일·검증 보고, 원문 이동 |
| 검토 보드 | request_read/create/note/revise | 사람 결정·보류와 Agent 메모, 실제 검토 화면 이동 |
| 자료 가져오기 | context_import/resource_status | 브라우저 검토 대기와 완료 영수증 구별, 결과 판본 원문 |
| 회의 | context_explore view=meeting | 이미 저장된 회의 원문과 연결 결정·작업·용어 |

표의 도구 이름에는 `durumari_` 접두사가 붙는다. 도메인 화면은 조회한 기록을
보여준다. 문서에 적힌 테스트·배포 보고를 독립 검증이나 실제 실행 성공으로
승격하지 않는다. 사람의 결정은 기존 검토 화면에서 수행한다.

## 탐색과 보안 경계

각 탐색 호출은 독립적인 session_ref/workspace_id/project_id를 발급한다.
다른 대화나 전역 도구 이력은 합산하지 않는다. 화면 내 발견 노드를 선택하면
일반 context_read로 권한을 재검사하고 같은 탐색에 이어 붙인다. 검색 결과 사이의
관계를 만들지 않으며, 한 문서만 있으면 원문을 표시한다. pan/zoom과 노드 위치는
확장 중 유지하고 모바일에서 원문/그래프를 왕복한다.

기존 TextContent 본문과 영수증은 유지한다. `_meta`에는 도구·입력 식별자만
추가하며 본문·대화·비밀값을 복제하지 않는다. 동적 문자는 textContent로 삽입한다.
이전 화면은 결과 전환·권한 오류 때 비우고 비동기 응답은 화면 epoch로 격리한다.
가져오기 원문은 모델에 보내지 않는 기존 경계를 유지한다. 원문 대응·중복·제외
항목은 사용자의 기존 브라우저 검토 화면에서 확인한다.

## 검증

SDK/stdio 테스트는 실제 MCP 등록·전달을 검사한다. Playwright는 패키징된 HTML의
MCP Apps 프로토콜 harness이며 실제 Codex/ChatGPT 호스트 수락 증거와 구별한다.
3가지 폭(390/820/1440), 키보드, reduced motion, 판본 일치, 권한 회수, 전환과
원문 안전성을 검사한다. HTTP 도구 목록과 호출은 같은 token_scoped 경계를 쓴다.
동일 SHA의 보호 CI·리뷰·통합 이후 실제 호스트 확인은 별도 완료 조건이다.
