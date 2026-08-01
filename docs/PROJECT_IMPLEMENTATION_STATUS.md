# Personal Hedge Fund Agent 실행 현황과 통합 계획

> 문서 상태: Confirmed Execution and Coordination Plan v2.0
> 감사 기준일: 2026-08-01
> 감사 기준: 로컬 `main`과 `origin/main`의 `3cab251`, 실행 중인 Docker, 실제 DB와 재실행한 Test
> 목적: 팀원별 진척도, 애로사항, 선행 의존성과 다음 실행 순서를 한곳에서 관리한다.
> 완료 조건 기준: [Core Feature Backlog](02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md)

## 1. 지금 프로젝트는 어디까지 왔는가

이 프로젝트는 문서나 빈 Scaffold 단계가 아니다. 리서치 데이터 수집은 실제 Docker 환경에서 동작하고,
리서치·퀀트·트레이딩·리스크·회계·QA의 결정론적 모듈도 상당 부분 구현됐다. 그러나 각 본부의 산출물이
공식 Event와 Canonical DB를 통해 다음 본부로 전달되는 하나의 서비스는 아직 완성되지 않았다.

현재 단계를 한 문장으로 정리하면 다음과 같다.

> **리서치 Data Plane과 본부별 Prototype은 실행되지만, 한 Investment Case를 전 본부가 이어받아
> Paper Fill과 PnL까지 처리하는 통합 Runtime은 아직 없다.**

다음 공동 목표는 새 기능 수를 늘리는 것이 아니다.

```text
실제 Research Packet
  -> 구조화된 OrderIntent
  -> Risk Decision
  -> Paper Order와 Fill
  -> Journal, Position과 PnL
  -> QA Decision과 Audit Trace
  -> AI Office 공식 Snapshot
```

이 한 건을 같은 `case_id`, `trace_id`, Versioned Contract와 Canonical DB Row로 재현해야 한다.

## 2. 상태 판정 기준

문서마다 `완료`의 뜻이 달라지는 문제를 막기 위해 아래 다섯 단계만 사용한다.

| 상태 | 뜻 | 완료로 보지 않는 예 |
|---|---|---|
| `RUNTIME_VERIFIED` | Process가 실행 중이고 실제 API·DB 입출력을 확인함 | 외부 서비스 한 번 호출한 코드 |
| `TEST_VERIFIED` | 결정론적 Test 또는 자체 점검을 현재 Commit에서 재실행해 통과함 | 과거 실행 기록만 있는 모듈 |
| `IMPLEMENTED` | 코드와 계약은 있으나 다른 본부·DB·Container와 통합되지 않음 | In-memory Prototype |
| `DOCUMENTED` | 설계와 완료 조건만 확정됨 | 구현 파일이 없는 기능 |
| `BLOCKED` | 선행 결정, Credential, 데이터 또는 다른 본부 산출물이 없어 진행 불가 | 단순한 후순위 작업 |

본부 가이드의 체크박스는 **그 본부가 자기 산출물을 만들었는지**를 뜻한다. 전체 제품 완료는 이 문서와
Feature Backlog의 End-to-End 완료 조건으로만 판단한다.

## 3. 2026-08-01 실행 감사 결과

### 3.1 Git과 팀 작업

- 로컬 `main`과 `origin/main`은 감사 시작 시점에 동일한 `3cab251`이었다.
- 기준 문서 Commit `4402a58` 이후 재일님 35개, 동규님 13개, 도현님 4개, 영주님 4개 Commit이 반영됐다.
- 같은 기간 변경은 리서치·퀀트 구현, Risk·QA API/Test, CEO·HR Local Model Smoke, Portfolio API,
  Supabase Migration과 Compose 확장에 집중됐다.
- 추적하지 않는 `ai-office/ai-office-dev.log`는 개인 Runtime Log이므로 감사와 Commit 범위에서 제외했다.

### 3.2 실제 실행 중인 서비스

루트 `docker-compose.yml`의 7개 서비스가 실행 중이었다.

