# AWS AWQ Arithmetic Pipeline Comparison

Run: `hgfinance-arithmetic-pipeline-v1-scoped-rag`

This is an augmented end-to-end pipeline comparison. It must not be interpreted as a pure AWQ-versus-FP8 model comparison.

| Metric | AWQ baseline | AWQ+ArithmeticPipeline | Delta | Verdict |
|---|---:|---:|---:|---|
| Internal Quality | 72.0% | 90.0% | +18.0%p | Improved |
| Financial Arithmetic | 20.0% | 100.0% | +80.0%p | Improved |
| Accounting | 66.7% | 100.0% | +33.3%p | Improved |
| Critical Failures | 1 | 1 | same | PASS |
| New Critical Regression | N/A | 0 | none | PASS |
| Request Errors | 0 | 0 | same | PASS |
| External Auto Mean | 0.7709 | 0.7325 | -0.0384 | HOLD |
| FinQA | 65.0% | 65.0% | +0.0%p | Same |
| TAT-QA | 93.3% | 86.7% | -6.7%p | HOLD |
| FinanceBench | 0.5895 diagnostic | 0.7873 diagnostic | manual | Manual |
| Scoped RAG hits | N/A | 18 internal / 21 external | scoped | PASS |
| Final Gate | Baseline | HOLD_EXTERNAL_REGRESSION | — | HOLD |

## Runtime

- NVIDIA L4 / Python 3.12.13 / vLLM 0.27.1 / FlashInfer 0.6.16.post3
- `max_model_len=8192`, `gpu_memory_utilization=0.85`, `kv_cache_dtype=fp8_e4m3`
- Prefix caching enabled
- Endpoint verified with the AWQ base model and arithmetic adapter
- Frozen Internal-50 v2 and External-50 v1 hashes are recorded in `comparison.json`

## Interpretation

- The arithmetic and accounting categories improve materially.
- Critical failures and request errors do not increase.
- External Auto Mean falls from `0.7709` to `0.7325`; therefore the pipeline remains HOLD.
- FinanceBench is diagnostic only and requires manual adjudication.
- Model-only latency and full pipeline E2E latency must be reported separately in any performance table.
