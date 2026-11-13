# 백업·복구와 삭제·철회 재노출 방지

> 핵심 불변조건: **삭제된 원본이나 철회된 권한이 백업 복구 때문에 다시 활성화되어서는 안 된다.** 오래된 백업은 이후의 삭제·철회를 알 수 없으므로, 백업과 별도로 보관되는 **정책 ledger**를 기준으로 복구 후 재적용하고, 검증이 끝날 때까지 서비스를 비공개(격리)로 둔다. 복구 기본 정책은 fail closed다.

## 1. 구성 요소

| 요소 | 내용 |
| --- | --- |
| 백업 | `recovery backup`: 읽기 전용 DB 역할로 `pg_dump`(custom) + `manifest.json`을 tar로 묶어 **gpg 대칭 AES-256**으로 암호화. manifest에는 ledger head, `policy_revision`, 마이그레이션, 엔진 spec, HMAC 키 지문이 있다 |
| 정책 ledger | `MEMORY_POLICY_LEDGER`(`/ledger` 볼륨)의 해시 체인 JSONL. `delete_experience`, `revoke_principal`, `grant_reduce`를 **DB 커밋 전에** fsync로 기록(충돌 시 과다 적용 쪽으로 실패). ledger 쓰기가 실패하면 해당 연산이 중단된다 |
| 복원 | 비어 있는 DB에만 복원하고 즉시 `quarantined`로 둔다 |
| 재적용 | ledger 전체를 멱등 재생(삭제·철회·권한 축소). 권한은 **줄이기만** 한다. 실행 중이던 attempt는 fence(재큐잉, attempt+1) |
| 검증 | 불변조건과 ledger의 모든 항목이 복원 DB에 반영됐는지 확인(실패 시 종료 코드 3) |
| release | `--attest-ledger-is-latest` 명시, 검증 통과, 재적용한 ledger head가 **현재** head와 같을 때만 서비스 재개 |

`quarantined` 상태에서는 접수·검색·조회·인증·worker claim/finalize가 모두 503 `RECOVERY_QUARANTINE`이며 관리/복구 경로만 동작한다. Worker는 자동으로 일을 재개하지 않는다.

## 2. 백업

```bash
R="docker compose --profile tools run --rm -T recovery"
mkdir -p backups && sudo chown 10001 backups            # 컨테이너가 uid 10001로 쓴다
$R backup --passphrase-file /run/secrets/backup_passphrase --out-dir /backups
```

결과물: `backups/memory-<UTC>.tar.gpg` (모드 0600). 이 파일을 **별도 저장 위치**로 복사한다. 다음은 **별도로, 서로 다른 위치에** 보관해야 한다.

1. `secrets/backup_passphrase` — 없으면 아카이브를 열 수 없다.
2. `secrets/hmac_key` — 없거나 다르면 복원 후 기존 credential이 검증되지 않는다(복원 시 경고). 이 경우 credential을 모두 재발급한다.
3. **ledger(`ledger` 볼륨의 `policy.jsonl`)** — 백업보다 *최신*이어야 한다. DB 백업과 같은 위치에만 두면 의미가 없다. ledger는 append-only이며 매일 한 번이 아니라 **변경 시점마다** 오프사이트로 복제하는 것이 이상적이다.

복원에 필요한 서비스/엔진 버전은 manifest와 저장소 태그로 확인한다.

## 3. 격리 복원 (실DB를 덮어쓰지 않는다)

복원은 **새로 만든 빈 DB**에만 한다(비어 있지 않으면 거부).

```bash
# 1) DB 관리자가 빈 DB와 접속 파일을 준비(스키마 소유자 권한)
docker compose exec db psql -U postgres -c \
  "CREATE DATABASE memory_restore OWNER memory_owner TEMPLATE template0 ENCODING 'UTF8'"
printf 'postgresql://memory_owner:%s@db:5432/memory_restore' "$(cat secrets/db_owner_password)" > secrets/restore_database_url

# 2) 복원 → 재적용 → 검증 (모두 격리 상태에서)
M="-v $PWD/secrets/restore_database_url:/run/secrets/restore_database_url:ro"
T="--target-database-url-file /run/secrets/restore_database_url"
docker compose --profile tools run --rm -T $M recovery restore --archive /backups/<file> \
    --passphrase-file /run/secrets/backup_passphrase $T
docker compose --profile tools run --rm -T $M recovery reapply $T          # 필요 시 --revoke-all-principals
docker compose --profile tools run --rm -T $M recovery verify  $T          # 실패하면 exit 3
```

