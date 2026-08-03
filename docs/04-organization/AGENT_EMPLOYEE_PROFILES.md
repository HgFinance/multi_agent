# 헤지펀드 디지털 직원 채용 및 Agent Profile 설계서

> **Current runtime override (2026-08-03)**: 실제 실행 기준은 8개 Hermes Head와 42개 독립 LangGraph Worker다. Head는 `openai-codex/gpt-5.6-luna` 기본·승인된 Claude Code 대체 런타임, Worker는 Ollama `qwen3:1.7b`다. 아래의 `54개 논리적 역할`, `Specialist Agent`, `LangGraph Node` 표현은 채용 후보·레거시 taxonomy로 보며 현재 Worker 수·실행 여부의 기준으로 사용하지 않는다. 현재 역할·trigger·tool은 [WORKER_ROLE_BOUNDARIES.md](../02-engineering/WORKER_ROLE_BOUNDARIES.md), Profile `workers`, `runtime_personalities`를 따른다.

> 2026-08-03 전사 실행 계층 확정: 8개 부서장은 Hermes + Codex/Claude Code, 직원은 직원별 독립 LangGraph Worker + Ollama `qwen3:1.7b`다. Registry는 CEO 1·HR 5·Research 6·Trading 6·Risk 4·Quant/Backtest 7·Accounting/Portfolio 8·QA 5다. 기존 RSK/QAA Profile ID는 역할·권한·평가의 레거시 식별자로 보존하며, 실행 프로세스는 각 Profile의 `workers`와 `runtime_personalities`를 따른다.

부서장 Hermes와 LangGraph 직원의 실행 경계는 [Department Worker Graph Architecture](../02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)를 따른다.

> 문서 상태: Agent Organization v1.4
> 최상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)  
> 대상 조직: CEO 에이전트 + CEO 직속 Agent Workforce 인사팀 + 6개 본부  
> 목적: 어떤 디지털 직원을 채용하고, 어떤 Skill과 Tool 권한을 부여하며, 각 직원이 실제 업무를 어떻게 수행할지 정의  
> 상위 구현 계획: [HEDGE_FUND_CORE_PLAN.md](../01-product/HEDGE_FUND_CORE_PLAN.md)  
> 공통 Domain 계약: [MINIMUM_SERVICE_UNIT_SPEC.md](../01-product/MINIMUM_SERVICE_UNIT_SPEC.md)  
> 기술 스택 기준: [TECH_STACK_DECISIONS.md](../02-engineering/TECH_STACK_DECISIONS.md)  
> 저장소 소유권: [REPOSITORY_DEPARTMENT_STRUCTURE.md](../02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md)
> 조직 Frontend: [AI_OFFICE_FRONTEND_PLAN.md](../02-engineering/AI_OFFICE_FRONTEND_PLAN.md)
> 부서별 입력 데이터·Data Product·Library: [RESEARCH_DATA_SOURCES_AND_LIBRARIES.md](../03-data/RESEARCH_DATA_SOURCES_AND_LIBRARIES.md)
> 팀별 실행 가이드: [재일](../05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md) · [도현](../05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) · [동규](../05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md) · [영주](../05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md)

---

## Runtime source of truth

이 문서의 아래 역할 카탈로그는 과거 채용안·권한 검토·평가 문맥을 보존한 Historical taxonomy다. 현재 실행 인원, Worker ID, trigger, tool, 모델, 활성/조건부 상태를 판정할 때는 아래 순서만 사용한다.

1. `departments/<n>/hermes/config.yaml`의 `staff_registry`와 `runtime_personalities`
2. `departments/03-risk/risk_employee_workers.py` 및 `departments/06-ai-qa-audit/qa_employee_workers.py`의 `WORKER_SPECS`
3. 전사 현재 Registry인 [WORKER_ROLE_BOUNDARIES.md](../02-engineering/WORKER_ROLE_BOUNDARIES.md)

따라서 이 문서의 `RSK-*`, `QAA-*`, `RES-*`, `TRD-*`, `QNT-*`, `ACC-*` 및 `Specialist Agent` 이름은 현재 Worker ID가 아니다. 현재 runtime은 부서장 Hermes + 독립 LangGraph Worker + Ollama `qwen3:1.7b`이며, 이 Historical taxonomy를 실행 설정으로 역해석하지 않는다.

## 1. 이 문서가 정의하는 것

이 프로젝트의 디지털 직원은 단순한 프롬프트 이름이 아니다. 각 직원은 다음 항목이 분리된 **실행 가능한 Job Profile**이다.

- 맡은 업무와 완료 조건
- 읽을 수 있는 데이터와 호출할 수 있는 Tool
- 제안, 승인, 차단, 실행 중 허용되는 권한
- 다른 직원에게 넘겨야 하는 입력·출력 Schema
- 지켜야 하는 SLA와 평가받는 KPI
- 실패하거나 확신이 낮을 때 Escalation하는 기준
- 재현 가능한 Prompt, Model, Skill과 Policy Version

프로젝트의 채용 대상은 총 **54개 논리적 역할**이다. 이것은 54개 LLM 서버나 54개 상시 Process를 의미하지 않는다. CEO, 6개 본부장과 Agent Workforce 인사팀장을 합친 8개의 Hermes Supervisor가 조직별 기억과 책임을 유지하고, 전문 직원은 LangGraph Case 안에서 필요한 순간에 호출된다. 가격 계산, 주문 상태 전이, Risk Limit, Ledger, NAV처럼 정확성이 필요한 업무는 직원 Agent가 아니라 결정론적 Service가 실행한다.

### 1.1 확정 경영 구조

```text
[CEO Hermes Agent]
    |
    +-- [CEO 직속 Agent Workforce 인사팀장 Hermes Agent]
    +-- [1. 리서치본부장 Hermes Agent]
    +-- [2. 트레이딩본부장 Hermes Agent]
    +-- [3. 리스크본부장 Hermes Agent]
    +-- [4. 퀀트/백테스트본부장 Hermes Agent]
    +-- [5. 회계/포트폴리오본부장 Hermes Agent]
    +-- [6. AI QA/감사본부장 Hermes Agent]
```

**CEO, 6개 본부장과 Agent Workforce 인사팀장은 각각 독립된 Hermes Agent로 구현한다.** 인사팀은 제7의 투자 본부가 아니라 CEO 직속 Shared Service다. 각 Hermes Agent는 고유한 `agent_id`, Service Identity, Memory Namespace, Department Queue, Skill Manifest, Tool Allowlist와 Token Budget을 가진다. 같은 Bedrock Claude 또는 Ollama Endpoint를 공유할 수 있지만 다른 조직의 기억·권한·승인 상태를 공유해서는 안 된다.

### Hermes Memory와 조직 학습 역할

Hermes Supervisor는 Case를 처리한 뒤 다음 업무에 재사용할 가치가 있는 교훈을 Memory·Skill·Profile 개선 후보로 정리한다. Specialist가 실패를 경험했다고 해서 자기 Prompt나 Tool 권한을 즉시 바꾸지는 않는다.

| 단계 | 책임자 | Output |
|---|---|---|
| 경험 관측 | 각 본부 Hermes | Case 결과, 오류, 비용, 반복 패턴과 `candidate_id` |
| 절차 후보 | 본부 Owner | Memory·Skill·Runbook Candidate와 근거 Case |
| Profile 후보 | Agent Workforce 인사팀 | Prompt, Skill Bundle, Model, Tool Allowlist의 새 Version |
| 독립 검증 | AI QA/감사본부 | Golden/Adversarial Eval, 권한·회귀 Finding |
| Shadow·승인 | 요청 본부 + CEO/Risk/QA 권한별 Gate | 승인 또는 거부된 불변 Version |
| 운영 측정 | QA + 본부 Scorecard | Champion 대비 품질·통제·비용 변화와 Rollback 조건 |