| Service | 감사 시 상태 | 판정 |
|---|---|---|
| `timescaledb` | 28시간 이상 실행, Health `healthy` | `RUNTIME_VERIFIED` |
| `ls-realtime` | 30시간 이상 실행, 주식 체결·호가 수신과 적재 Log 확인 | `RUNTIME_VERIFIED` |
| `news-watcher` | 22시간 이상 실행 | `RUNTIME_VERIFIED` |
| `ls-news` | 22시간 이상 실행 | `RUNTIME_VERIFIED` |
| `batch-collectors` | 9시간 이상 실행 | `RUNTIME_VERIFIED` |
| `market-api` | 8시간 이상 실행, `/health` 응답 확인 | `RUNTIME_VERIFIED` |
| `research-api` | 5시간 이상 실행, `/health`와 Evidence 조회 Log 확인 | `RUNTIME_VERIFIED` |

같은 PC의 `trading-*` Container는 별도 Trading Bot 프로젝트다. 이 프로젝트의 Trading·Risk·Accounting
Runtime으로 계산하지 않는다. Risk Redis Test에는 격리 DB 15를 잠시 사용했지만 제품 의존성으로 채택하지 않는다.

### 3.3 실제 적재 데이터

TimescaleDB의 7개 Hypertable과 실제 행 수를 확인했다.

| 데이터 | 행 수 | 해석 |
|---|---:|---|
| `market.market_ticks` | 2,396,036 | LS 체결 적재 동작 |
| `market.market_quotes` | 3,319,359 | LS 호가 적재 동작 |
| `market.market_bars` | 3,973,545 | 분·일봉 적재 동작 |
| `market.market_breadth` | 50 | 시장 폭 Snapshot 존재 |
| `market.data_quality_windows` | 30 | DQ Window 기록 존재 |
| `market.derivative_snapshots` | 0 | 수집 코드만 있고 실제 적재 증거 없음 |
| `market.microstructure_features` | 0 | API 계산면은 있으나 영속 Feature 적재 없음 |

Research API가 연결한 Supabase에서는 NAVER 14,829건, OpenDART 3,380건, LS 뉴스 4,509건,
Alpaca 뉴스 192건, 재무 Fact 25,480건, 거시 관측 776건을 확인했다.

Quant Schema에는 Dataset Manifest 1개, Universe 1개, Partition 31개, Hypothesis 3개,
완료 Experiment 4개, Backtest Run 3개, Trade 10,544개와 Metric 108개가 있었다. 따라서 Quant는 더 이상
Prompt-only 단계가 아니다. 반면 Canonical `execution`, `risk`, `accounting`의 Trade Case, Intent, Order,
Fill, Risk Decision, Journal, Position과 Portfolio Snapshot은 모두 0건이었다. 현재 통합 병목이 이 경계다.

Governance의 Mandate·Investment Case·Approval도 0건이고 Workforce는 Department 1개, Agent Profile과
Profile Version 각 5개가 등록돼 있다. Improvement Candidate와 Access Request는 0건이다.

### 3.4 재실행한 검증

| 검증 | 결과 | 주의사항 |
|---|---|---|
| Schema, Paper E2E, Risk, QA pytest | `111 passed` | Redis 격리 DB를 명시해 실행 |
| Research Department Pipeline | 8개 영역 통과 | LLM·API 없는 결정론적 자체 점검 |
| Risk Department Pipeline | 3개 영역 통과 | Redis·Hermes 없는 자체 점검 |
| QA Department Pipeline | 3개 영역 통과 | Ollama·Hermes 없는 자체 점검 |
| Quant Hypothesis/PIT/Backtest/Walk-Forward/Orchestrator | 26개 영역 통과 | DB 없는 합성 Fixture 자체 점검 |
| AI Office clean build | 성공 | Node 22 임시 Container에서 실행 |
| AI Office Server Render Test | 1/2 통과 | `Agent Workforce 인사팀` 기대값이 현재 UI의 `인사팀`을 따라오지 못함 |

전체 Repository를 아무 인자 없이 `pytest`로 수집하면 CEO와 HR의 같은 파일명
`scripts/test_ollama_agent.py`가 충돌해 Collection 단계에서 실패한다. 핵심 Test 자체는 통과하지만,
현재 상태로는 전사 CI 한 명령 실행이 불가능하다.

