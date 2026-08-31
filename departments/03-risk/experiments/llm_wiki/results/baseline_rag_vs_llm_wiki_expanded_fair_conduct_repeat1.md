# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

데이터셋: Risk legal LLM-Wiki expanded independent holdout v1 / query_kinds=conduct_assessment
질문 수: 24 / 부서장 지시+mandate 고정 / as_of=2026-08-26
판정 지표: query_kind=conduct_assessment인 문항만 Verdict에 포함하고, remedy_entitlement/rule_lookup/scope_assessment는 답변·근거 평가로만 남긴다.
공정 비교 모드: Arm A/C에 동일 생성 모델·프롬프트·JSON 스키마·최종 안전 게이트를 적용하고 검색 경로만 비교한다.

| Arm | 평균 F1 | EM 비율 | 평가대상 Verdict 일치율 | 행위판정 정확도 | 확정 커버리지 | 안전하지 않은 오답률 | 평균 context 문자수 | 평균 소요(ms) |
|---|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.269 | 0.0 | 0.6667 | 0.6667 (24문항) | 0.4167 | 0.0833 | 1950.8 | 16050.6 |
| C_llm_wiki_grep_bm25 | 0.2659 | 0.0 | 0.75 | 0.75 (24문항) | 0.4583 | 0.0417 | 2519.1 | 19268.5 |

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
| e12 | 0.3256 | False | breach | no_breach | 881 |
| e13 | 0.338 | False | ambiguous | no_breach | 2858 |
| e14 | 0.093 | False | ambiguous | no_breach | 1236 |
| e15 | 0.2222 | False | ambiguous | no_breach | 3200 |
| e16 | 0.4267 | False | ambiguous | no_breach | 2009 |
| e17 | 0.1887 | False | ambiguous | ambiguous | 3200 |
| e18 | 0.1333 | False | ambiguous | ambiguous | 475 |
| e19 | 0.1695 | False | ambiguous | ambiguous | 3200 |
| e20 | 0.1795 | False | ambiguous | ambiguous | 2486 |
| e21 | 0.3333 | False | ambiguous | ambiguous | 3002 |
| e22 | 0.127 | False | ambiguous | ambiguous | 3200 |
| e23 | 0.2273 | False | ambiguous | ambiguous | 669 |
| e24 | 0.2727 | False | ambiguous | ambiguous | 1021 |

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
