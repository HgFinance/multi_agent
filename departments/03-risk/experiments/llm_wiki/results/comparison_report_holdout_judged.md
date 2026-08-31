# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

데이터셋: Risk legal LLM-Wiki independent holdout v1
질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-26

| Arm | 평균 F1 | EM 비율 | Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) | Semantic F1(LLM judge) | Semantic 정확도(LLM judge) |
|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.0419 | 0.0 | 0.7333 | 1760.0 | 2066.5 | 0.6833 | 0.4667 |
| B_llm_wiki_bm25 | 0.2817 | 0.0 | 0.6 | 2373.3 | 6881.7 | 0.78 | 0.6 |
| C_llm_wiki_grep_bm25 | 0.281 | 0.0 | 0.6 | 2196.5 | 5918.3 | 0.7667 | 0.6 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| h01 | 0.0 | False | breach | breach | 1600 | 1.0 | True |
| h02 | 0.0 | False | ambiguous | breach | 3200 | 0.0 | False |
| h03 | 0.0 | False | breach | breach | 2400 | 0.5 | False |
| h04 | 0.3939 | False | breach | breach | 800 | 1.0 | True |
| h05 | 0.0 | False | breach | breach | 2400 | 1.0 | True |
| h06 | 0.0 | False | no_breach | no_breach | 800 | 1.0 | True |
| h07 | 0.0 | False | ambiguous | no_breach | 2400 | 0.0 | False |
| h08 | 0.0 | False | ambiguous | no_breach | 1600 | 0.0 | False |
| h09 | 0.0 | False | breach | no_breach | 800 | 1.0 | True |
| h10 | 0.2353 | False | no_breach | no_breach | 800 | 0.75 | False |
| h11 | 0.0 | False | ambiguous | ambiguous | 1600 | 1.0 | True |
| h12 | 0.0 | False | ambiguous | ambiguous | 2400 | 0.75 | False |
| h13 | 0.0 | False | ambiguous | ambiguous | 1600 | 1.0 | True |
| h14 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.75 | False |
| h15 | 0.0 | False | ambiguous | ambiguous | 2400 | 0.5 | False |

### B_llm_wiki_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| h01 | 0.5294 | False | breach | breach | 2749 | 1.0 | True |
| h02 | 0.4286 | False | breach | breach | 2252 | 1.0 | True |
| h03 | 0.4062 | False | breach | breach | 2393 | 1.0 | True |
| h04 | 0.1887 | False | ambiguous | breach | 825 | 0.0 | False |
| h05 | 0.2857 | False | no_breach | breach | 2485 | 1.0 | True |
| h06 | 0.2069 | False | ambiguous | no_breach | 2295 | 0.0 | False |
| h07 | 0.3 | False | no_breach | no_breach | 2234 | 1.0 | True |
| h08 | 0.1905 | False | no_breach | no_breach | 3051 | 0.75 | False |
| h09 | 0.0816 | False | no_breach | no_breach | 2377 | 0.5 | False |
| h10 | 0.3913 | False | breach | no_breach | 435 | 1.0 | True |
| h11 | 0.3265 | False | ambiguous | ambiguous | 2431 | 1.0 | True |
| h12 | 0.1707 | False | no_breach | ambiguous | 2607 | 0.75 | False |
| h13 | 0.2222 | False | ambiguous | ambiguous | 3595 | 0.9 | False |
| h14 | 0.2444 | False | ambiguous | ambiguous | 3305 | 0.9 | True |
| h15 | 0.2524 | False | no_breach | ambiguous | 2566 | 0.9 | True |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| h01 | 0.5294 | False | breach | breach | 2749 | 1.0 | True |
| h02 | 0.4286 | False | breach | breach | 2252 | 1.0 | True |
| h03 | 0.4062 | False | breach | breach | 2393 | 1.0 | True |
| h04 | 0.1887 | False | ambiguous | breach | 825 | 0.0 | False |
| h05 | 0.2326 | False | no_breach | breach | 829 | 1.0 | True |
| h06 | 0.2069 | False | ambiguous | no_breach | 2295 | 0.0 | False |
| h07 | 0.3 | False | no_breach | no_breach | 2234 | 1.0 | True |
| h08 | 0.1905 | False | no_breach | no_breach | 3051 | 0.75 | False |
| h09 | 0.0816 | False | no_breach | no_breach | 2377 | 0.5 | False |
| h10 | 0.3913 | False | breach | no_breach | 435 | 1.0 | True |
| h11 | 0.3265 | False | ambiguous | ambiguous | 2431 | 1.0 | True |
| h12 | 0.1707 | False | no_breach | ambiguous | 2607 | 0.75 | False |
| h13 | 0.2222 | False | ambiguous | ambiguous | 3595 | 1.0 | True |
| h14 | 0.2857 | False | no_breach | ambiguous | 2214 | 0.5 | False |
| h15 | 0.2545 | False | ambiguous | ambiguous | 2660 | 1.0 | True |
