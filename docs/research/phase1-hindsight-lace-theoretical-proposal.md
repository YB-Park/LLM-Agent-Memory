# Phase 1 이론 설계 제안 — Hindsight 중심 기억 + MemoryLACE 국소 생명주기

> **상태:** 이론적 Working Hypothesis · 2026-11-17 · 구현 명세/확정 ADR 아님.
>
> **제안:** **Hindsight의 World·Experience·Observation·Opinion과 Retain·Recall·Reflect를 개념적 골격으로 삼고**, **MemoryLACE의 국소적 merge·supersession·contradiction 및 관계 인식 증거 조회를 근거 기억의 생명주기 계층에 선택적으로 도입**해 보자. 두 연구의 모든 기능을 합하거나 처음부터 동일한 DB 구조로 구현하자는 뜻은 아니다.
>
> **자료의 성격:** [논문/공개 구현]은 연구 출처가 설명하는 동작, [우리 제안]은 프로젝트의 목적에 맞춘 새로운 해석, [미결정]은 실험이 필요한 선택이다. **외부에 존재하던 이 프로젝트 문서와 공개 연구만 이용했다. 사내 코드·실험·데이터에는 접근하지 않았으며 반출을 요구하지 않는다.** 실제 사내 구현 상태는 내부 저장소가 기준이다.

## 1. 기존 논의에서 이번 제안까지

지금까지 우리는 **어느 기억 엔진을 복제할지**보다 **경험의 원본과 기억의 파생, 출처와 검증, 시간과 변경, 저장과 회상, 권한과 삭제**를 분리하는 데 집중했다. Phase 0 Reference Engine을 유지한 것도 처음부터 복잡한 알고리즘을 공통 시스템 계약에 고정하지 않기 위해서였다.

이제는 Phase 1의 첫 연구 중심축으로 **Hindsight 스타일의 구조적·시간적·엔터티 인식 기억**을 제안할 만하다. 그 위에서 **MemoryLACE의 증거 간 국소 생명주기 관계와 relation-aware retrieval**을 접목하는 가설을 세운다.

- [Hindsight 원 논문] World, agent experience, synthesized observation, opinion이라는 네 인식론적 영역과 Retain/Recall/Reflect, TEMPR의 여러 검색 경로, CARA의 의견 진화가 핵심이다.
- [MemoryLACE 논문/공개 설명] 출처가 있는 atomic 자연어 기억과 희소한 merge/supersession/contradiction, 가까운 관련 후보와 이웃에 한정한 갱신, 현재/역사/상충 근거를 묶는 evidence retrieval이 핵심이다.
- [우리 제안] **기억의 종류/인식론적 역할**과 **기억 사이의 생명주기 관계**를 별개의 축으로 다룬다. 두 축은 상보적일 수 있지만 이득은 아직 실험 전이다.

이는 Hindsight의 세계관을 참고하되, MemoryLACE를 **일부 문제를 보완하는 메커니즘**으로 사용해 보려는 제안이다.

## 2. 이론적 골격: 서로 다른 네 가지 층위

프로젝트의 Phase 0 원본 Experience는 Hindsight의 agent experience network와 **같은 뜻이 아니다**. 서로 혼동하지 않는 것이 첫 번째 조건이다.

| 층위 | 의미 | 책임/주의 |
| --- | --- | --- |
| **Source Experience** | 사용자가 제출한 원문과 제출·출처 기록 | 서버가 원본, 인증, 보존/삭제, 추적성을 관리. 내용이 참임을 보증하지 않음 |
| **Evidence memory** | 원문으로부터 추출한 Hindsight식 World facts 또는 Agent Experiences | 보고된 주장·행동·시간·조건·이유를 서술하며 원문 근거로 돌아갈 수 있어야 함 |
| **Derived understanding** | Hindsight식 Observation 및 선택적 Opinion | 다수의 근거를 종합한 설명과 주관적 판단을 분리, 근거 변화에 따라 재평가 |
| **Retrieval / reasoning** | Recall 후보와 Evidence Unit, Reflect의 추론 | 무엇이 현재·과거·불확실한지 표시. 반환된 데이터는 지시 명령이 아님 |

