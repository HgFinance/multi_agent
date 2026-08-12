# Personal Hedge Fund Agent 전사 고도화 연구 로드맵

> 문서 상태: Research Recommendation v1.0  
> 기준일: 2026-07-31  
> 최상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)  
> 구현 기준: [PROJECT_IMPLEMENTATION_STATUS.md](../PROJECT_IMPLEMENTATION_STATUS.md) · [Feature Backlog](../02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md)  
> 기술 기준: [Technology Stack Decisions](../02-engineering/TECH_STACK_DECISIONS.md)  
> 차별화 연구: [Multi-Agent Trading Competitive Analysis](MULTI_AGENT_TRADING_COMPETITIVE_ANALYSIS.md)  
> 목적: 기능을 더 많이 나열하는 것이 아니라, 현재 계획을 실제 운용 가능한 개인형 헤지펀드 서비스로 발전시키는 순서와 기술 도입 조건을 정한다.

## 1. 결론

현재 마스터 플랜은 리서치, 전략, 실시간 거래, 리스크, 회계, 감사, 인사와 Production 운영까지
범위가 충분히 넓다. 다음 고도화의 핵심은 새로운 본부나 Agent를 계속 추가하는 일이 아니다.
이미 계획된 기능을 다음 여섯 개 폐쇄 루프로 연결하고, 각 루프가 실제 증거로 작동함을 검증하는 일이다.

1. `Data Truth Loop`: 시장에서 일어난 사실을 정확한 시점, 출처와 품질 상태로 저장하고 재현한다.
2. `Intelligence Loop`: Agent의 주장과 예측을 사전에 기록하고 실제 결과로 채점한다.
3. `Strategy Loop`: 가설을 재현 가능한 실험으로 검증하고 탈락, Shadow, Paper와 승격을 관리한다.
4. `Trading Loop`: 승인된 결정만 주문, 체결, 포지션, 현금과 손익으로 연결한다.
5. `Control Loop`: 리스크, 회계와 감사 조직이 Agent와 전략을 독립적으로 차단하고 검증한다.
6. `Learning Loop`: 성공과 실패의 원인을 Agent, 데이터, 전략, 정책과 운영 절차 개선에 다시 사용한다.

제품 정의는 다음과 같이 유지한다.

> 사용자의 Mandate 안에서 시장을 조사하고 전략을 발굴·검증·배포하며, 거래·위험·회계·감사와
> 자기 개선까지 수행하는 **Personal Hedge Fund Operating System**

외부에서는 CEO Agent가 대표하는 하나의 투자 서비스로 보인다. 내부에서는 권한이 분리된 디지털
헤지펀드 회사가 움직인다. 이 구조에서 LLM은 조사, 가설과 설명을 담당하고 Risk Engine, OMS,
Ledger와 권한 정책은 결정론적 서비스가 담당한다.

## 2. 이번 연구가 바꾸는 것

### 2.1 기능 중심 계획을 Gate 중심 계획으로 바꾼다

기존 52주 계획은 목표 기능의 전체 지도를 제공한다. 실제 출시는 달력에 적힌 주차가 아니라
검증 증거를 기준으로 진행해야 한다. 앞 단계의 Gate를 통과하지 못하면 파생상품, 자본 확대와
외부 투자자 기능은 시작하지 않는다.

### 2.2 Agent 수보다 평가 가능한 판단을 늘린다

Agent가 많다고 판단이 독립적이거나 정확해지는 것은 아니다. 각 Agent가 다음 항목을 남겨야 한다.

- 무엇을 예측했는가
- 언제 예측했는가
- 당시 어떤 데이터만 알고 있었는가
- 확률과 유효기간은 얼마였는가
- 어떤 반증 조건을 제시했는가
- 실제 결과와 오차는 무엇이었는가
- 그 판단이 주문, 위험과 손익에 얼마나 기여했는가

이 기록을 `Investment Intelligence Ledger`로 관리한다. 자연어 보고서가 아니라 조직 학습의
Source of Truth다.

### 2.3 새로운 기술은 도입 조건을 통과해야 한다

Kafka, Flink, Feast, Neo4j, Ray와 Kubernetes는 유용하지만 초기부터 모두 필요하지 않다.
각 기술은 해결할 문제가 실측되고, 현재 스택으로 SLO를 만족하지 못하며, 운영 책임자가 정해졌을
때만 ADR을 거쳐 도입한다.

## 3. 전사 성숙도 모델

| 단계 | 의미 | 사용자에게 보이는 상태 | 핵심 증거 |
|---|---|---|---|
| M0 설계·Prototype | 계약과 개별 기능이 존재 | Demo와 개별 실행 | Schema, 단위 테스트, Prototype |
| M1 통합 Paper 회사 | 한 Investment Case가 전 본부를 관통 | 실시간 Paper 판단과 장부 | E2E Trace, Replay, Reconciliation |
| M2 측정 가능한 Paper Fund | 품질·성과·비용·SLO를 지속 측정 | 운영 Dashboard와 일일 Close | 10거래일 Dry Run, Scorecard, Incident |
| M3 자기 개선 Digital Fund | 전략·Agent·정책 개선이 검증 후 배포 | Champion/Challenger와 개선 이력 | Shadow 결과, 승인, Rollback |
| M4 제한 실거래 서비스 | 최소 자기자본으로 통제된 실거래 | 제한된 Live Book | 법률 검토, Broker 인증, Live Gate |
| M5 외부 자본 준비 | 투자자 자금을 받을 운영 구조 | Fund 운영 서비스 | 등록·계약·독립 NAV·감사 |

현재 저장소는 여러 영역에서 M0를 넘어섰고 일부 Paper E2E Prototype도 존재한다. 그러나 공식
Read Model, 상시 통합 Runtime, 전사 Trace, 실험 계보와 10거래일 운영 증거가 아직 하나로
확정되지 않았으므로 회사 전체 성숙도는 **M0에서 M1로 전환 중**으로 평가한다.

최근 로컬 `main`에는 구현 현황 문서 작성 이후 리서치 수집·Calendar·샤딩 관련 커밋이 추가됐다.
따라서 `PROJECT_IMPLEMENTATION_STATUS.md`를 정기적으로 코드와 CI 증거에서 다시 생성하는 작업도
P0 통제 항목으로 본다.

