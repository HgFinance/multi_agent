# AWQ Hybrid vs BOK800 LLM-Wiki paired replay

## 결론

현재 frozen 평가셋과 4K endpoint에서는 BOK800 LLM-Wiki fallback의 품질 개선이
확인되지 않았다. 세 번 모두 후보 문서는 검색됐지만 relevance grader를 통과한 문서가
0건이어서 실제 답변 프롬프트에 Wiki context가 들어가지 않았다. 검색은 평균 1.765초의
추가 지연만 만들었으므로 이 구성은 현재 Hybrid 운영 후보에 합치지 않고 `REJECT`한다.

이 결론은 LLM-Wiki 일반론이 아니라, 영어 중심의 Internal/FinQA/TAT-QA/FinanceBench
frozen set에 한국은행 경제금융용어 800선 Wiki를 붙인 이번 구성에 한정한다.

## 실행 계약

- 실행일: 2026-08-25 UTC
- endpoint: `http://127.0.0.1:8000/v1/chat/completions`
- 현재 runtime: `max_model_len=4096`
- base: `qwen2.5-14b-instruct-awq`
- arithmetic adapter: `hgfinance-awq-arithmetic-2epoch`
- Hybrid stage: `selective_unit_scale`
- temperature: `0`
- 반복: baseline 3회 + Wiki candidate 3회, paired 순서
- Internal SHA256: `ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978`
- External SHA256: `197f0828fee37a8a0ca7551304efa722b005a3d26379a2cc613ece36b315956f`
- Wiki: 한국은행 800선 source-preserving entity 789개
- Wiki SHA256: `b5490001c884bd3d0dafe70a86fdee22166f739d305a9d051d8bf21ef629da65`
- answer-key fallback: 미사용

Wiki candidate는 기존 glossary 정확 매칭을 먼저 수행하고, miss일 때만 다음 순서로
동작한다.

```text
Qwen 한국어 검색어 계획
→ 789개 Wiki BM25 top-1
→ 연관검색어 링크 탐색(최대 3페이지)
→ Qwen relevance grade
→ 관련 문서만 최대 페이지당 600자 주입
```

## 3회 평균 품질

| 지표 | Historical Upgrade v1 참고값 | 현재 Hybrid 재현 | Hybrid + LLM-Wiki | Wiki 변화 |
|---|---:|---:|---:|---:|
| Internal Quality | 90.0% | 88.0% | 88.0% | 0.0%p |
| Critical Failures | 0 | 1.0 | 1.0 | 0.0 |
| Request Errors | 2 | 0.0 | 0.0 | 0.0 |
| Financial Arithmetic | 80.0% | 83.3% | 80.0% | -3.3%p |
| Risk Reasoning | 100.0% | 100.0% | 100.0% | 0.0%p |
| Portfolio / Trading | 100.0% | 77.8% | 83.3% | +5.6%p |
| Accounting | 83.3% | 100.0% | 100.0% | 0.0%p |
| Quant | 100.0% | 100.0% | 100.0% | 0.0%p |
| Evidence | 100.0% | 100.0% | 100.0% | 0.0%p |
| Structured Output | 60.0% | 40.0% | 40.0% | 0.0%p |
| Uncertainty / Fail-Closed | 100.0% | 100.0% | 100.0% | 0.0%p |
| FinQA | 83.3% 평균 | 70.0% | 70.0% | 0.0%p |
| TAT-QA | 93.3% | 93.3% | 93.3% | 0.0%p |
| FinanceBench diagnostic | 58.19% 평균 | 54.40% | 54.42% | +0.02%p |
| Auto Mean | 0.8754 평균 | 0.7992 | 0.7992 | 0.0000 |

Wiki가 세 번 모두 0건 주입됐으므로 Financial Arithmetic, Portfolio 및
FinanceBench의 작은 차이는 Wiki 지식 효과로 귀속할 수 없다. 같은 answer prompt에서도
vLLM 반복 실행 결과가 일부 달랐고, FinanceBench는 `manual_required` diagnostic이다.

