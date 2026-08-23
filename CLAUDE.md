# CLAUDE.md

Claude Code가 이 저장소에서 작업할 때 지켜야 할 규칙. 설계 중심 초기 구현 단계 — `departments/<n>/` 8개 폴더가 실행 기준이다.

## 경로

- `docs/` — 설계 Source of Truth ([REPOSITORY_DEPARTMENT_STRUCTURE.md](docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md)). `departments/<n>/hermes/`가 부서 Profile(git), 실제 Runtime은 `~/.hermes/profiles/<department>/`(`scripts/sync_hermes_profiles.sh`로 동기화).
- 실행 가능 코드: `departments/01-research/collectors/`의 시장데이터 전용 수집기, `departments/01-research/api/external_*.py`의 비영속 요청형 MCP, `skills/agentic-rag/`, 트레이딩·회계 거래 생명주기(`departments/02-trading/{contracts,oms,broker}/`, `departments/05-accounting-portfolio/{ledger,reconciliation}/`, `db/`). 비시장 상주 수집기는 운영 경로가 아니다.
- Frontend 현재 `ai-office/` → 목표 `apps/operator-web/` ([AI Office Frontend Plan](docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md)). 구 경로(`orchestration/hermes/`, 루트 `trading/`,`execution/`,`accounting/`, `fetch_news.py`)는 삭제됨.

## 명령어

```bash
pip install -r requirements.txt   # Hermes Runtime은 PyPI 아님, 별도 설치 필요
research-department chat -q '...'
risk-management     chat -q '...'
ceo-agent           chat -q '...'
python3 skills/agentic-rag/main.py --persona compliance-policy-agent --query "..." --as-of 2026-07-29  # OPENAI_API_KEY
python3 departments/01-research/collectors/collector_scheduler.py --check                              # 네트워크 없음
python3 departments/01-research/api/external_sources.py                                                # 네트워크 없음
supabase db reset                                    # 운영 DB: supabase/migrations/, 시계열: timescaledb/migrations/
python scripts/check_test_user_wiring.py             # fixture actor map ↔ 실 DB 대조(읽기 전용)
python -m unittest discover -s tests/schema -p "test_*.py" -v
```

루트 `db/001~004_*.sql`은 D0-D2 Prototype 전용 — `supabase/migrations/`와 같은 DB에 함께 적용하지 않는다. pytest 미도입, 각 모듈 `__main__`의 assert 자체 점검(`contracts.py`/`oms.py`/`paper_broker.py`/`ledger.py`/`reconciliation.py`/`apps/api/main.py`)으로 대체한다.

## 작업 시 주의

- Risk·QA 브랜치는 전용 환경 사용: `source ~/claude/bin/activate` 후 `python --version`, `command -v ruff` 확인, 저장소 `.venv`와 혼용 금지.
- `/graphify`는 서브에이전트 없이 단일 스레드로 실행하고, `graphify-out/graph.json`·`graph.html`은 `Read`로 직접 열지 않는다(용량 큼) — `graphify query "<question>"` CLI나 `GRAPH_REPORT.md`를 쓴다. 단순 검색용 Subagent는 고비용 모델을 상속하지 말고 `flash`/`flash_lite`를 지정한다.

## 아키텍처

**5개 흐름**([multi-agent-workflow.yaml](multi-agent-workflow.yaml)) — 서로 분리, 섞지 않는다:

| 흐름 | 순서 |
|---|---|
| `workflow`(실시간 신호) | research → trading → risk → qa → accounting → ceo |
| `strategy_research_cycle`(전략 연구) | quant-backtest → qa → ceo |
| `workforce_management_cycle`(신규 채용)/`agent_evolution_cycle`(기존 Agent 개선) | hr → hr → qa → ceo → hr |
| `event_routing`(동적 라우팅) | 이벤트별 필요 페르소나만, 고정 순서 없음 |

