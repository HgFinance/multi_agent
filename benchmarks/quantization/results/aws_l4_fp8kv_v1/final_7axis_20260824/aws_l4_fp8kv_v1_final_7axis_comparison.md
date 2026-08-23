# AWS L4-fp8KV-v1 — final seven-variant comparison (2026-08-24)

이 표는 동일 NVIDIA L4와 동일 `L4-fp8KV-v1` runtime에서 7개 변형을 순차 재실행한 결과다. FP8/AWQ는 순수 양자화 비교이고, 나머지 열은 각 pipeline의 adapter·glossary·rewrite·guided output 처리를 포함한다.

FinanceBench 15건은 frozen scorer가 `manual_required`를 반환하므로 diagnostic만 기록하고 공식 External Overall은 확정하지 않았다. 성능 C1/C2/C4도 이번 quality rerun에서는 측정하지 않았다.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid | AWQ+Hybrid+Unit/Scale |
|---|---:|---:|---:|---:|---:|---:|---:|
| Internal Quality | 70.0% (35/50) | 72.0% (36/50) | 74.0% (37/50) | 70.0% (35/50) | 68.0% (34/50) | 86.0% (43/50) | 92.0% (46/50) |
| Relative Quality Delta vs FP8 | baseline | +2.86% | +5.71% | +0.00% | -2.86% | +22.86% | +31.43% |
| Critical Failures | 1 | 1 | 2 | 5 | 2 | 1 | 0 |
| New Critical Regression | 0 | 0 | 1 | 4 | 1 | 0 | 0 |
| Request Errors | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Financial Arithmetic | 30.0% (3/10) | 20.0% (2/10) | 30.0% (3/10) | 60.0% (6/10) | 20.0% (2/10) | 80.0% (8/10) | 80.0% (8/10) |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 85.7% (6/7) | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) |
| Portfolio / Trading | 83.3% (5/6) | 83.3% (5/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | 66.7% (4/6) | 83.3% (5/6) |
| Accounting | 66.7% (4/6) | 66.7% (4/6) | 83.3% (5/6) | 50.0% (3/6) | 66.7% (4/6) | 100.0% (6/6) | 100.0% (6/6) |
| Quant | 83.3% (5/6) | 100.0% (6/6) | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) |
| Evidence | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 83.3% (5/6) | 66.7% (4/6) | 100.0% (6/6) | 100.0% (6/6) |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 80.0% (4/5) |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 50.0% (2/4) | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) |
| External Overall | N/A — manual required | N/A — manual required | N/A — manual required | N/A — manual required | N/A — manual required | N/A — manual required | N/A — manual required |
| FinQA | 75.0% | 65.0% | 75.0% | 75.0% | 65.0% | 65.0% | 70.0% |
| TAT-QA | 100.0% | 93.3% | 80.0% | 93.3% | 93.3% | 93.3% | 93.3% |
| FinanceBench diagnostic | 58.7% | 59.0% | 39.2% | 67.3% | 64.9% | 55.5% | 62.2% |
| FinanceBench official accuracy | MANUAL REQUIRED | MANUAL REQUIRED | MANUAL REQUIRED | MANUAL REQUIRED | MANUAL REQUIRED | MANUAL REQUIRED | MANUAL REQUIRED |
| Auto Mean | 0.8556 | 0.7709 | 0.7612 | 0.8270 | 0.7709 | 0.7706 | 0.7992 |
| Performance | Not measured in this quality rerun | Not measured in this quality rerun | Not measured in this quality rerun | Not measured in this quality rerun | Not measured in this quality rerun | Not measured in this quality rerun | Not measured in this quality rerun |
| Final Gate | BASELINE | HOLD — manual/full performance gate | HOLD — manual/full performance gate | HOLD — manual/full performance gate | HOLD — manual/full performance gate | HOLD — manual/full performance gate | HOLD — manual/full performance gate |

## Critical failure IDs

| Variant | Critical IDs |
|---|---|
| FP8 | v2-046 |
| AWQ | v2-046 |
| AWQ+Finetune | v2-020, v2-046 |
| AWQ+Reasoning | v2-013, v2-015, v2-042, v2-043, v2-046 |
| AWQ+RAG | v2-038, v2-046 |
| AWQ+Hybrid | v2-046 |
| AWQ+Hybrid+Unit/Scale | None |

## Run artifacts

- JSON: `aws_l4_fp8kv_v1_final_7axis_comparison.json`
- Per-variant raw/score/provenance: `final_7axis_20260824/<variant>/`
- Rollback baseline: `../aws_l4_fp8kv_v1_rollback_baseline_20260824.md`
- All frozen dataset hashes: PASS
- No secrets or model weights copied into Git
