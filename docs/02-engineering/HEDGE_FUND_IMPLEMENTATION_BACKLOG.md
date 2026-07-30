# Personal Hedge Fund Agent - Core Feature Backlog

> 문서 상태: Implementation Backlog v1.2  
> 최상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)  
> 범위: 단일 사용자, 한국 상장주식·ETF Multi-Strategy Paper Trading, Capability 기반 파생상품 확장  
> 관련 계획: [HEDGE_FUND_CORE_PLAN.md](../01-product/HEDGE_FUND_CORE_PLAN.md)  
> 확정 기술 스택: [TECH_STACK_DECISIONS.md](TECH_STACK_DECISIONS.md)  
> 목표: 전 종목 실시간 감시부터 전략 판단, Risk 검증, Paper 주문과 성과 평가까지 필요한 기능만 구현한다.

## 1. 우선순위

| 등급 | 의미 |
|---|---|
| P0 | End-to-End Paper Trading에 반드시 필요 |
| P1 | P0 안정화와 전략 품질에 필요 |
| P2 | Core 완료 후 확장 |

P0가 모두 동작하기 전 P2 기능을 구현하지 않는다.

## 2. P0 기능 목록

| ID | 기능 | 핵심 결과 |
|---|---|---|
| F01 | 사용자 Mandate | 자본과 Risk 한도 저장·검증 (ceo-agent) |
| F02 | Instrument Universe | 거래 가능 전 종목 유지 (research-department) |
| F03 | Market WebSocket | 실시간 시세 수신과 재연결 (research-department) |
| F04 | Event 정규화 | 공급자 Payload를 공통 Schema로 변환 (research-department) |
| F05 | Feature Engine | 전 종목 특징과 Snapshot 계산 (research-department) |
| F06 | Event Priority | Agent가 분석할 종목 선별 (research-department) |
| F07 | Point-in-Time RAG | 승인 문서를 판단 시점 기준으로 검색 (research-department, compliance 코퍼스는 risk-management) |
| F08 | Investment Agent | 근거 있는 구조화 판단 생성 (research-department) |
| F09 | Strategy/Capability Registry | 전략 상태, Version과 환경별 실행 가능 여부 관리 (quant-backtest-department) |
| F10 | Backtest | 전략 성과와 Risk Metric 계산 (quant-backtest-department) |
| F11 | Signal/Target Portfolio | 단일 Position, Pair와 Basket 목표·주문 의도 생성 (Signal은 quant-backtest-department, OrderIntent 변환은 trading-department) |
| F12 | Risk Engine | 주문 승인·축소·거절 (risk-management) |
| F13 | Paper Broker | 주문 접수와 모의 체결 (trading-department) |
| F14 | OMS | 주문 상태와 멱등성 관리 (trading-department) |
| F15 | Portfolio/PnL | Cash, Position과 성과 계산 (accounting-portfolio-department) |
| F16 | Audit/Replay | 판단부터 체결까지 추적·재현 (qa-department) |
| F17 | Operator Control | Entry Block, Pause와 Kill Switch (risk-management) |
| F18 | Dashboard | 시장·전략·주문·위험 상태 조회 (공통 — Frontend Framework 미정, 본부별 조회 API는 각 소유 본부) |

## 3. P0 기능 명세

### F01. 사용자 Mandate

**입력**

- Paper Capital, 허용 시장과 거래시간
- 종목·섹터 최대 비중
- 최대 동시 Position과 Gross Exposure
- 일일 최대 손실
- 자동 Paper 주문 또는 사용자 승인

**구현 기능**

- 값 범위와 상호 모순 검증
- 변경 전후 Version과 적용 시각 저장
- 장중 Risk 완화는 즉시 적용
- 장중 Risk 확대는 사용자 재승인

**완료 조건**

- 잘못된 한도 조합을 저장할 수 없다.
- Signal과 Risk Decision이 Mandate Version을 기록한다.

### F02. Instrument Universe

**구현 기능**

- 거래소/Vendor 종목 Master 수집
- 영구 `instrument_id` 부여
- Ticker, 시장, 통화, 섹터와 거래상태 저장
- 상장, 상장폐지, 거래정지와 Ticker 변경 반영
- 가격·유동성 기준 거래 가능 필터
- 장 시작 전 Universe Snapshot 고정

**완료 조건**

- Ticker 변경 후에도 동일 Instrument를 추적한다.
- 거래정지 종목은 Signal과 신규 주문에서 제외된다.

### F03. Market WebSocket

