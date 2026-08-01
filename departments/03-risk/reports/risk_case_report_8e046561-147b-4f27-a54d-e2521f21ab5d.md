# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `8e046561-147b-4f27-a54d-e2521f21ab5d` |
| **판정 (verdict)** | **approve** |
| **승인 수량** | 100 |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`risk-p0-v1`) |
| **input_hash** | `b6f7c89f772963bc1aa85d2c4ec8064753620e0638fbae033196176622dafbae` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | ENABLED |
| **주문** | BUY 100 x 29c1ab41-7c4d-4715-9a14-bf24a6a50490 (fund f683a617-fdf5-4127-8535-ecde532a7080) |
| **escalate** | False |
| **생성** | risk-department-pipeline-v1, 2026-08-01T16:45:55.652741+00:00 |

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
| answer | {'verdict': 'ambiguous', 'cited_documents': ['policy-mandate-001'], 'rationale': 'The policy excerpts provide criteria for tradability but do not specify whether the instrument in question meets these criteria. Without information on the average trading volume, stock price, or any designations (e.g., management, trading halt, investment warning), it is unclear if the BUY order can be executed.', 'confidence': 0.5, 'escalate': True} |

| 참조 문서 | version | score |
|---|---|---|
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3747 |
| Investment Mandate (`policy-mandate-001`) | 1.1.0 | 0.3562 |

## 종합 서술 (risk-supervisor, Hermes)

거래 건(a6c1636b-9cba-4f26-8650-f72176a729ae)에 대해 결정론적 위험 엔진이 'approve' 판정을 내렸으며, data_freshness, market_tradable, mandate, restricted_list, notional_bounds, buying_power, concentration, turnover, trading_state, counterparty_health 등 10개 검사를 모두 통과했습니다. 준법 검토는 policy-mandate-001(버전 1.1.0)에 따라 'ambiguous' 판정을 내렸지만 신뢰도는 0.5로, 평균 거래량이나 주가 기준 충족 여부를 확인하지 못해 회의가 있습니다. counterparty 증거가 없으므로, 엔진의 approve 판정을 존중하여 전량(100주) 거래를 진행합니다.

## Notion 업로드 (Reporter Node)

업로드 성공: https://app.notion.com/p/risk_request_id-8e046561-147b-4f27-a54d-e2521f21ab5d-3afc2ded5680818cb5c8eec975c2ad66

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.