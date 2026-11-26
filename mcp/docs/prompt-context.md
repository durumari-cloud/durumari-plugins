# 질문별 관련 Context 주입

구현: `durumari_mcp/prompt_context.py`, 호출: `project_cli.project_hook`(UserPromptSubmit).

## 동작

Claude Code·Codex의 `UserPromptSubmit` 훅이 메시지마다 실행됩니다.

1. 건너뛰기: 빈 메시지, `/`·`$`·`!`·`#`로 시작하는 명령, "응"·"계속"·"ok"처럼 짧은 후속 메시지(공백 제외 4자 미만 포함). 이 단계는 자격 증명을 읽지 않습니다.
2. 깊이 결정(아래 순서). `off`면 요청하지 않습니다.
3. 토큰 확인: 만료 60초 이내·갱신 불확실·갱신 기록이 있으면 요청하지 않습니다(훅 마감에 갱신이 끊기지 않게).
4. 검색어 1~3개 추출: 조사·요청 어미 제거, 불용어·Context ID·링크·코드 블록 제외, 이슈 키·식별자·파일명 우선.
5. 검색어마다 `GET /api/v2/projects/{id}/contexts/search?q=&limit=&budget_tokens=1000`을 병렬로 보내고 **약 300ms**에 마감합니다. 늦은 응답은 버립니다.
6. 관련도 기준: 검색어 2개 이상에 걸린 문서 또는 제목에 검색어가 있는 문서. 검색어가 1개면 제목이나 발췌에 검색어가 있어야 합니다. 서버 검색은 점수를 주지 않으므로 일치 근거로 판단합니다.
7. 같은 세션에서 이미 넣은 문서(같은 revision)는 다시 넣지 않습니다. 압축(`SessionStart` `source=compact`)이나 `/clear`(`source=clear`) 뒤에는 기록을 비워 다시 넣습니다.
8. 출력: `hookSpecificOutput.additionalContext`에 제목·발췌(또는 요약)·`[id@r{revision}]`·관계만. 본문 전체는 넣지 않으며 "신뢰하지 않는 근거" 머리말을 붙입니다. 토큰은 UTF-8 바이트/3으로 보수적으로 어림해 상한 안에서 항목 단위로 자릅니다.

실패(연결 끊김·권한·시간 초과)는 아무것도 출력하지 않고 재시도 대기 기록도 남기지 않습니다. 연결 문제는 SessionStart가 알립니다.

## 깊이

| 단계 | 키 | 결과 수 | 그래프 이웃 | 토큰 상한 |
|---|---|---|---|---|
| 끄기 | `off` | 0 | 없음 | 0 |
| 가볍게 | `light` | 3 | 없음 | 300 |
| 보통(기본) | `normal` | 3 | 1단계, 최대 2 | 800 |
| 깊게 | `deep` | 5 | 2단계, 최대 5 | 2,000 |

수치는 초기값입니다. 단계별 실제 토큰 사용량과 답변에 쓰인 비율을 측정해 조정합니다(`prompt_context.DEPTHS`).

### 우선순위

1. 환경변수 `DURUMARI_MCP_CONTEXT_DEPTH`(세션 단위 임시 변경, 진단용; 이전 이름 `DURUMARI_CONTEXT_DEPTH`도 읽음)
2. 사용자 설정 — 프로젝트별(`prompt_context.projects.<project_id>`)
3. 사용자 설정 — 기본(`prompt_context.depth`)
4. 프로젝트 기본 — 서버 값(세션 시작 시 캐시)
5. 제품 기본 `normal`

값은 영어 키 또는 한국어(`끄기`·`가볍게`·`보통`·`깊게`)를 받습니다.

### 사용자 설정 파일

`<사용자 데이터 폴더>/settings.json`(`~/.durumari/settings.json`, 이전 설치는 `~/.malmoa/settings.json`; 0600, 자격 증명 없음):