**구현 기능**

- Provider 인증과 전 종목 구독
- Heartbeat, 재연결과 구독 복구
- Rate Limit과 Session 제한 대응
- Sequence Gap, 중복과 지연 탐지
- 원본 Payload Append-only 저장
- 종목별 마지막 수신 시각 제공

**완료 조건**

- 연결 단절 후 자동 복구한다.
- 예상 Peak 2배 Replay 입력을 처리한다.
- Gap과 Stale 종목을 조회할 수 있다.

### F04. Market Event 정규화

**공통 Schema**

```text
event_id, provider, instrument_id, event_type,
event_time, received_at, sequence, price, size,
bid, ask, quality_flags, schema_version
```

**구현 기능**

- Provider Symbol을 `instrument_id`로 매핑
- Timestamp UTC 변환
- 가격, 수량, 통화와 Decimal 정규화
- 중복, 역전 Timestamp와 비정상 가격 Flag
- 변환 실패 Event Quarantine과 재처리

**완료 조건**

- Adapter가 바뀌어도 하위 Feature Schema는 유지된다.

### F05. 실시간 Feature Engine

**계산 기능**

- 1분·5분·20분 수익률
- 거래량·거래대금 Z-score
- 당일 고가·저가 거리
- 단기 변동성
- 시장·섹터 대비 상대강도
- Spread 또는 Quote Imbalance
- Feed Staleness

**출력**: Version이 있는 `FeatureSnapshot`

**완료 조건**

- 전 종목 Feature를 목표 지연 안에서 갱신한다.
- 동일 Replay에서 동일 Feature를 생성한다.
- 누락과 Stale 값을 정상값으로 처리하지 않는다.

### F06. Event 탐지와 Priority Queue

**탐지 기능**

- 가격·거래량 급변
- 상대강도 상위 진입
- 변동성 확대
- 보유 종목 손실 임계치 접근
- Feed Gap/Staleness

**우선순위 입력**

- 이벤트 강도, 보유·미체결 여부, 유동성
- 현재 Exposure, 최신성, Agent Queue 부하

**완료 조건**

- 동일 이벤트 반복 요청을 억제한다.
- Queue 포화 시 비보유 종목의 낮은 우선순위부터 폐기한다.
- 보유 종목 Risk Event를 최우선 처리한다.

### F07. Point-in-Time RAG

**Core Source**: 공시 또는 Licensed News 중 한 종류

**구현 기능**

- 원문, 게시시각, 수집시각과 Version 저장
- Parsing, Chunking과 Embedding
- Instrument, 문서 유형과 `available_at` Metadata
- 판단 시점 이전 문서만 검색
- Document/Chunk ID와 원문 위치 반환

**완료 조건**

- 미래 문서가 과거 Replay 검색에 나타나지 않는다.
- 모든 Agent Evidence를 원문까지 추적한다.

### F08. Investment Agent Workflow

**역할**

- Research Agent: 투자 가설, 근거와 반증 조건
- Bull/Bear Reviewer: 찬성·반대와 불확실성
- Portfolio Agent: Target Portfolio 또는 Pass

**입력**

- AnalysisRequest, FeatureSnapshot, Portfolio Snapshot
- RAG Evidence, Mandate Version

**출력**

```text
decision_id, scope_instrument_ids, strategy_family, directionality, thesis,
confidence, horizon, evidence_ids, invalidation,
target_portfolio[], expires_at, model_version
```

**완료 조건**

- Schema 실패는 재시도 후 `PASS` 처리한다.
- Evidence가 없거나 만료된 판단은 주문으로 전환되지 않는다.
- Agent는 Risk 한도와 주문 상태를 수정할 수 없다.
- Model, Prompt, 입력 Snapshot과 결과를 기록한다.

### F09. Strategy Registry

**상태**

```text
DRAFT -> BACKTESTED -> APPROVED -> SHADOW -> PAPER -> PAUSED/REJECTED
```

**저장 기능**

- Strategy ID와 Version
- Strategy Family, Directionality와 Holding Horizon
- Universe, Entry/Exit, Ranking, Position/Basket Sizing Rule
- Required Data Product, Instrument, Borrow, Margin, Multi-leg, Risk와 Accounting Capability
- Dataset/Feature Version
- Backtest 결과, 승인자와 배포시각
- 환경별 `RESEARCH_ONLY`, `SHADOW_ELIGIBLE`, `PAPER_ELIGIBLE`, `LIVE_ELIGIBLE` 상태와 차단 사유

