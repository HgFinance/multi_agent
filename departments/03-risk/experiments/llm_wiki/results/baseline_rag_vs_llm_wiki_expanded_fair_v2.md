# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

데이터셋: Risk legal LLM-Wiki expanded independent holdout v1
질문 수: 36 / 부서장 지시+mandate 고정 / as_of=2026-08-26
판정 지표: query_kind=conduct_assessment인 문항만 Verdict에 포함하고, remedy_entitlement/rule_lookup/scope_assessment는 답변·근거 평가로만 남긴다.
공정 비교 모드: Arm A/C에 동일 생성 모델·프롬프트·JSON 스키마·최종 안전 게이트를 적용하고 검색 경로만 비교한다.

| Arm | 평균 F1 | EM 비율 | 평가대상 Verdict 일치율 | 행위판정 정확도 | 확정 커버리지 | 안전하지 않은 오답률 | 평균 context 문자수 | 평균 소요(ms) |
|---|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.2694 | 0.0 | 0.6667 | 0.6667 (24문항) | 0.375 | 0.0417 | 1847.3 | 15827.8 |
| C_llm_wiki_grep_bm25 | 0.2467 | 0.0 | 0.75 | 0.75 (24문항) | 0.4583 | 0.0417 | 2585.0 | 16972.2 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| e01 | 0.5397 | False | breach | breach | 475 |
| e02 | 0.4583 | False | breach | breach | 3200 |
| e03 | 0.35 | False | breach | breach | 881 |
| e04 | 0.2424 | False | breach | breach | 881 |
| e05 | 0.1754 | False | ambiguous | breach | 3200 |
| e06 | 0.2963 | False | breach | breach | 976 |
| e07 | 0.3019 | False | breach | breach | 3200 |
| e08 | 0.3284 | False | breach | breach | 3200 |
| e09 | 0.127 | False | ambiguous | no_breach | 881 |
| e10 | 0.2295 | False | breach | no_breach | 1859 |
| e11 | 0.3704 | False | no_breach | no_breach | 630 |
| e12 | 0.2927 | False | ambiguous | no_breach | 881 |
| e13 | 0.338 | False | ambiguous | no_breach | 2858 |
| e14 | 0.093 | False | ambiguous | no_breach | 1236 |
| e15 | 0.25 | False | ambiguous | no_breach | 3200 |
| e16 | 0.4267 | False | ambiguous | no_breach | 2009 |
| e17 | 0.2182 | False | ambiguous | ambiguous | 3200 |
| e18 | 0.1333 | False | ambiguous | ambiguous | 475 |
| e19 | 0.1967 | False | ambiguous | ambiguous | 3200 |
| e20 | 0.1795 | False | ambiguous | ambiguous | 2486 |
| e21 | 0.3333 | False | ambiguous | ambiguous | 3002 |
| e22 | 0.127 | False | ambiguous | ambiguous | 3200 |
| e23 | 0.2273 | False | ambiguous | ambiguous | 669 |
| e24 | 0.2727 | False | ambiguous | ambiguous | 1021 |
| e25 | 0.2989 | False | ambiguous | not_scored | 1425 |
| e26 | 0.3562 | False | ambiguous | not_scored | 1425 |
| e27 | 0.5 | False | ambiguous | not_scored | 1425 |
| e28 | 0.5263 | False | ambiguous | not_scored | 630 |
| e29 | 0.4 | False | ambiguous | not_scored | 881 |
| e30 | 0.1923 | False | ambiguous | not_scored | 2009 |
| e31 | 0.0 | False | ambiguous | not_scored | 1575 |
| e32 | 0.0952 | False | ambiguous | not_scored | 669 |
| e33 | 0.2326 | False | ambiguous | not_scored | 330 |
| e34 | 0.062 | False | ambiguous | not_scored | 3200 |
| e35 | 0.3182 | False | ambiguous | not_scored | 3200 |
| e36 | 0.2105 | False | ambiguous | not_scored | 2915 |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| e01 | 0.5 | False | breach | breach | 3200 |
| e02 | 0.4706 | False | breach | breach | 3200 |
| e03 | 0.4 | False | breach | breach | 3200 |
| e04 | 0.2258 | False | breach | breach | 3200 |
| e05 | 0.3636 | False | breach | breach | 2785 |
| e06 | 0.2162 | False | breach | breach | 2785 |
| e07 | 0.339 | False | breach | breach | 1516 |
| e08 | 0.2264 | False | breach | breach | 1640 |
| e09 | 0.1176 | False | no_breach | no_breach | 3200 |
| e10 | 0.2692 | False | breach | no_breach | 2785 |
| e11 | 0.3774 | False | no_breach | no_breach | 3200 |
| e12 | 0.1905 | False | ambiguous | no_breach | 2761 |
| e13 | 0.2813 | False | ambiguous | no_breach | 1375 |
| e14 | 0.1786 | False | ambiguous | no_breach | 2785 |
| e15 | 0.2105 | False | ambiguous | no_breach | 1340 |
| e16 | 0.3896 | False | ambiguous | no_breach | 3200 |
| e17 | 0.1754 | False | ambiguous | ambiguous | 1290 |
| e18 | 0.129 | False | ambiguous | ambiguous | 2455 |
| e19 | 0.2143 | False | ambiguous | ambiguous | 2785 |
| e20 | 0.2195 | False | ambiguous | ambiguous | 2894 |
| e21 | 0.3188 | False | ambiguous | ambiguous | 3200 |
| e22 | 0.12 | False | ambiguous | ambiguous | 1290 |
| e23 | 0.1852 | False | ambiguous | ambiguous | 1633 |
| e24 | 0.2642 | False | ambiguous | ambiguous | 2740 |
| e25 | 0.3596 | False | ambiguous | not_scored | 3200 |
| e26 | 0.1091 | False | ambiguous | not_scored | 3200 |
| e27 | 0.2381 | False | ambiguous | not_scored | 2740 |
| e28 | 0.4878 | False | ambiguous | not_scored | 3200 |
| e29 | 0.1509 | False | ambiguous | not_scored | 2761 |
| e30 | 0.2083 | False | ambiguous | not_scored | 3200 |
| e31 | 0.0 | False | ambiguous | not_scored | 3199 |
| e32 | 0.1569 | False | ambiguous | not_scored | 1638 |
| e33 | 0.2778 | False | ambiguous | not_scored | 2920 |
| e34 | 0.1356 | False | ambiguous | not_scored | 2785 |
| e35 | 0.2381 | False | ambiguous | not_scored | 838 |
| e36 | 0.137 | False | ambiguous | not_scored | 2920 |
