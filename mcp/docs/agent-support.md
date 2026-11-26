# Agent 지원 수준과 버전 기준

이 문서는 소스에서 제공하는 연결 경로와 실제 검증한 범위를 구분합니다. 계정 경로 설명은 2026-10-19 소스 기준이며, 아래 2026-10-10 값과 호스트 실측 표는 당시 기록입니다. 이를 현재 공개 다운로드·운영 상태로 사용하지 않습니다. 새로 검증하면 날짜·버전·OS·근거와 함께 갱신합니다.

## 현재 소스의 연결 경로

계정 연결과 대화별 프로젝트 선택은 [계정·대화 사용 안내](account-conversations.md)를 따릅니다. 계정 연결은 폴더 연결과 별도로 설치되며 기존 `durumari.yaml`을 바꾸지 않습니다. 계정 연결도 공용 Skill 도구를 쓰며, 호출마다 대화의 `project_ref`를 전달합니다(0.4.22부터).

| 대상 | 소스에서 제공하는 경로 | 실제 사용 확인의 경계 |
| --- | --- | --- |
| Codex·Claude Code·Cursor | `account-setup --client`에 `codex`, `claude-code`, `cursor`를 지정해 브라우저 계정 연결 후 사용자 MCP `durumari_account` 등록. 독립 launcher가 시작 전 필수 버전 정책을 확인 | 설정 생성·단위 검사가 실제 호스트의 MCP 발견·신뢰·재시작·OAuth·영수증 검증을 대신하지 않음 |
| Chat의 원격 앱 | 호스트에 배포·등록된 원격 앱의 계정 연결. 대화 참조와 프로젝트 참조를 각 호출에 명시 | 로컬 `account-setup`·기기 자격 증명 복사가 아님. 앱 등록·사용자 연결·실제 도구 노출은 별도 확인 |
| Gemini CLI·OpenCode | 기존 폴더 고정 연결 어댑터와 공통 MCP runtime 유지 | `account-setup`의 자동 사용자 설정 등록 대상은 아님. 다섯 native 호스트의 새 계정 흐름이 모두 검증됐다고 표시하지 않음 |
| GitHub Copilot CLI·기타 MCP 호스트 | 기존 명시적 실험 어댑터 또는 수동 stdio 접합 | 호스트별 검증·설치·신뢰 절차가 필요 |

호스트가 설치·가져오기 UI를 제공하면 현재 제공되는 배포물을 그 화면에서 설치합니다. 이 소스에 포장 형식이 있다는 사실만으로 공개 marketplace 게시나 해당 버전의 설치 성공을 주장하지 않습니다. 계정 도구를 포함하는 배포와 사용자 승인이 준비돼야 새 흐름을 사용할 수 있습니다.

`account-connect`는 계정 연결만, `account-serve`는 하위 stdio 실행만 담당합니다. 자동 필수 업데이트는 `account-setup`이 등록한 launcher 경로의 계약입니다. 직접 실행한 `account-serve`나 기존 폴더 실행이 자동으로 같은 launcher를 거친다고 표시하지 않습니다.

## 버전의 의미

| 식별자 | 값의 출처 | 의미 |
| --- | --- | --- |
| 플러그인 버전 | `plugins/durumari-agent/plugin.json`, `.codex-plugin/plugin.json`, `.claude-plugin/plugin.json` | 설치용 catalog·ZIP의 버전 |
| runtime wheel 버전 | `plugins/durumari-mcp/pyproject.toml`, 설치 후 `durumari-mcp --version` | 공통 MCP·CLI runtime |
| MCP 서버 식별자 | MCP `initialize` 응답의 `serverInfo.version` | 실행 중인 runtime 버전과 같습니다(`version_info.runtime_version`) |
| 서버 릴리스 | `GET /api/v2/release`의 `release_id`, `context_api` | 배포된 서버. 플러그인과 따로 배포됩니다 |
| 공개 다운로드 | `GET /downloads/latest.json`의 `version`, `archive.sha256` | 사용자가 지금 내려받는 플러그인 |
| native 버전 정책 | `GET /api/v2/release`의 `native` | 최신·지원 최소·명시 필수 runtime 버전과 사람용 업데이트 안내 주소 |

- 플러그인, runtime wheel, MCP 서버 식별자는 같은 패키지에서 항상 같은 값이어야 합니다. `plugins/build_release.py`가 manifest와 wheel의 버전이 다르면 빌드를 중단합니다. `tests/test_version_info.py`는 MCP 식별자까지 같은지 확인합니다.
- 서버 릴리스와 Context API 버전은 플러그인 버전과 별개입니다. 호환성은 `context_api`로 판단합니다.
- 개발 소스(`dev`)의 버전은 공개 다운로드보다 앞설 수 있습니다. 안내에는 공개 다운로드 버전을 씁니다.
- 기존 폴더 경로의 `durumari-mcp context-status`와 MCP `durumari_project_status`는 연결 상태, 미전송 수, `runtime_version`, 연결된 `server`, 공개 `server_release`(`release_id`, `context_api`)를 보여 줍니다. 서버 릴리스를 조회하지 못하면 `unavailable`로 표시하고 연결 상태에는 영향을 주지 않습니다. 계정 상태는 계정 연결 메타데이터·runtime·server를, 선택한 `project_ref`의 상태는 해당 프로젝트와 미전송 수를 추가로 보여 줍니다. 토큰과 비밀값은 출력하지 않습니다.

## 과거 공개·운영 확인 기록 (2026-10-10)