우리 Phase 0에서 이미 존재하는 원본/파생/Generation 경계를 깨지 않고도 Phase 1의 의미적 모델을 실험할 수 있어야 한다. **월드 사실 = 외부 DB로 진위가 증명된 사실**도 아니다. 실제로는 특정 화자에게 귀속된 주장일 수 있다.

### 논리적 개념도

    불변 원본 Experience (서버 소유)
                   |
                Retain
                   |
        World facts / Agent experiences
             (Narrative 우선 가설)
                   |
       +-----------+----------------------+
       |                                  |
    Hindsight식 연결                   MemoryLACE식 연결
    entity / time /                    merge / supersession /
    semantic / causal                  contradiction / no action
       |                                  |
       +---------------+------------------+
                       |
              Observation (근거 있는 파생 요약)
              Opinion (별도 주관적 belief)
                       |
    Recall: 후보 탐색 -> lifecycle 증거 확장 -> 예산/근거 구성
                       |
        에이전트 응답 및 선택적 Reflect

위 구조는 물리적 그래프와 테이블, 모델 호출 순서를 뜻하지 않는다. **서로 다른 역할을 지닌 개념들을 어떤 계약으로 연결할지** 보여주는 그림이다.

## 3. Hindsight에서 중심으로 유지하고 싶은 이론

### 3.1 World와 Agent Experience의 구분

- **World:** 에이전트 바깥의 사람·시스템·사건·결정에 관한 주장/관측.
- **Agent Experience:** 특정 에이전트가 직접 했다고 보고한 행동이나 경험.
- **주의:** 한 사용자가 여러 에이전트를 쓰므로, 사용자 한 사람의 기억 공간에 있는 1인칭 문장도 **누가 말했는지** 알아야 한다. 에이전트의 "내가 수정했다"와 실제 Git 커밋은 다른 증거다.

### 3.2 Observation과 Opinion을 따로 둔다

- **Observation:** 여러 사실에 근거한, 가급적 선호/성향에 중립적인 요약. 원증거를 대체하는 절대 진실이 아니라 **다시 계산 가능한 파생 뷰**로 생각한다.
- **Opinion:** 에이전트의 추정·판단·선호로서 신뢰/형성 시점이 있을 수 있다. 객관적 사실과 같은 목록에 이름 없이 섞지 않는다.
- **Reflect:** 복잡한 질의에 대해 관련 증거로 추론한다. 결과가 단순 답변인지, 지속되는 Opinion 갱신인지 구분할 필요가 있다.

### 3.3 Retain·Recall·Reflect는 성격이 다르다

- **Retain:** 입력을 해석해 근거 기억을 만들고 연결한다.
- **Recall:** 여러 관련성 경로로 필요한 증거를 찾아 예산 안에서 제공한다.
- **Reflect:** 기억을 바탕으로 더 깊이 추론하고 경우에 따라 기존 의견을 수정할 수 있다.

**[우리 제안]** 검색이 곧 영속 의견 변경이 되거나, 요약이 곧 검증된 사실이 되는 설계를 피하자. Hindsight의 기능 분리는 유지할 가치가 있다. 다만 네 영역이 동일한 테이블/Enum이어야 한다거나 세 동작 모두 별도 외부 MCP 도구여야 한다는 뜻은 아니다.

## 4. MemoryLACE에서 우선 도입하고 싶은 부분

### 4.1 증거의 국소적 생명주기

[MemoryLACE 공개 설명] 새 메모리와 관련된 기존 후보를 찾고, 그 후보의 가까운 관계도 살펴본 뒤 **merge/update(=supersession)/contradict/no-action**을 판단한다. 작은 입력 묶음의 연결을 다시 살피는 **local batch evolution**도 설명한다.

**[우리 제안]** 이 관계 판단을 처음부터 모든 Observation과 Opinion에 강제하지 말고, **원본에 가까운 World/Agent Experience의 근거 기억**에 우선 적용할 수 있는지 검토한다.

