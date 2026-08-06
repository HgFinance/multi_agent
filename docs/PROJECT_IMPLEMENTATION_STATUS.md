# Personal Hedge Fund Agent 실행 현황과 통합 계획

> **Prototype boundary (2026-08-03)**: 일부 부서의 기존 `scripts.py`, `scripts/test_*_ollama_agent.py`, `Modelfile`은 초기 단일 호출·수동 smoke prototype이다. 이 파일들에 남은 `Nous/Laguna`, `qwen2.5`, `qwen3:14b`, `agent-*` 기본값은 현재 Hermes Head/독립 Worker Registry를 덮어쓰지 않는다. 현재 runtime 판정은 Profile `model`, `staff_registry`, `runtime_personalities`와 실제 Worker Registry를 기준으로 한다. 해당 prototype을 현재 pipeline adapter로 승격하려면 별도 migration과 테스트가 필요하다.

> **문서 해석 규칙(2026-08-03)**: 최신 런타임 사실은 상단 Registry와 각 부서 `config.yaml`·Worker 구현을 기준으로 한다. 날짜가 붙은 Commit·DB·Container 수치는 당시의 Historical snapshot이다. `IMPLEMENTED`는 코드·계약 존재, `TEST_VERIFIED`는 재실행 테스트 통과, `RUNTIME_VERIFIED`는 실제 API·DB 입출력 확인, `BLOCKED`는 안전한 중단 상태를 뜻한다. 과거 모델·Profile row·외부 연동 성공을 현재 운영 완료로 승격하지 않는다.

> **Risk/QA 보안 보정(2026-08-05)**: Worker Scope fail-closed, QA runtime 미설정 DENY, Risk/QA 명령 인증과 공통 Replay contract는 `IMPLEMENTED`/`TEST_VERIFIED`다. Redis 두 Decision Event Replay probe와 rollback형 DB/Event smoke, Risk/QA Compose 기동·formal healthcheck는 `RUNTIME_VERIFIED`다. Claude 환경에서 Risk/QA 전체 `ruff check`·`ruff format --check`가 통과했고 참조 없는 legacy Worker Graph·구형 fixture·중복 Counterparty self-check를 제거했다. Production preflight는 Event Redis와 Research Packet URL을 명시적으로 요구하며, 실제 API·PostgreSQL Decision/Case Replay, 승인 Corpus, 전역 Issuer·mTLS·IAM 매핑은 여전히 `BLOCKED`다.

> 전사 런타임 기준(2026-08-06): 8개 부서장은 Hermes + Codex/Claude Code다. LLM Worker Registry는 CEO 1·HR 5·Research 6·Trading 2·Risk 1·Quant 7·Accounting 8·QA 2이고, 결정론 runner는 Trading/Risk/QA 각 1개(`desk-runner`·`risk-runner`·`qa-runner`)다. 따라서 실제 직원 수는 부서별 1·5·6·3·2·7·8·3, 총 35명이다. 기존 역할명은 감사·Profile 호환 Alias일 수 있으며 현재 실행 수는 각 Profile의 `workers`와 `runtime_personalities`를 따른다.

> 문서 상태: Confirmed Execution and Coordination Plan v2.2
> 감사 기준일: 2026-08-03 10:20 KST
> 감사 기준: GitHub `main`의 `a1107c4`, 실행 중인 Docker, 실제 DB와 재실행한 Test
> 목적: 팀원별 진척도, 애로사항, 선행 의존성과 다음 실행 순서를 한곳에서 관리한다.
> 완료 조건 기준: [Core Feature Backlog](02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md)

## 1. 지금 프로젝트는 어디까지 왔는가

이 프로젝트는 리서치 Data Plane을 넘어 Hermes와 본부 도구가 연결되는 단계에 들어왔다. LS 실시간 수집,
Research API, Market API, Research MCP, Research·Quant Hermes가 Docker에서 실행되고 있다. Risk·QA에는
P1 계산, API, Repository, Redis Event, Harness, Replay와 AI Office Projection 코드가 추가됐다.

하지만 회사 전체의 공식 거래 생명주기는 아직 닫히지 않았다.

> **Research와 Risk·QA의 독립 실행 능력은 크게 늘었지만, 하나의 Investment Case가 Order, Fill,
> Journal과 PnL로 이어지는 Canonical Paper Runtime은 아직 없다.**

다음 공동 목표는 새 Agent나 Source를 늘리는 것이 아니라 아래 한 건을 같은 `case_id`, `trace_id`,
Versioned Contract와 Canonical DB Row로 재현하는 것이다.

```text
실제 Research Packet
  -> 구조화된 OrderIntent
  -> 영속 Risk Decision
  -> Paper Order와 Fill
  -> Journal, Position과 PnL
  -> QA Decision과 Audit Trace
  -> AI Office 공식 Snapshot
```

## 2. 상태 판정 기준

