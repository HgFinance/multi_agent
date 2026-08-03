# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `1f3e964b-a949-4646-b26a-6dcc87e701a8` |
| **판정 후보 (verdict)** | **reject** |
| **판정 상태** | **FINAL** |
| **판정 출처** | DETERMINISTIC_RISK_ENGINE |
| **안전 조치** | NOT_REQUIRED |
| **Risk 검사 실행** | True |
| **승인 수량** | None |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`risk-p0-v1`) |
| **input_hash** | `805748b047d6b11bbecdc1b1896d0905c03171f3497cb569c2eca549f05a78ab` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | HALTED |
| **주문** | BUY 100 x df084714-925c-4cdb-bfef-abd54035f518 (fund 51c6166d-8af8-4971-a731-632f48042934) |
| **escalate** | False |
| **생성** | risk-department-pipeline-v1, 2026-08-02T16:43:39.514150+00:00 |

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

결정론적 Risk Engine이 market_not_tradable 사유로 거래를 거절했습니다. 이 거절은 Risk Engine 판정이며 추가 주문 확대는 허용되지 않습니다.

## 평가 지표

| 지표 | 값 |
|---|---|
| verdict | "reject" |
| deterministic_check_count | 2 |
| passed_check_count | 1 |
| failed_check_count | 1 |
| fallback_count | 0 |
| escalated | false |
| notion_upload_ok | false |
| report_markdown_chars | 2082 |
| langsmith_enabled | true |

## LangSmith / HR 관측성 전달

| 필드 | 값 |
|---|---|
| trace_id | `t1` |
| LangSmith | {"enabled": true, "handoff_status": "configured", "project": "First", "run_id": null} |

## Agent 실행 매니페스트

| 구분 | Agent |
|---|---|
| 실행 | market-liquidity-risk-agent |
| 실행 | pre-trade-risk-analyst |
| 실행 | risk-supervisor |
| 미실행/조건부 | derivatives-margin-risk-agent |
| 미실행/조건부 | compliance-policy-agent |
| 미실행/조건부 | operational-counterparty-risk-agent |

## Notion 업로드 (Reporter Node)

업로드 생략/실패: 업로드 예외: <urlopen error [Errno 8] nodename nor servname provided, or not known>

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.