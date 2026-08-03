# AI QA/감사본부 — 감사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **qa_decision_id** | `1d7fd2b7-dcc6-4d6c-a9be-daee6c7bb1b5` |
| **판정 (verdict)** | **PASS** |
| **판정 엔진** | departments/06-ai-qa-audit/evidence/evidence_qa_engine.py (`qa-evidence-p0-v1`) |
| **input_hash** | `d6bb67292b936b598aefbfe3248a2d44454b5f11f22ddde4b30472a092ca66d6` (같은 Artifact·Context면 재현 가능) |
| **decision_time (PIT)** | 2026-08-02T17:21:59.085681+00:00 |
| **escalate** | True |
| **생성** | qa-department-pipeline-v1, 2026-08-02T17:22:00.860144+00:00 |

---

## Claim별 검사 결과

| # | Claim | 검사 결과 | 사유 |
|---|---|---|---|
| 0 | AAPL 종가는 70000원 | SUPPORTED |  |

## Hallucination Critic (hallucination-critic, Agentic RAG)

UNSUPPORTED/CONTRADICTED claim 없음 - 조건부 노드 미호출

## Reason Codes

없음

## Findings

없음

## Claim 서술 (evidence-qa-agent, 내부 Ollama - 판정 재해석 없이 결과만 풀어씀)

결정론적 Evidence QA 결과를 전달합니다. Claim 0: SUPPORTED — 

## 종합 서술 (qa-audit-supervisor, Hermes)

QA Supervisor Agent를 사용할 수 없어 결정론적 QA 판정만 유지했습니다 (PermissionError). 원본 부서와 독립 검토자에게 수동 확인을 요청합니다.

## 평가 지표

| 지표 | 값 |
|---|---|
| verdict | "PASS" |
| claim_count | 1 |
| finding_count | 0 |
| unsupported_or_contradicted_count | 0 |
| fallback_count | 1 |
| escalated | true |
| notion_upload_ok | false |
| report_markdown_chars | 2070 |
| langsmith_enabled | false |

## LangSmith / HR 관측성 전달

| 필드 | 값 |
|---|---|
| trace_id | `cb1c0163-df1a-4adc-a80e-431ca532fa09` |
| LangSmith | {"enabled": false, "handoff_status": "not_configured", "project": null, "run_id": null} |

## Agent 실행 매니페스트

| 구분 | Agent |
|---|---|
| 실행 | evidence-qa-agent |
| 미실행/조건부 | qa-audit-supervisor |
| 미실행/조건부 | hallucination-critic |
| 미실행/조건부 | model-risk-agent |
| 미실행/조건부 | internal-audit-agent |
| 미실행/조건부 | agent-ops-monitor |
| 미실행/조건부 | tool-permission-security-reviewer |
| 미실행/조건부 | incident-postmortem-agent |

## Fallback / Escalation

| 단계 | 오류 | 조치 |
|---|---|---|
| supervisor | PermissionError | ESCALATE |

## Notion 업로드 (Reporter Node)

업로드 생략/실패: 업로드 예외: <urlopen error [Errno 8] nodename nor servname provided, or not known>

---
> 이 문서는 evidence_qa_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.