## 4. 목표 운영 구조

```text
사용자 Mandate
      |
      v
CEO Agent / Hermes
      |
      v
Investment Case + Case Budget + Required Evidence
      |
      +---------- Research ----------> Claim / Forecast / Evidence
      |                                  |
      +---------- Quant -------------> Hypothesis / Experiment / Strategy
      |                                  |
      +---------- Trading -----------> OrderIntent / Execution Plan
      |                                  |
      +---------- Risk --------------> APPROVE / RESIZE / REJECT
      |                                  |
      +---------- OMS ----------------> Order / Fill / Position
      |                                  |
      +---------- Accounting --------> Cash / Ledger / NAV / Attribution
      |                                  |
      +---------- AI QA/Audit -------> Finding / Calibration / Incident
                                         |
                                         v
                       Investment Intelligence Ledger
                                         |
                                         v
                     ImprovementCandidate / Shadow / Approval
```

모든 경로는 `case_id`, `trace_id`, `event_id`, `decision_id`, `strategy_version`,
`policy_version`, `model_version`과 `evidence_id`로 연결한다.

## 5. 출시 Gate

### Gate 0. Canonical Truth

목표는 팀이 같은 상태와 계약을 보고 개발하게 만드는 것이다.

필수 산출물:

- 코드, Migration과 실제 Runtime에서 생성하는 구현 현황
- Event, Decision, Risk, Order, Fill, Ledger와 Agent Status 계약 Test
- `pyproject.toml`과 `uv.lock` 또는 동등한 재현 가능한 Dependency Lock
- 문서 요구사항에서 Test와 Runtime 증거로 이어지는 Traceability Matrix
- 환경별 `DEMO/PAPER/LIVE` Mode와 금지 기능 목록

통과 기준:

- 문서의 `구현됨` 항목마다 코드 경로와 자동 검증 명령이 있다.
- 같은 객체를 본부마다 다른 이름이나 상태로 해석하지 않는다.
- `LIVE` Credential이 개발, Replay와 CI에 존재하지 않는다.

### Gate 1. Integrated Paper Case

목표는 실제 시장 사건 하나가 전 본부를 통과하는 최소 서비스를 완성하는 것이다.

필수 흐름:

```text
LS WebSocket 또는 Replay
  -> Market Event
  -> Feature/Event Detection
  -> Research RAG
  -> Investment Committee
  -> Risk
  -> Paper OMS/Fill
  -> Ledger/Position/NAV
  -> QA/Audit
  -> AI Office
```

통과 기준:

- 한 `trace_id`로 데이터부터 손익과 감사 Finding까지 조회된다.
- 중복, 순서 역전, 재연결과 재시작 후에도 결과가 일관된다.
- Evidence가 없거나 데이터가 오래됐으면 신규 Entry를 만들지 않는다.
- Agent, Redis 또는 LLM 장애가 Risk/OMS/Ledger 상태를 오염시키지 않는다.

### Gate 2. Measured Paper Fund

목표는 “동작한다”를 “운영 상태를 숫자로 설명할 수 있다”로 바꾸는 것이다.

필수 산출물:

- 10거래일 연속 Paper Dry Run
- 일일 Reconciliation, Preliminary/Official Paper NAV와 Close Checklist
- Feed, Queue, Agent, Risk, OMS와 Ledger SLO
- Forecast Calibration, RAG 품질, 전략 성과와 운영 비용 Scorecard
- Incident, Postmortem, Error Budget와 Release Freeze 정책

통과 기준:

- 누락 데이터, Broker Simulation과 내부 장부의 차이를 매일 설명한다.
- p95/p99 지연과 데이터 신선도가 정의된 SLO 안에 있다.
- Error Budget을 초과하면 신규 기능과 전략 승격이 자동 동결된다.

### Gate 3. Scientific Strategy Factory

목표는 전략 아이디어 생성이 아니라 과적합을 통제한 연구·배포 공장을 만드는 것이다.

필수 산출물:

- PIT Dataset과 Feature/Label Registry
- 모든 실험의 Dataset, Code, Parameter, Trial Count와 Artifact 계보
- 거래비용, Slippage, Capacity와 Borrow/Market Impact 모델
- Purged Walk-Forward, Multiple Testing 보정과 독립 Strategy Court
- Champion/Challenger, Shadow, Paper, 중단과 Rollback

통과 기준:

- 선택된 결과뿐 아니라 실패한 Trial 수와 탐색 범위가 보존된다.
- Backtest 수익률만으로 승격할 수 없다.
- Red Team, Risk와 AI QA가 전략 개발 조직과 독립적으로 거부할 수 있다.
- 같은 Dataset과 Commit에서 같은 결과를 재현한다.

### Gate 4. Self-Improving Organization

목표는 Hermes의 Memory와 Skill을 회사 전체의 검증된 개선 구조로 연결하는 것이다.

필수 산출물:

- Agent Capability Registry와 역할별 평가 세트
- Agent, Prompt, Skill, Workflow와 Tool Policy Version
- ImprovementCandidate의 Eval, Shadow, 승인, 배포와 Rollback Runner
- Agent·Source·Strategy·Committee 기여도와 비용 Attribution
- 현재 조직과 후보 조직을 같은 사건으로 비교하는 Shadow Organization Lab

통과 기준:

- Memory에 기록됐다는 이유만으로 Prompt, Skill 또는 권한이 바뀌지 않는다.
- 개선은 고정 Eval과 Shadow 운영에서 기존 Version보다 우수해야 한다.
- PnL 하나가 아니라 정확성, Calibration, 위험, 비용과 운영 안정성으로 평가한다.

### Gate 5. Limited Live

목표는 작은 자기자본으로 통제 시스템의 현실성을 검증하는 것이다.

필수 산출물:

- 관할 법률 적용성 검토와 승인된 서비스 Mode
- Broker/FCM 인증, Session Recovery, Drop Copy와 3-way Reconciliation
- 실제 수수료, 세금, Slippage, Market Impact와 Settlement
- Kill Switch, Reduce-only, Break-glass, Backup/Restore와 DR Drill
- 독립 Risk, Accounting, Security와 운영 담당자의 서명

통과 기준:

- 승인된 한 Strategy, 한 Book과 제한 Universe만 활성화한다.
- 알 수 없는 주문 상태는 재전송하지 않고 조회와 사람 승인을 요구한다.
- 손실, 장애 또는 SLO 악화 시 자본과 권한을 자동 축소한다.

외부 투자자 자금은 Gate 5와 별개다. 등록·신고 또는 면제, 계약, 수탁, 독립 NAV, AML/KYC와
투자자 운영을 추가로 통과해야 한다.

## 6. 본부별 고도화 계획

### 6.1 CEO Agent와 Agent Workforce

고도화 목표:

- 사용자의 자연어 요구를 Version이 있는 Mandate와 Risk Appetite로 변환한다.
- 본부에 사건과 예산을 배정하되 Risk, Accounting과 Audit의 독립성을 침해하지 않는다.
- Agent를 “고용”할 때 역할, Tool, 데이터, 비용, 평가와 해고 조건을 함께 등록한다.

P0:

- Mandate Repository와 사용자 승인 Interrupt/Resume
- Case Budget, LLM Budget, SLA와 Escalation 정책
- Agent Capability/Permission Registry
- 조직 상태를 공식 Event와 Read Model에서 조회

P1:

- Agent 성과를 예측 정확도, 수정률, 비용과 정책 위반으로 평가
- Build, Extend, Retire 결정 자동 제안
- Shadow Agent와 Shadow Committee 비교

핵심 지표:

- 승인 없이 변경된 Mandate 수: 0
- 권한 초과 Tool 호출 수: 0
- Case당 비용, 처리시간과 사람 개입률
- Agent 개선 후 고정 Eval과 Shadow 성능 변화

중단 조건:

- CEO Agent가 Risk 거부, 원장 상태 또는 감사 Finding을 덮어쓰려 하면 해당 Command를 차단한다.

### 6.2 리서치본부

고도화 목표:

- 많이 수집하는 조직이 아니라, 특정 시점에 무엇을 알고 있었는지 증명하는 조직이 된다.
- 뉴스, 공시, X와 시장 데이터를 Claim과 Forecast로 구조화하고 결과로 채점한다.

P0:

- `event_time`, `received_at`, `observed_at`, `ingested_at`과 `as_known_at`의 의미와 순서 불변식
- Source License, Entitlement, Entity Resolution과 Duplicate Group
- Schema, 범위, 신선도, 결측과 이상치 Data Quality Gate
- 뉴스·공시 수정과 삭제 이력, 원문 Hash와 수집 Version
- Claim, Citation, Forecast, Resolution Rule과 Outcome Schema
- 역할별 Corpus-aware Retrieval Plan과 Context Timeline
- 독립 분석 Branch, Claim/Evidence Graph와 Citation·Numeric·Time Validator
- Macro/중기와 Micro/단기 Outlook의 독립 생성, 합성 및 Skeptic Challenge
- RES-08 전담 SearXNG Web Search MCP, `WebSearchRequest`와 Search Hit 검증 계약

P1:

- 사건 중심 Temporal Market Memory Graph
- Source와 Analyst의 Calibration 및 사후 정확도
- Query 난이도에 따른 no-RAG, single-hop, multi-hop 동적 라우팅
- 금융 문서 구조·표·Metadata를 보존하는 MimirRAG식 Parser와 FinSAgent식 Retrieval Spike
- X 유명인 Insight를 단독 근거가 아닌 교차 검증 대상 Evidence로 사용
- Web Search Queue·Citation 업무가 반복 병목일 때만 RES-10 Web Intelligence Researcher 조건부 신설

핵심 지표:

- Source별 게시-관측-적재 지연
- Duplicate, Quarantine, Late Event와 Entity Link 오류율
- Claim Citation Correctness와 Faithfulness
- Forecast Brier Score, Calibration Error와 Resolution 지연

중단 조건:

- 미래 시점 데이터, 미승인 Source, 사용권 불명 데이터와 수정 이력 없는 문서는 전략 학습과 거래
  근거에 사용하지 않는다.

### 6.3 퀀트/백테스트본부와 전략기획위원회

고도화 목표:

- 좋은 백테스트를 찾는 조직이 아니라, 거짓 발견을 줄이고 반복 가능한 전략을 생산하는 조직이 된다.

P0:

- Hypothesis, Dataset Manifest, Experiment, Trial과 Artifact Registry
- PIT Join, Delisting, Corporate Action, Survivorship와 Look-ahead 검사
- 수수료, 세금, Slippage, Borrow, Turnover와 Capacity 모델
- Walk-Forward, Purge/Embargo, Regime/Stress와 Parameter Stability
- ResearchPacket Claim ID가 연결된 가설 사전 등록과 대안 설명
- Deflated Sharpe Ratio, PBO, CPCV와 Trial Accounting

P1:

- 독립 `Adversarial Strategy Court`
- Qlib/RD-Agent-Quant 격리 Spike
- TimeSeriesScientist 역할 분리와 TimeCopilot Model Adapter Spike
- Strategy Family별 Adapter와 공통 Promotion Evidence
- Champion/Challenger와 자동 Shadow 배포

P2:

- Agent·전략별 연구 예산을 배정하는 Adaptive Research Budget
- 충분한 실행 궤적이 쌓인 뒤 Organization Credit Assignment
- L2 데이터와 실행 정책이 준비된 뒤 Market Digital Twin

핵심 지표:

- 가설당 Trial 수, 재현 성공률과 폐기율
- Backtest-Shadow-Paper 성과 괴리
- DSR, Drawdown, Turnover, Capacity와 Incremental Alpha
- 전략 승격 후 조기 중단과 Rollback 비율

중단 조건:

- Trial 수, 비용 모델, PIT 증거 또는 독립 검증이 없으면 Strategy Bundle을 만들 수 없다.

Research와 Quant의 현재/목표 구조, Hermes·LangGraph 책임, V2 계약과 구현 순서는
[Research-Quant Evidence-to-Strategy Framework](../02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)를
기준으로 한다.

### 6.4 트레이딩본부

고도화 목표:

- Agent 의견을 주문으로 바꾸는 조직이 아니라, 승인된 Intent를 안전하고 설명 가능하게 집행하는
  조직이 된다.

P0:

- OrderIntent, Parent/Child Order, Fill과 Cancel/Replace 상태 머신
- 멱등 Key, Sequence Gap, 재시작 Recovery와 `UNKNOWN` 상태
- 부분 체결, Fee, Tax, Slippage와 Paper Liquidity 모델
- 주문 전 Risk, 장중 Limit, Kill Switch와 Reduce-only
- Broker Simulation, OMS와 Ledger의 3-way Reconciliation

P1:

- Implementation Shortfall와 TCA
- Strategy/Instrument별 Execution Policy Challenger
- Queue Position과 Market Impact 근사
- Broker Sandbox/Certification Test Suite

핵심 지표:

- 중복 주문, Lost Order와 Unknown Order 수
- Decision-to-Ack, Cancel과 Recovery 지연
- 예상 대비 Slippage와 Implementation Shortfall
- 부분 체결·거절·취소 후 Position 불일치

중단 조건:

- Feed, Risk State, Position, Broker Session 또는 Reconciliation이 불명확하면 신규 Entry를 차단한다.

### 6.5 리스크본부

고도화 목표:

- 투자 판단을 설명하는 Agent가 아니라, 손실 한도와 운영 지속성을 결정론적으로 집행하는 독립
  통제 조직이 된다.

P0:

- Mandate와 Risk Appetite를 Version 있는 Policy로 변환
- Gross/Net, 종목·섹터·Factor 집중, Liquidity와 Daily Loss
- Data Staleness, Model/Strategy Status와 Operational Risk Gate
- Pre-trade, Intraday, Post-trade Risk와 Limit Breach Workflow
- Historical, Hypothetical, Reverse Stress Scenario Library

P1:

- Margin, Borrow, Basis, Greeks와 Tail Risk
- Crowding, Capacity와 다중 전략 중복 노출
- 독립 Model Inventory, Validation, Overlay와 Retirement
- Risk Policy Shadow Test와 Limit 변경 승인

핵심 지표:

- 승인되지 않은 Limit Breach 수: 0
- Breach 탐지와 De-risking 시간
- Stress Loss, Liquidity Days와 Margin Buffer
- False Block와 Missed Breach

중단 조건:

- Risk State Store 장애, 가격 신선도 초과 또는 Position 불일치 시 Fail-closed로 전환한다.

### 6.6 회계/포트폴리오본부

고도화 목표:

- 화면에 보이는 PnL이 아니라, 체결과 자본 이동에서 재구성 가능한 공식 장부를 만든다.

P0:

- Double-entry Ledger를 Cash, Position과 NAV의 Source of Truth로 확정
- OMS, Broker Statement와 Ledger 3-way Reconciliation
- Fee, Tax, Borrow, Corporate Action, Settlement와 Correction Entry
- Strategy, Book, Pod와 Fund별 PnL/Exposure Projection
- Preliminary/Official Paper NAV와 Daily Close

P1:

- Performance Attribution을 Alpha, Beta, Factor, FX, Carry와 Execution Cost로 분해
- Tax Lot, Margin, Collateral와 Treasury Forecast
- Restatement, Break Aging과 독립 Close Approval

핵심 지표:

- Unexplained Break와 장부 직접 수정 수: 0
- NAV 확정시간과 Restatement 수
- Strategy별 Gross/Net PnL, Cost와 Capital Efficiency
- Broker 대비 Position, Cash와 Margin 일치율

중단 조건:

- 장부 Break가 허용 범위를 넘거나 NAV가 확정되지 않으면 자본 확대와 전략 승격을 동결한다.

### 6.7 AI QA/감사본부

고도화 목표:

- LLM 답변을 다시 LLM으로 평가하는 조직이 아니라, 데이터·주장·Tool·정책·배포와 결과를 독립적으로
  검증하는 조직이 된다.

P0:

- Contract, Frozen Case, Adversarial, Shadow와 Live Monitoring의 Eval Pyramid
- Claim-Citation, Retrieval, Generation과 Forecast Calibration 평가
- Tool Allowlist, Secret/PII Redaction과 Prompt Injection Test
- Agent/Model/Prompt/Skill/Policy Inventory와 Version
- Trace, Finding, Incident, Remediation과 Evidence Export

P1:

- 고위험 변경의 독립 Model Validation
- Data Poisoning, Indirect Prompt Injection, Tool Abuse와 Egress Red Team
- Container, Model, Strategy Bundle 서명과 SBOM
- Audit 표본 추출과 Control Effectiveness Score

핵심 지표:

- 근거 없는 중요 Claim, 정책 위반과 승인 우회 수
- Finding Aging, 재발률과 탐지시간
- Eval Regression과 Rollback 시간
- Prompt Injection과 Tool Abuse 차단률

중단 조건:

- LLM Judge 하나만 통과한 변경은 Production 또는 Paper 자동 승격 대상으로 인정하지 않는다.

### 6.8 AI Office와 공통 Platform

고도화 목표:

- Scripted Demo를 공식 상태를 읽고 승인·차단·복구를 수행하는 Operator Control Plane으로 전환한다.

P0:

- Hermes Kanban Status Bridge와 `agent.status.v1`
- Supabase 공식 Read Model, Auth와 RLS
- `/ui/snapshot`, `/ws/operations`, Sequence Gap과 Snapshot Recovery
- `DEMO/PAPER/LIVE`, 데이터 신선도와 Degraded Mode 표시
- Case, Position, Risk, Order, Break, Finding과 Incident Workbench

P1:

- OpenTelemetry 기반 전사 Trace와 비용 분석
- SLO, Error Budget, Alert, On-call과 Incident Command
- 수동 Takeover, Kill Switch, Approval와 Break-glass
- RPO/RTO, Backup/Restore와 DR Drill Dashboard

핵심 지표:

- UI와 Source of Truth 상태 불일치
- WebSocket Gap 복구시간
- SLO Burn Rate, MTTA, MTTR와 Change Failure Rate
- 서비스·Agent·Model·Source별 비용

중단 조건:

- AI Office는 Risk, OMS와 Ledger를 직접 수정하지 않는다. 명령은 인증된 Domain API와 승인
  Workflow를 통해서만 전달한다.

## 7. 공통 고도화 기능

### 7.1 Investment Intelligence Ledger

다음 객체를 별도 테이블로 관리하고 ID로 연결한다.