검증된 Strategy Bundle만 트레이딩으로 넘어가고, 이미 배포된 Agent 개선(`agent_evolution_cycle`)도 QA·CEO 승인을 건너뛰지 않는다. 모든 step은 실패 시 안전한 기본값(REJECT/HOLD/DENY/ESCALATE/ROLLBACK)으로 떨어진다. `hr-department`는 투자 본부가 아니라 CEO 직속 Shared Service다.

**Hermes(부서) vs LangGraph(직원)**: Hermes Profile 8개가 부서 오케스트레이션을 맡고, 소속 직원(**LLM Worker 10명**)은 각자 독립 LangGraph Worker + Ollama `qwen3:1.7b`로 동작한다. Worker는 읽기 결과만 부서장에게 전달할 뿐 주문·판정·원장·권한 변경은 하지 않는다. **결정론 러너 5개**(`desk-runner`/`risk-runner`/`qa-runner`/`back-office-runner`/`ceo-runner`)는 모델을 부르지 않으므로 따로 센다.

부서별 LLM Worker 편제(총 10명) — 정본은 각 부서 `hermes/config.yaml`의 `workers`이고, `tests/test_worker_architecture.py::test_final_worker_shape_has_no_duplicate_roles`이 대조한다:

| 부서 | 총 | 상시 | 조건부 |
|---|---:|---:|---:|
| ceo | 1 | 1 | 0 |
| hr | 1 | 0 | 1 |
| research | 2 | 0 | 2 |
| trading | 0 | 0 | 0 |
| risk | 1 | 0 | 1 |
| quant-backtest | 2 | 0 | 2 |
| accounting-portfolio | 1 | 1 | 0 |
| qa | 2 | 0 | 2 |

⚠ 2026-08-12 정정: 이 문단은 오래 **19명 / 결정론 4개 / "HR은 Worker 0"** 이라고 적고 있었다. 셋 다 코드와 달랐다 — 실제로는 러너 흡수(risk 2026-08-06, qa 2026-08-06, 회계 2026-08-07)로 10명이 됐고, `ceo-runner`가 추가됐으며, HR에는 `profile-architecture-worker`(조건부) 1명이 있다. 편제를 바꿀 때 이 표와 위 테스트를 같이 고친다. 역할 경계의 근거는 [WORKER_ROLE_BOUNDARIES.md](docs/02-engineering/WORKER_ROLE_BOUNDARIES.md).

**절대 깨면 안 되는 권한 분리** — 담당자가 같아도, 급해도 이전되지 않는다:
- Agent Decision ≠ Strategy Signal ≠ OrderIntent ≠ Order. Agent·alpha·전략 Worker·rebalancer의 자동 주문 후보는 결정론적 Risk Engine을 통과하며, `risk-management`는 근거·권고만 만든다.
- `trader-pm-agent`는 Risk/Compliance Gate 통과 전 주문을 보내지 않는다. CEO는 주문 제출·리스크 승인·원장 수정·NAV 확정·Audit 종결 권한이 없다.
- [ADR-0007](docs/02-engineering/adr/0007-authenticated-user-paper-directive-authority.md)의 `USER_DIRECTIVE`는 별도 권한이다. 인증된 사용자가 자기 ACTIVE Fund/Book에 명시한 PAPER 주문은 Risk·alpha·rebalancer가 경제적으로 veto하지 않지만, auth·membership·결정론 parser·cash/position/reservation·lot/tick/TTL·멱등·durable PAPER admission은 필수다. Hermes/LLM은 이 권한을 소유하거나 주문을 보충하지 않는다.
- `hr-department`는 자기 후보를 스스로 최종 승인할 수 없다(검증=QA, 승인=CEO, 권한 생성=IAM). `quant-backtest-department`는 Production 승격을 직접 하지 않는다.
- LLM은 관련성 판단·서술에만 쓴다. PIT 필터·인용 검증·한도 검사는 결정론적 Python(`skills/agentic-rag/src/nodes.py`).

