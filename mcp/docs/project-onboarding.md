# 프로젝트 연결과 기존 자료 이전 (현행 안내)

이 문서의 이전 내용(`project-init`·`project-login`·`project-doctor`, 원격 ChatGPT·Claude 연결, 자동 수집 기반 이전)은 0.3.x 이전 방식이며 현행이 아니다. 원문은 Git 이력(커밋 `a3b44a7` 시점 내용)에 보존한다. 현행 동작의 기준은 [연결 계약](connection-contract.md)이다.

## 연결
1. 구름 owner가 웹에서 프로젝트를 만들고 저장소를 등록한다.
2. 각 참여자는 자기 Git 저장소 최상위 폴더(origin remote 필요)에서 durumari-connect 스킬 또는 `durumari-mcp context-connect --workspace <루트>`를 실행한다. 비Git 또는 origin 없는 폴더는 승인할 폴더 소스를 `--folder-source <UUID>`로 지정한다.
3. 브라우저에서 본인 계정으로 구름·프로젝트·저장소·작업 폴더를 확인하고 승인한다. Agent가 대신 승인하지 않는다.
4. 호스트를 다시 시작하고 `context-status`와 Context 검색·읽기로 확인한다.

기기·폴더마다 따로 승인한다. 연결 파일 복사로는 접근을 얻지 못한다. 폴더 이동·`git init`·저장소 분리 전에는 `context-status --change move|git-init|detach`로 재연결 필요 여부를 확인한다.

## 기존 자료 이전
- 현행 이전은 사람이 고른 내용을 Agent가 일반 Context 저장(`durumari_context_save`)으로 하나씩 올리는 방식이다. 원본은 그대로 두고, 저장마다 서버 영수증을 확인한다([durumari-migrate 스킬](../../durumari-agent/skills/durumari-migrate/SKILL.md)).
- 서버 보관 문서는 웹의 보관 문서 화면에서 사람이 정확한 버전을 선택해 승인한다.
- `project-import`·`project-export`는 폐기됐으며 실행하지 않고 현행 명령 안내를 반환한다. 이전은 durumari-migrate 스킬만 사용한다([연결 계약](connection-contract.md) 12절 결정 4).
- 저장소 전체·대화 원문·비밀값·첨부를 자동으로 올리지 않는다.

## 제거
같은 폴더에서 `context-remove`(또는 플러그인 remove)를 실행한다. 결과와 남는 항목은 [연결 계약](connection-contract.md) 8절을 따른다.

직접 호스팅·DB/키 백업과 복원은 서버 배포판의 호스팅 안내를 따른다.
