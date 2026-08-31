# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

데이터셋: Risk legal LLM-Wiki independent holdout v1
질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-26

| Arm | 평균 F1 | EM 비율 | 원시 Verdict 일치율 | 행위판정 Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) | Semantic F1(LLM judge) | Semantic 정확도(LLM judge) |
|---|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.0429 | 0.0 | 0.6667 | 0.75 (8문항) | 1760.0 | 2598.2 | 0.7 | 0.4667 |
| C_llm_wiki_grep_bm25 | 0.341 | 0.0 | 0.8667 | 0.75 (8문항) | 2394.4 | 6738.0 | 0.8489 | 0.6 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| h01 | 0.0 | False | breach | breach | 1600 | 1.0 | True |
| h02 | 0.0 | False | ambiguous | breach | 3200 | 0.0 | False |
| h03 | 0.0 | False | ambiguous | breach | 2400 | 0.5 | False |
| h04 | 0.3939 | False | breach | breach | 800 | 1.0 | True |
| h05 | 0.0 | False | breach | breach | 2400 | 1.0 | True |
| h06 | 0.0 | False | no_breach | no_breach | 800 | 1.0 | True |
| h07 | 0.0 | False | ambiguous | no_breach | 2400 | 0.0 | False |
| h08 | 0.0 | False | ambiguous | no_breach | 1600 | 0.0 | False |
| h09 | 0.0 | False | breach | no_breach | 800 | 1.0 | True |
| h10 | 0.25 | False | no_breach | no_breach | 800 | 1.0 | False |
| h11 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.75 | False |
| h12 | 0.0 | False | ambiguous | ambiguous | 2400 | 0.75 | False |
| h13 | 0.0 | False | ambiguous | ambiguous | 1600 | 1.0 | True |
| h14 | 0.0 | False | ambiguous | ambiguous | 1600 | 1.0 | True |
| h15 | 0.0 | False | ambiguous | ambiguous | 2400 | 0.5 | False |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| h01 | 0.5294 | False | breach | breach | 2320 | 1.0 | True |
| h02 | 0.4286 | False | breach | breach | 2710 | 1.0 | True |
| h03 | 0.4127 | False | breach | breach | 2625 | 1.0 | True |
| h04 | 0.1975 | False | no_breach | breach | 2555 | 0.5 | False |
| h05 | 0.3922 | False | ambiguous | breach | 830 | 0.5 | False |
| h06 | 0.5556 | False | no_breach | no_breach | 2334 | 1.0 | True |
| h07 | 0.2857 | False | no_breach | no_breach | 2381 | 0.8333 | True |
| h08 | 0.2927 | False | no_breach | no_breach | 3365 | 0.75 | False |
| h09 | 0.5185 | False | no_breach | no_breach | 2546 | 1.0 | True |
| h10 | 0.2951 | False | no_breach | no_breach | 827 | 0.5 | False |
| h11 | 0.3333 | False | ambiguous | ambiguous | 2884 | 1.0 | True |
| h12 | 0.2182 | False | ambiguous | ambiguous | 2506 | 1.0 | True |
| h13 | 0.2456 | False | ambiguous | ambiguous | 3093 | 0.9 | False |
| h14 | 0.1923 | False | ambiguous | ambiguous | 2306 | 1.0 | True |
| h15 | 0.2182 | False | ambiguous | ambiguous | 2634 | 0.75 | False |
