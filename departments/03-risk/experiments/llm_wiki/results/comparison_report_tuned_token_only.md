# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-07

| Arm | 평균 F1 | EM 비율 | Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) |
|---|---|---|---|---|---|
| A_plain_rag | 0.0614 | 0.0 | 0.5333 | 1813.3 | 3379.2 |
| B_llm_wiki_bm25 | 0.0581 | 0.0 | 0.4667 | 1423.5 | 2009.7 |
| C_llm_wiki_grep_bm25 | 0.1051 | 0.0 | 0.4 | 1489.5 | 2360.6 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| q01 | 0.439 | False | breach | breach | 1600 |
| q02 | 0.0 | False | ambiguous | breach | 2400 |
| q03 | 0.0 | False | ambiguous | no_breach | 2400 |
| q04 | 0.0 | False | ambiguous | breach | 2400 |
| q05 | 0.0 | False | breach | breach | 800 |
| q06 | 0.0 | False | no_breach | no_breach | 800 |
| q07 | 0.0 | False | breach | breach | 2400 |
| q08 | 0.0 | False | breach | breach | 1600 |
| q09 | 0.4815 | False | breach | breach | 1600 |
| q10 | 0.0 | False | ambiguous | breach | 1600 |
| q11 | 0.0 | False | ambiguous | breach | 1600 |
| q12 | 0.0 | False | ambiguous | no_breach | 1600 |
| q13 | 0.0 | False | ambiguous | ambiguous | 1600 |
| q14 | 0.0 | False | ambiguous | breach | 3200 |
| q15 | 0.0 | False | ambiguous | ambiguous | 1600 |

### B_llm_wiki_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| q01 | 0.2927 | False | breach | breach | 429 |
| q02 | 0.2857 | False | breach | breach | 1535 |
| q03 | 0.0 | False | ambiguous | no_breach | 1535 |
| q04 | 0.0 | False | ambiguous | breach | 1420 |
| q05 | 0.0 | False | ambiguous | breach | 1845 |
| q06 | 0.0 | False | ambiguous | no_breach | 429 |
| q07 | 0.1538 | False | breach | breach | 1535 |
| q08 | 0.0 | False | breach | breach | 360 |
| q09 | 0.1395 | False | breach | breach | 1535 |
| q10 | 0.0 | False | ambiguous | breach | 1535 |
| q11 | 0.0 | False | ambiguous | breach | 1535 |
| q12 | 0.0 | False | ambiguous | no_breach | 2564 |
| q13 | 0.0 | False | ambiguous | ambiguous | 1535 |
| q14 | 0.0 | False | ambiguous | breach | 2026 |
| q15 | 0.0 | False | ambiguous | ambiguous | 1535 |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| q01 | 0.2553 | False | breach | breach | 1351 |
| q02 | 0.0 | False | ambiguous | breach | 1548 |
| q03 | 0.0 | False | ambiguous | no_breach | 1349 |
| q04 | 0.0 | False | ambiguous | breach | 1420 |
| q05 | 0.0 | False | ambiguous | breach | 1548 |
| q06 | 0.4737 | False | breach | no_breach | 1339 |
| q07 | 0.1778 | False | breach | breach | 1548 |
| q08 | 0.1333 | False | breach | breach | 1548 |
| q09 | 0.5357 | False | breach | breach | 1548 |
| q10 | 0.0 | False | ambiguous | breach | 1548 |
| q11 | 0.0 | False | ambiguous | breach | 1548 |
| q12 | 0.0 | False | ambiguous | no_breach | 1548 |
| q13 | 0.0 | False | ambiguous | ambiguous | 1403 |
| q14 | 0.0 | False | ambiguous | breach | 1548 |
| q15 | 0.0 | False | ambiguous | ambiguous | 1548 |
