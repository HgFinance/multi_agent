# AI QA/감사본부 — 감사 보고서 (결정론적 생성, LLM 자유 서술 아님)

| 항목 | 값 |
|---|---|
| **qa_decision_id** | `d2df85f0-cde5-4fb0-9dcf-0d4c600f5e2a` |
| **판정 (verdict)** | **FAIL** |
| **판정 엔진** | departments/06-ai-qa-audit/evidence/evidence_qa_engine.py (`qa-evidence-p0-v1`) |
| **input_hash** | `7e4ea8b170e394db1cf2a5d166c0d211d7d2a87b75ddcec4e16bb7249eac69e3` (같은 Artifact·Context면 재현 가능) |
| **decision_time (PIT)** | 2026-08-01T16:51:19.608249+00:00 |
| **escalate** | True |
| **생성** | qa-department-pipeline-v1, 2026-08-01T16:52:08.452803+00:00 |

---

## Claim별 검사 결과

| # | Claim | 검사 결과 | 사유 |
|---|---|---|---|
| 0 | AAPL 종가는 70000원 | UNSUPPORTED | 사실 주장인데 인용 근거가 없습니다 |

## Reason Codes

`fact_without_evidence`

## Findings

### `658b31b8-641c-476c-bcac-7e714eee9e9a`

| 필드 | 값 |
|---|---|
| 유형 | `unsupported_claim` |
| 심각도 | HIGH |
| 설명 | Claim #0 (research_packet): 사실 주장인데 인용 근거가 없습니다 |


## Claim 서술 (evidence-qa-agent, 내부 Ollama - 판정 재해석 없이 결과만 풀어씀)

아래는 미리 계산한 Claim별 검사 결과의 한국어 요약입니다:
0번째 주장 - "AAPL 종가는 70000원" : 이 주장은 근거가 없어서 거부(PASS)됩니다. CLAIM_INDEX: 0, CLAIM: "AAPL 종가는 70000원", RESULT: UNSUPPORTED, REASON: "사실 주장인데 인용 근거가 없습니다"

## 종합 서술 (qa-audit-supervisor, Hermes)

증거 QA 엔진의 결정은 FAIL이며, 사실 주장에 대한 인용 근거 부재(fact_without_evidence)로 인해 문턱이 붕괴했습니다. 연구 자료 패킷 내 주장 #0('AAPL 종가는 70000원')은 근거 없이 UNSUPPORTED 판정을 받았고, 이는 HIGH 심각도의 unsupported_claim Finding(658b31b8-641c-476c-bcac-7e714eee9e9a)으로 기록되었습니다. 책임 제어 담당자와 CEO에게 이 검증 불가 지적을 동시에 공유하여, 해당 부서의 운용 데이터 무결성 및 책임 있는 주장 체계의 결함을 즉시 조치하도록 요구해야 합니다.

## Notion 업로드 (Reporter Node)

업로드 성공: https://app.notion.com/p/qa_decision_id-d2df85f0-cde5-4fb0-9dcf-0d4c600f5e2a-3afc2ded568081c59c08d9f64413cf30

---
> 이 문서는 evidence_qa_engine.py의 결정론적 판정과 스키마 검증된 LLM 서술을 Python이 그대로
> 옮긴 것이다 - LLM이 이 파일의 형식이나 내용을 자유롭게 창작하지 않았다.