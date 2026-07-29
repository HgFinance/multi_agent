# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 현재 상태

구현 전 설계 단계다. Application Scaffold도 테스트 스위트도 없다. 이 저장소에 실제로 존재하는 것은 셋뿐이다.

1. `docs/` — 13개 설계 문서. 이 저장소의 Source of Truth.
2. 루트의 8개 `*.yaml` — Hermes Agent Runtime이 읽는 부서 Profile.
3. 실행 가능한 코드 — `fetch_news.py`와 `skills/agentic-rag/`. 이 둘이 전부다.

[docs/README.md](docs/README.md)의 "예상 저장소 구조"(`apps/`, `services/`, `contracts/` …)는 **아직 만들어지지 않은 목표 구조**다. 그 경로가 실재하는 것처럼 참조하거나 import하지 않는다.

## 명령어

```bash
pip install -r requirements.txt
```

Hermes Runtime(`NousResearch/hermes-agent`) 자체는 PyPI 패키지가 아니라서 `requirements.txt`에 없다. 별도 저장소 지침대로 설치한다.

부서 에이전트 단독 실행 — 각 `.yaml`의 `usage:` 블록에 부서별 예시가 있다.

```bash
research-department chat -q 'Build a Research Packet for AAPL'
risk-management     chat -q 'Assess risk of AAPL long position'
ceo-agent           chat -q 'Summarize current portfolio decisions and open risks'
```

부서 간 위임은 프롬프트 안에서 `then delegate to <department>` 형태로 표현한다. 전체 목록은 [multi-agent-workflow.yaml](multi-agent-workflow.yaml)의 `usage:` 참고.

Agentic RAG 단독 실행 (`OPENAI_API_KEY` 필요):

```bash
python3 skills/agentic-rag/main.py \
  --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" \
  --as-of 2026-07-29
```

뉴스 조회 (`TAVILY_API_KEY` 필요):

```bash
python3 fetch_news.py 'AAPL Apple stock'
```

**테스트와 린트는 아직 없다.** 목표 스택은 [TECH_STACK_DECISIONS.md](docs/02-engineering/TECH_STACK_DECISIONS.md)가 정한 `pytest + pytest-asyncio + Hypothesis + respx + testcontainers`와 `ruff + pyright + pip-audit + bandit`이다. 실제로 도입하면 이 절을 함께 갱신한다.

## 아키텍처

### 부서 토폴로지와 3개 주기

[multi-agent-workflow.yaml](multi-agent-workflow.yaml)에는 **서로 분리된 세 개의 흐름**이 있다. 섞으면 안 된다.

| 주기 | 순서 | 타임아웃 |
|---|---|---|
| `workflow` (실시간 신호) | research → trading → risk → qa → accounting → ceo | 420s |
| `strategy_research_cycle` (전략 연구) | quant-backtest → qa → ceo | 180s |
| `workforce_management_cycle` (인사) | hr → hr → qa → ceo → hr | 300s |

전략 연구 주기는 실시간 파이프라인과 분리돼 있다. 검증된 불변 Strategy Bundle만 트레이딩본부로 넘어가며, 실시간 운용 중 전략 코드를 직접 수정하지 않는다.

`hr-department`는 **제7의 투자 본부가 아니라 CEO 직속 Shared Service**다. 투자 본부는 리서치·트레이딩·리스크·퀀트/백테스트·회계/포트폴리오·AI QA/감사 6개다.

### 절대 깨면 안 되는 권한 분리

Profile을 수정하거나 코드를 쓸 때 아래는 협상 대상이 아니다. 대부분의 페르소나 프롬프트가 이걸 금지하는 문장을 명시적으로 갖고 있으므로, 프롬프트를 다듬을 때 그 문장을 지우지 않는다.

