# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `1558025b-b81a-42bf-92b6-fea295d17a53` |
| **판정 (verdict)** | **approve** |
| **승인 수량** | 100 |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`risk-p0-v1`) |
| **input_hash** | `8cb10cd6ecc6c3cfd556f0bf67a1ea90a42a71ee7f18faff68b737db2a281a17` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | ENABLED |
| **주문** | BUY 100 x fa582be0-84e0-402c-9797-ac7e6e253148 (fund 45625544-bb1a-4874-9baf-defd7ca5b22d) |
| **escalate** | True |
| **생성** | risk-department-pipeline-v1, 2026-08-01T15:45:29.242311+00:00 |

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
| counterparty_health | True | DEGRADED 상태 - 통과하되 기록 |

## Counterparty / Broker 점검 (operational-counterparty-risk-agent)

브로커는 DEGRADED 상태로 기록된 바 있으나, 카운터파티 검증을 통과하여 주문이 approve되었습니다. 신규 주문보다 먼저 브로커 상태 확인 및 회계와의 Reconciliation을 우선하여 진행하시기 바랍니다.
(escalate: False)

## Reason Codes

없음

## Compliance (compliance-policy-agent, Agentic RAG)

| 필드 | 값 |
|---|---|
| grounded | True |
| attempts | 1 |
| answer | {'verdict': 'ambiguous', 'cited_documents': ['policy-mandate-001'], 'rationale': 'The policy excerpts do not provide specific information about the tradability of the instrument fa582be0-84e0-402c-9797-ac7e6e253148, such as its average trading volume or price. Therefore, it is unclear whether the order can be executed under the current policy guidelines.', 'confidence': 0.5, 'escalate': True} |

| 참조 문서 | version | score |
|---|---|---|
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3699 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3579 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3523 |

## 종합 서술 (risk-supervisor, Hermes)

거래건(6ded5028) BUY 100주 @ 70000원(LIMIT, 시장 매수호가 69900/매도호가 70000) 주문은 Risk Engine이 data_freshness, market_tradable, mandate, restricted_list, notional_bounds, buying_power, concentration, turnover, trading_state, counterparty_health 10개 검사를 통과하여 approve(승인수량 100)라는 바인딩 판결을 내렸습니다. counterparty_health 검사는 브로커가 DEGRADED 상태임을 기록했으나 검증을 통과했고, counterparty 증거에 따르면 신규 주문보다 브로커 상태 확인 및 회계와의 Reconciliation을 우선해야 한다는 권고가 있습니다. 반면 compliance(agentic-rag) 검사는 policy-mandate-001 기준 해당 종목의 거래 가능성에 대한 구체적 규정이 없어 verdict를 ambiguous(신뢰도 0.5)로 판정했고 escalate을 요청하여 CEO/Audit 추가 검토가 필요합니다.

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.