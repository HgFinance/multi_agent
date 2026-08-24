# AWS L4-fp8KV-v1 — Seven-axis current status

이 문서는 기존 7축 기록과 최신 개선 후보를 구분하기 위한 상태표다. 값이 `historical`인 열은 기존 controlled A/B 실행 결과이며, `current candidate` 열은 최신 typed JSON/AST Hybrid 내부 재측정과 FinanceBench 15건 paired screening 결과다.

## 공정성 범위

- FP8과 AWQ는 동일한 NVIDIA L4, 동일 컨테이너/runtime profile, 동일 frozen dataset과 scorer로 비교된 fair-v2 base 비교다.
- 기존 AWQ+Hybrid와 AWQ+Hybrid+Unit/Scale은 같은 frozen benchmark를 사용한 과거 controlled pipeline A/B 기록이다.
- 최신 후보는 같은 AWQ endpoint와 Internal-50 v2를 사용했지만, FinanceBench는 15건만 새 경로로 교체한 paired screening이다. FinQA/TAT-QA는 기존 Hybrid baseline 결과를 보존했다.
- 따라서 마지막 열은 **유망한 현재 후보**이지, 아직 모든 7개 열을 동일 시점에 전부 재실행한 최종 공정 비교 결과가 아니다.
- External Overall은 frozen scorer의 FinanceBench manual adjudication이 필요하므로 자동 수치로 확정하지 않는다.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid baseline | AWQ+Hybrid + FinanceBench scoped RAG / typed candidate |
|---|---:|---:|---:|---:|---:|---:|---:|
| Internal Quality | 70.0% (35/50) | 72.0% (36/50) | 76.0% (38/50) | 36.0% (18/50) | 70.0% (35/50) | 82.0% (41/50) | **92.0% (46/50)** |
| Relative Quality Delta vs FP8 | baseline | +2.86% | +8.57% | -48.57% | 0.00% | +17.14% | **+31.43%** |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | 1 | **0** |
| New Critical Regression vs FP8 | 0 | 0 | 0 | 12 | 1 | 0 | **0** |
| Request Errors | 0 | 0 | 0 | 0 | 0 | 3 | **0** |
| Financial Arithmetic | 30.0% (3/10) | 20.0% (2/10) | 40.0% (4/10) | 30.0% (3/10) | 20.0% (2/10) | 60.0% (6/10) | **90.0% (9/10)** |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 14.3% (1/7) | 100.0% (7/7) | 100.0% (7/7) | **100.0% (7/7)** |
| Portfolio / Trading | 83.3% (5/6) | 83.3% (5/6) | 83.3% (5/6) | 16.7% (1/6) | 83.3% (5/6) | 83.3% (5/6) | 83.3% (5/6) |
| Accounting | 66.7% (4/6) | 66.7% (4/6) | 66.7% (4/6) | 16.7% (1/6) | 83.3% (5/6) | 83.3% (5/6) | **100.0% (6/6)** |
| Quant | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | 100.0% (6/6) | **100.0% (6/6)** |
| Evidence | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | 100.0% (6/6) | **100.0% (6/6)** |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | **60.0% (3/5)** |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 50.0% (2/4) | 100.0% (4/4) | 100.0% (4/4) | **100.0% (4/4)** |
| External Overall | N/A — manual | N/A — manual | N/A — manual | N/A — manual | N/A — manual | N/A — manual | N/A — manual |
| FinQA | 75.0% (15/20) | 65.0% (13/20) | 75.0% (15/20) | 55.0% (11/20) | 80.0% (16/20) | **80.0% (16/20)** | **80.0% (16/20, paired)** |
| TAT-QA | 100.0% (15/15) | 93.3% (14/15) | 80.0% (12/15) | 80.0% (12/15) | 86.7% (13/15) | **93.3% (14/15)** | **93.1% (14/15, paired)** |
| FinanceBench diagnostic mean | 58.74% | 58.95% | 38.53% | 50.07% | **62.11%** | 56.8% | **73.0% (15-case paired)** |
| FinanceBench official accuracy | manual | manual | manual | manual | manual | manual | **manual — pending** |
| Auto Mean (FinQA + TAT-QA) | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | **0.8563** | **0.8563 (paired)** |
| Performance | fair-v2 artifact | fair-v2 artifact | historical | historical | historical | not re-measured here | not re-measured here |
| Final Gate | BASELINE | HOLD | HOLD | HOLD | HOLD | HOLD | **HOLD — full 7-axis rerun and FinanceBench adjudication pending** |

## Latest candidate artifacts

- Internal raw: `/tmp/hgfinance-structured-typed-v1/internal50_raw.json`
- Internal score: `/tmp/hgfinance-structured-typed-v1/internal50_score.json`
- Internal provenance: `/tmp/hgfinance-structured-typed-v1/provenance.json`
- Paired External raw: `/tmp/hgfinance-financebench-ab-v5/merged_external50_raw.json`
- Paired External score: `/tmp/hgfinance-financebench-ab-v5/merged_external50_score.json`
- Paired comparison: `/tmp/hgfinance-financebench-ab-v5/comparison.md`

## Non-critical remaining failures

| ID | Current behavior | Root cause | Next non-deterministic fix |
|---|---|---|---|
| `v2-008` | `1500` instead of `150.0` | `KRW 240 billion` was serialized with the wrong magnitude before AST evaluation | Typed input-unit extraction, magnitude-preservation audit, and one bounded rewrite; reject an expression whose normalized units do not match the requested percentage |
| `v2-025` | `INVALID` instead of `VALID` | The semantic audit overturned a valid guided-choice result | Make the audit verify the supplied rule/choice evidence and retain the first valid result when the audit contradicts it without evidence |
| `v2-047` | `pnl=22000` instead of `268000` | JSON/schema was valid, but the model omitted the 80-share multiplier in the semantic calculation | Add a generic dimensional check for `quantity × price` terms and a bounded recalculation-plan rewrite; no gold answer or hardcoded fallback |
| `v2-049` | `9000000` instead of `1500000` | The model computed `limit - exposure` with a scale error and misread the action condition | Require typed `limit_amount`, `current_exposure`, `remaining_capacity` fields and audit the relation `remaining = limit - exposure` before serialization |

`v2-009` is no longer a failure in the latest run: the typed numeric contract returns `3.6` with `scale=1` and `result_unit=%`. The AST calculator was not the problem; the old failure was a ratio-versus-percentage-point interpretation error.

## Current conclusion

The latest candidate is the best **measured candidate** so far on the available evidence: Internal 92.0%, Critical Failures 0, Request Errors 0, Financial Arithmetic 90.0%, Structured Output 60.0%, and FinanceBench diagnostic 73.0%. It is not yet the final best production result because its external result is paired screening rather than a fresh full 50-case run, and FinanceBench official accuracy remains manual.

The historical `AWQ+Hybrid + Unit/Scale` value `Auto Mean=0.8841` must not be mixed with this latest candidate: it came from a different controlled A/B runner and had a FinanceBench diagnostic regression to 50.4%. A final promotion claim requires one fresh, same-profile, same-prompt, all-variant rerun.
