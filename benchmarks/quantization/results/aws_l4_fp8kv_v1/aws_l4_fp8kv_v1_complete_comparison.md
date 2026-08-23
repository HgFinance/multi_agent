# AWS L4-fp8KV-v1 Complete Comparison — Five Variants Plus Selective Hybrid

This is the complete presentation table for the measured five-variant run plus the latest selective hybrid External-50 result. The Hybrid column is intentionally marked `PENDING` where Internal-50 or fair model-performance evidence has not yet been rerun.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid Selective | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| Quality Pass Rate | 70.0% (35/50) | 72.0% (36/50) | 76.0% (38/50) | 36.0% (18/50) | 70.0% (35/50) | PENDING — Internal rerun | Reported/HOLD |
| Relative Quality Delta vs FP8 | Baseline | +2.86% | +8.57% | -48.57% | 0.00% | PENDING — Internal rerun | Reported/HOLD |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | PENDING — Internal rerun | Reported/HOLD |
| New Critical Regression | — | 0 | 0 | 12 | 1 | PENDING — Internal rerun | Reported/HOLD |
| Request Errors | 0 | 0 | 0 | 0 | 0 | 0 (External-50) | Reported/PENDING |
| Financial Arithmetic | 30.0% (3/10) | 20.0% (2/10) | 40.0% (4/10) | 30.0% (3/10) | 20.0% (2/10) | PENDING — Internal rerun | Reported/HOLD |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 14.3% (1/7) | 100.0% (7/7) | PENDING — Internal rerun | Reported/HOLD |
| Portfolio / Trading | 83.3% (5/6) | 83.3% (5/6) | 83.3% (5/6) | 16.7% (1/6) | 83.3% (5/6) | PENDING — Internal rerun | Reported/HOLD |
| Accounting | 66.7% (4/6) | 66.7% (4/6) | 66.7% (4/6) | 16.7% (1/6) | 83.3% (5/6) | PENDING — Internal rerun | Reported/HOLD |
| Quant | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | PENDING — Internal rerun | Reported/HOLD |
| Evidence | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | PENDING — Internal rerun | Reported/HOLD |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | PENDING — Internal rerun | Reported/HOLD |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 50.0% (2/4) | 100.0% (4/4) | PENDING — Internal rerun | Reported/HOLD |
| External Overall (current fair-v2) | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | PENDING — FinanceBench manual | HOLD |
| FinQA | 75.0% (15/20) | 65.0% (13/20) | 75.0% (15/20) | 55.0% (11/20) | 80.0% (16/20) | 80.0% (16/20) | Reported |
| TAT-QA | 100.0% (15/15) | 93.3% (14/15) | 80.0% (12/15) | 80.0% (12/15) | 86.7% (13/15) | 93.3% (14/15) | Reported |
| FinanceBench (current diagnostic; manual pending) | 58.7% | 59.0% | 38.5% | 50.1% | 62.1% | 57.9% | Diagnostic only |
| Auto Mean (FinQA + TAT-QA) | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | 0.8563 | Reported/HOLD |
| Historical FinanceBench manual reference (old run, not fair-v2) | 7/15 = 46.7% | 8/15 = 53.3% | N/A | N/A | N/A | N/A | Reference only |
| Historical External Overall (old run, not fair-v2) | 37/50 = 74.0% | 37/50 = 74.0% | N/A | N/A | N/A | N/A | Reference only |
| C1 Throughput | 12.46 tok/s | 22.24 tok/s | 21.00 tok/s | 22.24 tok/s (model-only) | 22.24 tok/s (model-only) | PENDING — fair model-only rerun | HOLD |
| C2 Throughput | 24.80 tok/s | 43.98 tok/s | 41.74 tok/s | 43.98 tok/s (model-only) | 43.98 tok/s (model-only) | PENDING — fair model-only rerun | HOLD |
| C4 Throughput | 49.06 tok/s | 85.20 tok/s | 81.56 tok/s | 85.20 tok/s (model-only) | 85.20 tok/s (model-only) | PENDING — fair model-only rerun | HOLD |
| C1 E2E | 0.401s p50 | 0.180s p50 | 0.238s p50 | 0.874s avg pipeline | 0.764s avg pipeline | 7.674s avg full pipeline* | Separate latency class |
| Model Load Memory | 15.39 GiB | 9.38 GiB | 9.51 GiB | 9.38 GiB (AWQ base) | 9.38 GiB (AWQ base) | PENDING — fair performance rerun | HOLD |
| KV Cache | 2.79 GiB | 8.90 GiB | 8.77 GiB | 8.90 GiB (AWQ base) | 8.90 GiB (AWQ base) | PENDING — fair performance rerun | HOLD |
| 8K Concurrency | N/A — not measured | N/A — not measured | N/A — not measured | N/A — not measured | N/A — not measured | PENDING — fair performance rerun | N/A/HOLD |
| Free VRAM | 1416 MiB | 1344 MiB | 338 MiB | 1344 MiB (AWQ base) | 1344 MiB (AWQ base) | PENDING — fair performance rerun | HOLD |
| Startup/Endpoint | PASS HTTP 200 (~175.5s) | PASS HTTP 200 (~52.5s) | PASS HTTP 200 (~5.9s; adapter) | PASS HTTP 200 (AWQ base) | PASS HTTP 200 (AWQ base) | PASS HTTP 200 | Reported |
| Final Gate | BASELINE | HOLD — External Overall/manual | HOLD — External Overall/manual | HOLD — critical regression + External Overall/manual | HOLD — critical regression + External Overall/manual | HOLD — Internal/performance/manual pending | BASELINE/HOLD |

\* Hybrid `7.674s` is the External-50 selective pipeline average, not a fair C1 model-only latency measurement. It must not be compared directly with the model-only C1 p50 rows.

## What is and is not final

- The five base columns are complete for the committed `aws-l4-fp8kv-v1-fair-v2-20260822` quality/performance artifacts.
- The Hybrid column has a real paired External-50 result: FinQA `16/20`, TAT-QA `14/15`, Auto Mean `0.8563`, and FinanceBench diagnostic `0.5793`; its External gate is positive relative to its paired fresh AWQ baseline.
- Hybrid is not a final promoted model yet. Internal-50 v2, fair model-only C1/C2/C4, and FinanceBench manual adjudication remain required.
- The historical `37/50` and FinanceBench `7/15`, `8/15` values are retained as reference rows only; they are not merged into the current fair-v2 result.
- `Auto Mean` is the automated secondary metric. It must not be relabeled as External Overall.
