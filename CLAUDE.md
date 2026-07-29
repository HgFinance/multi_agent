# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 현재 상태

대부분 설계 단계다. Application Scaffold도 테스트 프레임워크도 아직 없다.

1. `docs/` — 설계 문서. 이 저장소의 Source of Truth.
2. 루트의 8개 `*.yaml` — Hermes Agent Runtime이 읽는 부서 Profile.
3. 실행 가능한 코드 — `fetch_news.py`, `skills/agentic-rag/`, 그리고 트레이딩·회계본부의
   거래 생명주기 구현(`db/`, `trading/contracts.py`, `execution/`, `accounting/`).

트레이딩·회계본부만 Sprint D0~D2가 구현돼 있다(계약·스키마·Paper OMS·원장·대사).
다른 본부는 아직 Profile과 설계 문서뿐이다. 어느 본부의 코드를 찾다가 없으면
"아직 없는 것"이 맞다 — 다른 경로에 있는지 헤매지 않아도 된다.

트레이딩·회계 코드는 팀 가이드 v1.2(상태 머신 2단 분리, Multi-Strategy) 반영 전이라
재작업 예정이다. 각 부서 yaml의 `implementation:` 블록이 무엇이 되고 무엇이 안 됐는지
표시한다.

[docs/README.md](docs/README.md)의 "예상 저장소 구조"(`apps/`, `services/`, `contracts/` …)는 **아직 만들어지지 않은 목표 구조**다. 실재하는 경로처럼 참조하거나 import하지 않는다.

## 명령어

```bash
pip install -r requirements.txt
```

Hermes Runtime(`NousResearch/hermes-agent`)은 PyPI 패키지가 아니라 `requirements.txt`에 없다. 별도 저장소 지침대로 설치한다.

```bash
# 부서 단독 실행 — 각 .yaml의 usage: 블록에 예시 있음
research-department chat -q 'Build a Research Packet for AAPL'
risk-management     chat -q 'Assess risk of AAPL long position'
ceo-agent           chat -q 'Summarize current portfolio decisions and open risks'

# Agentic RAG 단독 실행 (OPENAI_API_KEY 필요)
python3 skills/agentic-rag/main.py --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" --as-of 2026-07-29

# 뉴스 조회 (TAVILY_API_KEY 필요)
python3 fetch_news.py 'AAPL Apple stock'
```

DB 마이그레이션 (로컬 PostgreSQL 또는 Supabase). 002는 001의 `execution.funds`/`books`를
참조하므로 순서를 지킨다.

```bash
psql -d <db> -f db/001_execution.sql -f db/002_accounting.sql \
             -f db/003_roles.sql -f db/004_seed.sql
```

RLS가 fail-closed다. **조회가 이유 없이 0건이면 `SET app.fund_id`를 빠뜨린 것**이다.

**테스트 프레임워크는 아직 없다.** 다만 트레이딩·회계 모듈은 각 파일의 `__main__`에
assert 기반 자체 점검을 두고 있어 그대로 실행하면 검증된다.

```bash
python trading/contracts.py        # 계약 6개 영역
python execution/oms.py            # OMS 불변식 10개
python execution/paper_broker.py   # Paper Broker 4개 영역
python accounting/ledger.py        # 원장 불변식 10개
python accounting/reconciliation.py # 대사 12개
```

목표 스택은 [TECH_STACK_DECISIONS.md](docs/02-engineering/TECH_STACK_DECISIONS.md)가 정한 `pytest + pytest-asyncio + Hypothesis + respx + testcontainers`와 `ruff + pyright + pip-audit + bandit`이다. 실제로 도입하면 위 자체 점검을 pytest로 옮기고 이 절을 갱신한다.

## 아키텍처

### 부서 토폴로지와 3개 주기

[multi-agent-workflow.yaml](multi-agent-workflow.yaml)에는 **서로 분리된 세 흐름**이 있다. 섞지 않는다.

