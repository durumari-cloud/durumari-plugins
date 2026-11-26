# 계정 연결과 대화별 프로젝트

사용자는 “이 채팅은 새 프로젝트로 시작해 줘”, “다른 구름으로 바꿔 줘”, 또는
두루마리 프로젝트 링크를 말하면 된다. Agent가 접근 가능한 목록이나 선택 화면을
열고, 선택 결과를 이 대화의 작업 위치로 유지한다. 구름·프로젝트 생성과 보안 작업은
사람이 화면에서 직접 실행한다.

계정 연결은 사용자와 제공사 권한을 확인하고, 대화별 참조는 이번 작업의 프로젝트를
지정한다. 프로젝트 폴더 안의 채팅과 폴더 없는 채팅이 같은 계약을 쓴다. 새 계정
연결은 기존 `durumari.yaml`, 폴더 연결, 다른 대화의 선택을 덮어쓰지 않는다.
이 문서는 현재 소스의 계약이며, 공개 배포나 실제 호스트 검증의 완료 기록은 아니다.
검증 범위는 [Agent 지원 수준](agent-support.md)에서 구분한다.

## 설치와 계정 연결

호스트가 제공하는 플러그인 설치·가져오기 화면에서 배포된 두루마리를 설치한 뒤
“두루마리 연결해 줘”라고 말한다. 호스트의 신뢰·실행 확인과 브라우저의 계정 승인은
사람이 직접 한다. 공개 marketplace에 등록돼 있다고 가정하지 말고, 현재 제공되는
배포 경로를 확인한다. Chat에서는 호스트의 원격 앱 연결 화면을 사용하며, 로컬
설정 파일이나 다른 기기의 자격 증명을 복사하지 않는다.

Codex·Claude Code·Cursor의 계정용 간편 설정은 다음 명령으로도 실행할 수 있다.
Python 3.12+와 `uv`, 이 명령을 포함하는 runtime이 필요하다.

```sh
durumari-mcp account-setup --client codex --server https://app.durumari.cloud
```

설치한 플러그인의 wrapper를 사용하는 경우:

```sh
python3 <plugin>/scripts/durumari_plugin.py account-setup --client codex
```

Claude Code는 `--client claude-code`, Cursor는 `--client cursor`를 쓴다. Windows에서는
Python 3.12+인 `python` 또는 `py -3`를 사용한다. runtime이 없거나 계정 명령을 모르면
현재 배포 경로로 플러그인·runtime을 설치 또는 업데이트한 뒤 재시작한다.

`account-setup`은 계정 연결을 회수한 뒤 사용자 설정에 별도 `durumari_account` MCP를
등록한다. 기존 `durumari_project`와 다른 MCP 항목은 보존한다.

| 호스트 | 계정 MCP를 등록하는 사용자 설정 |
| --- | --- |
| Codex | `~/.codex/config.toml` |
| Claude Code | `~/.claude.json` |
| Cursor | `~/.cursor/mcp.json` |

같은 이름의 다른 설정을 발견하면 덮어쓰지 않고 `account_mcp_configuration_conflict`로
멈춘다. 충돌을 확인한 뒤 같은 명령으로 재개한다. `state=configured`는 설정 완료이며,
실제 MCP 발견·호스트 재시작·연결 조회는 별도로 확인해야 한다.

브라우저를 자동으로 열 수 없으면 같은 명령에 `--no-browser`를 붙인다. 반환된
승인 주소를 사람이 열고 승인한 뒤 **같은 client·server·profile로 같은 명령**을
다시 실행한다. `pending`은 승인 완료가 아니다. 기본 profile은 client와 server마다
구분되며, 명시하려면 `--profile my-codex`처럼 지정한다. 토큰과 진행 중인 기기 증명은
OS 자격 증명 저장소에만 보관하며 모델 입력·명령 결과·폴더 YAML에 출력하지 않는다.

수동 호스트 접합이 필요한 환경의 하위 명령은 다음과 같다.

```sh
durumari-mcp account-connect --client codex --server https://app.durumari.cloud --profile my-codex --no-browser
durumari-mcp account-serve --client codex --profile my-codex
```

`account-connect`는 계정 연결만 하며 호스트 설정을 등록하지 않는다. `account-serve`는
stdio 서버를 직접 실행하는 하위 명령이다. 자동 필수 업데이트를 적용하는 호스트 실행
항목은 `account-setup`이 등록한 독립 launcher를 사용한다.

제공사 묶음 승인이 이미 있고 추가 권한이 없으면 서버가 이를 확인해 동의 반복을
줄일 수 있다. OpenAI(ChatGPT·Codex), Anthropic(Claude·Claude Code) 등의 승인과
각 앱·기기의 자격 증명은 구분한다. 이전 프로젝트 고정 자격 증명을 계정 전체 승인으로
자동 승격하지 않는다. 새 권한은 별도 사람 승인이 필요하며 현재 멤버십·구름 정책·승인
범위는 매 호출 다시 확인한다.

## 대화에서 고르기·생성·전환

