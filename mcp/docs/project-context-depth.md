# 프로젝트 기본 Context 안내 깊이

프로젝트 기본값은 서버에 저장한다. `durumari.yaml`에는 추가하지 않는다. 기존
manifest를 거부하는 구형 클라이언트와의 호환성을 유지하기 위해서다.

## 설정 계약

- 조회: `GET /api/v2/projects/{project_id}/context-injection`
- 변경: `PUT /api/v2/projects/{project_id}/context-injection`, JSON `{"depth":"light"}`
- 허용 값: `off`, `light`, `normal`, `deep`. 기존·신규 프로젝트의 기본값은 `normal`.
- 응답: `{"project_id":"…","depth":"light","can_edit":true}`
- 조회에는 현재 프로젝트 읽기 권한이 필요하다. 변경에는 프로젝트 읽기 권한과
  해당 구름 소유자 권한이 모두 필요하다. 브라우저 변경은 CSRF 토큰을 요구한다.
  Agent의 프로젝트 연결 자격 증명으로 설정을 변경할 수 없다.
- 실제 값이 바뀔 때만 변경 전후 값을 감사 기록에 남긴다.

## Agent 적용

`GET /api/v2/agent/connection`은 현재 연결 프로젝트의
`"context_injection":{"depth":"light","source":"project"}`를 함께 반환한다.
기존 클라이언트는 추가 필드를 무시할 수 있다. 세션 시작 훅은 이미 이 응답을
읽고 `prompt_context_depth`를 로컬 세션 스냅숏에 저장한다. 메시지마다 설정을
읽는 서버 요청을 추가하지 않는다. 변경된 프로젝트 기본값은 다음 세션 시작에 적용된다.

우선순위는 환경변수 → 사용자 프로젝트별 설정 → 사용자 기본 설정 → 프로젝트
기본 설정 → 제품 기본값 `normal`이다. 프로젝트 기본값이 `off`여도 사용자가
프로젝트별 `light`를 선택하면 사용자 선택이 우선한다. 최종 깊이가 `off`이면
메시지 훅은 자격 증명·네트워크에 접근하기 전에 끝난다.

이 문서의 API와 0080 마이그레이션이 함께 배포된 환경부터 서버 계약이 제공된다.
구형 서버에서는 응답 필드가 없어도 기존 제품 기본값 `normal`을 유지한다.

## 검증 범위와 eval 제한

PostgreSQL 통합 시험으로 저장·권한·입력 검증·감사 기록·Agent 연결 응답과
기존 데이터/구형 INSERT 호환성을 확인한다. 실제 서버 응답을 클라이언트의
세션 스냅숏과 메시지 훅에 전달해 Codex·Claude 분기와 사용자 우선순위를
검사한다. 이 시험은 네이티브 앱에서 새 세션을 시작한 실호스트 검증과 구분한다.

스킬 계약 검사는 현행 스킬의 발견·이름·호출 정책·참조 파일·팀 규칙 비포함을
대상으로 한다. 스킬 개수 고정이나 SKILL.md 줄 수 하한을 요구하지 않는다.

2026-10-25 기록된 `claude plugin eval . --ablation none --runs 3 --no-publish
--no-scaffold` 시도는 `plugin eval is currently in early access`로 평가 시작 전에
거절되었다(exit 1). 실행 결과·점수는 없으며 eval 완료 조건은 미충족이다.
접근 제한을 우회하지 않는다. 정적 계약 검사나 manifest 검증을 eval 통과로
대체하지 않는다.