격리 상태에서는 서비스 요청이 모두 503이므로 확인은 `verify`와 psql 질의(삭제된 원문·철회된 principal)로 한다. 통과하면 다음으로 넘어간다.

```bash
docker compose --profile tools run --rm -T $M recovery release $T --attest-ledger-is-latest
```

`release`는 복원 DB를 서비스 상태로 바꿀 뿐이다. 이후 **운영 DB를 복원본으로 교체하는 일**(연결 문자열 전환 등)은 운영자의 수동 절차이며 자동화하지 않았다.

### release가 거부되는 경우 (시험됨)

- `--attest-ledger-is-latest` 없음
- `verify` 실패, 또는 `reapply`가 아직 수행되지 않음
- ledger 체인 손상·누락, ledger head가 백업의 head보다 오래됐거나 분기됨(= 최신 정책 이력을 확보하지 못함) → 격리 유지
- 재적용 이후 ledger가 더 늘어남 → 다시 `reapply`

**최신 정책 이력을 확보하지 못했다면 이전 백업을 그대로 외부에 공개하지 않는다.**

## 4. 시나리오와 시험 근거

| 시나리오 | 결과 | 시험 |
| --- | --- | --- |
| 백업 후 Experience 삭제, 이전 백업 복원 | 복원 직후엔 원문과 철회 전 principal이 되살아나 있고(`verify` 실패), 재적용 후 원문이 제거되고 철회가 반영되어 `verify` 통과, release 뒤 검색에서도 나타나지 않음(`test_p0_5_recovery`) | `test_p0_5_recovery`, compose e2e(컨테이너 기반) |
| 백업 후 기기 A 철회 | 재적용 후 A 철회, B 유지 | 동 |
| 백업 후 권한 축소, 이후 재부여 | 축소는 재생되고 확대는 되돌리지 않음(재부여는 운영자가 다시 수행) | 동 |
| 백업 시점에 running이던 attempt | 재큐잉 + attempt 증가(fence) | 동 |
| 비어 있지 않은 대상 DB | 복원 거부 | 동 |
| ledger 손상·없음 | 격리 유지, release 불가 | 동 |
| 백업 파일 | 암호화됨(평문 문자열 없음), 패스프레이즈 없이는 열리지 않음 | 동 |
| 출력/로그의 비밀 | 없음 | 동 |

## 5. 한계 (명시)

- **복원 이후 새로 만든 데이터는 없다**: 백업 이후의 Experience·Job·credential은 복원 DB에 없다. 정책(삭제·철회)만 ledger로 되살린다. 백업 이후 발급한 credential은 사라지므로 다시 발급해야 한다.
- 삭제된 원문의 **물리적 소거를 보장하지 않는다**: 이미 만들어진 백업 파일·스냅샷에는 삭제 전 원문이 남아 있다(`backup_erasure: not_guaranteed`). 암호화와 보관 위치 통제, 보존 기간 만료 삭제가 별도로 필요하다.
- ledger의 해시 체인은 **우발적 손상·부분 절단은 탐지**하지만, ledger 볼륨에 쓰기 권한이 있는 주체가 체인 전체를 다시 쓰는 것은 막지 못한다.
- ledger가 최신이라는 판단은 운영자의 **attestation**이다. 시스템은 백업의 head와 비교할 뿐 외부의 진짜 최신 ledger를 알 수 없다.
- 모든 재해 상황의 무손실 복구, PITR/WAL 아카이브, 자동 보존 정책, 오프사이트 전송은 구현하지 않았다.
- 복원 시험은 합성 데이터, 같은 PostgreSQL 서버의 별도 DB에서만 했다. 다른 호스트·다른 PostgreSQL 마이너 버전으로의 복원은 시험하지 않았다.

## 6. 운영 권장 (미자동화)

- ledger와 백업을 서로 다른 위치에 보관하고 ledger는 변경 즉시 복제한다.
- 복구 시험을 정기적으로(합성 데이터로) 수행한다.
- 복구 후에는 `--revoke-all-principals`로 모든 기기를 철회하고 credential을 재발급하는 쪽이 가장 보수적이다.
