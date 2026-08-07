# CLAUDE.md

Claude Code가 이 저장소에서 작업할 때 지켜야 할 규칙. 설계 중심 초기 구현 단계 — `departments/<n>/` 8개 폴더가 실행 기준.

## 경로

- `docs/` — 설계 Source of Truth. 목표 구조: [REPOSITORY_DEPARTMENT_STRUCTURE.md](docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md)
- `departments/<n>/hermes/` — 부서 Profile(git 관리). 실제 Runtime은 `~/.hermes/profiles/<department>/`(별도, `scripts/sync_hermes_profiles.sh`로 동기화)
- 실행 가능 코드: `departments/01-research/collectors/news.py`, `skills/agentic-rag/`, 트레이딩·회계 거래 생명주기(`departments/02-trading/{contracts,oms,broker}/`, `departments/05-accounting-portfolio/{ledger,reconciliation}/`, `db/`). 나머지 부서는 대부분 Profile·설계 문서 단계.
- Frontend 현재 `ai-office/` → 목표 `apps/operator-web/`. 계약은 [AI Office Frontend Plan](docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md).
- 구 경로(`orchestration/hermes/`, 루트 `trading/`,`execution/`,`accounting/`, `fetch_news.py`)는 삭제됨 — 위 새 경로만 유효.

## 명령어

```bash
pip install -r requirements.txt   # Hermes Runtime은 PyPI 아님, 별도 설치 필요

research-department chat -q '...'
risk-management     chat -q '...'
ceo-agent           chat -q '...'

python3 skills/agentic-rag/main.py --persona compliance-policy-agent --query "..." --as-of 2026-07-29  # OPENAI_API_KEY
python3 departments/01-research/collectors/news.py 'AAPL Apple stock'                                  # TAVILY_API_KEY

supabase db reset                                    # 운영 DB: supabase/migrations/, 시계열: timescaledb/migrations/
python -m unittest discover -s tests/schema -p "test_*.py" -v
```

루트 `db/001~004_*.sql`은 D0-D2 Prototype 전용 — `supabase/migrations/`와 같은 DB에 함께 적용하지 않는다(Table 계약 다름).

pytest 미도입. 각 모듈 `__main__`의 assert 자체 점검으로 대체: `contracts.py`/`oms.py`/`paper_broker.py`(trading), `ledger.py`/`reconciliation.py`(accounting), `apps/api/main.py`.

## 작업 시 주의

- Risk·QA 브랜치는 전용 환경 사용: `source ~/claude/bin/activate` 후 `python --version`, `command -v ruff` 확인. 저장소 `.venv`와 혼용 금지.
- `/graphify`는 서브에이전트 없이 단일 스레드로 실행한다. `graphify-out/graph.json`·`graph.html`은 `Read`로 직접 열지 않는다(용량 큼) — `graphify query "<question>"` CLI나 `graphify-out/GRAPH_REPORT.md`를 쓴다.
- 단순 검색용 Subagent는 부모의 고비용 모델을 상속하지 말고 `flash`/`flash_lite` 등 저렴한 모델을 지정한다.

## 아키텍처

### 5개 흐름 ([multi-agent-workflow.yaml](multi-agent-workflow.yaml)) — 서로 분리, 섞지 않는다

| 흐름 | 순서 |
|---|---|
| `workflow`(실시간 신호) | research → trading → risk → qa → accounting → ceo |
| `strategy_research_cycle`(전략 연구) | quant-backtest → qa → ceo |
| `workforce_management_cycle`(신규 채용) | hr → hr → qa → ceo → hr |
| `agent_evolution_cycle`(기존 Agent 개선) | hr → hr → qa → ceo → hr |
| `event_routing`(동적 라우팅) | 이벤트별 필요 페르소나만, 고정 순서 없음 |

전략 연구는 실시간 파이프라인과 분리 — 검증된 불변 Strategy Bundle만 트레이딩으로 넘어간다. `agent_evolution_cycle`(프롬프트 수정 포함)도 "이미 배포됐다"는 이유로 QA·CEO 승인을 건너뛰지 않는다. 모든 step은 실패 시 안전한 기본값(REJECT/HOLD/DENY/ESCALATE/ROLLBACK)으로 떨어지며 승인 방향으로 자동 fallback하지 않는다. `hr-department`는 투자 본부가 아니라 CEO 직속 Shared Service.

### Hermes(부서) vs LangGraph(직원) — 같은 층으로 섞지 않는다

Hermes Profile 8개가 부서 오케스트레이션·Queue·Memory·Tool Allowlist를 맡고, 부서 소속 직원은 각자 독립 LangGraph Worker + Ollama `qwen3:1.7b`로 동작한다. Worker는 허용된 읽기 결과만 부서장에게 전달할 뿐 주문·Risk/QA 판정·원장·권한 변경은 하지 않는다. 모델 배치 변경은 [WORKER_MODEL_MATRIX.md](docs/02-engineering/WORKER_MODEL_MATRIX.md) 절차를 거친 뒤에만.

