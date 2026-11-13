# LLM Agent Memory

LLM/Agent Harness에 종속되지 않는 **개인 소유 장기 에이전트 메모리 시스템**을 실험·개발하는 프로젝트입니다. GitHub Copilot Agent(VS Code)를 첫 클라이언트로 삼고, 여러 PC·세션·다른 에이전트와 같은 원격 기억을 활용하는 것을 목표로 합니다.

> **현재 상태: Phase 0 구현 진행 중 (P0-0/P0-1/P0-2 구현·로컬 시험 통과. 삭제·재구축 이후 미구현). 기준 런타임: Ubuntu 24.04 컨테이너 / Python 3.12 / PostgreSQL 16, 20.04는 레거시 호환성 기록**  
> 설계 기준선은 채택됐고 접수·멱등성·범위 격리·Worker·Reference Engine·검색까지 코드와 시험이 있습니다. **삭제·철회/Rebuild/MCP/TLS 및 실제 20.04 호스트·Copilot 클라이언트 검증은 아직 완료되지 않았습니다.** 진행·관측 결과: [구현 기록](docs/implementation/phase0-progress.md) — 시험 실행: `./scripts/verify.sh` (레거시: `./scripts/verify.sh legacy`)

## 먼저 읽을 문서

1. **[Architecture v0.1](docs/architecture/v0.1.md)** — 시스템 목적, MCP/REST·서비스·원본/기억 분리와 장기 아키텍처 원칙
2. **[MVP Contracts v0.1](docs/design/mvp-contracts-v0.1.md)** — 경험·Job·Memory·Generation의 개념적 책임·상태와 불변조건
3. **[Phase 0 Detailed Design v0.1](docs/design/phase0-detailed-design-v0.1.md)** — **Phase 0 구현 기준선.** Reference Engine, PostgreSQL 스키마·API/MCP 후보, Worker 동시성, 삭제, 세대 전환, 테스트 및 구현 순서

**[Phase 0 구현 트래커 — Issue #6](https://github.com/YB-Park/LLM-Agent-Memory/issues/6)**에서 P0-0~P0-5 구현과 검증을 추적합니다. 현재 세부 구현에서는 3번 문서를 우선 참조하되, 상위 설계 원칙을 위반해서는 안 됩니다.

## Phase 0의 경계

- **기본 구조:** 개인용 원격 MCP + REST Adapter, Modular Monolith, Background Worker, PostgreSQL. 작은 UTF-8 텍스트 원본·접수/Job을 같은 PostgreSQL 트랜잭션에 기록.
- **Reference Memory Engine:** `reference/1`과 `reference/2`. 일반 Experience는 원본 전체/문단 투영, Explicit Memory는 제출된 문구 그대로 보존. 키워드/제목/힌트 기반의 제한적 검색으로 새 세션 회상을 시험.
- **데이터·운영:** 인증 주체 및 namespace 권한, 출처·버전, Job attempt/lease, 삭제 후 재노출 차단, 격리된 세대 비교·수동 전환. Phase 0 Rebuild는 입력을 잠시 중지할 수 있으며 **신규 입력 후 단순 포인터 롤백을 허용하지 않음**.
- **범위 밖:** 실제 강한 근거 연결(`evidence_refs`), 자동 기억 추출·통합·모순 해결, 연상 그래프, 중요도/기억 강도/접근성·망각, 임베딩/LLM 메모리 알고리즘, 무중단 전환, 범용 플러그인 프레임워크.
- **수동 정정:** 잘못된 명시적 기억은 관리자 CLI로 해당 Experience 삭제 후 수정 문구를 새 키로 제출. 독립 Experience들 사이의 근거 의존성을 추측해 연쇄 삭제하지 않음.

## 개발·검증 원칙

**설계 기준선은 변경 가능**합니다. 상세한 DB/API 값과 기술 스택은 구현·시험 결과를 바탕으로 필요하면 수정하고 이유를 기록합니다. Hindsight·MemoryLACE 등은 참고 연구일 뿐 채택이 확정되지 않았습니다. Phase 1은 Phase 0 Reference Engine의 실제 사용 결과를 바탕으로 계획합니다.

실제 개인·업무 데이터를 투입하기 전에는 **원격 인증/TLS, 기기별 철회, 삭제·보존 범위, 백업/격리 복구**를 검증해야 합니다. 그 전까지는 합성 테스트 데이터로 개발합니다. Ubuntu 20.04는 호환성 **희망 사항**이며 공식 지원·실제 호스트 검증 완료를 뜻하지 않습니다. 현재는 `ubuntu:20.04` 컨테이너(사용자 공간)에서의 시험만 있습니다.

상위 설계는 `docs/architecture/`, 세부/버전별 설계는 `docs/design/`에 둡니다. 큰 설계 변경 시 새 버전을 기록하고 README의 현재 문서 링크를 갱신합니다.