- **Merge/Support:** 같은 진술의 중복 또는 지지. 서로 독립된 출처인지, 같은 원문의 복사인지를 구분해야 한다.
- **Supersession:** 같은 대상과 범위에서 새 근거가 과거 상태를 대체. 과거 사실을 물리적으로 지우지 않는다.
- **Contradiction:** 함께 참이라고 보기 어려운 주장이 존재. **발견과 해결은 다르다.**
- **No action:** 충분한 연결 근거가 없거나 서로 다른 대상/조건/시점이면 **관계를 맺지 않는다**는 선택도 필수다.

### 4.2 Active/Inactive는 접근 허가나 진위가 아니다

MemoryLACE의 active anchor 개념은 **현재형 질의의 우선 근거**를 찾는 힌트로 해석할 수 있다. 그러나:

- 과거형 질의에는 superseded된 기억이 필요하다.
- unresolved contradiction은 두 주장을 함께 보여줄 수 있어야 한다.
- inactive는 **정책상 삭제**를 뜻하지 않는다.
- 기기 credential 철회와 기억 내용의 중요도/현재성은 다른 문제다.
- 직접 회상되지 않는 오래된 기억도 관련 조건에 따라 다시 회상될 수 있다.

MemoryLACE의 이진 active/inactive를 우리 엔진의 모든 수명주기·신뢰·삭제 상태를 나타내는 유일한 분류로 채택해서는 안 된다.

## 5. 가장 중요한 설계 충돌: Narrative vs Atomic

Hindsight의 Narrative는 **무엇을 결정했고 왜 그랬는지, 조건과 예외가 무엇인지**를 함께 담기에 좋다. MemoryLACE의 국소 비교는 **같은 대상/속성/유효 기간에 관한 작은 주장 단위**가 있을 때 더 분명하다.

예를 들어:

> 과거 시제품에서는 DB A를 선택했다. 트랜잭션 보장과 팀의 운영 경험이 이유였고, 운영 환경까지 보장한 결정은 아니었다.

나중에 **운영 버전에서 DB B를 선택했다**는 경험이 들어오면 DB 선택이라는 일부 주장은 달라져도, 시제품 당시의 결정 이유는 여전히 역사적으로 유효하다.

| 표현 방식 | 장점 | 예상되는 위험 |
| --- | --- | --- |
| **A. Narrative 단위 그대로 lifecycle** | 구현·문맥 유지가 단순하고 Hindsight에 가까움 | 일부만 바뀌어도 Narrative 전체를 supersede하는 오판 |
| **B. Atomic을 전부 기본 기억 단위로** | 같은 사실/변경/모순 비교가 명확 | 이유·조건·화자·맥락의 소실, 다수의 별도 연결 관리 |
| **C. Narrative 유지 + 비교용 claim anchors** | 설명 맥락과 세밀한 lifecycle를 함께 실험 | 추출 비용, 같은 내용을 두 표현으로 유지하는 정합성 부담 |

**[우리의 약한 출발 선호: C]** Narrative를 원문 근거를 담는 설명 단위로 유지하고, lifecycle 판단이 필요한 경우에만 *비교 가능한 주장/속성/유효 기간*을 보조 단서로 식별해 보는 방향이다.

그러나 **claim을 모두 별도 DB에 저장하거나, 모든 기억을 강제로 잘게 자르라는 지시가 아니다.** C의 이득이 작거나 오류가 많으면 A/B가 우수할 수 있다. 세 접근을 **이유·부분 갱신·역사적 질의가 같은 데이터에서 발생하는 시험**으로 비교하자.

## 6. Hindsight 연결과 Lifecycle 관계를 같은 Edge로 취급하지 않는다

| Hindsight의 연결 계열 | 무엇을 의미하는가 |
| --- | --- |
| Entity | 같은 대상/개체에 대한 기억 |
| Semantic | 내용이나 의미의 유사성 |
| Temporal | 시간상 가깝거나 같은 기간에 대한 기억 |
| Causal | 원인이나 영향으로 보고된 관계 |

| MemoryLACE의 관계 계열 | 무엇을 의미하는가 |
| --- | --- |
| Merge | 실질적으로 반복·동일하거나 지지하는 진술 |
| Supersession | 특정 범위에서 새 상태가 이전 상태를 대체 |
| Contradiction | 서로 양립하지 않는 진술이 존재 |
| No action | 생명주기 관계를 연결할 이유가 충분하지 않음 |