## 4. 본부별 진척도와 다음 인수인계

### 4.1 재일님: 리서치본부와 퀀트/백테스트본부

**확인된 진척도**

- LS 실시간 체결·호가, 뉴스와 Batch Collector를 Docker에서 상시 실행한다.
- TimescaleDB, `market-api`, `research-api`와 Point-in-Time Evidence 조회면이 동작한다.
- Research Packet Pipeline v2가 기술·펀더멘털·뉴스·레짐·미시구조 분석과 수치 Guard를 결합한다.
- Quant의 Hypothesis, PIT Dataset, Backtest, Walk-Forward와 Experiment Orchestrator가 구현됐고 실제
  Supabase에 Dataset·Experiment·Trade·Metric이 존재한다.
- 결정론적 Research Packet Markdown 산출물과 Strategy Candidate 연결 경로가 생겼다.

**애로사항과 남은 경계**

- `derivative_snapshots`와 영속 `microstructure_features`가 0건이라 파생·Feature Runtime 완료로 볼 수 없다.
- Research Packet이 Versioned Event와 Canonical Artifact로 Trading에 전달되지 않는다.
- 전 종목 실시간 Feature Engine, Event Priority Queue와 Redis Stream Producer가 없다.
- Quant Worker/API와 Strategy Registry 승격 Gate가 Compose에 없고 현재 실행은 개인 Script 중심이다.
- KRX 지수 구성 이력과 미래 거래일 Calendar의 승인 Source가 확정되지 않았다.

**다음 작업**

| ID | 작업 | 선행 입력 | 완료 증거 |
|---|---|---|---|
| `RQ-01` | `ResearchPacket v1` Artifact와 `research.packet.ready.v1` Contract 확정 | `PLAT-01` Event Envelope | 같은 Packet을 API·Event·DB에서 같은 ID로 조회 |
| `RQ-02` | 실시간 Feature/Event Engine 최소형 구현 | Market API와 Universe | 급변 Fixture 1건이 Priority Queue에 한 번만 생성 |
| `RQ-03` | Quant API·Worker Container와 Job Contract 구현 | `PLAT-02` Compose Core | Dataset→Experiment→Candidate Job 재시작 복구 |
| `RQ-04` | 파생 수집 첫 적재와 DQ 확인 | Scope 승인, 거래일 | `derivative_snapshots > 0`, Source·시각·품질 보고 |

### 4.2 도현님: 트레이딩본부, 회계/포트폴리오본부와 공통 Platform

**확인된 진척도**

- 구조화 OrderIntent, Risk Decision, OMS, Paper Broker, Multi-leg와 Derivatives Capability Gate가 있다.
- 이중분개 Ledger, Position/Cash Projection, Corporate Action, Reconciliation과 Daily Report가 있다.
- `/accounting/v1/portfolio-snapshot`은 공식 수치를 복제하지 않고 Snapshot Reference만 반환한다.
- `apps/api`와 AI Office가 DEMO Paper Loop를 Read-only Snapshot으로 표시한다.

**애로사항과 남은 경계**

- Trading·Accounting 전용 API/Worker Container가 없고 Canonical DB의 주문·체결·원장 행은 모두 0건이다.
- OMS 재시작 복구, 부분 체결, Broker Reconciliation과 Event Store 연결이 없다.
- BFF는 DEMO Loop를 매번 공식 상태처럼 투영할 수 있어 `PAPER` 운영 Read Model로 사용할 수 없다.
- AI Office clean build는 되지만 조직명 Test가 낡았고, Kanban Status Bridge와 WebSocket은 미구현이다.
- 전사 Event·Compose·Frontend를 연결할 Platform 작업이 한 사람에게 집중될 위험이 있다.

**다음 작업**