```json
{
  "version": 1,
  "prompt_context": {
    "depth": "light",
    "projects": {"0d1d8f6d-...": "off"}
  }
}
```

변경: `durumari-mcp prompt-context --workspace <root> --depth <키> [--all-projects]`(플러그인 wrapper `durumari_plugin.py prompt-context`도 같음). `--depth` 없이 실행하면 현재 단계와 출처(`environment`·`user_project`·`user`·`project`·`default`)를 보여 줍니다. 다음 메시지부터 적용됩니다.

### 프로젝트 기본 — 서버 설정

프로젝트 읽기 권한이 있는 구름 소유자가 서버에 기본 깊이를 저장합니다.
`GET/PUT /api/v2/projects/{project_id}/context-injection`에서 조회·변경하며,
허용 값은 `off`·`light`·`normal`·`deep`, 기존·신규 프로젝트 기본값은 `normal`입니다.
변경 입력은 `{"depth":"light"}`이며 브라우저 요청은 CSRF 토큰이 필요합니다.
Agent 연결 자격 증명으로 설정을 변경할 수 없습니다.

- `GET /api/v2/agent/connection` 응답에 `"context_injection": {"depth": "off"|"light"|"normal"|"deep", "source": "project"}`.
- 세션 시작 훅이 이미 이 엔드포인트를 읽으므로(응답 언어와 같은 요청) 값을 세션 스냅숏(`hook-health/<profile>/session-snapshot.json`의 `prompt_context_depth`)에 저장하고, 메시지 훅은 로컬 스냅숏만 읽습니다. 메시지마다 추가 요청은 없습니다.
- 필드가 없으면(이전 서버) 제품 기본 `normal`.
- 프로젝트 기본값 변경은 다음 세션 시작에 적용됩니다. 사용자 설정이 우선합니다.

저장·권한·검증 범위는 [프로젝트 기본 깊이 계약](project-context-depth.md)을 참고하세요.

`durumari.yaml`에 두지 않은 이유: 0.4.x의 `read_manifest`는 모르는 키가 있으면 `invalid_project_manifest`로 거부하므로, 키를 추가하면 이전 클라이언트가 연결 자체를 잃습니다. 넣으려면 `schema_version`을 올리는 업데이트 계약(!238)을 거쳐야 합니다.

## 문제 시 끄기

`off`는 자격 증명을 읽기 전에 멈춥니다(서버 요청·상태 파일 없음, 종료 코드 0).

| 범위 | 방법 |
|---|---|
| 이 사용자, 모든 프로젝트 | `durumari-mcp prompt-context --depth off --all-projects` 또는 "관련 문서 안내 꺼 줘" |
| 이 사용자, 이 프로젝트 | `durumari-mcp prompt-context --workspace <root> --depth off` |
| 호스트 프로세스 전체(긴급) | 호스트를 띄우는 환경에 `DURUMARI_MCP_CONTEXT_DEPTH=off` |
| 훅 자체 제거 | 설정 파일에서 `UserPromptSubmit` 두루마리 항목 삭제(다시 추가하지 않음) |
| 프로젝트 기본(서버) | 설정 API에 `{"depth":"off"}` 저장 — 사용자 설정이 있으면 사용자 설정이 우선 |

질문 훅의 예상하지 못한 예외는 내용 없는 진단 라벨(`last-failure.json`, event `other`)만 남기고 출력 없이 끝나며, 메시지를 막거나 실패로 만들지 않습니다. SessionStart·Stop의 기존 동작(예외 시 종료 코드 유지)은 바꾸지 않았습니다.

## 그래프 이웃(트랙 E 자리)

검색 항목에 `neighbors`(또는 `related`) 배열이 있으면 각 항목 `{id, title, relation, revision, hop?}`을 깊이의 단계·개수 제한 안에서 표시합니다(`hop` 기본 1). 현재 서버 검색 응답에는 이웃이 없으므로 검색 결과만 넣습니다. 트랙 E가 읽기 응답에만 이웃을 넣는다면, 300ms 안에서 상위 1건 읽기를 추가하거나 검색 응답에 이웃 요약을 싣는 것 중 하나를 통합 단계에서 정합니다.

