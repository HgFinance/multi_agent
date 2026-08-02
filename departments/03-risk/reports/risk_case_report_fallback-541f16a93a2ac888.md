# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `fallback-541f16a93a2ac888` |
| **판정 (verdict)** | **reject** |
| **승인 수량** | None |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (`risk-pipeline-fallback-v1`) |
| **input_hash** | `541f16a93a2ac88831848c4a21529eb422fb38d6f0e9fb77ee6d3301230af87d` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | HALTED |
| **주문** | BUY 100 x 21b77dfd-985e-447a-b977-ff3932e84de2 (fund a15f2cd4-e8e7-4b06-8831-d34d2badb325) |
| **escalate** | True |
| **생성** | risk-department-pipeline-v1, 2026-08-02T13:59:52.202853+00:00 |

---

## Pre-trade 검사 결과

| Check | 통과 | 상세 |
|---|---|---|
| — | — | (check_results 없음) |

## Counterparty / Broker 점검 (operational-counterparty-risk-agent)

counterparty_health 미플래그 - 조건부 노드 미호출

## Reason Codes

`pipeline_fallback`

## Compliance (compliance-policy-agent, Agentic RAG)

REJECT 조기 종료 - compliance_check 생략됨

## 종합 서술 (risk-supervisor, Hermes)

Risk pipeline Agent를 사용할 수 없어 결정론적 결과만 유지했습니다 (ModuleNotFoundError). 수동 검토와 후속 상태 확인이 필요합니다.

## 평가 지표

| 지표 | 값 |
|---|---|
| verdict | "reject" |
| deterministic_check_count | 0 |
| passed_check_count | 0 |
| failed_check_count | 0 |
| fallback_count | 1 |
| escalated | true |
| notion_upload_ok | true |
| report_markdown_chars | 2018 |
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
| 미실행/조건부 | market-liquidity-risk-agent |
| 미실행/조건부 | derivatives-margin-risk-agent |
| 미실행/조건부 | compliance-policy-agent |
| 미실행/조건부 | pre-trade-risk-analyst |
| 미실행/조건부 | operational-counterparty-risk-agent |

## Fallback / Escalation

| 단계 | 오류 | 조치 |
|---|---|---|
| pipeline | ModuleNotFoundError | ESCALATE |

## Notion 업로드 (Reporter Node)

업로드 성공: https://app.notion.com/p/risk_request_id-fallback-541f16a93a2ac888-3b0c2ded5680817d879fc6465e361c95

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.