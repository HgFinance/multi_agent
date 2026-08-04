# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 현재 상태

설계 중심의 초기 구현 단계다. 완전한 Application Scaffold와 End-to-End 서비스는 아직 없지만 실행 가능한 Prototype, Migration과 Schema Test는 존재한다.

[REPOSITORY_DEPARTMENT_STRUCTURE.md](docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md) 11절 "단계적 이전 계획"의 단계 1~3(Department Scaffold, Hermes Profile 이동, 본부 코드 이동)이 완료됐다.
`departments/<n>/` 8개 폴더가 실행 기준이며, Hermes Profile과 트레이딩·회계·리서치 코드가 여기로 옮겨졌다.

1. `docs/` — 설계 문서. 이 저장소의 Source of Truth. 목표 폴더 구조는 [REPOSITORY_DEPARTMENT_STRUCTURE.md](docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md)를 따른다.
2. `departments/<n>/hermes/` — 8개 부서 Profile(`config.yaml` + `SOUL.md`).
   Hermes Agent Runtime이 실제로 읽는 `~/.hermes/profiles/<department>/`와는 별개 사본이며,
   `scripts/sync_hermes_profiles.sh`로 동기화한다 (아래 "부서 Profile 규약" 참고).
3. 실행 가능한 코드 — `departments/01-research/collectors/news.py`, `skills/agentic-rag/`, 그리고 트레이딩·회계본부의
   거래 생명주기 구현(`db/`, `departments/02-trading/{contracts,oms,broker}/`, `departments/05-accounting-portfolio/{ledger,reconciliation}/`).

트레이딩·회계본부는 Sprint D0~D2 Prototype(계약·Paper OMS·원장·대사)이 있고, Risk의 `compliance-policy-agent`에는 Agentic RAG baseline이 있다. 다른 본부는 대부분 Profile과 설계 문서 단계다. DB에는 Supabase·TimescaleDB 통합 Migration과 Schema Contract Test가 별도로 존재한다.

트레이딩 OMS는 팀 가이드 v1.2의 상태 머신 2단 분리가 **반영 완료됐다**(2026-08-03 확인).
`IntentState`/`BrokerOrderState`가 별도 전이표를 갖고, `can_transition()`이 두 머신의 상태를
섞으면 `False`를 내며, Python 전이표가 `supabase/migrations/20260729000400_execution_risk_accounting.sql`의
`execution.order_state_transitions`와 대조 검증된다. 각 부서 `config.yaml`의 `implementation:`
블록이 무엇이 되고 무엇이 안 됐는지 표시한다.

구 경로 `orchestration/hermes/<department>/`, 루트 `trading/`, `execution/`, `accounting/`, 루트 `fetch_news.py`는
이동 후 삭제됐다 — 위 새 경로가 유일한 실행 경로다. 임시 CLI 호환 Wrapper는 예정보다 일찍(2026-07-30) 제거됐다.
DB Prototype 통합(단계 4)과 구조 Gate(단계 5)는 아직 진행 전이며, `db/`와 `references/`는 그대로다.

Frontend의 현재 실행 경로는 `ai-office/`이고 목표 경로는 `apps/operator-web/`다. 이름만 바꿔 현재 Demo를 금융 상태처럼 사용하지 않는다. 8개 조직, REST Snapshot, FastAPI WebSocket, Mode 분리와 Command 경계는 [AI Office Frontend Plan](docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md)을 따른다. Agent 업무 상태는 [ADR-0001](docs/02-engineering/adr/0001-hermes-kanban-agent-status-bridge.md)에 따라 Hermes Kanban을 읽기 전용 Bridge로 연결한다. 공통 Frontend Platform 기술 DRI는 도현님, Live Office Business Owner는 영주님, Risk·QA Reviewer는 동규님이다.

## 명령어

```bash
pip install -r requirements.txt
```

Hermes Runtime(`NousResearch/hermes-agent`)은 PyPI 패키지가 아니라 `requirements.txt`에 없다. 별도 저장소 지침대로 설치한다.

