# LLM Agent Memory

LLM/Agent Harness에 종속되지 않는 **개인 소유 장기 에이전트 메모리 시스템**을 실험·개발하는 프로젝트입니다. GitHub Copilot Agent(VS Code)를 첫 클라이언트로 삼고, 여러 PC·세션·다른 에이전트와 같은 원격 기억을 활용하는 것을 목표로 합니다.

> Phase 0의 현재 계약은 **인스턴스 하나에 장기 기억 집합 하나**입니다. 여러 PC와 에이전트가 기기별 credential로 같은 기억을 공유합니다. 새 설치에서 Migration이 빈 `reference/1` 활성 Generation을 준비합니다. [검증 결과](docs/implementation/phase0-progress.md#10-단일-기억-공간-전환)

```python
memory_submit(text="배포 DB는 PostgreSQL을 사용한다.", idempotency_key="deploy-db-1", intent="explicit_memory")
memory_search(query="배포 DB")
```

REST는 `/v2/*`를 사용합니다. MCP는 `/mcp`에서 도구 목록을 다시 불러오고, 기존 클라이언트 인자와 지침에서 `namespace_id`를 제거해야 합니다. 기존 PostgreSQL을 사용한다면 먼저 [이전 조건과 절차](docs/operations/deployment.md#10-기존-db-이전)를 확인하세요.

## 먼저 읽을 문서

1. **[Architecture v0.1](docs/architecture/v0.1.md)** — 시스템 목적, MCP/REST·서비스·원본/기억 분리와 장기 아키텍처 원칙
2. **[MVP Contracts v0.1](docs/design/mvp-contracts-v0.1.md)** — 경험·Job·Memory·Generation의 개념적 책임·상태와 불변조건
3. **[Phase 0 Detailed Design v0.1](docs/design/phase0-detailed-design-v0.1.md)** — **Phase 0 구현 기준선.** Reference Engine, PostgreSQL 스키마·REST/MCP 계약, Worker 동시성, 삭제, 세대 전환, 테스트 및 구현 순서

**[Phase 0 구현 트래커 — Issue #6](https://github.com/YB-Park/LLM-Agent-Memory/issues/6)**에서 P0-0~P0-5 구현과 검증을 추적합니다. 현재 세부 구현에서는 3번 문서를 우선 참조하되, 상위 설계 원칙을 위반해서는 안 됩니다.

## 사내망 이전 전: 장기 설계·연구 배경 보존

이 프로젝트는 사내망의 **외부 코드/데이터 반출 금지 환경**으로 이전될 수 있다. 외부 GitHub·기존 대화·논문 사이트에 접속하지 못해도 배경을 이해할 수 있도록 Phase 0 구현 문서와 별도로 다음 맥락을 기록했다.

- **[프로젝트 배경과 설계 방향](docs/context/project-background.md)** — 왜 만들었는지, 중요하게 구분한 개념, 합의·보류·미결정 사항
- **[Hindsight · MemoryLACE 연구 인사이트](docs/research/hindsight-memorylace-insights.md)** — 연구 내용과 우리가 얻은 통찰을 구분. 특정 엔진 채택 결정이 아님
- **[향후 연구 질문과 평가 관점](docs/research/future-experiments-and-questions.md)** — 추출·통합·검색·시간·망각·재활성화·평가의 열린 질문; 고정 로드맵이 아님
- **[Phase 1 스터디 보충 메모](docs/research/phase1-study-supplement.md)** — 논문·GitHub 구현과 기존 논의 사이의 판단 근거, 조용한 정보 손실·증거 사슬·시간·화자·엔진 교체 문제; 확정 구현 지시 아님
- **[Phase 1 이론 설계 제안 — Hindsight 중심 + MemoryLACE 국소 생명주기](docs/research/phase1-hindsight-lace-theoretical-proposal.md)** — Retain/Recall/Reflect와 네 인식론적 기억 영역을 기본으로, 관계 갱신·Observation·검색 증거 확장의 결합 가능성, 충돌 지점, 판별 실험을 제안한 작업 가설
- **[격리망 이전 핸드오프](docs/context/offline-handoff.md)** — 이전 전 테스트·미검증의 경계, 승인된 반입·오프라인 재현·보안 주의사항

**격리 이후의 실제 코드·설정·로그·조직 데이터는 GitHub나 공개 LLM/외부 서비스로 반출하지 않는다.** 이후 내부에서 달라진 결정과 시험 결과는 사내에서 승인된 저장소·문서가 최신 기준이다.

## Phase 0의 경계

- **기본 구조:** 개인용 원격 MCP + REST Adapter, Modular Monolith, Background Worker, PostgreSQL. 작은 UTF-8 텍스트 원본·접수/Job을 같은 PostgreSQL 트랜잭션에 기록.
- **Reference Memory Engine:** `reference/1`과 `reference/2`. 일반 Experience는 원본 전체/문단 투영, Explicit Memory는 제출된 문구 그대로 보존. 키워드/제목/힌트 기반의 제한적 검색으로 새 세션 회상을 시험.
- **데이터·운영:** 기기별 인증과 Principal 권한, 출처·버전, Job attempt/lease, 삭제 후 재노출 차단, 격리된 세대 비교·수동 전환. Phase 0 Rebuild는 서버 전체의 신규 접수를 일시 중지하며 **신규 입력 후 단순 포인터 롤백을 허용하지 않음**.
- **범위 밖:** 실제 강한 근거 연결(`evidence_refs`), 자동 기억 추출·통합·모순 해결, 연상 그래프, 중요도/기억 강도/접근성·망각, 임베딩/LLM 메모리 알고리즘, 무중단 전환, 범용 플러그인 프레임워크.
- **수동 정정:** 잘못된 명시적 기억은 관리자 CLI로 해당 Experience 삭제 후 수정 문구를 새 키로 제출. 독립 Experience들 사이의 근거 의존성을 추측해 연쇄 삭제하지 않음.

개인·업무처럼 서로 다른 신뢰 또는 보안 경계에는 별도의 서버 인스턴스를 사용합니다. 같은 인스턴스의 읽기 권한은 보존 중인 전체 기억 집합에 적용됩니다. 프로젝트·주제·사건의 분류와 기억 간 연결은 Memory Engine의 연구 대상입니다. 서버는 원본과 출처, 처리 이력, 권한, 데이터 수명주기를 관리합니다.

## 개발·검증 원칙

**설계 기준선은 변경 가능**합니다. 상세한 DB/API 값과 기술 스택은 구현·시험 결과를 바탕으로 필요하면 수정하고 이유를 기록합니다. Hindsight·MemoryLACE 등은 참고 연구일 뿐 채택이 확정되지 않았습니다. Phase 1은 Phase 0 Reference Engine의 실제 사용 결과를 바탕으로 계획합니다.

실제 개인·업무 데이터를 투입하기 전에는 **원격 인증/TLS, 기기별 철회, 삭제·보존 범위, 백업/격리 복구**를 검증해야 합니다. 그 전까지는 합성 테스트 데이터로 개발합니다. Ubuntu 20.04는 호환성 **희망 사항**이며 공식 지원·실제 호스트 검증 완료를 뜻하지 않습니다. 현재는 `ubuntu:20.04` 컨테이너(사용자 공간)에서의 시험만 있습니다.

상위 설계는 `docs/architecture/`, 세부/버전별 설계는 `docs/design/`에 둡니다. 큰 설계 변경 시 새 버전을 기록하고 README의 현재 문서 링크를 갱신합니다.
