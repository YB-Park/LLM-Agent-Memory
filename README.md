# LLM Agent Memory

LLM 모델이나 Agent Harness에 종속되지 않는 **개인용 장기 에이전트 메모리 시스템**을 탐구하고 개발하는 프로젝트입니다.

여러 PC와 세션, GitHub Copilot Agent·Claude Code 등 다양한 에이전트가 동일한 원격 메모리를 활용하는 것을 목표로 합니다. 원본 경험(Experience)과 파생 기억(Memory)을 분리하고, 실제 사용과 평가를 통해 기억 엔진을 점진적으로 개선하려고 합니다.

> **현재 상태:** 설계 단계. 아직 구현 코드와 상세 API·DB 스키마는 확정되지 않았습니다.

## 설계 문서

- **[Architecture v0.1 — 상위 아키텍처 초안](docs/architecture/v0.1.md)**: 시스템 경계, MCP/REST, 서버·DB·Worker 기준안, 불변조건과 미결정 사항
- **[MVP Contracts v0.1 — 구체화 설계 초안](docs/design/mvp-contracts-v0.1.md)**: 수직 경로, 원본·Job·Memory·Generation 상태, Write/Read/Worker/Rebuild 계약 후보 및 검증 시나리오

상위 아키텍처는 `docs/architecture/`, 세부 설계 초안은 `docs/design/`에서 관리합니다. 각 문서는 변경 가능하며 큰 설계 변경이 발생하면 새 버전 문서를 추가하고 이 README의 링크를 갱신합니다. 이전 버전은 기록으로 남깁니다.

## 프로젝트 진행 방향

작은 MVP를 구현해 실제로 사용하면서 필요에 따라 메모리 저장·검색·통합·망각 방식을 실험하고 보강할 계획입니다. Hindsight와 MemoryLACE 등은 참고 연구이며 채택이 확정된 구현은 아닙니다.

구현을 시작하기 전에는 위 설계 초안을 먼저 읽어주세요.
