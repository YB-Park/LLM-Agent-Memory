# 배포·운영 가이드 (Phase 0, 개인용 단일 서버)

> 이 문서는 **절차 설명**이다. 여기 적힌 구성을 이 저장소의 개발 호스트 한 곳(Docker 29.1.3 / Compose 2.40.3 / Ubuntu 24.04, 합성 데이터, 개발용 CA)에서 실행해 본 결과만 [§9](#9-검증-범위)에 있다. **실제 외부 호스트·공인 도메인·실제 TLS 인증서·실데이터 운영은 검증하지 않았다.**

## 1. 구성

```
LAN/인터넷 ──TLS 8443──▶ proxy (Caddy) ──HTTP, edge 네트워크(internal)──▶ api (uvicorn: MCP + REST)
                                                                                │ backend 네트워크(internal)
                                                          worker (별도 프로세스) ┤
                                                                          db (PostgreSQL 16)
```

| 서비스 | 역할 | 비고 |
| --- | --- | --- |
| `db` | PostgreSQL 16, 볼륨 `pgdata` | 포트 미공개. 최초 기동 시 `deploy/db/10-roles.sh`가 역할 3개 생성 |
| `migrate` | 일회성 마이그레이션(스키마 소유자 권한) + 최소 권한 재부여 | 완료 후 api/worker 기동 |
| `api` | ASGI(uvicorn). MCP(`/mcp`)와 REST(`/v1/*`)가 같은 Application Service를 호출 | 런타임 DB 역할만 사용 |
| `worker` | `python -m memory_server.worker`. 같은 코드베이스의 별도 프로세스 | 정책 ledger 접근 없음 |
| `proxy` | Caddy. TLS 종료, 본문 128KB 제한, `/mcp`·`/v1/*`만 전달(그 외 404 — `/healthz`·`/readyz` 포함) | 유일하게 포트를 게시(기본 `127.0.0.1:8443`) |
| `admin`, `recovery` | 운영자 도구(`--profile tools`) | `up`으로 시작되지 않음 |

기본 하드닝: 읽기 전용 루트, `cap_drop: ALL`, `no-new-privileges`, 비root(uid 10001), `restart: unless-stopped`, 종료 유예 30–40초. `backend`·`edge` 네트워크는 `internal`이라 외부 경로가 없고, `public` 네트워크는 프록시의 게시 포트만 담당한다.

기존 `http.server` 기반 어댑터(`rest.py`)는 **삭제하고 ASGI(uvicorn + 공식 MCP SDK)로 교체**했다. 공식 MCP SDK 2.3.0은 Python 3.10 이상이 필요하고 REST와 MCP를 한 프로세스에서 제공해야 했기 때문이다. Application Service·도메인 로직은 그대로 재사용한다.

**관측한 이상**: 검증 호스트에서 Docker 내장 DNS가 `internal: true` 네트워크의 서비스 이름을 해석하지 못했다(새 네트워크로도 재현). 그래서 고정 IP(`db` 172.29.0.5, `proxy` 172.28.0.2, `api` 172.28.0.10)와 `extra_hosts`를 쓴다. 다른 호스트에서 서브넷이 충돌하면 `docker-compose.yml`의 IPAM과 `--forwarded-allow-ips`(프록시 IP)를 함께 바꿔야 한다.

## 2. 최초 설치

```bash
./scripts/gen-secrets.sh            # ./secrets (0700, gitignored). 기존 파일은 덮어쓰지 않는다
```

`.env` (저장소에 포함하지 않음, gitignore):

```
MEMORY_ALLOWED_HOSTS=memory.example.net   # 필수. 클라이언트가 쓰는 Host 이름(쉼표 구분). 없으면 compose가 실패한다
MEMORY_ALLOWED_ORIGINS=                   # 브라우저 Origin을 허용할 때만
MEMORY_PUBLIC_PORT=8443
MEMORY_PUBLIC_BIND=127.0.0.1              # 외부에 열려면 의도적으로 0.0.0.0 또는 NIC 주소
MEMORY_BACKUP_DIR=./backups               # 백업 보관 위치(별도 디스크 권장, uid 10001 쓰기 가능: chown 10001)
```

```bash
# TLS 인증서: §4에서 하나를 선택해 ./certs/server.crt, ./certs/server.key 를 준비
docker compose up -d --build
docker compose ps                         # migrate는 Exited(0), api/worker/proxy는 healthy
```

시작 순서: `db`(healthy) → `migrate`(성공 종료) → `api`(`/readyz` healthy) → `worker`, `proxy`. 마이그레이션은 `migrate` 서비스가 advisory lock 아래 한 번만 적용하며 api/worker는 DDL 권한이 없다.

### 환경 변수 (애플리케이션)

비밀 값은 `*_FILE`(파일 경로)로 받는다. 값 자체를 환경 변수나 compose 파일에 넣지 않는다.

| 변수 | 의미 |
| --- | --- |
| `MEMORY_DATABASE_URL[_FILE]` | 런타임 DB 연결(`memory_app`) |
| `MEMORY_HMAC_KEY[_FILE]` | credential 다이제스트 HMAC 키(32자 이상) |
| `MEMORY_MIGRATION_DATABASE_URL[_FILE]`, `MEMORY_RUNTIME_ROLE`, `MEMORY_BACKUP_ROLE` | `migrate` 전용 |
| `MEMORY_BACKUP_DATABASE_URL[_FILE]` | `recovery backup` 전용(읽기 전용 역할) |
| `MEMORY_ALLOWED_HOSTS`, `MEMORY_ALLOWED_ORIGINS` | Host/Origin 허용 목록. 목록에 없는 Host는 loopback(상태 확인용)만 |
| `MEMORY_POLICY_LEDGER`, `MEMORY_REQUIRE_LEDGER` | 정책 ledger 경로와 "ledger 없이는 기동 거부" |
| `MEMORY_AUTH_FAIL_LIMIT`, `MEMORY_REQUEST_LIMIT_PER_MINUTE` | 인증 실패 제한(클라이언트 IP별)과 credential별 분당 요청 예산 |

## 3. DB 권한 분리

| 역할 | 용도 | 권한 |
| --- | --- | --- |
| `postgres` (부트스트랩) | 최초 초기화 스크립트만 | 어떤 서비스도 사용하지 않음 |
| `memory_owner` | `migrate` | 스키마 소유(DDL) |
| `memory_app` | api, worker, admin | 테이블 SELECT/INSERT/UPDATE, 시퀀스 사용. DELETE/TRUNCATE/DDL/역할 관리 없음. `audit_events`·`schema_migrations`는 UPDATE 불가, `schema_migrations`는 INSERT도 불가 |
| `memory_backup` | `recovery backup` | SELECT만 |

`provision_roles`는 매 마이그레이션마다 권한을 다시 부여하고, **PostgreSQL 15 미만의 기본값(PUBLIC의 `public` 스키마 CREATE)이 남아 있으면 실패**한다(레거시 PG12 시험에서 실제로 드러났다). 테스트의 SUPERUSER는 `TEST_DATABASE_URL`의 일회성 컨테이너에서만 쓴다.

## 4. TLS

Caddy는 `./certs/server.crt`·`server.key`를 읽는다(`auto_https off`, TLS 1.2+). 선택지:

1. **개발·시험**: `./scripts/dev-certs.sh` — 30일짜리 임시 CA와 localhost 인증서. 클라이언트가 CA를 신뢰해야 한다. **시험 전용.**
2. **자체 인증서/사설 CA**: 두 파일을 배치하고 클라이언트에 CA를 설치.
3. **ACME(Let's Encrypt)**: `deploy/Caddyfile`의 `auto_https off`·`tls` 지시문을 도메인 블록과 자동 HTTPS로 바꾸고 80/443을 게시한다. **구성하지도 시험하지도 않았다.**

평문 HTTP 8080은 `edge`/`backend` 내부 네트워크에만 있다. 시험에서 호스트 8443에 평문 HTTP를 보내면 400이었고 8080/5432는 호스트에 게시되지 않았다.

## 5. 기기별 credential

각 기기는 별도 principal과 credential을 가진다. 서버는 HMAC 다이제스트만 저장하고 토큰 원문은 생성 시 한 번만 출력된다. 요청마다 인증과 namespace 권한을 DB에서 다시 확인하므로(stateless HTTP) MCP 세션 재사용이 철회를 우회하지 못한다.

```bash
A="docker compose --profile tools run --rm -T admin"
$A create-namespace --name personal
$A create-principal --name laptop                           # principal_id와 token (한 번만 출력)
$A grant --principal <id> --namespace <ns> --read --write
$A rotate-credential --principal <id> --name laptop-v2      # 같은 권한의 새 principal을 만들고 기존 것을 철회
$A revoke-principal --principal <id>                        # 즉시 차단. 같은 namespace의 다른 기기는 유지
```

토큰은 클라이언트의 비밀 저장소에만 둔다. 서버는 기록하지 않는다(Caddy는 요청 헤더를 로그에서 제거, 애플리케이션은 access log 비활성, e2e에서 전체 컨테이너 로그를 검사).

**`MEMORY_HMAC_KEY`를 바꾸면 모든 credential이 무효화**되므로 키 교체는 전 기기 재발급과 함께 계획한다. 키는 데이터 백업과 **별도로** 보관해야 한다([backup-recovery.md](backup-recovery.md)).

## 6. 종료·재시작

- `docker compose stop`/`restart`: api는 uvicorn graceful shutdown(20초). worker는 SIGTERM에서 진행 중인 Job을 마친 뒤 종료하고(`worker stopped` 로그), 끝내지 못한 Job은 lease 만료로 재시도된다. `kill -9`도 안전하며 lease(30초) 후 attempt+1로 재선점된다.
- 입력 gate가 `paused`(Rebuild 중)인 채 전체 재시작해도 gate는 DB에 남아 자동으로 열리지 않는다. `resume-namespace`로만 연다.
- DB가 내려가면 api는 `/readyz`가 실패하고 인증이 필요한 요청은 **503 `DB_UNAVAILABLE`**(내부 정보 없음)로 응답한다. 복구되면 별도 조치 없이 정상화된다.

## 7. 헬스·준비 상태

`/healthz`(프로세스), `/readyz`(DB·서비스 상태)는 loopback Host에서만 응답하며 프록시를 통해서는 404다. 컨테이너 헬스체크가 `127.0.0.1:8080`을 직접 조회한다.

## 8. 장애 해결

| 증상 | 원인·조치 |
| --- | --- |
| `docker compose up`이 `MEMORY_ALLOWED_HOSTS` 오류 | `.env`에 클라이언트가 쓰는 Host를 설정 |
| 클라이언트 400 | Host/Origin이 허용 목록에 없음 |
| 401 + `WWW-Authenticate` | 토큰 오류·철회. 인증 실패가 반복되면 429(클라이언트 IP 기준) |
| 503 `RECOVERY_QUARANTINE` | 복원 후 격리 상태. [backup-recovery.md](backup-recovery.md)의 검증·release 전에는 정상 동작 |
| 503 `INGEST_PAUSED` | Rebuild 중 접수 중단. `Retry-After` 후 재시도 |
| 503 `DB_UNAVAILABLE` | DB 중단/연결 불가. `docker compose logs db` |
| Job이 `running`에서 멈춤 | worker가 죽었다면 lease(30초) 후 자동 재시도. `docker compose ps worker` |
| `migrate`가 "can CREATE in schema public"으로 실패 | DB 관리자가 `REVOKE ALL ON SCHEMA public FROM PUBLIC` 실행(PG<15 기본값) |
| secrets를 읽지 못함 | 파일 모드/소유자. 컨테이너는 uid 10001로 실행 |

## 9. 검증 범위

| 항목 | 상태 |
| --- | --- |
| Compose 스택(db/migrate/api/worker/proxy) 빌드·기동, 8개 e2e | **검증됨**(이 호스트 1곳, 합성 데이터). `E2E_COMPOSE=1 pytest --noconftest tests/test_p0_5_compose_e2e.py` (호스트 venv에 `mcp`·`pytest` 필요) |
| TLS 전용 접근, 평문 거부, 포트 비게시, 인증 없는 접근 401 | **검증됨**(개발용 CA, localhost) |
| worker `kill -9` 후 복구, worker 중단 중 접수된 Job 처리, api·DB 재시작, gate paused 상태의 전체 재시작 | **검증됨**(컨테이너 수준) |
| 로그의 credential 누출 없음 | 이 e2e 실행 로그에서만 검사 |
| 백업·격리 복원·정책 재적용·release 거부 | **검증됨**(합성 데이터, 같은 DB 서버의 별도 DB) |
| 실제 Ubuntu 20.04 **호스트**(커널·systemd·패키지) | **미검증**. 20.04 컨테이너 사용자 공간에서 Python 3.8/PG12로 비-MCP 테스트만 통과(MCP SDK가 3.10+ 필요) |
| 공인 도메인·ACME·외부 네트워크 노출 | **미검증** |
| 장기 운영·대용량·디스크 풀·WAL 아카이브 | **미검증** |
| 의존성 해시 고정(`--require-hashes`)·이미지 취약점 스캔 | **미수행**(버전 고정만) |

### 합성 데이터와 실데이터의 경계

지금까지의 모든 시험은 합성 데이터다. 실제 개인·업무 데이터를 넣으려면 최소한 (1) 실제 호스트에서 위 절차를 재현하고, (2) 신뢰되는 인증서로 TLS를 구성하고, (3) 별도 위치로의 암호화 백업과 격리 복원·정책 재적용을 해당 환경에서 시험하고, (4) 실제 VS Code Copilot 연결을 확인해야 한다([vscode-copilot.md](vscode-copilot.md)). 현재 상태는 **실데이터 운영 미승인**이다.
