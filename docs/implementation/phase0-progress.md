# Phase 0 구현 기록 — P0-0 환경 / P0-1 접수 / P0-2 Worker·검색 / P0-3 삭제·철회

> 구현 진행 중 기록이다. 설계 기준선([Phase 0 Detailed Design v0.1](../design/phase0-detailed-design-v0.1.md))을 바꾸지 않고, 구현에서 선택한 값과 **실제로 관측한 결과**만 적는다. 추적: [Issue #6](https://github.com/YB-Park/LLM-Agent-Memory/issues/6)

## 1. 실행 방법

```bash
./scripts/verify.sh                     # 기준선: ubuntu:24.04 컨테이너 → Python 3.12 / PostgreSQL 16 → pytest
./scripts/verify.sh legacy              # 레거시: ubuntu:20.04 컨테이너 → Python 3.8 / PostgreSQL 12 → pytest
./scripts/verify.sh -k worker           # 추가 인자는 pytest로 전달
python3 -m memory_server.worker --once  # Worker 1회 실행 (큐가 빌 때까지)
```

수동 실행(환경 변수 `MEMORY_DATABASE_URL`, `MEMORY_HMAC_KEY` 필요, `PYTHONPATH=src`):

```bash
python3 -m memory_server.migrate
python3 -m memory_server.admin create-namespace --name personal
python3 -m memory_server.admin create-principal --name laptop     # 토큰은 한 번만 출력
python3 -m memory_server.admin grant --principal <id> --namespace <id> --write
python3 -m memory_server.rest --port 8080
```

## 2. P0-0 결정 (최초 선택 — 런타임 부분은 §5에서 대체됨)

| 항목 | 선택 | 이유 / 한계 |
| --- | --- | --- |
| 언어 | Python 3.8 (Ubuntu 20.04 배포판 기본) | 추가 저장소 없이 20.04에서 실행. 3.8은 upstream EOL |
| DB / 드라이버 | PostgreSQL 12 (20.04 배포판), `psycopg2` 2.8.4, 명시적 SQL (ORM 없음) | 20.04 단독 검증 가능. **PostgreSQL 12는 upstream EOL**(2024-11) — 실데이터 전 상위 버전 재검증 필요 |
| REST | 표준 라이브러리 `http.server` (평문 HTTP; TLS는 앞단 proxy) | 의존성 최소화. 운영용 서버 아님 |
| 관리 | CLI가 DB에 직접 접속 (`memory_server.admin`) | §9.3: 관리 전송은 CLI 하나만 |
| 인증 | opaque bearer, SHA-256 digest만 저장, 멱등성 키/요청은 HMAC | §10.1 private 프로필의 서버 측만. 클라이언트 연결·TLS는 P0-5 |
| 시험 | pytest + 실제 PostgreSQL, 별도 연결/스레드 | §11.1 |

**P0-0 발견 사항**

- **공식 `mcp` Python SDK는 20.04 기본 Python 3.8에서 설치할 수 없다** (`pip3 download mcp` → `No matching distribution found`). MCP(Streamable HTTP) 어댑터 단계 전에 (a) 20.04에 Python ≥3.10 별도 설치(deadsnakes 등 비배포판 저장소), (b) 다른 언어/SDK, (c) 지원 OS 변경 중 하나를 결정해야 한다. **미결정.** 업무 로직(`service.py`)은 전송과 분리되어 있어 어느 쪽이든 재사용된다.
- PostgreSQL 12에는 `DROP DATABASE ... WITH (FORCE)`(13+)가 없어 테스트 정리는 `pg_terminate_backend` 후 DROP을 쓴다.
- 슬림 이미지(`--no-install-recommends`)에는 snakeoil 인증서가 없어 시험용 PostgreSQL은 `ssl=off`로 기동한다. 운영 TLS 시험은 P0-5.

## 3. 20.04 검증 결과 (레거시 호환성 기록, P0-1 시점의 44 cases)

| 항목 | 값 |
| --- | --- |
| 사용자 공간 | `ubuntu:20.04` 컨테이너 — Ubuntu 20.04.6 LTS |
| 커널 / CPU | **6.12.8+** (호스트 커널을 공유), x86_64, 2 vCPU |
| 런타임 | Python 3.8.10, psycopg2 2.8.4, pytest 4.6.9, PostgreSQL 12.22 |
| 결과 | pytest 44 cases 통과 (`test_p0_1_submit.py` 40, `test_rest.py` 4) |
| 수동 스모크 | CLI(migrate/create-namespace/create-principal/grant) → REST 접수 202 → 같은 키 200 replay → 무인증 401 |

**이 결과가 뜻하는 것:** 20.04 **사용자 공간**(패키지·Python·PostgreSQL 12·libc)에서 P0-1 코드가 동작한다.
**뜻하지 않는 것:** 실제 20.04 **호스트**(커널 5.4/HWE, Docker Engine 설치, cgroup, 재부팅·복구)에서의 검증이다. 위 커널은 6.12다. §10.2의 "실제 호스트 실행 — 미검증" 항목은 그대로 **미검증**이며 프로젝트의 20.04 지원 선언도 하지 않는다.

## 4. P0-1 구현 범위와 설계 시험 대응

구현: `migrations/0001_init.sql`(§4.1의 11개 테이블·복합 FK·CHECK·인덱스), 마이그레이션 러너, 관리 CLI(namespace+빈 활성 세대 원자 생성, principal/grant), `submit`/`get_job`/`get_experience`(§5.1 `system_control` 잠금 규약, §6.2 멱등성), REST 어댑터(§9.1·§9.2 오류 매핑).

| 설계 시험 | 상태 | 위치 |
| --- | --- | --- |
| T1 접수 원자성·응답 유실 | P0-1 범위 통과 | `test_t1_*` |
| T2 동시 중복·범위별 키 | 통과 | `test_t2_*` |
| T8 범위 격리/존재 비노출/주입 | 접수·조회 경로만 통과 (검색은 P0-2) | `test_t8_*`, `test_unauthenticated_*`, `test_hostile_*` |
| T17 / T18 | 접수 시점 부분(409, 기본값 동등, `evidence_refs` 거절)만 통과. 본문 보존 Artifact·표시 구분은 P0-2 | `test_t17_*`, `test_t18_*` |
| T3~T7, T9~T16, T19, T20 | **미구현** | P0-2 이후 |

아직 없는 것: Worker/Reference Engine/검색(P0-2), 삭제·철회·retry CLI(P0-3 — 삭제 상태 테스트는 SQL로 tombstone을 만들어 접수·조회의 410 경로만 확인), Rebuild(P0-4), MCP·TLS·백업(P0-5), 서비스 계정과 migration 계정 분리.

## 5. 런타임 기준선 변경 (P0-2 착수 시 결정)

P0-0의 Python 3.8 / PostgreSQL 12는 upstream 지원이 끝났고 공식 `mcp` SDK가 3.8에서 설치되지 않는다(§2). 이후 기능을 EOL 런타임에 쌓지 않기 위해 기준선을 바꿨다.

| 항목 | 기준선 (신규) | 레거시 (보존) |
| --- | --- | --- |
| 컨테이너 | `docker/Dockerfile.ubuntu2404` — Ubuntu 24.04 | `docker/Dockerfile.ubuntu2004` — Ubuntu 20.04 |
| Python | 3.12.3 (배포판 패키지) | 3.8.10 |
| PostgreSQL | 16.15 (배포판 패키지) | 12.22 |
| 드라이버/시험 | psycopg2 2.9.9, pytest 7.4.4 | psycopg2 2.8.4, pytest 4.6.9 |
| 용도 | 모든 신규 시험의 기준 | P0-1 호환성 결과. 신규 기능은 여기서 통과를 요구하지 않음 |

선택 근거(관측): 공식 `mcp` SDK 2.3.0은 Python ≥3.10을 요구하며 24.04 컨테이너의 3.12에서 설치·import됐다(pydantic 2.14.3, starlette 1.10.0, uvicorn 0.54.0). 3.12는 24.04 배포판 기본이라 별도 저장소가 필요 없다. PostgreSQL 16은 배포판 패키지로 제공되고 지원 중이다. **MCP 어댑터 자체는 P0-5 범위이며 아직 구현·검증하지 않았다.**

호스트 OS와 앱 런타임은 구분한다: 두 시험 모두 "컨테이너 사용자 공간"의 검증이고 호스트는 Ubuntu 24.04 위의 Docker다. 실제 20.04 호스트·커널·운영 재기동은 여전히 미검증이다.

## 6. P0-2 — Worker, Reference Engine, 검색

### 구현 항목과 모듈 책임

| 모듈 | 책임 (하지 않는 것) |
| --- | --- |
| `engine.py` | 순수 함수 `ReferenceEngine.process(snapshot) → [ArtifactDraft]`. spec/digest 도우미. DB·인증·Job 상태·Generation 정책을 모른다 |
| `worker.py` | claim(SKIP LOCKED + lease) · heartbeat · reap · finalize(재검증 후 원자 공개) · 재시도/백오프. 엔진 계산 중에는 트랜잭션·잠금을 잡지 않는다 |
| `search.py` | `reference-keywords/v1`, `literal-substring/v1` 질의. 활성 Generation의 valid·성공 Job·보존 출처 artifact만 후보 |
| `textnorm.py` | 색인·질의 양쪽이 쓰는 단일 정규화(NFC + ASCII 소문자) |
| `service.py` | `MemoryService.search` (control SHARE → 인증 → 읽기 권한 → 활성 Generation) |
| `rest.py`, `admin.py`, `config.py` | `POST /v1/memories/search`, `create-namespace`의 engine_version/index_profile 인자, lease/heartbeat/attempt 설정 |

- **Explicit Memory**: 원문 그대로 1개 artifact(`local_key="0"`), 제목·힌트는 `payload.search`의 별도 메타데이터. 검증 상태는 `not_verified`, 근거는 `no_source_linked`로만 표기한다.
- **reference/1**: 일반 Experience 전체를 1개 projection. **reference/2**: 빈 줄(`\n\n`+) 기준 문단 projection, 순서(`local_key`)와 UTF-8 바이트 span을 출처에 기록(문단 ≤256개, 출력 ≤1 MiB).
- **결과 확정 규칙**: finalize는 control FOR UPDATE + job 행 잠금 아래에서 상태/attempt/owner/DB-시각 lease, 원본 상태(삭제), 입력 digest, 권한(principal·auth_epoch·can_write), Generation 상태, spec digest를 다시 확인한다. 하나라도 어긋나면 STALE(결과 폐기) 또는 Job 취소. artifact·`artifact_sources`·Job 성공·`result_revision`은 한 트랜잭션.
- **중복 실행 vs 중복 공개**: 중복 실행은 허용한다(lease 만료 후 재선점 시 이전 Worker는 계속 계산할 수 있다). 중복 *공개*만 attempt/lease fencing으로 막는다. 외부 부작용이 없는 엔진이라 exactly-once 구조는 두지 않았다.
- 검색 순위: 일치 키워드 수 → 제목 일치 수 → 힌트 일치 수 → 본문 일치 수 → experience_id → local_key (결정론적). 결과에 `matched_keywords`/`matched_fields`(`title`/`hints`/`body`), 출처(`experience_id`, `source_version`, 비검증 locator, span), `content_trust=untrusted_data`.

### 실제 실행한 시험

```bash
./scripts/verify.sh        # Ubuntu 24.04 / Python 3.12.3 / PostgreSQL 16.15
```

| 파일 | 설계 시험 | 내용 |
| --- | --- | --- |
| `test_p0_2_worker.py` | T3, T4, T17(일부) | 상호 배타적 claim, Worker 사망→reap→재선점, attempt 소진, 늦은 finalize 3종(reap 전·B 실행 중·B 성공 후), heartbeat, 커밋 전/후 프로세스 종료 모사(hook), 병렬 Worker 4×24 Job 중복 artifact 없음, 계산 중 잠금 비보유, 잘못된 엔진 출력 거절(8종), 재시도·백오프, 영구 오류, 미지원 엔진 버전, 입력 digest 변조 시 취소 |
| `test_p0_2_search.py` | T8(검색), T18 | 왕복, 출처/신뢰 상태, 제목·힌트 전용 일치, 순위·동률, 대소문자·한영 혼용·코드 식별자, limit/excerpt, 권한·존재 비노출, 입력 검증, literal 프로필, reference/2 span, **알려진 한계 5종 고정** |
| `test_rest.py` | | `POST /v1/memories/search` 200/404/400/401 |

결과: **98 passed** (기존 44 + 신규 54) — 24.04 / 3.12 / PG16 (30.8s). 레거시 `verify.sh legacy`(20.04 / 3.8 / PG12)에서도 98 passed(11.3s)이나, 신규 기능의 통과 요구 대상은 아니다. 병렬·경합 시험은 별도 연결과 스레드를 쓰는 실제 PostgreSQL이다. 프로세스 종료는 실제 kill이 아니라 예외 주입(hook)으로 모사했다.

### 기록한 검색 한계 (고정 시험, 의도된 현재 동작)

1. 조사 미처리: `데이터베이스를`로는 `데이터베이스` 본문이 검색되지 않는다(다른 단어는 일치).
2. 동의어 없음: `car maintenance`는 `자동차 정비`를 찾지 못한다.
3. 기호: `C++`는 자동 추출에서 버려져 `EMPTY_SEARCH_TERMS`; `keywords=["C++"]`가 필요하다.
4. 부분 문자열 오탐: `cat`이 `catalog`에 일치한다.
5. literal 프로필은 자연어 질문(`어떤 DB를 쓰는지 PostgreSQL 알려줘`)을 찾지 못하고 keywords 프로필은 찾는다 — 비교 기준선 역할.

### 설계와 다른 점 / 판단

- 설계 §6.3의 attempt 소진 처리를 "reap 시 `retry_count >= max`이면 `failed(LEASE_EXPIRED)`"로 구현했다(설계는 상한 초과 시 실패라고만 서술).
- finalize 시 Generation이 retired/aborted면 Job을 취소한다(결과를 공개하지 않음). 원본이 유효한 retired 세대의 Job 처리는 P0-4에서 재검토.
- 시험 DB는 세션 공용이므로 `World` 생성 시 이전 시험의 미완료 Job을 `TEST_LEFTOVER`로 취소한다(큐가 전역이기 때문). 시험 격리용 장치이며 제품 동작이 아니다.

### 아직 검증하지 못한 것

- 실제 OS 프로세스 kill·재시작 후 복구(예외 주입으로만 모사), 장시간 lease/clock skew, 대용량 큐 성능.
- 검색 품질 평가(회상률)는 하지 않았다. 위 한계는 고정만 했다.

## 7. P0-3 — 삭제, 권한 철회, 수동 정정

### 구현 항목과 모듈 책임

`operations.py`(신규)가 관리 명령의 유일한 구현이다. 모든 명령은 **control 잠금 exclusive 아래 한 트랜잭션**이라 접수·claim·finalize와 전순서가 정해진다. 전송은 관리 CLI뿐이며 REST/MCP에는 등록하지 않았다(시험으로 확인).

| 명령 (CLI) | 동작 |
| --- | --- |
| `delete-experience --experience ID` | 원본 tombstone(본문·메타·digest 삭제), 모든 Generation의 파생 artifact를 `invalidated`로 바꾸고 payload/표시/검색 텍스트 삭제, 미종료 Job을 `cancelled(SOURCE_DELETED)`+attempt 증가, `policy_revision`·영향 세대 `result_revision` 증가, audit(ID·개수만). 응답: `visibility=hidden`, `operational_payload=redacted`, `backup_erasure=not_guaranteed`. 멱등 |
| `revoke-principal --principal ID` | `revoked_at`+`auth_epoch` 증가, 그 principal이 요청한 미종료 Job을 `cancelled(AUTHORITY_REVOKED)`로 fence, `policy_revision` 증가. **공유 원본·기존 결과는 삭제하지 않음.** 멱등 |
| `grant` (권한 축소) | 기존 CLI에 추가: 축소 시 `auth_epoch` 증가와 동일하게 해당 principal의 미종료 Job fence |
| `retry-job --job ID --expected-attempt N --principal ID` | can_manage 보유 principal이 failed/cancelled Job을 **자기 권한으로** 재큐잉(requested_by/authority_epoch 교체, retry_count 초기화, audit에 이전·새 주체). succeeded·삭제 원본·retired/aborted 세대는 409. attempt 불일치는 409 `STALE_ATTEMPT`, 이미 적용된 동일 명령은 `already_applied` |

- **수동 정정**은 새 기능이 아니라 위 명령의 조합이다: `delete-experience` → 올바른 내용을 새 멱등성 키로 제출 → Job/검색 확인. 옛 키로 재제출하면 410이라 되살아나지 않는다. supersession·자동 통합 없음.
- 독립 원본 간 근거 의존성은 만들지 않았다: 일반 원본 삭제가 `source_ref`를 가진 Explicit Memory를 지우지 않는다(시험).
- `evidence_refs`는 여전히 접수 시 거절(P0-1)이며 변경하지 않았다.

### 실제 실행한 시험 (`tests/test_p0_3_policy.py`, 18 cases)

24.04 / Python 3.12 / PG16, 전체 **116 passed** (P0-1 44 + P0-2 54 + P0-3 18). 경합 시험은 별도 연결·스레드이며 "대기 중" 여부는 `pg_stat_activity`의 Lock 대기로 확인한다(sleep 추정 아님).

| 설계 시험 | 시나리오 | 결과 |
| --- | --- | --- |
| T5 | Worker 계산 후 / finalize 트랜잭션 직전 삭제 → 늦은 결과 폐기, artifact 없음 | 통과 |
| T5 | finalize가 잠금을 쥔 채 커밋 직전, 삭제 대기 → finalize 커밋 후 삭제가 즉시 무효화 | 통과 |
| T5/T6 | 접수 직후(Worker 전) 삭제, 공개 후 삭제, 멱등 재삭제, 알 수 없는 ID | 통과 |
| T6 | 다른 Generation의 결과까지 무효화, 이후 그 세대를 active로 바꿔도 검색 불가(실제 Worker로 2세대 생성) | 통과 |
| T7 | A 철회: A 401, A의 대기 Job 취소, B는 A가 만든 공유 결과·원본 계속 조회 | 통과 |
| T7 | 계산 중 철회 → 늦은 finalize 차단 / 커밋 직전 철회 → 이미 확정된 공유 결과는 유지 | 통과 |
| T7 | 취소된 Job은 B의 `retry-job` 전에는 공개 안 됨, retry 후 B 권한으로 공개, manage 없으면 404, 철회된 principal 404 | 통과 |
| T19 | 수동 정정 흐름, 옛 기억 재등장 없음, 독립 Explicit 비연쇄 삭제 없음 | 통과 |

### 설계와 달라진 점 / 판단

- 삭제의 `artifact_sources`는 행을 유지한다(복합 FK 때문). span은 위치 정보뿐이고 본문이 없다. 검색은 원본 `retained` 여부를 매번 확인하므로 이 행이 노출 경로가 되지 않는다.
- grant 축소가 해당 principal의 **모든 namespace** Job을 fence한다(auth_epoch가 principal 단위라서). 설계와 동일한 규칙이나 범위가 넓으므로 기록한다. 필요 시 manage 주체가 `retry-job`으로 되살린다.
- `ingest_receipts.request_digest`와 `jobs.input_digest`는 HMAC/SHA 파생값으로 삭제 후에도 남는다. 원문 복원은 불가하지만 "동일 내용 여부 추측"은 키를 가진 쪽에 가능하다. 설계 §6.4가 이 값을 지우라고 요구하지 않아 유지했다. 이후 변경 여부는 열린 이슈.
- 설계 §9.3의 REST `/v1/admin/*` 형태는 구현하지 않고 CLI만 둔다(P0-0 결정 "관리 전송은 CLI 하나"와 일치).

### 아직 검증하지 못한 것

- Rebuild·롤백 경로에서의 삭제 재노출 방지(T6 후반, T10 일부)는 P0-4에서 확인한다. 이번에는 과거 Generation을 SQL로 active 전환하는 모사만 했다.
- 백업/복원 후 삭제 재적용(T15)은 P0-5 범위다. `backup_erasure`는 `not_guaranteed` 그대로다.
- 삭제 도중 프로세스 kill은 트랜잭션 원자성에 의존하며 별도 kill 시험은 하지 않았다.