같은 프로젝트나 같은 엔터티를 언급했다는 사실은 **변경/모순의 충분조건이 아니다**. 내용이 비슷하다는 것도 곧 중복 증거라는 뜻은 아니다.

**[우리 제안]** Association/탐색용 관계와 Lifecycle/상태 변화 관계를 **의미적으로 분리**한다. 물리 저장소가 하나인지 둘인지, graph DB를 사용할지는 여기서 정하지 않는다.

이 구분이 없다면 entity 연결이나 semantic similarity의 오탐이 잘못된 supersession으로 이어져 오히려 현재 기억을 망칠 수 있다.

## 7. Retain에 Local Lifecycle를 붙일 때의 개념적 과정

아래는 **이론적으로 분리 가능한 처리 기능**이지 고정 실행 순서나 Worker 명세가 아니다.

1. **원본 기록:** Experience의 출처·시점·화자·정책·원문 보존.
2. **Narrative 증거 형성:** 결정/사건/이유/조건을 문맥과 함께 추출.
3. **후보 수집:** 의미·엔터티·시간·기존 연결을 활용해 새 증거와 관련된 근거를 찾음.
4. **국소 관계 평가:** 후보와 그 인접 관계에서 merge/supersession/contradiction/no-action을 판별.
5. **영향 범위 확인:** 어떤 Observation, Opinion 후보, 인덱스 또는 근거 묶음이 영향을 받는지 파악.
6. **영향받는 파생 결과 재검토:** 필요 부분의 국소 갱신과 엔터티 전체/전역 재평가를 비교.

### Local 갱신의 장점과 약점

장점은 새로운 원본이 들어올 때 **전체 장기 기억을 매번 다시 평가하지 않을 가능성**이다. 하지만 후보 검색이 관련 옛 증거를 놓치면 **중요한 모순/대체 관계를 발견하지 못하고**, 이 실수가 누적될 수 있다.

따라서 연구할 질문은 "더 빠른가"뿐만 아니라:

- 후보 집합에 들어오지 않는 원거리 관련 기억의 비율은?
- 제한된 1-hop 이웃과 batch recheck가 관계 누락을 줄이는가?
- 국소 갱신의 상태가 넓은 범위 재평가와 얼마나 달라지는가?
- 나중에 잘못된 relation을 되돌리거나 재판정할 수 있는가?
- 전체를 다시 보지 않는 이득이 LLM 분류 비용보다 큰가?

**[미결정]** 후보 수, hop 깊이, similarity threshold, 주기, 모델, 처리 단계의 동기/비동기 여부.

## 8. Hindsight Observation과 MemoryLACE 현재성이 충돌할 수 있다

**[Hindsight 논문]** Observation은 관련 World/Agent Experience 사실을 모아 엔터티에 대한 중립적 파생 요약을 만든다.

그런데 MemoryLACE의 lifecycle를 추가하면 한 엔터티에 **과거에 참이었던 주장, 현재 상태, 반복 근거, 미해결 모순**이 함께 쌓인다.

- 오래된 근거를 모두 제외하면 당시의 이유/상태를 물을 수 없다.
- 오래된 근거를 최신 사실처럼 섞으면 잘못된 "현재 프로필"을 만든다.
- 모순되는 근거를 조건 없이 합치면 확신에 찬 허위 요약이 생긴다.
- 증분 Observation 갱신만 허용하면 누락된 근거가 영원히 반영되지 않을 수 있다.

### 비교할 세 가지 Observation 이론

**O1. 엔터티 관련 증거 전체 재생성**: 관련 사실을 전부 다시 검토. 비교 기준으로 유용하나 비용이 늘 수 있다.

**O2. 영향을 받은 Observation의 국소 수정**: 새로운 evidence와 lifecycle 근방을 중심으로 갱신. 계산을 절약할 수 있으나 누락 위험.

**O3. 현재 상태 요약과 역사/충돌 증거의 구분**: 요약에 시점/불확실성을 보존하고, 필요할 때 과거/반례로 확장.

