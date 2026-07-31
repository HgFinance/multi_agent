# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `01d8dfad-c850-4e78-9948-500edd8f9d75` |
| **판정 (verdict)** | **approve** |
| **승인 수량** | 100 |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`risk-p0-v1`) |
| **input_hash** | `6e6f89eab08bfef0f6e932515bf4e61ea40242cee90798209394ecb52f49bd81` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | ENABLED |
| **주문** | BUY 100 x 61beca24-fed7-4460-8642-2d5cb03b29cd (fund f6de3af4-b102-4fd9-a819-06f630ee7ff6) |
| **escalate** | True |
| **생성** | risk-department-pipeline-v1, 2026-07-31T16:45:52.050622+00:00 |

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

## Reason Codes

없음

## Compliance (compliance-policy-agent, Agentic RAG)

| 필드 | 값 |
|---|---|
| grounded | True |
| attempts | 1 |
| answer | {'verdict': 'ambiguous', 'cited_documents': ['policy-mandate-001'], 'rationale': 'The policy excerpts do not provide specific information about the tradability of the instrument in question (61beca24-fed7-4460-8642-2d5cb03b29cd) or its compliance with the required trading conditions such as average trading volume, price, or any current trading status. Therefore, it is unclear whether executing a BUY order is permissible under the current policy.', 'confidence': 0.5, 'escalate': True} |

| 참조 문서 | version | score |
|---|---|---|
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3657 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3477 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3433 |

## 종합 서술 (risk-supervisor, Hermes)

CASE_ID c685f2d9-9d25-4991-83c8-1a505ca499f4 (BUY, qty 100, limit 70,000) — Deterministic Risk Engine 'approve' (approved_quantity 100, 10/10 checks passed: data_freshness, market_tradable, mandate, restricted_list, notional_bounds, buying_power, concentration, turnover, trading_state, counterparty_health). Compliance Policy Agent returned 'ambiguous' on policy-mandate-001 (ver. 1.1.0) — cited mandate lacks instrument-specific tradability criteria (avg volume, price, trading status) — confidence 0.5, escalate=true. Recommendation: CEO/Audit review the policy gap; risk verdict remains 'approve' pending clarification.

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.