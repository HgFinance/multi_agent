# AWS L4-fp8KV-v1 Five-Variant Comparison

Run ID: `aws-l4-fp8kv-v1-20260822`

All measured variants use the same NVIDIA L4 runtime profile. This table is not comparable to earlier autoKV or FP8-KV performance runs.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | Verdict |
|---|---|---|---|---|---|---|
| Internal Quality | 70.00% | 72.00% | HOLD | 36.00% | 70.00% | HOLD |
| Relative Quality Delta vs FP8 | Baseline | 2.86% | HOLD | -48.57% | 0.00% | HOLD |
| Critical Failures | 1 | 1 | HOLD | 13 | 2 | HOLD |
| New Critical Regression | — | 0 | HOLD | 12 | 1 | HOLD |
| Request Errors | 0 | 0 | HOLD | 0 | 0 | HOLD |
| Financial Arithmetic | 30.00% | 20.00% | HOLD | 30.00% | 20.00% | HOLD |
| Structured Output | 40.00% | 40.00% | HOLD | 40.00% | 40.00% | HOLD |
| External Overall | N/A (FinanceBench manual) | N/A (FinanceBench manual) | HOLD | N/A (FinanceBench manual) | N/A (FinanceBench manual) | HOLD |
| FinQA | 75.00% | 65.00% | HOLD | 55.00% | 80.00% | HOLD |
| TAT-QA | 100.00% | 93.33% | HOLD | 80.00% | 86.67% | HOLD |
| FinanceBench | Manual required (diag 58.7%) | Manual required (diag 59.0%) | HOLD | Manual required (diag 50.1%) | Manual required (diag 62.1%) | HOLD |
| Auto Mean | 0.8556 | 0.7709 | HOLD | 0.6555 | 0.8310 | HOLD |
| C1 Throughput | 11.15 tok/s | 20.66 tok/s | HOLD | N/A (pipeline not measured) | N/A (pipeline not measured) | HOLD |
| C2 Throughput | 22.13 tok/s | 40.82 tok/s | HOLD | N/A (pipeline not measured) | N/A (pipeline not measured) | HOLD |
| C4 Throughput | 43.50 tok/s | 78.45 tok/s | HOLD | N/A (pipeline not measured) | N/A (pipeline not measured) | HOLD |
| C1 E2E | 0.268s p50 | 0.144s p50 | HOLD | 0.874s avg | 0.764s avg | HOLD |
| Model Load Memory | 15.39 GiB | 9.38 GiB | HOLD | 9.38 GiB | 9.38 GiB | HOLD |
| KV Cache | 1.53 GiB | 8.90 GiB | HOLD | 8.90 GiB | 8.90 GiB | HOLD |
| 8K Concurrency | N/A (capacity not measured) | N/A (capacity not measured) | HOLD | N/A (capacity not measured) | N/A (capacity not measured) | HOLD |
| Free VRAM | 1416 MiB | 1344 MiB | HOLD | 1344 MiB | 1344 MiB | HOLD |
| Startup/Endpoint | PASS HTTP 200 (~126.2s load) | PASS HTTP 200 (~79.9s load) | HOLD | PASS HTTP 200 (AWQ endpoint reused) | PASS HTTP 200 (AWQ endpoint reused) | HOLD |
| Final Gate | BASELINE | HOLD: External Overall/manual or variant gate | HOLD | HOLD: External Overall/manual or variant gate | HOLD: External Overall/manual or variant gate | BASELINE |

## Notes

- External Overall is N/A because FinanceBench requires manual adjudication; Auto Mean is reported separately.
- AWQ+Finetune is HOLD because no exact AWQ-compatible adapter was present; no NF4 adapter was substituted.
- AWQ+RAG uses the final term-explicit glossary. The first body-wide-alias attempt was excluded for prompt contamination.
- AWQ+Reasoning stores the AWQ draft separately and scores only successful gpt-4o-mini rewrites.
- Port mapping was verified as `127.0.0.1:8000` only.
