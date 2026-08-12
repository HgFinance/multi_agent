# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `6b1b6d4f-5334-4b5c-abd5-6078f2151bbd` |
| **판정 후보 (verdict)** | **approve** |
| **판정 상태** | **DEGRADED** |
| **판정 출처** | DEGRADED_RISK_ENGINE |
| **안전 조치** | HOLD |
| **Risk 검사 실행** | True |
| **승인 수량** | 100 |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (degraded dependency) (`risk-p0-v1`) |
| **input_hash** | `ae9e241dc65c8249acfaa06ef040e3b585d03b1e06af67ce6b9317c6779a709f` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | ENABLED |
| **주문** | BUY 100 x caeda000-0d11-4d5b-8893-9526dfd54e85 (fund d436a5d2-5cb1-4574-9503-e233a788d6b5) |
| **escalate** | True |
| **생성** | risk-department-pipeline-v1, 2026-08-03T12:50:18.161237+00:00 |

---

## Pre-trade 검사 결과

| Check | 통과 | 상세 |
|---|---|---|
| data_freshness | True |  |
| market_tradable | True |  |
| mandate | True |  |
| restricted_list | True |  |
| notional_bounds | True |  |
| buying_power | True |  |
| concentration | True |  |
| turnover | True |  |
| trading_state | True |  |
| counterparty_health | True |  |

## Counterparty / Broker 점검 (operational-counterparty-risk-agent)

counterparty_health 미플래그 - 조건부 노드 미호출

## Reason Codes

없음

## Compliance (compliance-policy-agent, Agentic RAG)

| 필드 | 값 |
|---|---|
| grounded | True |
| attempts | 1 |
| answer | {"cited_documents": ["policy-mandate-001"], "confidence": 0.5, "escalate": true, "rationale": "The policy excerpts do not provide specific information about the tradability of the instrument 'caeda000-0d11-4d5b-8893-9526dfd54e85' or the fund 'd436a5d2-5cb1-4574-9503-e233a788d6b5'. Therefore, it is unclear whether the order can be executed without additional context regarding the instrument's trading volume, price, and the fund's current position limits.", "verdict": "ambiguous"} |

| 참조 문서 | version | score |
|---|---|---|
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3951 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3544 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3479 |

## 종합 서술 (risk-supervisor, Hermes)

결정론적 Risk Engine은 본 주문에 대해 approved_quantity 100으로 승인(approve)했으며, data_freshness, market_tradable, mandate, restricted_list, notional_bounds, buying_power, concentration, turnover, trading_state 및 counterparty_health 점검을 모두 통과했다. 다만 compliance 결과는 policy-mandate-001에 근거해 해당 상품과 펀드의 거래 가능성 및 포지션 한도가 불명확하다는 ambiguous 판정(confidence 0.5)을 내렸고 추가 맥락을 요구했다. 또한 market-liquidity-worker, pre-trade-risk-worker, compliance-policy-worker가 검증 실패로 DEGRADED 상태였으므로, 엔진의 승인 verdict는 변경하지 않되 CEO/Audit의 사후 검토 및 컴플라이언스 확인을 위해 에스컬레이션한다.

## 평가 지표

| 지표 | 값 |
|---|---|
| verdict | "approve" |
| deterministic_check_count | 10 |
| passed_check_count | 10 |
| failed_check_count | 0 |
| fallback_count | 3 |
| escalated | true |
| notion_upload_ok | true |
| report_markdown_chars | 4446 |
| langsmith_enabled | true |

## LangSmith / HR 관측성 전달

| 필드 | 값 |
|---|---|
| trace_id | `t1` |
| LangSmith | {"enabled": true, "handoff_status": "configured", "project": "First", "run_id": null} |

## Agent 실행 매니페스트

| 구분 | Agent |
|---|---|
| 실행 | risk-supervisor |
| 실패 | market-liquidity-worker |
| 실패 | pre-trade-risk-worker |
| 실패 | compliance-policy-worker |
| 미실행/조건부 | derivatives-counterparty-worker |

### LangGraph Employee Workers

| Worker | 상태 | 도구 |
|---|---|---|
| `market-liquidity-worker` | DEGRADED | risk.trading_state.read, risk.p1.snapshot |
| `pre-trade-risk-worker` | DEGRADED | risk.case.check |
| `compliance-policy-worker` | DEGRADED | risk.compliance.check |
- executor: `LangGraph`
- model: ``

### Hermes Runtime

- profile: `risk-management`
- provider/model: `openai-codex` / `gpt-5.6-luna`
- runtime config matches source: `True`
- supervisor call: `succeeded`
- skills: `70`; memory files: `0`

## Fallback / Escalation

> 의존성 fallback이 기록되었습니다. Risk Engine은 fail-closed로 실행됐으며, 정상 승인 경로로 해석하지 말고 안전 조치를 우선합니다.

| 단계 | 노드 | 오류 | 메시지 | 조치 |
|---|---|---|---|---|
| employee:market-liquidity-worker | employee_workers | BadRequestError;BadRequestError;BadRequestError | — | ESCALATE |
| employee:pre-trade-risk-worker | employee_workers | BadRequestError;BadRequestError;BadRequestError | — | ESCALATE |
| employee:compliance-policy-worker | employee_workers | BadRequestError;BadRequestError;BadRequestError | — | ESCALATE |

## Notion 업로드 (Reporter Node)

업로드 성공: https://app.notion.com/p/risk_request_id-6b1b6d4f-5334-4b5c-abd5-6078f2151bbd-3b1c2ded568081b78635edfbac800a0f

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.