| 상태 | 뜻 | 완료로 보지 않는 예 |
|---|---|---|
| `RUNTIME_VERIFIED` | Process가 실행 중이고 실제 API·DB 입출력을 확인함 | 외부 서비스를 한 번 호출한 코드 |
| `TEST_VERIFIED` | 결정론적 Test 또는 자체 점검을 현재 Commit에서 재실행해 통과함 | 과거 실행 기록만 있는 모듈 |
| `IMPLEMENTED` | 코드와 계약은 있으나 다른 본부·DB·Container와 통합되지 않음 | In-memory Prototype |
| `DOCUMENTED` | 설계와 완료 조건만 확정됨 | 구현 파일이 없는 기능 |
| `BLOCKED` | 선행 결정, Credential, 데이터 또는 다른 본부 산출물이 없어 진행 불가 | 단순한 후순위 작업 |

본부 가이드의 체크박스는 그 본부가 자기 산출물을 만들었는지를 뜻한다. 전체 제품 완료는 이 문서와
Feature Backlog의 End-to-End 완료 조건으로만 판정한다.

## 3. 2026-08-03 실행 감사 결과

### 3.1 Git과 팀 작업

- 감사 시작 시 로컬 `main`에는 리서치 8개 Commit, `origin/main`에는 Risk·QA 4개 Commit이 따로 있었다.
- 두 이력을 일반 Merge Commit `8130d80`으로 통합해 어느 팀의 기록도 재작성하지 않았다.
- 이전 감사 `0d6d356` 이후 Commit 기록은 Email Identity를 합쳐 재일님 49개, 동규님 39개다. Merge Commit도 포함한다.
- 같은 기간 208개 파일이 바뀌었고 리서치·Hermes·Risk·QA·AI Office·Supabase Migration과 Report가 중심이다.
- 도현님과 영주님 명의의 신규 Commit은 이번 구간에서 확인되지 않았다. 진행 완료로 추정하지 않는다.
- 추적하지 않는 `ai-office/ai-office-dev.log`는 개인 Runtime Log이므로 감사와 Commit 범위에서 제외한다.
- v2.1 반영 직후 동규님 Risk·QA PR #56·#57이 추가돼 `a1107c4`까지 Fast-forward했다. 결정론적
  Markdown 보고서, Notion Block Projection과 회귀 Test가 포함됐다.
- 감사 중 `docker-compose.yml`의 Claude 인증·Base URL 변경과 `scripts/claude_code_proxy.py`가
  로컬 미커밋 WIP로 추가됐다. 이번 문서 Commit에는 포함하지 않고 `MODEL-04` Review 대상으로 남긴다.

### 3.2 실제 실행 중인 서비스

루트 `docker-compose.yml`은 10개 기본 서비스를 정의하며 감사 시 모두 실행 중이었다.

| Service | 감사 시 상태 | 판정 |
|---|---|---|
| `timescaledb` | 2일 이상 실행, `healthy` | `RUNTIME_VERIFIED` |
| `ls-realtime` | 19시간 이상 실행, 거래일 Tick·Quote 적재 | `RUNTIME_VERIFIED` |
| `news-watcher` | 2일 이상 실행 | `RUNTIME_VERIFIED` |
| `ls-news` | 19시간 이상 실행 | `RUNTIME_VERIFIED` |
| `batch-collectors` | 9시간 이상 실행 | `RUNTIME_VERIFIED` |
| `market-api` | 45시간 이상 실행, `/health`, `/dq/summary` 응답 | `RUNTIME_VERIFIED` |
| `research-api` | 재기동 후 `/health`, Tool Gateway `enforce` 확인 | `RUNTIME_VERIFIED` |
| `research-mcp` | 12시간 이상 실행 | `RUNTIME_VERIFIED` |
| `research-hermes` | 17시간 이상 실행 | `RUNTIME_VERIFIED` |
| `quant-hermes` | 19시간 이상 실행 | `RUNTIME_VERIFIED` |

`hermes-dashboard`는 `dashboard` Profile이라 기본 기동에서 제외된다. Risk·QA·Trading·Accounting·CEO·HR
API는 Compose에 없다. 같은 PC의 `trading-*` Container와 Redis는 별도 Trading Bot 프로젝트이므로 이
프로젝트 Runtime으로 계산하지 않는다.

### 3.3 실제 적재 데이터

#### TimescaleDB

| 데이터 | 행 수 | 해석 |
|---|---:|---|
| `market.market_ticks` | 3,783,138 | LS 체결 적재 동작 |
| `market.market_quotes` | 4,203,513 | LS 호가 적재 동작 |
| `market.market_bars` | 3,973,545 | 분·일봉 적재 동작 |
| `market.market_breadth` | 58 | 시장 폭 Snapshot 존재 |
| `market.data_quality_windows` | 132 | DQ Window 기록 증가 |
| `market.derivative_snapshots` | 3,910 | 파생 첫 적재 완료, 이전 0건 Blocker 해소 |
| `market.microstructure_features` | 0 | 조회 계산은 있으나 영속 Feature 적재 없음 |

Market API의 2026-08-03 거래일 DQ 응답은 348개 Symbol, 최근 10분 Tick 280,726건이었다.

#### Research와 Quant