## 검색 성능

| 검색 지표 | 3회 평균 |
|---|---:|
| Wiki fallback 대상 | 17문항/run |
| BM25 후보 검색 | 7/17 = 41.2% |
| 연관검색어 링크까지 탐색한 후보 | 2/17 = 11.8% |
| relevance 승인·실제 주입 | 0/17 = 0.0% |
| 평균 BM25 시간 | 0.356 ms |
| 평균 query planner 시간 | 0.835 s |
| 후보당 평균 relevance grader 시간 | 2.259 s |
| fallback 대상당 평균 총 검색 overhead | 1.765 s |
| 평균 주입 context | 0자 |

대표 거절 사례는 다음과 같다.

| 질문 | 검색어 | Wiki 후보 | 거절 이유 |
|---|---|---|---|
| reconciliation 분류 | 정산 | 프로젝트 한강 → CBDC → 예금토큰 | reconciliation과 무관 |
| realized PnL 계산 | 실현 손익 KRW | 은행경영공시제도 | 손익 계산식과 무관 |
| JPM gross margin | JP모건 총마진 | 경제 서프라이즈 지수 | gross margin과 무관 |
| Amazon 당기순이익 조회 | 아마존 당기순이익 | 주당순이익(EPS) | 회사별 수치 조회에 도움 없음 |
| AMCOR/Ulta 인수 내역 | 인수합병 | M&A | 일반 정의일 뿐 회사별 사실이 없음 |
| Kenvue 분할 현금수익 | 분할 현금 수익 | 지로 → 입금이체 → 출금이체 | 거래 수단 정의로 답을 도출할 수 없음 |

## 조기중단 판정

2회 누적 시점에 모든 higher-is-better 품질지표가 baseline보다 낮지는 않았다. 동률
지표가 다수였고 Portfolio 및 FinanceBench diagnostic이 소폭 높아, 사전에 정한 규칙에
따라 폐기하지 않고 3회차까지 실행했다.

최종 3회 결과에서는 품질 개선이 없고 실제 Wiki 주입도 0건이며 latency만 증가했다.
따라서 2회 조기중단 규칙과 별개로 최종 verdict는 `REJECT`다.

## Historical 90% 재현 여부

사용자가 제시한 Upgrade v1의 `90% / Structured 60% / FinQA 83.3% / Auto Mean
0.8754`는 현재 코드와 4K endpoint에서 재현되지 않았다. 현재 3회 baseline은
`88% / 40% / 70% / 0.7992`다.

저장소 Git 이력의 historical controlled report도 `AWQ+Hybrid + Unit/Scale`을
`88% / FinQA 85% / Auto Mean 0.8841`의 단일 historical run으로 기록하고 있으며,
90% Upgrade v1의 원시 3회 artifact와 정확한 실행 명령은 현재 추적 파일에서 확인되지
않았다. 따라서 이번 현재 측정값과 90% 값을 같은 재현 series로 평균내거나 덮어쓰지
않는다.

## 산출물

- `summary.json`: 3회 평균, delta, retrieval 통계, per-run 경로
- `baseline_run1..3/`: 현재 Hybrid raw/score/provenance
- `wiki_run1..3/`: LLM-Wiki candidate raw/score/provenance
- `pilot_wiki_run1/`: relevance gate 전 BM25 noise 진단; 최종 평균 제외
- `planner_gate_wiki_run1/`: 과도하게 보수적인 planner gate 진단; 최종 평균 제외

한국어 용어 질의에서 LLM-Wiki의 효과를 검증하려면 현재 영어 문서 QA 세트가 아니라
BOK800의 표제어·동의어·연관검색어·복합질문을 포함한 별도 한국어 retrieval golden set을
동결하고 Relevant Hit@1/3과 answer accuracy를 측정해야 한다.
