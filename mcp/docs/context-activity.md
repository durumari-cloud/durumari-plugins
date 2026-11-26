# Context 결과 카드와 호스트 화면

Context 읽기·생성·수정·삭제·복원의 원래 도구 응답은 그대로 유지하고,
원래 JSON은 TextContent에 한 번만 유지하고 `_meta.activity`에 화면용 metadata를 추가한다.
모델 결과를 축약하는 structuredContent를 기존 도구에 추가하지 않으며 본문을 중복 전송하지 않는다.
호스트가 메타데이터를 누락하면 위젯은 서버의 기존 텍스트 영수증에서 기본 결과를 표시한다.
그때 project_ref가 없으면 임의로 추정하지 않고 결과 브라우저 버튼을 숨긴다.
쓰기 성공은 commit 이후 `stored` 영수증으로만 표시한다. 읽기 카드의 판본과 범위는
실제 반환 page에서 가져오며 목차·일부 절·잘린 응답을 전체 읽기로 표현하지 않는다.
접근 로그는 best-effort이므로 로그 부재를 읽기 실패로 해석하지 않는다.

## 호스트별 경로

| 표면 | 구현 경로 | 이번 변경의 검증 경계 |
| --- | --- | --- |
| ChatGPT·Claude의 MCP Apps 지원 표면 | `ui://durumari/context-activity/v1.html`과 MCP Apps bridge | 실제 MCP SDK resource·도구 응답 및 iframe 브리지 시험. 각 제공사 계정의 실제 렌더는 배포·연결 갱신 후 별도 확인 |
| MCP Extensions 지원 Codex·ChatGPT | `durumari_open_context_workspace`의 global/thread entrypoint | `{}` 인자와 메뉴 metadata를 실제 SDK로 검증. 설치된 호스트의 메뉴 노출은 갱신 후 별도 확인 |
| Codex CLI·IDE, UI 미지원/차단 환경 | 원래 TextContent + 결과 브라우저 도구 | 기존 파서 호환과 실제 HTTP MCP→브라우저 작업 왕복 검증 |

지원은 호스트 이름으로 추정하지 않는다. UI를 렌더하지 않아도 원래 도구와 결과는
동작한다. 사용자 제공 메뉴 화면은 목표 표면이며 두루마리가 이미 등록됐다는 증거가 아니다.
상시 온라인이나 AI의 이해·답변 반영을 표시하지 않는다.

## 결과 브라우저

`durumari_context_result_browser`는 원래 쓰기의 `project_ref`와 `stored_request_key`로
먼저 영수증을 조회한다. 별도 `request_key`로 `context_activity/browse` 작업을 만들며
기존 immutable target·요청자·연결·scope 검사와 10분 만료를 재사용한다.
서버는 현재 요청자가 실제 저장한 문서·판본만 조회하고 원문이나 다른 사람 이력은 반환하지 않는다.
이 작업 종류를 허용하는 additive 제약 변경은 migration `0086_context_activity`다.

브라우저는 로그인 후 같은 task로 복귀하고, 카드가 로드되면 빈 completion으로 화면
표시를 회신한다. 저장을 다시 실행하거나 별도 확인 버튼을 요구하지 않는다.
`durumari_resource_status`의 completed는 화면 표시 회신이며 원래 stored와 다르다.
만료·취소·권한 상실·알 수 없는 저장 결과를 성공으로 바꾸지 않는다. 탭 닫기는 취소가 아니다.

## 메뉴 화면과 배포

정식 Agent 플러그인의 portable `plugin.json`과 Codex 전용 매니페스트는 같은
`Interactive` UI 지원 선언을 가진다. 실제 사이드바/대화 탭 진입은 기존
`durumari_open_context_workspace`의 global/thread metadata와 패키지에 포함된 UI resource가
담당한다. 기존 계정/폴더 연결의 인증과 권한을 그대로 사용하며, 로컬 검증용
도구 prefix·프로필·터널·샘플 데이터는 정식 패키지에 넣지 않는다.

탐색 화면은 프로젝트·Context 목록과 본문을 제공하고, 채팅의 기존 생성·수정 도구는
서버 `stored` 영수증에 근거한 결과 카드를 같은 UI resource로 표시한다. WRITE가 없는
계정은 기존 서버 권한 검사에서 거절되며 화면 진입이 권한을 추가하지 않는다.
이 등록 변경은 삭제 도구나 새로운 권한을 추가하지 않는다. UI는 확정 디자인으로
동결하지 않고 현재 Figma v2 표현을 유지해 후속 사용자 피드백에 따라 수정한다.

이미 배포된 버전의 아티팩트를 덮어쓰지 않는다. 이 변경은 다음 정식 릴리스에 포함할
소스·패키징 준비이며, 통합 브랜치 반영과 dev/main 승격·배포·호스트 갱신은 별도다.