**완료 조건**

- 승인되지 않은 Version은 Signal을 만들 수 없다.
- Capability가 부족한 환경에서는 승인된 Version도 주문을 만들 수 없다.
- 이전 Version으로 즉시 Rollback할 수 있다.
- Agent가 자기 전략을 직접 승인할 수 없다.

### F10. Backtest

**구현 기능**

- Point-in-Time Universe와 Feature 사용
- 거래비용, Slippage와 주문 지연 반영
- In-sample/Out-of-sample 분리
- Benchmark 비교와 Trade Log
- 수익률, Sharpe, MDD, Turnover, Gross/Net·Factor Exposure, Borrow/Financing Cost와 거래 수 계산
- Pair/Basket 관계 붕괴, Leg Slippage와 Capacity Stress 계산

**완료 조건**

- 동일 Dataset, Parameter와 Seed에서 결과가 재현된다.
- 미래 데이터 유입 Test를 통과한다.
- 결과가 Strategy Version에 연결된다.

### F11. Signal과 Target Portfolio

**구현 기능**

- Agent Decision과 승인 Strategy Rule 결합
- 현재 Portfolio 대비 Instrument별 목표 수량과 Long/Short 방향 계산
- Pair·Basket의 Leg 관계, 총 Gross/Net 목표와 Rebalance 이유 유지
- 최소 주문금액, Lot Size와 Signal 만료 적용
- 하나 이상의 `OrderIntent`와 공통 `intent_group_id` 생성

**완료 조건**

- 동일 Signal에서 중복 OrderIntent를 만들지 않는다.
- 한 Leg 실패 시 전략 정책에 따라 전체 취소, Hedge 또는 안전 축소로 전환한다.
- `PASS`와 만료된 판단은 주문을 만들지 않는다.

### F12. Risk Engine

**검사 기능**

- 시장 Session, Feed와 Position Freshness
- 종목 거래 가능 상태
- 종목·섹터·Gross Exposure
- Net·Factor·Strategy Book Exposure
- Short Availability, Borrow Fee와 Recall 상태
- Leverage, Margin과 Financing Limit
- 최대 동시 Position
- 주문금액, 수량과 가격 범위
- 일일 손실과 Drawdown
- 중복·미체결 주문

**출력**: `APPROVE`, `RESIZE`, `REJECT`와 Reason Code

**완료 조건**

- Risk Decision 없이 OMS 제출 상태로 이동할 수 없다.
- Stale Feed, 손실 초과와 Position 불일치 시 신규 진입을 차단한다.
- Agent와 Dashboard가 결과를 덮어쓸 수 없다.

### F13. Paper Broker

**구현 기능**

- Market/Limit 주문
- 부분 체결, 미체결, Cancel과 Reject
- Spread, Slippage와 Commission 모델
- Version이 있는 Paper Borrow Availability, Borrow Fee와 Short Recall Scenario
- 거래시간과 유동성 기반 체결

**완료 조건**

- 미래 가격을 사용하지 않는다.
- 동일 Request ID를 중복 체결하지 않는다.
- 체결 모델 Version을 기록한다.

### F14. OMS

**OrderIntent 상태 머신**

```text
DRAFT -> RISK_PENDING -> APPROVED | RESIZED | REJECTED | EXPIRED
APPROVED | RESIZED -> READY_TO_SUBMIT
```

**Broker Order 상태 머신**

```text
CREATED -> SUBMITTED -> ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED
CREATED | SUBMITTED | ACKNOWLEDGED | PARTIALLY_FILLED -> CANCEL_PENDING -> CANCELLED
SUBMITTED -> REJECTED
CREATED | ACKNOWLEDGED -> EXPIRED
BROKER_STATE_AMBIGUOUS -> UNKNOWN
```

`RISK_APPROVED`는 Order 상태가 아니라 유효한 `risk_decision_id` 제출 전제조건이다. `UNKNOWN` 상태에서는 신규 주문을 차단하고 Broker Reconciliation을 실행한다.

**구현 기능**

- Client Order ID와 Idempotency Key
- 주문 제출, 취소, Fill과 상태 전이
- Timeout, 상태 재조회와 재시작 복구

**완료 조건**

- 허용되지 않은 상태 전이를 거부한다.
- 재시작 후 미체결 주문을 복구한다.
- Intent, Risk, Order와 Fill을 연결한다.

### F15. Portfolio와 PnL

**구현 기능**