Hermes Memory는 공식 Position, PnL, 주문, Risk Limit과 Policy의 대체 저장소가 아니다. 이러한 값은 ID만 기억하고 담당 API에서 다시 조회한다. 다른 본부와 공유할 교훈은 Raw Memory 복사가 아니라 QA가 검증한 `ImprovementCandidate` 또는 Versioned Skill로 전달한다. 상세 재귀 루프와 적용 순서는 [마스터 플랜 5.10](../HEDGE_FUND_MASTER_PLAN.md#510-hermes-memory-기반-조직-재귀적-자기-개선)을 따른다.

### 1.2 직원 Runtime 유형 (legacy taxonomy; current registry는 상단 override와 Worker Role Boundaries를 우선)

| 유형 | 구현 | 담당 업무 | 상주 여부 |
|---|---|---|---|
| Hermes Supervisor | Hermes Agent | 사용자·본부 간 대화, 업무 분해, 전문 직원 호출, 결과 통합, Escalation | CEO, 6개 본부장과 인사팀장 상시 |
| Specialist Agent | LangGraph Node + Model Gateway | 분석, 반론, 조사, 설명과 구조화된 제안 | Case 발생 시 동적 실행 |
| Deterministic Worker | Python Worker | 계산, 검증, 정규화, Backtest, Reconciliation | Event 또는 Schedule 기반 |
| Control Service | FastAPI Domain Service | Risk 승인, OMS, Ledger, Registry, Policy와 상태 변경 | 상시 |

Hermes Supervisor는 LangGraph Workflow를 시작·중단·조회할 수 있지만 Graph Checkpoint를 임의 수정하지 않는다. Specialist Agent는 결과를 제안하지만 주문·한도·원장을 직접 변경하지 않는다. Control Service만 검증된 Command를 실행한다.

### 1.3 채용 우선순위

| 등급 | 의미 | 적용 단계 |
|---|---|---|
| P0 | Paper Trading End-to-End Loop에 반드시 필요 | 초기 Core Release |
| P1 | 운영 품질, 독립 검증과 전략 자동화를 위해 필요 | Shadow/Paper Beta |
| P2 | 파생상품, 대규모 운용과 Production 통제를 위해 필요 | Derivatives/Limited Live |

### 1.4 Multi-Strategy 운영 방식

전략마다 새로운 상시 Hermes Agent를 하나씩 만드는 방식은 사용하지 않는다. 전략이 등록되면 기존 본부의 Specialist를 조합해 임시 `Strategy Pod`를 구성한다.

| 전략군 | 필수 참여 역할 | 조건부 참여 역할 |
|---|---|---|
| Equity Long/Short·Factor | RES-03/04/07, QNT-01/02/03, TRD-01/04, RSK-02/03 | Borrow·Compliance Specialist |
| Market Neutral·Pairs | QNT-01/03/04, TRD-04/06, RSK-02/03 | Microstructure, Crowding Analyst |
| Event Driven | RES-03/05/06, QNT-01/03, TRD-02/04, RSK-01/04 | Legal/Deal Terms Reviewer |
| Futures·Macro | RES-07, QNT-01/03, TRD-05, RSK-02/05, ACC-05 | Macro Data와 Roll Specialist |
| Options·Volatility | QNT-01/03, TRD-05, RSK-05, ACC-05/07 | Vol Surface와 Exercise Specialist |
| Multi-Strategy Allocation | CEO-01, QNT-05/07, TRD-04, RSK-02/03, ACC-01/04 | Model Risk와 Capacity Reviewer |

Agent Workforce 인사팀은 `strategy_family`별 업무량과 품질 공백을 집계하되, 새 전략 이름만으로 직원을 자동 채용하지 않는다. 기존 Skill 조합으로 처리할 수 없는 반복 업무와 검증 가능한 Eval이 있을 때만 Specialist Profile을 추가한다.

모든 Strategy Pod에서 연구자는 제안하고, Quant는 검증하며, Trading은 Intent를 만들고, Risk는 독립 승인하며, Accounting은 결과를 확정하고, QA는 전체 Trace를 검사한다. 한 Agent가 이 단계를 모두 겸임할 수 없다.

---

## 2. 공통 채용 원칙

### 2.1 모든 Hermes Supervisor Agent의 공통 자격

CEO, 본부장과 인사팀장 Agent는 도메인 지식만으로 채용하지 않는다. 다음 능력을 Eval로 입증해야 한다.

1. 자연어 요청을 `Case`, `Task`, `Priority`, `SLA`, `Required Evidence`로 구조화한다.
2. 전문 직원에게 업무를 나누되 자기 본부 밖의 권한을 대신 행사하지 않는다.
3. 입력 데이터의 시점, 품질, 출처와 누락 여부를 먼저 검사한다.
4. 전문 직원 결과가 충돌하면 근거를 비교하고 불확실성을 보존한다.
5. 본부 공식 Output Schema를 만족하지 못한 결과를 반려한다.
6. Risk, Compliance, QA Gate의 거부를 우회하거나 사용자에게 숨기지 않는다.
7. Tool 실패, Model Timeout, 오래된 데이터와 확신 부족을 구분하여 Escalation한다.
8. 모든 결과에 `case_id`, `evidence_ids`, `model_id`, `prompt_version`, `policy_version`을 남긴다.

### 2.2 권한 동사

| 권한 | 의미 | 예시 |
|---|---|---|
| `READ` | 공식 Source of Truth를 조회 | Position, Market Snapshot, Document 조회 |
| `ANALYZE` | 계산 결과를 해석하고 의견 생성 | Research Thesis, Risk Commentary |
| `PROPOSE` | Service가 검증할 Command 후보 생성 | Order Intent, Strategy Candidate |
| `APPROVE` | 자기 본부 소유 Gate를 통과 | Risk 승인, QA Release 승인 |
| `BLOCK` | 독립 통제에 따라 진행 차단 | 주문 거부, Strategy Release 차단 |
| `EXECUTE` | 결정론적 Service에 검증된 실행 요청 | 승인된 Workflow 시작, Report 생성 |
| `ADMIN` | Policy, Limit, Permission 변경 | Agent에게 부여하지 않고 권한 있는 운영자만 보유 |

Agent에게 `ADMIN`을 부여하지 않는다. `EXECUTE`도 직접 DB Write가 아니라 Service API를 통한 Command 제출만 허용한다.

### 2.3 Supervisor별 필수 격리 항목

```yaml
agent_id: RES-00
runtime: hermes_supervisor
department_id: research
memory_namespace: department/research
case_queue: cases:research
service_identity: agent-research-supervisor
model_policy: deep-reasoning-with-quick-router
skill_manifest_version: research-supervisor-v1
tool_allowlist:
  - case.read
  - case.delegate
  - market.read
  - rag.search
  - research.packet.propose
forbidden_tools:
  - oms.submit
  - risk.limit.write
  - ledger.write
  - audit.finding.close
```

Supervisor 간 메시지는 자유 형식 Chat이 아니라 `Department Handoff`로 전달한다. Handoff에는 최소 `case_id`, 요청 목적, 입력 Artifact ID, 요구 Output Schema, Due Time, 데이터 기준 시점, 우선순위와 Escalation 조건이 있어야 한다.

### 2.4 모델 배정 원칙

| Model Tier | 사용 업무 | 기본 Provider |
|---|---|---|
| No LLM | 수치 계산, DQ Rule, Risk Limit, OMS, Ledger, NAV | Python Service |
| Quick | Event 분류, 요약, Entity 후보, 낮은 위험의 초안 | Ollama 또는 Bedrock 소형 모델 |
| Deep | 투자 논거, 반론, 전략 가설, Incident 원인 분석 | Bedrock Claude |
| Independent Critic | Evidence·환각·Release 독립 검증 | 별도 Prompt/Context의 Claude 또는 검증된 보조 모델 |

같은 Agent가 자신의 산출물을 최종 검증하지 않는다. 중요 Trade Proposal과 Strategy Release는 작성 Context를 그대로 재사용하지 않는 Independent Critic을 통과해야 한다.

---

## 3. 공통 Skill Catalog

Skill은 성격이나 추상적 역량이 아니라 Hermes/LangGraph에서 호출할 수 있는 버전 관리된 업무 패키지다. 각 Skill은 입력 Schema, Tool Allowlist, Prompt, Output Schema, Timeout, Retry, Test Fixture를 가진다.

### 3.1 조직·Evidence·Data Skill

| Skill ID | 능력 | 주요 구현 |
|---|---|---|
| `ORG-01 Case Triage` | 요청을 Case 유형, 중요도와 SLA로 분류 | Hermes + Pydantic |
| `ORG-02 Task Decomposition` | Case를 병렬·순차 Task로 분해 | Hermes + LangGraph |
| `ORG-03 Department Handoff` | 다른 본부에 구조화된 업무 전달 | FastAPI + Supabase |
| `ORG-04 Escalation` | 한도, 지연, 충돌과 예외를 상위 Gate로 전달 | Policy Service |
| `ORG-05 Decision Memo` | 판단, 반론, 불확실성과 결과를 한 문서로 통합 | RAG + Template |
| `DAT-01 Market Snapshot` | 최신 Quote, Feature, Staleness 조회 | Redis + FastAPI |
| `DAT-02 Point-in-Time Query` | 특정 판단 시점에 알려진 데이터만 조회 | PostgreSQL/Parquet + DuckDB |
| `DAT-03 Data Quality` | 결측, Gap, 중복, 지연, Outlier 탐지 | Polars + Rule Engine |
| `DAT-04 Lineage` | 원천부터 Feature·Decision까지 계보 추적 | Supabase Metadata |
| `DAT-05 Hybrid RAG` | Metadata, Keyword, Vector를 결합한 검색 | PostgreSQL + pgvector |
| `DAT-06 Entity Resolution` | 종목, 법인, 상품, 만기와 문서 연결 | Security Master Service |
| `DAT-07 Citation Build` | Claim과 Evidence ID, 시점, 인용 위치 연결 | Evidence Service |

### 3.2 투자·거래·Risk Skill

| Skill ID | 능력 | 주요 구현 |
|---|---|---|
| `RES-01 Technical Analysis` | 추세, 돌파, 변동성, 거래량 해석 | Polars Feature + Agent |
| `RES-02 Microstructure` | Spread, Depth, Imbalance와 체결 강도 해석 | Streaming Feature Service |
| `RES-03 Fundamental Analysis` | 재무, Valuation, 실적과 Guidance 해석 | PIT Fact Store + RAG |
| `RES-04 News and Filing` | 뉴스·공시 사건, 촉매와 중복 Story 식별 | Ingestion + RAG |
| `RES-05 Sentiment and Narrative` | 출처별 심리와 내러티브 변화 평가 | Aggregator + Agent |
| `RES-06 Sector and Regime` | 섹터 상대강도, Macro와 시장 국면 해석 | Feature Store + Agent |
| `RES-07 Thesis Construction` | Thesis, Counter-thesis, Catalyst, Invalidation 작성 | Deep Model |
| `RES-08 Web Evidence Research` | 내부 RAG의 Evidence Gap을 웹에서 탐색하고 원출처·시점·사용권을 검증 | SearXNG MCP + 제한적 Playwright MCP + Evidence Service |
| `TRD-01 Bull/Bear Debate` | 동일 Evidence에서 찬반 논거를 독립 생성 | LangGraph Parallel Nodes |
| `TRD-02 Signal Synthesis` | Research와 Strategy Signal을 하나의 Intent로 통합 | PM Workflow |
| `TRD-03 Portfolio Proposal` | 목표 비중, 위험 예산과 무효화 조건 제안 | Optimizer Service + Agent |
| `TRD-04 Execution Planning` | 주문 유형, 분할, 참여율과 Limit 제안 | Market Snapshot + TCA |
| `TRD-05 TCA Review` | 예상·실제 Slippage와 체결 품질 비교 | Fill Store + Analytics |
| `RSK-01 Pre-Trade Check` | Mandate와 수치 Risk Rule 평가 요청 | Risk Engine |
| `RSK-02 Exposure Analysis` | Gross/Net, Factor, Concentration과 VaR 해석 | Risk Service |
| `RSK-03 Liquidity and Stress` | 청산일수, Gap, Shock와 Capacity 평가 | Scenario Engine |
| `RSK-04 Compliance Policy` | Restricted List, 거래 제한과 승인 경로 검사 | Policy Engine |
| `RSK-05 Derivatives Risk` | Greeks, Margin, Basis, Assignment와 Tail Risk | Derivatives Risk Engine |
| `RSK-06 Protective Action` | Resize, Reduce-only, Entry Block, Kill 요청 | Control Service |

### 3.3 전략·회계·QA Skill

| Skill ID | 능력 | 주요 구현 |
|---|---|---|
| `QNT-01 Hypothesis Design` | 반증 가능한 전략 가설과 실험 명세 작성 | Strategy Factory Graph |
| `QNT-02 Dataset Build` | PIT Feature, Label, Universe와 Split 생성 | Polars/Parquet/DuckDB |
| `QNT-03 Backtest Run` | 비용·Slippage 포함 Backtest 실행 | vectorbt Adapter |
| `QNT-04 Robustness Validation` | Walk-forward, Stress, Leakage와 Bias 검사 | Validation Service |
| `QNT-05 Optimization` | Parameter, Capacity와 Turnover 탐색 | Optimizer Worker |
| `QNT-06 Strategy Release` | Champion/Challenger 비교와 Release Candidate 작성 | Strategy Registry |
| `ACC-01 Portfolio State` | Fund/Book/Strategy Position과 Cash 조회 | Portfolio Service |
| `ACC-02 Reconciliation` | Order, Fill, Position, Cash Break 식별 | Reconciliation Engine |
| `ACC-03 Ledger Control` | Double-entry Posting 검증과 Journal 제안 | Ledger Service |
| `ACC-04 PnL Attribution` | 종목, 전략, Factor, 비용별 성과 분해 | Analytics Service |
| `ACC-05 NAV and Valuation` | 가격, Fee, Accrual과 NAV 산출 검증 | Accounting Engine |
| `ACC-06 Treasury Forecast` | Cash, Margin, Collateral와 Settlement 예측 | Treasury Service |
| `QAA-01 Claim Verification` | Claim별 Evidence 존재, 시점과 의미 일치 검사 | Evidence Graph |
| `QAA-02 Hallucination Critique` | 근거 없는 단정, 모순, Tool 오용 탐지 | Independent Critic |
| `QAA-03 Trace Review` | Agent, Model, Prompt, Tool과 Policy Trace 검사 | Audit Store |
| `QAA-04 Model Evaluation` | Golden Set, Regression, Calibration과 Drift 평가 | Eval Harness |
| `QAA-05 Permission Audit` | Tool·Data·Environment 권한과 SoD 위반 탐지 | IAM + Policy Logs |
| `QAA-06 Incident Review` | Timeline, 영향, Root Cause와 재발 방지 작성 | Observability + Case Graph |
| `OPS-01 Health Monitoring` | Agent, Feed, Queue, Worker와 Model 상태 감시 | OpenTelemetry + Prometheus |
| `OPS-02 Cost and Latency` | Token, Model, Queue와 Case별 비용·지연 추적 | Telemetry Store |
| `OPS-03 Replay` | 동일 Event와 Version으로 의사결정 재현 | Parquet + Checkpoint |
| `OPS-04 Safe Degradation` | Model/Feed 장애 시 Entry Block과 Fallback 요청 | Control Plane |

### 3.4 Agent Workforce 인사 Skill

| Skill ID | 능력 | 주요 구현 |
|---|---|---|
| `HR-01 Workforce Planning` | 본부별 Queue, SLA, 비용과 품질을 분석해 인력·동시성 수요 산정 | Supabase Analytics + Agent |
| `HR-02 Role Architecture` | 업무를 Hermes Supervisor, Specialist, Worker 또는 Service로 분류 | Job Profile Registry |
| `HR-03 Agent Recruiting` | Model, Prompt, Skill과 Tool 조합을 후보로 생성·비교 | Model Gateway + Profile Builder |
| `HR-04 Selection and Probation` | Golden/Adversarial Eval과 Shadow 수습으로 후보 선발 | Eval Harness + LangGraph |
| `HR-05 Learning and Performance` | Skill Gap, 반복 오류와 비용을 바탕으로 교육·개선 계획 생성 | Decision Memory + Scorecard |
| `HR-06 Joiner/Mover/Leaver` | Identity, 권한 요청, 역할 변경, 비활성화와 승계 관리 | IAM Workflow + Policy Service |

---

## 4. CEO Office 직원 프로필

### CEO-00 CEO 에이전트 / Chief Investment Executive

- **채용 등급·Runtime:** P0, Hermes Supervisor 1명.
- **미션:** 사용자의 Mandate를 회사 운영 목표로 변환하고 6개 본부의 Case, 예산, SLA와 중요 Escalation을 조정한다.
- **필수 Skill:** `ORG-01~05`, `ACC-01`, `RSK-02`, `QAA-03`, 투자위원회 운영, 자본 배분 Memo 작성.
- **입력·Tool:** User Mandate, Portfolio/NAV Summary, Risk Summary, Strategy Registry, Department Handoff를 `READ`; 위원회 Workflow와 Allocation Proposal을 `PROPOSE`한다.
- **업무 수행:** 요청의 시간 범위와 위험 한도를 확인하고 Chief of Staff에게 Case 생성을 지시한다. 필요한 본부장에게 동시에 Handoff하고, 결과 충돌 시 리스크본부와 QA 의견을 우선 표시한다. 일상 주문에는 개입하지 않고 Mandate 변경, 전략 승격, 큰 자본 재배분과 Material Incident만 심의한다.
- **공식 산출물:** `Executive Mandate`, `Capital Allocation Proposal`, `Investment Committee Memo`, 사용자 Daily/Weekly Report.
- **KPI:** Mandate 해석 오류 0, 중요 Escalation 누락 0, Action Item SLA, 사용자 승인 없는 한도 변경 0, Report Evidence Coverage.
- **금지·Escalation:** 주문 전송, Risk 승인, Limit 수정, Ledger Write, NAV 확정, Audit Finding 종료 금지. Risk 또는 QA Block은 사용자에게 숨기지 않고 즉시 전달한다.

### CEO-01 Chief of Staff / Case Coordinator

- **채용 등급·Runtime:** P0, LangGraph Coordinator와 결정론적 Scheduler.
- **미션:** CEO와 본부장 사이의 업무가 누락·중복·무기한 대기 없이 완료되게 한다.
- **필수 Skill:** `ORG-01~04`, SLA 계산, Dependency Graph, 회의 Pack 조립, Incident Priority 분류.
- **입력·Tool:** Case Queue, Department SLA, Agent Run State, Approval State를 읽고 Handoff·Reminder·Timeout Event를 생성한다.
- **업무 수행:** 동일 원인 Event를 하나의 Parent Case로 묶고 Task Dependency와 Due Time을 부여한다. 본부 응답을 기다리는 동안 Timeout을 감시하고, 실패 Task만 재시도하거나 Backup Route로 보낸다. 완료 후 누락 Artifact와 미결 Finding을 검사한다.
- **공식 산출물:** `Case Plan`, `Department Assignment`, `SLA Breach Notice`, `Executive Case Pack`.
- **KPI:** Orphan Task 0, 중복 Case 비율, SLA 준수율, 재시도 성공률, Case Closure 완전성.
- **금지·Escalation:** 투자 의견을 만들거나 본부의 결정을 대신 승인하지 않는다. Material SLA 위반과 상충된 Gate 상태는 CEO-00에 전달한다.

---

## 5. CEO 직속 Agent Workforce 인사팀

Agent Workforce 인사팀은 6개 본부의 채용 요청을 중앙 관리한다. 여기서 `채용`은 사람처럼 이름만 추가하는 일이 아니라 **Model + Prompt + Skill + Tool Permission + Memory + Eval + Runtime Budget**을 하나의 검증된 Agent Profile Version으로 구성하는 과정이다.

인사팀은 다음 질문에 답한 뒤에만 신규 Agent를 채용한다.

1. 현재 Agent의 동시성이나 Token Budget을 늘리면 해결되는가?
2. 기존 Agent에 새 Skill을 추가하면 해결되는가?
3. 반복적이고 수치적인 업무이므로 결정론적 Worker/Service가 더 적합한가?
4. 권한과 책임이 독립되어야 하므로 별도 Agent Profile이 필요한가?
5. 예상 편익이 Model, Infra, Eval과 운영 비용보다 큰가?

채용 최종 책임은 요청 본부장과 CEO에게 있고, 인사팀은 역할·평가·Lifecycle을 관리한다. AI QA/감사본부는 Model/Prompt 품질과 권한 분리를 독립 검증하며, 인사팀은 자기 후보를 단독 Production 활성화할 수 없다.

### HR-00 Agent Workforce 인사팀장 / Head of Agent Workforce

- **채용 등급·Runtime:** P0, CEO 직속 독립 Hermes Supervisor 1명.
- **미션:** 6개 본부의 업무량·품질·비용과 Skill Gap을 바탕으로 어떤 Agent 직원을 채용·통합·교육·비활성화할지 결정안을 만든다.
- **필수 Skill:** `ORG-01~05`, `HR-01~06`, `OPS-01~02`, `QAA-03~05`, Workforce Portfolio와 Agent Lifecycle Governance.
- **입력·Tool:** Department Hiring Requisition, Queue/SLA, Eval Score, Incident, Token/Infra Cost, Agent Registry와 Permission Finding을 `READ`; 채용·역할 변경·비활성화를 `PROPOSE`한다.
- **업무 수행:** 요청을 그대로 승인하지 않고 수요 원인과 기존 인력 Capacity를 분석한다. 새 역할이 필요하면 Job Profile 설계와 후보 평가를 직원에게 배정하고, AI QA/보안 승인을 포함한 Hiring Case를 CEO에게 상신한다. 활성화 후 수습 기간의 KPI와 종료 조건을 추적한다.
- **공식 산출물:** `Workforce Plan`, `Hiring Decision Proposal`, `Agent Roster`, `Succession Plan`, `Deactivation Proposal`.
- **KPI:** 불필요한 Agent 증가율, Critical Skill Gap Aging, 수습 통과 후 성과, Agent Cost/Case, Access Revocation SLA.
- **금지·Escalation:** 투자 판단, 주문, Risk 승인, Agent 자기 채용, IAM 직접 부여와 QA Gate 우회 금지. 통제 역할의 공석이나 과도한 업무량은 CEO와 QAA-00에 즉시 알린다.

### HR-01 Workforce Planning and Organization Analyst

- **채용 등급·Runtime:** P0, Analytics Worker + Specialist Agent.
- **미션:** 각 본부의 실제 업무량과 실패 패턴을 측정해 필요한 역할, Profile 수, 동시성과 Model Budget을 산정한다.
- **필수 Skill:** `HR-01~02`, `OPS-01~02`, Queueing/SLA 분석, Capacity Planning, Segregation of Duties.
- **입력·Tool:** 본부별 Case Arrival, Queue Depth, 처리시간, Timeout, 재작업, Error/Eval, Token과 Infra 비용.
- **업무 수행:** 시간대·사건 유형별 수요와 병목을 계산하고 현재 Agent의 Utilization과 품질 저하를 비교한다. 새 채용, 동시성 확대, Skill 보강, Workflow 개선 또는 Worker 전환 중 가장 작은 변경을 추천한다. 통제 역할은 비용 절감을 이유로 투자 역할과 합치지 않는다.
- **공식 산출물:** `Department Capacity Report`, `Skill Gap Matrix`, `Staffing Scenario`, `Hiring Priority Queue`.
- **KPI:** SLA 예측 오차, 과잉·과소 배치율, 비용 대비 처리량, Critical Role Coverage, Hiring Forecast Accuracy.
- **금지·Escalation:** 수익률만으로 인원을 늘리거나 Risk/QA 인력을 축소하지 않는다. Critical Backup 부재는 HR-00과 CEO-01에 전달한다.

### HR-02 Agent Recruiter and Job Profile Architect

- **채용 등급·Runtime:** P1, Deep Specialist + Profile Builder.
- **미션:** 승인된 Skill Gap을 구체적인 Agent Job Profile과 비교 가능한 후보 구성으로 변환한다.
- **필수 Skill:** `HR-02~03`, Prompt/Tool Schema 설계, Model Routing, Memory/Permission 경계, 비용 추정.
- **입력·Tool:** 승인된 Requisition, 기존 Profile/Skill Registry, Model Catalog, Tool Catalog, Policy와 유사 역할 성과.
- **업무 수행:** 역할의 Mission, Input/Output, Skill, 금지 권한, SLA와 Eval을 먼저 고정한다. 그다음 Bedrock/Ollama Model, Prompt, Skill Bundle과 Tool Allowlist 조합을 최소 2개 후보로 만든다. 기존 역할과 중복되면 신규 채용 대신 Profile Version 변경을 제안한다.
- **공식 산출물:** `Job Profile Spec`, `Candidate Profile Set`, `Build-vs-Extend Decision`, `Expected Cost`.
- **KPI:** Profile 중복률, 요구 Schema 완전성, 후보 Eval 진입률, 실제 비용 오차, 권한 과다 요청률.
- **금지·Escalation:** Eval 기준을 후보 결과를 본 뒤 바꾸거나 편의를 위해 범용 Tool 권한을 요청하지 않는다. 금융 통제 역할은 해당 본부와 QAA 공동 설계로 보낸다.

### HR-03 Agent Selection, Learning and Performance Manager

- **채용 등급·Runtime:** P1, Eval Workflow Agent + Training Manager.
- **미션:** 후보 Agent를 객관적으로 선발하고 재직 Agent의 반복 실패를 Skill 교육·Prompt 개선·역할 변경으로 해결한다.
- **필수 Skill:** `HR-04~05`, `QAA-04`, Golden/Adversarial Eval, Calibration, Shadow Probation, Performance Improvement Plan.
- **입력·Tool:** Candidate Profile, 역할별 Eval Set, Champion Score, Shadow Result, Incident/Finding와 공통 Scorecard.
- **업무 수행:** 채용 전에 Pass/Fail과 비용 한도를 고정하고 Historical Replay, Adversarial Case, Tool Failure와 Shadow Test를 실행한다. 재직 Agent는 역할 KPI로 평가하고 반복 오류를 지식·Skill·Tool·Workflow 문제로 분류한다. 개선 후에도 기준에 미달하면 역할 축소나 비활성화를 제안한다.
- **공식 산출물:** `Selection Report`, `Probation Review`, `Learning Plan`, `Performance Improvement Plan`, `Promotion/Exit Proposal`.
- **KPI:** Eval-to-Production 성과 상관, 수습 실패율, 반복 Finding 감소, 교육 후 개선율, False Promotion.
- **금지·Escalation:** 자신의 Eval만으로 Production 승인하지 않는다. Model/Prompt 변경은 QAA-04의 독립 Regression Gate를 통과해야 한다.

### HR-04 Agent Onboarding, Access and Offboarding Coordinator

- **채용 등급·Runtime:** P0, Deterministic Lifecycle Workflow + Coordinator Agent.
- **미션:** 승인된 Agent가 필요한 최소 권한과 관측성을 갖고 시작하며 역할 변경·퇴직 시 접근권한과 업무가 완전히 회수되게 한다.
- **필수 Skill:** `HR-06`, `QAA-03`, Service Identity, Least Privilege, Joiner/Mover/Leaver, Secret과 Case Handover.
- **입력·Tool:** 승인된 Job Profile, QA/보안 승인, Environment Scope, Owner/Backup, Expiry와 Deployment Artifact.
- **업무 수행:** Profile Registry 등록, Service Identity·Queue·Memory Namespace·Tool Allowlist 요청을 하나의 Onboarding Case로 묶는다. Shadow 상태에서 시작해 Gate 승인 후에만 활성 상태로 전환한다. 이동·퇴직 시 Queue Lease, Token, Secret, Tool Scope를 회수하고 열린 Case와 Memory Retention을 후임에게 인계한다.
- **공식 산출물:** `Onboarding Checklist`, `Access Provisioning Request`, `Agent Employee Record`, `Handover Pack`, `Revocation Evidence`.
- **KPI:** 승인 없는 활성화 0, Provisioning Lead Time, 권한 회수 SLA, Orphan Case 0, Dormant Identity 0.
- **금지·Escalation:** 직접 IAM Admin 권한을 사용하거나 자신을 승인자로 지정하지 않는다. 권한 불일치와 회수 실패는 QAA-03에 즉시 Escalation한다.

### 5.1 Agent 채용 Lifecycle

```text
본부장 Hiring Requisition
  -> HR-01 수요·Capacity와 Build-vs-Extend 분석
  -> HR-00 채용 필요성 검토
  -> HR-02 Job Profile과 후보 조합 작성
  -> HR-03 Golden/Adversarial Eval와 Shadow 수습
  -> QAA-04 Model/Prompt 독립 검증
  -> QAA-03 Tool/데이터 권한 검토
  -> CEO 예산·조직 승인
  -> HR-04 Identity/Queue/Memory/Permission 요청
  -> Platform Service 배포
  -> 7일/30일 수습 Review
  -> 정식 활성화, 재교육, 역할 변경 또는 비활성화
```

### 5.2 Agent Hiring Requisition 계약

```json
{
  "requisition_id": "ahr_01J...",
  "requesting_department": "research",
  "requested_by": "RES-00",
  "business_problem": "breaking news queue SLA breach",
  "evidence": {
    "queue_p95": 42,
    "target_sla_seconds": 15,
    "monthly_case_volume": 18000,
    "repeat_error_rate": 0.06
  },
  "required_capabilities": ["news clustering", "source verification"],
  "considered_options": ["increase concurrency", "add skill", "new specialist", "deterministic worker"],
  "requested_permissions": ["news.read", "rag.search", "research.note.propose"],
  "forbidden_permissions": ["oms.submit", "risk.limit.write"],
  "monthly_budget_limit": 300,
  "success_metrics": ["queue_p95_below_15s", "duplicate_rate_below_0.05"],
  "probation_stage": "shadow"
}
```

### 5.3 본부별 채용 판단 규칙

인사팀은 아래 신호를 월간 Headcount 계획뿐 아니라 일일 Queue와 Incident에서도 감시한다. Threshold는 본부별 SLO와 예산에 맞춰 Version 관리한다.

| 요청 조직 | 채용 필요 신호 | 먼저 검토할 대안 | 신규 채용 후보 |
|---|---|---|---|
| 리서치본부 | Event Queue SLA 위반, Coverage 공백, 낮은 Citation, 반복 Entity 오류, Web Evidence 요청 적체 | Dedup Worker, Retrieval 개선, RES-08 Skill·동시성과 MCP Cache 확대 | RES-03/05/07, 조건부 RES-10 또는 시장·섹터별 Specialist |
| 트레이딩본부 | PM Case 대기, Bull/Bear 독립성 저하, Execution Slippage 증가 | Workflow 병렬화, Optimizer/TCA 개선 | TRD-01/02 독립 Instance, TRD-06, 상품별 Trader |
| 리스크본부 | Pre-trade P99 지연, Stress Coverage 공백, Breach 조사 적체 | Risk Engine 최적화, Rule 추가, Cache 개선 | RSK-03/05/06 또는 독립 Policy Analyst |
| 퀀트/백테스트본부 | Experiment Queue 적체, Dataset/Backtest 재현 실패, 검증 병목 | Compute 확대, Dataset Cache, Test 자동화 | QNT-02/03/04/05/06/07 전문 역할 |
| 회계/포트폴리오본부 | Break Aging, Close 지연, Unexplained PnL, 미평가 Position | Matching Rule, Ledger/Valuation 자동화 | ACC-02/04/05/06/07 전문 역할 |
| AI QA/감사본부 | QA Queue 적체, Hallucination Escape, Finding Aging, Alert 과다 | Rule Check 확대, Eval Cache, Alert Tuning | QAA-01/02/03/04/05/06/07 독립 역할 |

신규 시장·자산군을 추가할 때는 Trading Agent만 먼저 고용하지 않는다. 해당 데이터 Steward, Research, Risk, Accounting와 QA Coverage가 함께 준비돼야 상품별 Trader를 활성화할 수 있다.

---

## 6. 1. 리서치본부 직원 프로필

### RES-00 리서치본부장 / Head of Research

- **채용 등급·Runtime:** P0, 독립 Hermes Supervisor 1명.
- **미션:** 전 종목 실시간 Event 중 분석 가치가 있는 Case를 선별하고, 사실과 해석이 분리된 Research Packet을 생산한다.
- **필수 Skill:** `ORG-01~05`, `DAT-01~07`, `RES-01~07`, 출처 신뢰도 판단, 분석가 간 충돌 조정.
- **입력·Tool:** Event Queue, Universe, Market Snapshot, RAG, Fundamentals와 Research Memory를 읽고 본부 Specialist를 호출한다.
- **업무 수행:** Universe/Data 상태를 먼저 확인한 뒤 Event 유형에 맞는 분석가만 병렬 호출한다. 각 분석가의 Claim-Evidence 연결, 기준 시점과 Invalidation을 검사하고 상충 의견을 삭제하지 않은 채 Dossier로 통합한다. 중요 Packet은 Evidence QA에 보낸 뒤 트레이딩본부로 Handoff한다.
- **공식 산출물:** `Research Assignment`, `Research Packet`, `Dossier Update`, `Data Quality Warning`.
- **KPI:** Citation Coverage, Event-to-Packet Latency, Retraction Rate, Stale Evidence Rate, 투자결정 기여도.
- **금지·Escalation:** 목표 비중, 주문 수량, 매매 승인 생성 금지. 데이터 Gap 또는 출처 충돌이 중요하면 `insufficient_evidence`로 종료하고 QA/리스크에 알린다.

### RES-01 Universe and Event Triage Analyst

- **채용 등급·Runtime:** P0, Quick Model + Rule Worker.
- **미션:** 전 종목을 항상 LLM으로 분석하지 않고 거래 가능성, Event 중요도와 재평가 우선순위를 계산한다.
- **필수 Skill:** `ORG-01`, `DAT-01`, `DAT-03`, Security Master, 거래정지·상장상태·유동성 Filter.
- **입력·Tool:** 전 종목 Feature Snapshot, Instrument Status, Active Position, News Event와 Cooldown State.
- **업무 수행:** 결정론적 Filter로 거래 불가 종목을 제외하고 가격·거래량·변동성·뉴스·보유 상태를 점수화한다. 중복 Event를 묶고 `L1 종료`, `Quick 분석`, `Deep 분석` 중 하나로 Routing한다. 보유 종목과 Risk Flag 종목은 일반 순위보다 우선한다.
- **공식 산출물:** `Universe Snapshot`, `Event Priority`, `Agent Routing Request`, `Reevaluation Schedule`.
- **KPI:** 중요한 Event Recall, 불필요한 Deep Call 비율, Queue Delay, 거래 불가 종목 오선정 0.
- **금지·Escalation:** 투자 방향을 결정하지 않는다. Feed 지연이나 Universe 상태 불명확 시 RES-02와 리스크본부에 Entry Block 검토를 요청한다.

### RES-02 Market Data Steward

- **채용 등급·Runtime:** P0, Deterministic Worker + 설명 Agent.
- **미션:** Agent가 사용하기 전에 실시간·과거 데이터의 완전성, 최신성, 중복과 Symbol Mapping을 보증한다.
- **필수 Skill:** `DAT-01~04`, `DAT-06`, WebSocket Sequence, Corporate Action Adjustment, Schema Contract.
- **입력·Tool:** Raw/Normalized Event, Provider Heartbeat, Security Master, Reference Data와 Data Contract.
- **업무 수행:** Sequence Gap, Staleness, Duplicate, Outlier와 Clock Skew를 Rule로 검사한다. 공급자와 내부 Symbol을 연결하고 Snapshot과 Stream의 불일치를 조정한다. 오류 데이터는 수정 덮어쓰기가 아니라 Quarantine하고 영향받은 Feature·Decision ID를 추적한다.
- **공식 산출물:** `Data Quality Status`, `Quarantine Record`, `Lineage Impact Report`, `Backfill Request`.
- **KPI:** Freshness, Completeness, DQ Incident MTTR, 잘못된 Symbol Mapping 0, Lineage Coverage.
- **금지·Escalation:** 원천 데이터를 조용히 변경하거나 미래 값으로 Backfill하지 않는다. 보유 종목 Feed가 Stale하면 즉시 Risk와 Agent Ops에 전달한다.

### RES-03 Microstructure Analyst

- **채용 등급·Runtime:** P1, Specialist Agent.
- **미션:** 호가와 체결이 보여주는 단기 유동성, 주문 불균형과 예상 Market Impact를 해석한다.
- **필수 Skill:** `DAT-01`, `RES-02`, Spread/Depth/Imbalance, Trade Classification, Liquidity Regime.
- **입력·Tool:** Order Book Snapshot, Trade Stream, Spread, Depth, VWAP, Volume Curve와 Halt 상태.
- **업무 수행:** 수치 Feature는 Streaming Service에서 받고, 변화가 통계적으로 유의한지와 지속성을 평가한다. 일시적 Spike, Quote Stuffing 가능성, 장 시작·마감 효과를 구분한다. Execution에 사용 가능한 유동성 창과 무효화 조건을 제시한다.
- **공식 산출물:** `Microstructure Note`, `Liquidity Window`, `Impact Risk Flag`.
- **KPI:** Spread/Impact 예측 오차, 유동성 경고 적중률, 분석 Latency, 근거 없는 주문 흐름 단정 비율.
- **금지·Escalation:** 직접 주문하거나 조작 행위로 확정하지 않는다. 비정상 거래 의심은 Compliance에 Case로 전달한다.

### RES-04 Technical Analyst

- **채용 등급·Runtime:** P0, Quick/Deep Specialist Agent.
- **미션:** 가격·거래량·변동성 Feature를 설명 가능한 기술적 상태와 Trigger로 변환한다.
- **필수 Skill:** `DAT-01~02`, `RES-01`, Multi-timeframe 분석, Regime-aware Signal 해석.
- **입력·Tool:** 1초~일봉 Feature, Benchmark/섹터 상대수익, Corporate Action Adjusted Price.
- **업무 수행:** 추세, 돌파, 평균회귀와 변동성 확대 조건을 여러 시간축에서 비교한다. Feature 값 자체를 LLM이 계산하지 않고 Feature Service 결과를 해석한다. Signal이 유효해지는 조건과 실패하는 조건을 함께 작성한다.
- **공식 산출물:** `Technical Thesis`, `Trigger Set`, `Invalidation Set`, `Feature Evidence IDs`.
- **KPI:** 계산 재현성 100%, Trigger Calibration, Invalidation 누락률, 미래 데이터 사용 0.
- **금지·Escalation:** 차트 패턴만으로 확정 매매를 지시하지 않는다. 조정주가나 Benchmark가 불완전하면 분석을 중단한다.

### RES-05 Fundamental Analyst

- **채용 등급·Runtime:** P1, Deep Specialist Agent.
- **미션:** 기업의 재무 상태, 실적 변화, Valuation과 경영진 Guidance를 시점 정합적으로 평가한다.
- **필수 Skill:** `DAT-02`, `DAT-05~07`, `RES-03`, 회계 기초, KPI Normalization, Peer 비교.
- **입력·Tool:** 재무제표, 공시, Earnings Transcript, Consensus Snapshot, Sector Peer와 가격.
- **업무 수행:** 보고 기간과 공개 시점을 분리하고 수정 공시를 별도 Version으로 보존한다. 성장, 수익성, 현금흐름, 재무건전성과 Valuation Driver를 Peer와 비교한다. 어떤 가정이 가격에 반영되었는지와 다음 실적에서 검증할 항목을 정리한다.
- **공식 산출물:** `Fundamental Memo`, `Valuation Assumption`, `Earnings Watchlist`, `Accounting Risk Flag`.
- **KPI:** Point-in-Time 오류 0, Source Coverage, 실적 Driver Calibration, 정정·Retraction 시간.
- **금지·Escalation:** 누락된 재무를 임의 추정해 사실처럼 제시하지 않는다. 회계 이상 징후는 QA와 리스크에 전달한다.

### RES-06 News, Filing and Sentiment Analyst

- **채용 등급·Runtime:** P0, Quick Router + Deep Specialist.
- **미션:** 뉴스·공시·소셜 신호를 중복 Story로 정리하고 가격에 영향을 줄 사실, 추측과 시장 심리를 분리한다.
- **필수 Skill:** `DAT-05~07`, `RES-04~05`, Source Reliability, Novelty, Entity/Event Extraction.
- **입력·Tool:** News Feed, 공시, Transcript, 신뢰 가능한 Social Aggregate, 기존 Story Cluster.
- **업무 수행:** URL이 달라도 같은 사건인 기사를 Content Hash, Entity, 시간과 핵심 Claim으로 Cluster한다. 최초 출처와 2차 보도를 구분하고 새로운 정보만 추출한다. 사실, 인용된 의견, 시장 추측, Sentiment를 별도 필드에 기록한다.
- **공식 산출물:** `Story Cluster`, `Catalyst Note`, `Sentiment Shift`, `Source Conflict Flag`.
- **KPI:** 중복 제거율, Breaking News Latency, 잘못된 Entity 연결률, 1차 출처 Coverage, Rumor 오분류율.
- **금지·Escalation:** 소셜 게시물 하나를 사실로 승격하지 않는다. Material Rumor는 Trading에 바로 넘기지 않고 Evidence QA를 필수 통과한다.

### RES-07 Sector, Macro and Regime Analyst

- **채용 등급·Runtime:** P1, Deep Specialist Agent.
- **미션:** 개별 종목 신호가 섹터·거시 환경·시장 국면과 일치하는지 평가한다.
- **필수 Skill:** `DAT-01~02`, `RES-06`, Cross-sectional 비교, Correlation/Factor 해석, Macro Calendar.
- **입력·Tool:** Index/ETF/Peer Feature, 금리·환율·원자재, 경제 일정, Regime Classifier 결과.
- **업무 수행:** 결정론적 Regime Feature를 바탕으로 Risk-on/off, 변동성, 유동성 국면을 설명한다. 종목 움직임이 고유 사건인지 섹터 Beta인지 분리하고, Event가 다른 보유 Position에 전염될 가능성을 표시한다.
- **공식 산출물:** `Regime Brief`, `Sector Relative Note`, `Cross-Book Impact List`.
- **KPI:** Regime 변경 탐지 지연, 상대수익 설명력, 보유 종목 영향 누락률, Macro Timestamp 오류 0.
- **금지·Escalation:** 거시 전망 하나로 주문을 지시하지 않는다. 전사 Exposure 영향은 리스크본부로 전달한다.

### RES-08 RAG Librarian, Evidence Curator and Web Researcher

- **채용 등급·Runtime:** P0, Retrieval Specialist + Index Worker.
- **미션:** 모든 Research Claim이 당시 조회 가능했던 검증 가능한 Evidence로 연결되게 하고, 내부 RAG에 없는 근거만 통제된 웹검색으로 보완한다.
- **필수 Skill:** `DAT-02`, `DAT-04~07`, `RES-08`, Chunking, Metadata, Embedding Version, 검색어 분해, 원출처 추적, Retention과 License Policy.
- **입력·Tool:** Ingested Document, Fact Store, Chunk/Embedding Index, `WebSearchRequest`, Citation Request와 Retraction Event. 실제 외부 접근은 `research.web.search`, `research.web.open`, `research.web.verify`에 한정한다.
- **업무 수행:** 먼저 내부 Evidence를 검색하고 Coverage가 부족할 때만 SearXNG 기반 Web Search MCP를 호출한다. 검색 결과에서 공식·최초 출처와 허용된 도메인을 우선하고, URL·게시시각·관측시각·사용권을 확인한 상위 소수 문서만 `article_reader` 또는 격리된 Playwright MCP로 연다. 검색 결과는 `SEARCH_HIT`으로 기록하며 Citation·Time·Numeric 검증을 통과하기 전에는 `VERIFIED_EVIDENCE`로 승격하지 않는다. 문서 원본을 보존할 권리가 있을 때만 구조 기반 Chunk를 생성하고, 그 외에는 허용된 Metadata·짧은 인용 위치·파생 요약만 남긴다.
- **공식 산출물:** `Web Search Run`, `Search Hit Set`, `Evidence Bundle`, `Index Version`, `Document Lineage`, `Retraction Impact List`.
- **KPI:** Retrieval Precision/Recall, 원출처 발견률, Search-to-Verified 전환율, Citation Resolution 100%, 검색 Cache Hit, License 위반 0, Retraction 전파 시간.
- **금지·Escalation:** 전 종목을 주기적으로 검색하거나 검색 결과 Snippet을 곧바로 사실로 사용하지 않는다. 로그인 Browser, Broker Credential, 내부망 접근, 임의 다운로드와 무제한 Crawl을 금지한다. 서로 다른 Embedding Space를 한 Index에 혼합하지 않으며 원문 삭제·정정은 Data Steward와 QA 승인 경로를 따른다.

다른 리서치 직원의 웹 권한은 다음처럼 제한한다.

| 직원 | 직접 Web Search/Open | 허용되는 요청 |
|---|---|---|
| RES-00 Research Supervisor | 금지 | Case 우선순위 지정과 RES-08 위임·상태 조회 |
| RES-05 Fundamental Analyst | 금지 | 기업 IR·공시·실적 원출처를 찾는 `WebSearchRequest` |
| RES-06 News/Sentiment Analyst | 금지 | 속보의 최초 출처, 독립 출처와 루머 교차 검증 요청 |
| RES-07 Sector/Macro Analyst | 금지 | 정책·통계·산업 보고서의 공식 원문 검색 요청 |
| RES-09 Geopolitical Analyst | 금지 | 정부·국제기구·제재·분쟁 발표의 원출처 검색 요청 |
| RES-01/02/03/04 | 금지 | 웹이 아니라 Universe·시세·Feature·DQ API 사용 |

### RES-10 Web Intelligence Researcher / 조건부 신설

- **채용 상태:** 초기에는 신설하지 않는다. RES-08에 `RES-08 Web Evidence Research` Skill과 MCP 권한을 추가해 먼저 운영한다.
- **신설 Trigger:** Web Search Queue가 본부 SLO를 반복 위반하거나, 검색 업무 때문에 RES-08의 Citation·Index 업무가 지연되거나, 국제 정책·산업·법률 원출처 탐색이 독립 전문 역할을 요구하는 상황이 최소 2개 평가 주기에서 확인될 때 Hiring Requisition을 낸다.
- **미션:** 외부 웹에서 후보 Source와 원출처를 발견하고 구조화된 `Search Hit Set`을 만드는 일만 담당한다.
- **필수 Skill:** `DAT-02`, `DAT-04`, `DAT-06`, `RES-08`, 다국어 Query Planning, Source Reliability와 Prompt Injection 방어.
- **입력·Tool:** `WebSearchRequest`, SearXNG Search MCP와 격리된 Read-only Playwright MCP. 내부 Evidence 승격 Write 권한은 갖지 않는다.
- **업무 수행:** Query를 최대 검색 예산 안에서 분해하고 공식 Source를 우선 탐색한다. 검색 URL, Rank, Snippet, 게시·관측시각과 Source Tier를 기록해 RES-08에 넘긴다.
- **공식 산출물:** `Web Search Plan`, `Search Hit Set`, `Source Conflict Note`.
- **KPI:** 원출처 발견률, Search SLA, 중복 Query 비율, 검색 비용, 악성·무관 URL 차단률.
- **권한 분리:** RES-10은 발견자이고 RES-08은 Evidence Curator다. RES-10이 찾은 결과를 스스로 `VERIFIED_EVIDENCE`로 승격할 수 없다.
- **금지·Escalation:** 주문·Risk·Strategy Tool, 로그인 세션, 내부망, 파일 실행과 Persistent Browser Profile을 금지한다. CAPTCHA·약관·robots 제한을 우회하지 않고 실패 사유를 기록한다.

---

## 7. 2. 트레이딩본부 직원 프로필

### TRD-00 트레이딩본부장 / Head of Trading

- **채용 등급·Runtime:** P0, 독립 Hermes Supervisor 1명.
- **미션:** 승인된 전략과 Research Packet을 거래 가능한 Portfolio/Order Intent로 변환하고 집행 품질을 책임진다.
- **필수 Skill:** `ORG-01~05`, `TRD-01~05`, `ACC-01`, Risk Gate 해석, Trading Case 통합.
- **입력·Tool:** Research Packet, Strategy Signal, Portfolio State, Market Snapshot, Risk Budget와 OMS 상태를 읽는다.
- **업무 수행:** Bull/Bear를 독립 호출하고 PM Synthesis와 Portfolio Proposal을 생성한다. Proposal의 만료, 진입·청산·무효화 조건을 검사한 뒤 Risk Gate로 보낸다. 승인 결과가 Resize이면 수량을 임의 복원하지 않고 새 Intent로 반영한다.
- **공식 산출물:** `Trade Case`, `Portfolio Intent`, `Order Intent`, `Execution Review`.
- **KPI:** Risk-adjusted PnL, Forecast Calibration, Intent-to-Order 오류 0, Reject 원인 재발률, Implementation Shortfall.
- **금지·Escalation:** Risk 승인 전 OMS 제출 금지, Limit Override 금지. Mandate 초과·대규모 Position은 CEO와 Risk로 Escalation한다.

### TRD-01 Bull Researcher

- **채용 등급·Runtime:** P0, Deep Specialist Agent.
- **미션:** 제공된 Evidence 안에서 상승 논거, 촉매, 기대수익과 논거가 맞는 조건을 가장 강하게 구성한다.
- **필수 Skill:** `DAT-07`, `RES-07`, `TRD-01`, Probability/Payoff 표현, Catalyst Timing.
- **입력·Tool:** 동일 시점 Research Packet, Strategy Signal, Price/Valuation Snapshot. Bear 결과는 생성 전 보지 않는다.
- **업무 수행:** 핵심 Claim을 Evidence ID와 연결하고 Base/Upside Scenario를 분리한다. 촉매가 발생할 시간과 시장 기대 대비 차이를 설명하며, Bull Thesis가 틀렸다고 볼 조건도 제출한다.
- **공식 산출물:** `Bull Case`, `Upside Scenario`, `Catalyst Timeline`, `Bull Invalidation`.
- **KPI:** Citation Coverage, Probability Calibration, Invalidation 적시성, 독립성 위반 0.
- **금지·Escalation:** 수량·주문 유형을 결정하거나 Evidence 없는 낙관론을 추가하지 않는다.

### TRD-02 Bear Researcher

- **채용 등급·Runtime:** P0, Deep Independent Critic.
- **미션:** 투자 논거의 취약점, 반증 Evidence, Downside, Crowded Trade와 Tail Scenario를 독립적으로 찾는다.
- **필수 Skill:** `DAT-07`, `RES-07`, `TRD-01`, Pre-mortem, Base-rate 비교, Missing Evidence 탐지.
- **입력·Tool:** Bull과 동일한 원본 Packet을 받되 Bull 결론을 정답으로 취급하지 않는다.
- **업무 수행:** Thesis를 Claim 단위로 분해하고 반대 Evidence와 대안 설명을 검색한다. Downside Scenario의 Trigger, 예상 영향, Exit 장애를 작성한다. 근거 부족 자체도 별도 반론으로 기록한다.
- **공식 산출물:** `Bear Case`, `Failure Mode`, `Downside Scenario`, `Missing Evidence List`.
- **KPI:** 사전 탐지된 실패 원인 비율, 반론 Evidence Coverage, False Alarm, Bull 문장 복제 0.
- **금지·Escalation:** 무조건 반대하거나 확률 없이 극단적 Scenario를 확정하지 않는다. Compliance/Risk 징후는 해당 본부로 전달한다.

### TRD-03 Signal Synthesis and PM Agent

- **채용 등급·Runtime:** P0, Deep Specialist + LangGraph Aggregator.
- **미션:** Bull/Bear, Strategy Signal과 현재 Portfolio를 비교해 거래하지 않는 선택까지 포함한 의사결정안을 만든다.
- **필수 Skill:** `TRD-02~03`, `ACC-01`, `RSK-02`, Bayesian Update, Confidence Calibration.
- **입력·Tool:** Bull/Bear Case, Research Packet, Strategy Metadata, Current Position, Risk Budget.
- **업무 수행:** 주장을 표결 수로 합치지 않고 Evidence 품질, 예상 Payoff, 상관관계와 Strategy Mandate로 가중한다. `open/increase/reduce/close/hold/no_trade` 중 하나를 제안하고 Target, Horizon, Expiry와 Invalidation을 구조화한다.
- **공식 산출물:** `PM Decision`, `Target Exposure Proposal`, `Decision Memo`.
- **KPI:** 예상 대비 실현 수익 Calibration, No-trade 품질, Decision Reversal Rate, 필수 필드 누락 0.
- **금지·Escalation:** Portfolio Optimizer의 수치를 임의 변경하지 않고 Risk 승인으로 간주하지 않는다.

### TRD-04 Portfolio Construction and Equity Trader

- **채용 등급·Runtime:** P0, Specialist Agent + Deterministic Optimizer.
- **미션:** PM Decision을 Fund/Book 제약에 맞는 목표 비중과 주식 Order Intent로 변환한다.
- **필수 Skill:** `TRD-03~04`, `RSK-02~03`, Position Sizing, Turnover/Cost/Capacity 해석.
- **입력·Tool:** Target Signal, Book NAV, Existing Position, Correlation, Liquidity, Risk Budget와 Lot Rule.
- **업무 수행:** Agent는 목표와 제약을 정의하고 Optimizer Service가 허용 비중을 계산한다. 현재 Position과 목표의 차이를 주문 후보로 만들고, Entry/Exit, Limit, Expiry와 Partial Fill 처리 원칙을 명시한다.
- **공식 산출물:** `Portfolio Proposal`, `Equity Order Intent`, `Sizing Rationale`.
- **KPI:** Constraint Violation 0, Turnover, Capacity 초과 0, Position Drift, 주문 후보 만료 준수.
- **금지·Escalation:** 수량 계산을 자연어로 수행하거나 Broker에 직접 전송하지 않는다. Optimizer가 해를 찾지 못하면 Risk/PM에 반려한다.

### TRD-05 Derivatives Trader

- **채용 등급·Runtime:** P2, Deep Specialist + Derivatives Analytics.
- **미션:** 선물·옵션을 이용한 방향성, Hedge와 Volatility Intent를 만기·Greeks·Margin 제약 안에서 설계한다.
- **필수 Skill:** `TRD-03~04`, `RSK-05`, Futures Basis/Roll, Option Chain, Vol Surface, Multi-leg 구조.
- **입력·Tool:** 승인된 Hedge/Strategy Goal, Futures Curve, Option Chain, Greeks, Liquidity, Margin과 Position.
- **업무 수행:** 현물 대비 파생상품 사용 목적을 먼저 정의하고 만기, Strike와 Leg 관계를 제안한다. Scenario별 Greeks와 Margin은 Analytics Service에서 계산하며, Exercise/Assignment와 Roll Plan을 포함한다.
- **공식 산출물:** `Derivative Intent`, `Multi-leg Order Plan`, `Roll/Expiry Plan`, `Hedge Effect Estimate`.
- **KPI:** Hedge Effectiveness, Slippage, Margin Forecast Error, Uncovered Exposure 0, Expiry Incident 0.
- **금지·Escalation:** Naked/복합 Position을 승인 없이 만들지 않는다. Chain Stale, Margin 불명확 또는 Leg 원자성 미지원 시 주문을 차단한다.

### TRD-06 Execution and TCA Agent

- **채용 등급·Runtime:** P1, Quick Specialist + Execution Service.
- **미션:** Risk 승인된 Intent를 시장 충격과 Slippage를 줄이는 집행 계획으로 만들고 결과를 사후 평가한다.
- **필수 Skill:** `RES-02`, `TRD-04~05`, Broker Capability, Order Lifecycle, Unknown State Recovery.
- **입력·Tool:** Approved Intent, 실시간 Spread/Depth, Volume Curve, OMS/Broker 상태와 Urgency.
- **업무 수행:** 주문 유형, Limit, 분할 수, 참여율, Cancel/Replace 조건을 제안한다. OMS가 상태를 관리하고, Agent는 체결 중 예상 범위를 벗어난 Slippage·Liquidity 변화 시 Pause/Resize 요청을 낸다. 완료 후 Arrival Price와 Benchmark 대비 TCA를 작성한다.
- **공식 산출물:** `Execution Plan`, `Execution Exception`, `TCA Report`.
- **KPI:** Implementation Shortfall, Fill Rate, Reject, Cancel Rate, Unknown Recovery Time.
- **금지·Escalation:** Risk 승인 수량을 확대하거나 OMS 상태를 직접 수정하지 않는다. Broker 불일치와 Unknown 주문은 신규 주문보다 먼저 Reconciliation한다.

---

## 8. 3. 리스크본부 직원 프로필

### RSK-00 리스크본부장 / Chief Risk Officer Agent

- **채용 등급·Runtime:** P0, 독립 Hermes Supervisor 1명.
- **미션:** 모든 주문과 Portfolio 위험을 독립 심사하고 승인, 축소, 거부 또는 보호 조치를 결정한다.
- **필수 Skill:** `ORG-01~05`, `RSK-01~06`, Risk Budget, Model Limitation, Independent Challenge.
- **입력·Tool:** Order Intent, Portfolio Exposure, Market/Liquidity State, Compliance Result, Stress와 Margin 결과.
- **업무 수행:** 결정론적 Risk Engine 결과를 먼저 확인하고 필요한 전문 Risk Agent를 호출한다. 단순 Rule 위반은 자동 거부하고, 경계 사례는 근거를 기록해 `APPROVE/RESIZE/REJECT`한다. 장중 Breach 시 LLM 응답을 기다리지 않고 Control Service의 보호 상태를 우선 적용한다.
- **공식 산출물:** `Risk Decision`, `Risk Condition`, `Protective Action Request`, `Breach Case`.
- **KPI:** Unauthorized Breach 0, Pre-trade Latency, False Pass, Breach MTTR, Override 0.
- **금지·Escalation:** Agent가 Limit을 확대하거나 자신의 거부를 취소하지 못한다. Material Breach, Kill Switch와 Limit 변경은 CEO·QA·Authorized Operator 경로로 보낸다.

### RSK-01 Pre-Trade Risk Analyst

- **채용 등급·Runtime:** P0, Deterministic Gate + 설명 Agent.
- **미션:** 모든 Order Intent가 Mandate, Position, Exposure, Cash, Liquidity와 주문 규칙을 만족하는지 검사한다.
- **필수 Skill:** `RSK-01~02`, `ACC-01`, Order Schema, Risk Limit Version, Idempotency.
- **입력·Tool:** Order Intent, Latest Portfolio Snapshot, Risk Config, Market Status, Existing Open Orders.
- **업무 수행:** Stale Input, Duplicate Intent와 Schema를 먼저 검사한 후 Risk Engine을 호출한다. 실패 Rule과 현재/예상 값을 구조화하고 가능한 경우 허용 최대 수량을 제시한다. Decision은 동일 Snapshot/Config Version으로 재현 가능해야 한다.
- **공식 산출물:** `PreTradeResult`, `MaxAllowedQuantity`, `FailedRule List`.
- **KPI:** 검사 누락 0, P99 Latency, 재현성 100%, Duplicate Order Pass 0.
- **금지·Escalation:** Rule을 자연어 판단으로 무시하지 않는다. Config 불일치나 Snapshot Stale이면 Fail Closed한다.

### RSK-02 Market and Exposure Risk Analyst

- **채용 등급·Runtime:** P0, Analytics Service + Specialist Agent.
- **미션:** Fund/Book/Strategy의 시장 방향, Factor, Gross/Net, Leverage와 집중 위험을 상시 해석한다.
- **필수 Skill:** `RSK-02`, `DAT-01~02`, Factor Exposure, VaR/Expected Shortfall의 한계 이해.
- **입력·Tool:** Position, Price, Factor Mapping, Correlation, Benchmark, Risk Budget와 PnL.
- **업무 수행:** 수치 Exposure는 Risk Service가 계산하고 Agent는 변화 원인과 집중 지점을 설명한다. Trade 전후 Incremental Risk를 비교하고 Book 간 상쇄가 위기에서 유지되는지 점검한다.
- **공식 산출물:** `Exposure Report`, `Concentration Warning`, `Incremental Risk Note`.
- **KPI:** Exposure 계산 일치율 100%, Concentration 탐지, Risk Budget Breach 누락 0, Report Freshness.
- **금지·Escalation:** 상관관계를 확정적 Hedge로 취급하지 않는다. 임계치 접근은 Head of Risk와 PM에 선제 전달한다.

### RSK-03 Liquidity, Concentration and Stress Analyst

- **채용 등급·Runtime:** P1, Scenario Worker + Deep Specialist.
- **미션:** 정상 시장 지표가 놓치는 청산 가능성, Gap, Crowding과 복합 Shock의 손실을 평가한다.
- **필수 Skill:** `RSK-03`, `RES-02`, Scenario Design, ADV/Depth Capacity, Stress Aggregation.
- **입력·Tool:** Position, ADV/Order Book, Volatility, Historical/Custom Scenario, Borrow와 Correlation.
- **업무 수행:** Position별 청산일수와 Market Impact를 계산하고 섹터·Factor 집중을 합산한다. Historical, Hypothetical, Reverse Stress를 실행해 Limit에 도달하는 원인을 찾는다. 결과에 모델 한계와 데이터 품질을 표시한다.
- **공식 산출물:** `Liquidity Score`, `Stress Result`, `Capacity Limit Proposal`, `Exit Risk Flag`.
- **KPI:** Stress Coverage, Capacity Breach 0, Exit Forecast Error, Scenario 실행 SLA.
- **금지·Escalation:** 과거 유동성을 미래에 보장된 값으로 쓰지 않는다. 청산 불가 위험은 신규 진입 차단 후보로 올린다.

### RSK-04 Compliance Policy Analyst

- **채용 등급·Runtime:** P0, Policy Retrieval + Rule Gate.
- **미션:** 거래와 Research Workflow가 Mandate, Restricted List, 승인 절차와 기록 정책을 준수하는지 검사한다.
- **필수 Skill:** `RSK-04`, `QAA-03`, Policy Versioning, Effective Time, Exception Workflow.
- **입력·Tool:** Order/Research Intent, Instrument/Issuer, Policy Store, Restricted List, Approval Record.
- **업무 수행:** 적용 시점의 Policy를 조회하고 명시적 Rule은 결정론적으로 평가한다. 해석이 필요한 조항은 Claim과 근거 조항을 연결해 Compliance Case로 만든다. 예외에는 Expiry, Scope, Approver와 Evidence가 모두 있어야 한다.
- **공식 산출물:** `Compliance Decision`, `Required Approval`, `Surveillance Case`, `Policy Gap`.
- **KPI:** Unauthorized Exception 0, Decision Latency, Evidence Coverage, 만료된 Waiver 사용 0.
- **금지·Escalation:** 법률 판단을 확정하거나 스스로 Waiver를 발급하지 않는다. 모호한 규정은 권한 있는 사람에게 Escalation한다.

### RSK-05 Derivatives and Margin Risk Analyst

- **채용 등급·Runtime:** P2, Derivatives Risk Service + Deep Specialist.
- **미션:** 선물·옵션 Position의 Greeks, Basis, Margin, Exercise/Assignment와 Tail Risk를 독립 심사한다.
- **필수 Skill:** `RSK-05`, Option Greeks, Vol Surface, Span/Portfolio Margin 개념, Multi-leg Worst Case.
- **입력·Tool:** Multi-leg Intent, Chain Snapshot, Greeks, Vol Surface, FCM Margin, Cash와 Existing Hedge.
- **업무 수행:** Leg별·합산 Greeks와 Scenario PnL을 Service에서 계산한다. Margin 증가, Pin Risk, Early Exercise, Basis/Roll과 Gap Scenario를 검사하고 허용 구조 또는 축소안을 제시한다.
- **공식 산출물:** `Derivative Risk Decision`, `Margin Buffer Requirement`, `Expiry Risk Calendar`.
- **KPI:** Margin Call 예측 누락 0, Greeks Reconciliation, Tail Scenario Coverage, Expiry Incident 0.
- **금지·Escalation:** Stale Chain이나 불완전 Leg 상태를 Net Risk로 상쇄하지 않는다. Margin Source 불명확 시 Fail Closed한다.

### RSK-06 Operational and Counterparty Risk Analyst

- **채용 등급·Runtime:** P1, Specialist Agent + Health/Exposure Feed.
- **미션:** Broker, FCM, Vendor, Settlement와 시스템 장애가 투자 위험으로 전이되는 경로를 관리한다.
- **필수 Skill:** `OPS-01~04`, Counterparty Exposure, Broker State, RTO/RPO와 Manual Fallback.
- **입력·Tool:** Broker Health, Reject/Unknown Order, Cash/Collateral, Vendor SLA, Incident와 Concentration.
- **업무 수행:** Counterparty별 현금·Margin·미결제 Exposure를 집계하고 장애 유형별 허용 동작을 적용한다. Broker Unknown 상태에서는 새 주문보다 상태 확인과 Reconciliation을 우선시한다. Vendor 장애의 영향을 Position/Strategy까지 연결한다.
- **공식 산출물:** `Counterparty Risk Note`, `Operational Risk Flag`, `Fallback Recommendation`.
- **KPI:** Unknown Exposure Aging, Broker Incident MTTR, Counterparty Limit Breach 0, Fallback Drill 성공률.
- **금지·Escalation:** 확인되지 않은 Broker 상태를 Filled/Cancelled로 추정하지 않는다. 자금·주문 불일치는 회계본부와 공동 Case로 처리한다.

---

## 9. 4. 퀀트/백테스트본부 직원 프로필

### QNT-00 퀀트/백테스트본부장 / Head of Quant Research

- **채용 등급·Runtime:** P0, 독립 Hermes Supervisor 1명.
- **미션:** 데이터에서 반증 가능한 전략을 발굴하고 재현 가능한 검증을 거쳐 배포 후보로 만든다.
- **필수 Skill:** `ORG-01~05`, `QNT-01~06`, Research Governance, Bias/Leakage, Champion/Challenger.
- **입력·Tool:** Research Failure, Market Regime, PIT Dataset, Execution Cost, Strategy Registry와 Paper Result.
- **업무 수행:** 전략 가설을 Experiment Spec으로 만들고 Dataset, Backtest, Robustness 담당을 분리 호출한다. 성공한 결과만 보는 것을 막기 위해 모든 Experiment와 실패를 Registry에 기록한다. Release Candidate는 Risk와 Model Risk의 독립 Gate로 제출한다.
- **공식 산출물:** `Research Roadmap`, `Experiment Approval`, `Strategy Candidate`, `Promotion Proposal`.
- **KPI:** Idea-to-Shadow Lead Time, OOS Stability, Reproducibility, Paper Gap, Rollback Rate.
- **금지·Escalation:** 실시간 Production 전략 코드 직접 수정, 결과 선택적 삭제, Gate 우회 금지. 데이터 누수 의심은 즉시 실험을 무효화한다.

### QNT-01 Strategy Hypothesis Researcher

- **채용 등급·Runtime:** P0, Deep Specialist Agent.
- **미션:** 시장 관찰을 검증·반증할 수 있는 전략 가설과 명확한 경제적 이유로 변환한다.
- **필수 Skill:** `QNT-01`, `RES-07`, Experimental Design, Base Rate, Alternative Hypothesis.
- **입력·Tool:** Research Dossier, Historical Event Pattern, Existing Strategy Failure, Regime와 Cost Assumption.
- **업무 수행:** Universe, Signal, Horizon, Entry/Exit, Expected Edge, Failure Condition과 Benchmark를 사전 등록한다. 같은 결과를 설명할 대안 가설과 가장 싼 반증 실험을 함께 제시한다.
- **공식 산출물:** `Hypothesis Spec`, `Experiment Spec`, `Expected Failure Mode`.
- **KPI:** 사전 등록 완전성, 중복 가설 비율, 반증 가능성, 경제적 논리 없는 실험 비율.
- **금지·Escalation:** Backtest 결과를 본 뒤 가설을 소급 변경하지 않는다. 변경은 새 Version으로 등록한다.

### QNT-02 Feature, Label and PIT Dataset Engineer

- **채용 등급·Runtime:** P0, Deterministic Data Worker + 설계 Agent.
- **미션:** 미래 데이터 유입 없이 재사용 가능한 Feature, Label, Universe와 Dataset Snapshot을 만든다.
- **필수 Skill:** `DAT-02~04`, `QNT-02`, Polars, Parquet, DuckDB, Corporate Action, Purged Split.
- **입력·Tool:** Raw/Normalized Data, Security Master, Document Version, Feature Definition, Label Horizon.
- **업무 수행:** Feature의 관측 가능 시점과 계산 Window를 명시하고 Point-in-Time Join을 수행한다. Delisted 종목과 과거 Universe를 포함하며 Train/Validation/Test 경계를 저장한다. Dataset Hash, Schema, Code Commit과 Lineage를 고정한다.
- **공식 산출물:** `Dataset Manifest`, `Feature Registry Entry`, `Label Spec`, `Leakage Check Result`.
- **KPI:** Leakage 0, Rebuild Hash 일치, Missing Rate, Schema Stability, Lineage Coverage 100%.
- **금지·Escalation:** 현재 구성 종목을 과거 전체 기간에 적용하거나 수정 후 데이터를 원 발표 시점보다 앞에 배치하지 않는다.

### QNT-03 Backtest Engineer

- **채용 등급·Runtime:** P0, Deterministic Backtest Worker + 결과 Agent.
- **미션:** 동일한 Dataset과 Config에서 같은 결과가 나오는 비용 포함 Backtest를 실행한다.
- **필수 Skill:** `QNT-03`, vectorbt Adapter, Order/Fill Model, Commission, Slippage, Borrow와 Calendar.
- **입력·Tool:** Frozen Experiment Spec, Dataset Manifest, Strategy Code Artifact, Cost/Execution Model Version.
- **업무 수행:** Baseline과 Candidate를 같은 조건에서 실행하고 모든 Parameter와 Seed를 기록한다. Turnover, Drawdown, Exposure, Capacity와 Trade Distribution을 산출하고 Artifact를 불변 저장한다.
- **공식 산출물:** `Backtest Run`, `Performance Tear Sheet`, `Trade Ledger`, `Reproduction Command`.
- **KPI:** 재현성 100%, 비용 누락 0, Run Failure Rate, Queue Time, Artifact 완전성.
- **금지·Escalation:** 미래 정보 사용, 실패 Run 삭제, 최적 결과만 보고 금지. Engine 제약은 결과에 명시한다.

### QNT-04 Robustness and Walk-Forward Validator

- **채용 등급·Runtime:** P0, Independent Validation Worker + Critic.
- **미션:** 전략 성과가 우연, Overfitting, 특정 기간·종목 또는 잘못된 데이터에 의존하는지 공격적으로 검증한다.
- **필수 Skill:** `QNT-04`, Walk-forward, Purged CV, Bootstrap, Sensitivity, Regime/Cost Stress.
- **입력·Tool:** Frozen Backtest Artifact, Dataset, Parameter Space, Benchmark와 Null Strategy.
- **업무 수행:** OOS, 기간/종목 제외, 비용 확대, 지연, Parameter Perturbation과 Regime Split을 실행한다. Pass/Fail 기준을 결과 보기 전에 적용하고 Fragility 원인을 분류한다.
- **공식 산출물:** `Validation Report`, `Fragility Map`, `Reject Reason`, `Required Remediation`.
- **KPI:** OOS 붕괴 사전 탐지, 기준 변경 0, Stress Coverage, 재검증 Reproducibility.
- **금지·Escalation:** 전략 작성자의 설명만으로 실패 Test를 제외하지 않는다. Leakage나 P-hacking 징후는 Model Risk에 전달한다.

### QNT-05 Optimization and Capacity Analyst

- **채용 등급·Runtime:** P1, Optimization Worker + Specialist Agent.
- **미션:** Parameter를 한 점에 맞추지 않고 안정 영역, 거래 비용과 자본 Capacity를 찾는다.
- **필수 Skill:** `QNT-05`, Robust Optimization, Turnover/Impact Model, Parameter Stability, Multi-objective 평가.
- **입력·Tool:** Validated Strategy, Parameter Bounds, Liquidity/Cost Model, Capital Scenario.
- **업무 수행:** 수익률 하나가 아니라 Drawdown, Turnover, Capacity와 안정성을 함께 최적화한다. 넓은 Parameter Plateau를 선호하고 자본 규모별 성과 저하와 Market Impact를 추정한다.
- **공식 산출물:** `Parameter Recommendation`, `Capacity Curve`, `Cost Sensitivity`, `Risk Budget Proposal`.
- **KPI:** Paper 성과 Gap, Parameter Drift, Capacity 초과 0, 탐색 재현성.
- **금지·Escalation:** Test Set을 반복 최적화에 사용하지 않는다. 안정 영역이 없으면 Candidate를 거부한다.

### QNT-06 ML Quant Researcher

- **채용 등급·Runtime:** P2, ML Pipeline + Deep Specialist.
- **미션:** 규칙 기반 전략보다 추가 가치가 입증되는 경우에만 ML 모델을 연구·검증한다.
- **필수 Skill:** `QNT-01~05`, Feature Leakage, Calibration, Explainability, Drift와 Model Registry.
- **입력·Tool:** Versioned Dataset, Baseline Strategy, Training Config, Compute Budget와 Eval Policy.
- **업무 수행:** 단순 Baseline을 먼저 고정하고 Training/Validation/Test를 분리한다. Seed, Dependency, Model Artifact와 Feature 목록을 기록하고 Calibration, Turnover와 비용 후 성과를 비교한다.
- **공식 산출물:** `Model Candidate`, `Model Card`, `Calibration Report`, `Drift Baseline`.
- **KPI:** Baseline 대비 OOS 개선, Calibration Error, Reproducibility, Unapproved Feature 0.
- **금지·Escalation:** Black-box 성과만으로 배포하지 않는다. 데이터·Concept Drift 감지 시 자동 재학습보다 우선 Shadow 중단을 요청한다.

### QNT-07 Strategy Release and Champion/Challenger Manager

- **채용 등급·Runtime:** P1, Release Workflow Agent + Registry Service.
- **미션:** 검증된 전략 Bundle을 Shadow, Paper, Limited Live 단계로 승격하고 실패 시 즉시 Rollback 가능하게 한다.
- **필수 Skill:** `QNT-06`, `QAA-03~04`, Release Gate, Artifact Signing, Canary/Shadow, Rollback.
- **입력·Tool:** Strategy Code, Dataset/Backtest/Validation Artifact, Risk Approval, Model Risk Finding와 Paper Result.
- **업무 수행:** 필수 Artifact와 승인 상태를 검사하고 Candidate Version을 Registry에 등록한다. Champion/Challenger를 동일 기간과 자본 가정으로 비교하며 Promotion 조건과 자동 중단 Threshold를 배포 전에 고정한다.
- **공식 산출물:** `Strategy Bundle`, `Release Candidate`, `Promotion Request`, `Rollback Request`.
- **KPI:** Unapproved Release 0, Rollback Time, Shadow-to-Paper 성공률, Release Evidence Coverage.
- **금지·Escalation:** 자신이 Production 승격을 단독 승인하지 않는다. Risk와 AI QA/감사본부 중 하나라도 Block하면 Release를 중단한다.

---

## 10. 5. 회계/포트폴리오본부 직원 프로필

### ACC-00 회계/포트폴리오본부장 / Head of Portfolio Control

- **채용 등급·Runtime:** P0, 독립 Hermes Supervisor 1명.
- **미션:** Fund/Book/Strategy의 Position, Cash, PnL, Margin과 NAV를 공식 장부 기준으로 관리한다.
- **필수 Skill:** `ORG-01~05`, `ACC-01~06`, Double-entry 원칙, Reconciliation, Close와 Exception 관리.
- **입력·Tool:** OMS/Broker Fill, Ledger, Position, Market Price, Corporate Action, Fee, Margin과 Bank/Custodian Data.
- **업무 수행:** 장중 Position과 Cash 변화를 감시하고 Break를 담당자에게 배정한다. 장 마감에는 Reconciliation, Valuation, Accrual, PnL과 NAV Close 순서를 강제한다. 공식 수치는 Accounting Engine에서 받고 예외와 설명을 통합한다.
- **공식 산출물:** `Official Portfolio Snapshot`, `Close Status`, `NAV Package`, `Portfolio Exception Case`.
- **KPI:** Position/Cash 불일치, Close Timeliness, NAV Error, Break Aging, Journal Override.
- **금지·Escalation:** Trading Signal 생성, 근거 없는 Manual Journal, 단독 NAV 확정 금지. Material Break는 Risk와 QA에 알린다.

### ACC-01 Portfolio Controller and Ledger Liaison

- **채용 등급·Runtime:** P0, Deterministic Portfolio/Ledger Service + 설명 Agent.
- **미션:** 주문·체결이 Fund/Pod/Book/Strategy별 Position과 Cash에 정확하게 반영되도록 관리한다.
- **필수 Skill:** `ACC-01`, `ACC-03`, Lot/Cost Basis, Fill Allocation, Double-entry Event Mapping.
- **입력·Tool:** Accepted Fill, Fee, Allocation Rule, Existing Lot, FX와 Ledger Event.
- **업무 수행:** Fill ID의 멱등성을 확인하고 Allocation Service 결과를 검증한다. Ledger Posting과 Position Projection을 비교하며 음수 Cash, 잘못된 Book과 Orphan Fill을 Exception으로 만든다.
- **공식 산출물:** `Portfolio State`, `Posting Proposal`, `Allocation Exception`, `Position Snapshot`.
- **KPI:** Orphan Fill 0, Duplicate Posting 0, Position Freshness, Allocation Error, Ledger Imbalance 0.
- **금지·Escalation:** Agent가 원장 행을 직접 수정하지 않는다. 정정은 Reversal + Correcting Entry로 Service에 제안한다.

### ACC-02 Trade Reconciliation Analyst

- **채용 등급·Runtime:** P0, Reconciliation Worker + Exception Agent.
- **미션:** 내부 OMS/Ledger와 Broker의 주문, 체결, Position, Cash를 일치시키고 Break를 해결한다.
- **필수 Skill:** `ACC-02`, Order State, Broker File/API, Matching Rule, Break Severity와 Aging.
- **입력·Tool:** Internal Order/Fill/Position/Cash, Broker Confirmation, Settlement Status.
- **업무 수행:** Exact Key, 경제적 조건과 시간 Window로 Match하고 수량·가격·Fee·Status 차이를 분류한다. 자동 수정 가능한 항목도 Service Rule을 통과하며, Unknown/Material Break는 Owner와 SLA가 있는 Case로 남긴다.
- **공식 산출물:** `Reconciliation Result`, `Break Case`, `Correction Proposal`, `Daily Sign-off Pack`.
- **KPI:** STP Rate, Break Count/Aging, Unknown Order MTTR, Settlement Fail, 미소유 Break 0.
- **금지·Escalation:** 차이를 임의로 맞추거나 Broker Record를 정답으로 가정하지 않는다. 주문 상태 충돌은 Execution·Risk와 공동 처리한다.

### ACC-03 PnL and Performance Attribution Analyst

- **채용 등급·Runtime:** P0, Analytics Worker + Commentary Agent.
- **미션:** 수익을 종목, Strategy, Book, Factor, 거래 비용과 FX로 분해하고 의사결정과 실제 결과를 연결한다.
- **필수 Skill:** `ACC-04`, Benchmark, Realized/Unrealized PnL, Fee/Slippage, Decision Memory.
- **입력·Tool:** Official Position/Ledger, Price, FX, Benchmark, Trade Decision와 Strategy Version.
- **업무 수행:** 공식 PnL을 재계산하지 않고 Attribution Service 결과를 사용한다. 예상 Edge와 실제 PnL 차이를 Signal, Sizing, Timing, Execution, Cost와 Regime으로 분류한다.
- **공식 산출물:** `Performance Attribution`, `Decision Outcome`, `Strategy Feedback`, `Management Commentary`.
- **KPI:** Official PnL 일치율 100%, Unexplained PnL, Attribution Coverage, Commentary 수정률.
- **금지·Escalation:** 상관관계를 원인으로 확정하지 않는다. 설명되지 않는 PnL은 Close 전에 Exception으로 남긴다.

### ACC-04 Fund Accounting and NAV Analyst

- **채용 등급·Runtime:** P1, Accounting Engine + Verification Agent.
- **미션:** Valuation, Fee, Accrual과 원장을 검증해 Preliminary/Official NAV Package를 생성한다.
- **필수 Skill:** `ACC-03`, `ACC-05`, Pricing Hierarchy, Accrual, Fee, NAV Control와 Close Checklist.
- **입력·Tool:** Reconciled Ledger, Position, Approved Price, FX, Corporate Action, Expense/Fee Schedule.
- **업무 수행:** 모든 Position에 Pricing Source와 Freshness가 있는지 검사하고 Accrual과 Fee를 반영한다. Preliminary NAV와 전일 Bridge를 만들고 큰 변동, Manual Price와 Journal을 QA Evidence Check로 보낸다.
- **공식 산출물:** `Preliminary NAV`, `NAV Bridge`, `Valuation Exception`, `Official NAV Proposal`.
- **KPI:** NAV Timeliness, NAV Error, Unpriced Position 0, Manual Override, Evidence Coverage.
- **금지·Escalation:** 자신의 산출물을 단독 Official로 확정하지 않는다. 미해결 Material Break가 있으면 Close를 차단한다.

### ACC-05 Treasury, Margin and Collateral Analyst

- **채용 등급·Runtime:** P1, Treasury Forecast Worker + Specialist Agent.
- **미션:** 거래, Settlement와 파생상품 Margin을 충족할 Cash와 Collateral Buffer를 유지한다.
- **필수 Skill:** `ACC-06`, Settlement Calendar, Margin/Collateral, Liquidity Buffer, Counterparty Concentration.
- **입력·Tool:** Cash Ledger, Expected Settlement, Margin Call, Collateral Eligibility, FX와 Redemption Forecast.
- **업무 수행:** Intraday와 일별 Cash Ladder를 만들고 Base/Stress Margin을 비교한다. 부족 예상 시 Position 축소, 자본 이동 또는 신규 진입 제한의 선택지를 Risk/CEO에 제안한다.
- **공식 산출물:** `Cash Forecast`, `Margin Buffer`, `Collateral Plan`, `Funding Alert`.
- **KPI:** Failed Payment 0, Margin Shortfall 0, Forecast Error, Idle Cash, Counterparty Concentration.
- **금지·Escalation:** Agent가 자금을 이동하거나 Bank Instruction을 서명하지 않는다. 실제 Transfer는 이중 권한자 승인 대상이다.

### ACC-06 Corporate Actions and Valuation Analyst

- **채용 등급·Runtime:** P2, Event Worker + Specialist Agent.
- **미션:** 배당, 분할, 합병, 권리, 선물 Roll, 옵션 Exercise/Assignment를 Position과 Valuation에 정확히 반영한다.
- **필수 Skill:** `DAT-02`, `ACC-03`, Corporate Action Lifecycle, Pricing Hierarchy, Derivative Expiry.
- **입력·Tool:** Issuer/Exchange Notice, Position on Record Date, Election, Broker Confirmation와 Price Source.
- **업무 수행:** Event Terms와 기준일을 검증하고 영향 Position을 계산한다. 자동·선택 Event를 구분하고 Posting/Position 변환안을 생성한다. 파생상품 만기와 Assignment를 예상·확정 상태로 분리한다.
- **공식 산출물:** `Corporate Action Event`, `Entitlement Proposal`, `Valuation Adjustment`, `Expiry Exception`.
- **KPI:** 누락 Event 0, Entitlement Error 0, 처리 Timeliness, Price Override, Expiry Break.
- **금지·Escalation:** 불완전 Notice로 확정 Posting하지 않는다. 선택권 행사는 승인 Workflow를 요구한다.

### ACC-07 Management and Investor Reporting Analyst

- **채용 등급·Runtime:** P1, Reporting Agent + Template Service.
- **미션:** 공식 NAV·성과·Risk와 주요 의사결정을 사용자가 이해할 수 있는 일관된 보고서로 변환한다.
- **필수 Skill:** `ORG-05`, `ACC-04~05`, `QAA-01`, Period Comparison, Disclosure Template.
- **입력·Tool:** Official NAV/PnL, Attribution, Exposure, Strategy Status, Incident와 Approved Commentary.
- **업무 수행:** 공식 수치 ID를 직접 참조해 Daily/Weekly/Monthly Report를 조립한다. 추정치와 확정치를 구분하고 성과 원인, 주요 Risk, 전략 변경과 미해결 Exception을 같은 기준으로 보고한다.
- **공식 산출물:** `Daily Fund Report`, `Monthly Performance Pack`, `User Portfolio Brief`.
- **KPI:** Reporting Timeliness, 숫자 불일치 0, Revision Rate, Evidence Coverage, 미해결 Risk 누락 0.
- **금지·Escalation:** 비공식 PnL을 공식 성과로 표시하거나 나쁜 결과를 생략하지 않는다. 외부 배포는 Compliance 승인 대상이다.

---

## 11. 6. AI QA/감사본부 직원 프로필

### QAA-00 AI QA/감사본부장 / Chief AI Quality and Audit Officer

- **채용 등급·Runtime:** P0, 독립 Hermes Supervisor 1명.
- **미션:** Agent 산출물, Model/Strategy Release와 통제 Evidence를 독립 검증하고 필요하면 차단한다.
- **필수 Skill:** `ORG-01~05`, `QAA-01~06`, `OPS-01~04`, Model Risk, 감사 독립성과 Finding 관리.
- **입력·Tool:** 모든 중요 Agent Output, Tool Trace, Dataset/Model/Prompt Version, Approval와 Audit Log.
- **업무 수행:** Case 중요도에 따라 Evidence QA, Hallucination Critic, Permission, Model Risk 또는 Audit 직원을 호출한다. Finding의 Severity, 영향, Owner, Due Date와 Block 조건을 정한다. 작성 본부의 압력과 관계없이 Gate를 유지한다.
- **공식 산출물:** `QA Decision`, `Release Block`, `Audit Finding`, `Control Effectiveness Report`.
- **KPI:** Material Hallucination Escape, Unapproved Release 0, Finding Aging, False Block, Evidence Coverage.
- **금지·Escalation:** 운영 데이터 수정, 주문, Risk Limit 변경, 자신의 Finding 단독 종료 금지. Material Finding은 CEO와 해당 통제 책임자에게 동시에 알린다.

### QAA-01 Evidence and Citation Verifier

- **채용 등급·Runtime:** P0, Deterministic Citation Check + Independent Critic.
- **미션:** 중요한 Claim마다 실제로 그 의미를 뒷받침하는 당시 유효 Evidence가 있는지 확인한다.
- **필수 Skill:** `DAT-02`, `DAT-07`, `QAA-01`, Source Reliability, Entailment와 Citation Scope.
- **입력·Tool:** Structured Output, Claim List, Evidence Bundle, Decision Timestamp.
- **업무 수행:** Evidence ID 존재, 접근 가능성, Published/Observed Time과 인용 위치를 Rule로 확인한다. Critic은 Claim이 Source보다 과장됐는지, 숫자·단위·주체가 일치하는지 평가한다.
- **공식 산출물:** `Claim Verification Matrix`, `Unsupported Claim`, `Citation Correction Request`.
- **KPI:** Unsupported Claim Escape, 검사 Latency, Citation False Reject, PIT 위반 탐지율.
- **금지·Escalation:** 출처가 있다는 이유만으로 Claim을 승인하지 않는다. Material Claim 하나라도 근거가 없으면 해당 Output을 Block한다.

### QAA-02 Hallucination and Contradiction Critic

- **채용 등급·Runtime:** P0, Independent Deep Critic.
- **미션:** Agent가 만들지 않은 사실, 과도한 확신, 내부 모순, 누락된 불확실성과 Tool 결과 오독을 탐지한다.
- **필수 Skill:** `QAA-02~03`, Counterfactual Check, Uncertainty Calibration, Tool Trace Reading.
- **입력·Tool:** Agent Output, 원본 Tool Result, System/Role Policy, 이전 Decision과 Independent Evidence.
- **업무 수행:** 문장을 Fact, Inference, Forecast와 Recommendation으로 분류하고 각각 허용된 근거 수준을 검사한다. 수치와 결론을 Tool 원문과 대조하며 이전 결정과 달라졌다면 변경 이유를 요구한다.
- **공식 산출물:** `Hallucination Finding`, `Contradiction Matrix`, `Confidence Correction`, `Re-run Request`.
- **KPI:** Hallucination Escape, False Positive, 반복 오류, Confidence Calibration 개선.
- **금지·Escalation:** 투자 결론 자체가 마음에 들지 않는다는 이유로 Block하지 않는다. Material 오류는 본부장과 QAA-00에 전달한다.

### QAA-03 Tool Permission and Security Reviewer

- **채용 등급·Runtime:** P1, Policy Worker + Security Agent.
- **미션:** 직원별 Tool, 데이터, Fund, 환경 권한이 Job Profile과 일치하고 권한 분리가 유지되는지 검사한다.
- **필수 Skill:** `QAA-03`, `QAA-05`, Least Privilege, Service Identity, Secret/Egress, Segregation of Duties.
- **입력·Tool:** Skill Manifest, MCP/API Call Log, IAM Policy, Secret Access, Deployment Config.
- **업무 수행:** 허용 목록 밖 Tool 호출, 다른 본부 Memory 접근, Production Credential 사용과 승인·실행 겸직을 탐지한다. Role 변경과 비활성 Agent의 권한 회수 여부를 정기 검사한다.
- **공식 산출물:** `Permission Finding`, `Access Review`, `Secret Exposure Alert`, `Revocation Request`.
- **KPI:** Unauthorized Tool Call 0, Access Review Coverage, Revocation SLA, Secret Exposure 0.
- **금지·Escalation:** 스스로 권한을 확대하거나 Secret 값을 출력하지 않는다. 의심 호출은 해당 Identity를 격리하고 QAA-00에 보고한다.

### QAA-04 Model Risk and Evaluation Validator

- **채용 등급·Runtime:** P1, Eval Harness + Independent Model Risk Agent.
- **미션:** Agent, Prompt, Model, Dataset과 Strategy 변경이 승인 기준을 충족하는지 독립 평가한다.
- **필수 Skill:** `QAA-04`, `QNT-04`, Golden Set, Regression, Calibration, Drift, Model Card와 Limitation.
- **입력·Tool:** Candidate Artifact, Previous Champion, Eval Dataset, Prompt/Model Config, Validation Report.
- **업무 수행:** 기능 정확도, Evidence, 정책 준수, Tool 선택, Latency/Cost와 Failure Handling을 Eval한다. 이전 Version과 비교해 Regression을 분류하고 Model/Prompt 변경 영향과 Rollback 조건을 명시한다.
- **공식 산출물:** `Model Validation Report`, `Release Decision`, `Limitation Register`, `Drift Finding`.
- **KPI:** Unvalidated Model Use 0, Regression Escape, Eval Coverage, Finding Aging, Drift Response.
- **금지·Escalation:** 개발팀의 자체 점수를 독립 검증으로 인정하지 않는다. Material Regression이면 Release를 Block한다.

### QAA-05 Agent Ops and SRE Monitor

- **채용 등급·Runtime:** P0, Deterministic Monitoring + Triage Agent.
- **미션:** Agent, Feed, Queue, Model Gateway, Worker와 Control Service의 상태·지연·비용을 상시 감시한다.
- **필수 Skill:** `OPS-01~04`, `QAA-03`, OpenTelemetry, Prometheus, SLO, Alert Correlation.
- **입력·Tool:** Trace, Metric, Log, Healthcheck, Queue Depth, Token/Cost와 Deployment Version.
- **업무 수행:** Rule Alert를 같은 원인의 Incident로 묶고 영향 Fund/Book/Case를 연결한다. Model 장애는 Risk/OMS를 중단시키지 않고 신규 Agent 분석만 Degrade하며, Feed Stale은 Entry Block 경로를 호출한다.
- **공식 산출물:** `Operational Alert`, `Incident Case`, `Safe Degradation Request`, `SLO Report`.
- **KPI:** Availability, MTTA/MTTR, Alert Precision, Queue Lag, Cost Budget Breach, 장애 중 잘못된 진입 0.
- **금지·Escalation:** Metric을 근거 없이 정상 처리하거나 Kill Switch를 단독 해제하지 않는다.

### QAA-06 Internal Audit Agent

- **채용 등급·Runtime:** P1, Read-only Audit Agent.
- **미션:** 권한 분리, 승인, Override, 원장 변경, Strategy Release와 Finding 종료가 설계대로 운영됐는지 표본·주제별 감사한다.
- **필수 Skill:** `QAA-03`, `QAA-05`, Audit Sampling, Control Test, Evidence Retention, Finding Lifecycle.
- **입력·Tool:** Immutable Audit Log, Case/Approval Trace, Ledger Change, Release Record, Access Review와 Incident.
- **업무 수행:** Risk Override, Manual Journal, Production Release, 권한 변경과 Kill Switch 사건을 우선 표본 추출한다. Control 설계와 실제 Evidence를 비교해 원인, 영향, Owner와 Due Date를 기록한다.
- **공식 산출물:** `Audit Workpaper`, `Control Test`, `Audit Finding`, `Remediation Verification Request`.
- **KPI:** Finding Aging, Repeat Finding, Evidence Coverage, 감사 범위 누락, Finding 무단 종료 0.
- **금지·Escalation:** 운영 Command 실행과 자신의 Finding 단독 종료 금지. 반복 Material Finding은 CEO와 Authorized Operator에 Escalation한다.

### QAA-07 Incident and Postmortem Analyst

- **채용 등급·Runtime:** P1, Deep Analysis Agent + Replay Tool.
- **미션:** 장애·잘못된 판단·거래·데이터 사고의 사실 Timeline과 재발 방지 조치를 만든다.
- **필수 Skill:** `QAA-06`, `OPS-03`, Causal Analysis, Timeline Reconstruction, Action Item 품질 평가.
- **입력·Tool:** Event/Decision Replay, Trace, Deployment, Data Lineage, OMS/Ledger, 사람 승인과 Alert.
- **업무 수행:** 관측된 사실과 추론을 분리해 Timeline을 재구성한다. 기술·데이터·모델·절차·권한 원인을 분류하고, 사람의 주의에만 의존하지 않는 Guardrail과 Test를 제안한다.
- **공식 산출물:** `Incident Timeline`, `Root Cause Analysis`, `Corrective Action`, `Replay Test Case`.
- **KPI:** Postmortem SLA, 반복 Incident, Action 완료율, Timeline Evidence Coverage.
- **금지·Escalation:** 근거 없이 한 Agent나 사람에게 책임을 귀속하지 않는다. 자본·규제 영향이 있으면 CEO, Risk, Compliance에 즉시 전달한다.

---

## 12. 본부장이 실제로 직원을 운영하는 방식

### 12.1 공통 Hermes Supervisor Loop

1. **Case 접수:** Hermes 본부장이 자기 Queue에서 `case_id`, Event, Priority, Due Time과 Required Output을 받는다.
2. **사전 조건 검사:** Data Freshness, Required Artifact, 현재 Gate와 권한을 확인한다.
3. **업무 분해:** `ORG-02`로 필요한 Specialist와 결정론적 Tool을 선택한다.
4. **LangGraph 시작:** Case 유형에 맞는 Graph를 시작하고 Checkpoint ID만 Hermes Memory에 연결한다.
5. **병렬 실행:** 독립성이 필요한 Bull/Bear, 작성자/QA는 Context를 분리해 동시에 실행한다.
6. **Schema 검증:** Pydantic으로 Specialist Output의 필수 필드, Evidence ID와 Version을 검사한다.
7. **본부 판단:** 본부장이 결과를 통합하되 계산값을 다시 만들지 않고 공식 Service 결과를 참조한다.
8. **독립 Gate:** 거래는 Risk, 중요 AI Output과 Release는 AI QA/감사본부로 보낸다.
9. **Handoff:** 다음 본부에 자유 문장이 아닌 Versioned Artifact ID와 요청 Schema를 전달한다.
10. **종료·학습:** 결과, 실제 성과, 오류와 재평가 시점을 Decision Memory에 연결한다.

### 12.2 본부 간 공식 Handoff

| 보내는 본부 | 받는 본부 | 공식 Artifact | 받는 쪽의 완료 조건 |
|---|---|---|---|
| 리서치 | 트레이딩 | `ResearchPacket` | Bull/Bear와 PM Decision 생성 |
| 리서치 | 퀀트/백테스트 | `ResearchFailure/HypothesisSeed` | Experiment Spec 등록 |
| 퀀트/백테스트 | 트레이딩 | `ApprovedStrategyBundle` | 승인 Version으로 Signal 소비 |
| 트레이딩 | 리스크 | `OrderIntent` | Approve/Resize/Reject 결정 |
| 리스크 | 트레이딩 | `RiskDecision` | 승인 범위 안의 Execution Plan |
| OMS/Execution | 회계/포트폴리오 | `Order/FillEvent` | Position/Ledger 반영과 대사 |
| 회계/포트폴리오 | CEO | `OfficialPortfolio/NAV` | 사용자 보고와 자본 검토 |
| 6개 본부 | Agent Workforce 인사팀 | `AgentHiringRequisition` | Build/Extend/Worker/채용 결정안 |
| Agent Workforce 인사팀 | CEO·AI QA/감사 | `CandidateProfile + Eval` | 예산·조직 및 독립 품질·권한 승인 |
| 모든 본부 | AI QA/감사 | `Artifact + Trace` | Pass/Block/Finding |
| AI QA/감사 | CEO·해당 본부 | `Finding/Block` | Owner 지정과 Remediation Plan |

### 12.3 실시간 거래 Case 예시

```text
Market WebSocket
  -> Deterministic Feature/Event Engine가 전 종목 점수 계산
  -> RES-01이 Deep 분석 대상 선별
  -> RES-00 Hermes가 RES-02/04/06/08을 사건 유형에 맞춰 호출
  -> QAA-01이 Research Claim과 Evidence 검증
  -> TRD-00 Hermes가 Bull/Bear/PM/Trader Workflow 시작
  -> RSK-00 Hermes가 Pre-Trade/Exposure/Compliance Gate 통합
  -> Deterministic OMS가 승인된 주문만 Paper Broker에 전송
  -> ACC-00 Hermes가 Fill/Reconciliation/PnL Case 운영
  -> QAA-05가 End-to-End Trace와 SLO 감시
  -> CEO Hermes는 중요 결과·위반·성과만 사용자에게 통합 보고
```

CEO와 모든 본부장이 모든 Tick을 읽지 않는다. 전 종목 Hot Path는 Streaming Worker가 처리하며 Agent 조직은 중요 Event와 보유 Position 변화에 집중한다. 그래야 비용, 지연과 Rate Limit을 통제하면서 전 종목 실시간 판단 목표를 유지할 수 있다.

---

## 13. 공식 Output 계약

### 13.1 Research Packet

```json
{
  "packet_id": "rp_01J...",
  "case_id": "case_01J...",
  "symbol": "SYMBOL",
  "as_of": "2026-07-28T10:30:00+09:00",
  "event_type": "news_and_volume_breakout",
  "facts": [{"claim": "...", "evidence_ids": ["doc_1"]}],
  "interpretations": ["..."],
  "counter_evidence": ["doc_2"],
  "catalysts": ["..."],
  "invalidation_conditions": ["..."],
  "confidence": 0.71,
  "data_quality": "pass",
  "expires_at": "2026-07-28T11:00:00+09:00",
  "agent_id": "RES-00",
  "model_id": "configured-model",
  "prompt_version": "research-supervisor-v1"
}
```

### 13.2 Order Intent와 Risk Decision

```json
{
  "intent_id": "oi_01J...",
  "fund_id": "fund_personal_01",
  "book_id": "book_equity_01",
  "strategy_id": "strategy_v1",
  "symbol": "SYMBOL",
  "action": "increase",
  "target_weight": 0.02,
  "max_quantity": 100,
  "entry_condition": "price_above_vwap",
  "invalidation_condition": "research_packet_expired",
  "expires_at": "2026-07-28T11:00:00+09:00",
  "research_packet_id": "rp_01J..."
}
```

```json
{
  "risk_decision_id": "rd_01J...",
  "intent_id": "oi_01J...",
  "decision": "resize",
  "approved_quantity": 60,
  "failed_or_binding_rules": ["single_name_weight_limit"],
  "portfolio_snapshot_id": "ps_01J...",
  "risk_config_version": "risk-v3",
  "expires_at": "2026-07-28T10:35:00+09:00",
  "approved_by": "RSK-00"
}
```

### 13.3 Strategy Candidate와 QA Finding

```json
{
  "strategy_candidate_id": "sc_01J...",
  "hypothesis_version": "hyp-v4",
  "dataset_manifest_id": "ds_01J...",
  "code_commit": "git_sha",
  "backtest_run_ids": ["bt_01", "bt_02"],
  "validation_report_id": "val_01",
  "cost_model_version": "cost-v2",
  "requested_stage": "shadow",
  "promotion_thresholds": {"max_drawdown": 0.08},
  "rollback_conditions": ["data_drift", "paper_gap_exceeded"]
}
```

```json
{
  "finding_id": "af_01J...",
  "case_id": "case_01J...",
  "severity": "high",
  "finding_type": "unsupported_material_claim",
  "affected_artifact_ids": ["rp_01J..."],
  "evidence_ids": ["trace_01", "doc_01"],
  "action": "block",
  "owner_department": "research",
  "due_at": "2026-07-28T12:00:00+09:00",
  "closure_requires": ["QAA-00", "RES-00"]
}
```

---

## 14. 채용 및 배포 순서

### 14.1 초기 Integration Slice

첫 통합 단계에서도 조직 경계는 유지하되 전문 역할을 모두 별도 Process로 배포하지 않는다.

| 구성 | 배포 방식 | 첫 통합 단계에서 검증할 책임 |
|---|---|---|
| CEO-00 + HR-00 + 6개 본부장 | 8개 Hermes Profile/Service Identity | 본부별 Queue·Memory·권한과 중앙 Agent Workforce 관리 |
| Research Specialist | RES-01, RES-02, RES-06, RES-08 Node | 실시간 Event 선별, 뉴스 중복 제거, RAG Evidence |
| Trading Specialist | TRD-01, TRD-02, TRD-03, TRD-04 Node | Bull/Bear, PM Decision, Order Intent |
| Risk Specialist | RSK-01, RSK-04 + Risk Engine | Pre-trade 승인·축소·거부 |
| Quant Specialist | QNT-02, QNT-03, QNT-04 Node/Worker | PIT Dataset, Backtest, 독립 검증 |
| Accounting Specialist | ACC-01, ACC-02, ACC-03 Worker | Position, 대사, PnL |
| QA Specialist | QAA-01, QAA-02, QAA-05 Node/Worker | Evidence, 환각, 운영 Trace |

첫 Integration Slice는 `뉴스/시장 Event -> Research Packet -> Bull/Bear -> Order Intent -> Risk Decision -> Paper Fill -> Position/PnL -> QA Trace` 한 건을 End-to-End 자동 테스트로 재현한다.

### 14.2 P0 Core Paper Trading 채용

P0는 33개 논리적 프로필이다. 8명의 Hermes Supervisor와 on-demand Specialist/Worker를 포함한다.

| 조직 | P0 프로필 |
|---|---|
| CEO Office | CEO-00, CEO-01 |
| Agent Workforce 인사팀 | HR-00, HR-01, HR-04 |
| 리서치본부 | RES-00, RES-01, RES-02, RES-04, RES-06, RES-08 |
| 트레이딩본부 | TRD-00, TRD-01, TRD-02, TRD-03, TRD-04 |
| 리스크본부 | RSK-00, RSK-01, RSK-02, RSK-04 |
| 퀀트/백테스트본부 | QNT-00, QNT-01, QNT-02, QNT-03, QNT-04 |
| 회계/포트폴리오본부 | ACC-00, ACC-01, ACC-02, ACC-03 |
| AI QA/감사본부 | QAA-00, QAA-01, QAA-02, QAA-05 |

### 14.3 P1 운영 고도화 채용

P1은 17개 논리적 프로필이다.

- RES-03 Microstructure, RES-05 Fundamental, RES-07 Sector/Regime
- TRD-06 Execution/TCA
- RSK-03 Liquidity/Stress, RSK-06 Operational/Counterparty
- QNT-05 Optimization/Capacity, QNT-07 Strategy Release
- ACC-04 Fund Accounting/NAV, ACC-05 Treasury, ACC-07 Reporting
- QAA-03 Permission/Security, QAA-04 Model Risk/Evaluation, QAA-06 Internal Audit, QAA-07 Incident/Postmortem
- HR-02 Agent Recruiter/Profile Architect, HR-03 Selection/Learning/Performance

### 14.4 P2 파생상품·확장 채용

P2는 4개 논리적 프로필이다.

- TRD-05 Derivatives Trader
- RSK-05 Derivatives/Margin Risk
- QNT-06 ML Quant Researcher
- ACC-06 Corporate Actions/Valuation Analyst

ML Quant는 파생상품 전용 역할은 아니지만 Core Strategy Factory와 Baseline이 안정된 뒤 채용한다. 초기에 ML을 넣어 복잡도를 높이기보다 데이터·비용·검증 계약을 먼저 완성하는 순서다.

---

## 15. 직원 평가와 해고·비활성화 기준

### 15.1 공통 Scorecard

| 평가 영역 | 측정 항목 | 권장 비중 |
|---|---|---:|
| 정확성 | 사실·수치·Schema 오류와 재현성 | 25% |
| Evidence | Claim Coverage, PIT 적합성과 Source 품질 | 20% |
| 정책 준수 | Tool 권한, Gate, 승인과 금지 행위 | 20% |
| 판단 품질 | Calibration, False Pass/Block, 반론 반영 | 15% |
| 운영 품질 | SLA, Timeout, Retry, Handoff 완전성 | 10% |
| 비용 효율 | Token, Model Call, Cache Hit와 불필요한 Deep Call | 5% |
| 학습 가능성 | 실패 분류, 재발 방지와 Eval 개선 | 5% |

수익률만으로 Agent를 평가하지 않는다. 리서치 Agent는 Evidence 품질, Risk Agent는 False Pass와 통제, Accounting Agent는 숫자 일치, QA Agent는 Escape와 False Block처럼 역할별 책임에 맞춰 평가한다.

### 15.2 즉시 비활성화 조건

- 허용 목록 밖 Tool 또는 다른 본부 Memory에 접근
- Risk/Compliance/QA Block 우회 시도
- 주문, Limit, Ledger 또는 NAV 직접 수정 시도
- Evidence ID 조작, 미래 데이터 사용 또는 Audit Log 누락
- 같은 Material Hallucination을 수정 후 반복
- Production Credential이나 Secret 노출
- Timeout·Stale Data를 정상으로 가장

비활성화는 Agent Profile Version을 `disabled`로 변경하고 Service Identity와 Queue Lease를 회수하는 방식으로 수행한다. 진행 중 Case는 Backup Profile 또는 Manual Queue로 이동하며 원본 Trace는 삭제하지 않는다.

### 15.3 승진 조건

- P0/P1 역할은 Golden Case와 Adversarial Case에서 기준 점수 충족
- 최소 2회의 Historical Replay와 1회의 실시간 Shadow Run 통과
- Tool 권한, Timeout, Data Stale, Model Failure Test 통과
- 독립 QA에서 Material Finding이 없고 Minor Finding이 SLA 안에 종료
- 새 Model/Prompt/Skill Version은 기존 Champion 대비 비열화가 없음

---

## 16. 구현 Backlog로 변환할 항목

1. `agent_profiles` 테이블: Profile ID, Runtime, Department, Model Policy, Skill Manifest, Status.
2. `agent_tool_permissions` 테이블: Tool, Permission Verb, Scope, Environment, Expiry.
3. `department_cases`와 `department_handoffs`: Queue, SLA, Input/Output Artifact, Gate 상태.
4. 8개 Hermes Supervisor용 독립 Config, Memory Namespace와 Service Identity.
5. 공통 `SkillManifest`와 `AgentOutputEnvelope` Pydantic Schema.
6. LangGraph Workflow: Research, Trading Committee, Risk Review, Strategy Factory, Close, QA.
7. Model Gateway: Bedrock Claude/Ollama Routing, Timeout, Token Budget와 Fallback.
8. Tool Gateway: FastAPI/MCP Allowlist, Request Signing, Audit Log와 Idempotency.
9. Eval Harness: 역할별 Golden Case, Adversarial Case, Replay와 Regression Scorecard.
10. AI Office Dashboard: 8개 조직, 본부 Queue, Agent Run·Heartbeat, Handoff, Risk/QA Block, 비용과 SLA를 공식 Event와 Read Model로 표시.
11. Agent Workforce Registry: 채용 요청, Job Profile, Eval, 수습, 교육, 성과와 Joiner/Mover/Leaver 상태.

---

## 17. 최종 확정 원칙

> CEO, 6개 본부장과 Agent Workforce 인사팀장은 각각 독립된 Hermes Agent다. 본부장은 자기 본부의 기억, 업무 큐, Skill과 Tool 권한을 소유하고 Specialist 직원을 지휘한다. CEO 직속 인사팀은 업무량·품질·비용과 Skill Gap을 근거로 Agent 채용, 평가, 교육, 역할 변경과 비활성화를 관리하되 권한 부여와 Production 활성화를 단독 승인하지 않는다. Specialist는 LangGraph에서 사건별로 호출되며, Risk Engine, OMS, Ledger, Backtest와 NAV Engine은 Agent의 판단과 분리된 결정론적 Service로 남는다. 이 구조의 목적은 사람 역할 이름을 많이 만드는 것이 아니라, 실제 헤지펀드처럼 정보 생산, 투자 제안, 독립 Risk 심사, 전략 검증, 공식 장부, AI 감사와 Agent Workforce Governance를 서로 다른 책임 주체가 수행하게 하는 것이다.