| 객체 | 핵심 필드 |
|---|---|
| Forecast | 대상, 방향/값, 확률, Horizon, Resolution Rule, `as_known_at` |
| Claim | 주장, 중요도, 작성 Agent, 반증 조건 |
| Evidence | Source, 원문 Hash, Event/Observed/Known Time, License |
| Decision | Action, Confidence, Alternative, Expiry, Policy Version |
| Outcome | 실제 값, Resolution Time, Data Revision |
| Attribution | Agent, Source, Strategy, Execution, Risk와 Cost 기여 |
| Improvement | 변경 대상, 근거, Eval, Shadow, 승인, Rollback |

이 Ledger는 Agent Memory와 다르다. Memory는 업무 Context를 돕고, Ledger는 평가와 감사의 공식
기록이다.

### 7.2 시간과 데이터 계약

모든 데이터는 최소 다음 시각을 구분한다.

```text
event_time     거래소·원천에서 실제 사건이 발생한 시각
received_at    우리 Gateway가 Event를 처음 수신한 시각
observed_at    회사 시스템이 의사결정에 사용할 수 있게 된 시각
ingested_at    우리 저장소에 기록한 시각
as_known_at    과거 조회에서 허용할 정보의 관측 시각 Cut-off
valid_from/to  참조 데이터가 유효한 기간
```

실시간 집계는 Event Time, Replay와 학습은 `observed_at <= as_known_at`을 기준으로 한다.
늦게 도착한 Event는
삭제하지 않고 `late`, `corrected`, `supersedes_event_id`로 기록한다.

### 7.3 서비스 Mode와 법률 경계

기능이 같아도 누구의 자금을 어떤 권한으로 운용하는지에 따라 법률 검토 범위가 달라진다.

| Mode | 허용 범위 | 추가 Gate |
|---|---|---|
| Research | 분석과 Replay | 데이터 사용권, 연구 고지 |
| Self-directed Paper | 사용자 Mandate 기반 Paper | Auth, 개인정보, Paper 통제 |
| Proprietary Live | 자기자본 제한 실거래 | 법인·세무·Broker·운영 검토 |
| Advisory | 타인에게 개인화된 조언 | 관할 등록·광고·적합성 법률 검토 |
| Discretionary | 타인 자금을 재량 운용 | 등록·계약·수탁·감사·투자자 운영 |
| External Fund | 외부 자본 Fund | Fund Vehicle, Admin, AML/KYC와 독립 NAV |

법률 적용 여부는 코드나 Agent가 확정하지 않는다. 관할 법률 자문과 승인 기록이 Source of Truth다.

### 7.4 복잡도 예산

새 서비스나 Library는 ADR에 다음 내용을 포함해야 한다.

- 현재 어떤 장애, SLO 또는 개발 병목을 해결하는가
- 기존 스택으로 해결하지 못한 실측 증거는 무엇인가
- 운영 Owner와 On-call 책임자는 누구인가
- 데이터 이관, 장애 복구와 제거 방법은 무엇인가
- 성능, 비용과 통제 개선 목표는 얼마인가
- 목표를 달성하지 못하면 언제 제거하는가

## 8. 기술 스택 고도화 연구

### 8.1 유지할 Core Stack

현재 선택은 전체적으로 적절하다. 다음 도구는 역할 경계를 유지하며 계속 사용한다.

| 계층 | 유지 도구 | 역할 |
|---|---|---|
| 상위 Agent | Hermes | CEO/Supervisor, Memory, Skill, 사용자 Context와 업무 위임 |
| 투자 Workflow | LangGraph | 상태 머신, Checkpoint, Interrupt/Resume와 승인 |
| 주 LLM | Amazon Bedrock Claude | 통합 환경의 Deep Reasoning과 Model Gateway |
| 로컬 LLM | Ollama | 개발, Offline Eval, 저비용 분류와 장애 대체 |
| Domain API | FastAPI, Pydantic v2 | 계약 검증과 본부별 API |
| Transaction DB | Supabase PostgreSQL | 업무, 결정, 주문, 장부, 감사와 Auth/RLS |
| Vector Search | pgvector | 초기 Hybrid RAG와 Evidence Retrieval |
| Time Series | TimescaleDB | Tick, Quote, Bar와 Feature |
| Hot State/Event | Redis, Redis Streams | Rolling State, Queue와 초기 Event Transport |
| Research Data | Polars, PyArrow, Parquet, DuckDB | Feature, PIT Dataset, Archive와 Local Analytics |
| Backtest | vectorbt + Strategy Adapter | 초기 전략 검증 |
| Runtime | Docker, Docker Compose | 서비스 격리와 재현 가능한 배포 |
| Frontend | Next.js, React, TypeScript | AI Office와 Operator Control Plane |

Hermes와 LangGraph는 Risk, OMS와 Ledger를 대체하지 않는다. Bedrock Guardrails도 결정론적 Risk
Gate나 Tool 권한 통제를 대체하지 않는다.

### 8.2 바로 보강할 도구

| 도구 | 적용 시점 | 사용하는 이유 | 경계 |
|---|---|---|---|
| `pyproject.toml` + `uv.lock` | Gate 0 | 문서와 실제 Dependency 차이 제거 | 서비스별 Extra/Group 분리 |
| Pandera | Gate 0~1 | Polars/Pandas Dataset Schema와 DQ 검사 | Pydantic Event 계약과 중복 금지 |
| OpenTelemetry | Gate 1 | Agent-to-Order-to-Ledger Trace | Prompt/PII 원문 기본 수집 금지 |
| Prometheus + Grafana | Gate 1~2 | Feed, Queue, Risk, OMS와 SLO | AI 평가 결과는 별도 Ledger |
| MLflow | Gate 2~3 | Experiment, Dataset, Model과 Artifact 계보 | Strategy 승인 Workflow는 LangGraph |
| Optuna | Gate 3 | 제한된 탐색과 Trial 기록 | Trial Budget와 DSR 없이 자동 최적화 금지 |
| Ragas | Gate 2 | RAG 회귀 진단 보조 | 금융 정확성의 단독 승인자 금지 |
| Cosign + SBOM | Gate 2~3 | Container와 Strategy Artifact 출처 검증 | 서명되지 않은 Bundle 승격 금지 |
| OPA/Conftest | Gate 2~3 | CI, 배포, 권한과 Promotion Policy-as-Code | 수치 Risk 계산에는 사용하지 않음 |