- Agent Decision ≠ Strategy Signal ≠ OrderIntent ≠ Order. 서로 다른 객체이며 같은 것으로 취급하지 않는다.
- 모든 주문은 결정론적 Risk Engine을 통과한다. `risk-management`의 에이전트는 근거와 권고(approve/resize/reject)만 만들고 바인딩 집행과 한도 관리는 Risk Engine이 한다 — 에이전트가 원장을 직접 바꾸지 않는다.
- `trader-pm-agent`는 주문을 직접 전송하지 않는다. Risk/Compliance Gate 통과가 선행 조건이다.
- CEO는 주문 제출, 리스크 승인, 원장 수정, NAV 확정, Audit Finding 종결 권한이 **없다**.
- `hr-department`는 자기 후보를 스스로 최종 승인할 수 없다. 권한 독립 검증은 AI QA/감사본부, 예산·조직 승인은 CEO, 실제 Identity/권한 생성은 Platform/IAM Service만 한다.
- `quant-backtest-department`는 Production 승격을 직접 하지 않는다. CEO·Risk·QA 승인이 필요하다.
- LLM은 관련성 판단과 서술 작성에만 쓴다. Point-in-Time 필터, 인용 검증, 한도 검사 같은 규칙 판정은 결정론적 Python이 한다. `skills/agentic-rag/src/nodes.py`가 이 원칙의 구현 예시다.

### 부서 Profile `.yaml` 규약

8개 파일이 같은 형태다 — `model` / `env` / `agent.personalities` / `skills` / `usage`. 편집할 때:

- 페르소나 프롬프트는 영어 2인칭(`You are the ...`)으로, 파일 상단 주석과 설명은 한국어로 쓴다. 8개 파일 전부가 이 규칙을 따른다.
- 상단 주석에 담당자와 `HEDGE_FUND_MASTER_PLAN.md` 절 번호를 남긴다.
- **`env:`가 부서마다 다르다.** `ANTHROPIC_API_KEY` — ceo, research, qa, quant-backtest / `OPENAI_API_KEY` — trading, risk, accounting, hr. 아무 키나 넣으면 안 된다. `skills/agentic-rag`가 OpenAI를 쓰는 것도 risk-management가 OpenAI에 배정돼 있기 때문이다.
- `model`은 8개 파일 모두 `provider: nous` / `poolside/laguna-s-2.1:free`로 동일하다. 바꾸려면 8개를 함께 바꾼다.
- `agent.timeout_seconds`는 `multi-agent-workflow.yaml`의 해당 step 값과 맞춘다.
- 미구현 항목은 코드가 아니라 **주석 백로그**로 남긴다 — [risk-management.yaml](risk-management.yaml) 34-38행, [qa-department.yaml](qa-department.yaml) 35-37행이 그 예다. `agentic_rag.status` 필드가 실제 구현 여부를 기록하므로 여기 적힌 상태를 신뢰한다.

### `skills/agentic-rag`

`compliance-policy-agent` 하나만 실제로 연결된 baseline이다. 그래프는 `retrieve → grade → generate → hallucination_check → (retry, 최대 3회)`.

- `src/graph.py` — LangGraph 배선.
- `src/nodes.py` — PIT 필터와 인용 검증은 순수 Python, LLM은 grade/generate에만 쓴다.
- `src/retriever.py` — OpenAI 임베딩 + 로컬 코사인 유사도, `.embedding_cache.json`에 캐시(내용 해시가 바뀌면 자동 재계산). pgvector로 교체할 때 `search()`의 인터페이스(`DocumentChunk` in, `list[ScoredChunk]` out)만 유지하면 `nodes.py`는 안 고쳐도 된다.
- `corpus/compliance/*.md`의 frontmatter(`document_id`, `version`, `effective_from`/`effective_to`)가 PIT 필터를 구동한다. frontmatter 파서는 자체 구현이라 **한 줄 `key: value`만 읽는다** — 중첩 YAML이나 리스트를 쓰면 조용히 무시된다.
- 코퍼스 3개 문서는 전부 `status: SAMPLE_PLACEHOLDER`다. 실제 정책으로 교체하기 전에는 결과를 신뢰하지 않는다.
- `grounded: false`로 끝나면 결과는 inconclusive이며 escalate 한다. 통과한 것처럼 진행하지 않는다.
- 페르소나 추가는 corpus 디렉터리 추가 + `main.py`의 `PERSONA_CORPUS` 등록. 두 번째 페르소나를 붙일 때는 `nodes.py`의 compliance 전용 시스템 프롬프트를 복붙하지 말고 페르소나별 프롬프트 테이블로 분리한다.