- Research API: Bluesky 421, NAVER 15,573, OpenDART 3,419, LS 뉴스 8,907, Alpaca 192,
  재무 Fact 74,314, 거시 관측 3,183건.
- Research Runtime: `pipeline_runs` 19, `collector_runs` 367, `daily_labels` 2,
  `symbol_restrictions` 503건. Collector Run은 `OK 248`, `SKIP 111`, `FAILED 11`이다.
- Quant: Dataset Manifest 1, Hypothesis 5(`TESTING 4`, `REJECTED 1`), Experiment 6
  (`COMPLETED 5`, `RUNNING 1`), Backtest Run 3건.

#### Risk, QA, 거래와 조직

- Risk: `risk_decisions`, `trading_states`, `run_log_events`가 모두 0건이다.
- QA: `qa_decisions` 2건(`FAIL 1`, `WARN 1`), Incident Event 2건, Corrective Action 1건이다.
  `agent_runs`, `tool_calls`, `audit.run_log_events`는 0건이다.
- Execution·Accounting: Trade Case, Intent, Order, Fill, Journal, Position, Portfolio Snapshot 모두 0건이다.
- Governance: Mandate, Investment Case, Approval 모두 0건이다.
- Workforce: Agent Profile 19개, Profile Version 19개(`ACTIVE 6`, `DRAFT 13`)가 있다.
  Improvement Candidate와 Access Request는 0건이다.

코드나 Seed가 있다는 사실과 실제 운영 Workflow가 사용됐다는 사실을 구분한다. 특히 Risk·Execution·Accounting의
0건 상태가 현재 End-to-End 병목이다.

### 3.4 재실행한 검증

| 검증 | 결과 | 주의사항 |
|---|---|---|
| 전체 Repository pytest | CEO·HR 원인 해소(2026-08-03) | CEO·HR `test_ollama_agent.py`를 `test_ceo_ollama_agent.py`/`test_hr_ollama_agent.py`로 분리. Accounting 쪽 동일 파일명 충돌 여부는 도현님 확인 필요 |
| Core·Risk·QA 명시 테스트 | `179 passed`, `1 failed`, 16 subtests 통과 | 신규 Migration을 Schema 기대 목록에 누락 |
| Research Pipeline | 11개 영역 통과 | LLM·API 없는 결정론적 자체 점검 |
| Risk Pipeline | 7개 영역 통과 | Redis·Hermes 없는 자체 점검 |
| QA Pipeline | 5개 영역 통과 | Ollama·Hermes 없는 자체 점검 |
| Quant 5개 Entry Point | 26개 영역 통과 | DB 없는 합성 Fixture 자체 점검 |
| AI Office clean build | 성공 | Node 22 임시 Container |
| AI Office Server Render Test | `2/2 passed` | 이전 조직명 Test Blocker 해소 |
| AI Office dependency audit | 18건 | High 13, Moderate 4, Low 1. Upgrade 영향 검토 필요 |
| Hermes Profile Contract | 실패 2건, 경고 5건 | Risk·QA 모델 선언 불일치, 5개 Profile Tool Allowlist 미선언 |
| Risk·QA Credential Preflight | 필수 2개 누락 | `QA_POLICY_SOURCE_ID`, `OPENAI_API_KEY` |
| Risk·QA Report/Notion 회귀 | `18 passed` | Reporter 실패가 Risk·QA 판정을 바꾸지 않는지 검증 |

전체 Python Test의 유일한 Assertion 실패는 `20260802002200_research_as_known_at.sql`을
`tests/schema/test_schema_contract.py`의 기대 순서에 추가하지 않은 것이다. Risk·QA Redis Integration은
격리 DB 15에서 통과했지만 별도 Trading Bot Redis를 사용했으므로 제품 Runtime 증거로 보지 않는다.

## 4. 본부별 진척도와 다음 인수인계

### 4.1 재일님: 리서치본부와 퀀트/백테스트본부

**확인된 진척도**

- 전 종목 Universe 2,596개와 거래정지·관리종목 제외 경로를 구현했다.
- Research MCP, Tool Gateway 강제 모드와 Bearer 인증을 추가하고 Research·Quant Hermes를 Docker로 실행했다.
- GPR·GDELT·Bluesky, Story Cluster, 일별 Label, Packet Outcome, DART 현금흐름과 F-Score를 추가했다.
- `as_known_at`과 정정 재무 PIT 보존, Research Data Steward, Amihud·Roll 유동성 지표를 구현했다.
- 파생 Snapshot 3,910건이 적재돼 `RQ-04`의 첫 적재 조건을 충족했다.
- Research 11개와 Quant 26개 Self-check가 통과했고 Quant 실제 Experiment도 6개로 늘었다.
- Claude Code CLI를 Anthropic Messages 형태로 감싸는 Host Proxy와 Compose 연결이 로컬 WIP로 생겼다.

**애로사항과 남은 경계**

