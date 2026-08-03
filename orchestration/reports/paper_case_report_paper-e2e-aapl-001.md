# Paper Investment Case Report

> 이 문서는 주문·브로커·원장·DB를 변경하지 않는 `paper-e2e` 연결 검증 결과다.
> 시장 데이터 기반 투자 자문이나 실거래 승인으로 사용할 수 없다.

## 1. CEO 요약

| 항목 | 값 |
|---|---|
| Case ID | `paper-e2e-aapl-001` |
| Pipeline | `PAPER_CONNECTED` |
| Workflow run | `wf-20260803T031139Z-c09271a7` |
| Workflow status | `COMPLETED` |
| Binding decision | **`HOLD / ESCALATE`** |
| Binding | `False` |
| Generated at | `2026-08-03T03:19:12.321524+00:00` |

### CEO 최종 페이퍼 판정

**HOLD / ESCALATE**

7개 Hermes Profile smoke와 handoff 계약은 통과했다. 그러나 이 실행은 실제 직원 작업·시장 Snapshot·결정론적 Risk/QA 결과·OMS/Fill·원장 반영을 수행하지 않았으므로 실거래 승격 근거가 없다.

CEO는 모든 부서 결과를 종합해 이 Case의 페이퍼 판정을 내릴 수 있지만,
Risk 한도 승인·주문 제출·원장 수정·NAV 확정 권한을 갖지 않는다. 실거래 전환은
별도의 결정론적 Risk/QA/OMS 및 승인된 production adapter가 필요하다.

## 2. 입력 Case

| 필드 | 값 |
|---|---|
| Symbol | `AAPL` |
| Side | `BUY` |
| Quantity | `100` |
| Order type | `LIMIT` |
| Limit price | `200.00` |
| Stage | `paper` |

## 3. Paper 예측 — 예시 시나리오

> 아래 확률은 외부 시세·포트폴리오·정책 Corpus를 조회해 산출한 값이 아니다.
> 연결 검증을 위한 고정 illustrative baseline이며, CEO 판정이나 주문 수량에 사용하지 않았다.

| Horizon / outcome | Probability |
|---|---:|
| T+5 up | 45.00% |
| T+5 sideways | 30.00% |
| T+5 down | 25.00% |

Prediction status: `SIMULATION_ONLY`  
Prediction binding: `False`  
Prediction action: `HOLD` — 실제 Snapshot과 근거가 없으므로 진입 신호로 승격하지 않음

## 4. 전체 부서 종합

| Step | Hermes/Profile | Status | Contract handoff | Binding | Failure action | Evidence |
|---|---|---|---|---|---|---|
| research | research-department | PAPER_SMOKE_PASS | `case_request` → `research_packet` | No | HOLD | hermes_smoke=PASS profile=research-department input=case_request output=research_packet paper_no_side_effects=true |
| trading | trading-department | PAPER_SMOKE_PASS | `research_packet` → `order_intent` | No | HOLD | hermes_smoke=PASS profile=trading-department input=research_packet output=order_intent paper_no_side_effects=true |
| risk | risk-management | PAPER_SMOKE_PASS | `order_intent` → `risk_decision` | No | REJECT | hermes_smoke=PASS profile=risk-management input=order_intent output=risk_decision paper_no_side_effects=true |
| qa | qa-department | PAPER_SMOKE_PASS | `risk_decision` → `qa_assessment` | No | ESCALATE | hermes_smoke=PASS profile=qa-department input=risk_decision output=qa_assessment paper_no_side_effects=true |
| oms-fill-gate | trading-department | PAPER_SMOKE_PASS | `qa_assessment` → `execution_result` | No | HOLD | hermes_smoke=PASS profile=trading-department input=qa_assessment output=execution_result paper_no_side_effects=true |
| accounting | accounting-portfolio-department | PAPER_SMOKE_PASS | `execution_result` → `accounting_snapshot` | No | BREAK | hermes_smoke=PASS profile=accounting-portfolio-department input=execution_result output=accounting_snapshot paper_no_side_effects=true |
| ceo | ceo-agent | PAPER_SMOKE_PASS | `accounting_snapshot` → `ceo_case_summary` | No | ESCALATE | hermes_smoke=PASS profile=ceo-agent input=accounting_snapshot output=ceo_case_summary paper_no_side_effects=true |

각 단계의 `PAPER_SMOKE_PASS`는 프로필 호출과 계약 경계가 통과했다는 뜻이다.
직원 LangGraph 분석, 실제 시장 예측, Risk 계산, QA evidence 판정, OMS/Fill,
Accounting posting이 실행됐다는 뜻은 아니다.

## 5. Production adapter 승인 기준

실제 운영 adapter는 다음 순서를 모두 통과해야 한다. 한 단계라도 실패하면 자동 승격하지 않고
`HOLD` 또는 `ESCALATE`한다.

1. **Adapter manifest 고정:** 소유 팀, 버전/commit, 입력·출력 계약, 허용 도구, 금지 부작용을 등록한다.
2. **QA 독립 검증:** schema/contract, replay, idempotency, timeout/retry, 로그·trace·PII 마스킹을 검증한다.
3. **Risk 승인:** 실제 Portfolio/Market Snapshot, limit, Stress/VaR/Greeks, Kill Switch와 fail-closed를 검증한다.
4. **Paper acceptance:** 주문 제출 없이 전체 handoff와 예상 결과를 재현하고, 실패 주입 시 HOLD/REJECT/ESCALATE를 확인한다.
5. **운영 승인:** CEO/권한 있는 운영자가 승인 범위·유효기간·rollback owner를 명시한 approval record를 만든다.
6. **Production gate:** IAM이 승인된 immutable artifact만 배포하고, shadow/canary 후에만 live 권한을 별도로 부여한다.

필수 승인 증거: `adapter_version`, `artifact_digest`, `qa_run_id`, `risk_run_id`,
`replay_hash`, `approval_id`, `approved_scope`, `expires_at`, `rollback_plan`.

## 6. 안전성·한계

- Broker order, Paper Broker fill, Ledger posting, Supabase/Redis/Notion write: **수행하지 않음**
- 실제 시장 데이터·계좌 잔고·정책 원문: **이 보고서에는 없음**
- Hermes smoke/runtime 오류: `없음`
- 최종 바인딩 상태: **HOLD / ESCALATE**

따라서 이 결과는 “연결이 잘 되었는가”에 대한 페이퍼 검증으로는 유효하지만,
“투자해도 되는가”에 대한 승인 결과는 아니다.