`requirements.txt`는 현재 구현 Dependency의 일부만 표현하고 설계 문서의 P0 도구와 차이가 있다.
Gate 0에서 실제 Import Scan, Docker Build와 Test를 기준으로 `pyproject.toml`/`uv.lock`을
정규화하고, Hermes Runtime과 Domain Service Runtime은 별도 환경으로 유지한다.

### 8.3 격리 Spike로 검증할 도구

| 후보 | Spike 질문 | 채택 조건 |
|---|---|---|
| Qlib | 한국 시장 PIT Dataset과 비용 모델을 Adapter로 연결할 수 있는가 | 기존 Backtest와 동일 사건 결과 재현 |
| RD-Agent-Quant | 가설·Factor·Model 반복 연구가 우리 Promotion Gate를 준수하는가 | 격리 Runner, Trial 계보, 비용 상한과 실패 재현 |
| MimirRAG/FinSAgent 패턴 | DART 표·절·Metadata와 로컬 Corpus 구조를 보존할 때 Citation/Numeric 정확도가 개선되는가 | 고정 Finance QA Fixture에서 기존 Hybrid RAG보다 개선 |
| TimeCopilot | 통계·ML·TSFM을 같은 PIT Dataset과 비용 조건으로 비교할 수 있는가 | 기존 Runner 계약 유지, 단순 Baseline 대비 반복 OOS 개선 |
| SearXNG + Playwright MCP | API Quota를 보완하면서 Search/Open 권한과 Evidence 승격을 분리할 수 있는가 | RES-08 전담, Replay 호출 0건, Browser Secret·내부망 차단 |
| Feast | Offline/Online Feature 정의가 실제로 어긋나는가 | 공유 Feature와 Online Model 증가로 mismatch가 반복 |
| Dagster | 수집·Dataset·Experiment 의존성을 Asset으로 관리할 가치가 있는가 | Scheduler/수동 Handoff가 반복 장애 원인 |
| Ray | Backtest와 Hyperparameter Trial의 단일 Host 시간이 병목인가 | 병렬화로 비용 대비 완료시간 SLO 개선 |
| ABIDES/JAX-LOB | 실행 정책과 Market Impact 검증에 L2 시뮬레이션이 필요한가 | L2 Replay, Calibration 데이터와 TCA 기준 존재 |

Spike는 Core Runtime과 별도 Docker Image에서 수행한다. 채택하지 않아도 Dataset, Experiment와
Strategy 계약은 유지돼야 한다.

### 8.4 조건부 도입 기술과 Trigger

아래 수치는 보편적 정답이 아니라 팀의 초기 ADR Trigger다. 실제 SLO와 부하 시험에서 조정한다.

| 후보 | 도입 Trigger | 도입 전 기본안 |
|---|---|---|
| Kafka 또는 Redpanda | Redis가 Peak 2배 부하, 장기 Retention, 다수 Consumer와 Replay SLO를 반복 실패 | Redis Streams + Parquet Archive |
| Flink | Kafka 도입 후 Event-time Join, Watermark, Late Event와 Stateful Recovery가 Python Worker 한계를 초과 | Polars/Python Streaming Worker |
| ClickHouse | TimescaleDB가 압축·Retention·p99 분석 SLO 또는 비용 목표를 실패 | TimescaleDB + Parquet + DuckDB |
| Feast | 3개 이상 Online Model이 공유 Feature를 사용하고 Training/Serving Skew가 운영 사고로 확인 | Versioned Feature Spec + Redis Snapshot |
| Neo4j | Postgres/pgvector의 시간·사건 3-hop Query가 성능·개발 SLO를 반복 실패 | PostgreSQL Edge Table + pgvector |
| dbt | 소유자가 다른 SQL Transformation이 증가해 Lineage/Test 누락이 반복 | Alembic + SQL View + pytest |
| OpenLineage | Scheduler, MLflow와 Data Job 간 계보가 분리돼 Root Cause 추적에 실패 | 공통 Dataset/Experiment ID |
| Ray | 단일 Node Backtest/Trial이 승인된 연구 SLA와 비용 목표를 초과 | Process Pool, Optuna와 단일 GPU Worker |
| Kubernetes | 여러 Host, 자동 Scaling, 강한 Tenant 격리와 무중단 배포가 실제 요구 | Docker Compose + 단일 VM/Managed Container |

### 8.5 지금 보류할 기술

- 모든 Tick을 LLM 또는 LangGraph State에 넣는 구조
- Agent가 Risk Limit, Position, Cash와 Ledger를 직접 수정하는 Tool
- 실패 Trial을 남기지 않는 자동 전략 생성
- 유명 투자자의 정체성·문체를 흉내 내는 Persona Fine-tuning
- 충분한 Label과 평가 없이 Agent를 강화학습하는 구조
- Kafka, Flink, Kubernetes와 Graph DB를 동시에 도입하는 Platform 재작성
- LLM Judge 하나로 전략, Agent 또는 배포를 자동 승인하는 구조

### 8.6 Cloud-neutral 배포 기준

Cloud Provider가 미정이므로 서비스 계약을 특정 공급자가 아니라 기능으로 정의한다.

| 기능 | Cloud-neutral 계약 | AWS 후보 예시 |
|---|---|---|
| Container Runtime | OCI Image, Healthcheck, Rolling/Blue-Green | ECS/Fargate 또는 EC2 |
| Object Storage | S3-compatible, Versioning, Lifecycle | S3 |
| Transaction DB | PostgreSQL, PITR, RLS와 Backup | Supabase 또는 RDS/Aurora 검토 |
| Time Series | Timescale 호환 PostgreSQL 또는 검증된 대안 | EC2/RDS 가능성 별도 Benchmark |
| Cache/Event | Redis Protocol과 Durable Stream | ElastiCache, 자체 Redis 또는 Managed 대안 |
| LLM | Model Gateway Contract | Bedrock Claude |
| Secret/KMS | Workload Identity, Rotation, Audit | IAM, Secrets Manager, KMS |
| Telemetry | OTLP, Prometheus Export | CloudWatch/AMP/Grafana 후보 |
| Container Registry | OCI, Digest, Signature | ECR |