```bash
# 부서 단독 실행 — 각 config.yaml의 usage: 블록에 예시 있음
research-department chat -q 'Build a Research Packet for AAPL'
risk-management     chat -q 'Assess risk of AAPL long position'
ceo-agent           chat -q 'Summarize current portfolio decisions and open risks'

# Agentic RAG 단독 실행 (OPENAI_API_KEY 필요)
python3 skills/agentic-rag/main.py --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" --as-of 2026-07-29

# 뉴스 조회 (TAVILY_API_KEY 필요)
python3 departments/01-research/collectors/news.py 'AAPL Apple stock'
```

Canonical 운영 DB Migration은 `supabase/migrations/`, 시장 시계열 Migration은 `timescaledb/migrations/`다.

```bash
supabase db reset
python -m unittest discover -s tests/schema -p "test_*.py" -v
```

루트 `db/001_execution.sql`부터 `db/004_seed.sql`은 D0-D2 Prototype 전용이다. `supabase/migrations/`와 같은 Database에 함께 적용하지 않는다. 두 계열은 Fund/Book과 거래·회계 Table 계약이 다르며, 통합 절차는 [Database Schema Foundation](docs/database/README.md)의 Migration 권위 규칙을 따른다.

전체 Application Test Suite는 아직 없지만 `tests/schema/`의 `unittest` 계약 검사와 PostgreSQL/Timescale Runtime Smoke SQL이 있다. 트레이딩·회계 모듈도 각 파일의 `__main__`에 assert 기반 자체 점검을 둔다.

```bash
python departments/02-trading/contracts/contracts.py                 # 계약 6개 영역
python departments/02-trading/oms/oms.py                              # OMS 불변식 10개
python departments/02-trading/broker/paper_broker.py                  # Paper Broker 4개 영역
python departments/05-accounting-portfolio/ledger/ledger.py           # 원장 불변식 10개
python departments/05-accounting-portfolio/reconciliation/reconciliation.py  # 대사 12개
python apps/api/main.py                                          # Read-only DEMO BFF 6개 영역
```

목표 스택은 [TECH_STACK_DECISIONS.md](docs/02-engineering/TECH_STACK_DECISIONS.md)가 정한 `pytest + pytest-asyncio + Hypothesis + respx + testcontainers`와 `ruff + pyright + pip-audit + bandit`이다. 실제로 도입하면 위 자체 점검을 pytest로 옮기고 이 절을 갱신한다.

## Claude Code 작업 시 주의

### Risk·QA 브랜치 실행 환경

Risk·QA 부서 브랜치의 Python 테스트, 계약 검증, 실행 점검과 lint는 전용 Claude 환경을 사용한다.
작업 시작 전에 `source ~/claude/bin/activate`를 실행하고 `python --version`과
`command -v ruff`를 확인한다. 저장소 `.venv`와 `~/claude` 환경을 혼용하지 않으며,
CI workflow의 Python 버전과 로컬 검증 결과를 구분한다. `ruff`, `pyright`, `bandit`,
`pip-audit`가 없으면 설치 명령과 미설치 상태를 기록한다.

- When running `/graphify` or analyzing code graphs, execute strictly in a single thread without spawning subagents.

- **저비용 하위 에이전트 모델 기용**:
단순 파일 검색, 코드 리서치 등을 위해 Subagent를 생성해야 하는 경우, 무조건 부모의 고비용 모델을 상속(`inherit`)하지 말고 의도적으로 `flash`나 `flash_lite` 등 저렴하고 빠른 모델을 지정하여 실행한다.

`graphify-out/graph.json`(~1.3MB)과 `graph.html`(~1.1MB)은 `Read`로 직접 열지 않는다 — 컨텍스트를 한 번에 채워 조기 auto-compact를 유발한다. 저장소 구조나 연관관계 질문에는 `/graphify` 스킬의 query 흐름(`graphify query "<question>"` CLI, 또는 그 NetworkX 폴백)을 쓴다 — 둘 다 Bash/python으로 처리하고 작은 결과만 컨텍스트에 올린다. 사람이 읽는 요약이 필요하면 `graphify-out/GRAPH_REPORT.md`(~17KB)만 직접 읽는다. 그래프 파일 일부만 필요하면 `jq`/`grep`으로 필요한 조각만 추출한다.

