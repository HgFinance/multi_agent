# grep/BM25 seed 튜닝 — 무-LLM 리콜 스윕

질문 수: 15 (gold_page_ids 기준, LLM 호출 없이 결정론적으로 계산)

| keyword_tier | top_k | tmax | B_recall | B_avg_ctx | C_recall | C_avg_ctx |
|---|---|---|---|---|---|---|
| False | 1 | 3 | 0.867 | 1720.2 | 0.867 | 1699.9 |
| True | 1 | 3 | 0.867 | 1720.2 | 0.933 | 1791.5 |
| True | 2 | 3 | 0.867 | 1890.0 | 0.933 | 1792.4 |
| True | 1 | 2 | 0.867 | 1198.1 | 0.933 | 1277.8 |
| True | 1 | 4 | 0.867 | 2235.2 | 0.933 | 2302.0 |
| True | 1 | 5 | 0.867 | 2652.7 | 0.933 | 2763.9 |

결론: keyword_tier 추가만 C_recall을 0.867 -> 0.933(제외: q15)로 올렸다. top_k(1->2)나 tmax(3->5)를 키우는 건 context만 늘리고 리콜은 그대로였다 (top_k는 리콜 무변화, tmax는 3에서 이미 평탄화). 채택: top_k=1, tmax=3 유지, grep_seed+keyword_seed 합집합만 추가(arms.py).