초기 Paper 서비스는 단일 VPS 또는 VM의 Docker Compose도 가능하다. 다만 Transaction DB,
Object Storage와 Backup은 가능하면 Managed Service로 분리하고, WebSocket Worker 재시작,
고정 Egress, Disk 장애, Secret, RPO/RTO와 운영자 부재 상황을 반드시 시험한다.

### 8.7 권장 Runtime 분리

```text
control-plane
  hermes-supervisors
  langgraph-workflows
  api-bff

data-plane
  ls-websocket-workers
  research-collectors
  feature-event-workers

trading-plane
  risk-engine
  oms-paper-broker
  ledger-reconciliation

research-plane
  dataset-builder
  backtest-runner
  mlflow
  isolated-rd-spike

observability-plane
  otel-collector
  prometheus
  grafana
  audit-exporter
```

Plane 사이에는 Version이 있는 API/Event 계약만 사용한다. Hermes와 연구 Runner는 Trading DB의
직접 쓰기 Credential을 갖지 않는다.

## 9. 우선순위

### P0. 지금부터 Gate 1까지

1. 구현 현황을 최근 코드와 CI 증거로 다시 생성한다.
2. Dependency를 `pyproject.toml`과 `uv.lock`으로 고정한다.
3. Market Event부터 Ledger와 Audit까지 한 Case를 실제 Runtime으로 관통한다.
4. Event Time, Known-at, Duplicate, Late Data와 Quarantine 계약을 확정한다.
5. Forecast·Claim·Evidence·Outcome 최소 Ledger를 만든다.
6. Risk/OMS/Ledger 재시작, 멱등성과 3-way Reconciliation을 통합 시험한다.
7. AI Office를 공식 Snapshot, Auth와 `/ws/operations`에 연결한다.
8. OpenTelemetry, Feed/Queue/Risk/OMS SLI와 Fail-closed Alert를 연결한다.

### P1. Gate 2까지

1. 10거래일 Paper Dry Run과 Daily Close를 수행한다.
2. Calibration, Citation, RAG, Strategy와 운영 비용 Scorecard를 만든다.
3. SLO, Error Budget, Incident, Postmortem과 Release Freeze를 운영한다.
4. MLflow를 Experiment와 Artifact Registry에 연결한다.
5. Container/Strategy Bundle 서명과 기본 Agentic Security Red Team을 도입한다.
6. Mandate, Agent Status, ImprovementCandidate의 승인·복구 Workflow를 완성한다.

### P2. Gate 3까지

1. PIT Dataset Factory와 Trial Accounting을 완성한다.
2. 거래비용, Capacity, DSR와 독립 Strategy Court를 Promotion Gate에 넣는다.
3. Qlib/RD-Agent-Quant를 격리 Spike하고 채택 여부를 ADR로 결정한다.
4. Strategy Champion/Challenger, Shadow, Paper와 Rollback을 자동화한다.
5. TCA, Performance Attribution과 Model Risk Inventory를 운영한다.
6. Event/Claim Temporal Graph를 PostgreSQL 기반으로 먼저 구현한다.

### P3. Gate 4 이후

1. Shadow Organization Lab과 Agent Capability Registry를 운영한다.
2. Agent·Source·Strategy 기여도와 Adaptive Research Budget을 연결한다.
3. 충분한 궤적과 검증된 Reward가 있을 때만 Agent Learning을 실험한다.
4. 주식 Paper Fund가 안정된 뒤 선물, 옵션과 Market Digital Twin을 단계적으로 확장한다.
5. Broker 인증, 법률 검토와 DR을 통과한 뒤 Limited Live를 시작한다.

## 10. 전사 Scorecard

수익은 필요하지만 단독 목표가 될 수 없다.

| 축 | 대표 지표 |
|---|---|
| Data Truth | 누락·중복·Late·Quarantine, 신선도, PIT 위반 |
| Intelligence | Brier Score, Calibration, Claim 정확성, Citation Faithfulness |
| Strategy Science | DSR, OOS 안정성, Trial 수, Capacity, Shadow/Paper 괴리 |
| Trading | Ack/Cancel 지연, Slippage, Unknown Order, Fill/Position 일치 |
| Risk | Limit Breach, Stress Loss, Liquidity, De-risking 시간 |
| Accounting | Break, NAV 정시율, Restatement, Cash/Position 일치 |
| Agent Quality | 수정·거절률, 비용, 정책 위반, Shadow 개선 |
| Reliability | SLO, Error Budget, MTTA, MTTR, RPO/RTO |
| Security | Prompt Injection, Tool Abuse, Secret/PII 노출, Finding Aging |
| User Control | 승인 대기, Mandate 위반, Kill Switch와 설명 가능성 |

CEO Agent의 목표 함수는 `PnL 최대화`가 아니라 다음 제약 최적화로 표현한다.

```text
maximize risk-adjusted, capacity-aware, evidence-backed utility
subject to mandate, risk, liquidity, operational, legal and cost constraints
```

## 11. 12주 실행안

| 기간 | 핵심 작업 | 완료 증거 |
|---|---|---|
| 1~2주 | 현황 재생성, Dependency Lock, 계약 정합성 | Gate 0 Checklist |
| 3~4주 | LS/Replay에서 Research·Decision까지 통합 | Case Trace 전반부 |
| 5~6주 | Risk·Paper OMS·Ledger·Reconciliation 통합 | Case Trace 후반부 |
| 7주 | AI Office 공식 Snapshot/WebSocket | 상태 Gap 복구 E2E |
| 8주 | Forecast/Claim/Outcome Ledger와 Calibration | 첫 Resolved Forecast |
| 9주 | OTel, SLI, Alert와 Incident Drill | 전사 Trace와 Postmortem |
| 10주 | PIT Dataset/Experiment Manifest 최소 구현 | 재현 가능한 첫 실험 |
| 11주 | Cost/DSR/Strategy Court Fixture | 탈락·승격 판정 |
| 12주 | 연속 Paper 운영 시작과 Gate Review | Gate 1 승인, Gate 2 Runbook |

12주 안에 파생상품, 외부 자본, Kubernetes와 전면적인 RD-Agent 자동 연구를 동시에 시작하지 않는다.
먼저 한 Case와 한 Strategy의 정확성, 장부와 복구를 완성한다.

## 12. 결정 사항

### 즉시 채택