Global/thread entrypoint는 별도의 프로젝트 선택을 시작하며 다른 대화나 폴더 설정을
바꾸지 않는다. 계정 연결의 기존 프로젝트 선택·목록·읽기 도구로 문서를 보여준다.
기존 프로젝트 고정 연결에는 계정 탐색 권한을 부여하지 않는다.

새 코드의 도구 정의는 서버와 패키지에 반영한 뒤 호스트에서 갱신해야 한다.
이 변경 자체는 개인 플러그인 업로드·공개 심사·dev/main 배포를 실행하지 않는다.

## 검증

- Python: `tests/test_activity_ui.py`, 기존 cloud protocol·project tools 회귀.
- PostgreSQL/HTTP MCP: 서버 저장소의 activity 결과·resource interaction·account MCP 회귀.
- Web: ActivityCard와 기존 ResourceInteraction/API 테스트.
- Browser: 서버 저장소의 웹 E2E. 실제 저장 작업의 로그인 복귀·자동 표시 회신과,
  별도로 **합성 host protocol harness**에서 실제 패키지 HTML을 검증한다. harness는 실제
  Codex/ChatGPT/Claude renderer의 증거가 아니다. 390/820/desktop에서 확인한다.
- 실제 browser 시나리오는 소유한 loopback 테스트 task JSON을 `DURUMARI_WIDGET_TASK`로
  지정한다. 자격 증명은 JSON에 넣지 않는다. CI에 fixture가 없으면 이 시나리오는 미실행이며
  통과로 계산하지 않는다. iframe harness는 독립적으로 실행된다.

공식 계약:
- https://github.com/modelcontextprotocol/ext-apps/blob/main/specification/2026-01-26/apps.mdx
- https://developers.openai.com/plugins/build/chatgpt-ui
- https://github.com/openai/mcp-extensions/blob/main/docs/spec.md#mcp-app-entrypoints
- https://support.claude.com/en/articles/13454812-use-interactive-connectors-in-claude

## 화면 안의 프로젝트·문서 탐색 (로컬 검증)

메뉴 진입점은 독립적인 conversation을 시작하고 `durumari_project_catalog`로
접근 가능한 프로젝트를 표시한다. 사용자 클릭은 기존 `durumari_project_select`를
호출하며, 반환된 immutable `project_ref`만 목록·검색·본문 요청에 전달한다.
프로젝트를 전환하면 기존 목록·본문을 즉시 비우고 이전 요청의 늦은 응답을 무시한다.
호스트나 폴더의 다른 대화 선택은 변경하지 않는다. 프로젝트 고정 연결은 해당
프로젝트의 Context만 보여준다.

같은 화면에서 프로젝트 검색·목록, Context 목록·검색, 선택 판본의 본문과
연속 페이지를 확인한다. 작은 화면은 `목록으로`로 돌아온다. 본문은 textContent로
표시하며 문서 안의 HTML을 실행하지 않는다. 로그인·최초 연결 승인은 기존 연결
절차이며 이 탐색 화면이 권한을 부여하거나 대신 승인하지 않는다.

로컬 플러그인은 계정·네트워크 접근 없는 샘플을 사용한다. `context-workspace.spec.ts`는
호스트 bridge harness에서 탐색·페이지 이어 읽기·전환 후 지연 응답 격리·오류 복구·검색을
검증한다. 이 변경의 dev/main 승격·배포나 실제 계정 검증을 의미하지 않는다.

## Figma v2 표현과 독립 자원

현재 디자인 정본은 DURUMARI-v2 (`s2axFaY2BQLtBmdBZ137bS`) 사용자 화면
`2665:59307`, 상태 가이드 `2200:14965`다. 흰색 탐색 영역, 연한 회색 배경,
파란 선택 상태, 16px 둥근 문서 읽기 영역과 Wanted Sans를 사용한다.
프로젝트·목록·본문의 탐색 구조는 유지하며 관련 없는 서비스 메뉴는 추가하지 않는다.

`activity_assets.py`는 원본 Figma 아이콘과 Web의 생성된 Wanted Sans subset을
data URI로 묶어 MCP HTML을 제공한다. 외부 이미지·글꼴 요청이나 임시 Figma URL에
의존하지 않는다.
기존 Web의 녹색 테마를 복제하지 않고 위 정본의 현재 토큰을 적용한다.

실제 계정 로컬 연결은 기존 `AccountContextClient`와 OS 보안 저장소의 별도 프로필을
사용한다. 탐색 전용 서버는 시작·프로젝트 조회/선택·Context 목록/검색/읽기만 노출하며
작성·비밀값 도구는 제외한다. 연결 승인 계정이 사용자 의도와 다르면 탐색에 사용하지
않고 올바른 계정으로 새 승인을 받는다. 승인 완료와 실제 목록·본문 검증을 구분한다.