Query rewriting, reranking, fusion, semantic cache는 **의도적으로 없다.** 없다고 버그가 아니다 — baseline이 실사용으로 검증된 뒤 붙일 백로그다.

## 문서 규칙

`docs/HEDGE_FUND_MASTER_PLAN.md`가 최상위 기준이다. 문서가 충돌하면 이 순서로 해석한다.

1. `HEDGE_FUND_MASTER_PLAN.md` — 제품 정의, 조직, 통제 원칙, 출시 단계
2. `MINIMUM_SERVICE_UNIT_SPEC.md`의 Domain Contract, `DATA_GOVERNANCE_GUIDE.md`의 데이터 통제
3. `TECH_STACK_DECISIONS.md`의 Runtime·Library·저장소 경계
4. `HEDGE_FUND_CORE_PLAN.md`, `HEDGE_FUND_IMPLEMENTATION_BACKLOG.md`의 단기 범위와 완료 조건
5. `AGENT_EMPLOYEE_PROFILES.md`와 팀별 가이드
6. `README.md`

하위 문서는 마스터 플랜을 더 구체화할 수는 있어도 **변경할 수는 없다.** 마스터 플랜 자체를 바꿔야 하면 ADR로 근거를 승인한 뒤 영향받는 문서를 같은 변경에서 함께 갱신한다. 후보 기술이나 확장안을 ADR 승인 전에 새 Markdown으로 추가하지 않는다 — 현재 13개가 확정 문서 전체다.

**아직 미결정이므로 임의로 정하지 않는다:** Paper/Live Broker, Frontend Framework, Cloud Provider, 첫 활성 Strategy Portfolio, TimescaleDB Retention, Production Data Vendor, 자동 Paper 승인 방식.

## 담당자

| 담당 | 영역 | Profile | 팀 가이드 |
|---|---|---|---|
| 재일 | 리서치 / 퀀트·백테스트 | `research-department.yaml`, `quant-backtest-department.yaml` | [TEAM_JAEIL](docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md) |
| 도현 | 트레이딩 / 회계·포트폴리오 | `trading-department.yaml`, `accounting-portfolio-department.yaml` | [TEAM_DOHYUN](docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) |
| 동규 | 리스크 / AI QA·감사 | `risk-management.yaml`, `qa-department.yaml` | [TEAM_DONGGYU](docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md) |
| 영주 | CEO / Agent 인사팀 | `ceo-agent.yaml`, `hr-department.yaml` | [TEAM_YOUNGJU](docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md) |

같은 담당자가 서로를 견제해야 하는 두 본부를 함께 맡는 경우가 있다(동규: 리스크 ↔ QA, 도현: 트레이딩 ↔ 회계). 담당자가 같다는 이유로 두 본부의 권한을 합치지 않는다.

## 알려진 불일치

고치기 전에 담당자와 확인할 것들이다. 발견 즉시 무조건 수정하지 말고 의도된 것인지 먼저 물어본다.

- [skills/agentic-rag/SKILL.md](skills/agentic-rag/SKILL.md) 34행의 실행 예시가 `/Users/baiohelseu/Desktop/Project/multi_agent/...` 절대 경로로 하드코딩돼 있다. 다른 머신에서는 그대로 동작하지 않는다.
- `market_data.json`은 AAPL/TSLA/SPY의 2024-01-15 스냅샷 샘플이다. 문서가 정한 대상 시장은 KRX와 LS증권 Feed다.
- `fetch_news.py`는 `https://api.tavily.ai/search`를 호출한다. Tavily 공식 도메인은 `api.tavily.com`이므로 뉴스 조회가 실패하면 여기부터 확인한다.
- [ceo-agent.yaml](ceo-agent.yaml) 18행 주석이 타임아웃을 "step 3와 동일"이라고 하지만 CEO는 워크플로우의 step 6다.

## 참고 문헌

`references/`에 8편의 논문 PDF와 [references.md](references/references.md)가 있다. 설계 근거를 물어보면 여기부터 본다 — Bull/Bear 토론 구조는 Multiagent Debate(2305.14325), Agentic RAG 루프는 Agentic RAG Survey(2501.09136), `hallucination-critic` 기준은 LLM Hallucination Survey(2510.06265), 감사 접근은 Auditing LLM Agents in Finance(2502.15865)에서 왔다.
