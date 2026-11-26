# 두루마리 Agent 연결 계약 (현행 기준)

상태: **초안**. 코드·스킬·참조 문서·오류 안내는 이 문서와 일치해야 한다. 다르면 이 문서를 근거로 고치거나, 이 문서를 먼저 개정한다.

- 기준: runtime·plugin 0.4.9의 현행 연결 계약. [폴더 소스](folder-sources.md) 계약을 반영한다.
- 표기: **[현행]** 코드에서 확인한 동작. **[불일치]** 계약과 다른 현재 동작(수정 대상). **[미정]** 팀 결정이 필요한 항목.

## 1. 지원 범위

| 항목 | 계약 |
|---|---|
| 지원 호스트 | Codex, Claude Code는 플러그인과 연결 설정을 제공한다. Cursor, Gemini CLI, OpenCode는 연결 설정을 제공하며 실제 사용 검증은 릴리스 기록에서 따로 표시한다. Copilot CLI는 명시 선택 시에만 설정하는 실험 경로다. |
| OS | Windows, macOS, Linux. 실제 검증한 OS·호스트 조합만 "검증됨"으로 표시한다. |
| 버전 | wrapper(플러그인)와 runtime(`durumari-mcp`)은 같은 버전으로 함께 배포한다. 0.3.x 플러그인·wrapper는 **미지원**이다(2026-10-13 결정: 개발진 외 배포 대상 없음). |
| 대화 수집 | **제공하지 않는다.** 연결 프로필은 `context_only=True`, `capture_enabled=False`다. 훅은 안내만 한다. |
| 비Git 작업 폴더 | 새 비Git·origin 없는 폴더는 `context-connect --workspace <폴더>`가 폴더 소스 ID를 자동 준비하고 브라우저 승인을 요청한다. UUID를 직접 입력하지 않아도 된다(아래 3절). |

새 공용 연결 형식을 사용하는 팀은 **wrapper·runtime 0.4.9 이상으로 모두 업데이트하고 호스트를 재시작한 뒤** 연결 조회·제거·재연결을 사용한다. Git 공용 manifest는 `context_mode`를 쓰지 않아 0.4.8의 형식 검증은 통과하지만, 0.4.8의 CLI는 공용 파일에서 profile을 찾으므로 새 형식의 연결 조회·제거·재사용을 지원하지 않는다. 새 형식은 0.4.9의 `.durumari/context-binding.json`과 함께 사용한다. 새 wrapper는 같은 버전의 번들 runtime을 설치·검증하며, 직접 실행 중인 구형 CLI·MCP 프로세스도 업데이트 후 재시작해야 한다. 구형 manifest는 계속 읽는다.

## 2. 진입점

### 2.1 사용자·Agent가 쓰는 명령 (wrapper `scripts/durumari_plugin.py`)

| 명령 | runtime 호출 | 하는 일 |
|---|---|---|
| `connect --workspace <작업 폴더> [--client <agent>] [--server] [--project]` | `context-connect --agents <client>` | 브라우저 승인으로 이 폴더를 연결한다(4절). runtime이 없으면 번들 wheel로 설치한다. |
| `status --workspace <루트> [--change git-init\|move\|detach]` | `context-status [--change]` | 연결 상태를 서버에서 확인한다. `--change`는 서버·자격 증명 없이 로컬에서 변경 전 재연결 필요 여부를 예측한다. |
| `remove --workspace <루트>` | `context-remove` | 이 폴더의 연결을 해제한다(8절). |
| `install` | `uv tool install <번들 wheel>` | runtime만 설치한다. 자격 증명은 바꾸지 않는다. |

### 2.2 runtime 명령 (`durumari-mcp`)
`context-connect`, `context-status`, `context-remove`, `context-serve`(MCP 서버), `context <list|search|read|save|revise|delete|restore|history|operation>`(CLI), `update`/`release-install`, `project-skills`, `project-device`, `secret <put|list|info|status|run|env|cache>`(값은 파이프·자식 환경으로만, 출력 없음:·248), `prompt-context`(질문별 관련 Context 주입 훅:).

