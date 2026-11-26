# 배포물 호환성과 2단계 배포

`durumari-mcp update`는 설치된 runtime의 `releases.py`가 새 ZIP을 검사합니다. 따라서 새 배포물은 **이미 설치된 updater**가 받아들일 수 있어야 합니다.

## 검사 규칙

- 0.4.0–0.4.4 updater: 고정 허용 목록(`allowed_member`)에 없는 파일이 있으면 `invalid_release_archive`로 거부합니다. `runtime/bundle.json`의 모르는 키는 무시합니다.
- 0.4.5부터: `runtime/bundle.json`의 `catalog` manifest(`schema_version: 1`, `files: {경로: SHA-256}`)가 있으면 그 목록과 ZIP의 파일이 정확히 같고 해시가 모두 맞아야 합니다. 경로 탈출·절대 경로·역슬래시·심볼릭 링크·특수 파일·중복·대소문자 충돌·숨김 파일·Windows 예약 이름·파일 수(64)·전개 크기(24 MiB)·`bundle.json` 크기(16 KiB) 제한은 계속 적용됩니다. 허용 위치는 catalog `README.md`, `plugins/durumari-agent/` 아래(단, `runtime/`에는 해당 버전 wheel과 `requirements.txt`만), 최상위 숨김 호스트 디렉터리의 `.json` 파일입니다.
- manifest가 없는 이전 배포물은 고정 허용 목록으로 검사합니다.
- 릴리스 빌드(`build_release.py`)가 manifest를 만들고, 배포 전에 현재 updater로 다시 검사합니다.

## 2단계 배포 순서

1. **1단계 0.4.5**: manifest를 읽는 updater만 배포합니다. 새 호스트 파일은 넣지 않습니다. manifest는 기존 `bundle.json`의 새 키라 0.4.0–0.4.4 updater도 그대로 받습니다(`tests/test_release_manifest.py`가 동결된 0.4.4 updater `tests/fixtures/releases_0_4_4.py`로 확인).
2. 대부분의 사용자가 0.4.5 이상으로 업데이트될 때까지 기다립니다. 0.4.4 이하에 남은 사용자는 2단계 배포물을 받지 못하고 기존 설치가 유지됩니다. 0.4.6이 나온 뒤에는 `latest.json`이 0.4.5를 가리키지 않으므로 아래 「0.4.4 이하 설치본 복구」의 재설치로 안내합니다.
3. **2단계 0.4.6**: Agent 어댑터 파일(Cursor `.cursor-plugin/marketplace.json`·`plugins/durumari-agent/.cursor-plugin/plugin.json`, Gemini CLI `plugins/durumari-agent/gemini-extension.json` 등)을 넣은 배포물을 냅니다.

다음 호스트 파일을 추가할 때는 빌드 선택 목록(`allowed_member`와 빌드 스크립트의 최상위 marketplace 목록)에 추가하면 manifest에 자동으로 기록되며 updater 변경이 필요 없습니다. 위 허용 위치를 벗어나는 파일이나 manifest 형식 변경은 다시 2단계 배포가 필요합니다.

## 0.4.4 이하 설치본 복구

0.4.0–0.4.4 설치본에서 `update`를 실행하면 `invalid_release_archive`로 멈춥니다. 0.4.6부터 배포물에 들어간 Cursor·Gemini 어댑터 파일과 `skills/durumari-*/` 경로가 구 updater의 고정 허용 목록에 없기 때문입니다. 압축 해제 단계에서 멈추므로 catalog·runtime·호스트 플러그인은 바뀌지 않습니다.

구 updater는 정적 `/downloads/latest.json`만 읽고 자기 버전을 보내지 않습니다. 그래서 서버가 중간 버전(0.4.5)을 골라 줄 수 없습니다. 복구 경로는 같은 서비스의 최신 `install.py`로 다시 설치하는 것입니다(2026-10-18 Codex 0.4.1→0.4.14로 확인).

```sh
curl -fsSLO https://app.durumari.cloud/downloads/install.py && python3 install.py install
```

- Preview 설치본은 `https://preview.durumari.cloud`에서 받습니다. `https://durumari.kr`·`https://preview.durumari.kr`로 설치한 기존 설치본은 같은 서버의 이전 주소로 인정되어(`server_aliases.py`) 계속 업데이트됩니다. 서로 다른 서버(예: Live↔Preview)로 바꾸면 `release_origin_change_requires_new_installation`으로 거부됩니다.
- `install.py`는 빌드 때 복사한 최신 `releases.py`입니다(`plugins/build_release.py`). `install`과 `update`는 같은 `execute_release`를 실행하고, 기존 `installation.json`의 서비스 주소와 호스트 목록을 그대로 씁니다. `--client`를 생략해도 됩니다.
- 배포 ZIP의 크기·SHA-256을 `latest.json`과 대조한 뒤 설치합니다. 실패하면 이전 catalog·runtime·호스트 플러그인으로 되돌립니다.
- 바꾸는 범위는 `releases/` 아래 catalog, uv tool runtime, 호스트 플러그인입니다. OS 키체인의 자격 증명, `project-sync` 저장 큐, 작업 폴더의 프로젝트 연결은 건드리지 않습니다.
- 끝나면 호스트를 다시 시작하고 `durumari-status`로 버전과 연결을 확인합니다.

manifest 형식이나 허용 위치를 다시 바꿔 구 updater가 거부하게 될 때도 같은 경로로 복구합니다.
