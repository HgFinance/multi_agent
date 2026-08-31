# LLM-Wiki 부분 도입 실험 — Arm A/B/C 비교

데이터셋: Risk legal LLM-Wiki independent holdout v2 / query_kinds=conduct_assessment
질문 수: 4 / 부서장 지시+mandate 고정 / as_of=2026-08-26
판정 지표: query_kind=conduct_assessment인 문항만 Verdict에 포함하고, remedy_entitlement/rule_lookup/scope_assessment는 답변·근거 평가로만 남긴다.
공정 비교 모드: Arm A/C에 동일 생성 모델·프롬프트·JSON 스키마·최종 안전 게이트를 적용하고 검색 경로만 비교한다.

| Arm | 평균 F1 | EM 비율 | 평가대상 Verdict 일치율 | 행위판정 정확도 | 확정 커버리지 | 안전하지 않은 오답률 | 평균 context 문자수 | 평균 소요(ms) |
|---|---|---|---|---|---|---|---|---|
| A_plain_rag | 0.3405 | 0.0 | 0.75 | 0.75 (4문항) | 0.5 | 0.0 | 2331.0 | 18054.4 |
| C_llm_wiki_grep_bm25 | 0.3966 | 0.0 | 0.75 | 0.75 (4문항) | 0.5 | 0.0 | 3090.2 | 16701.0 |

## 문항별 상세

### A_plain_rag

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| h01 | 0.5294 | False | breach | breach | 3200 |
| h02 | 0.3913 | False | breach | breach | 881 |
| h03 | 0.2154 | False | ambiguous | breach | 3200 |
| h15 | 0.2258 | False | ambiguous | ambiguous | 2043 |

### C_llm_wiki_grep_bm25

| id | F1 | EM | verdict | gold_verdict | context_chars |
|---|---|---|---|---|---|
| h01 | 0.5075 | False | breach | breach | 3200 |
| h02 | 0.381 | False | breach | breach | 2761 |
| h03 | 0.4211 | False | ambiguous | breach | 3200 |
| h15 | 0.2769 | False | ambiguous | ambiguous | 3200 |