- 신규 `as_known_at` Migration 때문에 Schema Contract Test 1건이 실패한다.
- `collector_runs`에 실패 11건이 있으며 Source별 허용 실패와 실제 장애를 분류해야 한다.
- Research Packet은 MCP로 생성되지만 공식 Artifact/Event와 Trading Handoff는 아직 없다.
- 영속 `microstructure_features`는 0건이고 Event Priority Queue·Project Redis Producer가 없다.
- NAVER의 마지막 관측은 08-02 04:20, Alpaca는 07-31 01:49라 Staleness 정책 확인이 필요하다.
- Quant API·Worker와 Strategy Registry Promotion Runtime은 아직 없다.
- Claude Proxy는 구독 한도 공유, Host 단일 장애점, 동시 실행 제한과 Provider 약관을 검증하지 않았다.

**다음 작업**

| ID | 상태 | 작업 | 완료 증거 |
|---|---|---|---|
| `CI-06` | `BLOCKED` | 신규 Migration을 Schema Contract 기대 순서에 반영 | 전체 Schema Test 통과 |
| `RQ-01` | `PARTIAL` | `ResearchPacket v1` Schema와 MCP 생성 경로를 Canonical Artifact·Event로 연결 | API·Event·DB에서 같은 Packet ID 조회 |
| `RQ-02` | `DOCUMENTED` | Feature/Event Engine과 Priority Queue 연결 | 급변 Fixture가 Stream에 한 번만 생성 |
| `RQ-03` | `DOCUMENTED` | Quant API·Worker Container와 Job Contract | Dataset→Experiment→Candidate 재시작 복구 |
| `RQ-04` | `RUNTIME_VERIFIED` | 파생 첫 적재와 DQ | 3,910행과 DQ 증거 확인, 다음은 연속성 검증 |
| `RQ-05` | `DOCUMENTED` | Microstructure Feature 영속 Worker | `microstructure_features > 0`, Replay Hash 일치 |
| `RQF-01` | `DOCUMENTED` | Research V2 계약, PIT Cutoff와 Claim/Evidence Graph | Fact Claim 100% Citation, Replay 미래 조회 0건 |
| `RQF-02` | `DOCUMENTED` | Research Branch/Fan-in, Hermes Case Adapter와 부분 복구 | 분석가 실패 Fixture가 Checkpoint에서 PARTIAL 복구 |
| `RQF-03` | `DOCUMENTED` | Evidence-linked 가설 사전 등록과 독립 Quant Validation | Packet→Claim→Hypothesis→Experiment 역추적, 생성자 자기 승인 0건 |
| `RQF-WEB-01` | `DOCUMENTED` | RES-08 전담 SearXNG/Playwright Web Search MCP와 Evidence 승격 | 타 Persona Search 403, Replay 호출 0건, 검증 전 Fact 승격 0건 |
| `RQF-IDM-01` | `DOCUMENTED` | QNT-08 InvestmentDoctrine 계약, Fine-tuning Need Gate와 독립 Frozen Eval | 인물 모방 0건, Prompt/RAG Baseline 비교, 자기 승인 0건 |
| `MODEL-04` | `IMPLEMENTED` | Claude Code Host Proxy 보안·비용·지연 검증 | Commit·Self-check·Probe·429/Timeout·Fallback 증거 |

### 4.2 도현님: 트레이딩본부, 회계/포트폴리오본부와 공통 Platform

**확인된 진척도**

- 이전 Prototype인 OrderIntent, Risk Decision, OMS, Paper Broker, Ledger, Position과 Reconciliation은 유지된다.
- 이번 감사 구간에 도현님 명의 신규 Commit은 확인되지 않았다.
- AI Office는 다른 팀의 Risk·QA Panel 추가 후 clean build와 Render Test 2건을 통과했다.

**애로사항과 남은 경계**

- Trading·Accounting API/Worker Container가 없고 Canonical 주문·체결·원장 행이 모두 0건이다.
- 프로젝트 전용 Redis, 공통 Event Envelope, Transactional Outbox와 Health Contract가 없다.
- AI Office Risk·QA Panel은 계약 Projection이며 실제 Risk·QA Runtime 상태가 아니다.
- 공식 Snapshot/WebSocket, Kanban Bridge, Auth와 Sequence Gap 복구는 미구현이다.
- `npm audit`에서 High 13건을 포함한 18개 취약점이 보고됐다.

**다음 작업**

| ID | 상태 | 작업 | 완료 증거 |
|---|---|---|---|
| `CI-01` | `BLOCKED` | 중복 Ollama Smoke Test 수집 문제 해소 | Repository 전체 pytest 수집·실행 |
| `PLAT-01` | `DOCUMENTED` | Event Envelope, Error, Idempotency와 Health Contract 코드화 | 전 본부 Contract Test 통과 |
| `PLAT-02` | `DOCUMENTED` | 프로젝트 전용 Redis와 Compose Core | 별도 `trading-*` 없이 Core 기동 |
| `TRD-01` | `IMPLEMENTED` | Trading API·OMS Worker와 Supabase 연결 | Risk 승인 없는 Submit 0건, 재시작 복구 |
| `ACC-01` | `RUNTIME_VERIFIED` | Fill Consumer·Ledger·Snapshot Projector | 2026-08-04 실 Supabase 왕복. `ledger/fill_consumer.py` 자체 점검이 체결 1건 → `accounting.journals`(POSTED, 3라인) · `positions` · `cash_balances` · `portfolio_snapshots` 생성과 재실행 멱등을 확인. **남은 것: 체결 원천이 `execution.fills`가 아니라 API 주입**(TRD-01 대기) |
| `UI-01` | `IMPLEMENTED` | 공식 Snapshot/WebSocket Client | `DEMO/PAPER` 분리와 Gap 복구 E2E |
| `UI-03` | `BLOCKED` | AI Office 취약점 Upgrade 계획과 회귀 Test | High 취약점 처리·수용 기록 |