| 주기 | 순서 | 타임아웃 |
|---|---|---|
| `workflow` (실시간 신호) | research → trading → risk → qa → accounting → ceo | 420s |
| `strategy_research_cycle` (전략 연구) | quant-backtest → qa → ceo | 180s |
| `workforce_management_cycle` (인사) | hr → hr → qa → ceo → hr | 300s |

전략 연구 주기는 실시간 파이프라인과 분리돼 있다. 검증된 불변 Strategy Bundle만 트레이딩본부로 넘어가며, 실시간 운용 중 전략 코드를 직접 수정하지 않는다.

`hr-department`는 **제7의 투자 본부가 아니라 CEO 직속 Shared Service**다. 투자 본부는 리서치·트레이딩·리스크·퀀트/백테스트·회계/포트폴리오·AI QA/감사 6개뿐이다.

### Hermes(부서) vs LangGraph(직원) 실행 계층

**부서는 Hermes로 돌아가고, 부서 안의 직원(개별 페르소나)은 LangGraph로 작동한다.** 이 둘을 같은 층으로 섞지 않는다.

- Hermes Profile(8개 `.yaml` = 8개 Supervisor)이 부서 단위 오케스트레이션·Queue·Memory Namespace·Tool Allowlist를 맡는다.
- 부서 소속 직원(예: `market-liquidity-risk-agent`, `evidence-qa-agent`)은 사건별로 동적 실행되는 LangGraph Node가 실제 구현이어야 한다(`HEDGE_FUND_MASTER_PLAN.md` 5.5절).
- **현재 격차**: 지금 8개 Profile의 `agent.personalities`는 전부 prompt-only 텍스트이고, 실제 LangGraph로 구현된 직원은 `compliance-policy-agent`(`skills/agentic-rag`) 하나뿐이다. 나머지 직원을 prompt만으로 이미 완성된 것처럼 다루지 않는다.
- **현재 우선순위**: 나머지 직원의 LangGraph 구현을 서두르기 전에 **Hermes 엔지니어링(Profile 구성, 부서 간 계약, 권한 경계)을 먼저 완성**한다. 순서를 건너뛰지 않는다.

### 절대 깨면 안 되는 권한 분리

**부서 간 권한은 어떤 이유로도 이전되지 않는다.** 담당자가 같다고, 구현이 급하다고, 편의를 위해서라도 한 부서가 다른 본부의 권한(Risk의 거부권, QA의 감사 권한 등)을 대신 수행하거나 위임받지 않는다. Profile을 수정하거나 코드를 쓸 때 협상 대상이 아니다. 대부분의 페르소나 프롬프트가 이를 금지하는 문장을 명시적으로 갖고 있으므로, 프롬프트를 다듬을 때 그 문장을 지우지 않는다.

- Agent Decision ≠ Strategy Signal ≠ OrderIntent ≠ Order. 서로 다른 객체이며 같은 것으로 취급하지 않는다.
- 모든 주문은 결정론적 Risk Engine을 통과한다. `risk-management` 에이전트는 근거와 권고(approve/resize/reject)만 만들고, 바인딩 집행·한도 관리는 Risk Engine이 한다.
- `trader-pm-agent`는 주문을 직접 전송하지 않는다. Risk/Compliance Gate 통과가 선행 조건이다.
- CEO는 주문 제출, 리스크 승인, 원장 수정, NAV 확정, Audit Finding 종결 권한이 **없다**.
- `hr-department`는 자기 후보를 스스로 최종 승인할 수 없다. 권한 독립 검증은 AI QA/감사본부, 예산·조직 승인은 CEO, 실제 Identity/권한 생성은 Platform/IAM Service만 한다.
- `quant-backtest-department`는 Production 승격을 직접 하지 않는다. CEO·Risk·QA 승인이 필요하다.
- LLM은 관련성 판단과 서술 작성에만 쓴다. Point-in-Time 필터, 인용 검증, 한도 검사 같은 규칙 판정은 결정론적 Python이 한다 (`skills/agentic-rag/src/nodes.py`가 구현 예시).

### 부서 Profile `.yaml` 규약