### 2.3 MCP 도구 (`context-serve`, 14개)
`durumari_context_search`, `durumari_context_list`, `durumari_context_read`, `durumari_context_related`, `durumari_context_save`, `durumari_context_revise`, `durumari_context_version`, `durumari_context_operation`, `durumari_policy_index`, `durumari_project_status`, `durumari_skill_list`, `durumari_skill_read`, `durumari_secret_list`, `durumari_secret_status`.

비밀값 도구 두 개는 읽기 전용(title·readOnlyHint)이며 metadata 권한이 있는 이름·버전·상태만 반환한다. 값·등록·교체·삭제·공유 도구는 없다. 새 값 등록은 로컬 `durumari-mcp secret put`(파이프)으로만 한다. 같은 이름 교체는 `secret put NAME --stdin --replace`로 요청만 하고(값은 암호화 보관, 항상 승인 필요), 실제 교체는 보안 화면에서 비밀번호 재확인 후 승인할 때만 일어난다(쓰기 권한 없거나 없는 이름은 같은 `secret_not_found`). Streamable HTTP(`/api/v2/mcp`)도 같은 두 도구를 공개한다. **[현행]**

도구 노출은 요청 URL이 아니라 토큰의 연결 종류로 정한다. Agent 기기 연결은 위 도구 전부, Chat OAuth 연결(`/mcp`)은 Context·Policy·프로젝트 상태 도구 10개만 보고 비밀값 도구는 아직 보지 않는다(서버가 연결 출처를 매번 검사). `durumari_context_related`는 이웃 문서의 관계·제목·ID만 준다(`docs/context-neighbors.md`). **[현행]** (·245·247)

`durumari_policy_index`는 인자 없이 부르면 시작 색인을 준다. 전체 행이 예산에 들어가지 않으면 기존 크기 안에서 적용 조건을 줄여(끝에 `…`. `when_truncated`는 원문이 160자를 넘을 때만) 더 많은 Policy를 싣고, 그래도 남으면 `truncated`와 `next_after`를 준다. `after=next_after`로 이어 읽고, `after=0`부터 읽으면 적용 조건을 160자까지 준다. 인자를 모르는 옛 클라이언트와 hook도 시작 색인의 행은 그대로 읽는다.

서버 안내문은 HTTP 서버가 덧붙이는 문구까지 2,048자 이내다(Claude Code의 전달 상한). 안전 규칙은 안내문에, 한 도구의 절차는 그 도구 설명에, 실패 뒤 절차는 `error.details.next_step`에 둔다.

`durumari_document_types`는 공간의 문서 유형(표준·사용자 정의)과 기본·추가 컬럼을 웹 문서 유형 화면과 같은 목록으로 주는 읽기 전용 도구다. 프로젝트 고정 연결은 자기 공간, 계정 연결은 `target_project_id` 또는 `project_ref`로 대상을 정하며 다른 읽기 도구와 같은 검사를 거친다. 계정 연결의 `durumari_account_search`는 웹 통합 검색과 같은 서비스로 연결이 읽을 수 있는 종류를 한 번에 찾는다. **[현행]**

계정 연결이 공간을 지정하지 않으면 서버가 `project_selection_required`(409)에 `accessible_spaces`·`next_action`(`retry_with_target_project_id`, `project_ref` 전용 도구는 `select_with_durumari_project_select`)을 담아 돌려주고, `durumari_project_status`도 같은 목록을 준다. 읽을 수 있는 공간이 하나뿐이면 Context 읽기는 그 공간으로 처리한다(`target_source: only_readable_space`). 쓰기·접수 확인·회의록 같은 작업 도구는 항상 대상을 지정한다. `durumari_project_catalog`는 `conversation_ref` 없이 읽기 전용으로 공간 목록을 주고, `durumari_my_work`는 공간을 가로질러 내 미완료 작업 기록과 내 처리가 필요한 검토 요청을 준다. **[현행]** (2026-11-18 ChatGPT 공간 선택 보고)

### 2.4 폐기한 명령