- Cash와 Position Ledger
- 평균단가, 실현·미실현 PnL
- 종목·섹터 Exposure
- Strategy별 성과 귀속
- 일일 Portfolio Snapshot

**완료 조건**

- Position 변화가 Fill 또는 Adjustment로 설명된다.
- Cash와 Position Value가 Portfolio NAV와 일치한다.
- Online과 Replay 결과가 일치한다.

### F16. Audit와 Replay

**추적 경로**

```text
Market Event -> Feature -> AnalysisRequest -> Agent Decision
-> Strategy Version -> OrderIntent -> Risk Decision
-> Order -> Fill -> Position -> PnL
```

**구현 기능**

- 공통 Trace/Correlation/Event ID
- Append-only Audit Event
- 날짜·종목·전략·주문별 조회
- 저장된 Event로 장중 Replay

**완료 조건**

- 주문 한 건의 데이터와 판단 근거를 역추적한다.
- Replay는 실제 Broker로 주문할 수 없다.

### F17. Operator Control

**거래 상태**

```text
NORMAL -> ENTRY_BLOCKED -> REDUCE_ONLY -> HALTED
```

**구현 기능**

- 신규 진입 차단과 축소만 허용
- 전체·개별 Strategy Pause
- 미체결 주문 전체 취소
- Kill Switch
- 수동 주문 승인·거절

**완료 조건**

- Kill Switch가 Agent와 Strategy보다 우선한다.
- 상태 상향은 사용자 승인을 요구한다.
- 모든 Command를 Audit에 기록한다.

### F18. 운영 Dashboard

| View | 표시 기능 |
|---|---|
| Market | 연결, 처리량, Gap, Stale 종목과 상위 Event |
| Portfolio | Cash, Position, Exposure, PnL과 Drawdown |
| Strategies | 상태, Version, Backtest와 Paper 성과 |
| Decisions | Thesis, Evidence, Confidence와 Risk 결과 |
| Orders | 상태, Fill, Reject와 Cancel Reason |
| Control | Trading State, Strategy Pause와 Kill Switch |

**완료 조건**

- 5초 이내 운영 상태를 갱신한다.
- Dashboard 장애가 Trading Process에 영향을 주지 않는다.
- 위험한 Command는 확인과 사유를 요구한다.

## 4. P1 기능

| ID | 기능 | 목적 |
|---|---|---|
| F19 | Shadow 비교 | 신규 전략을 주문 없이 평가 |
| F20 | Drift Monitor | Feature·Signal·성과 변화 탐지 |
| F21 | 자동 Strategy Pause | 손실·오류 초과 시 중단 |
| F22 | Daily Report | 전략별 PnL, Drawdown, 비용과 오류 |
| F23 | Notification | Feed, Risk, Order Incident 알림 |
| F24 | Corporate Action | Split, Dividend와 Symbol 변경 |
| F25 | Benchmark/Sector | 상대성과와 Exposure 개선 |
| F26 | LLM Budget | 호출량, 비용과 Degradation 관리 |
| F27 | Strategy Family Adapter | Event, Ranking, Pair, Basket, Futures와 Options 전략을 공통 계약에 연결 |
| F28 | Borrow/Short Simulator | 공매도 가능 수량, 비용, Recall과 주문 규칙 Simulation |
| F29 | Multi-leg Execution | Leg 관계, 부분 체결 복구와 Atomicity Policy |
| F30 | Derivatives Capability | Contract, Margin, Greeks, Roll과 Exercise/Assignment |

## 5. 최소 데이터 모델

```text
Mandate
Instrument / UniverseSnapshot
RawMarketEvent / NormalizedMarketEvent
FeatureSnapshot / AnalysisRequest
Document / DocumentChunk
AgentDecision
Strategy / StrategyVersion / StrategyCapabilityProfile / BacktestRun
Signal / TargetPortfolio / IntentGroup / OrderIntent / RiskDecision
Order / Fill
CashLedger / Position / PortfolioSnapshot
AuditEvent
```

모든 주요 Entity는 `id`, `version`, `created_at`을 가지며 거래 Entity에는 `trace_id`와 `strategy_version_id`를 포함한다.

## 6. 최소 API

```text
GET  /health
GET  /market/status
GET  /market/events
GET  /universe
GET  /mandates/current
POST /mandates
GET  /decisions
GET  /decisions/{id}
GET  /strategies
GET  /strategies/capabilities
POST /strategies
POST /strategies/{id}/backtests
POST /strategies/{id}/promote
POST /strategies/{id}/pause
GET  /portfolio
GET  /positions
GET  /orders
GET  /intent-groups/{id}
POST /orders/{id}/cancel
GET  /risk/status
POST /control/trading-state
POST /control/kill-switch
POST /replay
GET  /audit/{trace_id}
```