**[잠정 조언]** O2의 비용 이득을 O1과 비교하고, O3의 설명력을 별도 평가하자. Observation은 **다른 출처보다 높은 권위의 사실이 아니라 재계산 가능한 파생물**이라는 관점이 중요하다.

## 9. Opinion/Reflect는 국소 관계와 무조건 같은 방식으로 업데이트하지 않는다

Hindsight의 Opinion은 주관적 belief와 confidence, Reflect는 추론과 선택적인 Opinion 진화를 포함할 수 있다. MemoryLACE가 어떤 증거를 Contradiction으로 연결했다고 해서 **특정 비율로 Opinion의 confidence를 감소시켜야 한다는 결론**은 나오지 않는다.

세 개의 변화가 구별돼야 한다.

1. **Evidence 관계의 변화:** 새 진술이 이전 증거를 보강/대체/반박하는가?
2. **Observation의 변화:** 중립적 요약이 여전히 근거에 부합하는가?
3. **Opinion의 변화:** 에이전트의 해석, 선호, confidence가 어떻게 바뀌어야 하는가?

**[잠정 조언]** Retain/Recall의 증거·시간·lifecycle 실험과, Reflect의 응답 생성 및 **영속 의견 쓰기**를 분리해 평가해 보는 편이 안전하다. 읽기/검색만 하려던 요청이 조용히 Opinion을 지속적으로 바꾼다면 재현성과 책임 추적이 어려워질 수 있다.

또한 한 사용자가 여러 Agent/PC에서 쓰는 시스템에서 **어느 에이전트의 성향/의견인가**도 검토해야 한다. Hindsight의 bank별 disposition을 우리 서버의 필수 사용자 프로필로 가져오지는 않는다.

## 10. Recall은 두 단계를 분리해 보는 것이 유망하다

**[Hindsight 논문]** Recall은 semantic·keyword·graph·temporal 후보를 통합하고 재순위화하여 토큰 예산 안에서 돌려준다.

**[MemoryLACE 공개 설명]** 현재의 active anchor를 찾고 lifecycle 관계로 과거·상충 증거를 확장해 **Evidence Unit**으로 묶는다.

**[우리 제안]** 검색을 개념상 다음과 같이 나눠 비교한다.

    질의
      -> 관련성 후보 탐색 (semantic / lexical / entity / temporal)
      -> 후보 결합 및 검색 의도 해석
      -> lifecycle 이웃 확장 (반복 / 과거 / 모순)
      -> 시점·출처·현재성 표시가 있는 증거 묶음
      -> 필요 증거와 토큰 예산의 조정
      -> Agent에 전달

이 구조의 요지는 **"무엇이 비슷한가"를 찾는 일과 "그 기억의 변화 이력 중 무엇을 함께 봐야 하나"를 구분**하는 것이다.

### 검토해야 할 설계 갈등

- **확장을 언제 할까?** 후보 융합 전에 확장하면 노이즈/비용이 증가할 수 있고, 상위 결과에서만 확장하면 중요한 과거를 놓칠 수 있다.
- **언제 historical anchor를 검색할까?** 일반 현재형 질문은 현재 근거가 우선이지만, "과거에 왜 A를 골랐지?"에는 과거 근거를 직접 찾을 필요가 있다.
- **Evidence Unit의 크기는?** 한두 문장인가, narrative와 과거/반례가 들어간 작은 묶음인가? 큰 단위는 설명력이 있지만 토큰을 더 쓴다.
- **모순을 어떻게 포장할까?** 더 최근의 단정을 내놓기보다 출처·시점·미해결 상태를 명시해야 할 수 있다.
- **비용과 검색 누락은?** 확장이 retrieval hit를 높이면서 precision을 얼마나 떨어뜨리는가?

**[잠정 조언]** **그냥 top-k 반환**과 **상위 후보의 lifecycle evidence expansion 반환**을 같은 질문으로 비교하자. 필요할 때만 확장하는 질의 적응형 전략도 가설로 남길 수 있다.

## 11. 시간은 관계 판단의 중요한 차원이다

