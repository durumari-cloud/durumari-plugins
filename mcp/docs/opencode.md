# OpenCode v1·v2 연결

`durumari-mcp context-connect --agents opencode`는 저장소 루트의 `opencode.json` 또는 `opencode.jsonc`에 `durumari_project` MCP 서버 하나와 `.opencode/skills/durumari-context/SKILL.md`를 만듭니다. 두 파일 모두 `.gitignore`에 추가되며, 다른 서버·주석·사용자 설정은 그대로 둡니다. 두 설정 파일이 함께 있으면 어느 쪽인지 모호하므로 아무 파일도 바꾸지 않습니다.

## 버전 판별과 설정 형식

| 설치된 `opencode --version` | 형식 | 결과 `opencode.basis` |
| --- | --- | --- |
| `1.x`(이전 `0.x` 정식 릴리스 포함) | v1: `mcp.durumari_project`, `enabled` | `installed:<버전>` |
| `2.x` | v2: `mcp.servers.durumari_project`, 끌 때만 `disabled: true` | `installed:<버전>` |
| 미설치 | v1 형식 | `not_installed_compatible_v1` |
| `0.0.0-beta-*`, `0.0.0-dev-*` 같은 채널 빌드 | v1 형식 | `channel_build:<버전>_compatible_v1` |
| `3.x` 이상 등 알 수 없는 주 버전 | 거부, 파일 변경 없음 | `unsupported_opencode_version_choose_opencode_config` |

- v1과 v2의 채널 빌드는 둘 다 `0.0.0-*` 형식이어서 버전만으로 구분할 수 없습니다. v2는 v1 형식을 읽어 메모리에서 변환하지만, v1은 `mcp.servers`를 읽지 못합니다. 그래서 판별이 불확실할 때는 두 버전이 모두 읽는 v1 형식을 씁니다.
- 파일에 이미 있는 형식이 우선합니다. 다른 서버가 v2 형식(`mcp.servers`)으로 있는데 v1이 설치되어 있으면 `opencode_v1_cannot_read_v2_configuration`으로 중단합니다. 다른 서버가 v1 형식이면 v2가 설치되어 있어도 v1 형식을 유지합니다. 한 파일에 두 형식이 섞여 있으면 `opencode_configuration_mixed_formats`로 중단합니다. 사용자의 다른 서버를 대신 변환하지 않습니다.
- `--opencode-config v1|v2`로 형식을 직접 고를 수 있습니다. 이 경우에도 위의 기존 파일 보호 규칙은 그대로 적용됩니다.
- 이미 연결된 v1 항목은 v2 판별 후 다시 연결하면 `mcp.servers`로 옮깁니다. 프로필 이름, OS 자격 증명, 미전송 요청은 바꾸지 않습니다. 사용자가 끈 서버(`enabled: false` 또는 `disabled: true`)는 옮긴 뒤에도 꺼진 상태를 유지합니다.
- 같은 이름의 서버가 Durumari 소유가 아니면 `existing_mcp_binding_conflict`로 중단합니다. `context-remove`는 이 프로필이 소유한 항목만 두 위치 중 어디서든 제거합니다.

## 스킬

OpenCode v1과 v2는 모두 프로젝트의 `.opencode/skills/<id>/SKILL.md`를 찾습니다. 공통 `context` 스킬을 v1 이름 규칙(`^[a-z0-9]+(-[a-z0-9]+)*$`, 폴더 이름과 동일)에 맞춘 `durumari-context`로 설치합니다. 관리 표시가 없는 같은 경로의 스킬은 사용자 파일로 보고 덮어쓰거나 지우지 않습니다. runtime을 업데이트한 뒤 `context-connect`를 다시 실행하면 스킬도 갱신됩니다. 연결·상태·제거는 `durumari-mcp context-connect`, `context-status`, `context-remove`를 직접 사용합니다. 별도의 JS plugin은 필요하지 않아 추가하지 않았습니다.

## 검증 수준

자동 검사(`tests/test_opencode_config.py`)는 판별 규칙과 각 형식의 필드 집합, jsonc 주석, 다른 서버, 충돌, 이관, 제거를 확인합니다. 이 저장소의 개발 환경에서는 실제 OpenCode v1·v2 실행, MCP 검색·읽기·저장 영수증, 스킬 발견을 확인하지 않았습니다. 이 항목들은 미검증입니다.

공식 근거(2026-10-10 확인):

- v1: <https://opencode.ai/docs/mcp-servers/>, 스키마 <https://opencode.ai/config.json>
- v2: <https://opencode.ai/v2/docs/mcp-servers>, <https://opencode.ai/v2/docs/migrate-v1/>
- 스킬: <https://opencode.ai/docs/skills/>, <https://opencode.ai/v2/docs/skills>
- 버전: npm `opencode-ai` latest `1.18.35`, `https://opencode.ai/update/api/latest/cli/npm` `@opencode/cli` `2.0.26`
