# Personal Hedge Fund Agent - Technology Stack Decisions

> 문서 상태: Core Stack v1.5
> 최상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)  
> 범위: Core Paper Trading 구현  
> 원칙: 사용자가 지정한 필수 도구를 유지하되 기능 중복과 Vendor Lock-in을 최소화한다.  
> 관련 문서: [HEDGE_FUND_IMPLEMENTATION_BACKLOG.md](HEDGE_FUND_IMPLEMENTATION_BACKLOG.md)
> 전사 데이터·부서별 Library 구현: [RESEARCH_DATA_SOURCES_AND_LIBRARIES.md](../03-data/RESEARCH_DATA_SOURCES_AND_LIBRARIES.md)
> Frontend 구현 기준: [AI_OFFICE_FRONTEND_PLAN.md](AI_OFFICE_FRONTEND_PLAN.md)
> 조건부 고도화 기술과 도입 Trigger: [WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md](../01-product/WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md#8-기술-스택-고도화-연구)

## 1. 확정 스택 요약

### 1.1 사용자가 지정한 필수 도구

| 도구 | 결정 | 프로젝트 역할 |
|---|---|---|
| Hermes Agent | 필수 | 사용자-facing CIO/Supervisor, Tool·Skill 실행, 장기 사용자 Context |
| LangGraph | 필수 | 투자위원회, Strategy Promotion과 승인 가능한 상태 Workflow |
| Amazon Bedrock Claude | 필수 | Production/통합 환경의 주 LLM |
| Ollama | 필수 | 로컬 개발, Offline Test, 저비용 보조 모델과 Embedding |
| Supabase PostgreSQL | 필수 | PostgreSQL, pgvector, 문서·Artifact Storage (사용자 Auth 제외) |
| Docker | 필수 | 서비스별 Runtime 격리와 재현 가능한 개발 환경 |
| Render | 보류 | 초기 Demo 배포 후보, 실시간 Worker 적합성 검증 전 미확정 |
| Frontend | `ai-office` 기반 Next.js + React + TypeScript | Pixel Office와 운영 Dashboard를 결합한 Operator Control Plane |

Supabase pgvector가 기본 RAG Vector Store다. Risk/QA의 정적 규정·정책 코퍼스에 한한
Pinecone 예외는 [ADR-0006](adr/0006-pinecone-for-risk-qa-static-corpora.md)과 3.5절을
따른다.

### 1.2 추가 확정 권장 도구

| 영역 | 권장 선택 | 필수도 |
|---|---|---|
| Backend API | FastAPI | P0 필수 |
| Schema | Pydantic v2 | P0 필수 |
| ORM/SQL | SQLAlchemy 2 + Alembic + asyncpg | P0 필수 |
| Package Manager | uv | P0 필수 |
| Queue/Hot State | Redis + redis-py | P0 필수 |
| Market Data | LS증권 Open API + KRX Tick Collector Adapter | P0 확정 |
| Time-Series DB | 별도 TimescaleDB + Parquet 장기 Archive | P0, 리서치·퀀트 전용 |
| Agent Model Adapter | langchain-aws + langchain-ollama | P0 필수 |
| LangGraph Persistence | langgraph-checkpoint-postgres | P0 필수 |
| DataFrame/Feature | Polars + NumPy | P0 필수 |
| File Format | PyArrow + Parquet | P0 필수 |
| Local Analytics | DuckDB | P0 필수 |
| Backtest | vectorbt, Strategy Adapter 뒤에서 사용 | P0 권장 |
| Market Calendar | exchange-calendars | P0 필수 |
| HTTP/WebSocket | httpx + websockets | P0 필수 |
| Retry/Resilience | tenacity | P0 필수 |
| Logging | structlog | P0 필수 |
| Telemetry | OpenTelemetry + Prometheus Client | P1 필수 |
| Error Tracking | Sentry | P1 권장 |
| Frontend Framework | Next.js + React + TypeScript, 현재 `ai-office` Baseline | P0 확정 |
| Server State | TanStack Query | P0 확정 |
| Runtime Schema | Zod | P0 확정 |
| Table | TanStack Table | P0 확정 |
| Chart | TradingView Lightweight Charts | Market View P0 확정 |
| UI Primitive | shadcn/ui + Radix UI | P0 권장, 기존 Pixel UI와 함께 사용 |
| Icon | lucide-react | P0 필수 |
| Frontend Test | Vitest + React Testing Library + Playwright | P0 필수 |
| Backend Test | pytest + pytest-asyncio + Hypothesis | P0 필수 |
| Integration Test | Testcontainers | P0 권장 |
| Load Test | Locust | P1 필수 |
| Lint/Type | Ruff + Pyright | P0 필수 |
| Security Scan | pip-audit + Bandit + Trivy | P1 필수 |
| CI | GitHub Actions | P0 필수 |

라이브러리 Version은 이 문서에 고정 숫자로 적지 않는다. `uv.lock`, Frontend Lockfile과 Container Digest로 고정하고 월 1회 의존성 갱신 PR에서 검증한다.

### 1.3 부서별 추가 Library 결정

전 부서가 아래 Package를 한 Image에 모두 설치하지 않는다. 공통 Contract Package와 본부별 Runtime Image를 분리하며, 상세 데이터·API·권한 기준은 [RESEARCH_DATA_SOURCES_AND_LIBRARIES.md](../03-data/RESEARCH_DATA_SOURCES_AND_LIBRARIES.md)의 6장을 따른다.

| 조직 | P0 Library | P1 이후 후보 | 결정 원칙 |
|---|---|---|---|
| CEO | Hermes, LangGraph, Pydantic, HTTPX, Jinja2 | OpenTelemetry | 집계 API만 조회, SQL Client 제외 |
| Agent Workforce 인사팀 | Pydantic, JSON Schema, SQLAlchemy, Polars | Prometheus Client, OpenTelemetry | Eval 원본은 QA 소유 |
| 리서치본부 | HTTPX, WebSockets, Polars, PyArrow, Arelle, Kiwi, pgvector | Trafilatura, Pandera, OCR | 외부 Source 수집의 Business Owner |
| 트레이딩본부 | Decimal, Pydantic, Redis, Polars, NumPy, exchange-calendars | SciPy, CVXPY | Agent는 Order Intent까지만 생성 |
| 리스크본부 | Decimal, Pydantic, NumPy, Polars, Hypothesis | SciPy, Statsmodels, CVXPY, QuantLib | Risk 수치와 Limit은 결정론적 Service |
| 퀀트/백테스트본부 | Polars, NumPy, PyArrow, DuckDB, vectorbt, pytest | scikit-learn, Optuna, MLflow, Pandera | PIT Dataset과 Experiment Manifest 필수 |
| 투자철학 모델 Factory | Pydantic, Dataset Manifest, Frozen Eval | Transformers, PEFT, TRL, Accelerate, vLLM, MLflow | P2 조건부. Prompt/RAG Baseline 실패와 QA 승인 후 Adapter Training |
| 회계/포트폴리오본부 | Decimal, Pydantic, SQLAlchemy, Polars, PyArrow, Jinja2 | DuckDB, openpyxl | Ledger·Position·NAV는 Transaction Service 전용 |
| AI QA/감사본부 | pytest, Hypothesis, structlog, 보안 Scanner | OpenTelemetry, Prometheus, Ragas, MLflow, Sentry | LLM Judge 단독 승인 금지 |

새 후보 중 `CVXPY`, `Optuna`, `MLflow`, `Ragas`, `QuantLib`은 이름만으로 도입하지 않는다. 각각 제약 최적화, Trial 관리, Experiment Lineage, RAG Eval, 파생상품 Pricing의 실제 요구와 운영 Fixture가 준비될 때 ADR을 통과시킨다.

## 2. Hermes와 LangGraph 역할 분리

두 도구를 같은 Agent Loop에 중첩하면 상태, 재시도, Tool 호출과 Memory 소유권이 모호해진다. 다음 경계를 고정한다.

### Hermes가 담당

- 사용자의 자연어 Mandate와 운영 명령 접수
- CIO형 대화 Interface
- 장기 사용자 Preference와 Skill
- 일정 작업과 Daily Report 요청
- 허용된 Tool/MCP 호출
- LangGraph Workflow 시작·중단·상태 조회
- 사용자 승인 요청과 결과 전달

### LangGraph가 담당

- Research → Bull/Bear → Portfolio Workflow
- 구조화 State와 Node별 입력·출력
- 조건부 Routing, Retry와 Timeout
- Human-in-the-loop Interrupt
- Agent Decision Checkpoint
- Strategy Review와 Promotion Workflow
- 실패 후 Resume

### 담당하지 않는 기능

- Hermes와 LangGraph 모두 주문을 직접 Broker에 전송하지 않는다.
- 두 도구 모두 Risk Limit, OMS State와 Ledger를 직접 수정하지 않는다.
- Market Tick을 Agent Graph State에 저장하지 않는다.
- 대용량 문서·Feature는 Graph State가 아니라 ID Reference만 저장한다.

LangGraph는 PostgreSQL Checkpointer를 지원하므로 Supabase PostgreSQL에 별도 Schema를 만들 수 있다. Checkpoint에는 큰 Payload를 넣지 않고 Event, Feature와 Document ID만 기록한다.

### Hermes Memory와 자기 개선 경계

Hermes를 채택하는 핵심 이유는 대화 Interface만이 아니다. 각 본부장이 이전 업무에서 얻은 교훈을 다음 업무에 재사용하고, 반복되는 절차를 Version이 있는 Skill 후보로 만들 수 있기 때문이다. 이때 세 가지 저장 수단을 구분한다.

| 수단 | 용도 | 저장하지 않는 것 |
|---|---|---|
| Hermes Memory | 본부 Mandate, 반복되는 업무 교훈, 사용자 Preference와 짧은 운영 원칙 | 현재 Position, Cash, PnL, Risk Limit과 주문 상태 |
| Session Search | 과거 작업의 근거와 맥락을 다시 찾는 보조 수단 | 공식 감사 원장과 최신 운영 상태 |
| Hermes Skill | 검증된 조사·분석·보고 절차를 재사용하는 절차적 Memory | 승인되지 않은 전략 코드와 Production 권한 변경 |

Hermes가 관찰한 개선점은 즉시 자기 Prompt나 Tool 권한에 반영하지 않는다. `ImprovementCandidate`로 구조화해 근거, 예상 효과, 위험, 영향받는 Profile·Skill·Workflow Version과 Rollback 대상을 기록한다. AI QA/감사본부의 고정 Eval, 인사팀의 Build-vs-Extend 검토, Shadow 실행과 승인 Gate를 통과한 Version만 활성화한다.

따라서 재귀적 자기 개선은 다음 폐쇄 루프로 구현한다.

```text
업무 실행 -> 결과·오류 관찰 -> 개선 후보 등록 -> 독립 Eval
         -> Shadow/Champion-Challenger -> 승인된 Version 배포
         -> 운영 지표 관찰 -> 유지 | Rollback | 다음 개선 후보
```

공식 수치와 상태의 Source of Truth는 항상 Supabase·TimescaleDB·OMS·Ledger다. Hermes Memory는 해당 Record와 Evidence ID를 가리킬 수 있지만 이를 대체하지 않는다. 조직 전체 단계와 승인 책임은 [마스터 플랜 5.10](../HEDGE_FUND_MASTER_PLAN.md#510-hermes-memory-기반-조직-재귀적-자기-개선), 저장 규칙은 [데이터 거버넌스 지침 18.5](../03-data/DATA_GOVERNANCE_GUIDE.md#185-hermes-memorysession-searchskill-거버넌스)를 따른다.

## 3. LLM과 Embedding 구성

### 3.1 Model Gateway

Application은 다음 내부 Interface만 사용한다.

```python
class ModelGateway:
    async def generate(self, request: ModelRequest) -> ModelResponse: ...
    async def embed(self, texts: list[str]) -> EmbeddingResponse: ...
```

구현 Adapter:

- `BedrockClaudeAdapter`: Amazon Bedrock의 Claude 호출
- `OllamaChatAdapter`: 로컬 Chat Model
- `BedrockEmbeddingAdapter`: Production Embedding 후보
- `OllamaEmbeddingAdapter`: 로컬 Embedding

독립 LangGraph Worker Graph에서는 직접 `boto3`나 Ollama URL을 호출하지 않고 Gateway와 allow-listed tool을 주입한다.

### 3.2 Bedrock Claude

- `langchain-aws`의 Bedrock Chat Integration 또는 AWS SDK를 사용한다.
- 공통 Model Gateway에서는 Bedrock Converse/Messages 계열 API를 감싼다.
- Model ID, Region, Temperature, Max Token과 Prompt Version을 설정으로 관리한다.
- AWS Credential은 환경변수 Access Key보다 Workload Identity/Profile을 우선한다.
- Token, Latency, Error, Throttle과 추정 비용을 기록한다.
- Claude 장애 시 신규 Agent 분석만 중단하며 Risk/OMS는 계속 동작한다.

AWS를 전체 배포 Cloud로 선택하지 않아도 Bedrock을 Model Provider로 사용할 수 있다.

### 3.3 Ollama

- 로컬 개발과 CI의 선택적 Agent Test에 사용한다.
- 빠른 Event 분류와 요약용 소형 Model을 둔다.
- Prompt 구조는 Bedrock Adapter와 동일하게 유지한다.
- Test에서는 Model Tag 또는 Digest를 고정한다.
- Ollama 결과와 Bedrock 결과를 동일하다고 가정하지 않는다.
- Risk, 주문수량과 PnL처럼 결정론이 필요한 계산에는 사용하지 않는다.

### 3.4 Embedding 주의사항

Claude는 RAG Vector 생성을 위한 Embedding Model 역할로 사용하지 않는다. Ollama Embedding과 Bedrock Embedding은 Vector Space가 다르므로 하나의 Index에 혼합하지 않는다.

```text
embedding_model_id
embedding_dimension
embedding_version
index_version
```

위 값을 모든 Chunk에 저장하고 Model 변경 시 새 Index를 만들어 재색인한다.

### 3.5 Vector Store 분리: pgvector vs Pinecone

기본값은 Supabase pgvector다. [ADR-0006](adr/0006-pinecone-for-risk-qa-static-corpora.md)에
따라 다음 예외만 Pinecone을 쓴다.

| 데이터 | Vector Store | 이유 |
|---|---|---|
| Risk/QA 정적 규정·정책 코퍼스 (`compliance-policy-worker`, `hallucination-critic-worker`) | Pinecone | Order/Ledger와 SQL Join 불필요, Metadata Filter로 PIT 후보 축소, 트레이딩 hot path와 IOPS/CPU 격리 |
| 그 외 모든 RAG Evidence (research·execution·accounting에 Join되는 Evidence 포함) | Supabase pgvector | 관계형 Join 필요, Source of Truth 단일화 |

- `skills/agentic-rag/src/retriever.py`의 `search()` 인터페이스(`DocumentChunk` in,
  `list[ScoredChunk]` out)는 두 Store 모두에서 동일하게 유지한다 — Adapter만 바뀐다.
- Pinecone Index는 Risk/QA가 같은 Index를 쓰더라도 **Namespace를 부서별로 분리**한다
  (`risk-compliance-policy`, `qa-hallucination-reference`) — 현재 Baseline이 이미
  `corpus/compliance/`와 `corpus/evidence/`로 완전히 나눠져 있는 것과 같은 경계다.
- Pinecone Index에도 3.4의 `embedding_model_id`/`embedding_dimension`/
  `embedding_version`/`index_version`을 Metadata로 저장한다.
- RAG 감사 Trail(`audit.rag_runs` 등)은 Vector Store 선택과 무관하게 항상 Supabase에
  남는다. Pinecone은 검색 백엔드일 뿐 감사 Source of Truth가 아니다.
- 최종 PIT 필터·인용 검증은 Vector Store와 무관하게 항상 결정론적 Python
  (`skills/agentic-rag/src/nodes.py`)이 한다. Pinecone Metadata Filter는 후보를
  좁히는 1차 단계일 뿐이다.

## 4. Supabase 사용 범위

### Supabase에 저장

- Mandate와 Version
- Instrument Master와 Universe Snapshot
- Agent Decision과 Evidence Metadata
- Strategy, Backtest Run과 Promotion
- Risk Decision, Order, Fill, Position과 Portfolio Snapshot
- LangGraph Checkpoint 전용 Schema
- RAG Document Metadata와 pgvector (Risk/QA 정적 코퍼스 예외는 3.5, ADR-0006 참고)
- 사용자 Auth와 Dashboard 접근 권한
- 문서, Parquet와 Model Artifact의 Storage Object Metadata

### Supabase에 저장하지 않음

- 모든 Tick을 개별 PostgreSQL Row로 영구 적재
- Market WebSocket Message Bus
- 초고빈도 Hot Quote Cache
- Process 간 실시간 Queue
- Secret 원문
- 대용량 LangGraph State Blob

### DB 소유권 경계

- 전사 업무 데이터의 기본 System of Record는 Supabase PostgreSQL이다.
- `research`, `quant`, `strategy`, `execution`, `accounting`, `risk`, `audit`, `workforce`, `governance` Schema를 분리한다.
- 별도 TimescaleDB는 재일님 담당 리서치·퀀트 Data Plane만 직접 사용한다.
- 트레이딩·리스크·회계·QA·CEO·인사팀은 TimescaleDB Credential 없이 `market-api`의 Snapshot·Bar·Feature Endpoint를 사용한다.
- Supabase PostgreSQL 17에서 TimescaleDB Extension이 Deprecated 상태이므로 신규 시계열 구조를 Supabase 내부 Extension에 종속시키지 않는다.

### 연결 규칙

- FastAPI의 장기 실행 Backend는 용도에 맞는 Direct 또는 Session Pool 연결을 사용한다.
- Serverless/짧은 Transaction은 Transaction Pooler를 사용한다.
- Connection Pool 상한과 PostgreSQL 최대 연결 수를 함께 관리한다.
- SQLAlchemy Pool 위에 무분별하게 외부 Pool을 겹치지 않는다.
- Migration은 Supabase Dashboard 수동 변경이 아니라 Alembic으로 수행한다.
- Frontend는 로컬 fixture의 고정 데모 ID를 사용하고, 금융 상태 조회와 모든 Command는 FastAPI BFF를 통한다.
  외부 사용자 로그인·세션은 현재 구현하지 않는다.
- Row Level Security를 활성화하되 Service Role Key를 Browser에 노출하지 않는다.

Supabase Realtime은 Dashboard 상태 알림에는 사용할 수 있지만 Market Data 전송 계층으로 사용하지 않는다.

## 5. Redis 사용 결정

Supabase만으로 다음 기능을 안정적으로 대체하기 어려우므로 Redis를 P0 추가 도구로 확정한다.

- 최신 Quote와 Feature Hot Cache
- Event Priority Queue
- Deduplication과 Cooldown
- Agent Job Lease와 Rate Limit
- WebSocket Dashboard Pub/Sub
- 짧은 수명의 Trading Session State

초기에는 Docker Redis를 사용한다. 영구 Source of Truth는 PostgreSQL과 Parquet이며 Redis 유실 후 재구성할 수 있어야 한다.

`Celery`는 초기 Core에 도입하지 않는다. Streaming Worker와 LangGraph Worker를 명시적 `asyncio` Process로 운영하고, Job 복잡도가 커질 때 Dramatiq 또는 Temporal을 별도 ADR로 검토한다.

## 6. 데이터 처리와 Backtest

### Polars

- 실시간 Window 집계와 Feature Batch 계산
- Point-in-Time Dataset 가공
- 엄격한 Schema와 Lazy Query
- Pandas 사용은 외부 Library Adapter 경계로 제한

### PyArrow와 Parquet

- Raw/Normalized Market Event Archive
- Feature Snapshot과 Backtest Dataset
- 날짜·시장·종목군 Partition
- Schema Metadata와 Dataset Version

### DuckDB

- 로컬 Parquet Query
- Data Quality 검사
- Backtest Dataset 탐색
- 운영 PostgreSQL에 분석성 대형 Query를 보내지 않기 위한 Local Analytics

### vectorbt

- Ranking, Factor, Signal과 단순 Event Strategy의 빠른 Parameter 탐색과 Backtest에 사용한다.
- `BacktestEngine` Interface 뒤에 배치해 향후 다른 Engine으로 교체할 수 있게 한다.
- 체결, 비용과 Slippage Model은 명시적으로 Version 관리한다.
- Paper OMS의 상태 머신과 Risk Engine은 vectorbt에 맡기지 않는다.

### Multi-Strategy Engine 경계

전략마다 Backtest Library가 다를 수 있으므로 하나의 엔진에 모든 전략을 억지로 맞추지 않는다. 대신 모든 엔진을 공통 `StrategyPlugin`과 `BacktestEngine` Interface 뒤에 둔다.

| 전략 유형 | 우선 도구 | 사용 범위 |
|---|---|---|
| Ranking·Factor·단순 Signal | vectorbt | 대규모 Parameter 탐색과 횡단면 Backtest |
| Pair·Market Neutral·통계 차익 | statsmodels, SciPy, scikit-learn | Cointegration, Factor Neutralization과 검증 |
| Portfolio·Risk Allocation | CVXPY | Gross/Net, Factor, Turnover와 Risk Budget 제약 최적화 |
| 상태 의존 Event·주문 Simulation | 자체 Event Harness, 필요 시 NautilusTrader 비교 | 부분 체결, 여러 Leg와 상태 기반 전략 |
| Futures·Options Analytics | NumPy/SciPy, 검증 후 QuantLib | Curve, Pricing, Greeks와 Scenario Fixture |
| ML Strategy | scikit-learn, 필요 시 LightGBM/Optuna/MLflow ADR | Walk-forward 학습, 실험 추적과 Model Registry |

각 Strategy Plugin은 `required_data_products`, `required_capabilities`, `signal_schema`, `target_portfolio_schema`, `cost_model_id`와 `risk_model_id`를 선언한다. Plugin은 Order를 직접 만들지 않고 Target Portfolio까지만 반환한다.

대표 전략군 Benchmark에서 vectorbt의 Cross-sectional 처리, Corporate Action 또는 주문 모델이 부족하면 전략군별로 `NautilusTrader`, `Zipline-reloaded` 또는 별도 Engine을 비교하고 ADR을 남긴다. Engine 교체가 Strategy/Risk/OMS Domain Contract를 바꾸게 해서는 안 된다.

## 7. Backend

### Runtime

| Service | Runtime |
|---|---|
| Domain Backend/LangGraph | Python 3.12 |
| Hermes | Hermes가 공식 지원하는 독립 Python Runtime, 현재 개발 경로는 3.11 기준 |
| Frontend | 현재 LTS Node.js |

Hermes를 Domain Backend의 Python Environment에 직접 설치하지 않는다. 독립 Image와 API/MCP 경계로 통신한다.

### 필수 Python Package 그룹

```text
api:
  fastapi, uvicorn, pydantic, pydantic-settings

database:
  sqlalchemy, alembic, asyncpg, supabase

agent:
  langgraph, langgraph-checkpoint-postgres,
  langchain-aws, langchain-ollama

streaming:
  websockets, httpx, redis, tenacity

data:
  polars, numpy, pyarrow, duckdb, vectorbt,
  exchange-calendars

operations:
  structlog, opentelemetry-api, opentelemetry-sdk,
  prometheus-client

test:
  pytest, pytest-asyncio, hypothesis, respx, testcontainers

quality:
  ruff, pyright, pip-audit, bandit
```

Vendor Market Data와 Broker SDK는 공급자 확정 후 Adapter Package로 추가한다.

## 8. AI Office Frontend 결정

현재 `ai-office/`의 Next.js·React·TypeScript Pixel Office를 Frontend Baseline으로 확정한다. 원본 12개 부서는 8개 조직·2개 층으로 전환됐고 Trading/Portfolio DEMO Snapshot과 `apps/api/main.py` BFF가 추가됐다. 다만 Scripted Simulation과 테스트 Paper Loop를 금융 운영 상태로 사용하지 않는다. 실제 Agent 상태는 Hermes Kanban Status Bridge의 `agent.status.v1`, Supabase Read Model과 FastAPI REST·WebSocket Adapter로 단계적으로 교체한다.

```text
Next.js + TypeScript
TanStack Query
TanStack Table
TradingView Lightweight Charts
shadcn/ui + Radix UI
lucide-react
Zod
Vitest + React Testing Library + Playwright
```

현재 `vinext`, Vite, Cloudflare Worker와 Wrangler 구성은 Prototype Hosting Baseline으로만 유지한다. 전체 Cloud Provider, 금융 Backend Hosting과 Production Frontend Hosting은 별도 결정이며 Cloudflare D1·Drizzle을 금융 Source of Truth로 사용하지 않는다.

실시간 연결은 `GET /ui/snapshot` 다음 FastAPI `/ws/operations` 순서다. WebSocket Event는 Sequence, Schema Version과 Server Time을 포함하고 Client는 Heartbeat, 재연결, Gap Recovery와 Staleness를 구현한다. 전 종목 Tick을 Pixel Office로 직접 전송하지 않고 Feed Health와 집계 Event를 제공한다.

Hermes Kanban은 Agent 업무 상태 Source로 재사용한다. 같은 Runtime 경계의 읽기 전용 Bridge가
Task·Assignee 변경을 `agent.status.v1`로 Redis Streams에 발행하고 Projector가 Supabase Agent Status
Read Model을 갱신한다. Browser·BFF의 SQLite 직접 접근과 Task 수정은 금지한다. 공통 Frontend Platform
기술 DRI는 도현님, Live Office Business Owner는 영주님, Risk·QA Reviewer는 동규님이다.

### Frontend 책임

- 시장과 Feed Health 조회
- 상위 Event와 Agent Decision 조회
- Portfolio, Exposure, PnL과 Drawdown
- Strategy 상태, Backtest와 Promotion 승인
- Order와 Fill 조회
- Trading State, Strategy Pause와 Kill Switch
- CEO Office, 6개 본부와 Agent Workforce의 Queue·SLA·Handoff·Incident
- Agent Profile·Skill·Tool·Model Version, Trace와 개선 후보
- `DEMO/PAPER/LIVE`, 연결 상태와 마지막 갱신 시각의 상시 표시

### Frontend 금지

- Browser에서 Bedrock/Ollama 직접 호출
- Supabase Service Role 사용
- Risk 계산과 주문 상태 결정
- Broker Credential 보관
- Database Table 직접 거래 Write
- 캐릭터 Animation이나 Browser State를 공식 Agent·Risk·주문 상태로 간주
- `DEMO`, `PAPER`와 `LIVE` 데이터 혼합

FastAPI가 모든 위험한 Command의 유일한 Backend Entry Point다.

## 9. Docker 구성

Core의 `docker compose` 서비스는 다음으로 제한한다.

```text
api                 FastAPI, Risk, OMS, Portfolio
streaming-worker    WebSocket, Normalize, Feature, Event
agent-worker        LangGraph Workflow
hermes              CIO/Supervisor Runtime
frontend            ai-office 기반 Next.js Operator Control Plane
redis               Queue와 Hot State
ollama               Local Model
otel-collector      Trace와 Metric
```

Supabase Cloud를 사용하면 로컬 Compose에 PostgreSQL을 중복 실행하지 않는다. Offline 통합 Test가 필요할 때만 Supabase CLI 또는 Testcontainers PostgreSQL Profile을 사용한다.

각 Image는 Healthcheck, Non-root User, 고정 Dependency Lock과 읽기 전용 Filesystem 가능 여부를 가진다.

## 10. Render 보류 기준

Render는 Dashboard, FastAPI와 낮은 빈도의 Agent Worker Demo에는 후보가 될 수 있다. 하지만 다음 검증 전 Market WebSocket과 Trading Worker 배포 대상으로 확정하지 않는다.

- Always-on Process와 재시작 정책
- WebSocket 장시간 연결 안정성
- 고정 Egress IP 필요 여부
- Private Network와 Supabase/AWS 지연
- Persistent Disk 필요성
- Background Worker Scaling
- Deploy 중 연결 Drain
- 월 비용과 Log Retention

Render 검증 실패 시 Cloud Provider 선정 전에는 단일 VM과 Docker Compose를 기본 배포안으로 사용한다.

## 11. Observability와 품질 도구

### P0

- `structlog`: JSON Log와 공통 Trace ID
- `Ruff`: Format과 Lint
- `Pyright`: Type Check
- `pytest`, `pytest-asyncio`: Unit/Async Test
- `Hypothesis`: Risk, 수량, OMS 상태 Property Test
- `Playwright`: 선택된 운영 UI의 핵심 Dashboard와 Kill Switch E2E
- `GitHub Actions`: Test와 Image Build

### P1

- OpenTelemetry: Agent-to-Order Trace
- Prometheus Client: Feed, Queue, Risk와 OMS Metric
- Sentry: Backend/Frontend Exception
- Locust: API와 Event Ingestion 부하
- Trivy: Container/Image 취약점
- pip-audit/Bandit: Python 의존성과 정적 보안 검사

LangSmith는 선택적 개발 추적 어댑터다. 기본 tracing은 `LANGSMITH_TRACING=false`로 비활성화하며, 금융 데이터 마스킹·외부 전송 정책·비용·자격증명·네트워크를 확인한 실제 run만 연결 성공으로 기록한다. 현재 Source of Truth는 OpenTelemetry와 자체 Agent Run Record이고, 코드나 API Key가 있다는 사실만으로 LangSmith 연결 완료로 표시하지 않는다.

**Langfuse (2026-08-10 신규 도입, 영주 결정)** — LangSmith와 이중 계측되는 두 번째 선택적 추적 어댑터다. 기본값은 비활성(`LANGFUSE_TRACING=false`).

- **기존 스택으로 못 푸는 문제**: LangSmith는 이미 8개 부서 전부에 배선돼 있지만, HR(07-agent-workforce)이 6개 투자본부 Worker의 "최근 실행 시각"만 안전하게 조회할 방법이 없었다. LangSmith Trace 원문에는 Mandate·제한종목 질의응답 같은 Risk/Compliance 내용이 그대로 담겨 있어(.env.example 3-1절), HR이 그 클라이언트로 직접 조회하면 권한 밖 데이터에 노출될 위험이 있다. Langfuse를 별도로 붙여 HR 전용 조회 경로(`departments/07-agent-workforce/scorecard/observability.py`)를 LangSmith와 분리했다 — 같은 SDK를 공유하지 않으므로 HR의 접근 범위가 코드 구조상으로도 LangSmith 원본과 섞이지 않는다.
- **HR이 읽는 것**: `orchestration/llm_observability.py`의 `publish_langfuse_metric()`이 `_metric_metadata()`로 redact된 필드(worker_id·stage·상태·지연시간 등)만 이벤트로 보내고, HR은 그 이벤트의 timestamp만 조회한다. input/output(원문)은 항상 비어 있다.
- **제거 기준**: LangSmith 쪽에 부서별 접근 범위 제한(예: HR 전용 Project/API Key 분리)이 구현되면 Langfuse는 불필요해진다 — 그 시점에 이 어댑터를 제거하고 HR도 LangSmith 하나로 합친다.

## 12. Secret과 설정

- 개발 설정은 `.env.example`만 Version 관리한다.
- 실제 Key를 `.env`, Log, Prompt와 Frontend Bundle에 넣지 않는다.
- AWS Credential은 Local Profile 또는 Workload Identity를 사용한다.
- Supabase URL, Anon Key와 Service Role을 구분한다.
- Service Role, Database URL과 Broker Key는 Backend 전용이다.
- Ollama Model, Bedrock Model ID와 Prompt Version은 Config로 관리한다.
- Pydantic Settings로 환경변수를 검증하고 시작 시 누락을 실패 처리한다.

## 13. 확정하지 않은 항목

다음은 정보가 부족하므로 지금 Library를 고정하지 않는다.

| 항목 | 확정 조건 |
|---|---|
| LS Adapter Dependency | 현재 KRX Tick Collector Lockfile과 LS 공식 가이드 대조 후 고정 |
| Paper/Live Broker SDK | Broker 선정 |
| Embedding Model | 한국어/영어 문서와 검색 평가 |
| Render | 장시간 WebSocket/Worker Benchmark |
| 전체 Cloud Provider | 별도 Cloud 평가 |
| Backtest 대체 Engine | 대표 전략군별 Benchmark와 주문·비용 모델 적합성 결과 |
| Alert 채널 | Telegram, Email, Slack 등 운영방식 선정 |

## 14. 구현 순서

1. `uv`, Python, Docker Compose와 Repository Scaffold
2. Supabase Project, Schema, RLS와 Alembic
3. Redis Queue/Cache와 Event Contract
4. FastAPI Domain API
5. WebSocket/Feature Worker
6. Model Gateway와 Ollama Adapter
7. Bedrock Claude Adapter
8. LangGraph Decision Workflow와 Postgres Checkpoint
9. Hermes API/MCP Adapter
10. RAG와 pgvector
11. Backtest Adapter와 Strategy Registry
12. Risk/OMS/Portfolio
13. AI Office 조직 Projection, Snapshot·WebSocket와 Operator Control
14. Observability, Security Scan와 E2E Test

## 15. 기술 선택 완료 조건

- [ ] Hermes와 LangGraph의 소유 State와 호출 방향이 분리됐다.
- [ ] Bedrock/Ollama가 동일 Model Gateway Contract를 구현한다.
- [ ] Embedding Model별 Vector Index가 분리된다.
- [ ] Supabase에 Tick Stream을 Row 단위로 적재하지 않는다.
- [ ] Redis 장애 후 Source of Truth에서 상태를 재구성한다.
- [ ] Frontend가 Risk, OMS와 Database 거래 상태를 직접 수정하지 않는다.
- [ ] AI Office가 8개 조직과 `DEMO/PAPER/LIVE` Mode를 구분하고 공식 Backend 상태만 표시한다.
- [ ] WebSocket Sequence Gap과 재연결 후 Snapshot 정합성 회복을 E2E로 검증한다.
- [ ] Python과, Frontend를 도입한 경우 JavaScript Dependency가 Lockfile로 고정된다.
- [ ] 모든 서비스가 Docker Healthcheck를 가진다.
- [ ] Unit, Property, Integration과 Browser E2E Test가 CI에서 실행된다.

## 16. 참고 문서

- [Hermes Agent Repository](https://github.com/NousResearch/hermes-agent)
- [LangGraph Checkpoint Reference](https://langchain-ai.github.io/langgraph/reference/checkpoints/)
- [Amazon Bedrock API Options](https://docs.aws.amazon.com/bedrock/latest/userguide/apis.html)
- [Ollama Documentation](https://docs.ollama.com/)
- [Supabase PostgreSQL Connections](https://supabase.com/docs/guides/database/connecting-to-postgres)
- [Supabase Vector Columns](https://supabase.com/docs/guides/ai/vector-columns)
- [Polars Streaming](https://docs.pola.rs/user-guide/concepts/streaming/)
- [DuckDB Parquet](https://duckdb.org/docs/stable/data/parquet/overview)
- [OpenTelemetry Python](https://opentelemetry.io/docs/languages/python/)

## 17. 최종 결정

> Hermes는 사용자-facing CIO Supervisor, LangGraph는 투자 Workflow, Bedrock Claude는 주 LLM, Ollama는 로컬·저비용 Model, Supabase PostgreSQL은 Transaction·Vector·Storage, 별도 TimescaleDB는 리서치·퀀트 시계열, Redis는 Queue·Hot State, Docker는 Runtime 경계로 사용한다. 이 로컬 모의투자 범위에는 사용자 Auth·로그인·세션을 구현하지 않는다. FastAPI/Pydantic/SQLAlchemy가 Domain API를 구성하고 Polars/Parquet/DuckDB가 시장 데이터와 연구 Dataset을 처리한다. Frontend는 `ai-office` 기반 Next.js·React·TypeScript로 확정하며 Pixel Office와 업무 Dashboard를 결합한다. UI는 공식 Backend 상태의 Projection과 승인 요청만 담당하고 Risk, OMS와 거래 원장은 결정론적 Backend가 독점한다.

Kafka/Redpanda, Flink, ClickHouse, Feast, Neo4j, Ray와 Kubernetes는 현재 Core 확정 스택이 아니다.
부하, Replay, Feature 일관성, Graph Query, 분산 연구 또는 배포 격리 문제가 실측되고 기존 스택이
정의된 SLO를 충족하지 못할 때만 [전사 고도화 연구 로드맵](../01-product/WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md)의
Trigger와 ADR을 통과해 도입한다. Qlib와 RD-Agent-Quant는 Strategy Factory의 격리 Spike
후보이며 Trading, Risk, OMS와 Ledger Runtime의 직접 Dependency로 넣지 않는다.