## 검토·결정 요청 알림(`request_notices.py`)

- 출처: Policy 색인 응답의 `review_requests`(`needs_me`·`recent`). 서버는 `requests:read`가 있을 때만 이 필드를 보낸다. #732는 알림이 없는 첫 확인도 빈 필드로 명시하여 기준선을 저장한다. 권한이 없으면 필드를 생략하며 권한·사용자·프로젝트 조회 경계는 유지한다.
- 세션 시작
  - `needs_me`(최대 5건)는 미결인 동안 매번 넣는다.
  - `recent`(최대 10건)는 이미 저장된 커서 이후만 넣는다. 프로필·프로젝트의 첫 확인은 과거 이력의 기준선만 저장하며 예전 이벤트 제목을 재생하지 않는다. 현재 `needs_me`는 첫 확인에도 표시한다. 이미 `needs_me`로 보인 요청의 변화는 겹쳐 넣지 않는다.
  - 과거 이벤트의 생성·응답 동작과 요청의 현재 `state`를 구분한다. 완료 요청의 옛 `create`를 새 요청으로 부르지 않는다. 상태가 없는 이전 서버의 응답은 현재 상태 미확인으로 표시한다. 응답 필요는 현재 `needs_me`만 근거로 삼는다.
- 메시지마다
  - 관련 Context 검색을 하는 메시지에서만 Policy 색인을 한 번 더 읽는다. 같은 300ms 마감 안에서 끝나야 한다.
  - 저장된 기준선 이후 `recent`의 새 항목만 붙인다. 첫 확인 또는 커서 기록 불가 시에는 현재 `needs_me`도 보존한다. 늦은 응답은 버린다.
  - 검색을 하지 않는 메시지는 서버를 부르지 않는다. 이때 알림은 다음 세션 시작에 보인다.
    - 해당: 깊이 `off`, 짧은 후속 메시지·명령, 검색어가 나오지 않는 메시지(숫자·링크뿐인 경우 등)
    - 검색 여부 판단은 `prompt_context.searches` 하나를 같이 쓴다.
- 커서
  - 위치: `hook-health/<profile>/request-cursor.json`
  - 내용: 프로젝트별 마지막 `at`과 그 시각의 `event_id`만 둔다. 제목·본문·자격 증명은 넣지 않는다.
  - 명시적으로 알림이 없는 첫 확인도 빈 기준선을 저장하므로 이후 들어오는 실제 첫 검토·승인 결과를 버리지 않는다. 다른 프로필·기기의 첫 확인도 이전 이력 재생 없이 기준선을 잡는다.
- 실패
  - 세션 시작에서 서버에 닿지 못하면 "요청 확인 불가"를 넣는다. 토큰을 갱신해야 하는 경우와 연결 끊김 알림(`request_notice`)도 같다.
  - "없음"으로 표시하지 않는다. 요청 목록은 캐시하지 않는다.
  - 프로필이 없거나 커서를 저장하지 못하면 과거 제목을 반복하지 않고 `기록 확인 불가`와 목록 수동 확인 안내를 넣는다. 현재 응답 필요 목록은 계속 표시한다. 기록을 저장한 뒤에만 새 변화를 출력하므로, 기존 기준선이 있는 저장 실패는 복구 후 미소비 변화를 다시 전달한다. 최초 기준선조차 저장할 수 없었던 기간은 수동 이력 확인이 필요하며 정확한 자동 중복 제거를 주장하지 않는다.
  - 정상 커서 이후의 새 승인·거절·보류·검토·보완 결과는 현재 완료 여부와 관계없이 전달한다. 완료 알림 전체를 삭제하거나 완료 요청을 다시 pending으로 바꾸지 않는다.