Hindsight는 사건의 발생 시각과 언급/기록 시각을 구분한다. MemoryLACE는 새 정보가 이전 정보를 대체했는지에 관심을 둔다.

개발 경험에서는 다음이 각각 다르다.

- 사건이 실제로 발생했다고 보고된 시각
- 최초로 언급/기록된 시각
- 결정이나 상태가 유효했던 기간
- 서버에서 가공한 시각

**"2025년 시제품은 A, 2026년 운영은 B"**가 동시에 참일 수 있는 것과, 같은 범위·시점에서 A와 B를 주장하는 **미해결 모순**은 다르다. Project/Repo/Agent 같은 문맥도 이런 판단을 돕는 약한 신호이며, **Phase 0에서 제거한 Namespace를 다른 이름으로 재도입할 이유는 아니다**.

**[미결정]** 시간 모델, 필드 수, 시간 추론 방식. 원문에 없는 정확한 시간을 발명하거나 시점 없는 새 주장에 자동으로 최신성의 권위를 부여하지 않는 것이 더 중요한 원칙이다.

## 12. 삭제·근거 의존성·Generation과의 긴장

원본 Experience를 기반으로 파생 기억이 여러 번 요약·연결·재통합되면, **여러 근거를 참조하는 파생 Artifact**와 관계·캐시의 삭제 처리가 훨씬 어려워진다.

예를 들어 두 Experience를 요약한 Observation의 하나가 삭제됐다면:

- 남은 원본이 같은 요약 내용을 **독립적으로 충분히 뒷받침**하는가?
- 단순 Merge 관계가 원본 내용을 복제하여 남기지는 않았나?
- 과거 Generation이나 deep recall 경로에서 삭제된 근거를 다시 보여주지는 않나?

**[잠정 안전 원칙]** 어떤 파생 결과가 삭제된 출처의 내용을 포함하는지 안전하게 판정하기 어렵다면, **그 결과를 노출하지 않고 재검토/재계산**하는 방식이 기본 검토안이다. 그렇다고 모든 다중 근거를 한 번의 삭제로 영구 소거하라고 확정하는 것은 아니다.

중요한 불변조건은 **삭제/철회 정책이 엔진의 활성·비활성이나 관련성 점수보다 우선**한다는 것이다. 원본과 파생 결과, 모든 Generation, 결과 캐시와 관계 확장에 공통으로 적용돼야 한다.

LLM 기반 통합이 비결정적이라는 문제도 남는다. 같은 원본으로 재구축한 **현재 상태의 새로운 재계산**과 과거에 실제로 만들어졌던 **역사적 상태의 정확한 재현**은 다르다. 이를 위한 모델·프롬프트·엔진 버전 및 상태/이벤트 이력의 보존 수준은 실제 엔진에서 결정하자.

## 13. 두 연구가 부딪히는 지점 정리

| 접점 | 예상되는 불일치 | 이론적 조언 / 판별 실험 |
| --- | --- | --- |
| 단위 | Narrative는 이유·맥락, Atomic은 비교·변경 추적에 강점 | Narrative-only / Atomic-only / 보조 claim anchors 비교 |
| 관계 | Hindsight 연상 연결과 MemoryLACE 생명주기 링크의 의미가 다름 | 링크 **역할 분리**와 오탐 측정 |
| 현행 상태 | 새 정보가 생겨도 과거 이유는 남아야 함 | Active를 보존/삭제/진위와 분리, 시간형 질문 시험 |
| Observation | 엔터티 요약 갱신이 역사·모순을 잘못 합칠 수 있음 | 전체 재평가 / 국소 갱신 / 시간 인식 요약 비교 |
| Opinion | Evidence lifecycle 변화와 주관적 belief 강화가 다름 | Evidence/Observation/Opinion 업데이트 독립 평가 |
| Reflect | 응답/추론과 영속 상태 쓰기가 혼합될 수 있음 | 읽기 전용 추론과 명시적 의견 갱신 분리 |
| Search | 관련성 순위만 높아도 필수 반례가 누락될 수 있음 | lifecycle expansion의 질의별 이득·비용 비교 |
| Locality | 작은 후보 집합이 멀리 있는 모순을 놓침 | 국소 후보+1-hop vs 더 넓은 재평가의 차이 |
| Provenance | 다중 원본 합성 후 삭제가 복잡해짐 | 관계/Observation의 의존성·무효화 시나리오 평가 |
| Memory bank | Hindsight의 bank 분리는 제품 격리 개념 | 우리 단일 기억 공간을 의미별로 다시 나누지 않음 |

