# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

데이터셋: Risk legal LLM-Wiki independent holdout v1
질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-26

| Arm | 평균 F1 | EM 비율 | Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) |
|---|---|---|---|---|---|
| A_plain_rag | 0.0267 | 0.0 | 0.6667 | 1760.0 | 2587.1 |
| C_llm_wiki_grep_bm25 | 0.3307 | 0.0 | 0.8 | 2394.4 | 7016.6 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| h01 | 0.0 | False | breach | breach | 1600 |
| h02 | 0.0 | False | ambiguous | breach | 3200 |
| h03 | 0.0 | False | ambiguous | breach | 2400 |
| h04 | 0.4 | False | breach | breach | 800 |
| h05 | 0.0 | False | breach | breach | 2400 |
| h06 | 0.0 | False | no_breach | no_breach | 800 |
| h07 | 0.0 | False | ambiguous | no_breach | 2400 |
| h08 | 0.0 | False | ambiguous | no_breach | 1600 |
| h09 | 0.0 | False | breach | no_breach | 800 |
| h10 | 0.0 | False | no_breach | no_breach | 800 |
| h11 | 0.0 | False | ambiguous | ambiguous | 1600 |
| h12 | 0.0 | False | ambiguous | ambiguous | 2400 |
| h13 | 0.0 | False | ambiguous | ambiguous | 1600 |
| h14 | 0.0 | False | ambiguous | ambiguous | 1600 |
| h15 | 0.0 | False | ambiguous | ambiguous | 2400 |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| h01 | 0.5294 | False | breach | breach | 2320 |
| h02 | 0.4878 | False | breach | breach | 2710 |
| h03 | 0.4062 | False | breach | breach | 2625 |
| h04 | 0.2169 | False | ambiguous | breach | 2555 |
| h05 | 0.1739 | False | ambiguous | breach | 830 |
| h06 | 0.5556 | False | no_breach | no_breach | 2334 |
| h07 | 0.3077 | False | no_breach | no_breach | 2381 |
| h08 | 0.2041 | False | no_breach | no_breach | 3365 |
| h09 | 0.5185 | False | no_breach | no_breach | 2546 |
| h10 | 0.3103 | False | breach | no_breach | 827 |
| h11 | 0.3333 | False | ambiguous | ambiguous | 2884 |
| h12 | 0.2143 | False | ambiguous | ambiguous | 2506 |
| h13 | 0.2 | False | ambiguous | ambiguous | 3093 |
| h14 | 0.2564 | False | ambiguous | ambiguous | 2306 |
| h15 | 0.2456 | False | ambiguous | ambiguous | 2634 |
