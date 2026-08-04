# 리스크본부 (Risk Management)

부서장 `risk-supervisor`는 Hermes(Codex/Claude Code)이고 직원은 [Risk Worker Graph](risk_employee_workers.py)의 Ollama `qwen3:1.7b` LangGraph Worker다. 결정론적 Risk Engine이 바인딩 판정을 소유한다.

## 현재 승인 상태 (2026-08-04)

- P1 외부 Portfolio/Market Snapshot, PIT Instrument Mapping, Redis Stream Projection Worker, P1 Gate와 Repository 경계가 구현·테스트됐다.
- P2 파생상품 Snapshot, Margin, Volatility Surface, Greeks·Stress Gate와 선택적 DB 적재가 구현·테스트됐다.
- 실제 운영 활성화는 API 자격증명, governed Fund/Book/Instrument/Stress Scenario ID, Redis, DATABASE_URL, migration 적용이 모두 필요하다. 입력이 없거나 검증되지 않으면 항상 HOLD/REJECT로 종료한다.

## P1 현재 상태 (2026-08-03)

- `p1/analytics.py`가 canonical instrument UUID 매핑, PIT/staleness 검사, Exposure Snapshot, Stress/VaR/Correlation 지표와 `ENABLED` 외 진입 차단을 하나의 결정론적 경계로 묶는다.
- `p1/repository.py`가 `risk.snapshots`, `risk.exposure_components`, `risk.stress_results`, `risk.kill_switch_events`를 한 트랜잭션으로 적재한다. Fund/Book/Instrument/승인된 Stress Scenario FK가 없으면 생성·우회하지 않고 rollback한다.
- LS증권 어댑터는 읽기 전용이다. 실제 키·계좌·운영 DB가 주입되기 전에는 실제 데이터를 수집하거나 운영 Snapshot을 만들지 않는다. `RISK_REQUIRE_P1_ANALYTICS=true`인 pre-trade API는 P1 Snapshot이 없거나 PASS가 아니면 차단한다.
- 남은 운영 조건은 실제 API 자격증명, governed FK 원장, RLS/OMS E2E 및 운영 장애 검증이다. P1 계산 코드가 구현됐다는 뜻이지 실거래 승인을 뜻하지 않는다.
- 2026-08-03 감사에서 Self-check 7개와 명시 pytest는 통과했지만 Compose Service와 실제
  `risk.risk_decisions`, `risk.trading_states`, `risk.run_log_events` Row는 0건이었다.
- 결정론적 Markdown 보고서와 Notion Block Projection을 추가했고 현재 Risk 보고서 11개가 있다.
  Notion 실패는 Risk 판정을 바꾸지 않으며 운영 전 Report Hash·Artifact Storage·Page 멱등 계약이 필요하다.

현재 실행 상태와 동규님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#43-동규님-리스크본부와-ai-qa감사본부)을 따른다.

## Skill Harness

`harness/manifest.py`가 Risk 스킬과 허용 Tool을 고정하고, `harness/core.py`가 trace·비밀값·금지 Tool을 호출 전에 차단한다. 실패 fallback은 `REJECT + HALTED`다. Redis 연결 상태는 실제 비밀값을 출력하지 않는 `harness/redis_check.py`로 확인한다.

`harness/journal.py`는 Hermes 부서 실행과 LangGraph 직원 실행을 `run_id`로 묶어 `InputSnapshot → AgentOutput → Validation → Decision`을 기록한다. Order와 Fill은 별도 이벤트이며 `inputs_hash`, 모델·프롬프트·파라미터 버전, retry/fallback, rationale/evidence를 보존한다. `RunJournal.replay()`와 `RunJournal.review()`는 단계 재실행과 폴백 사유 집계를 제공하고, 운영 적재 스키마는 `risk.run_log_events`다.

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.

## Worker Registry 수와 실제 실행 수