| ID | 작업 | 선행 입력 | 완료 증거 |
|---|---|---|---|
| `PLAT-01` | 공통 Event Envelope, Error, Idempotency와 Health Contract 코드화 | 전 본부 Review | Contract Test와 Version Registry 통과 |
| `PLAT-02` | 프로젝트 전용 Redis와 Compose Core 구성 | `PLAT-01` | 별도 `trading-*` 없이 Core Service가 기동 |
| `TRD-01` | Trading API·OMS Worker와 Supabase Repository 연결 | `RQ-01`, `RSK-01` | Risk 승인 없는 Submit 0건, 재시작 후 Order 복구 |
| `ACC-01` | Fill Consumer, Ledger Repository와 Portfolio Snapshot Projector | `TRD-01` | Fill 1건이 Balance Journal·Position·Snapshot 생성 |
| `UI-01` | Render Test 교정, 공식 Snapshot/WebSocket Client 구현 | `ACC-01`, `QA-01` | DEMO/PAPER 분리와 Sequence Gap 복구 E2E |

### 4.3 동규님: 리스크본부와 AI QA/감사본부

**확인된 진척도**

- Pre-trade Risk Engine, Redis Trading State와 Fail-closed 경로가 구현됐다.
- Risk API와 QA API가 있고 핵심 Test가 현재 환경에서 통과한다.
- Evidence QA, Trace, Tool Permission, Ops Health, Incident와 Corrective Action 모듈이 있다.
- QA Write-through Repository와 `audit.qa_decisions`의 `calculation_version`, `input_hash` Migration이 적용됐다.
- Risk·QA LangGraph Pipeline은 결정론적 판정을 LLM이 바꿀 수 없도록 분리한다.

**애로사항과 남은 경계**

- Risk·QA Container가 없어 다른 본부가 안정된 Service Endpoint로 호출할 수 없다.
- Canonical Risk Decision은 0건이며 Risk API가 판정을 Supabase와 Event Bus에 기록하지 않는다.
- QA의 Evidence Store와 Workforce Tool Allowlist는 실제 Source가 아니라 Stub이다.
- QA Pipeline은 팀 GPU 주소를 코드에 고정해 Model Gateway 원칙과 충돌한다.
- Case 단위 `qa-check`와 `qa.decision.v1`의 상위 Domain Contract 승인이 끝나지 않았다.

**다음 작업**

| ID | 작업 | 선행 입력 | 완료 증거 |
|---|---|---|---|
| `RSK-01` | Risk API Container, Decision Repository와 `risk.decision.v1` 발행 | `PLAT-01`, 프로젝트 Redis | Request 1건의 DB·Event·응답 Hash 일치 |
| `QA-01` | QA API Container, Trace와 QA Decision 영속화 | `PLAT-01` | Case의 Claim→Evidence→Decision→Finding 재현 |
| `QA-02` | 실제 Tool Allowlist와 Evidence API 연결 | `HR-02`, `RQ-01` | 미허용 Tool이 차단되고 Trace에 기록 |
| `QA-03` | Hard-coded Ollama Endpoint 제거와 Model Gateway 전환 | `MODEL-01` | 코드에 개인 IP 0건, Gateway Trace 존재 |

### 4.4 영주님: CEO Office와 Agent Workforce 인사팀

**확인된 진척도**

- Mandate 정책, Version, Effective Time과 승인 Lifecycle Domain이 있다.
- Improvement Candidate, 승인 분리 상태 머신, Cost Scorecard와 Access Lifecycle이 있다.
- Workforce·Access·Quality 관련 Migration이 실제 DB에 적용돼 있다.
- CEO와 HR의 고도화된 `Modelfile`, `agent-ceo`·`agent-hr` Smoke Script가 있다.

**애로사항과 남은 경계**

- Governance API와 Workforce API가 없고 Mandate·Investment Case·Approval 실제 행은 0건이다.
- Mandate Repository는 In-memory이며 사용자 승인 Interrupt/Resume가 없다.
- Improvement Candidate의 Eval→Shadow→승인→배포→Rollback Runner가 없다.
- CEO·HR Smoke Script는 육안 검사이며 자동 Eval이나 Model Digest를 남기지 않는다.
- AI Office에서 보여줄 공식 Roster, Queue, SLA와 Approval Inbox Read Model이 없다.

**다음 작업**

