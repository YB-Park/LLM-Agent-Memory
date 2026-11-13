# LLM Agent Memory

> 설계 상태: **Architecture Draft v0.1** · 2026-11-13  
> 이 저장소는 현재 **설계 단계**다. 구현 코드·API 계약·DB 스키마는 아직 확정하지 않았다.

## 1. 비전과 목표

LLM 모델 및 Agent Harness에 종속되지 않는 **개인 소유의 범용 장기 메모리 시스템**을 탐구하고 구축한다.

- GitHub Copilot Agent(VS Code)를 우선 대상으로 하되, Claude Code·Codex 등 **다른 에이전트와 여러 PC/세션**에서도 동일한 원격 메모리를 활용한다.
- 대화·문서·개발 작업 등에서 경험(Experience)을 수집하고, 이를 재사용 가능한 기억(Memory)으로 변환한다.
- 장기적으로 기억의 **연관성, 중요도, 유지 강도, 접근성, 통합, 재구성, 망각·재활성화**를 연구하고 개선한다.
- 처음부터 인간의 기억을 재현하려 하지 않는다. **작은 시스템을 실제 사용하며 측정하고 개선**하는 개발 방식을 따른다.

## 2. 가장 중요한 설계 원칙

1. **Experience ≠ Memory**: 원본 경험과 엔진이 생성한 파생 기억을 분리한다. 기억 표현이나 알고리즘이 바뀌어도 원본이 허용하는 범위 내에서 재처리할 수 있어야 한다.
2. **Engine Independence**: Hindsight, MemoryLACE 또는 자체 메모리 엔진을 실험·교체할 수 있도록 애플리케이션과 엔진의 책임을 분리한다. 초기부터 과도한 범용 플러그인 프레임워크를 구축하지 않는다.
3. **Protocol Independence**: MCP는 에이전트 접근 인터페이스이지 시스템의 핵심이 아니다. HTTP API 등 다른 소비자도 같은 애플리케이션 서비스를 사용한다.
4. **Provenance / Versioning / Rebuildability**: 기억마다 출처와 처리 버전을 추적하고, 재처리·비교·점진적 전환을 가능하게 한다.
5. **Observability**: 어떤 기억이 생성·검색·수정되었는지, 응답에 어떤 근거가 사용되었는지, 비용·지연시간·정확도를 관찰한다.
6. **Security & Deletion**: 개인·업무·프로젝트 범위의 접근 제어를 처음부터 고려한다. 보존 및 삭제 정책을 구분하고, 원본을 영구 보존한다고 가정하지 않는다.

## 3. 외부 시스템 아키텍처 초안

```text
[VS Code Copilot]  [Claude Code]  [Other Agents / PCs]
         \             |              /
          MCP (remote Streamable HTTP)
                      |
       [Access Layer: MCP + REST + Auth]
                      |
       [Application: Modular Monolith]
       ├─ Experience / Source Management
       ├─ Memory Engine Boundary (구현 미정)
       ├─ Retrieval Orchestration
       └─ Jobs / Audit / Evaluation
                      |
          +-----------+------------+
          |                        |
     [PostgreSQL]          [Source Storage]
     metadata / state      raw conversations,
     relations / logs      documents / files
     pgvector(optional)
          |
     [Background Worker]
     extraction / indexing / reprocessing /
     consolidation / evaluation
```

**초기 배포는 단순하게 유지한다.** 애플리케이션과 Worker는 같은 코드베이스의 별도 프로세스로 실행 가능하게 구성하고, PostgreSQL을 주 저장소로 사용한다. 초기에는 Docker Compose 기반의 단일 원격 서버/VM을 가정한다. 이는 물리적 서비스 분리가 아닌 **논리적 책임 구분**이다.

### 잠정 기술 선택 (확정 아님)

| 영역 | 초기 기준안 | 참고 |
| --- | --- | --- |
| 서버 구조 | Modular Monolith | 필요할 때만 서비스 분리 |
| Agent 연결 | Remote MCP, Streamable HTTP | MCP는 얇은 어댑터 |
| 기타 접근 | REST API | 동일 Application Service 재사용 |
| 메타데이터·관계·작업 상태 | PostgreSQL | 저장소 엔진 교체 여지 보존 |
| 의미 검색 | pgvector (옵션) | 초기에는 전문 검색과 병행 검토 |
| 원본 데이터 | 로컬 파일시스템 → S3 호환 스토리지 검토 | 출처, 보존·삭제 정책 포함 |
| 비동기 처리 | PostgreSQL 기반 Job Queue + Worker | 재시도·멱등성 필수 |
| 운영·배포 | Docker Compose | 초기 개인용 원격 환경 |
| 인증 | 접근 제한 및 개인 인증부터 시작 | 향후 OAuth/다중 사용자 확장 가능 |
| 언어·프레임워크·ORM·LLM | **미정** | 구현 직전에 결정 |

