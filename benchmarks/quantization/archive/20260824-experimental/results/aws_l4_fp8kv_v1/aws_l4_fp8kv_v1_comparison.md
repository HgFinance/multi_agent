# AWS L4-fp8KV-v1 Five-Variant Comparison

Run ID: `aws-l4-fp8kv-v1-fair-v2-20260822`

All five measured variants use the same NVIDIA L4 runtime profile. Model-only performance rows use the recorded fair-v2 prompt and identical C1/C2/C4 measurement policy. This table is not comparable to earlier autoKV or FP8-KV performance runs.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | Verdict |
|---|---:|---:|---:|---:|---:|---|
| Quality Pass Rate | 70.0% (35/50) | 72.0% (36/50) | 76.0% (38/50) | 36.0% (18/50) | 70.0% (35/50) | Reported |
| Relative Quality Delta vs FP8 | Baseline | +2.86% | +8.57% | -48.57% | 0.00% | Reported |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | Reported |
| New Critical Regression | — | 0 | 0 | 12 | 1 | Reported |
| Request Errors | 0 | 0 | 0 | 0 | 0 | Reported |
| Financial Arithmetic | 30.0% (3/10) | 20.0% (2/10) | 40.0% (4/10) | 30.0% (3/10) | 20.0% (2/10) | Reported |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 14.3% (1/7) | 100.0% (7/7) | Reported |
| Portfolio / Trading | 83.3% (5/6) | 83.3% (5/6) | 83.3% (5/6) | 16.7% (1/6) | 83.3% (5/6) | Reported |
| Accounting | 66.7% (4/6) | 66.7% (4/6) | 66.7% (4/6) | 16.7% (1/6) | 83.3% (5/6) | Reported |
| Quant | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | Reported |
| Evidence | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | Reported |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | Reported |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 50.0% (2/4) | 100.0% (4/4) | Reported |
| External Overall (current fair-v2) | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | HOLD |
| FinQA | 75.0% (15/20) | 65.0% (13/20) | 75.0% (15/20) | 55.0% (11/20) | 80.0% (16/20) | Reported |
| TAT-QA | 100.0% (15/15) | 93.3% (14/15) | 80.0% (12/15) | 80.0% (12/15) | 86.7% (13/15) | Reported |
| FinanceBench (current diagnostic; manual pending) | 58.7% | 59.0% | 38.5% | 50.1% | 62.1% | Diagnostic only |
| Auto Mean (FinQA + TAT-QA) | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | Reported |
| Historical FinanceBench manual reference (old run, not fair-v2) | 7/15 = 46.7% | 8/15 = 53.3% | N/A | N/A | N/A | Reference only |
| Historical External Overall (old run, not fair-v2) | 37/50 = 74.0% | 37/50 = 74.0% | N/A | N/A | N/A | Reference only |
| C1 Throughput | 12.46 tok/s | 22.24 tok/s | 21.00 tok/s | 22.24 tok/s (model-only) | 22.24 tok/s (model-only) | Reported |
| C2 Throughput | 24.80 tok/s | 43.98 tok/s | 41.74 tok/s | 43.98 tok/s (model-only) | 43.98 tok/s (model-only) | Reported |
| C4 Throughput | 49.06 tok/s | 85.20 tok/s | 81.56 tok/s | 85.20 tok/s (model-only) | 85.20 tok/s (model-only) | Reported |
| C1 E2E | 0.401s p50 | 0.180s p50 | 0.238s p50 | 0.874s avg pipeline | 0.764s avg pipeline | Reported |
| Model Load Memory | 15.39 GiB | 9.38 GiB | 9.51 GiB | 9.38 GiB (AWQ base) | 9.38 GiB (AWQ base) | Reported |
| KV Cache | 2.79 GiB | 8.90 GiB | 8.77 GiB | 8.90 GiB (AWQ base) | 8.90 GiB (AWQ base) | Reported |
| 8K Concurrency | N/A — not measured | N/A — not measured | N/A — not measured | N/A — not measured | N/A — not measured | N/A |
| Free VRAM | 1416 MiB | 1344 MiB | 338 MiB | 1344 MiB (AWQ base) | 1344 MiB (AWQ base) | Reported |
| Startup/Endpoint | PASS HTTP 200 (~175.5s) | PASS HTTP 200 (~52.5s) | PASS HTTP 200 (~5.9s; adapter) | PASS HTTP 200 (AWQ base) | PASS HTTP 200 (AWQ base) | Reported |
| Final Gate | BASELINE | HOLD — current External Overall/manual pending | HOLD — current External Overall/manual pending | HOLD — critical regression + External Overall/manual | HOLD — critical regression + External Overall/manual | BASELINE/HOLD |

## Interpretation

- The current fair-v2 run reports `External Overall` as unavailable until FinanceBench manual adjudication. `Auto Mean` is the automated secondary metric and must not be relabeled as External Overall.
- The historical `37/50` and FinanceBench `7/15`, `8/15` values are retained as reference rows only; they are not merged into the current L4-fp8KV-v1 run.
- AWQ+Reasoning and AWQ+RAG throughput, memory, and free-VRAM values are AWQ base model-only measurements. Their full-pipeline latency is shown separately and must not be interpreted as pure GPU throughput.
- AWQ+Finetune is valid only because the exact AWQ adapter passed save/reload verification; the NF4 adapter was not substituted.
- AWQ+RAG uses the final term-explicit glossary. The earlier body-wide-alias attempt was excluded for prompt contamination.
