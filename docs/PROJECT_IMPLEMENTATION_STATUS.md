# Personal Hedge Fund Agent 구현 현황

> 문서 상태: Confirmed Implementation Snapshot v1.0
> 기준일: 2026-07-31
> 기준 브랜치: `main`
> 목적: 팀원별 구현 결과, 통합 상태와 다음 작업을 실제 저장소 기준으로 한곳에 정리한다.
> 완료 조건 기준: [Feature Backlog](02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md)

## 1. 한눈에 보는 현재 상태

이 저장소는 더 이상 문서만 있는 단계는 아니다. 각 본부의 결정론적 Prototype, 데이터 수집기,
Database Migration, Hermes Profile과 AI Office 화면이 존재한다. 그러나 이들을 항상 실행되는 하나의
서비스로 연결한 End-to-End Runtime은 아직 완성되지 않았다.

| 영역 | 현재 상태 | 해석 |
|---|---|---|
| 조직 | CEO Office, 6개 본부, Agent Workforce의 Hermes Profile 8개 | 역할·권한 계약은 있음 |
| 시장·리서치 | LS·DART·거시·뉴스 수집 Adapter와 Timescale/Supabase Repository 구현 | 상시 Worker, Redis Snapshot과 `market-api`는 미완성 |
| 거래·회계 | Order Contract, Risk Gate, OMS, Paper Broker, Ledger, Portfolio, Reconciliation Prototype | 단일 Process DEMO Loop는 가능, 운영 복구·Broker 연동은 미완성 |
| QA·감사 | Evidence QA, Trace, Tool Permission, Ops Health와 Incident Prototype | 전사 Event Bus와 실제 Model Gateway 연동은 미완성 |
| 자기 개선 | Improvement Candidate Domain, Repository, Migration과 Workforce Seed | Eval·Shadow·승인·배포 전체 Runtime은 미완성 |
| AI Office | 8개 조직 Pixel Office, Trading/Portfolio DEMO Snapshot과 Read-only BFF | 실시간 WebSocket, Auth와 공식 Agent 상태 연결은 미완성 |
| 전체 서비스 | 기능별 실행·자체 점검 가능 | 10거래일 Paper Dry Run과 Production Gate 미통과 |

`구현됨`은 코드와 계약이 존재한다는 뜻이다. Feature Backlog의 모든 완료 조건을 통과했거나
Production 사용이 가능하다는 뜻은 아니다.

## 2. GitHub와 로컬 통합 상태

2026-07-31에 GitHub `HgFinance/multi_agent`와 로컬을 대조했다.

- 원격 `main`의 팀원 병합 작업은 PR #22의 Hermes Kanban ADR까지 로컬에 모두 포함됐다.
- 재일님 리서치 Sprint J0~J2와 국내 뉴스 작업 7개 커밋은 로컬 `main`에 추가돼 있었다.
- 도현님의 `apps/api/main.py`와 영주님의 Workforce seed/config 수정은 본부 브랜치에만 있어,
  원 저자 정보를 보존해 `main`에 선별 반영했다.
- 본부 브랜치의 나머지 고유 기능 커밋은 없다. 과거 merge commit과 이미 `main`에 들어온 변경만 남아 있다.
- BIGKinds는 비용 대비 현재 필요성이 낮아 `DISABLED`로 두고 NAVER를 국내 뉴스 P0 Source로 사용한다.

## 3. 팀별 구현 결과

### 3.1 영주님: CEO Office와 Agent Workforce

**구현됨**

- `departments/00-ceo-office/src/mandate/`: Mandate 정책 검증, Version, Effective Time과 승인 Lifecycle.
- `departments/07-agent-workforce/improvements/`: 개선 후보 Domain, 상태 전이와 PostgreSQL Repository.
- `supabase/migrations/20260730000600_workforce_improvement_candidates.sql`: 개선 후보·Event Schema.
- `supabase/seed.sql`: Model 3개, 역할 Template 5개와 P0 Agent Profile 3개 Seed.
- CEO·Workforce Hermes Profile과 분리된 Memory/Service Identity 계약.

**부분 구현 또는 다음 작업**

- Mandate의 asyncpg Repository와 LangGraph 사용자 승인 Interrupt/Resume.
- Workforce Candidate의 실제 Eval, Shadow, 활성화와 Rollback Runner.
- Live Office의 제품·업무 Owner로서 Agent Roster·Queue·SLA Read Model 정의.
- Kanban Task 제목에 Secret·개인정보·미공개 주문 정보가 들어가지 않도록 업무 작성 정책 확정.

### 3.2 재일님: 리서치와 퀀트/백테스트

**구현됨**