| ID | 작업 | 선행 입력 | 완료 증거 |
|---|---|---|---|
| `GOV-01` | Governance API와 Mandate PostgreSQL Repository | `PLAT-01` | Mandate Version 활성화·재승인 Test |
| `GOV-02` | Investment Case·Approval·Escalation 상태 머신 API | `GOV-01` | Risk/QA Block을 CEO가 우회하지 못함 |
| `HR-01` | Workforce API와 Candidate/Access Repository Runtime | `PLAT-02` | 후보 생성부터 독립 승인 전까지 DB Event 재현 |
| `HR-02` | Agent Profile·Tool Permission 공식 Read API | `HR-01` | QA와 Model Gateway가 같은 Version 조회 |
| `HR-03` | Eval·Shadow·Promotion·Rollback Orchestrator | `QA-01`, `MODEL-01` | 후보 1건의 승인형 자기 개선 폐쇄 루프 |

## 5. 공통 모순과 결정이 필요한 사항

| 우선순위 | 발견 사항 | 영향 | 결정 또는 수정 방향 |
|---|---|---|---|
| P0 | Compose에는 Research 계층만 있음 | 본부 간 HTTP/Event 통합 불가 | `PLAT-02`, `RSK-01`, `QA-01`부터 확장 |
| P0 | Canonical Execution/Risk/Accounting DB가 모두 0건 | 현재 Paper E2E는 In-memory DEMO | 첫 Case를 DB에 기록하는 Wave 2 수행 |
| P0 | 전체 pytest가 같은 파일명 때문에 수집 실패 | CI 신뢰 불가 | Smoke 파일명을 고유화하거나 `testpaths` 분리 |
| P0 | Canonical 문서는 `Agent Workforce 인사팀`, 실제 UI는 `인사팀`을 사용 | Merge Gate 실패 | `UI-01`에서 표시명을 결정한 뒤 UI·Test·문서를 동시에 통일 |
| P0 | Host `.venv`에 Python 실행 파일이 없고 Node도 없음 | 팀원별 재현성 저하 | Dev Container 또는 Bootstrap Script를 단일 기준으로 제공 |
| P0 | Hermes Config는 `nous/poolside`, Master Plan 목표는 Bedrock Claude | Model Routing 기준 불명확 | `MODEL-01` ADR로 개발·Paper·Production Routing 확정 |
| P0 | Ollama Guide의 Alias와 Base Model이 실제 Modelfile과 다름 | 잘못된 모델 Build | 실제 `agent-*`, Research/Quant `qwen3:14b`로 교정 |
| P0 | QA Script에 개인 GPU IP가 고정됨 | 재현성·보안·Failover 문제 | Model Gateway 환경변수 경계로 이동 |
| P1 | Derivative와 영속 Microstructure 행이 0건 | 선물·옵션 전략 검증 불가 | 실제 거래일 적재와 DQ 후 Capability 승격 |
| P1 | Kanban Bridge가 ADR만 있고 구현 없음 | AI Office Agent 상태는 Scripted | `UI-02`에서 Read-only Bridge·Projector 구현 |

`MODEL-01`은 최소 다음을 결정해야 한다.

- Hermes Supervisor의 개발, Paper와 Production Provider
- Ollama `agent-*` Alias의 허용 업무와 Fallback
- Bedrock Claude 사용 시 Region, Model ID, 비용 한도와 Timeout
- 팀 공용 GPU Endpoint를 Model Gateway 뒤로 숨기는 방식
- Model/Prompt/Tool/Eval Version을 Workforce Registry에 기록하는 계약

## 6. 본부 간 선행 의존성

| 생산자 | 산출물 | 소비자 | 현재 병목 |
|---|---|---|---|
| CEO | 활성 Mandate Version | Trading, Risk | API·DB Runtime 없음 |
| Research | `ResearchPacket v1` | Trading, QA | Artifact/Event Contract 없음 |
| Quant | Strategy Candidate | QA, Risk, CEO | 승격 API와 Registry Gate 없음 |
| Trading | OrderIntent | Risk | 서비스 호출과 영속화 없음 |
| Risk | `risk.decision.v1` | Trading, QA, CEO | DB/Event Adapter 없음 |
| Trading | Fill Event | Accounting, QA | OMS Worker·Event Bus 없음 |
| Accounting | Portfolio Snapshot Ref | CEO, Risk, UI | DB Snapshot 0건 |
| QA | `qa.decision.v1`, Finding | CEO, Workforce, UI | Case Contract 승인·Container 없음 |
| Workforce | Profile와 Tool Permission | Hermes, QA, Gateway | 공식 Read API 없음 |
| Hermes Kanban | 업무 진행 상태 | AI Office | Bridge·Projector 없음 |

