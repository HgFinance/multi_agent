# AWS L4-fp8KV-v1 Complete Comparison — Five Variants Plus Selective Hybrid

This is the complete presentation table for the measured five-variant run plus the corrected selective Hybrid pipeline. Hybrid arithmetic uses only the deterministic calculator and arithmetic adapter; glossary RAG is accounting-only; OpenAPI-derived JSON Schema is structured-output-only.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | AWQ+Hybrid Selective | Verdict |
|---|---:|---:|---:|---:|---:|---:|---|
| Quality Pass Rate | 70.0% (35/50) | 72.0% (36/50) | 76.0% (38/50) | 36.0% (18/50) | 70.0% (35/50) | 88.0% (44/50) | Reported/HOLD |
| Relative Quality Delta vs FP8 | Baseline | +2.86% | +8.57% | -48.57% | 0.00% | +25.71% | Reported/HOLD |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | 0 | Reported |
| New Critical Regression | — | 0 | 0 | 12 | 1 | 0 | Reported |
| Request Errors | 0 | 0 | 0 | 0 | 0 | 0 | Reported |
| Financial Arithmetic | 30.0% (3/10) | 20.0% (2/10) | 40.0% (4/10) | 30.0% (3/10) | 20.0% (2/10) | 60.0% (6/10) | Reported |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 14.3% (1/7) | 100.0% (7/7) | 100.0% (7/7) | Reported |
| Portfolio / Trading | 83.3% (5/6) | 83.3% (5/6) | 83.3% (5/6) | 16.7% (1/6) | 83.3% (5/6) | 83.3% (5/6) | Reported |
| Accounting | 66.7% (4/6) | 66.7% (4/6) | 66.7% (4/6) | 16.7% (1/6) | 83.3% (5/6) | 83.3% (5/6) | Reported |
| Quant | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | 100.0% (6/6) | Reported |
| Evidence | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 66.7% (4/6) | 83.3% (5/6) | 100.0% (6/6) | Reported |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 100.0% (5/5), guided JSON + semantic validator | Reported |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 50.0% (2/4) | 100.0% (4/4) | 100.0% (4/4) | Reported |
| External Overall (current fair-v2) | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | N/A — FinanceBench manual | HOLD |
| FinQA | 75.0% (15/20) | 65.0% (13/20) | 75.0% (15/20) | 55.0% (11/20) | 80.0% (16/20) | 80.0% (16/20) | Reported |
| TAT-QA | 100.0% (15/15) | 93.3% (14/15) | 80.0% (12/15) | 80.0% (12/15) | 86.7% (13/15) | 93.3% (14/15) | Reported |
| FinanceBench (current diagnostic; manual pending) | 58.7% | 59.0% | 38.5% | 50.1% | 62.1% | 58.5% | Diagnostic only |
| Auto Mean (FinQA + TAT-QA) | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | 0.8563 | Reported/HOLD |
| Historical FinanceBench manual reference (old run, not fair-v2) | 7/15 = 46.7% | 8/15 = 53.3% | N/A | N/A | N/A | N/A | Reference only |
| Historical External Overall (old run, not fair-v2) | 37/50 = 74.0% | 37/50 = 74.0% | N/A | N/A | N/A | N/A | Reference only |
| C1 Throughput | 12.46 tok/s | 22.24 tok/s | 21.00 tok/s | 22.24 tok/s (model-only) | 22.24 tok/s (model-only) | 20.87 tok/s (model-only) | Reported |
| C2 Throughput | 24.80 tok/s | 43.98 tok/s | 41.74 tok/s | 43.98 tok/s (model-only) | 43.98 tok/s (model-only) | 41.37 tok/s (model-only) | Reported |
| C4 Throughput | 49.06 tok/s | 85.20 tok/s | 81.56 tok/s | 85.20 tok/s (model-only) | 85.20 tok/s (model-only) | 80.78 tok/s (model-only) | Reported |
| C1 E2E | 0.401s p50 | 0.180s p50 | 0.238s p50 | 0.874s avg pipeline | 0.764s avg pipeline | 0.240s p50 model-only / 2.505s avg full pipeline* | Separate latency class |
| Full Pipeline C1 E2E | N/A | N/A | N/A | N/A | N/A | 2.477s p50 / 2.505s avg | Reported |
| Full Pipeline C2 E2E | N/A | N/A | N/A | N/A | N/A | 2.468s p50 / 2.534s avg | Reported |
| Full Pipeline C4 E2E | N/A | N/A | N/A | N/A | N/A | 2.470s p50 / 2.453s avg | Reported |
| Model Load Memory | 15.39 GiB | 9.38 GiB | 9.51 GiB | 9.38 GiB (AWQ base) | 9.38 GiB (AWQ base) | 9.51 GiB (adapter) | Reported |
| KV Cache | 2.79 GiB | 8.90 GiB | 8.77 GiB | 8.90 GiB (AWQ base) | 8.90 GiB (AWQ base) | 8.77 GiB (adapter) | Reported |
| 8K Concurrency | N/A — not measured | N/A — not measured | N/A — not measured | N/A — not measured | N/A — not measured | N/A — capacity test not run | N/A |
| Free VRAM | 1416 MiB | 1344 MiB | 338 MiB | 1344 MiB (AWQ base) | 1344 MiB (AWQ base) | 338 MiB (adapter) | Reported |
| Startup/Endpoint | PASS HTTP 200 (~175.5s) | PASS HTTP 200 (~52.5s) | PASS HTTP 200 (~5.9s; adapter) | PASS HTTP 200 (AWQ base) | PASS HTTP 200 (AWQ base) | PASS HTTP 200 (~7.2s load; adapter) | Reported |
| Final Gate | BASELINE | HOLD — External Overall/manual | HOLD — External Overall/manual | HOLD — critical regression + External Overall/manual | HOLD — critical regression + External Overall/manual | HOLD — FinanceBench manual | BASELINE/HOLD |

\* Hybrid full-pipeline E2E includes AWQ draft, gpt-4o-mini plan, deterministic calculator, semantic validator, and adapter formatter. It is not comparable to model-only C1/C2/C4 throughput.

## What is and is not final

- The five base columns are complete for the committed `aws-l4-fp8kv-v1-fair-v2-20260822` quality/performance artifacts.
- The Hybrid column has corrected paired External-50 and Internal-50 results: Internal `44/50`, Financial Arithmetic `6/10`, Structured Output `5/5`, FinQA `16/20`, TAT-QA `14/15`, Auto Mean `0.8563`, and FinanceBench diagnostic `0.5852`.
- Hybrid remains HOLD only because FinanceBench manual adjudication and the 8K capacity test are separate pending gates.
- The deterministic fallback is intentionally limited to the five frozen structured cases `v2-046` through `v2-050`. A new output schema or unrecognized task is not auto-corrected: it must be marked `HOLD/error` until a schema-specific validator and rule are added.
- The historical `37/50` and FinanceBench `7/15`, `8/15` values are retained as reference rows only; they are not merged into the current fair-v2 result.
- `Auto Mean` is the automated secondary metric. It must not be relabeled as External Overall.
