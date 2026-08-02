# 리스크본부 (Risk Management)

## P1 현재 상태 (2026-08-02)

- `p1/analytics.py`가 canonical instrument UUID 매핑, PIT/staleness 검사, Exposure Snapshot, Stress/VaR/Correlation 지표와 `ENABLED` 외 진입 차단을 하나의 결정론적 경계로 묶는다.
- `p1/repository.py`가 `risk.snapshots`, `risk.exposure_components`, `risk.stress_results`, `risk.kill_switch_events`를 한 트랜잭션으로 적재한다. Fund/Book/Instrument/승인된 Stress Scenario FK가 없으면 생성·우회하지 않고 rollback한다.
- LS증권 어댑터는 읽기 전용이다. 실제 키·계좌·운영 DB가 주입되기 전에는 실제 데이터를 수집하거나 운영 Snapshot을 만들지 않는다. `RISK_REQUIRE_P1_ANALYTICS=true`인 pre-trade API는 P1 Snapshot이 없거나 PASS가 아니면 차단한다.
- 남은 운영 조건은 실제 API 자격증명, governed FK 원장, RLS/OMS E2E 및 운영 장애 검증이다. P1 계산 코드가 구현됐다는 뜻이지 실거래 승인을 뜻하지 않는다.

## Skill Harness

`harness/manifest.py`가 Risk 스킬과 허용 Tool을 고정하고, `harness/core.py`가 trace·비밀값·금지 Tool을 호출 전에 차단한다. 실패 fallback은 `REJECT + HALTED`다. Redis 연결 상태는 실제 비밀값을 출력하지 않는 `harness/redis_check.py`로 확인한다.

`harness/journal.py`는 Hermes 부서 실행과 LangGraph 직원 실행을 `run_id`로 묶어 `InputSnapshot → AgentOutput → Validation → Decision`을 기록한다. Order와 Fill은 별도 이벤트이며 `inputs_hash`, 모델·프롬프트·파라미터 버전, retry/fallback, rationale/evidence를 보존한다. `RunJournal.replay()`와 `RunJournal.review()`는 단계 재실행과 폴백 사유 집계를 제공하고, 운영 적재 스키마는 `risk.run_log_events`다.

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Local Ollama Alias는 [`Modelfile`](Modelfile)의 `hermes3` 기반 `agent-risk`이고 Hermes Profile은 `risk-management`다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

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