- 달력 중심 진행을 Evidence Gate 중심 진행으로 보강한다.
- Investment Intelligence Ledger의 최소 Schema를 Gate 1 범위에 넣는다.
- `pyproject.toml`/`uv.lock`, Pandera, OpenTelemetry와 SLO를 Core 고도화 항목으로 사용한다.
- 공식 장부, Risk와 OMS는 결정론적 Source of Truth로 유지한다.
- 구현 현황을 코드와 CI 증거에서 주기적으로 갱신한다.

### Spike 후 결정

- Qlib와 RD-Agent-Quant
- MLflow의 Agent Trace까지의 확장 범위
- Feast, Dagster, Ray와 ABIDES/JAX-LOB
- OPA를 Runtime Policy에 사용할 범위
- InvestmentDoctrine의 Prompt/RAG 대비 SFT/LoRA 개선 폭과 Bedrock·Open-weight Training 경로

### Trigger 전 보류

- Kafka/Redpanda, Flink, ClickHouse, Neo4j와 Kubernetes
- Agent 강화학습과 자동 조직 재편
- 인물 정체성·문체 Persona Fine-tuning. 이름을 제거한 Doctrine Adapter는
  [Investment Doctrine Model Factory](../02-engineering/INVESTMENT_DOCTRINE_MODEL_FACTORY.md)의
  Dataset·Frozen Eval·QA Trigger 통과 후에만 허용
- 외부 투자자 대상 자동 자문·일임·Fund 기능

## 13. 연구 근거와 공개 자료

### Agent, 예측과 RAG

- [Hermes Agent](https://github.com/NousResearch/hermes-agent)
- [LangGraph Persistence and Durable Execution](https://docs.langchain.com/oss/python/langgraph/persistence)
- [LangChain Human-in-the-loop](https://docs.langchain.com/oss/python/langchain/human-in-the-loop)
- [R&D-Agent-Quant](https://arxiv.org/abs/2505.15155)
- [ForecastBench](https://arxiv.org/abs/2409.19839)
- [AIA Forecaster](https://arxiv.org/abs/2511.07678)
- [Multi-Agent Debate Controlled Study](https://arxiv.org/abs/2511.07784)
- [Adaptive-RAG](https://arxiv.org/abs/2403.14403)
- [DyG-RAG](https://arxiv.org/abs/2507.13396)
- [Correctness is not Faithfulness](https://arxiv.org/abs/2412.18004)
- [FinanceBench](https://arxiv.org/abs/2311.11944)
- [RAGChecker](https://arxiv.org/abs/2408.08067)
- [Nexus](https://arxiv.org/abs/2605.14389)
- [MimirRAG](https://arxiv.org/abs/2605.25030)
- [FinSAgent](https://arxiv.org/abs/2607.18102)
- [STORM](https://arxiv.org/abs/2402.14207)
- [Agentic Time Series Forecasting](https://arxiv.org/abs/2602.01776)
- [TimeSeriesScientist](https://arxiv.org/abs/2510.01538)
- [AlphaCast](https://arxiv.org/abs/2511.08947)
- [Synapse](https://arxiv.org/abs/2511.05460)
- [FinCon](https://arxiv.org/abs/2407.06567)

### 전략 연구와 시뮬레이션

- [Microsoft Qlib](https://github.com/microsoft/qlib)
- [The Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551)
- [A Comparison of Backtest Overfitting Prevention Methods](https://www.sciencedirect.com/science/article/pii/S0950705124011110)
- [MLAgentBench](https://arxiv.org/abs/2310.03302)
- [TimeCopilot](https://github.com/TimeCopilot/timecopilot)
- [Doubly Robust Off-policy Value Evaluation](https://proceedings.mlr.press/v48/jiang16.html)
- [Agent Lightning](https://arxiv.org/abs/2508.03680)
- [ABIDES](https://arxiv.org/abs/1904.12066)
- [JAX-LOB](https://arxiv.org/abs/2308.13289)

### 데이터, Platform과 기술

- [Apache Flink Event Time and Watermarks](https://nightlies.apache.org/flink/flink-docs-stable/docs/concepts/time/)
- [Apache Flink Checkpointing](https://nightlies.apache.org/flink/flink-docs-stable/docs/dev/datastream/fault-tolerance/checkpointing/)
- [Apache Kafka Introduction](https://kafka.apache.org/documentation/)
- [Feast Introduction](https://docs.feast.dev/v0.42-branch)
- [MLflow Tracking](https://mlflow.org/docs/latest/tracking)
- [OpenTelemetry Semantic Conventions](https://opentelemetry.io/docs/specs/semconv/)
- [Open Policy Agent](https://www.openpolicyagent.org/docs)
- [Sigstore Cosign Container Signing](https://docs.sigstore.dev/cosign/signing/signing_with_containers/)
- [Google SRE Error Budget Policy](https://sre.google/workbook/error-budget-policy/)
- [AWS Financial Services Industry Lens](https://docs.aws.amazon.com/wellarchitected/latest/financial-services-industry-lens/financial-services-industry-lens.html)

### 위험, 보안과 운영

- [NIST AI Risk Management Framework](https://www.nist.gov/itl/ai-risk-management-framework)
- [NIST Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/ai/NIST.AI.600-1.pdf)
- [NIST Adversarial Machine Learning Taxonomy](https://www.nist.gov/news-events/news/2025/03/nist-trustworthy-and-responsible-ai-report-adversarial-machine-learning)
- [OWASP Agentic AI Threats and Mitigations](https://genai.owasp.org/resource/agentic-ai-threats-and-mitigations/)
- [Federal Reserve Revised Model Risk Management Guidance SR 26-2](https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm)
- [Basel Committee Principles for Operational Resilience](https://www.bis.org/bcbs/publ/d516.htm)

### 국내 법률·개인정보 검토 출발점

- [국가법령정보센터 자본시장과 금융투자업에 관한 법률](https://www.law.go.kr/법령/자본시장과금융투자업에관한법률)
- [개인정보보호위원회 생성형 AI 개인정보 처리 안내](https://pipc.go.kr/np/cop/bbs/selectBoardArticle.do?bbsId=BS074&mCode=C020010000&nttId=11410)

이 자료는 설계 근거다. 법률 의견, 투자 성과 보증 또는 특정 기술 도입 승인을 대신하지 않는다.