- Registry에 등록된 실제 Worker는 4개다.
- 기본 입력에서 항상 실행되는 Worker는 2개(`market-liquidity-worker`, `pre-trade-risk-worker`)다.
- 조건부 Worker는 2개(`compliance-policy-worker`, `derivatives-counterparty-worker`)이며, 사건 신호가 있을 때 호출된다.
- 한 케이스의 최대 실행 수는 4개다. `agent.personalities`의 기존 6개 역할명은 감사·FK 호환 Alias이며 실행 직원 수에 포함하지 않는다.
직원 Worker의 실제 모델은 `OLLAMA_CHAT_MODEL`로 주입되는 `qwen3:1.7b`이며, `agent-risk`는 수동 호환 Alias일 뿐 `scripts.py`의 실행 경로가 아니다. Hermes Profile은 `risk-management`다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

## Mission

Pre/Post-Trade Risk, Compliance와 Kill State를 담당한다. 포지션 리스크 평가, 포트폴리오 노출도·변동성
영향 분석을 수행하고 approve/resize/reject 중 하나를 반환한다.

`risk-management` 에이전트는 근거와 권고(approve/resize/reject)만 만든다. 바인딩 집행·한도 관리는
`engine/risk_engine.py`의 결정론적 Risk Engine이 한다(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고).

## Owner

동규님 — [TEAM_DONGGYU_RISK_QA_GUIDE](../../docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md)

## 입력·출력 계약

- 입력: 트레이딩본부 OrderIntent(`departments/02-trading/contracts/contracts.py`), Mandate/Limit/Restricted
  List, Position/Cash Snapshot, Market Tradable 상태, Trading State, Counterparty Health
  (전부 `RiskContext` — market-api/portfolio-api/Governance Service 실연동 전까지는 스텁 값으로 채운다)
- 출력: `RiskDecision`(APPROVE/RESIZE/REJECT, 트레이딩본부 계약과 동일한 타입) →
  `workflow` step 3에서 OMS(`departments/02-trading/oms/oms.py`)로 직접 전달, QA본부(step 4)로도 근거 전달.
  감사용 상세(`RiskAssessment`: check_results/reason_codes/calculation_version/input_hash)는
  `risk.risk_decisions`(`supabase/migrations/20260729000400_execution_risk_accounting.sql`) 컬럼과 이름을 맞췄다.
- 이 결정론적 서비스를 FastAPI로 감싸는 API 설계와 부서 내·부서 간 통신 계약은
  [RISK_QA_DOMAIN_API_SPEC.md](../../docs/02-engineering/RISK_QA_DOMAIN_API_SPEC.md) 참고.

## 실행법

```bash
risk-management chat -q 'Assess risk of AAPL long position'
python departments/03-risk/engine/risk_engine.py
python departments/03-risk/engine/trading_state_store.py   # REDIS_URL 필요 (.env)
python3 skills/agentic-rag/main.py --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" --as-of 2026-07-29
```

## 테스트

- `engine/risk_engine.py` — P0 Pre-trade Risk Gate 22개 시나리오 자체 점검(팀 가이드 4.1 10단계 검사
  전부 + Hard/Soft 우선순위 + Trading State 예외 + 재현성 + 트레이딩본부 OMS End-to-End 통합).
- `engine/trading_state_store.py` — Redis Trading State 8개 시나리오 자체 점검(실제 Redis Cloud
  연결, 미설정/설정/덮어쓰기/해제, Redis 장애 시 예외와 fail-closed(HALTED) 구분,
  `risk_engine.RiskEngine`과의 End-to-End 통합 — Redis의 ENTRY_BLOCKED가 실제 주문을 막음).
- `compliance-policy-agent`의 Agentic RAG baseline은 `skills/agentic-rag/`(공용 skills 경계 유지, 이 본부가
  Domain Owner) 참고. 나머지 4개 페르소나는 정형 데이터 계산이라 RAG 대상이 아니다 — 기법 배정 결정과
  LightRAG 백엔드 교체 후보 기록은 `hermes/config.yaml`의 `rag_technique_assignment:` 참고.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `engine/risk_engine.py` — Sprint K1 P0 Pre-trade Risk Gate. 팀 가이드 4.1(10단계 검사), 5.2(Risk
  Request/Decision), 5.3(Trading State) 구현. LLM 호출 없음(팀 가이드 2절). Hard Limit 위반은 REJECT,
  Soft Limit 위반은 RESIZE — 실패는 항상 축소·차단 방향이지 확대 방향이 아니다.