### 4.3 동규님: 리스크본부와 AI QA/감사본부

**확인된 진척도**

- Risk P1 Exposure·Stress·VaR·Correlation·Kill Switch와 PIT/Staleness Gate를 구현했다.
- Risk·QA PostgreSQL Repository, Redis Event Bus, Bounded Retry, Fail-closed Harness와 Replay Journal을 추가했다.
- QA Model Risk, Internal Audit, Production Evidence Ingestion, Agentic RAG 회복성과 Incident Transaction을 구현했다.
- Risk·QA API Surface, Metrics, Observability와 AI Office Risk·QA 계약 Panel을 추가했다.
- 관련 DB Migration과 Test가 반영됐고 QA Decision 2건, Incident Event 2건, Corrective Action 1건이 존재한다.
- Risk 7개, QA 5개 Self-check와 명시 pytest 대부분이 통과한다.
- 결정론적 Risk 보고서 11개와 QA 보고서 9개, 공통 Markdown→Notion Block Renderer를 추가했다.
- 최신 Reporter·Pipeline 회귀 Test 18개가 통과했다. Notion은 판정 Source가 아니라 Projection으로 유지된다.

**애로사항과 남은 경계**

- Risk·QA는 Compose Service가 아니며 Canonical Risk Decision과 Run Log가 0건이다.
- 운영 Credential에서 `QA_POLICY_SOURCE_ID`, `OPENAI_API_KEY`가 비어 있다.
- Risk·QA 부서장 모델은 `head_runtime`의 `openai-codex/gpt-5.6-luna`, 직원 모델은 `employee_runtime`의 LangGraph/Ollama `qwen3:1.7b`로 분리됐다. Profile Checker도 이 두 계층을 각각 검증해야 한다.
- QA/Risk Script의 직원 Ollama 주소·모델은 `OLLAMA_BASE_URL`·`OLLAMA_CHAT_MODEL` 환경변수로 주입된다. 실제 Ollama Health와 응답 증거는 운영 전 별도 확인한다.
- Risk·QA의 기존 Profile row는 FK·감사 이력용 DRAFT/PROBATION 호환 레코드이며, 현재 실행 직원 수는 Risk 2명(LLM 1 + `risk-runner`)·QA 3명(LLM 2 + `qa-runner`)이다. Governed Fund·Policy·ACTIVE 승인 경로는 여전히 운영 조건이다.
- 생성 보고서의 Git 보존 여부, Canonical Artifact Storage, Report Hash와 Notion Idempotency 정책이 미확정이다.

**다음 작업**

| ID | 상태 | 작업 | 완료 증거 |
|---|---|---|---|
| `RSK-01` | `IMPLEMENTED` | Risk API Container와 Decision DB/Event 연결 | 응답·DB·Event Hash 일치 |
| `QA-01` | `IMPLEMENTED` | QA API Container와 Trace/Decision 영속화 | Claim→Evidence→Decision→Finding Replay |
| `QA-02` | `IMPLEMENTED` | Workforce Tool Allowlist와 실제 Evidence API 연결 | 미허용 Tool 차단과 Trace |
| `QA-03` | `BLOCKED` | 개인 GPU 주소 제거와 Model Gateway 전환 | 개인 IP 0건, Gateway Trace |
| `MODEL-03` | `BLOCKED` | Risk·QA Hermes Head 모델 선언과 Worker Registry 일치 | Head/Worker 계층 Contract Check 통과 |
| `OPS-01` | `BLOCKED` | Risk·QA 운영 Credential과 Governed FK 준비 | Preflight 필수 항목 전부 `true` |
| `RPT-01` | `IMPLEMENTED` | 결정론적 Report Artifact·Notion Projection 운영 계약 | DB Artifact Hash·Notion Page ID·재실행 멱등성 |

### 4.4 영주님: CEO Office와 Agent Workforce 인사팀

**확인된 진척도**

- 이전 Mandate, Improvement Candidate, Access Lifecycle, Cost Scorecard와 승인 상태 머신은 유지된다.
- Risk·QA Migration으로 Workforce Agent Profile과 Version이 총 19개로 늘었다.
- 이번 감사 구간에 영주님 명의 신규 Commit은 확인되지 않았다.

**애로사항과 남은 경계**

