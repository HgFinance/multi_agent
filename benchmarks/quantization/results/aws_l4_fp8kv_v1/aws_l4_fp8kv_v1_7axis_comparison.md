# AWS L4-fp8KV-v1 — Corrected seven-axis comparison

This table uses the original controlled Hybrid A/B run. The sixth column is the existing `AWQ+Hybrid` baseline with only the deterministic Structured Output answer fallback removed. The seventh column adds only the generic Unit/Scale normalization treatment to that same Hybrid path. It does not add a new metadata protocol, new external router, or new answer fallback.

The five base columns are the existing fair-v2 artifacts. The sixth and seventh columns use the same frozen datasets/scorers and the same L4-fp8KV-v1 runtime profile. FinanceBench remains diagnostic-only because the frozen scorer requires manual adjudication.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid | AWQ+Hybrid + Unit/Scale | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Internal Quality | 70.0% (35/50) | 72.0% (36/50) | 76.0% (38/50) | 36.0% (18/50) | 70.0% (35/50) | 82.0% (41/50) | **88.0% (44/50)** | Improved |
| Relative Quality Delta vs FP8 | baseline | +2.86% | +8.57% | -48.57% | 0.00% | +17.14% | **+25.71%** | Internal pass |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | 1 | 1 | No increase |
| New Critical Regression vs FP8 | 0 | 0 | 0 | 12 | 1 | 0 | 0 | Pass |
| Request Errors | 0 | 0 | 0 | 0 | 0 | 3 | 3 | Unchanged |
| Financial Arithmetic | 30.0% (3/10) | 20.0% (2/10) | 40.0% (4/10) | 30.0% (3/10) | 20.0% (2/10) | 60.0% (6/10) | **80.0% (8/10)** | Improved |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 14.3% (1/7) | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | Same |
| Portfolio / Trading | 83.3% (5/6) | 83.3% (5/6) | 83.3% (5/6) | 16.7% (1/6) | 83.3% (5/6) | 83.3% (5/6) | 100.0% (6/6) | Improved |
| Accounting | 66.7% (4/6) | 66.7% (4/6) | 66.7% (4/6) | 16.7% (1/6) | 83.3% (5/6) | 83.3% (5/6) | 83.3% (5/6) | Same |
| Quant | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | Same |
| Evidence | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | Same |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | Fallback removed; unchanged |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 50.0% (2/4) | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | Same |
| External Overall | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | Manual adjudication required |
| FinQA | 75.0% (15/20) | 65.0% (13/20) | 75.0% (15/20) | 55.0% (11/20) | 80.0% (16/20) | 80.0% (16/20) | **85.0% (17/20)** | Improved |
| TAT-QA | 100.0% (15/15) | 93.3% (14/15) | 80.0% (12/15) | 80.0% (12/15) | 86.7% (13/15) | 93.3% (14/15) | 93.3% (14/15) | Same |
| FinanceBench Diagnostic Mean | 58.74% | 58.95% | 38.53% | 50.07% | **62.11%** | 56.8% | 50.4% | Diagnostic regression |
| FinanceBench auto proxy (diagnostic ≥ 0.5; not official) | 7/15 | 7/15 | 5/15 | 6/15 | 8/15 | 7/15 | 7/15 | No official score |
| Auto Mean (FinQA + TAT-QA) | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | 0.8563 | **0.8841** | Best automated candidate |
| Performance | Existing fair-v2 artifact | Existing fair-v2 artifact | Existing fair-v2 artifact | Existing fair-v2 artifact | Existing fair-v2 artifact | Same Hybrid profile | Same Hybrid profile | Separate performance gate |
| Final Gate | BASELINE | HOLD | HOLD | HOLD | HOLD | HOLD | **HOLD — FinanceBench manual/diagnostic pending** | Candidate, not promoted |

## Correct interpretation

Yes: among the controlled A/B stages, Unit/Scale is the best candidate by the automated metrics. It raises Internal Quality from `82%` to `88%`, Financial Arithmetic from `60%` to `80%`, FinQA from `80%` to `85%`, and Auto Mean from `0.8563` to `0.8841`, while preserving TAT-QA, Critical Failures, and Request Errors.

It is not yet a final production pass because FinanceBench diagnostic mean falls from `56.8%` to `50.4%`. The frozen scorer explicitly marks FinanceBench `manual_required`; therefore the `7/15` proxy is not an official accuracy result. The correct next step is manual adjudication of the 15 FinanceBench cases, especially the unit/formula regressions, before promotion.

## Why the later fresh rerun differed

The later fresh rerun was not the same sixth-axis baseline. It used a different runner path and changed the external routing/prompt behavior; its `65%` Hybrid FinQA result therefore cannot replace the controlled A/B baseline's `80%`. That rerun is retained only as diagnostic evidence and is excluded from this corrected table.

## Artifacts

- Original controlled A/B table: `benchmarks/quantization/results/aws_l4_fp8kv_v1/AWQ+HybridAB/hybrid_ab_comparison.md`
- Original A/B raw/score evidence: `/tmp/hgfinance-hybrid-e2e-v1/` and `/tmp/hgfinance-hybrid-ab/`
- Frozen Internal-50 v2 SHA256: `ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978`