후속 본부는 선행 산출물의 필드가 확정되기 전 임의 JSON을 만들지 않는다. 막힌 팀은 Stub을 만들더라도
Contract Fixture와 제거 조건을 함께 기록한다.

## 7. 실행 계획 v2.0

### Wave 0. 테스트와 상태 기준 고정

**목표:** 누구나 같은 Commit에서 같은 명령으로 상태를 재현한다.

- `CI-01`: 중복 Ollama Test Module 이름 문제를 해소한다.
- `CI-02`: AI Office 조직명 Render Test를 현행 UI와 맞춘다.
- `CI-03`: Python 111개 Test와 AI Office clean build/Test를 GitHub Actions에 추가한다.
- `CI-04`: Docker 기반 개발환경 또는 설치 Bootstrap을 제공한다.
- `CI-05`: 이 문서의 Commit, Test 수, Container와 DB 증거 갱신 양식을 PR Template에 추가한다.

완료 기준은 새 PC 또는 Clean Runner에서 Credential 없는 Unit Test가 한 명령으로 통과하고,
Credential이 필요한 Integration Test는 명시적으로 Skip 또는 별도 Job으로 분리되는 것이다.

### Wave 1. 공통 Runtime Backbone

**목표:** 각 본부가 같은 Event·Health·Idempotency 규칙으로 실행된다.

- `PLAT-01`: `contracts/events`에 Versioned Envelope과 Error Contract를 둔다.
- `PLAT-02`: 프로젝트 전용 Redis, Core Network와 Compose Fragment를 만든다.
- `PLAT-03`: Transactional Outbox, Relay와 Consumer Idempotency 최소 구현을 만든다.
- `RSK-01`, `QA-01`: 가장 먼저 Risk와 QA를 Container화한다.
- 모든 Service에 `/health/live`, `/health/ready`, `/metrics`를 제공한다.

완료 기준은 Research Event 한 건이 Redis Stream을 거쳐 Risk/QA Consumer에 한 번만 반영되고,
Consumer 재시작과 중복 Delivery에서도 결과가 바뀌지 않는 것이다.

### Wave 2. 결정론적 Paper Investment Case

**목표:** LLM과 UI 없이도 전 본부 핵심 통제가 연결된다.

1. `GOV-01`이 활성 Mandate Version을 제공한다.
2. `RQ-01`이 고정 Fixture Research Packet을 발행한다.
3. `TRD-01`이 OrderIntent를 만들고 `RSK-01`을 동기 호출한다.
4. 승인된 Intent만 Paper OMS가 주문·체결한다.
5. `ACC-01`이 Fill을 Journal, Position과 Portfolio Snapshot으로 투영한다.
6. `QA-01`이 Claim, Tool, Risk와 Fill Trace를 검사한다.
7. 한 `trace_id`로 모든 DB Row와 Event를 Replay한다.

완료 기준은 Canonical DB의 현재 0건 상태가 고정 Fixture 1건으로 바뀌고, 같은 Fixture 재실행 시
중복 Order·Fill·Journal이 생기지 않으며 Feed Stale 또는 Risk Reject 시 신규 진입이 차단되는 것이다.

### Wave 3. Hermes와 AI Office 실시간 연결

**목표:** 결정론적 폐쇄 루프 위에 Agent 조정과 사용자 경험을 올린다.

- `MODEL-01`: Provider와 Model Gateway ADR을 확정한다.
- `MODEL-02`: 8개 `agent-*` Alias Manifest, Digest와 Eval을 만든다.
- `HR-02`: Profile·Tool Permission을 Gateway와 QA에 제공한다.
- `UI-01`: 공식 Snapshot과 Domain Event WebSocket을 연결한다.
- `UI-02`: Hermes Kanban을 `agent.status.v1`로 변환하는 Read-only Bridge를 구현한다.
- CEO Approval Inbox에서 사람의 승인·거절·중단 명령만 Command API로 전송한다.