- Governance·Workforce API가 없고 Mandate·Investment Case·Approval·Improvement Candidate가 모두 0건이다.
- Profile Version 19개 중 13개가 DRAFT이며 승격·철회 Workflow가 없다.
- CEO와 HR Hermes Profile은 Tool Allowlist를 선언하지 않아 Profile Checker 경고가 난다.
- CEO·HR의 같은 Smoke Test 파일명이 전체 pytest 수집을 막는다.
- 공식 Roster는 HR-02로 해소됐다(아래 표 참고). Approval Inbox, Queue, SLA와 Kanban Read Model은 여전히 없다.

**다음 작업**

| ID | 상태 | 작업 | 완료 증거 |
|---|---|---|---|
| `GOV-01` | `IMPLEMENTED` | Governance API와 Mandate PostgreSQL Repository | Version 활성화·재승인 Test |
| `GOV-02` | `DOCUMENTED` | Investment Case·Approval·Escalation API | Risk/QA Block 우회 불가 |
| `HR-01` | `IMPLEMENTED` | Workforce API와 Candidate/Access Runtime | 후보 생성부터 독립 승인 DB Replay |
| `HR-02` | `IMPLEMENTED` | Profile·Tool Permission 공식 Read+Write API(roster/*.py, app.py 4개 엔드포인트) | QA와 Gateway가 같은 Version 조회, ACTIVE 전환 시 QA Eval·CEO 승인 둘 다 없으면 409 |
| `HR-03` | `DOCUMENTED` | Eval·Shadow·Promotion·Rollback Orchestrator | 승인형 자기 개선 폐쇄 루프 |
| `HR-04` | `BLOCKED` | 13개 DRAFT Profile Review와 Allowlist 보완 | 승인·거절 사유와 Version 상태 기록 |

## 5. 공통 Blocker와 결정 사항

| 우선순위 | 발견 사항 | 영향 | Owner/조치 |
|---|---|---|---|
| P0 | 전체 pytest가 같은 Smoke 파일명으로 수집 실패 | CI 신뢰 불가 | 도현·영주, `CI-01` |
| P0 | 신규 Migration이 Schema 기대 목록에 없음 | CI 1건 실패 | 재일, `CI-06` |
| P0 | Risk·QA 모델 선언과 Profile Checker 불일치 | Hermes 배포 기준 불명확 | 동규·영주, `MODEL-03` |
| P0 | Compose에 Risk·QA·Trading·Accounting이 없음 | 전사 Runtime 불가 | 도현·동규, `PLAT-02` |
| P0 | Execution·Risk·Accounting Canonical Row 0건 | Paper E2E 미완성 | 전 팀, Wave 2 |
| P0 | QA 개인 GPU IP 하드코딩 | 재현성·보안·Failover 문제 | 동규, `QA-03` |
| P0 | Risk·QA 필수 Credential 2개 누락 | Production Ingestion 불가 | 동규·영주, `OPS-01` |
| P1 | AI Office npm High 취약점 13건 | Frontend 배포 위험 | 도현, `UI-03` |
| P1 | Hermes Profile Tool Allowlist 미선언 — CEO·HR 2개는 해소(2026-08-03, GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2.4·3.6절 반영), 나머지 Owner 확인 필요 | 권한 경계 경고 | 해당 Owner, `HR-04` |
| P1 | Microstructure Feature 0건 | 전략·Risk Replay 제한 | 재일, `RQ-05` |
| P1 | Kanban Bridge가 ADR만 있고 미구현 | Agent 상태는 Scripted | 도현·영주, `UI-02` |
| P1 | Claude Host Proxy가 미커밋·미검증 | 구독 한도·보안·가용성 위험 | 재일·동규, `MODEL-04` |
| P1 | Risk·QA 생성 보고서 보존 경계 미확정 | Git 팽창·중복 Notion Page 위험 | 동규·영주, `RPT-01` |

`MODEL-01`과 `MODEL-03`에서 개발·Paper·Production Provider, Bedrock 전환 기준, 공용 GPU Gateway,
Model Digest와 Eval Version을 함께 결정한다. 임시 모델 변경은 Checker를 무시하는 방식으로 완료 처리하지 않는다.

## 6. 본부 간 선행 의존성

| 생산자 | 산출물 | 소비자 | 현재 병목 |
|---|---|---|---|
| CEO | 활성 Mandate Version | Trading, Risk | API·DB Runtime 없음 |
| Research | `ResearchPacket v1` | Trading, QA | MCP는 있으나 공식 Artifact/Event 없음 |
| Quant | Strategy Candidate | QA, Risk, CEO | Worker와 승격 API 없음 |
| Trading | OrderIntent | Risk | 서비스 호출과 영속화 없음 |
| Risk | `risk.decision.v1` | Trading, QA, CEO | Code/Test만 있고 Compose·DB Row 없음 |
| Trading | Fill Event | Accounting, QA | OMS Worker·Event Bus 없음 |
| Accounting | Portfolio Snapshot Ref | CEO, Risk, UI | DB Snapshot 0건 |
| QA | `qa.decision.v1`, Finding | CEO, Workforce, UI | 일부 Row는 있으나 Case Runtime 없음 |
| Workforce | Profile와 Tool Permission | Hermes, QA, Gateway | 19개 Version은 있으나 공식 Read API 없음 |
| Hermes Kanban | 업무 진행 상태 | AI Office | Bridge·Projector 없음 |

후속 본부는 선행 산출물의 필드가 확정되기 전 임의 JSON을 만들지 않는다. Stub에는 제거 Task ID와
실제 생산자 Contract를 기다리는 조건을 함께 기록한다.

## 7. 실행 계획 v2.2

### Wave 0. CI와 계약 기준 복구

**현재:** 진행 중, Merge Gate 실패 3개.

1. `CI-01`: CEO·HR Smoke Test 이름 또는 pytest 수집 범위를 고친다.
2. `CI-06`: `20260802002200` Migration을 Schema Contract에 추가한다.
3. `MODEL-03`: Risk·QA 모델 선언과 Profile Checker를 의도한 Provider로 일치시킨다.
4. Python 명시 Suite와 AI Office clean build/Test를 GitHub Actions에 추가한다.
5. Credential Integration은 별도 Job으로 분리하고 Unit Test는 Clean Runner에서 한 명령으로 통과시킨다.

**Exit Gate:** 전체 Test 수집 성공, Assertion 실패 0, Hermes Profile 위반 0.

### Wave 1. 공통 Runtime Backbone

**현재:** Research MCP·Hermes까지 확장, Core Event Runtime은 미완성.

1. `PLAT-01`: Versioned Event Envelope, Error, Idempotency와 Health Contract를 고정한다.
2. `PLAT-02`: 프로젝트 전용 Redis와 Core Network를 구성한다.
3. `PLAT-03`: Transactional Outbox, Relay와 Consumer Idempotency를 구현한다.
4. `RSK-01`, `QA-01`: 기존 API·Event 코드를 Compose Service로 올린다.
5. 모든 Service에 `/health/live`, `/health/ready`, `/metrics`를 제공한다.

**Exit Gate:** Research Event 한 건이 프로젝트 Redis를 거쳐 Risk·QA에 한 번만 반영되고 재시작 후 복구된다.

### Wave 2. 결정론적 Paper Investment Case

1. `GOV-01`이 활성 Mandate Version을 제공한다.
2. `RQ-01`이 고정 Fixture Research Packet을 발행한다.
3. `TRD-01`이 OrderIntent를 만들고 `RSK-01`을 호출한다.
4. 승인된 Intent만 Paper OMS가 Order·Fill을 생성한다.
5. `ACC-01`이 Fill을 Journal, Position과 Portfolio Snapshot으로 투영한다.
6. `QA-01`이 Claim, Tool, Risk와 Fill Trace를 검사한다.
7. 한 `trace_id`로 모든 DB Row와 Event를 Replay한다.

**Exit Gate:** 현재 0건인 Canonical 거래·위험·회계 Table에 멱등 Fixture 1건이 기록된다.

### Wave 3. Hermes와 AI Office 실시간 연결

1. `MODEL-01`: 개발·Paper·Production Provider와 Gateway ADR을 확정한다.
2. ~~`HR-02`: Profile·Tool Permission 공식 Read API를 제공한다.~~ (2026-08-03 구현 완료, 위 4.4절 표 참고)
3. `UI-01`: 공식 Snapshot과 Domain Event WebSocket을 연결한다.
4. `UI-02`: Hermes Kanban을 `agent.status.v1`로 변환하는 Read-only Bridge를 구현한다.
5. `UI-03`: Frontend 의존성 취약점을 검토하고 Upgrade 회귀 Test를 통과한다.

**Exit Gate:** 새 Browser Session이 Snapshot으로 복구되고 Scripted Simulation은 `DEMO`에서만 보인다.

### Wave 4. Strategy Factory와 Paper Dry Run

1. Quant Dataset→Hypothesis→Backtest→Walk-Forward를 Strategy Registry에 연결한다.
2. QA Eval, Risk Capability와 CEO 승인을 통과한 후보만 `SHADOW`, 이후 `PAPER`로 승격한다.
3. 파생은 연속 적재, Greeks, Margin, Multi-leg와 Accounting Capability를 검증한 뒤 활성화한다.
4. 10거래일 Paper Dry Run과 장애·재시작·Rollback Drill을 수행한다.

**Exit Gate:** Strategy Candidate 한 건의 데이터, 코드, 비용, Eval, 승인, 배포와 Rollback을 재현한다.

## 8. 2주 통합 실행 보드

기간은 2026-08-03부터 2026-08-14까지다. 각 팀은 동시에 주 작업 1개와 Review 1개만 진행한다.
선행 Contract가 없으면 임의 JSON을 만들지 않고 `BLOCKED`로 전환한다.

| 마일스톤 | 기간 | 주 작업 | Owner | Exit Gate |
|---|---|---|---|---|
| M0 기준 복구 | 08-03 | `CI-01`, `CI-06`, `MODEL-03` | 도현·재일·동규 | 전체 pytest 수집, Schema 실패 0, Profile 위반 0 |
| M1 계약 고정 | 08-04~05 | `PLAT-01`, `RQ-01`, Risk·QA Event Review, `HR-02` 응답 계약 | 도현 주도, 전 팀 Review | Fixture와 Contract Test, Owner 승인 |
| M2 Runtime Backbone | 08-06~07 | `PLAT-02`, `RSK-01`, `QA-01`, Health·Metrics | 도현·동규 | 프로젝트 Redis와 Risk·QA Compose 기동 |
| M3 첫 Paper Case | 08-10~12 | `GOV-01`, `TRD-01`, `ACC-01`, `RPT-01` | 영주·도현·동규·재일 | Mandate→Packet→Risk→Fill→Journal→QA 1건 |
| M4 운영 Projection | 08-13~14 | `UI-01`, `UI-02`, `UI-03`, Replay·보고서 | 도현·영주·동규 | 공식 Snapshot 복구, Trace·Report 탐색 |

### 8.1 Merge와 Handoff 순서

1. 생산자가 Contract Fixture와 Version을 먼저 Merge한다.
2. 소비자는 같은 PR 또는 후속 PR에서 Contract Test를 추가한다.
3. Runtime Service는 Health와 실패 정책을 통과한 뒤 Compose에 들어간다.
4. DB Row와 Event Hash가 일치한 뒤에만 AI Office와 Notion Projection을 연결한다.
5. Projection 실패는 Risk, QA, OMS와 Ledger의 결정 결과를 바꾸지 않는다.

### 8.2 Blocker 처리 시간

| 경과 시간 | 조치 |
|---|---|
| 발견 즉시 | 담당자 Scrum `Blocker`에 필요한 입력과 Owner 기록 |
| 4시간 | 생산자·소비자 공동 Issue와 최소 Fixture 등록 |
| 1영업일 | CEO/PM 스크럼에서 범위 축소·순서 변경 결정 |
| 2영업일 | 임시 Stub 허용 여부 ADR, 제거 Task와 만료일 필수 |

### 8.3 통합 완료 증거 묶음

각 마일스톤은 아래 다섯 가지가 모두 있어야 완료다.

- Merge Commit과 Reviewer.
- 재현 명령과 Test 결과.
- API Health 또는 Worker Heartbeat.
- 생성된 DB Row·Event·Artifact ID와 Hash.
- 다음 본부가 실제로 소비한 Handoff 증거.

## 9. Daily Scrum과 진행 공유 규칙

네 팀원 가이드의 `Daily Scrum`은 선택 항목이 아니다. 매일 아침 아래 세 항목을 실제 상태로 갱신한다.

| 필수 항목 | 기록 내용 |
|---|---|
| `Yesterday` | 전날 Merge된 Commit, 통과한 Test, 실행한 API·DB 증거와 완료 Task ID |
| `Today` | 오늘 종료할 Task ID, 산출물, Reviewer와 Handoff 대상 |
| `Blocker` | 현재·예상 장애, 필요한 입력, 해소 Owner와 우회 금지 조건 |

신규 작업이 없으면 빈칸 대신 `신규 Commit 확인 없음`이라고 적는다. Blocker가 없으면 `없음`이라고 명시한다.
완료 보고에는 파일명이 아니라 Commit, Test, API, DB Row 또는 Screenshot을 붙인다.

Issue와 PR에는 다음 계약을 사용한다.

```text
Task ID:
Owner / Reviewer:
현재 상태: DOCUMENTED | IMPLEMENTED | TEST_VERIFIED | RUNTIME_VERIFIED | BLOCKED
이번에 확인한 증거:
막힌 이유와 필요한 입력:
다음 Handoff:
```

## 10. 다음 스크럼에서 확정할 것

1. `CI-01`, `CI-06`, `MODEL-03`의 당일 Owner와 Merge 순서
2. `PLAT-01` Event Envelope과 `PLAT-02` 프로젝트 Redis의 공동 작업자
3. `ResearchPacket v1`, `risk.decision.v1`, `qa.decision.v1` 최종 Reviewer
4. Risk·QA 운영 Credential과 Governed Fund·Policy 준비 책임자
5. Wave 2 첫 Fixture 종목, Strategy Family와 Mandate 값
6. AI Office High 의존성 Upgrade 범위와 Kanban Bridge 시작일
7. `MODEL-04` Host Proxy를 실험용으로 허용할지와 비용·동시성 한도
8. `RPT-01` 생성 Markdown의 Git, Object Storage와 Notion 보존 경계

## 11. 현재 결론

Research는 실제 수집·MCP·Hermes·PIT·DQ까지 가장 앞서 있고 Risk·QA도 Prototype을 넘어 API, Event,
Repository와 Harness를 갖췄다. 그러나 Risk·QA는 아직 서비스로 실행되지 않고 Trading·Accounting·Governance는
Canonical DB를 사용하지 않는다. 다음 성공 기준은 코드 수가 아니라 **한 Paper Investment Case의 전사 폐쇄 루프**다.

매일의 Scrum은 이 문서의 Task ID와 팀별 `Yesterday / Today / Blocker`를 기준으로 진행하며, 실행 증거가
바뀔 때 중앙 상태와 담당자 가이드를 같은 PR에서 함께 갱신한다.