- `engine/trading_state_store.py` — Sprint K1 "Redis 최신 Trading State"(DoD 4번 절반). scope별
  현재 Trading State를 Redis에서 Get/Set. Key 없음은 ENABLED(제한한 적 없음), Redis 장애는
  ENABLED로 잘못 추정하지 않고 예외를 올리거나(`get_state`) HALTED로 fail-closed한다
  (`get_state_fail_closed`, Pre-trade Hot Path 전용).
- Compliance, Stress 모듈은 아직 미구현 — 코드가 생기면 `compliance/`, `stress/`에 배치.
  Compliance는 이미 `skills/agentic-rag/`의 `compliance-policy-agent`가 담당 중이므로 중복 구현하지 않는다.
- 미착수: Sprint K0(Supabase `risk` 스키마 실기록 — `accounting.funds`가 비어 있어 `risk.policies`부터
  fund_id FK로 막힘, 회계본부 영역), K2(Evidence QA — AI QA/감사본부 영역), K3(Kill Switch 이력 기록,
  Stress), K4(Release/Access Audit), P1 Risk Metric, P2 파생상품 Greeks. RLS는 담당자가 의도적으로 보류.
  자세한 진행 상태는 `hermes/config.yaml`의 `implementation:` 블록 참고.

## 안전한 단독 실행

Risk 실행 소스는 `ai-office/` 아래에 있지 않다. 현재 디렉터리가 `ai-office`여도 저장소 루트로 이동한 뒤 실행한다. `risk_engine.py`는 결정론적 엔진 자체 점검이고, Hermes/LangGraph 경계·폴백·JSONL 원장까지 확인하려면 `scripts.py --run`을 사용한다.

```bash
cd /Users/baiohelseu/Desktop/Project/multi_agent
source ~/claude/bin/activate
python departments/03-risk/scripts.py --run --reject --log-path /tmp/hg-risk-run.jsonl
python departments/03-risk/engine/risk_engine.py
python departments/03-risk/engine/trading_state_store.py  # REDIS_URL 필요
python skills/agentic-rag/main.py \
  --persona compliance-policy-agent \
  --query "Can we open a new long position in AAPL today?" \
  --as-of 2026-07-31
```

`--run` 결과의 `execution_evidence.pipeline_status`가 `DEGRADED`이면 승인으로 해석하지 않고 `HOLD/ESCALATE`로 처리한다. Redis·실제 정책 Corpus·DB가 없으면 해당 단계는 성공으로 위장하지 않는다.

## P1/P2 검증 기록 (2026-08-03)

Risk·QA Redis 경계 검증은 두 부서 공통 테스트가 아니라 이 부서의 `TradingState`·P1 Risk Gate 연동 수용 기준으로 기록한다. 반드시 저장소 루트에서 `~/claude` 환경으로 실행한다.

```bash
cd /Users/baiohelseu/Desktop/Project/multi_agent
source ~/claude/bin/activate
which python
python -m pytest \
  departments/03-risk/tests/test_trading_state_store.py \
  departments/06-ai-qa-audit/tests/test_redis_event_bus_integration.py \
  -q -rs
```

`which python`이 `/Users/baiohelseu/claude/bin/python`이고 결과가 `11 passed`이며 `skipped`가 없어야 실제 Redis PING·상태 저장·QA 이벤트/캐시 통합이 확인된 것으로 본다. `skipped`는 연결 성공이 아니며, 운영 승인 근거로 사용하지 않는다.

- P1: Instrument Mapping, PIT/staleness, Exposure, VaR/Stress/Correlation, Entry Gate와 DB Repository/RLS baseline은 구현·단위 검증 완료. 실제 Portfolio/Market API, governed FK 데이터, 운영 DB/RLS/E2E는 외부 조건 대기다.
- P2: `analytics/risk_metrics.py`의 결정론적 historical VaR·Stress·Black-Scholes Greeks 계산기와 단위 테스트는 준비됐다. 파생상품 Snapshot/마진/변동성 표면·Golden Fixture·QuantLib 검증·Risk Gate/API 연결 전에는 P2 운영 완료로 보지 않는다.
- Kill Switch 이력 저장은 `requested_by`와 `approved_release_by`를 분리해 기록하고, FK/RLS 실패 시 전체 트랜잭션을 rollback한다.