이 표의 권고는 **구현 지시가 아니라 설계 리뷰에서 다시 확인할 근거**다.

## 14. 실험으로 무엇을 결정하면 좋을까

### 14.1 비교할 개념적 모델

- **R — Reference:** 기존 Phase 0 단순 키워드/원문 투영.
- **H — Hindsight 중심:** Narrative·엔터티·시간·관련성 검색, 명시적 lifecycle relation이 없는 비교안.
- **HL — Hindsight + Local Lifecycle:** H에 국소 merge/supersession/contradiction 및 관계 확장을 추가하는 **이번 제안**.
- **HL-claim — 보조 주장 단위 결합:** HL에서 Narrative와 비교용 작은 명제를 함께 활용하는 변형.
- **L — Atomic 중심 비교(선택):** MemoryLACE의 표현 접근이 더 적합한지를 확인할 때 사용하는 선택적 비교안.

다섯 개 제품을 각각 구현하라는 뜻이 아니다. 같은 원본과 질의에 대해 **한 가지 가설이 무엇을 개선하는지** 비교할 수 있는 개념적 구성이다.

### 14.2 Ablation 후보

- Lifecycle expansion만 빼면 과거·변경·모순 질문의 정답/근거가 악화되는가?
- Local update vs 넓은 범위 재평가에서 속도·비용·관계 누락의 차이는?
- Narrative vs Narrative+claim에서 **일부 사실만 바뀐 경우**의 오탐이 감소하는가?
- Temporal 정보가 없을 때 단순 다른 시점의 상태를 모순으로 오인하는가?
- Observation 갱신 범위를 좁히면 오래된 요약이 더 많이 남는가?
- Opinion/Reflect의 상태 쓰기 없이도 기본 회상·변경 추적이 가능한가?
- 현행 상태를 먼저 찾는 경우 과거형 질문이 소외되는가?

### 14.3 합성 질문의 핵심 유형

| 원본 이력 | 질문 | 기대하는 행동 |
| --- | --- | --- |
| "시제품은 DB A, 운영은 DB B" | "지금은 무엇을 쓰지?" | 범위·시간에 맞는 현재 답 |
| 같은 이력 | "시제품에서 왜 A를 골랐지?" | Narrative 이유와 과거 근거 회복 |
| Agent가 "적용 완료" 주장, 별도 근거는 없음 | "실제 적용 확인됐어?" | 주장과 검증을 구분하고 필요시 답변 보류 |
| 같은 내용을 복사한 기록 + 독립된 확인 | "독립적인 근거가 몇 개인가?" | 중복과 반복 지지 증거 구분 |
| 동시각 상충하는 주장 두 개 | "어느 쪽이 사실이야?" | 무조건 최신 선택하지 않고 미해결 모순 표시 |
| 프로젝트 P의 장애 경험, Q의 유사 문제 | "이런 문제를 전에 겪었나?" | 교차 프로젝트 연상하되 다른 대상임을 표시 |
| 다중 근거 Observation 중 원본 하나 삭제 | "그 정보가 남아 있나?" | 어떤 retrieval/요약 경로도 삭제 데이터를 노출하지 않음 |

같은 질의 집합으로 **정답률뿐 아니라 근거 충실도, 시간 인식, 무관한 관계의 오탐, 보류의 적절성, 지연/LLM 호출 비용, 삭제 후 노출 방지**를 함께 살피는 것이 유용하다.

## 15. 이 제안이 틀렸다고 판정할 조건