**부서 Profile**: `departments/<n>/hermes/`에 `config.yaml`+`SOUL.md`. `env:`는 부서마다 다르다 — `ANTHROPIC_API_KEY`(ceo/research/qa/quant-backtest), `OPENAI_API_KEY`(trading/risk/accounting/hr). 부서장 기본 `provider: openai-codex`(Claude Code는 승인된 대체 런타임).

**`skills/agentic-rag`**: `compliance-policy-agent`만 실제 연결된 baseline. PIT 필터·인용 검증은 순수 Python, LLM은 grade/generate에만. 코퍼스는 전부 `SAMPLE_PLACEHOLDER`, `grounded: false`는 escalate. 상세는 [SKILL.md](skills/agentic-rag/SKILL.md).

## 문서 규칙

`docs/HEDGE_FUND_MASTER_PLAN.md`가 최상위 기준. 충돌 시 우선순위: MASTER_PLAN → `MINIMUM_SERVICE_UNIT_SPEC.md`/`DATA_GOVERNANCE_GUIDE.md` → `TECH_STACK_DECISIONS.md`/`AI_OFFICE_FRONTEND_PLAN.md` → `REPOSITORY_DEPARTMENT_STRUCTURE.md` → `HEDGE_FUND_CORE_PLAN.md`/`..._BACKLOG.md` → `AGENT_EMPLOYEE_PROFILES.md`/팀 가이드 → `README.md`. 하위 문서는 마스터 플랜을 변경할 수 없다(변경하려면 ADR 승인 후 관련 문서를 함께 갱신).

**아직 미결정, 임의로 정하지 않는다:** Live Broker·Live 주문 API, 전체 Cloud Provider·Frontend Hosting, 첫 Strategy Portfolio, TimescaleDB Retention, Production Data Vendor, 자동 전략 Paper 승인. 사용자 직접 PAPER 레인의 canonical 경제 계정은 LS증권 모의투자 계좌이고, Trading의 PAPER 전용 adapter와 durable admission/audit ledger를 사용한다. LS LIVE 연결은 시장데이터 read-only다(ADR-0007).

## 담당자

| 담당 | 영역 | 팀 가이드 |
|---|---|---|
| 재일 | 리서치/퀀트·백테스트 | [TEAM_JAEIL](docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md) |
| 도현 | 트레이딩/회계·포트폴리오 | [TEAM_DOHYUN](docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) |
| 동규 | 리스크/AI QA·감사 | [TEAM_DONGGYU](docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md) |
| 영주 | CEO/Agent 인사팀 | [TEAM_YOUNGJU](docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md) |

동규(리스크↔QA)·도현(트레이딩↔회계)처럼 견제해야 할 두 본부를 같은 담당자가 맡아도 권한을 합치지 않는다. 참고 논문 8편은 `references/references.md`.

## 개발 원칙

1. Agent보다 데이터 계약과 Risk/OMS를 먼저 안정화한다.
2. LLM 출력은 항상 Pydantic Schema로 검증한다.
3. Agent Decision과 Order를 같은 객체로 취급하지 않는다.
4. 모든 Agent·자동 전략 주문 후보는 결정론적 Risk Engine을 통과한다. 인증 사용자의 명시적 PAPER `USER_DIRECTIVE`만 ADR-0007의 별도 authority/admission 계약을 따른다.
5. 미래 데이터가 Backtest와 과거 Replay에 들어가지 않게 한다.
6. Position은 Fill 또는 승인된 Adjustment로만 변경한다.
7. Replay 환경은 실제 Broker Credential을 가질 수 없다.
8. 새 Library는 기존 Stack으로 해결할 수 없는 문제와 제거 기준을 함께 기록한다.
9. 위험한 기능은 실패 시 거래 확대가 아니라 Entry 차단 방향으로 동작한다.
10. 구현 완료는 코드 작성이 아니라 Acceptance Scenario 통과를 의미한다.
