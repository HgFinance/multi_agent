# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `c9769be7-9b1a-43da-ae67-6229d11041d0` |
| **판정 (verdict)** | **reject** |
| **승인 수량** | None |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`risk-p0-v1`) |
| **input_hash** | `c704b3c4bd00aca0721790ebf677e216fb28c0147a9c7bd83c349053d4786817` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | HALTED |
| **주문** | BUY 100 x 2980c0e8-85ec-4987-93ef-efe1ce93f69b (fund 3c92d660-e38e-40a2-872f-64618eb4debc) |
| **escalate** | False |
| **생성** | risk-department-pipeline-v1, 2026-08-02T15:47:39.860232+00:00 |

---

## Pre-trade 검사 결과

| Check | 통과 | 상세 |
|---|---|---|
| data_freshness | True |  |
| market_tradable | False | 시장/종목이 거래 불가 상태입니다 |

## Counterparty / Broker 점검 (operational-counterparty-risk-agent)

counterparty_health 미플래그 - 조건부 노드 미호출

## Reason Codes

`market_not_tradable`

## Compliance (compliance-policy-agent, Agentic RAG)

REJECT 조기 종료 - compliance_check 생략됨

## 종합 서술 (risk-supervisor, Hermes)

Trade Case b4f681c6(펀드 3c92d660, 북 81852689, 종목 2980c0e8)의 BUY 100 @ 70000 LIMIT 주문은 deterministic Risk Engine(risk-p0-v1)에서 'reject' 판정을 받았습니다. market_tradable 체크가 실패하여 시장/종목이 HALTED(거래 중단) 상태임이 확인되었고(reason code: market_not_tradable, 상세: '시장/종목이 거래 불가 상태입니다') 거래가 불가능함으로 주문이 거부되었습니다. data_freshness 체크는 통과했으나 시장 중단 자체를 해결하지는 못합니다. compliance 검토 결과와 counterparty 검토 결과가 각각 null이므로, 본 거부 판정은 오직 market_tradable 확인에 의해 결정되었습니다.

## 평가 지표

| 지표 | 값 |
|---|---|
| verdict | "reject" |
| deterministic_check_count | 2 |
| passed_check_count | 1 |
| failed_check_count | 1 |
| fallback_count | 1 |
| escalated | false |
| notion_upload_ok | true |
| report_markdown_chars | 2386 |
| langsmith_enabled | false |

## LangSmith / HR 관측성 전달

| 필드 | 값 |
|---|---|
| trace_id | `t1` |
| LangSmith | {"enabled": false, "handoff_status": "not_configured", "project": null, "run_id": null} |

## Agent 실행 매니페스트

| 구분 | Agent |
|---|---|
| 실행 | market-liquidity-risk-agent |
| 실행 | pre-trade-risk-analyst |
| 실행 | risk-supervisor |
| 미실행/조건부 | derivatives-margin-risk-agent |
| 미실행/조건부 | compliance-policy-agent |
| 미실행/조건부 | operational-counterparty-risk-agent |

## Fallback / Escalation

| 단계 | 오류 | 조치 |
|---|---|---|
| trading_state | KeyError | ESCALATE |

## Notion 업로드 (Reporter Node)

업로드 성공: https://app.notion.com/p/risk_request_id-c9769be7-9b1a-43da-ae67-6229d11041d0-3b0c2ded56808176905ce6b97d7e7d09

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.