1. 새 대화는 `durumari_conversation_start`의 `request_key`를 생략해 독립 키와
   `conversation_ref`를 받는다. 명시 키는 같은 시작 요청의 응답이 불명확할 때만
   재사용한다. 다른 대화의 키나 참조를 가져오지 않는다.
2. “접근 가능한 프로젝트 보여 줘”에는 `durumari_project_catalog`를 사용한다.
   화면이 편하면 `durumari_project_browser`의 `browser_url`을 열어 구름·프로젝트를
   선택한다. 새 구름·프로젝트는 화면에서 사람의 현재 권한으로 직접 생성한다.
3. 프로젝트 ID나 정식 링크가 있으면 `durumari_project_select`에 둘 중 하나와
   현재 `expected_revision`, 새 작업의 `request_key`를 전달한다. 링크도 실제 소속과
   현재 접근 권한을 확인한 뒤에만 선택된다.
4. 브라우저에서는 `task_ref`를 보관하고 `durumari_interaction_status`로 선택 결과를
   회수한다. 한 번의 대기는 최대 15초다. `pending`, 취소, 만료, 거부를 완료로 말하지 않는다.
5. 선택 결과의 불변 `project_ref`를 Context·비밀값 메타데이터·자원 화면 호출마다
   전달한다. 전환하거나 `durumari_project_disconnect`로 해제하면 이 대화의 revision만
   바뀐다. 프로젝트 데이터·폴더 설정·다른 대화의 선택은 삭제하지 않는다.

지원 링크는 해당 배포의 HTTPS `/clouds/{cloud}/projects/{project}` 및 그 아래
`contexts/{id}?revision=N`, `skills/{id}`이다. 프로젝트 선택 후 자원 ID와 버전·revision을
해당 읽기 도구나 화면에 전달한다. 다른 origin·자격 증명·인코딩 우회·임의 쿼리는 거부한다.

서버 참조는 호스트의 실제 채팅 ID도 접근 권한도 아니다. 호스트가 신뢰할 수 있는
채팅 식별자를 제공하지 않는 환경에서는 호출자가 각 대화의 참조를 정확히 유지해야 한다.
이 프로토콜이 실제 ChatGPT·Codex 채팅 경계를 독립적으로 인증한다고 주장하지 않는다.
계정 도구가 없으면 현재 연결 경로와 업데이트 필요 여부를 안내한다. 과거 채팅이나
메모리로 최신 프로젝트 내용을 재구성하거나 다른 프로젝트로 조용히 대체하지 않는다.

## Context와 사람의 자원 화면

Context의 검색·읽기·저장·수정·이력·삭제·복원은 노출된 공통 MCP 도구로 수행한다.
현재 권한과 revision을 확인한다.

공용 Skill은 폴더 연결과 같은 Skill 도구(목록·읽기·메타데이터·버전·발행·Git 가져오기·
수정·버전 선택·삭제·비밀값 참조 연결)를 쓰고, 호출마다 이 대화의 `project_ref`를 전달한다.
권한은 매 호출 현재 멤버십·구름 정책·연결 권한(READ·WRITE·SECURE)으로 다시 확인한다.
숨긴 Skill과 없는 Skill은 같은 404이며 권한 밖 Skill 이름은 목록·영수증에 나오지 않는다.
쓰기는 `request_key`가 필요하고, 응답이 유실되면 `durumari_skill_operation`에 원래
`project_ref`와 `request_key`로 영수증을 회수한다. 공유 권한·필수 지정은 바꾸지 않고
재인증하는 웹 화면 링크만 돌려준다. 비밀값 참조 연결·조회는 SECURE의 `secrets:list`가
필요하며 값은 다루지 않는다. 계정 Chat은 서버에서 Skill을 읽으며 로컬 설치를 하지 않는다.

| 사용자 작업 | 여는 도구 | 결과 회수 |
| --- | --- | --- |
| Context 목록·검색·한 문서 전문 | `durumari_context_browser` | `durumari_resource_status` |
| 선택한 자료를 현재 프로젝트로 가져오기 | `durumari_context_import` | `durumari_resource_status` |
| 파일 목록·업로드·교체·제거 | `durumari_files_browser` (`browse` 또는 `manage`) | `durumari_resource_status` |
| 검토 요청 조회·응답·처리 | `durumari_reviews_browser` (`browse` 또는 `manage`) | `durumari_resource_status` |
| 비밀값 관리 | `durumari_secret_browser` | 안전한 참조·상태만 `durumari_resource_status` |

각 화면은 원래 `project_ref`와 `request_key`로 열고 반환된 `browser_url`을 사용한다.
화면이 호스트 안에서 열릴지는 호스트에 달려 있으며, 일반 브라우저로도 이어진다.
사람이 로그인해 실행한 실제 작업을 서버가 확인한 뒤 ID·개수·revision·버전·hash 등
허용된 결과만 회수한다. 화면을 열었거나 버튼을 눌렀다는 사실만으로 완료 처리하지 않는다.
파일 바이트·비밀값·비밀번호는 이 결과에 포함하지 않는다.