## 아키텍처

### 부서 토폴로지와 5개 흐름

[multi-agent-workflow.yaml](multi-agent-workflow.yaml)에는 **서로 분리된 다섯 흐름**이 있다. 섞지 않는다.

| 흐름 | 순서 | 타임아웃 |
|---|---|---|
| `workflow` (실시간 신호) | research → trading → risk → qa → accounting → ceo | 420s |
| `strategy_research_cycle` (전략 연구) | quant-backtest → qa → ceo | 180s |
| `workforce_management_cycle` (인사 — 신규 채용) | hr → hr → qa → ceo → hr | 300s |
| `agent_evolution_cycle` (인사 — 기존 Agent 개선) | hr → hr → qa → ceo → hr | 300s |
| `event_routing` (동적 라우팅) | 이벤트 유형별 필요한 페르소나만 선택 호출, 고정 순서 없음 | — |

전략 연구 주기는 실시간 파이프라인과 분리돼 있다. 검증된 불변 Strategy Bundle만 트레이딩본부로 넘어가며, 실시간 운용 중 전략 코드를 직접 수정하지 않는다.

**`workforce_management_cycle`과 `agent_evolution_cycle`은 다른 목적이다.** 전자는 신규 채용, 후자는 이미 활성화된 Agent Profile 개선(프롬프트 수정 포함)이다. "이미 배포됐다"는 이유로 후자가 QA 독립검증·CEO 승인을 건너뛰지 않는다 — 프롬프트 한 줄을 고치는 것도 Agent Profile Version을 올리는 변경이다.

**모든 step은 `retry.max_attempts`와 `on_failure`를 가진다.** 실패를 통과로 취급해 다음 단계로 넘기지 않는다 — 안전한 기본값(REJECT/HOLD/DENY/ESCALATE/ROLLBACK)으로 떨어지며, 승인·승격·권한부여 방향으로 자동 fallback하지 않는다(`skills/agentic-rag`의 `grounded: false` 처리와 같은 원칙).

**`event_routing`은 8.1절 동적 라우팅(Expert Pool 패턴)을 구현한다.** 6.4절 이벤트 탐지가 발화시키며, `call: []`인 이벤트(예: `stale_market_data`, `loss_limit_approach`)는 에이전트를 부르지 않고 결정론적 코드가 즉시 처리한다는 뜻이다.

`hr-department`는 **제7의 투자 본부가 아니라 CEO 직속 Shared Service**다. 투자 본부는 리서치·트레이딩·리스크·퀀트/백테스트·회계/포트폴리오·AI QA/감사 6개뿐이다.

### Hermes(부서) vs LangGraph(직원) 실행 계층

**부서는 Hermes로 돌아가고, 부서 안의 직원(개별 페르소나)은 LangGraph로 작동한다.** 이 둘을 같은 층으로 섞지 않는다.

- Hermes Profile(8개 `config.yaml` = 8개 Supervisor)이 부서 단위 오케스트레이션·Queue·Memory Namespace·Tool Allowlist를 맡는다.
- 부서 소속 직원은 직원별 독립 LangGraph Worker Graph로 구현하고 사건별로 필요한 Worker만 동적으로 호출한다. Worker는 허용된 읽기 도구 결과를 `worker-context.v1`로 만들어 Hermes 부서장에게 전달하며 주문·Risk/QA 판정·원장·권한 변경은 수행하지 않는다.
- **현재 Worker Registry**: CEO 1, HR 5, Research 6, Trading 6, Risk 4, Quant/Backtest 7, Accounting/Portfolio 8, AI QA 5. 실제 런타임 수는 `workers`와 `runtime_personalities`를 기준으로 하며 `agent.personalities`의 기존 ID는 호환·감사 Alias로만 유지한다.
- **현재 실행 기준**: 모든 Worker는 독립 LangGraph + Ollama `qwen3:1.7b`를 사용한다. Worker별 경량·표준·중량 모델 배치는 [WORKER_MODEL_MATRIX.md](docs/02-engineering/WORKER_MODEL_MATRIX.md)의 benchmark·HR 제안·QA 검증·CEO 승인 절차를 거친 뒤에만 변경한다.