| 대상 | 값 |
| --- | --- |
| 공개 다운로드 | 0.4.1, sha256 `f06eb37df2cde61002a6085f482132bb47cdb3399b7f6f9001eeeba3fb847d8c` |
| 운영 서버 | `cc-40615038bf33-229494`, source tree `6bb901f5432db23488a600f841dd086c4c40df6b`, `context_api` 2 |
| 당시 개발 소스 `dev` | 0.4.4 (2d648e0) |
| 당시 다음 배포(묶음) | 0.4.5, 2단계 배포의 1단계([배포물 호환성](release-compatibility.md)) |
| 공개 0.4.1의 MCP 식별자 | `0.4.0`으로 고정되어 있습니다. 이 변경 이후 릴리스부터 패키지 버전과 같아집니다 |
| `/api/v2/agent/config` `supported_agents` | `codex`, `claude`, `cursor`, `gemini`, `opencode`(연결 설정을 만들 수 있는 대상 목록이며, 검증 완료를 뜻하지 않습니다). 실험적 `copilot`은 서버 목록에 없고 runtime에서 명시적으로 선택할 때만 설정합니다 |

## 지원 수준 구분

- **설정 제공**: `context-connect`가 해당 Agent의 프로젝트 MCP 설정을 만듭니다.
- **배포**: 공개 다운로드에 그 설정이나 설치 경로가 들어 있습니다.
- **실측**: 실제 Agent와 계정으로 확인한 범위를 날짜, Agent 버전, runtime 버전, OS와 함께 적습니다.
- **검증 완료**: 승인, 검색·읽기, 저장 영수증, 재조회, 재시작, 회수 후 거부를 같은 배포 버전에서 모두 확인한 상태입니다. 이 상태가 아니면 "지원 완료"나 "검증됨"으로 표시하지 않습니다.

아래는 기존 폴더 연결에 대한 과거 실측이며 새 계정 연결·자원 browser의 검증 결과가 아닙니다.

| Agent | 설정 제공 | 배포(0.4.5 → 0.4.6) | 실측 | 수준 |
| --- | --- | --- | --- | --- |
| Codex | 예(`.codex/config.toml`, Hook) | 예, 전용 plugin manifest | 2026-10-09 0.4.1에서 서버 조회 성공(보고). 저장 영수증·회수는 이 버전에서 기록 없음 | 부분 실측 |
| Claude Code | 예(`.mcp.json`, Hook) | 예, 전용 plugin manifest | 2026-10-10 macOS 27.2, Claude Code 2.1.261, runtime 0.4.1, 서버 `cc-40615038bf33-229494`에서 `durumari_context_search`와 `durumari_context_read` 성공. 저장·회수는 확인하지 않음 | 부분 실측(읽기) |
| Cursor | 예(`.cursor/mcp.json`) | 0.4.6부터 전용 plugin manifest(`.cursor-plugin`)와 `durumari-mcp update --client cursor` 로컬 설치. 0.4.4 이하 updater는 이 배포물을 받지 않고 기존 설치를 유지 | 없음 | 미검증 |
| Gemini CLI | 예(`.gemini/settings.json`, `GEMINI.md`) | 0.4.6부터 Extension 포장(`gemini-extension.json`)과 `durumari-mcp update --client gemini`. 0.4.4 이하 updater는 이 배포물을 받지 않고 기존 설치를 유지 | 없음 | 미검증 |
| OpenCode | 예(`opencode.json(c)` v1·v2 형식 판별, `.opencode/skills/durumari-context`, [opencode.md](opencode.md)) | 0.4.5 runtime에 포함(새 catalog 파일 없음). 공개 0.4.1은 v1 형식만 생성 | 없음. 자동 검사만 통과 | 미검증 |
| GitHub Copilot CLI | 예, 명시 선택만(`--agents copilot`, 공유 `.mcp.json`의 `"type": "stdio"`, [copilot.md](copilot.md)) | 0.4.5 runtime에 포함. 기존 `plugin.json`·`.claude-plugin/marketplace.json`을 재사용해 새 catalog 파일 없음 | 없음. 자동 검사만 통과 | 실험적·미검증 |
| 기타 MCP Agent | `context-serve --workspace <루트>` stdio | 공통 runtime | 없음 | 일반 MCP 호환(미검증) |

2026-10-01 기록의 Codex CLI 0.144.1과 Claude Code 2.1.229 결과는 이전 세대 adapter의 결과입니다. 현재 0.4.x Cloud Context 연결의 검증으로 쓰지 않습니다.

## 검증과 버전 적용

계정 흐름은 호스트별로 설치·신뢰, 사용자 승인과 재개, 도구 발견, 두 대화의 독립 선택, 원래 참조의 불명 쓰기 회수, 재시작과 권한 회수 후 거부를 확인합니다. 자원 browser는 화면의 실제 실행과 서버 검증 영수증을 함께 확인합니다. 로컬 자동 검사, 실제 호스트·OAuth, 공개 배포·Live는 서로 다른 증거로 남깁니다.

명시적 필수 버전이 있으면 launcher가 검증된 배포물을 별도 runtime에 준비하고 시작하며 서버도 native 요청을 `426 client_update_required`로 막을 수 있습니다. 정책이 불명확하거나 업데이트할 수 없으면 복구 안내로 멈춥니다. 완전한 기존 v2 릴리스 응답에 `native`만 없는 경우는 `legacy_advisory` 경고로 구분합니다. 필수 버전 적용은 권한 추가 승인이 아니며 READ·WRITE·SECURE를 자동 확대하지 않습니다.

## 기존 후속 기록

- 당시 웹 다운로드·가이드·Agent 설정 화면과 native 화면이 다섯 Agent를 같은 수준으로 나열하는 문제를 기록했습니다. 현재 화면과 후속 변경 여부는 별도 소스·배포 확인이 필요합니다.
- `/api/v2/agent/config`에 Agent별 지원 수준을 넣는 항목도 당시 별도 결정 대상으로 남겼습니다.
