# AI QA/감사본부 — 감사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **qa_decision_id** | `501ec19d-6fef-4bf9-bf69-4bd5823de54d` |
| **판정 (verdict)** | **PASS** |
| **판정 엔진** | departments/06-ai-qa-audit/evidence/evidence_qa_engine.py (`qa-evidence-p0-v1`) |
| **input_hash** | `af3acf22f7e913a9bb2ab8bc95081ea6c1332bd596d03ab30b346e95dca16292` (같은 Artifact·Context면 재현 가능) |
| **decision_time (PIT)** | 2026-08-03T01:09:15.058135+00:00 |
| **escalate** | False |
| **생성** | qa-department-pipeline-v1, 2026-08-03T01:09:38.455470+00:00 |

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

결정론적 Evidence QA Engine의 binding decision은 PASS이며, 별도 reason code나 finding은 없습니다. Claim 0("AAPL 종가는 70000원")은 SUPPORTED로 확인되어 추가 에스컬레이션 사유가 없습니다.

## 평가 지표

| 지표 | 값 |
|---|---|
| verdict | "PASS" |
| claim_count | 1 |
| finding_count | 0 |
| unsupported_or_contradicted_count | 0 |
| fallback_count | 1 |
| escalated | false |
| notion_upload_ok | true |
| report_markdown_chars | 2323 |
| langsmith_enabled | false |

## LangSmith / HR 관측성 전달

| 필드 | 값 |
|---|---|
| trace_id | `546d9c8c-c974-41e7-bcb7-08fd55e1fc40` |
| LangSmith | {"enabled": false, "handoff_status": "not_configured", "project": null, "run_id": null} |

## Agent 실행 매니페스트

| 구분 | Agent |
|---|---|
| 실행 | evidence-qa-agent |
| 실행 | qa-audit-supervisor |
| 미실행/조건부 | hallucination-critic |
| 미실행/조건부 | model-risk-agent |
| 미실행/조건부 | internal-audit-agent |
| 미실행/조건부 | agent-ops-monitor |
| 미실행/조건부 | tool-permission-security-reviewer |
| 미실행/조건부 | incident-postmortem-agent |

### Hermes Runtime

- profile: `qa-department`
- provider/model: `openai-codex` / `gpt-5.6-luna`
- runtime config matches source: `True`
- supervisor call: `succeeded`
- skills: `70`; memory files: `0`

## Fallback / Escalation

| 단계 | 오류 | 조치 |
|---|---|---|
| claim_narrative | APITimeoutError | ESCALATE |

## Notion 업로드 (Reporter Node)

업로드 성공: https://app.notion.com/p/qa_decision_id-501ec19d-6fef-4bf9-bf69-4bd5823de54d-3b1c2ded5680819ca24cd0218efa4398

---
> 이 문서는 evidence_qa_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.