### 절대 깨면 안 되는 권한 분리

**부서 간 권한은 어떤 이유로도 이전되지 않는다.** 담당자가 같다고, 구현이 급하다고, 편의를 위해서라도 한 부서가 다른 본부의 권한(Risk의 거부권, QA의 감사 권한 등)을 대신 수행하거나 위임받지 않는다. Profile을 수정하거나 코드를 쓸 때 협상 대상이 아니다. 대부분의 페르소나 프롬프트가 이를 금지하는 문장을 명시적으로 갖고 있으므로, 프롬프트를 다듬을 때 그 문장을 지우지 않는다.

- Agent Decision ≠ Strategy Signal ≠ OrderIntent ≠ Order. 서로 다른 객체이며 같은 것으로 취급하지 않는다.
- 모든 주문은 결정론적 Risk Engine을 통과한다. `risk-management` 에이전트는 근거와 권고(approve/resize/reject)만 만들고, 바인딩 집행·한도 관리는 Risk Engine이 한다.
- `trader-pm-agent`는 주문을 직접 전송하지 않는다. Risk/Compliance Gate 통과가 선행 조건이다.
- CEO는 주문 제출, 리스크 승인, 원장 수정, NAV 확정, Audit Finding 종결 권한이 **없다**.
- `hr-department`는 자기 후보를 스스로 최종 승인할 수 없다. 권한 독립 검증은 AI QA/감사본부, 예산·조직 승인은 CEO, 실제 Identity/권한 생성은 Platform/IAM Service만 한다.
- `quant-backtest-department`는 Production 승격을 직접 하지 않는다. CEO·Risk·QA 승인이 필요하다.
- LLM은 관련성 판단과 서술 작성에만 쓴다. Point-in-Time 필터, 인용 검증, 한도 검사 같은 규칙 판정은 결정론적 Python이 한다 (`skills/agentic-rag/src/nodes.py`가 구현 예시).

### 부서 Profile 규약

부서마다 폴더 하나, 폴더마다 `config.yaml` + `SOUL.md` — `departments/<n>/hermes/`. 8개 폴더가 같은 형태다 (`config.yaml`: `model` / `env` / `agent.personalities` / `skills` / `usage`, `SOUL.md`: Role/Key Responsibilities/Working Style/Hard Boundaries).

- **저장소 사본과 실제 런타임은 별개 경로다.** `departments/<n>/hermes/`는 git으로 관리되는 저장소 사본, `~/.hermes/profiles/<department>/`는 Hermes Runtime이 실제로 읽는 로컬 상태(+ `auth.json`, `.env`, `memories/`, `sessions/`, `state.db*` 등 머신별 파일 포함, 이들은 git에 올리지 않는다). `git pull` 후에는 `./scripts/sync_hermes_profiles.sh push`로 로컬 Hermes에 반영하고, 로컬에서 Profile을 고쳤으면 `./scripts/sync_hermes_profiles.sh pull`로 저장소에 반영한 뒤 커밋한다.
- 페르소나 프롬프트는 영어 2인칭(`You are the ...`), 파일 상단 주석·설명은 한국어.
- 상단 주석에 담당자와 `HEDGE_FUND_MASTER_PLAN.md` 절 번호를 남긴다.
- **`env:`가 부서마다 다르다.** `ANTHROPIC_API_KEY` — ceo, research, qa, quant-backtest / `OPENAI_API_KEY` — trading, risk, accounting, hr. 아무 키나 넣지 않는다. `skills/agentic-rag`가 OpenAI를 쓰는 것도 risk-management가 OpenAI에 배정돼 있기 때문이다.
- 모든 Hermes 부서장 Profile은 기본 `provider: openai-codex` / `gpt-5.6-luna`이며 Claude Code는 승인된 대체 런타임이다. 모든 직원은 Hermes `model`과 분리된 독립 LangGraph + Ollama `qwen3:1.7b`를 사용한다. 직원 모델 변경은 [WORKER_MODEL_MATRIX.md](docs/02-engineering/WORKER_MODEL_MATRIX.md)를 따른다. 기존 `agent.personalities` 목록은 런타임 직원 수가 아니라 호환·감사 카탈로그다.
- `agent.timeout_seconds`는 부서 단독 명령의 기본 한도다. `multi-agent-workflow.yaml`의 Step Timeout은 Case별 Orchestrator 한도이므로 더 길 수 있으며 Workflow 실행에서는 Step 값이 우선한다.
- 미구현 항목은 코드가 아니라 **주석 백로그**로 남긴다 ([risk-management/config.yaml](departments/03-risk/hermes/config.yaml), [qa-department/config.yaml](departments/06-ai-qa-audit/hermes/config.yaml) 참고). `agentic_rag.status` 필드가 실제 구현 여부를 기록하므로 그 값을 신뢰한다.

