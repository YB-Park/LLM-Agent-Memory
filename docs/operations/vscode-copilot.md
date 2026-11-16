# VS Code Copilot Agent 연결과 C1~C8 시험 가이드

> **상태: 실제 VS Code Copilot 검증은 수행하지 못했다(미검증).** 개발 환경에는 VS Code GUI, Copilot 로그인, 사용자 PC·원격 호스트가 없다. 아래 "자동 검증" 열은 **공식 MCP Python 클라이언트(SDK 2.3.0)로 실제 프로토콜**(HTTPS Streamable HTTP, 초기화, 도구 목록, 도구 호출)을 사용한 시험이며, Copilot이 도구를 호출하는지·답변에 쓰는지는 전혀 확인하지 않았다. 따라서 **P0-5는 부분 완료**다.

## 1. 연결 설정

`docs/operations/examples/vscode-mcp.json`을 VS Code의 `mcp.json`(사용자 또는 `.vscode/mcp.json`)에 복사하고 URL의 호스트를 `.env`의 `MEMORY_ALLOWED_HOSTS`와 일치시킨다. 토큰은 `inputs`의 `password` 프롬프트로 입력되며 파일에는 없다.

**미검증 가정(실제 PC에서 먼저 확인할 것)**: 정적 `Authorization: Bearer` 헤더 방식이 현재 VS Code/Copilot 버전에서 그대로 동작하는지. 서버는 401에 `WWW-Authenticate`만 돌려주고 OAuth 메타데이터(`/.well-known/...`)를 제공하지 않으므로, 클라이언트가 OAuth 발견을 시도하며 실패할 수 있다. 그 경우 무인증으로 우회하지 말고, 지원되는 인가 솔루션을 두거나 클라이언트 범위를 조정해야 한다(결과를 이 문서에 기록).

사설 CA를 쓴다면 VS Code가 실행되는 PC(및 Node 프로세스)가 CA를 신뢰해야 한다(`NODE_EXTRA_CA_CERTS` 등). 개발용 CA는 시험 전용이다.

도구 4개만 노출된다: `memory_submit`, `memory_job_status`, `memory_search`, `memory_get_source`. 삭제·재처리·Generation 전환 도구는 없다(관리자 CLI 전용).

## 2. 사전 준비

```bash
A="docker compose --profile tools run --rm -T admin"
$A create-namespace --name personal                     # namespace_id 기록
$A create-principal --name pc-a ; $A create-principal --name pc-b
$A grant --principal <A> --namespace <ns> --read --write
$A grant --principal <B> --namespace <ns> --read --write
```

PC A와 PC B(또는 두 VS Code 프로필)에 각각 다른 토큰을 설정한다. **namespace_id를 Copilot에게 알려 주어야** 한다(도구 인자). 예: 지침 파일에 "memory 도구의 namespace_id는 <UUID>".

## 3. 시나리오

각 시험마다 아래를 기록한다. **이전 대화·워크스페이스 파일에서 읽은 답은 회상 성공이 아니다.** 판정은 반드시 MCP 호출 로그(서버 로그·VS Code의 MCP 출력 채널)의 실제 `memory_search` 호출과 반환 결과로 한다.

| ID | 절차 | 기대 | 자동 시험(MCP 프로토콜 클라이언트) | Copilot 수동 |
| --- | --- | --- | --- | --- |
| C1 명시적 저장 | "우리 프로젝트의 배포 DB는 PostgreSQL이라는 것을 기억해" | `memory_submit(intent=explicit_memory, actor_reported=agent)`, receipt+job_id, `memory_job_status`가 succeeded, Artifact 원문=제출 문구. 접수 성공≠기억 생성 | **자동 확인**(`test_explicit_memory_roundtrip_over_mcp`, compose e2e) | **필요** |
| C2 새 세션 회상 | 새 세션(이전 대화 없음)에서 "우리 프로젝트에서 어떤 DB를 사용하기로 했지?" | 실제 `memory_search` 호출, 결과의 출처·엔진 버전 표시 | 서버 측 검색·출처는 자동 확인. **에이전트의 호출 여부는 확인 불가** | **필요** |
| C3 키워드 재시도 | 다른 표현("데이터베이스")으로 질문 → query만 검색 결과를 기록 → `keywords=[...]` 재시도 | 첫 miss를 숨기지 않고 별도 기록 | 키워드/힌트 일치는 Phase 0 시험이 확인 | **필요** |
| C4 한영·코드 | PostgreSQL 대소문자, `foo_bar`, `parseHTTPResponse`, `C++/std::vector`(명시 keywords) | 한계 사례는 실패로 기록 | 일부 자동(P0-2) | 필요 |
| C5 출처·신뢰 | `memory_get_source`로 primary Experience 확인 | agent 제출/사용자 주장/locator-only/투영을 구분하고 "검증된 사실"로 말하지 않음 | 도구 응답 필드는 자동 확인 | **답변 표현 관찰 필요** |
| C6 공유와 삭제 | PC B에서 같은 기억 조회 → A 철회 후 A 거부·B 유지 → 관리자 삭제·정정 후 재등장 없음 | | 자동 확인(`test_revoking_a_device...`, e2e) | 실제 기기로는 필요 |
| C7 버전 비교 | reference/1 vs 2 Generation 비교 | | 자동 확인(P0-4, CLI) | Copilot 불필요 |
| C8 자동 호출 | "검색 도구를 쓰라"는 말 없이 새 세션에서 질문 | 아래 3분류 | **자동 확인 불가** | **필요** |

## 4. C2·C8 결과 분류 (Phase 1 평가용)

관측 결과를 다음 중 하나로 기록한다.

1. **도구 미호출**: MCP 로그에 `memory_search`가 없다(에이전트가 스스로 호출하지 않음 / 지침 없음).
2. **호출했으나 miss**: `memory_search`가 호출됐고 `results`가 비었다(키워드·조사·동의어 한계. 서버 로그의 호출 인자를 보존).
3. **결과를 받았으나 답변에 미사용**: `results`에 원하는 항목이 있었는데 최종 답변이 이를 반영하지 않았다.

1과 2·3은 원인이 다르다(에이전트 지침/도구 설명 vs 검색 알고리즘 vs 프롬프트 사용). 합쳐서 "회상 실패"로 세지 않는다.

## 5. 확인할 로그

- 서버: `docker compose logs api proxy` — 요청 경로·상태 코드(헤더·토큰은 기록되지 않아야 한다). 401/403/429/503의 의미는 [deployment.md §8](deployment.md#8-장애-해결).
- 토큰이 로그에 나타나면 즉시 해당 principal을 철회하고 재발급한다.
- DB 상태: `docker compose exec db psql -U postgres -d memory -c "select state,count(*) from jobs group by 1"`.

## 6. 결과 기록란 (사용자가 채움)

| ID | 날짜 | 클라이언트/버전 | 결과(통과/실패/분류) | 비고 |
| --- | --- | --- | --- | --- |
| C1~C8 | | | | |