가져오기는 사람이 접근 가능한 텍스트·파일을 제공하고 미리보기에서 선택한 자료만
대상으로 한다. 접근할 수 없는 호스트의 과거 대화 전체를 자동으로 가져오지 않는다.
원본은 유지하고 중복은 기본적으로 건너뛴다. 검토 요청의 사람 승인과 파일 변경 권한은
화면에서도 유지된다.

## 불명확한 결과와 대상 전환

Context 쓰기는 `project_ref`와 원 요청 키에 묶인다. 같은 프로젝트의 서로 다른 대화가
같은 키를 사용해도 영수증이 섞이지 않는다. native의 불확실한 Context 쓰기는
연결·참조별 암호화 outbox에 남긴다.

- 응답이 유실되면 `durumari_context_operation`에 **원래 `project_ref`와
  `request_key`**를 전달한다.
- 프로젝트를 바꾼 뒤에도 원래 영수증을 회수한다. 이전 참조로 새 읽기·쓰기는 거부된다.
  불명확한 요청을 새 프로젝트나 새 키로 옮기지 않는다.
- 화면 작업은 원래 `task_ref`로 상태를 확인한다. `durumari_resource_status`와
  `durumari_interaction_status`의 대기는 한 번에 최대 15초이며 무기한 기다리지 않는다.
- `prepared`, `pending`, 전송 불명 상태는 성공이 아니다. 서버 영수증을 확인한 뒤
  완료를 보고한다. Context 저장은 `stored`를 확인한다.
- 토큰 갱신 결과가 불명확하면 이미 소비됐을 수 있는 refresh token을 자동 재전송하지
  않는다. 같은 profile의 계정 연결 명령으로 복구한다.

계정 Context 쓰기는 `/api/v2/agent/context/mutations`, 영수증은
`/api/v2/agent/projects/operation`을 사용한다. 조회는 기존 Context REST와
`X-Durumari-Project-Ref`를 쓴다. 계정 Skill은 `/api/v2/agent/account/skills`의
`read`·`mutations`·`operations`·`installations`에 `project_ref`를 담아 보내며, 쓰기
영수증은 연결·참조·요청 키별로 서버에 남고 native는 별도 암호화 outbox에 둔다.
계정 자격 증명으로 이전 프로젝트 범위 쓰기·영수증 경로를 사용하면 거부하며, 기존
프로젝트 고정 자격 증명의 동작은 유지한다.

## 비밀값·로컬 설치의 경계

계정 도구는 허용된 비밀값 메타데이터와 작업 상태만 제공한다. 실제 값의 등록·보기·
교체·삭제·공유는 보안 화면의 사람 로그인·금고 권한·재인증을 따른다. 비밀번호나 실제
값을 도구 인자·채팅·Context·Skill 본문으로 보내지 않는다.

계정 setup은 기기·금고 승인이 아니다. 기기가 필요한 호출은
`account_device_approval_required`로 안내하며, 해당 프로젝트에 별도로 승인된 기존
기기 경로를 사용한다. 다른 프로젝트의 기기 자격 증명으로 조용히 전환하지 않는다.

native의 Skill 설치·필수 Skill 동기화는 현재 폴더에 바인딩이 있으면 선택한
server·project·사용자와 그 바인딩·manifest가 일치할 때만 폴더의 `.durumari/skills`를
쓰고, 다르면 오류를 안내하며 YAML을 덮어쓰지 않는다. 바인딩이 없으면 사용자 데이터
폴더의 `account-skills/<server>/<project_id>/.durumari/skills`에 설치하고 설치 기록은
계정 연결로 남긴다. 계정 경로는 기기 등록을 하지 않는다. Chat은
서버에서 Skill을 읽으며 로컬 파일을 설치하지 않는다.

## 필수 버전과 복구

`account-setup`이 등록한 launcher는 MCP 시작 전에 공개 릴리스의 `native` 정책을
확인한다. 명시적 `required_native_version`보다 낮을 때만 검증된 배포물을 격리된
runtime에 설치하고 시작한다. 실행 중인 다른 runtime을 덮어쓰거나 호스트를 종료하지
않는다. 필수 버전은 READ·WRITE·SECURE의 권한 추가 승인과 별개이며 권한을 늘리지 않는다.

정책 조회 불가·잘못된 정책·업데이트 실패·다른 updater의 잠금은 복구 안내와 함께
시작을 중단한다. 검증된 기존 `context_api=2` 응답에 완전한 기존 `client` 정보가 있고
`native` 필드가 전혀 없는 경우에만 `legacy_advisory`로 진행하며
`native_required_policy_unsupported`를 알린다. 필수 정책을 지원한다고 표시하지 않는다.
`/downloads`는 사람이 여는 안내 주소이며 runtime ZIP 주소가 아니다.

서버도 native 작업의 버전 헤더를 검사해 필요하면 `426 client_update_required`로 막는다.
원래 쓰기·참조·키를 보존하고 업데이트·재시작 후 결과를 회수한다. 연결·승인·토큰 갱신·
연결 상태 같은 복구 경로는 열어 둔다. 일반 권장 업데이트와 native 플러그인 업데이트는
기존 업데이트 안내(durumari-update 스킬)를 따른다.