### `skills/agentic-rag`

`compliance-policy-agent` 하나만 실제로 연결된 baseline이다 (`retrieve → grade → generate → hallucination_check → retry`, 최대 3회). 상세 아키텍처는 [SKILL.md](skills/agentic-rag/SKILL.md) 참고, 여기서는 놓치기 쉬운 것만:

- PIT 필터·인용 검증은 순수 Python(`src/nodes.py`)이고 LLM은 grade/generate에만 쓴다.
- 코퍼스 3개 문서(`corpus/compliance/*.md`)는 전부 `status: SAMPLE_PLACEHOLDER`다 — 실제 정책으로 교체 전에는 결과를 신뢰하지 않는다.
- frontmatter 파서는 자체 구현이라 **한 줄 `key: value`만 읽는다.** 중첩 YAML이나 리스트를 쓰면 조용히 무시된다.
- `grounded: false`로 끝나면 inconclusive이며 escalate한다. 통과한 것처럼 진행하지 않는다.
- pgvector로 교체할 때는 `retriever.py`의 `search()` 인터페이스(`DocumentChunk` in, `list[ScoredChunk]` out)만 유지하면 `nodes.py`는 안 고쳐도 된다.
- 두 번째 페르소나(`evidence-qa-agent` 등)를 붙일 때는 `nodes.py`의 compliance 전용 시스템 프롬프트를 복붙하지 말고 페르소나별 테이블로 분리한다.
- Query rewriting, reranking, fusion, semantic cache는 **의도적으로 없다** — baseline이 검증된 뒤 붙일 백로그다.

## 문서 규칙

`docs/HEDGE_FUND_MASTER_PLAN.md`가 최상위 기준이다. 문서가 충돌하면 이 순서로 해석한다.

1. `HEDGE_FUND_MASTER_PLAN.md` — 제품 정의, 조직, 통제 원칙, 출시 단계
2. `MINIMUM_SERVICE_UNIT_SPEC.md`의 Domain Contract, `DATA_GOVERNANCE_GUIDE.md`의 데이터 통제
3. `TECH_STACK_DECISIONS.md`의 Runtime·Library·저장소 경계와 `AI_OFFICE_FRONTEND_PLAN.md`의 Frontend 계약
4. `REPOSITORY_DEPARTMENT_STRUCTURE.md`의 현재·목표 경로, 소유권과 이전 규칙
5. `HEDGE_FUND_CORE_PLAN.md`, `HEDGE_FUND_IMPLEMENTATION_BACKLOG.md`의 단기 범위와 완료 조건
6. `AGENT_EMPLOYEE_PROFILES.md`와 팀별 가이드
7. `README.md`

