# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `ffde14a4-c820-4247-b0d7-07cf2e54892b` |
| **판정 (verdict)** | **approve** |
| **승인 수량** | 100 |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`risk-p0-v1`) |
| **input_hash** | `e5af20fbe9f39791ee3e15698b53a613b140adfb513874c2f8516ce677ae9e3e` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | ENABLED |
| **주문** | BUY 100 x 38399e0b-bac6-4c80-878d-2b63132ef35d (fund dfe45fdc-8305-407c-8b46-32483fc4b42c) |
| **escalate** | True |
| **생성** | risk-department-pipeline-v1, 2026-08-01T15:34:06.224716+00:00 |

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
| answer | {'verdict': 'ambiguous', 'cited_documents': ['policy-mandate-001'], 'rationale': 'The policy excerpts do not provide specific information about the tradability of the instrument 38399e0b-bac6-4c80-878d-2b63132ef35d, such as its average trading volume or price. Therefore, it is unclear whether the order can be executed based on the tradability filter criteria.', 'confidence': 0.5, 'escalate': True} |

| 참조 문서 | version | score |
|---|---|---|
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3753 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3527 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3421 |

## 종합 서술 (risk-supervisor, Hermes)

결정적 위험 엔진(Risk Engine)의 판정은 'approve'이며, 이는 시정할 수 없는 최종 결정입니다. data_freshness, market_tradable, mandate, restricted_list, notional_bounds, buying_power, concentration, turnover, trading_state, counterparty_health 등 10개 검증 모두 통과한 결과이며, 특히 trading_state가 'ENABLED'상태를 확인했습니다. compliance Agent의 판정은 'ambiguous'로, policy-mandate-001(투자 위원회) 문서에 따라 해당 instrument(38399e0b-bac6-4c80-878d-2b63132ef35d)의 평균 거래량이나 가격 정보가 명시되어 있지 않아 tradability 기준 충족 여부를 판단하기 어렵다고 보고되었습니다(confidence 0.5, escalate: true). 이에 따라 내러티브 검증을 위해 policy-mandate-001 문서의 instrument별 거래성 기준을 보강할 필요가 있습니다.

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.