- Local relation 분류가 잘못된 대체를 자주 만들고 **현재·과거 정보를 더 혼동**한다.
- Narrative+claim의 유지 비용이 커지지만 **부분 갱신/근거 보존 품질 이득이 작다**.
- 국소 갱신이 중요한 먼 관계를 지속해서 누락하고 복구도 잘 되지 않는다.
- Lifecycle 확장이 일반 질의에서 노이즈/토큰 비용만 높인다.
- Observation/Opinion의 의존성과 삭제/재처리 보장이 지나치게 복잡해진다.
- 실제 Agent가 MCP를 호출/사용하지 않는 문제가 지배적이어서, 엔진 고도화가 사용성 문제를 해결하지 못한다.
- 같은 조건에서 단순한 Hindsight 방식이나 Reference가 품질·비용 면에서 우세하다.

위 결과가 나오면 **Hindsight 중심 + 국소 lifecycle**이라는 가설을 수정·축소하거나 포기할 수 있다. 이는 실험의 성과이지 실패가 아니다.

## 16. 개발팀을 위한 요약 (로드맵 아님)

> **Hindsight를 기억 형성·검색·인식론적 구분의 이론적 중심축으로 삼는다.** World/Agent Experience의 서술 기억은 맥락과 출처를 유지하고, 새 증거가 들어올 때 MemoryLACE의 **국소적 반복·대체·모순 관계**를 추가하는 방안을 연구한다. Observation은 근거에 의존하는 파생 요약, Opinion은 별도의 주관적 판단으로 취급한다. Recall은 Hindsight식 후보 검색과 MemoryLACE식 변화 이력/반례 확장을 결합해 증거 묶음을 제공할 수 있다. **Narrative/Atomic의 관계, Observation 증분 갱신, 검색 확장 시점, Reflect의 상태 갱신**은 실험으로 정한다.

**하지 않을 전제**

- Hindsight 논문/현행 GitHub의 모든 기능을 구현할 의무 없음.
- MemoryLACE 원자적 저장 방식을 공통 데이터 모델로 강제하지 않음.
- 새로운 Namespace, Bank per project, 사용자별 파티션을 만들지 않음.
- 서비스의 삭제·원본·출처·인증·Generation 계약을 엔진 추론에 떠넘기지 않음.
- Graph DB, 임베딩·LLM 모델, 구체 Confidence 함수·망각 수식을 여기서 확정하지 않음.
- 현재 GitHub 구현을 원 논문의 정확한 재현판으로 가정하지 않음.
- 외부 저장소에 없는 내부 코드·데이터·실험을 추측하거나 반출하지 않음.

## 17. 참조 자료와 해석 주의

**프로젝트 기록**

- [프로젝트 배경](../context/project-background.md)
- [Hindsight/MemoryLACE 인사이트](hindsight-memorylace-insights.md)
- [Phase 1 스터디 보충 메모](phase1-study-supplement.md)
- [향후 연구 질문과 평가](future-experiments-and-questions.md)
- [Architecture v0.1](../architecture/v0.1.md) · [MVP Contracts](../design/mvp-contracts-v0.1.md)
- [Phase 0 구현 기록](../implementation/phase0-progress.md)

**공개 연구**

- [Hindsight 논문 arXiv 2512.12818v1](https://arxiv.org/html/2512.12818v1) — 네 논리적 네트워크, TEMPR/CARA, Retain/Recall/Reflect, 원 실험.
- [Hindsight GitHub](https://github.com/vectorize-io/hindsight) 및 [Retain 문서](https://github.com/vectorize-io/hindsight/blob/main/hindsight-docs/docs/developer/retain.md) — **2026-11-17 열람 시점**의 구현·설명. 논문과 달리 Mental Models/Pages/Decision Models 등 추가 개념이 있다.
- [MemoryLACE 논문 arXiv 2609.03201](https://arxiv.org/abs/2609.03201) 및 [alphaXiv 공개 상세 개요](https://www.alphaxiv.org/abs/2609.03201) — 희소 lifecycle relation, local batch evolution, evidence unit. **공개 개요는 2차 설명이므로 세부 알고리즘 채택 전 원문과 대조한다.**

**이 문서의 최종 위치:** 기존의 중립적 서베이와 열린 연구 질문 위에 얹은 **이론적으로 조금 더 선명한 초기 가설**이다. 이후 실험 근거가 달라지면 내부에서 얼마든지 수정할 수 있다.