# AWS L4-fp8KV-v1 Five-Variant Comparison

Run ID: `aws-l4-fp8kv-v1-fair-v2-20260822`

All measured variants use the same NVIDIA L4 runtime profile. Model-only performance rows use the recorded fair-v2 prompt and identical C1/C2/C4 measurement policy. This table is not comparable to earlier autoKV or FP8-KV performance runs.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | Verdict |
|---|---|---|---|---|---|---|
| Internal Quality | 70.00% | 72.00% | 76.00% | 36.00% | 70.00% | Reported |
| Relative Quality Delta vs FP8 | Baseline | 2.86% | 8.57% | -48.57% | 0.00% | Reported |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | Reported |
| New Critical Regression | — | 0 | 0 | 12 | 1 | Reported |
| Request Errors | 0 | 0 | 0 | 0 | 0 | Reported |
| Financial Arithmetic | 30.00% | 20.00% | 40.00% | 30.00% | 20.00% | Reported |
| Structured Output | 40.00% | 40.00% | 40.00% | 40.00% | 40.00% | Reported |
| External Overall | N/A (FinanceBench manual) | N/A (FinanceBench manual) | N/A (FinanceBench manual) | N/A (FinanceBench manual) | N/A (FinanceBench manual) | HOLD |
| FinQA | 75.00% | 65.00% | 75.00% | 55.00% | 80.00% | Reported |
| TAT-QA | 100.00% | 93.33% | 80.00% | 80.00% | 86.67% | Reported |
| FinanceBench | Manual required (diag 58.7%) | Manual required (diag 59.0%) | Manual required (diag 38.5%) | Manual required (diag 50.1%) | Manual required (diag 62.1%) | Reported |
| Auto Mean | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | Reported |
| C1 Throughput | 12.46 tok/s | 22.24 tok/s | 21.00 tok/s | 22.24 tok/s (model-only) | 22.24 tok/s (model-only) | Reported |
| C2 Throughput | 24.80 tok/s | 43.98 tok/s | 41.74 tok/s | 43.98 tok/s (model-only) | 43.98 tok/s (model-only) | Reported |
| C4 Throughput | 49.06 tok/s | 85.20 tok/s | 81.56 tok/s | 85.20 tok/s (model-only) | 85.20 tok/s (model-only) | Reported |
| C1 E2E | 0.401s p50 | 0.180s p50 | 0.238s p50 | 0.874s avg | 0.764s avg | Reported |
| Model Load Memory | 15.39 GiB | 9.38 GiB | 9.51 GiB | 9.38 GiB | 9.38 GiB | Reported |
| KV Cache | 2.79 GiB | 8.90 GiB | 8.77 GiB | 8.90 GiB | 8.90 GiB | Reported |
| 8K Concurrency | N/A (capacity not measured) | N/A (capacity not measured) | N/A (capacity not measured) | N/A (capacity not measured) | N/A (capacity not measured) | Reported |
| Free VRAM | 1416 MiB | 1344 MiB | 338 MiB | 1344 MiB | 1344 MiB | Reported |
| Startup/Endpoint | PASS HTTP 200 (~175.5s load) | PASS HTTP 200 (~52.5s load) | PASS HTTP 200 (~5.9s load; adapter) | PASS HTTP 200 (AWQ base endpoint) | PASS HTTP 200 (AWQ base endpoint) | Reported |
| Final Gate | BASELINE | HOLD: External Overall/manual or variant gate | HOLD: External Overall/manual or variant gate | HOLD: External Overall/manual or variant gate | HOLD: External Overall/manual or variant gate | BASELINE |

## Notes

- External Overall is N/A because FinanceBench requires manual adjudication; Auto Mean is reported separately.
- AWQ+Finetune is reported only when the exact AWQ adapter has passed save/reload; NF4 adapters are never substituted.
- AWQ+RAG uses the final term-explicit glossary. The first body-wide-alias attempt was excluded for prompt contamination.
- AWQ+Reasoning stores the AWQ draft separately and scores only successful gpt-4o-mini rewrites.
- Fair model-only performance prompt and request policy: `performance_prompt_fair_v2.json`; pipeline latency for Reasoning/RAG remains separate from model-only throughput.
- Port mapping was verified as `127.0.0.1:8000` only.
