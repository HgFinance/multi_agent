# 리스크본부 (Risk Management)

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

## 실행법

```bash
risk-management chat -q 'Assess risk of AAPL long position'
python departments/03-risk/engine/risk_engine.py
python3 skills/agentic-rag/main.py --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" --as-of 2026-07-29
```

## 테스트

- `engine/risk_engine.py` — P0 Pre-trade Risk Gate 22개 시나리오 자체 점검(팀 가이드 4.1 10단계 검사
  전부 + Hard/Soft 우선순위 + Trading State 예외 + 재현성 + 트레이딩본부 OMS End-to-End 통합).
- `compliance-policy-agent`의 Agentic RAG baseline은 `skills/agentic-rag/`(공용 skills 경계 유지, 이 본부가
  Domain Owner) 참고.

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `engine/risk_engine.py` — Sprint K1 P0 Pre-trade Risk Gate. 팀 가이드 4.1(10단계 검사), 5.2(Risk
  Request/Decision), 5.3(Trading State) 구현. LLM 호출 없음(팀 가이드 2절). Hard Limit 위반은 REJECT,
  Soft Limit 위반은 RESIZE — 실패는 항상 축소·차단 방향이지 확대 방향이 아니다.
- Compliance, Stress 모듈은 아직 미구현 — 코드가 생기면 `compliance/`, `stress/`에 배치.
  Compliance는 이미 `skills/agentic-rag/`의 `compliance-policy-agent`가 담당 중이므로 중복 구현하지 않는다.
- 미착수: Sprint K0(Supabase 실배선 — 스키마 자체는 이미 존재), K2(Evidence QA), K3(Intraday
  Snapshot/Stress/Kill Switch), K4(Release/Access Audit), P1 Risk Metric, P2 파생상품 Greeks.
  자세한 진행 상태는 `hermes/config.yaml`의 `implementation:` 블록 참고.