- 길이: 제목 120자, 전체 1,500자. 넘치는 항목은 `durumari_request_list` 안내 한 줄로 바꾼다.

## 기존 설치에 훅 추가(업데이트 계약 사용자 기기 계층)

세션 시작(압축 제외) 때 `.claude/settings.local.json` 또는 `.codex/hooks.json`에 이 profile의 `SessionStart` 훅은 있고 `UserPromptSubmit` 훅이 없으면 템플릿으로 다시 씁니다. 다음 경우에는 파일을 바꾸지 않습니다.

- 두루마리 훅 중 하나라도 꺼져 있음(`disabled: true` 또는 `enabled: false`)
- 다른 profile의 두루마리 훅이 같은 파일에 있음
- 이미 `UserPromptSubmit` 훅이 있음
- 이 폴더·호스트에서 한 번 추가했거나 이미 있는 것을 확인함(`hook-health/<profile>/prompt-hook-*.json` 표식). 그 뒤 사람이 훅 항목을 지우면 다시 넣지 않습니다. 끄는 기본 방법은 깊이 `off`입니다.

호스트는 다음 세션부터 새 훅을 읽고, 자체 훅 승인 절차를 그대로 거칩니다. 바꾼 경우 SessionStart `systemMessage`로 알립니다.

## 호스트별

| 호스트 | 메시지 훅 | 압축 후 재주입 |
|---|---|---|
| Claude Code | `UserPromptSubmit` | `SessionStart` `source=compact` |
| Codex | `UserPromptSubmit`(입력 `prompt`·`turn_id`) | 공식 문서 기준 `SessionStart` `source`에 `compact`·`clear`가 있고 `PreCompact`·`PostCompact`도 있음. 실제 호스트 동작은 통합 단계에서 확인 |

## 실측 (2026-10-17, macOS, durumari.kr, 이 저장소 profile, 훅 명령 직접 실행)

| 경우 | 전체 시간(파이썬 기동 포함) | 결과 |
|---|---|---|
| "응" (건너뛰기) | 0.11초 | 출력 없음 |
| 질문, 토큰 유효 | 0.48~0.53초 | 관련 문서 2~3건, 상위가 실제 결정 문서 |
| 같은 세션 같은 질문 | 0.50초 | 출력 없음(중복 제거) |
| 질문, 접근 토큰 만료 | 2.3초(첫 실행은 키체인 확인 창 때문에 48초) | 출력 없음 |

- 서버 왕복(TLS 포함)은 65~80ms, 검색 1건 응답은 0.12~0.17초. 검색 3건 병렬은 이미 읽은 profile로 전송 계층을 직접 쓰면 300ms 안에 끝납니다(`context_request`는 요청마다 OS 저장소에서 profile을 다시 읽어 0.25~0.31초).
- 훅 진입은 `cli.hook_parser`로 전체 명령 정의(MCP SDK import, 약 0.9초)를 건너뜁니다. SessionStart·Stop도 같이 빨라집니다.
- **접근 토큰 수명 15분**(`ACCESS_TTL`). 훅은 토큰을 갱신하지 않으므로(훅 마감에 회전이 끊기면 재로그인이 필요해질 수 있음) MCP 도구를 15분 넘게 쓰지 않은 세션에서는 주입이 0건입니다. 훅에서 갱신을 허용할지(마감 3초 안, 갱신 저널 복구에 의존) 결정이 필요합니다.
- 검색 발췌(`snippet`)는 일치 위치 앞 24자부터 80자라 문장 중간에서 시작합니다. 검색 응답에 문서 `summary`가 있으면 그것을 씁니다(서버 변경 제안).

## 검증하지 않은 것

- 실제 Claude Code·Codex 호스트에서 훅 등록·출력 반영(위 실측은 훅 명령을 직접 실행한 것)
- Windows `commandWindows` 경로
- 서버 검색의 한국어 정확도에 따른 관련도 기준 조정(바이그램 색인 이후)
- 단계별 토큰 사용량과 답변 반영률 측정
