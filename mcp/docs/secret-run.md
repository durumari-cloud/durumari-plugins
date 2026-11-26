# 비밀값 로컬 사용 계약 (`secret run` · `secret env` · 캐시)

구현 순서 2단계. 1단계는 `secret put`과 값 없는 MCP 목록·상태다.

## 1. 명령

| 명령 | 동작 | 출력 |
| --- | --- | --- |
| `secret run [--env VAR[=NAME]]… [--template P] [--offline] [--verbose] -- <cmd>` | 비밀값을 자식 환경 변수로만 주입, 셸 없음, stdin은 자식에 연결하고 stdout·stderr는 주입된 비밀값(원문·base64·URL·JSON 인코딩 변형)을 실시간 마스킹(`[REDACTED]`) | 없음(`--verbose`는 이름만 stderr), 종료 코드 = 자식 코드(신호 종료는 128+신호) |
| `secret env [--template P] [--output P] [--force] [--offline] [--json]` | `.env.durumari` → `.env` (`recovery`·`runtime` 등급 또는 정책상 금지된 비밀값은 차단) | 이름·개수·gitignore 결과 |
| `secret cache status\|clear [--json]` | 이 연결의 캐시 | 이름·버전·대상 수·남은 시간 |

- 템플릿 참조: `[export ]KEY=durumari://NAME`, 값 전체가 참조(따옴표 허용, 뒤 `# 주석` 허용). 일부에 섞이면 `invalid_template_line`(줄 번호). 다른 줄은 그대로 복사, `run`은 일반 `KEY=value`도 주입(확장 없음).
- 이름 선택: 내 소유 같은 이름 우선, 아니면 공유받은 것이 정확히 하나여야 함(`secret_name_ambiguous`).
- 막는 변수 이름: `PATH`, `PATHEXT`, `COMSPEC`, `SYSTEMROOT`, `LD_*`, `DYLD_*`, 플러그인 전용 `DURUMARI_MCP_*`와 그 이전 이름(`DURUMARI_UV_EXECUTABLE`, `DURUMARI_EXECUTION_POLICY`, `DURUMARI_SESSION_ID`, `DURUMARI_CONTEXT_DEPTH`, `DURUMARI_CLIENT`, `DURUMARI_INSTALLER`, `DURUMARI_PLUGIN_INSTALLER_DELEGATED`, `DURUMARI_SERVER`)(`invalid_env_name`). 백엔드 설정(`DURUMARI_PADDLE_PRICES` 등 그 밖의 `DURUMARI_*`)은 주입할 수 있다. 이전 이름은 호환 기간 동안만 막고 읽는다.
- `.env` 쓰기: git 저장소 안이면 `git check-ignore`로 무시되는 파일만, 추적 중이면 `env_tracked_by_git`, 저장소인데 git이 없으면 `gitignore_check_unavailable`, 저장소 밖이면 경고 후 허용. 심볼릭 링크·비정규 파일 거부. 같은 폴더 임시 파일(O_EXCL, 0600, Windows 소유자 ACL) → fsync → 원자적 교체. 기존 파일은 터미널 확인 또는 `--force`, 아니면 `env_exists_confirm_required`.
- 값 인용: 안전 문자만이면 그대로, 작은따옴표 없고 한 줄이면 `'…'`(확장 없음), 아니면 `"…"`에 `\\ \" \n \r` 이스케이프.

## 2. 서버 전달 (기기 서명·봉인 재사용)

승인된 기기 = 브라우저 승인한 저장소 연결(1단계와 같음). 연결마다 기기 키 한 쌍(Ed25519 서명, X25519 봉인)을 OS 저장소 `DURUMARI.AgentSecretDevice.v1`에 둔다.