| 명령 | 계약 |
|---|---|
| `project-connect`, `project-init`, `project-doctor`, `project-preflight`, `project-import`, `project-export` | 실행 즉시 `agent_upgrade_required_use_context_connect`와 현행 명령 안내를 내고 파일을 바꾸지 않는다. **[현행]** (init·doctor·preflight 0336cf1, project-connect 8dc8289, import·export는 결정 4) |
| `project-login/configure/sync/status/logout/forget` | 같은 오류로 거절한다. **[현행]** |
| `project-serve` | `context_only` 프로필이면 `context-serve`로 넘긴다(기존 설정 호환). 그 외는 거절. **[현행]** 0.5.0에서 폐기한다(결정 5) |
| `project-hook` | 현행 훅 진입점이다(6절). |
| 구형 모듈 `connect_cli`, `browser_project_connect`, `codex_host`, `setup_files`, `profiles`, `project_server`, `project_work`, `context_tools`, `onboarding_cli`의 실행부 | 어떤 현행 명령에서도 호출되지 않아 제거했다. `project_cli`는 훅만, `onboarding_cli`는 폐기 안내용 인자 정의만 남겼다. **[현행]** `capture.py`의 민감정보 검사(`contains_sensitive`)는 현행 저장 경로가 쓰므로 남긴다. 그 밖의 `capture.py` 수집 함수·`capture_policy`·`capture_errors`와 `onboarding.py`의 이관 함수는 제거했다. |

## 3. 연결 조건

- 선택한 폴더가 origin을 가진 Git 루트면 기존 Git 저장소 연결을 사용한다. 새 비Git·origin 없는 Git 루트·별도로 선택한 하위 폴더는 폴더 소스 ID를 OS 보호 등록 기록에 자동 준비하고 브라우저 승인을 요청한다. 사용자가 UUID를 입력할 필요는 없다. 기존 manifest의 `folder_source_id`와 명시적 `--folder-source <UUID>`도 계속 지원한다. 폴더 소스 연결은 선택한 폴더의 절대 경로를 승인하며 서버의 `folder_sources` 지원이 필요하다. **[현행]**
- Git 조회가 실패하거나 기존 Git 연결에서 origin이 사라진 경우에는 폴더 소스로 암묵 전환하지 않고 `repository_root_and_remote_required`로 멈춘다. Git·접근 권한·origin 설정을 확인해야 한다. 폴더 소스로의 전환은 명시적인 선택과 새 브라우저 승인이 필요하다. **[현행]**
- Git 연결의 `repository_id`는 정규화한 origin URL(인증 정보 제외)의 해시이고 폴더 연결은 폴더 소스 ID의 해시다. 기존 Git 연결에서 다른 저장소로 remote가 바뀌면 `project_repository_mismatch_reconnect`로 정지하며 새 승인이 필요하다. 승인된 폴더 소스는 같은 루트에서 Git을 초기화하거나 origin을 추가·제거해도 유지한다. 기존 URL 동등성·해시와 전환 경계는 [폴더 소스 계약](folder-sources.md)의 Git 호환 절을 따른다. **[현행]**
- 작업 폴더 식별값 `workspace_identity`는 절대 경로의 해시다. 폴더를 옮기거나 복사하면 새 승인이 필요하다. 연결 파일 복사로는 접근을 얻지 못한다. **[현행]**
- 저장소는 구름 owner가 먼저 등록해야 승인 화면에서 선택할 수 있다.

## 4. connect 동작