완료 기준은 AI Office를 새로 열어도 공식 Snapshot으로 상태를 복구하고, Agent 상태와 Risk·Order·NAV를
서로 다른 Source of Truth로 표시하며, Scripted Simulation을 `DEMO` 밖에서 사용하지 않는 것이다.

### Wave 4. Strategy Factory와 Paper Dry Run

**목표:** 실제 수집 데이터로 전략 후보를 만들고 통제된 방식으로 반복 배포한다.

- Quant Dataset→Hypothesis→Backtest→Walk-Forward 결과를 Strategy Registry에 연결한다.
- QA Eval, Risk Capability와 CEO 승인을 통과한 후보만 `SHADOW`로 승격한다.
- Shadow Scorecard를 통과한 후보만 `PAPER`로 승격한다.
- 파생상품은 실제 적재·Greeks·Margin·Multi-leg·Accounting Capability가 모두 검증된 뒤 별도 활성화한다.
- 10거래일 연속 Paper Dry Run, 장애·재시작·Rollback Drill을 수행한다.

완료 기준은 Strategy Candidate 한 건의 데이터, 코드, 비용, Eval, 승인, 배포, 성과와 Rollback을
같은 Version Lineage로 재현하는 것이다.

## 8. 팀 진행 공유 규칙

각 작업은 GitHub Issue 또는 PR에 아래 여섯 항목을 남긴다.

```text
Task ID:
Owner / Reviewer:
현재 상태: DOCUMENTED | IMPLEMENTED | TEST_VERIFIED | RUNTIME_VERIFIED | BLOCKED
이번에 확인한 증거: Commit, Test, API, DB Row 또는 Screenshot
막힌 이유와 필요한 입력:
다음 Handoff: 받는 본부, Contract Version, 완료 예정 조건
```

운영 규칙은 다음과 같다.

1. `완료` 보고에는 파일명이 아니라 실행 증거를 붙인다.
2. 다른 본부 입력이 필요하면 `BLOCKED`와 필요한 Contract·담당자를 같은 날 기록한다.
3. 임시 Stub, Hard-coded Endpoint와 DEMO 데이터에는 제거 Task ID를 붙인다.
4. Schema·Event·API 변경 PR은 생산자와 소비자 Owner가 함께 Review한다.
5. 본부 가이드의 체크박스와 이 문서가 다르면 이 문서를 먼저 갱신하고 원인을 교정한다.
6. 매 통합 PR 뒤 이 문서의 DB 0건, Test 수, Container 수와 열린 Blocker를 갱신한다.

## 9. 다음 회의에서 확정할 것

1. `PLAT-01` 공통 Contract와 `PLAT-02` Compose 작업의 실제 공동 작업자
2. `MODEL-01`의 Hermes 개발 Provider와 Production Bedrock 전환 기준
3. `ResearchPacket v1`, `risk.decision.v1`, `qa.decision.v1`의 최종 Reviewer
4. Wave 2 첫 Fixture 종목, Strategy Family와 Mandate 값
5. AI Office의 확정 조직명 `인사팀`과 외부 표시명
6. 프로젝트 전용 Redis를 추가하고 별도 Trading Bot Redis와 분리하는 일정

## 10. 현재 결론

리서치와 퀀트는 실제 데이터와 실험 단계까지 가장 앞서 있고, Risk·QA·Trading·Accounting은 핵심
결정론 코드가 준비됐다. CEO·Workforce는 통제 Domain과 DB 틀이 있다. 지금 필요한 것은 Agent 수를 더
늘리는 작업이 아니라 이 산출물을 **공통 Contract, Runtime, Canonical DB와 Trace로 연결하는 일**이다.

다음 통합 성공의 기준은 문서 수나 Commit 수가 아니다. AI Office에서 한 Paper Investment Case를 열었을 때
Research 근거부터 Risk Decision, Fill, Journal, QA와 PnL까지 실제 저장된 상태로 설명하고 Replay할 수 있어야 한다.
