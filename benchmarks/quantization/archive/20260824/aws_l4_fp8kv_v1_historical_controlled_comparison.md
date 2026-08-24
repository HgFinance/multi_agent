# AWS L4 FP8 KV v1 — Historical Controlled Comparison Record

이 문서는 과거 controlled Hybrid A/B 실행에서 생성된 비교표를 보존하기 위한 기록이다.

`AWQ+Hybrid`는 외부 성능 보존을 위한 historical baseline으로 사용한다. 이후 개선 실험은 이
파이프라인을 기준으로 수행하되, 기존 표의 단일 실행값을 새 측정값과 섞지 않는다.

## Historical comparison table

| 지표 | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid | AWQ+Hybrid + Unit/Scale |
|---|---:|---:|---:|---:|---:|---:|---:|
| Quality | 70% | 72% | 76% | 36% | 70% | 82% | **88%** |
| Relative Quality Delta vs FP8 | 기준 | +2.86% | +8.57% | -48.57% | 0.00% | +17.14% | **+25.71%** |
| Financial Arithmetic | 30% | 20% | 40% | 30% | 20% | 60% | **80%** |
| Risk Reasoning | 100% | 100% | 100% | 14.3% | 100% | 100% | 100% |
| Portfolio / Trading | 83.3% | 83.3% | 83.3% | 16.7% | 83.3% | 83.3% | **100%** |
| Accounting | 66.7% | 66.7% | 66.7% | 16.7% | 83.3% | 83.3% | 83.3% |
| Quant | 83.3% | 100% | 100% | 66.7% | 83.3% | 100% | 100% |
| Evidence | 83.3% | 100% | 100% | 66.7% | 83.3% | 100% | 100% |
| Structured Output | 40% | 40% | 40% | 40% | 40% | 40% | 40% |
| Uncertainty / Fail-Closed | 100% | 100% | 100% | 50% | 100% | 100% | 100% |
| FinQA | 75% | 65% | 75% | 55% | 80% | 80% | **85%** |
| TAT-QA | 100% | 93.3% | 80% | 80% | 86.7% | 93.3% | 93.3% |
| FinanceBench diagnostic | 58.7% | 59.0% | 38.5% | 50.1% | **62.1%** | **61.97% ± 6.52%**¹ | 50.4%² |
| FinanceBench auto proxy (diagnostic ≥0.5; not official) | 7/15 | 7/15 | 5/15 | 6/15 | 8/15 | 7/15 | 7/15 |
| Auto Mean | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | 0.8563 | **0.8841** |
| Final Gate | BASELINE | HOLD | HOLD | HOLD | HOLD | HOLD | HOLD — FinanceBench pending |

¹ `AWQ+Hybrid`의 값은 동일 old runner를 현재 L4 endpoint에서 3회 재현한 baseline 평균이다.
개별 diagnostic 값은 **58.16%, 58.25%, 69.49%**, 평균은 **61.97% ± 6.52%**(표본 표준편차)였다.

² `AWQ+Hybrid + Unit/Scale`의 historical 단일 실행값이며, 이번 3회 baseline replay의 값이 아니다.

따라서 이후 개선 실험의 `AWQ+Hybrid` baseline은 **FinanceBench diagnostic 61.97% ± 6.52%**를
기준으로 삼는다. FinanceBench diagnostic은 공식
External Overall 점수가 아니며, 15개 사례 수동 adjudication이 필요하다.

## Baseline interpretation

The historical `AWQ+Hybrid` column is the baseline target for subsequent optimization. Its current
three-run replay under the same old runner produced:

| Metric | Three-run baseline |
|---|---:|
| Internal Quality | 82.0% |
| Critical Failures | 1.0 |
| Request Errors | 3.0 |
| Financial Arithmetic | 60.0% |
| Structured Output | 40.0% |
| FinQA | 80.0% |
| TAT-QA | 93.3% |
| FinanceBench diagnostic | 61.97% ± 6.52% |
| Auto Mean | 0.8563 |

The historical `AWQ+Hybrid + Unit/Scale` values `88% / 85% / 0.8841` remain historical single-run
values. They are not replaced by, or averaged with, the three-run baseline replay. FinanceBench is
diagnostic-only and still requires manual adjudication for an official result.

## Frozen comparison rules

- Keep `AWQ+Hybrid` as the baseline pipeline definition.
- Add each improvement as a separate candidate column.
- Use the same L4 runtime, base model, adapter, frozen datasets, scorer, request order, and decoding settings.
- Run at least three paired repetitions for each candidate.
- Do not overwrite this historical record or the baseline artifacts.
- Reject a candidate if External Auto Mean, FinQA, TAT-QA, Critical Failures, or Request Errors regress beyond the agreed gate.
