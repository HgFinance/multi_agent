# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-07

| Arm | 평균 F1 | EM 비율 | Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) | Semantic F1(LLM judge) | Semantic 정확도(LLM judge) |
|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.0286 | 0.0 | 0.5333 | 1813.3 | 3534.9 | 0.4722 | 0.3333 |
| B_llm_wiki_bm25 | 0.0581 | 0.0 | 0.4667 | 1423.5 | 1923.9 | 0.4 | 0.2 |
| C_llm_wiki_grep_bm25 | 0.0976 | 0.0 | 0.4 | 1489.5 | 2357.1 | 0.4 | 0.2 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| q01 | 0.4286 | False | breach | breach | 1600 | 1.0 | True |
| q02 | 0.0 | False | ambiguous | breach | 2400 | 0.0 | False |
| q03 | 0.0 | False | ambiguous | no_breach | 2400 | 0.0 | False |
| q04 | 0.0 | False | ambiguous | breach | 2400 | 0.5 | False |
| q05 | 0.0 | False | breach | breach | 800 | 1.0 | True |
| q06 | 0.0 | False | no_breach | no_breach | 800 | 1.0 | True |
| q07 | 0.0 | False | breach | breach | 2400 | 0.5 | False |
| q08 | 0.0 | False | breach | breach | 1600 | 0.8333 | True |
| q09 | 0.0 | False | breach | breach | 1600 | 1.0 | True |
| q10 | 0.0 | False | ambiguous | breach | 1600 | 0.0 | False |
| q11 | 0.0 | False | ambiguous | breach | 1600 | 0.0 | False |
| q12 | 0.0 | False | ambiguous | no_breach | 1600 | 0.0 | False |
| q13 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.0 | False |
| q14 | 0.0 | False | ambiguous | breach | 3200 | 0.5 | False |
| q15 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.75 | False |

### B_llm_wiki_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| q01 | 0.2927 | False | breach | breach | 429 | 1.0 | True |
| q02 | 0.2857 | False | breach | breach | 1535 | 1.0 | True |
| q03 | 0.0 | False | ambiguous | no_breach | 1535 | 0.0 | False |
| q04 | 0.0 | False | ambiguous | breach | 1420 | 0.0 | False |
| q05 | 0.0 | False | ambiguous | breach | 1845 | 0.0 | False |
| q06 | 0.0 | False | ambiguous | no_breach | 429 | 0.0 | False |
| q07 | 0.1538 | False | breach | breach | 1535 | 0.75 | False |
| q08 | 0.0 | False | breach | breach | 360 | 0.75 | False |
| q09 | 0.1395 | False | breach | breach | 1535 | 1.0 | True |
| q10 | 0.0 | False | ambiguous | breach | 1535 | 0.0 | False |
| q11 | 0.0 | False | ambiguous | breach | 1535 | 0.0 | False |
| q12 | 0.0 | False | ambiguous | no_breach | 2564 | 0.0 | False |
| q13 | 0.0 | False | ambiguous | ambiguous | 1535 | 0.0 | False |
| q14 | 0.0 | False | ambiguous | breach | 2026 | 0.75 | False |
| q15 | 0.0 | False | ambiguous | ambiguous | 1535 | 0.75 | False |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| q01 | 0.2553 | False | breach | breach | 1351 | 1.0 | True |
| q02 | 0.0 | False | ambiguous | breach | 1548 | 0.0 | False |
| q03 | 0.0 | False | ambiguous | no_breach | 1349 | 0.0 | False |
| q04 | 0.0 | False | ambiguous | breach | 1420 | 0.5 | False |
| q05 | 0.0 | False | ambiguous | breach | 1548 | 0.0 | False |
| q06 | 0.4737 | False | breach | no_breach | 1339 | 1.0 | True |
| q07 | 0.0889 | False | breach | breach | 1548 | 0.75 | False |
| q08 | 0.1333 | False | breach | breach | 1548 | 0.75 | False |
| q09 | 0.5135 | False | breach | breach | 1548 | 1.0 | True |
| q10 | 0.0 | False | ambiguous | breach | 1548 | 0.0 | False |
| q11 | 0.0 | False | ambiguous | breach | 1548 | 0.0 | False |
| q12 | 0.0 | False | ambiguous | no_breach | 1548 | 0.0 | False |
| q13 | 0.0 | False | ambiguous | ambiguous | 1403 | 0.0 | False |
| q14 | 0.0 | False | ambiguous | breach | 1548 | 0.5 | False |
| q15 | 0.0 | False | ambiguous | ambiguous | 1548 | 0.5 | False |