8개 파일이 같은 형태다 — `model` / `env` / `agent.personalities` / `skills` / `usage`.

- 페르소나 프롬프트는 영어 2인칭(`You are the ...`), 파일 상단 주석·설명은 한국어.
- 상단 주석에 담당자와 `HEDGE_FUND_MASTER_PLAN.md` 절 번호를 남긴다.
- **`env:`가 부서마다 다르다.** `ANTHROPIC_API_KEY` — ceo, research, qa, quant-backtest / `OPENAI_API_KEY` — trading, risk, accounting, hr. 아무 키나 넣지 않는다. `skills/agentic-rag`가 OpenAI를 쓰는 것도 risk-management가 OpenAI에 배정돼 있기 때문이다.
- `model`은 8개 파일 모두 `provider: nous` / `poolside/laguna-s-2.1:free`로 동일. 바꾸려면 8개를 함께 바꾼다.
- `agent.timeout_seconds`는 `multi-agent-workflow.yaml`의 해당 step 값과 맞춘다.
- 미구현 항목은 코드가 아니라 **주석 백로그**로 남긴다 ([risk-management.yaml](risk-management.yaml), [qa-department.yaml](qa-department.yaml) 참고). `agentic_rag.status` 필드가 실제 구현 여부를 기록하므로 그 값을 신뢰한다.

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
3. `TECH_STACK_DECISIONS.md`의 Runtime·Library·저장소 경계
4. `HEDGE_FUND_CORE_PLAN.md`, `HEDGE_FUND_IMPLEMENTATION_BACKLOG.md`의 단기 범위와 완료 조건
5. `AGENT_EMPLOYEE_PROFILES.md`와 팀별 가이드
6. `README.md`

하위 문서는 마스터 플랜을 구체화할 수는 있어도 **변경할 수는 없다.** 마스터 플랜 자체를 바꾸려면 ADR로 근거를 승인한 뒤 영향받는 문서를 같은 변경에서 함께 갱신한다. ADR 승인 전에 후보 기술이나 확장안을 새 Markdown으로 추가하지 않는다.

**아직 미결정이므로 임의로 정하지 않는다:** Paper/Live Broker, Frontend Framework, Cloud Provider, 첫 활성 Strategy Portfolio, TimescaleDB Retention, Production Data Vendor, 자동 Paper 승인 방식.

현재 hermes 8개 Profile의 `provider: nous`는 **베이스라인일 뿐 확정이 아니다** — 필요하면 바뀐다. 일부 팀 가이드가 언급하는 Bedrock 등도 후보일 뿐, 지금 nous를 쓴다고 Cloud/모델 Provider가 확정된 것으로 취급하지 않는다.

## 담당자

| 담당 | 영역 | Profile | 팀 가이드 |
|---|---|---|---|
| 재일 | 리서치 / 퀀트·백테스트 | `research-department.yaml`, `quant-backtest-department.yaml` | [TEAM_JAEIL](docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md) |
| 도현 | 트레이딩 / 회계·포트폴리오 | `trading-department.yaml`, `accounting-portfolio-department.yaml` | [TEAM_DOHYUN](docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) |
| 동규 | 리스크 / AI QA·감사 | `risk-management.yaml`, `qa-department.yaml` | [TEAM_DONGGYU](docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md) |
| 영주 | CEO / Agent 인사팀 | `ceo-agent.yaml`, `hr-department.yaml` | [TEAM_YOUNGJU](docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md) |

같은 담당자가 서로 견제해야 하는 두 본부를 함께 맡는 경우가 있다(동규: 리스크 ↔ QA, 도현: 트레이딩 ↔ 회계). 담당자가 같다는 이유로 두 본부의 권한을 합치지 않는다.

## 참고 문헌

`references/references.md`에 설계 근거가 된 논문 8편이 정리돼 있다 (Bull/Bear 토론, Agentic RAG, Hallucination 탐지, Finance Agent 감사 등). 설계 근거를 물어보면 여기부터 확인한다.