1. 선택한 작업 폴더와 기존 바인딩을 확인하고 OS 보안 저장소를 연다. 새 폴더는 Git 원격 또는 자동 준비한 폴더 소스로 식별한다. 미완료 제거 표시가 있으면 먼저 회수를 마친다(`reconcile`).
2. `.durumari/context-binding.json`(구형이면 `durumari.yaml`)의 profile이 유효하고 서버 연결 정보(연결 id·프로젝트·저장소·작업 폴더)와 일치하면 **브라우저 없이** 설정 파일만 다시 쓴다. 서버가 401·403·410이면 기존 profile을 비활성화하고 새 등록으로 간다. 기존 profile이 저장소 불일치(`project_repository_mismatch_reconnect`)나 작업 폴더 밖(`workspace_out_of_scope`)이면, 그 profile은 그대로 두고 새 등록(브라우저 승인)으로 간다. remote 변경, 폴더 복사·이동, worktree가 여기에 해당한다. **[현행]**
3. 새 등록: 승인 URL을 stderr에 출력하고 브라우저를 연다. 2초 간격, 최대 600초 확인한다.
   - 승인 → 범위(호스트 `mcp`, 권한 `context:read`·`context:write`, 저장소·작업 폴더) 검증 → 자격 증명 저장 → 설정 파일 기록 → 완료 통보.
   - 거절 → `project_connection_cancelled`(다음 실행에서 새 승인).
   - 시간 초과 → `browser_approval_pending`. 만료·범위 변경은 새 요청으로 다시 시작한다.
4. 출력: `{"project_id", "configured_agents", "profile", ...}`.

사람이 브라우저에서 계정·구름·프로젝트·저장소·작업 폴더를 확인하고 승인한다. Agent는 대신 승인하지 않는다.

## 5. 생성·변경 파일

| 파일 | 내용 | Git | 제거 시 [현행] |
|---|---|---|---|
| `.mcp.json` (Claude/Copilot), `.codex/config.toml`, `.cursor/mcp.json`, `.gemini/settings.json`, `opencode.json[c]` | `durumari_project` MCP 항목(`context-serve --profile <name>`). 다른 항목 보존, 같은 이름의 남의 항목은 `existing_mcp_binding_conflict` | 제외(.gitignore) | 이 profile 항목만 제거 |
| `.claude/settings.local.json`, `.codex/hooks.json` | SessionStart·Stop 훅 | 제외 | 이 profile 훅만 제거 |
| `durumari.yaml` | 팀 공용 값: 서버·프로젝트·저장소 id·폴더 소스, 그리고 처음 쓴 연결의 `source_id` | **추적** | **지우지 않는다.** 구형 파일에 이 profile의 `profile`·`workspace_identity`가 있으면 그 두 키만 뺀다(결정 1) **[현행]** |
| `AGENTS.md`, `CLAUDE.md`, `GEMINI.md` | 관리 블록. **선택한 Agent의 파일만** 만든다(claude → CLAUDE.md, gemini → GEMINI.md, codex·cursor·opencode·copilot → AGENTS.md). 이미 관리 블록이 있는 파일은 계속 갱신한다(결정 3) **[현행]** | 추적 | 관리 블록 제거. 비게 된 파일은 삭제(결정 2) |
| `.gitignore` | 위 로컬 설정 경로 중 없는 것을 `# Durumari local Agent settings` 블록으로 추가 | 추적 | 이 연결이 추가한 줄만 뺀다. 블록이 커밋돼 있으면 팀 공용으로 보고 남긴다(결정 2 보완) **[현행]** |
| `.durumari/context-binding.json` | 사용자별 값: profile, 연결된 Agent 목록, workspace_identity, 연결이 만든 파일(`created_files`)·폴더(`created_directories`)·`.gitignore` 줄(`gitignore_added`)·Codex `[features] hooks = true`(`codex_hooks_feature`) | 제외 | 삭제 |
| 사용자 데이터 폴더 `backups/<작업 폴더 식별자 16자>/<경로 해시>-<파일 이름>` (macOS·Linux `~/.local/share/durumari`, Windows `%LOCALAPPDATA%\DURUMARI`) | 바뀐 파일의 직전 내용, 파일마다 최신 1개 | 저장소 밖 | 남음 |
| `*.durumari-backup-*` (0.4.11 이하가 저장소에 남긴 것) | 바뀐 파일의 이전 내용. 새 runtime은 만들지도 지우지도 않고 `context-status`의 `local_config.repository_backups`로 목록과 정리 방법만 안내한다 | 제외 | 남음 |