- `contracts/market_events.py`: Decimal·3시각·멱등 ID·Quarantine을 포함한 Market Event 계약.
- `repository/market_repository.py`: In-memory/Timescale 공통 Repository와 Append-only 적재 경계.
- `collectors/ls_client.py`, `subscription_plan.py`: LS REST 종목 Master와 18개 실시간 TR 구독 계획.
- `collectors/ls_realtime_adapter.py`: 국내 주식 체결·호가 Payload 정규화.
- OpenDART 공시·재무, Corporate Action, Macro와 관측 기반 거래 Calendar 수집기.
- KOSPI/KOSDAQ Market Breadth와 DQ 검사.
- NAVER 국내 뉴스 Polling Stream, Alpaca 해외 뉴스와 KRX Instrument 연결.
- KRX Open API 31개, OpenDART 85개와 LS Open API 개발 참조 문서.
- Source Registry의 Key·계약·승인·라이선스·시장 범위 Fail-closed Gate.

**부분 구현 또는 다음 작업**

- LS WebSocket의 장시간 실행 Worker, 재접속·구독 복구와 Redis Event 발행.
- Redis Snapshot, `market-api`, Feature Engine, Event Priority Queue와 Parquet Archive.
- KRX Calendar는 공식 Calendar API 부재로 관측 역산 상태이며 미래 거래일을 추정하지 않는다.
- X 유명 인사 Social Insight는 P1 계획으로 확정했다. 승인 계정 Registry, Filtered Stream Collector,
  수정·삭제 Compliance Sync, 종목·주제 연결과 Evidence QA 교차 검증은 아직 미구현이다.
- 퀀트/백테스트본부의 Dataset Registry, PIT Backtest와 Strategy Registry는 아직 미구현이다.

### 3.3 도현님: 트레이딩, 회계/포트폴리오와 공통 Frontend Platform

**구현됨**

- 구조화 `OrderIntent`·`RiskDecision` 계약과 철학별 실행 Preset.
- 결정론적 OMS, Paper Broker와 단일 주문 상태 불변식.
- Double-entry Ledger, Position/Cash Projection과 Reconciliation.
- `portfolio/ui_read_model.py`: OMS·Ledger·Portfolio의 DEMO Snapshot Projection.
- `ai-office/`: 12개 원본 부서를 프로젝트의 8개 조직·2개 층 구조로 전환.
- `apps/api/main.py`: `/health`, `/ui/snapshot` Read-only DEMO BFF.

**부분 구현 또는 다음 작업**

- BFF는 아직 `tests/e2e` Paper Loop로 DEMO Snapshot을 만든다. Supabase 운영 Read Model이 아니다.
- `/agent/ask`는 Tool 실행 위험 때문에 기본 비활성화한다. Auth·Tool Allowlist 이후에만 재검토한다.
- OMS 재시작 복구, 부분 체결, 수수료·Slippage와 Supabase Repository.
- 공통 Frontend Platform 기술 DRI로 Auth, API Client, Realtime Store, WebSocket과 E2E를 통합한다.
- Hermes Kanban Status Bridge와 `agent.status.v1` Projector를 구현한다.

### 3.4 동규님: 리스크와 AI QA/감사

**구현됨**

- 결정론적 Pre-trade Risk Engine과 `APPROVE/RESIZE/REJECT` 계약.
- Redis Trading State Store와 장애 시 `HALTED` Fail-closed 경로.
- Evidence QA, Claim-Citation 검사와 Finding 초안.
- Agent Run·Tool Call Trace, Tool Permission 검사, Ops Health와 Incident Timeline.
- Compliance Agentic RAG Baseline과 본부별 Hermes SOUL/Config 보강.
- Hermes Kanban을 AI Office Agent 상태 Source로 재사용하는 ADR-0001 작성과 채택.

**부분 구현 또는 다음 작업**

- ADR-0001은 본 변경에서 채택됐으며, 구현은 아직 시작하지 않았다.
- Risk/QA Profile의 `tool_allowlist`와 `forbidden_tools`를 먼저 확정한다.
- Kanban 상태 매핑과 Event Contract를 독립 검증하고 Frontend 상태 오표시 Test를 담당한다.
- Risk Decision, QA Finding과 Trace를 Supabase/Event Bus에 실제 기록하는 Adapter가 필요하다.
- Risk: VaR, Correlation Shock 등 P1 Risk Metric과 P1 티어 페르소나 `operational-counterparty-risk-agent` 연동은 아직 착수하지 않았다.
- QA: P1 티어 페르소나(`model-risk-agent`, `internal-audit-agent`, `incident-postmortem-agent`)는 미구현이며, `audit.qa_decisions`에 `calculation_version`/`input_hash` 컬럼을 추가하는 Migration PR도 아직 진행하지 않았다.

