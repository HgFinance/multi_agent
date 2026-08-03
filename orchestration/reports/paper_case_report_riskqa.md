# Paper Investment Case Report

> 이 문서는 주문·브로커·원장·DB를 변경하지 않는 `paper-e2e` 또는 `paper` 결과다.
> 시장 데이터 기반 투자 자문이나 실거래 승인으로 사용할 수 없다.

## 1. CEO 요약

| 항목 | 값 |
|---|---|
| Case ID | `paper-case-aapl-001` |
| Pipeline | `INCONCLUSIVE` |
| Workflow run | `wf-20260803T052725Z-f9c612e1` |
| Execution mode | `paper` |
| Workflow status | `COMPLETED` |
| Binding decision | **`HOLD / ESCALATE`** |
| Binding | `False` |
| Generated at | `2026-08-03T05:27:29.000838+00:00` |

### CEO 최종 페이퍼 판정

**HOLD / ESCALATE**

전체 부서 결과가 완료되지 않았거나 실행 증거가 부족하다. 누락된 근거를 재현 가능한 run으로 보강하기 전까지 신규 진입을 차단한다.

CEO adapter: `ceo-agent / unknown / failed`

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
| research | research-department | PAPER_DOMAIN_DEGRADED | `case_request` → `research_packet` | No | HOLD | paper_department=research status=DEGRADED output=research_packet binding=False external_writes=false |
| trading | trading-department | PAPER_DOMAIN_PASS | `research_packet` → `order_intent` | No | HOLD | paper_department=trading status=COMPLETED output=order_intent binding=False external_writes=false |
| risk | risk-management | PAPER_DOMAIN_DEGRADED | `order_intent` → `risk_decision` | No | REJECT | paper_department=risk status=DEGRADED output=risk_decision binding=False external_writes=false |
| qa | qa-department | PAPER_DOMAIN_DEGRADED | `risk_decision` → `qa_assessment` | No | ESCALATE | paper_department=qa status=DEGRADED output=qa_assessment binding=False external_writes=false |
| oms-fill-gate | trading-department | PAPER_DOMAIN_PASS | `qa_assessment` → `execution_result` | No | HOLD | paper_department=oms-fill-gate status=BLOCKED output=execution_result binding=False external_writes=false |
| accounting | accounting-portfolio-department | PAPER_DOMAIN_PASS | `execution_result` → `accounting_snapshot` | No | BREAK | paper_department=accounting status=PAPER_NOT_POSTED output=accounting_snapshot binding=False external_writes=false |
| ceo | ceo-agent | PAPER_DOMAIN_DEGRADED | `accounting_snapshot` → `ceo_case_summary` | No | ESCALATE | paper_department=ceo status=DEGRADED output=ceo_case_summary binding=False external_writes=false |

`PAPER_SMOKE_PASS`는 프로필 호출과 계약 경계만 통과했다는 뜻이다.
`PAPER_DOMAIN_PASS`는 Research/Risk/QA 부서 진입점과 CEO 종합 adapter가
실행됐다는 뜻이다. 어느 경우에도 Broker 제출, 체결 확정, Ledger posting을 의미하지 않는다.

## 4. Risk / QA 실행 증거

| 부서 | 상태 | 직원/LangGraph 실행 | Hermes 모델/호출 | 폴백·오류 |
|---|---|---|---|---|
| Risk | DEGRADED | LangGraph=True; executed=market-liquidity-risk-agent, pre-trade-risk-analyst; failed=—; not_executed=risk-supervisor, derivatives-margin-risk-agent, compliance-policy-agent, operational-counterparty-risk-agent | gpt-5.6-luna / not_called | [{'stage': 'trading_state', 'node': 'trading_state', 'error': 'KeyError', 'error_message': "'REDIS_URL'", 'error_fingerprint': '0ed545bb17e1cfa1', 'action': 'ESCALATE', 'safe_action': 'HOLD', 'decision_origin': 'FALLBACK'}] |
| QA | DEGRADED | LangGraph=True; executed=evidence-qa-agent; failed=qa-audit-supervisor; not_executed=hallucination-critic, model-risk-agent, internal-audit-agent, agent-ops-monitor, tool-permission-security-reviewer, incident-postmortem-agent | gpt-5.6-luna / failed | [{'stage': 'supervisor', 'error': 'PermissionError', 'action': 'ESCALATE'}] |

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
