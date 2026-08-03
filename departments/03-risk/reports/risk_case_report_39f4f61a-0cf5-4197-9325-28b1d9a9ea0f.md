# 리스크본부 — Case 심사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **risk_request_id** | `39f4f61a-0cf5-4197-9325-28b1d9a9ea0f` |
| **판정 후보 (verdict)** | **reject** |
| **판정 상태** | **DEGRADED** |
| **판정 출처** | DEGRADED_RISK_ENGINE |
| **안전 조치** | HOLD |
| **Risk 검사 실행** | True |
| **승인 수량** | None |
| **판정 엔진** | departments/03-risk/engine/risk_engine.py (degraded dependency) (`risk-p0-v1`) |
| **input_hash** | `587b2d87a26f36bace69b6abd7e6fa32b7e49f45d379564da5b98786999a12f4` (같은 OrderIntent·Context면 재현 가능) |
| **trading_state** | HALTED |
| **주문** | BUY 100 x 4c0bec83-e330-499f-8e56-fe2de8bfef5b (fund 54004dc0-98e3-479b-a220-b2f31b8d1fb0) |
| **escalate** | True |
| **생성** | risk-department-pipeline-v1, 2026-08-02T17:25:51.868269+00:00 |

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

Risk pipeline이 완료되지 않아 결정론적 승인 판정을 만들 수 없습니다. 실패 단계=trading_state, 오류=KeyError; 비바인딩 fallback으로 HOLD/수동 검토가 필요합니다.

## 평가 지표

| 지표 | 값 |
|---|---|
| verdict | "reject" |
| deterministic_check_count | 2 |
| passed_check_count | 1 |
| failed_check_count | 1 |
| fallback_count | 1 |
| escalated | true |
| notion_upload_ok | false |
| report_markdown_chars | 2521 |
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
| 미실행/조건부 | risk-supervisor |
| 미실행/조건부 | derivatives-margin-risk-agent |
| 미실행/조건부 | compliance-policy-agent |
| 미실행/조건부 | operational-counterparty-risk-agent |

### Hermes Runtime

- profile: `risk-management`
- provider/model: `openai-codex` / `gpt-5.6-luna`
- supervisor call: `not_called`
- skills: `70`; memory files: `0`

## Fallback / Escalation

> 의존성 fallback이 기록되었습니다. Risk Engine은 fail-closed로 실행됐으며, 정상 승인 경로로 해석하지 말고 안전 조치를 우선합니다.

| 단계 | 노드 | 오류 | 메시지 | 조치 |
|---|---|---|---|---|
| trading_state | trading_state | KeyError | 'REDIS_URL' | ESCALATE |

## Notion 업로드 (Reporter Node)

업로드 생략/실패: 업로드 예외: <urlopen error [Errno 8] nodename nor servname provided, or not known>

---
> 이 문서는 risk_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.