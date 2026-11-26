# GitHub Copilot CLI 연결 (실험적, 미검증)

Copilot CLI 어댑터는 기존 공통 runtime, 5개 스킬, 8개 MCP 도구, 브라우저 승인과 OS 자격 증명을 그대로 씁니다. 인증이나 Context 저장소를 새로 만들지 않습니다. 자동 검사만 통과했고, 실제 Copilot CLI와 계정으로는 확인하지 않았습니다.

## 설치·업데이트·제거

배포 ZIP을 풀면 나오는 `durumari-catalog`에서 다음 명령을 실행합니다.

```sh
copilot plugin marketplace add .
copilot plugin install durumari@durumari
```

- `plugins/durumari-agent/plugin.json`은 Agent Plugins 1.0 manifest(`$schema` `https://agent-plugins.org/schemas/1.0.0/plugin.schema.json`)입니다. Copilot CLI는 이 파일을 plugin 루트에서 읽습니다. 스킬은 `skills/<이름>/SKILL.md`에서 찾습니다. catalog의 `.claude-plugin/marketplace.json`도 Copilot이 확인하는 marketplace 위치입니다. 그래서 Copilot 전용 manifest는 따로 추가하지 않았습니다.
- 업데이트는 `copilot plugin update durumari`, plugin 제거는 `copilot plugin uninstall durumari`로 합니다. 프로젝트 연결 해제는 plugin 제거와 별개입니다. 연결을 해제하려면 저장소 루트에서 `durumari_plugin.py remove --workspace <루트>` 또는 `durumari-mcp context-remove`를 실행합니다.
- plugin에는 MCP 서버를 넣지 않았습니다. 승인된 저장소에서만 서버가 시작되도록 연결 시 프로젝트 설정을 만듭니다.

## 프로젝트 연결

durumari-connect 스킬이나 `durumari-mcp context-connect --agents copilot`은 저장소 루트의 `.mcp.json`에 `durumari_project`를 추가합니다. Copilot CLI는 현재 폴더부터 저장소 루트까지 이 파일을 읽습니다.

- Copilot CLI와 Claude Code는 같은 `.mcp.json`을 사용합니다. `copilot`이 포함되면 두 호스트가 모두 받아들이는 `"type": "stdio"`를 넣습니다. 둘을 함께 연결하면 하나의 항목에 `--client mcp`를 사용합니다. `--client` 값은 로컬 검사용 표시일 뿐이고, 서버 권한은 항상 승인된 `mcp` 연결 하나입니다.
- `copilot`은 실험 단계라 기본 `--agents` 목록에 넣지 않았습니다. 선택하지 않으면 기존 Claude Code 항목은 바뀌지 않습니다.
- Copilot CLI는 `AGENTS.md`를 지침으로 읽습니다. 공통 관리 블록이 Context 사용 규칙을 제공합니다.
- 다른 서버, 사용자 파일, 자격 증명, 미전송 요청은 설치와 제거 과정에서 유지됩니다. 같은 이름의 타 소유 항목이 있으면 `existing_mcp_binding_conflict`로 중단하고 아무 파일도 바꾸지 않습니다. 중첩 저장소와 복사된 연결 파일로는 접근할 수 없습니다.
- Copilot CLI는 폴더 신뢰를 확인한 뒤에야 프로젝트 MCP 설정을 불러옵니다. 신뢰를 대신 승인하지 않습니다.

## 미검증 항목

- 실제 Copilot CLI 버전에서의 plugin 설치·업데이트·제거와 스킬 발견
- `.mcp.json`의 `"type": "stdio"` 수용 여부. 공식 예시는 `"local"`이고, `stdio` 별칭은 0.0.370 릴리스 노트를 옮긴 제3자 미러에서만 확인했습니다.
- 실제 계정에서의 검색·읽기·저장 영수증과 재조회, 회수 후 거부

공식 근거(2026-10-10 확인):

- <https://docs.github.com/en/copilot/concepts/agents/about-plugins>
- <https://docs.github.com/en/copilot/reference/copilot-cli-reference/cli-plugin-reference>
- <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers>
- <https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-custom-instructions>