| 경로 | 내용 |
| --- | --- |
| `POST /api/v2/agent/secret-devices` | `{platform, signing_key, encryption_key}` 등록. 같은 키는 멱등, 다른 키는 `device_key_changed`(연결 재승인 필요) |
| `POST /api/v2/agent/secret-devices/{id}/challenges` | 60초 1회 challenge(10분 30개 제한) |
| `POST /api/v2/agent/secret-devices/{id}/deliveries` | `{challenge_id, signature, request:{secrets:[{secret_id,revision,policy_revision}] ≤50, target:{kind,fingerprint,label}}}` |

- 서명 대상: `challenge.message + "\n" + canonical(request)`(키 정렬·공백 없는 JSON). 다른 요청에 대한 서명, 재사용, 다른 연결의 기기는 `device_access_denied`. 서명이 검증된 challenge는 이후 거부돼도 소비된다.
- 검사(프로젝트 잠금 한 트랜잭션): 연결 종류 = Agent 기기(`agent-context` 소스, Chat 연결 제외), 연결에 `secrets:*` 범위가 있으면 `secrets:use` 필요, 비밀값 종류 text·token만(계정·파일·2단계 인증 제외), 현재 revision·policy_revision, `use_tool`("Agent 사용") 권한. 실패는 `secret_use_denied`.
- 봉인: 도메인 `durumari-agent-secret-v1`, X25519-HKDF-SHA256-AES256GCM, binding `{challenge_id, connection_id, device_id, project_id, request_sha256, revision}`. 평문 `{secrets:[{secret_id,label,revision,policy_revision,value,receipt_id}], cache_seconds}`. 응답 `Cache-Control: no-store`, 512,000바이트 제한.
- 감사: `secret.agent_device.enrolled`, `skill.secret.used`(비밀값별 receipt), `secret.agent.delivered`(대상 종류·fingerprint·비밀값 ID·revision), `secret.agent.denied`(코드). 값 없음.

## 3. 대상별 첫 사용 승인 기억 (계약만, 웹 화면 후속)

- 대상 = `run`(실행 파일 이름, 인자 제외) 또는 `env`(작업 폴더 기준 출력 경로). 기기는 `sha256({kind,name,workspace})`와 짧은 라벨(`run: npm`, `env: .env`)만 보낸다.
- 서버 `agent_secret_targets`(연결·비밀값·종류·fingerprint 고유): `pending` → `approved`/`denied`(결정자·시각).
- 현재 `TARGET_APPROVAL_ENFORCED = False`: 첫 사용은 `pending`으로 기록하고 전달, 응답 `targets[].state = "recorded"`.
- 강제 후: `approved`만 전달. 그 외 403 `target_approval_required`, `details = {target_id, state, secure_url}`(`/projects/{project_id}/secrets/targets/{target_id}`). 거부돼도 `pending` 행은 남아 브라우저가 승인할 수 있다.
- 캐시는 값을 받은 대상 fingerprint를 기억하고, 새 대상은 캐시가 있어도 서버를 거친다(오프라인이면 `secret_offline_unavailable`).

## 4. 로컬 캐시

- OS 저장소만(`DURUMARI.AgentSecretCache.v1`): macOS Keychain, Windows 자격 증명 관리자, Linux Secret Service. 평문 파일·대체 저장소 없음. 연결 ID로 묶음.
- Windows 자격 증명 크기(2,560바이트) 때문에 1,000자 단위 분할 + sha256 확인. 끊긴 쓰기는 미스로 처리.
- 무효화: 목록의 revision·policy_revision 불일치, `use_tool` 상실·이름 사라짐(캐시 삭제), 만료(서버 `cache_seconds` 기본 86,400초, 클라이언트 상한 7일).
- 오프라인(`project_delivery_unknown`·`project_unavailable` 또는 `--offline`): 만료 전이고 같은 대상에서 받은 값만.

## 5. 한계

"Agent로부터 완전 격리"가 아니라 "두루마리 경로에서 값 비노출"이다. 자식 명령의 출력, 생성된 `.env`, Windows·Linux 저장소는 같은 사용자 권한의 Agent가 읽을 수 있다. 출력 차단 훅은 5단계(트랙 C), push 대상은 3단계, keychain import·웹 승인 화면은 4단계.