제거 시 우리 항목을 지운 뒤 **설치기가 새로 만들었다고 `created_files`에 기록한 파일만**, 빈 텍스트나 빈 MCP·훅 설정뿐이면 삭제한다. 원래 있던 파일, 사용자 주석·명시한 빈 설정, 생성 기록이 없는 구형 연결의 파일은 보존한다. 백업 파일은 남긴다. `.gitignore` 줄, Codex hooks 기능, 빈 폴더는 연결 기록에 있는 것만 되돌린다. 0.4.31 이하 기록에는 이 항목이 없어, 연결이 새로 만든 `.gitignore`·`.codex/config.toml`과 그 폴더만 정리한다(결정 2 보완) **[현행]**.

`durumari.yaml`의 `schema_version`은 공용 파일 형식 번호다(현재 1). 0.4.x runtime은 1만 읽으므로 번호를 올리는 형식 변경은 직전 runtime도 새 형식을 읽게 된 뒤에만 한다. runtime은 실행할 때 이 profile의 구형 키(`profile`·`workspace_identity`)를 `.durumari/context-binding.json`으로 옮기고(여러 번 실행해도 결과 같음), 더 새 번호의 파일은 바꾸지 않고 `client_update_required`로 업데이트를 안내한다. 공용 `durumari.yaml`은 비어도 남긴다(결정 1).

## 6. 훅

| 이벤트 | 동작 [현행] |
|---|---|
| SessionStart | 바인딩 확인 후 짧은 안내와 Policy 색인을 `additionalContext`로 준다. 서버 가용성은 확인하지 않는다. 실패하면 사유 코드와 "기록 보존" 안내를 준다. |
| UserPromptSubmit | 짧은 후속 메시지·명령은 건너뛰고, 검색어 1~3개로 관련 Context를 약 300ms 안에 찾아 제목·요약·ID·revision만 `additionalContext`로 준다. 깊이 4단계, 같은 세션 중복 제거, 압축 후 재주입. 실패는 출력 없이 넘긴다([prompt-context.md](prompt-context.md)). |
| Stop | 이 세션에 확인되지 않은 저장이 있으면 "같은 요청 키로 확인" 안내만 한다. |
| 그 외 | 무시한다. |

- 훅은 **보조 안내**이며 대화를 수집하거나 Agent 작업을 막지 않아야 한다.
- **[불일치]** 예상하지 못한 예외는 다시 던져져 종료 코드 2로 끝난다(`project_cli.project_hook`). Claude Code에서 종료 코드 2가 종료 차단으로 해석되는지 확인이 필요하다(142와 조율).
- Windows 인코딩 결함은 142에서 다룬다.

## 7. 저장 계약

- 쓰기(`save`, `revise`, `version`)는 안정적인 `request_key`를 쓴다. **서버 영수증의 `status=stored`만 저장 완료다.**
- 응답이 끊기면 같은 키로 `durumari_context_operation`을 조회한다. 새 키로 다시 쓰지 않는다.
- 같은 키로 같은 변경을 다시 보내면 서버 영수증이든 로컬 큐의 영수증이든 같은 `context_id`·`revision`과 `replayed: true`를 낸다. 다른 변경이면 `context_idempotency_conflict`다 **[현행]**.
- activate 없는 restore는 효력 없는 `stale` 새 버전이다. 기본 보기에는 절이 나오지 않고 `--revision`으로 읽는다. CLI 결과는 `state: stale`과 `next_action: read_with_revision_or_restore_with_activate`를 덧붙인다. 현재 문서로 되살릴 때만 `activate: true`를 쓴다 **[현행]**.
- 미확인 요청은 로컬 암호화 큐에 남고 임의로 폐기하지 않는다.
- `status`(CLI·MCP `durumari_project_status`)의 `pending_writes`는 **연결 전체**의 미확인 쓰기 수이고, `pending_writes_this_session`은 현재 세션만 센다(결정 6) **[현행]**. Stop 훅 알림은 세션 기준이다.

## 8. 제거 계약

순서 [현행]:
1. 제거 표시를 기록한다.
2. profile을 비활성화하고 전송 큐를 멈춘다.
3. 로컬 설정을 제거한다(5절).
4. 서버에 회수를 요청한다.

