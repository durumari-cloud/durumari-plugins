# Context 그래프 이웃 계약

상태: **초안**.

## 1. 무엇이 이웃인가

웹 그래프(`GET /api/v2/projects/{project}/graph`)가 그리는 **단언된 연결**만 이웃이다.

| origin | relation | 방향 | 출처 |
|---|---|---|---|
| `manual` | `references`·`supports`·`implements`·`blocks` | 저장 시 `relates_to` 또는 웹 수동 연결의 source→target | `context_relations` |
| `supersession` | `supersedes` | 대체하는 문서 → 대체된 문서 | `cloud_context_supersessions` |
| `provenance` | `references` | 출처 `kind=document`, `source_id=<id>@<revision>`을 가진 현재 revision → 그 문서 | `RevisionDetails.provenance` |

- 추정 유사도, 본문 속 ID, `source_url`, revision 없는 `source_id`는 이웃이 아니다(graph.py 원칙).
- 요약 페이지(`ProjectPage`) 안의 연결은 페이지 화면 전용이며 이웃에 포함하지 않는다.
- 이웃은 같은 프로젝트의 삭제되지 않은 현재 문서뿐이다. 검색 결과가 이미 보여 주는 범위(제목·revision·kind·상태) 밖의 정보는 없다. 공개 전 레거시 revision을 가리키는 출처 연결은 제외한다.

## 2. 항목 형식

```json
{"id": "...", "title": "최대 120자", "revision": 3, "kind": "decision", "state": "active",
 "relation": "supports", "direction": "out", "origin": "manual", "stale": false}
```

- `direction`: `out` = 읽는 문서가 source, `in` = 읽는 문서가 target.
- `revision`: 이웃의 현재 revision. `stale`: 연결이 가리키던 이웃 revision이 지금과 다르면 `true`(들어오는 출처 연결은 항상 `false`).
- 정렬: relation(`supports` → `implements` → `references` → `blocks` → `supersedes`) → kind(`decision` → `spec` → `architecture` → `instruction` → 나머지) → origin(`manual` → `supersession` → `provenance`) → 최근 수정 순.
- 같은 (이웃, relation, 방향)은 한 번만 낸다.

## 3. 읽기 결과에 포함

- `GET /contexts/{id}`(HTTP)와 MCP `durumari_context_read`는 **첫 페이지**(`after` 없음, `section_id` 없음)의 `document`·`outline` 보기에 `neighbors`(최대 5건)와 `neighbors_truncated`를 넣는다.
- 이웃이 없으면 두 키를 넣지 않는다. 이어 읽기·절 선택·`provenance`·`lifecycle` 보기에는 넣지 않는다.
- 이웃 블록도 `budget_tokens` 안에서 계산된다.

## 4. 이웃 조회 (`related`)

- HTTP: `GET /api/v2/projects/{project}/contexts/{id}/related?depth=1|2&limit=1..10` (기본 depth 1, limit 5)
- MCP: `durumari_context_related(context_id, depth=1, limit=5)` — 읽기 전용, 값 없음.
- 플러그인: `ContextClient.read("/" + id + "/related", depth=…, limit=…)`(게이트웨이 경로 허용 목록에 `related` 추가).
- 응답: `{"id","revision","depth","items":[…, "depth": 1|2, "via"?],"truncated","trust":"untrusted_evidence"}`
- `limit`은 두 단계 합계다. 1단계를 먼저 채우고 남은 수만큼 2단계(`via` = 거쳐 온 1단계 이웃 ID)를 채운다.
- 서버 함수: `durumari.cloud_context.neighbors.related(db, actor, project_id, context_id, depth, limit, relations=None)`. 같은 프로세스에서 relation을 거를 때 `relations`를 쓴다.

## 5. 주입 훅(트랙 C) 사용법

| 단계 | 호출 |
|---|---|
| 끄기·가볍게 | 호출하지 않는다 |
| 보통(기본) | 검색 상위 문서마다 `related(depth=1, limit=2)` |
| 깊게 | 검색 상위 문서마다 `related(depth=2, limit=5)` |

- 주입은 제목·ID·revision·관계만 넣고, `trust=untrusted_evidence`를 함께 표시한다. 본문은 모델이 `durumari_context_read`로 읽는다.
- 여러 검색 결과의 이웃이 겹치면 훅이 ID로 합친다.

## 6. 저장·수정 시 `relates_to`

- `durumari_context_save`의 `relates_to=[{"context_id", "relation"?, "revision"?}]`(최대 8), `durumari_context_revise`의 `changes.relates_to` 같은 형식. HTTP `POST /contexts`·`PATCH /contexts/{id}` 본문도 같다.
- `relation` 기본값은 `references`. `revision`을 생략하면 대상의 현재 revision에 고정한다.
- 저장된 revision(source) → 대상(target)으로 `context_relations` 행을 만든다. 대상이 다른 프로젝트·삭제됨·자기 자신이면 `422 invalid_context_reference`, 없는 revision이면 `404`. 실패하면 문서도 저장하지 않는다.
- 멱등: 같은 요청 키 재전송은 기존 영수증을 돌려주고 관계를 다시 만들지 않는다. 한 요청 안의 중복과 같은 revision 쌍의 기존 활성 관계는 한 번만 둔다.
- 영수증 형식은 바꾸지 않는다(구형 클라이언트의 엄격 검증 유지). 만든 관계는 읽기 결과의 `neighbors`로 확인한다.
- 비어 있는 `relates_to`는 요청 본문에서 빼고 보낸다. 서버도 빈 값을 지문에서 제외해 이전에 저장한 요청 키의 재전송이 충돌하지 않는다.
- `relates_to`만 있는 수정도 새 revision을 만든다(관계는 revision에 고정되기 때문).
- 수정으로 새 revision이 생기면 이전 revision에서 나간 관계는 웹 그래프에서 `stale`로 남는다. 계속 연결하려면 수정할 때 `relates_to`를 다시 준다.

## 7. 기존 사용자 적용

- 이웃 반환·`related` 도구: 서버 배포로 즉시 전체 적용. 로컬 stdio 플러그인은 새 버전에서 도구가 보인다.
- `relates_to`: 서버 도구 설명은 즉시, 로컬 스킬 안내는 업데이트 알림 후(트랙 C 소유).
- 본문 속 ID에서 "추출된 연결" 후보 만들기는 이 범위 밖이다(후속).
