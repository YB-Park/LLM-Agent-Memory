# Phase 0 구현 기록 — P0-0 환경 결정 / P0-1 저장·접수

> 구현 진행 중 기록이다. 설계 기준선([Phase 0 Detailed Design v0.1](../design/phase0-detailed-design-v0.1.md))을 바꾸지 않고, 구현에서 선택한 값과 **실제로 관측한 결과**만 적는다. 추적: [Issue #6](https://github.com/YB-Park/LLM-Agent-Memory/issues/6)

## 1. 실행 방법

```bash
./scripts/verify-ubuntu2004.sh          # ubuntu:20.04 이미지 빌드 → PostgreSQL 12 기동 → pytest
```

수동 실행(환경 변수 `MEMORY_DATABASE_URL`, `MEMORY_HMAC_KEY` 필요, `PYTHONPATH=src`):

```bash
python3 -m memory_server.migrate
python3 -m memory_server.admin create-namespace --name personal
python3 -m memory_server.admin create-principal --name laptop     # 토큰은 한 번만 출력
python3 -m memory_server.admin grant --principal <id> --namespace <id> --write
python3 -m memory_server.rest --port 8080
```

## 2. P0-0 결정 (구현에서 선택, 변경 가능)

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

## 3. 20.04 검증 결과 (이번 실행의 관측값)

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
