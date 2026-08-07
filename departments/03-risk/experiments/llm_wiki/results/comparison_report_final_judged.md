# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-07

| Arm | 평균 F1 | EM 비율 | Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) | Semantic F1(LLM judge) | Semantic 정확도(LLM judge) |
|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.0601 | 0.0 | 0.5333 | 1813.3 | 3923.7 | 0.4833 | 0.2667 |
| B_llm_wiki_bm25 | 0.3082 | 0.0 | 0.7333 | 1720.2 | 3095.1 | 0.7722 | 0.5333 |
| C_llm_wiki_grep_bm25 | 0.3354 | 0.0 | 0.8 | 1791.5 | 2824.6 | 0.8667 | 0.6 |

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
| q08 | 0.0 | False | breach | breach | 1600 | 0.75 | False |
| q09 | 0.4727 | False | breach | breach | 1600 | 1.0 | True |
| q10 | 0.0 | False | ambiguous | breach | 1600 | 0.0 | False |
| q11 | 0.0 | False | ambiguous | breach | 1600 | 0.0 | False |
| q12 | 0.0 | False | ambiguous | no_breach | 1600 | 0.0 | False |
| q13 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.0 | False |
| q14 | 0.0 | False | ambiguous | breach | 3200 | 0.75 | False |
| q15 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.75 | False |

### B_llm_wiki_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| q01 | 0.507 | False | breach | breach | 1558 | 1.0 | True |
| q02 | 0.2264 | False | breach | breach | 1763 | 1.0 | True |
| q03 | 0.127 | False | breach | no_breach | 1572 | 0.8333 | False |
| q04 | 0.5574 | False | breach | breach | 1561 | 1.0 | True |
| q05 | 0.386 | False | breach | breach | 1705 | 0.75 | False |
| q06 | 0.6471 | False | breach | no_breach | 1189 | 1.0 | True |
| q07 | 0.1905 | False | breach | breach | 2219 | 0.75 | False |
| q08 | 0.3478 | False | breach | breach | 427 | 1.0 | True |
| q09 | 0.5526 | False | breach | breach | 2033 | 1.0 | True |
| q10 | 0.0 | False | ambiguous | breach | 1765 | 0.0 | False |
| q11 | 0.3273 | False | breach | breach | 2218 | 0.5 | False |
| q12 | 0.52 | False | no_breach | no_breach | 2639 | 1.0 | True |
| q13 | 0.2333 | False | ambiguous | ambiguous | 1634 | 0.75 | False |
| q14 | 0.0 | False | ambiguous | breach | 1833 | 0.0 | False |
| q15 | 0.0 | False | ambiguous | ambiguous | 1687 | 1.0 | True |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| q01 | 0.5714 | False | breach | breach | 1558 | 1.0 | True |
| q02 | 0.2222 | False | breach | breach | 1763 | 1.0 | True |
| q03 | 0.1379 | False | breach | no_breach | 1572 | 0.75 | False |
| q04 | 0.5667 | False | breach | breach | 1561 | 1.0 | True |
| q05 | 0.4308 | False | breach | breach | 1705 | 1.0 | True |
| q06 | 0.6471 | False | breach | no_breach | 1189 | 1.0 | True |
| q07 | 0.3729 | False | breach | breach | 2219 | 0.75 | False |
| q08 | 0.3478 | False | breach | breach | 427 | 1.0 | True |
| q09 | 0.5263 | False | breach | breach | 1723 | 1.0 | True |
| q10 | 0.2121 | False | breach | breach | 2713 | 0.75 | False |
| q11 | 0.2381 | False | breach | breach | 2650 | 0.5 | False |
| q12 | 0.52 | False | no_breach | no_breach | 2639 | 1.0 | True |
| q13 | 0.2373 | False | ambiguous | ambiguous | 1634 | 0.75 | False |
| q14 | 0.0 | False | ambiguous | breach | 1833 | 0.5 | False |
| q15 | 0.0 | False | ambiguous | ambiguous | 1687 | 1.0 | True |