API는 Domain Service를 호출하며 Database Table에 직접 쓰지 않는다.

## 7. 핵심 Event

```text
market.raw.v1
market.normalized.v1
market.feature.v1
research.analysis_requested.v1
agent.decision_created.v1
strategy.candidate.v1
strategy.capability_evaluated.v1
strategy.version.approved.v1
strategy.signal.v1
trading.intent_group_created.v1
trading.order_intent.v1
risk.decision.v1
execution.order_event.v1
execution.fill.v1
portfolio.position_updated.v1
portfolio.snapshot.v1
risk.trading_state.v1
```

Event 이름은 `<domain>.<event>.v<major>` 규칙을 사용한다. 초기 Transport는 Redis Streams로 고정한다. Process Queue는 Unit Test와 단일 Process 개발 Profile에서만 사용하며 Event Contract는 Transport와 분리한다.

## 8. Sprint별 구현 순서

| Sprint | 구현 기능 | 산출물 |
|---|---|---|
| 1~2 | F01~F04, F16 기반 | 전 종목 수신·저장과 1일 Replay |
| 3 | F05, F06, Market View | 전 종목에서 분석 후보 생성 |
| 4 | F07, F08, Decision View | Evidence가 연결된 Agent Decision |
| 5 | F11~F17, Order/Portfolio View | Decision부터 Paper Fill과 PnL |
| 6~7 | F09, F10, F19, Strategy View | 대표 전략군과 Capability를 Backtest에서 Shadow/Paper로 승격 |
| 8 | F20~F23 일부, 장애·부하 Test | 10거래일 Paper Dry Run 시작 |

## 9. End-to-End Acceptance Scenario

1. WebSocket에서 전 종목 Event를 수신한다.
2. 가격·거래량 급변 종목을 탐지한다.
3. Priority Queue가 AnalysisRequest를 생성한다.
4. Agent가 Feature와 당시 이용 가능한 RAG Evidence로 Decision을 만든다.
5. 승인 Strategy가 단일 Position 또는 Long/Short Pair·Basket의 Target Portfolio와 OrderIntent를 만든다.
6. Risk Engine이 주문을 승인, 축소 또는 거절한다.
7. 승인 주문이 Paper Broker에서 체결된다.
8. OMS와 Portfolio가 Fill, Cash, Position과 PnL을 반영한다.
9. Dashboard에서 전체 Trace를 조회한다.
10. Feed를 중단하면 신규 진입이 자동 차단된다.
11. Kill Switch가 신규 주문을 막고 미체결 주문을 취소한다.

## 10. Definition of Done

- [ ] F01~F18의 완료 조건을 통과했다.
- [ ] 전 종목 Feed가 예상 Peak 2배에서 동작한다.
- [ ] Agent 장애가 Streaming, Risk와 OMS를 중단하지 않는다.
- [ ] 미래 데이터가 RAG와 Backtest에 유입되지 않는다.
- [ ] Risk 승인 없는 주문이 존재하지 않는다.
- [ ] 중복 주문과 잘못된 OMS 상태 전이가 0건이다.
- [ ] 주문을 Market Event부터 PnL까지 추적할 수 있다.
- [ ] Long/Short, Market Neutral, Event Driven과 Quant 대표 Fixture가 공통 Strategy Contract를 통과한다.
- [ ] Capability 미충족 Strategy가 Paper 주문을 만들지 못한다.
- [ ] 재시작 후 주문, Position과 Trading State를 복구한다.
- [ ] Kill Switch와 Entry Block을 검증했다.
- [ ] 10거래일 연속 Paper Dry Run을 완료했다.

## 11. 구현하지 않을 기능

- Live Broker 주문
- 실제 Borrow 계약과 실제 Margin을 사용하는 Live 주문
- Capability가 준비되지 않은 선물·옵션·복합 Multi-leg 주문
- 외부 투자자와 Fund Accounting
- 전체 부서 Agent 자동화
- 자동 생성 Python Strategy의 Production 실행
- Multi-Region과 복수 Broker Failover
- 다중 사용자·Fund·PM Pod
- HFT 수준 지연 최적화

위 기능은 P0/P1 완료 전 개발 Backlog에 추가하지 않는다.
