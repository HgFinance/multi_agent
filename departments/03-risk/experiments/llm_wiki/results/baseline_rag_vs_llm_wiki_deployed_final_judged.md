# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

데이터셋: Risk legal LLM-Wiki independent holdout v1
질문 수: 15 / 부서장 지시+mandate 고정 / as_of=2026-08-26

| Arm | 평균 F1 | EM 비율 | 원시 Verdict 일치율 | 행위판정 Verdict 정확도 | 평균 context 문자수 | 평균 소요(ms) | Semantic F1(LLM judge) | Semantic 정확도(LLM judge) |
|---|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.0213 | 0.0 | 0.6667 | 0.75 (8문항) | 1760.0 | 2295.7 | 0.6667 | 0.5333 |
| C_llm_wiki_grep_bm25 | 0.342 | 0.0 | 0.6667 | 0.75 (8문항) | 3138.3 | 6610.6 | 0.9133 | 0.7333 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| h01 | 0.0 | False | breach | breach | 1600 | 1.0 | True |
| h02 | 0.0 | False | ambiguous | breach | 3200 | 0.0 | False |
| h03 | 0.0 | False | ambiguous | breach | 2400 | 0.0 | False |
| h04 | 0.3188 | False | breach | breach | 800 | 1.0 | True |
| h05 | 0.0 | False | breach | breach | 2400 | 1.0 | True |
| h06 | 0.0 | False | no_breach | no_breach | 800 | 1.0 | True |
| h07 | 0.0 | False | ambiguous | no_breach | 2400 | 0.0 | False |
| h08 | 0.0 | False | ambiguous | no_breach | 1600 | 0.0 | False |
| h09 | 0.0 | False | breach | no_breach | 800 | 1.0 | True |
| h10 | 0.0 | False | no_breach | no_breach | 800 | 1.0 | True |
| h11 | 0.0 | False | ambiguous | ambiguous | 1600 | 0.75 | False |
| h12 | 0.0 | False | ambiguous | ambiguous | 2400 | 0.75 | False |
| h13 | 0.0 | False | ambiguous | ambiguous | 1600 | 1.0 | True |
| h14 | 0.0 | False | ambiguous | ambiguous | 1600 | 1.0 | True |
| h15 | 0.0 | False | ambiguous | ambiguous | 2400 | 0.5 | False |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars | semantic_f1 | semantic_correct |
|---|---|---|---|---|---|---|---|
| h01 | 0.5294 | False | breach | breach | 3617 | 1.0 | True |
| h02 | 0.381 | False | breach | breach | 2737 | 1.0 | True |
| h03 | 0.5 | False | breach | breach | 3562 | 1.0 | True |
| h04 | 0.3855 | False | ambiguous | breach | 2716 | 0.5 | False |
| h05 | 0.1887 | False | ambiguous | breach | 1633 | 1.0 | True |
| h06 | 0.5556 | False | no_breach | no_breach | 3940 | 1.0 | True |
| h07 | 0.3478 | False | ambiguous | no_breach | 3310 | 0.75 | False |
| h08 | 0.1739 | False | no_breach | no_breach | 3484 | 0.75 | False |
| h09 | 0.35 | False | ambiguous | no_breach | 3929 | 1.0 | True |
| h10 | 0.3913 | False | breach | no_breach | 1277 | 1.0 | True |
| h11 | 0.3404 | False | ambiguous | ambiguous | 2896 | 1.0 | True |
| h12 | 0.3137 | False | ambiguous | ambiguous | 2761 | 1.0 | True |
| h13 | 0.2222 | False | ambiguous | ambiguous | 3900 | 0.8 | False |
| h14 | 0.2316 | False | ambiguous | ambiguous | 3795 | 1.0 | True |
| h15 | 0.2182 | False | ambiguous | ambiguous | 3518 | 0.9 | True |