| 결과 | 의미 |
|---|---|
| `{"status":"removed","server_revocation":"confirmed"}` | 로컬 제거와 서버 회수 완료 |
| `{"status":"removed_locally","server_revocation":"pending"}` | 서버 응답 없음. 다음 connect/remove가 회수를 다시 시도한다. 그 전에는 새 연결이 `project_removal_pending_retry`로 멈춘다 |
| `{"status":"not_connected","server_revocation":"not_pending"}` | 이 폴더에 연결 없음 |

**제거 후에도 남는 것:**
- 서버의 프로젝트와 Context 문서
- 플러그인
- 공용 runtime
- 다른 폴더·프로젝트의 연결
- 이 profile의 암호화 큐와 격리 자료

제거 결과는 상태와 함께 `local_files`(`removed`: 지운 생성 파일, `restored`: 우리 항목만 빼고 남긴 사용자 파일)와 `kept`(`server_project_and_context`, `plugin`, `shared_runtime`, `other_folder_connections`, `encrypted_queue_and_quarantine`, 폴더에 `durumari.yaml`이 있으면 `shared_project_manifest`)를 낸다. 연결이 없으면 `not_connected`만 낸다. **[현행]**

구버전(0.3.x) 연결의 미전송 기록은 지우지도 옮기지도 않고 **보존만** 한다. 이전 API가 폐기되어 보낼 수 없고, 0.3.x는 미지원이다. 필요하면 사람이 내용을 보고 새 Context로 다시 저장한다(결정 7).

## 9. 상태와 전이

| 상태 | 판정 | 다음 |
|---|---|---|
| 미연결 | profile·바인딩 없음 | connect → 승인 대기 |
| 승인 대기 | 등록 요청 진행 중(최대 600초) | 승인 → 연결됨 / 거절·만료 → 미연결 |
| 연결됨(`active`) | 서버 연결 정보와 profile 일치 | remove → 제거 대기 또는 미연결 / 서버 회수 → 회수됨 |
| 정지(저장소 불일치) | `repository_id` 또는 작업 폴더 불일치 | 브라우저 재승인 → 연결됨 |
| 제거 대기 | 로컬 제거 완료, 서버 회수 미확인 | 다음 connect/remove에서 회수 → 미연결 |
| 회수됨 | 서버가 401·403·410 | connect → 새 승인 |
| 쓰기불가 | 로컬 암호화 큐가 손상되어 모든 쓰기가 `context_outbox_unavailable`로 실패. 읽기는 정상 | 원본을 보존한 채 큐를 격리하고 재생성 → 연결됨. 제품 경로는 146에서 만든다 |

`쓰기불가`는 2026-10-13 실제로 관찰되어 위 표에 넣었다.

## 10. 오류 코드와 사용자 안내