**현재 Registry(2026-08-07): 총 29명.** LLM Worker 25 — CEO 1, HR 5, Research 6, Trading 2, Risk 1, Quant/Backtest 7, Accounting/Portfolio 1, AI QA 2. 결정론 runner 4 — `desk-runner`, `risk-runner`, `qa-runner`, `back-office-runner`(모델을 부르지 않으므로 LLM 수에 안 센다). 실제 런타임 수는 각 Profile의 `workers`·`runtime_personalities`를 따르며, `agent.personalities`의 기존 ID는 호환·감사 Alias다.

### 절대 깨면 안 되는 권한 분리

부서 간 권한은 담당자가 같아도, 급해도, 편의를 위해서도 이전되지 않는다.

- Agent Decision ≠ Strategy Signal ≠ OrderIntent ≠ Order.
- 모든 주문은 결정론적 Risk Engine을 통과한다. `risk-management`는 근거·권고만 만들고, 바인딩 집행·한도 관리는 Risk Engine이 한다.
- `trader-pm-agent`는 Risk/Compliance Gate 통과 전 주문을 직접 전송하지 않는다.
- CEO는 주문 제출·리스크 승인·원장 수정·NAV 확정·Audit Finding 종결 권한이 없다.
- `hr-department`는 자기 후보를 스스로 최종 승인할 수 없다(권한 독립 검증=AI QA/감사본부, 예산·조직 승인=CEO, 권한 생성=Platform/IAM).
- `quant-backtest-department`는 Production 승격을 직접 하지 않는다(CEO·Risk·QA 승인 필요).
- LLM은 관련성 판단·서술 작성에만 쓴다. PIT 필터·인용 검증·한도 검사는 결정론적 Python(`skills/agentic-rag/src/nodes.py`).

### 부서 Profile & agentic-rag

`departments/<n>/hermes/`에 `config.yaml`+`SOUL.md`. 저장소 사본(git)과 실제 Runtime(`~/.hermes/profiles/`, git 미포함)은 별개 경로 — `sync_hermes_profiles.sh push/pull`로 동기화. `env:`는 부서마다 다르다: `ANTHROPIC_API_KEY`(ceo/research/qa/quant-backtest), `OPENAI_API_KEY`(trading/risk/accounting/hr). 부서장 기본 `provider: openai-codex`(Claude Code는 승인된 대체 런타임).

`skills/agentic-rag`는 `compliance-policy-agent`만 실제 연결된 baseline이다. PIT 필터·인용 검증은 순수 Python, LLM은 grade/generate에만 쓴다. 코퍼스는 전부 `SAMPLE_PLACEHOLDER` — 실제 정책 교체 전 결과를 신뢰하지 않는다. `grounded: false`는 escalate. 상세는 [SKILL.md](skills/agentic-rag/SKILL.md).

## 문서 규칙

`docs/HEDGE_FUND_MASTER_PLAN.md`가 최상위 기준. 충돌 시 우선순위: 1) MASTER_PLAN 2) `MINIMUM_SERVICE_UNIT_SPEC.md`/`DATA_GOVERNANCE_GUIDE.md` 3) `TECH_STACK_DECISIONS.md`/`AI_OFFICE_FRONTEND_PLAN.md` 4) `REPOSITORY_DEPARTMENT_STRUCTURE.md` 5) `HEDGE_FUND_CORE_PLAN.md`/`..._BACKLOG.md` 6) `AGENT_EMPLOYEE_PROFILES.md`/팀 가이드 7) `README.md`. 하위 문서는 마스터 플랜을 구체화할 수는 있어도 변경할 수 없다(변경하려면 ADR 승인 후 관련 문서를 함께 갱신).

**아직 미결정 — 임의로 정하지 않는다:** Paper/Live Broker, 전체 Cloud Provider·Frontend Hosting, 첫 활성 Strategy Portfolio, TimescaleDB Retention, Production Data Vendor, 자동 Paper 승인 방식.

## 담당자

| 담당 | 영역 | 팀 가이드 |
|---|---|---|
| 재일 | 리서치/퀀트·백테스트 | [TEAM_JAEIL](docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md) |
| 도현 | 트레이딩/회계·포트폴리오 | [TEAM_DOHYUN](docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) |
| 동규 | 리스크/AI QA·감사 | [TEAM_DONGGYU](docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md) |
| 영주 | CEO/Agent 인사팀 | [TEAM_YOUNGJU](docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md) |

동규(리스크↔QA)·도현(트레이딩↔회계)처럼 서로 견제해야 할 두 본부를 같은 담당자가 맡아도 권한을 합치지 않는다. 참고 논문 8편은 `references/references.md`.

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