## 4. 책임과 핵심 데이터 영역

- **Source / Experience**: 어떤 에이전트·세션·PC·문서에서 어떤 정보가 들어왔는지. 원본 또는 원본 위치, 수집 시점, 권한, 보존 상태를 관리한다.
- **Memory Artifact**: 메모리 엔진이 생성한 결과물. Narrative/Atomic/Observation/Opinion 등 특정 표현을 공통 데이터 모델로 **성급하게 고정하지 않는다**.
- **Memory Relations / Indexes**: 엔진별 연결·검색 구조. 원본 경험과 구분하여 재생성 가능한 파생 데이터로 취급한다.
- **Processing Run / Jobs**: 엔진·모델·프롬프트·설정 버전, 처리 상태, 실패·재시도와 산출물의 연결을 추적한다.
- **Retrieval / Evaluation Logs**: 질의, 선택된 기억, 근거, 지연시간, 비용, 피드백을 기록한다. 민감정보의 로깅 범위를 제한한다.
- **Namespace / Access Scope**: global, project, personal/work 등 논리적 범위와 권한을 관리한다. 서로 다른 범위의 기억이 자동으로 노출되거나 연결되지 않도록 한다.

## 5. 우선 설계할 데이터 흐름

### Write (수집·기억 형성)

1. 클라이언트/수집기가 새 Experience를 제출한다. **MCP 연결만으로 모든 대화가 자동 수집되는 것은 아니다.**
2. API는 인증·권한·중복 여부를 확인하고 원본과 메타데이터를 기록한 뒤 Job을 등록한다.
3. Worker가 선택한 Memory Engine으로 처리하고 기억/관계/인덱스를 생성한다.
4. 결과를 버전·출처와 연결하고 검색 가능 상태로 전환한다.

### Read (기억 사용)

1. 에이전트가 MCP 또는 API로 현재 작업의 질의와 접근 범위를 전달한다.
2. 권한 범위 안에서 Retrieval Engine이 기억을 선택한다.
3. 제한된 토큰 예산 내에 기억·근거·유효성 메타데이터를 반환한다.
4. 에이전트가 실제 작업에 사용한다. 어떤 기억이 쓰였는지 평가 가능하도록 기록한다.

### Rebuild (엔진 전환·재처리)

1. 원본 Experience와 Processing Run을 기준으로 **새 엔진/버전**을 별도 실행한다.
2. 기존 기억을 덮어쓰지 않고 새로운 파생 결과를 생성한다.
3. 동일한 질의·시나리오로 품질, 검색 결과, 비용, 지연시간을 비교한다.
4. 평가 후 점진적으로 전환하거나 롤백한다.

## 6. 아직 결정하지 않은 것

- 기억의 기본 단위(Narrative vs Atomic), 그래프 구조, 엔터티/관계 갱신 규칙
- Hindsight·MemoryLACE 도입 여부 또는 자체 엔진
- 중요도·유지 강도·접근성, 연결 가중치, 망각·재활성화 알고리즘
- LLM/임베딩 모델 선택, 정밀한 검색·토큰 예산 정책
- Copilot/Claude 등 각 Harness에서 자동 수집이 실제 가능한 범위
- 상세 DB 스키마, 인증 구현, 백업·복구 정책, 배포 환경 상세

**주의:** Hindsight나 MemoryLACE는 참고 연구이지 채택이 결정된 기술이 아니다. 특히 망각은 향후 핵심 연구 주제이지만 v0.1 구현의 필수 기능이 아니다.

## 7. 다음 LLM/개발자를 위한 핸드오프

지금은 **구현을 시작하라는 문서가 아니라 아키텍처 논의를 이어가기 위한 기준안**이다. 우선 Write / Read / Rebuild의 책임 경계와 최소 계약을 설계하고, 그 다음 경험 저장·검색을 확인할 수 있는 작은 MVP를 계획한다. 문서에서 **확정된 원칙과 잠정 선택을 혼동하지 말 것**. 엔진 고유 데이터 구조를 공용 API/저장 스키마에 무리하게 끼워 넣지 말 것.

초기 MVP의 성공 기준은 여러 세션·에이전트에서 동일한 메모리를 조회하고, 출처를 추적하며, 원본이 남아 있는 경우 엔진을 재처리·비교할 수 있는 것이다. 처음부터 모든 종류의 기억 형성, 완성형 그래프, 인간형 망각을 구현할 필요는 없다.

### 참고 연구

- [Hindsight — Retain, Recall, Reflect](https://arxiv.org/abs/2512.12818)
- [MemoryLACE — Lifecycle-aware consolidation](https://arxiv.org/abs/2609.03201)
- [Graphiti — Temporal knowledge graph](https://github.com/getzep/graphiti)
- [Letta / MemGPT](https://github.com/letta-ai/letta)