하위 문서는 마스터 플랜을 구체화할 수는 있어도 **변경할 수는 없다.** 마스터 플랜 자체를 바꾸려면 ADR로 근거를 승인한 뒤 영향받는 문서를 같은 변경에서 함께 갱신한다. ADR 승인 전에 후보 기술이나 확장안을 새 Markdown으로 추가하지 않는다.

**아직 미결정이므로 임의로 정하지 않는다:** Paper/Live Broker, 전체 Cloud Provider와 Frontend Production Hosting, 첫 활성 Strategy Portfolio, TimescaleDB Retention, Production Data Vendor, 자동 Paper 승인 방식.

Frontend Framework는 `ai-office` 기반 Next.js·React·TypeScript로 확정됐다. 현재 `vinext`·Cloudflare Worker 구성은 Prototype Hosting Baseline일 뿐 Backend와 전체 Cloud Provider 결정이 아니다. Frontend는 금융 상태의 Projection이며 Supabase Service Role, Broker·LS Credential, Risk 계산, OMS 상태 전이와 Ledger Posting을 소유하지 않는다.

현재 저장소 실행 기준은 Hermes 8개 Profile의 Head가 `provider: openai-codex` / `gpt-5.6-luna`이고, 승인된 Claude Code를 대체 런타임으로 사용하는 것이다. 직원은 직원별 독립 LangGraph Worker + Ollama `qwen3:1.7b`다. Amazon Bedrock은 `TECH_STACK_DECISIONS.md`에 남아 있는 목표 Model Gateway 후보이며 현재 로컬 Profile의 실행 모델로 오인하지 않는다. 과거 Nous/Laguna baseline은 역사 기록으로만 보존하고, 애플리케이션 Hosting Cloud 결정과 모델 선택을 같은 결정으로 취급하지 않는다.

## 담당자

| 담당 | 영역 | Profile | 팀 가이드 |
|---|---|---|---|
| 재일 | 리서치 / 퀀트·백테스트 | `departments/01-research/hermes/`, `departments/04-quant-backtest/hermes/` | [TEAM_JAEIL](docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md) |
| 도현 | 트레이딩 / 회계·포트폴리오 | `departments/02-trading/hermes/`, `departments/05-accounting-portfolio/hermes/` | [TEAM_DOHYUN](docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) |
| 동규 | 리스크 / AI QA·감사 | `departments/03-risk/hermes/`, `departments/06-ai-qa-audit/hermes/` | [TEAM_DONGGYU](docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md) |
| 영주 | CEO / Agent 인사팀 | `departments/00-ceo-office/hermes/`, `departments/07-agent-workforce/hermes/` | [TEAM_YOUNGJU](docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md) |

같은 담당자가 서로 견제해야 하는 두 본부를 함께 맡는 경우가 있다(동규: 리스크 ↔ QA, 도현: 트레이딩 ↔ 회계). 담당자가 같다는 이유로 두 본부의 권한을 합치지 않는다.

## 참고 문헌

`references/references.md`에 설계 근거가 된 논문 8편이 정리돼 있다 (Bull/Bear 토론, Agentic RAG, Hallucination 탐지, Finance Agent 감사 등). 설계 근거를 물어보면 여기부터 확인한다.

## 개발 원칙

1. Agent보다 데이터 계약과 Risk/OMS를 먼저 안정화한다.
2. LLM 출력은 항상 Pydantic Schema로 검증한다.
3. Agent Decision과 Order를 같은 객체로 취급하지 않는다.
4. 모든 주문은 결정론적 Risk Engine을 통과한다.
5. 미래 데이터가 Backtest와 과거 Replay에 들어가지 않게 한다.
6. Position은 Fill 또는 승인된 Adjustment로만 변경한다.
7. Replay 환경은 실제 Broker Credential을 가질 수 없다.
8. 새 Library는 기존 Stack으로 해결할 수 없는 문제와 제거 기준을 함께 기록한다.
9. 위험한 기능은 실패 시 거래 확대가 아니라 Entry 차단 방향으로 동작한다.
10. 구현 완료는 코드 작성이 아니라 Acceptance Scenario 통과를 의미한다.
