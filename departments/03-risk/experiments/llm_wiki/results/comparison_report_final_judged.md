# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-07

| Arm | 평균 F1 | EM 비율 | Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) | Semantic F1(LLM judge) | Semantic 정확도(LLM judge) |
|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.0601 | 0.0 | 0.5333 | 1866.7 | 3712.3 | 0.4833 | 0.3333 |
| B_llm_wiki_bm25 | 0.3747 | 0.0 | 0.8667 | 2542.1 | 3341.8 | 0.8833 | 0.6667 |
| C_llm_wiki_grep_bm25 | 0.3733 | 0.0 | 0.8667 | 2600.0 | 3500.6 | 0.8667 | 0.7333 |

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
| q08 | 0.0 | False | breach | breach | 1600 | 1.0 | True |
| q09 | 0.4727 | False | breach | breach | 1600 | 1.0 | True |
| q10 | 0.0 | False | ambiguous | breach | 1600 | 0.0 | False |
| q11 | 0.0 | False | ambiguous | breach | 1600 | 0.0 | False |
| q12 | 0.0 | False | ambiguous | no_breach | 1600 | 0.0 | False |
| q13 | 0.0 | False | ambiguous | ambiguous | 2400 | 0.0 | False |
| q14 | 0.0 | False | ambiguous | breach | 3200 | 0.5 | False |
| q15 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.75 | False |

### B_llm_wiki_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| q01 | 0.5217 | False | breach | breach | 2356 | 1.0 | True |
| q02 | 0.3158 | False | breach | breach | 2644 | 1.0 | True |
| q03 | 0.2692 | False | breach | no_breach | 2252 | 0.75 | False |
| q04 | 0.5667 | False | breach | breach | 2587 | 1.0 | True |
| q05 | 0.4923 | False | breach | breach | 2610 | 1.0 | True |
| q06 | 0.5789 | False | no_breach | no_breach | 1790 | 1.0 | True |
| q07 | 0.4928 | False | breach | breach | 3149 | 1.0 | True |
| q08 | 0.3288 | False | breach | breach | 666 | 1.0 | True |
| q09 | 0.5 | False | breach | breach | 3133 | 1.0 | True |
| q10 | 0.0968 | False | breach | breach | 2566 | 0.5 | False |
| q11 | 0.1887 | False | breach | breach | 3218 | 0.5 | False |
| q12 | 0.381 | False | no_breach | no_breach | 3408 | 0.75 | False |
| q13 | 0.275 | False | breach | ambiguous | 2434 | 0.75 | False |
| q14 | 0.6133 | False | breach | breach | 2833 | 1.0 | True |
| q15 | 0.0 | False | ambiguous | ambiguous | 2485 | 1.0 | True |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| q01 | 0.5217 | False | breach | breach | 2356 | 1.0 | True |
| q02 | 0.3158 | False | breach | breach | 2644 | 1.0 | True |
| q03 | 0.449 | False | breach | no_breach | 2252 | 1.0 | True |
| q04 | 0.5 | False | breach | breach | 2587 | 1.0 | True |
| q05 | 0.4923 | False | breach | breach | 2610 | 1.0 | True |
| q06 | 0.5641 | False | no_breach | no_breach | 1790 | 1.0 | True |
| q07 | 0.6102 | False | breach | breach | 3149 | 1.0 | True |
| q08 | 0.3288 | False | breach | breach | 666 | 1.0 | True |
| q09 | 0.5263 | False | breach | breach | 2623 | 1.0 | True |
| q10 | 0.1818 | False | breach | breach | 3714 | 0.75 | False |
| q11 | 0.3396 | False | breach | breach | 3449 | 0.5 | False |
| q12 | 0.0 | False | ambiguous | no_breach | 3408 | 0.0 | False |
| q13 | 0.1818 | False | ambiguous | ambiguous | 2434 | 0.75 | False |
| q14 | 0.5882 | False | breach | breach | 2833 | 1.0 | True |
| q15 | 0.0 | False | ambiguous | ambiguous | 2485 | 1.0 | True |