## 4. 공통 기반 구현

| 기반 | 구현된 내용 | 남은 핵심 작업 |
|---|---|---|
| Supabase | 6개 Canonical Migration, RLS/Schema Smoke Test, Workforce Seed | 실제 환경 Migration·RLS 통합 검증, Domain Repository 연결 |
| TimescaleDB | Market Schema, Hypertable, 압축·보존 정책, 로컬 Docker Profile | Retention/Archive 운영값, Backup·Restore와 부하 검증 |
| Redis | Trading State Store와 계획된 Event Transport | 공통 Stream, Consumer Group, DLQ와 Replay 운영화 |
| Hermes | 8개 Supervisor Profile, SOUL, Memory/Skill 경계 | Runtime 배포, Tool Allowlist, Kanban Bridge와 Eval 자동화 |
| LangGraph | Workflow Prototype과 Agentic RAG Baseline | 실제 Case State, Interrupt/Resume와 Durable Checkpoint |
| AI Office | 8개 조직 UI, DEMO Snapshot Panel과 BFF | Auth, 공식 Snapshot, WebSocket, Kanban 상태와 Department Workbench |

## 5. Hermes Kanban Agent 상태 브리지 결정

[ADR-0001](02-engineering/adr/0001-hermes-kanban-agent-status-bridge.md)을 채택한다.

```text
Hermes Kanban Task/Assignee
  -> Kanban Status Bridge (read-only)
  -> Redis Streams: agent.status.v1
  -> Agent Status Projector
  -> Supabase Read Model
  -> FastAPI /ui/snapshot + /ws/operations
  -> AI Office Agent/Department Projection
```

- Business Owner: 영주님.
- 공통 Frontend Platform 기술 DRI와 구현 PR Owner: 도현님.
- Risk·QA Contract Reviewer: 동규님.
- 각 본부는 자기 Task 의미, 민감도와 `department_id` Mapping을 검토한다.
- Browser는 Kanban SQLite를 직접 읽지 않는다.
- Kanban은 업무 진행 상태의 Source일 뿐 Risk 승인, 주문, 원장과 QA 판정의 Source of Truth가 아니다.

## 6. 다음 통합 우선순위

1. `agent.status.v1` Schema, 멱등 ID, 상태 우선순위와 Supabase Projection을 계약 Test로 확정한다.
2. Risk/QA부터 Kanban Task 1~2개를 실행해 `RUNNING`, `WAITING_APPROVAL`, `BLOCKED`를 검증한다.
3. BFF에 Supabase Auth, `/ws/operations`, Sequence Gap 복구와 공식 Agent Status Snapshot을 연결한다.
4. LS WebSocket Worker를 Redis·TimescaleDB에 연결하고 `market-api`를 만든다.
5. P0 폐쇄 루프 이후 X 승인 Watchlist와 Social Evidence 교차 검증을 P1로 연결한다.
6. Market Event부터 Risk, Paper Fill, Ledger와 AI Office까지 한 `trace_id`로 관통시킨다.
7. Quant Dataset·Backtest·Strategy Registry를 구현해 첫 Strategy를 Shadow/Paper로 승격한다.
8. 10거래일 Paper Dry Run 전까지 `LIVE` 기능을 비활성화한다.

## 7. 통합 점검 결과

2026-07-31 로컬 통합본에서 다음을 확인했다.

- 본부별 Python 자체 점검 28개 통과.
- Supabase·Timescale Schema Contract Test 11개 통과.
- Research → Trading → Risk → Paper Fill → Ledger/Portfolio E2E Test 통과.
- `apps/api/main.py` 자체 점검 통과. `/agent/ask`는 기본 비활성 상태다.
- AI Office `vinext build` 통과.
- 8개 조직 Server Render와 구성 연결 Test 2개 통과. 원본 Starter를 검사하던 오래된 Test를 현행 HgFinance 계약으로 교체했다.
- Canonical 문서 19개의 H1·Code Fence와 전체 77개 문서의 상대 Link를 검사해 오류가 없음을 확인했다.

실제 LS·X·DART 외부 호출, Supabase·Timescale·Redis 통합 환경, Hermes Runtime과 Broker 연동은
Credential과 상시 Service가 필요한 별도 Integration Test다. 위 결과를 Production 검증으로 해석하지 않는다.

## 8. 현재 결론

> 팀별 Prototype은 상당 부분 만들어졌지만 제품은 아직 통합 서비스가 아니다. 다음 단계의 성공 기준은
> 파일 수가 아니라, 공식 Snapshot과 Event를 사용하는 AI Office에서 하나의 Paper Trade Case를
> 시장 데이터부터 Agent 판단, Risk, OMS, Ledger와 Audit까지 재현하는 것이다.