| 코드 | 원인 | 사용자 행동 | 현재 안내 |
|---|---|---|---|
| `repository_root_and_remote_required` | Git 조회 실패 또는 기존 Git 연결에서 origin 제거 | Git·저장소 접근 권한·origin 설정 확인 후 재시도. 기존 Git 연결을 폴더 소스로 암묵 전환하지 않음. 새 비Git·origin 없는 폴더의 연결에는 UUID 입력 불필요 | 있음 |
| `project_repository_mismatch_reconnect` | remote·경로 변경 | 브라우저로 다시 연결 | 있음(`context-connect --workspace`) |
| `project_credential_missing`, `project_login_required` | 이 기기에 유효한 연결 자격 증명 없음 | connect | 있음(`context-connect --workspace`). 로컬 연결 기록이 없는 폴더는 "아직 연결되지 않음", 있으면 "다시 연결, 기록 보존" |
| `workspace_not_found` | `--workspace` 폴더가 없음 | 경로 확인 또는 폴더 생성 | 있음. 아무것도 바꾸지 않음 |
| `context_id_required`, `context_query_required`, `context_request_key_required`, `context_input_required`, `context_input_fields_required` | `context` 명령의 인자·`--input` 필드 누락(delete: `base_revision`, restore: `base_revision`·`restore_revision`) | 빠진 인자 지정 | 있음. delete·restore는 `--input` 예시 |
| `project_connection_cancelled` | 승인 거절 | 같은 명령 재실행 | 있음 |
| `browser_approval_pending` | 600초 내 미승인 | 같은 명령 재실행 | 있음 |
| `project_removal_pending_retry` | 이전 제거의 서버 회수 미완료 | 네트워크 확인 후 재시도 | 있음 |
| `project_access_denied` | v1 경로의 401·403·404·410 | 최신 플러그인으로 다시 승인 | 있음. 회수·만료·권한·이전 방식 요청 가능성을 함께 안내하고 "복구 불가"로 단정하지 않음. 원인별 코드 분리는 **[미정]** |
| `project_delivery_unknown`, `project_unavailable` | 네트워크·서버 | 재시도. 기록 보존 | 있음 |
| `project_removed`, `project_client_mismatch`, `invalid_project_credential`(훅) | 다른 Agent가 폴더를 다시 연결해 이 Agent 설정이 해제된 이전 profile을 가리킴 | `context-connect --agents <이 Agent>`. 폴더 연결이 유효하면 승인 없이 설정만 갱신. 기록 보존 | 있음. 재연결은 해제된 profile에 묶인 다른 Agent 설정도 새 연결로 옮긴다 |
| `project_local_check_failed`(훅) | 서버 요청 전 로컬 확인 실패. 서버 장애 아님 | 상태 확인 후 `context-connect` | 있음 |
| `agent_upgrade_required_use_context_connect` | 폐기 명령 | `context-connect`/`context-status` 사용 | 있음 (0336cf1) |
| `existing_mcp_binding_conflict` | 같은 이름의 남의 MCP 항목 | 해당 설정 확인 | 있음 |
| wrapper `setup_command_failed` | runtime 명령 실패 | 앞선 runtime 오류 확인 | 원인을 숨김 |
| wrapper `uv_required_install_from_astral_documentation`, `runtime_update_required_then_restart_host` | 실행 환경 | 문구대로 | 있음 |
| `feature_unavailable` | 운영자가 그 기능을 끈(off) 상태. MCP는 도구를 tools/list에서 빼고, 예전에 받아 둔 도구 호출은 `feature_unavailable: …` ToolError로, HTTP API는 404로 거절한다 | 웹에서 진행하거나 운영자가 켤 때까지 기다림. 자동 재시도 안 함 | 있음(995). 끈 상태는 일시 장애가 아니라서 503·`Retry-After`를 주지 않는다 |
| `service_feature_paused` | 운영 점검 등 일시 중단(회의 처리 등 `runtime.enforce`) | `Retry-After` 뒤 재시도 | 있음. 숨긴 기능 거절에는 쓰지 않는다(결정) |
| `tool_disabled` | 공개 Chat MCP(/mcp)에서 운영자가 도구 카탈로그로 그 도구 하나를 끔 | 웹에서 진행 | 있음 |

로그인·자격 증명 오류는 구형 `project-connect` 대신 `durumari-mcp context-connect --workspace <루트>`를 안내하고, `workspace_directory_required`는 존재하는 작업 폴더와 `context-connect --workspace`를 안내한다. Git 연결은 최상위 폴더를 사용한다. **[현행]**

## 11. 스킬·문서 정합

- durumari-connect·durumari-status 스킬과 플러그인 README는 Windows에서 `python` 또는 `py -3`(3.12+)를 쓰도록 안내한다. **[현행]**
- durumari-context 스킬은 도구 개수를 적지 않고 "모든 두루마리 Context MCP 도구"로 표현한다(runtime 동봉 사본과 동일). **[현행]**
- `docs/project-continuity.md`, `docs/project-onboarding.md`는 현행 요약으로 바꾸고 이전 내용은 Git 이력에 둔다. 직접 호스팅 안내는 개발자 연결을 `context-connect`로 안내하고 원격 MCP·OAuth를 폐기로 표시한다. runtime README의 도구 설명도 2.3절과 맞춘다. **[현행]**
- 업데이트 후 호스트를 재시작하지 않은 세션은 이전 스킬 문구를 계속 쓴다. 감지·안내 방법 **[미정]**.

## 12. 결정 (2026-10-14)

김준형이 판단을 위임해 에이전트가 정했다. **팀 확인 전**이며, 팀이 다르게 정하면 이 절을 개정한다.

| # | 결정 | 이유 | 적용 |
|---|---|---|---|
| 1 | `durumari.yaml`은 팀 공용 값(서버·프로젝트·source·저장소, 폴더 소스)만 두고, 사용자별 `profile`·`workspace_identity`는 git에서 제외되는 `.durumari/context-binding.json`으로 옮긴다. 제거할 때 공용 파일은 지우지 않는다. 기존 형식도 계속 읽는다 | 팀원마다 diff가 생기고, 한 사람이 제거하면 팀 공용 파일이 지워진다 | **적용.** 136 병합 뒤 적용했다. 구형 manifest(두 키를 함께 가진 것)도 읽고, 한 키만 있으면 거부한다. 복사본은 OS profile의 workspace_identity 검사로 막는다. **보완:** `source_id`는 서버가 승인마다 새로 만드는 연결별 값이라 팀원마다 다르다. 공용 파일의 `source_id`는 처음 쓴 연결 값으로 두고 이후 연결이 덮어쓰지 않으며, 연결 확인(`check_workspace`)과 제거 범위 확인은 서버·프로젝트·폴더 소스만 비교한다. 연결 자신의 `source_id`는 OS profile에 있고 서버가 따로 검증한다. 0.4.10 이하 runtime은 아직 공용 파일의 `source_id`를 비교하므로 팀원 모두 0.4.11 이상으로 업데이트해야 이 충돌이 사라진다 |
| 2 | 제거 시 비게 된 생성 파일은 삭제하고, 사용자 내용이 있는 파일·백업은 남긴다 | 연결 흔적을 남기지 않되 사용자 자료는 절대 지우지 않는다 | 적용. **보완:** 2026-10-21 Windows 회귀에서 `.gitignore` 블록·Codex hooks 기능·빈 폴더가 남아, 연결이 추가했다고 기록한 것만 되돌린다. 커밋된 `.gitignore` 블록은 팀 공용이라 남긴다 |
| 3 | 지침 파일은 선택한 Agent의 것만 만든다. AGENTS.md는 Codex·Cursor·OpenCode·Copilot 공용이다. 이미 관리 블록이 있는 파일은 계속 갱신한다 | `--agents claude`인데 GEMINI.md가 생기는 문제 | 적용 |
| 4 | `project-import`/`project-export`를 폐기한다. 이관은 사람이 고른 내용을 일반 Context 저장으로 올리는 durumari-migrate 스킬 경로만 지원한다 | 서버 경로가 이미 410이고, README가 일괄 업로드를 제공하지 않는다고 정한다 | 적용(실행부와 `context_tools` 제거) |
| 5 | `project-serve` 호환 연결 장치는 0.5.0에서 폐기한다 | 0.4.x 설정이 모두 `context-serve`로 바뀔 시간을 준다 | 문서만. 0.5.0 때 코드 제거 |
| 6 | `pending_writes`는 연결 전체 기준으로 바꾸고 `pending_writes_this_session`을 추가한다 | 새 프로세스의 status가 늘 0으로 보여 대기 기록이 없는 것처럼 보였다 | 적용 |
| 7 | 구버전 미전송 기록은 보존만 하고 이전 경로는 만들지 않는다 | 보낼 API가 없고 0.3.x는 미지원이다. 지우면 되돌릴 수 없다 | 문서만 |

그 밖의 결정: `쓰기불가` 상태를 9절에 넣었다. 검증 행렬 완료 기준(모든 전이 1회 이상, 설정 축은 짝지은 조합, 제외 사유 명시)은 별도 확인이 필요하다.

## 근거
- 소스: `context_cli.py`(connect·status·remove), `context_binding.py`(설정 파일), `context_removal.py`(제거), `project_cli.py`(훅), `project_gateway.py`(HTTP 오류 매핑), `context_outbox.py`(대기 건수), `cli.py`(폐기 명령·안내), `scripts/durumari_plugin.py`(wrapper), 서버의 폐기 API.
