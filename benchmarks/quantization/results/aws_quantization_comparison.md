# HgFinance AWS Quantization Comparison

Status: **HOLD**. The exact AWQ model was found, but the required localhost vLLM endpoint did not pass health verification. No AWQ inference or quality score is reported.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | Verdict |
|---|---:|---:|---:|---:|---:|---|
| Internal Quality | 74.0% baseline | HOLD | HOLD | HOLD | HOLD | HOLD |
| Relative Quality Delta | Baseline | HOLD | HOLD | HOLD | HOLD | HOLD |
| Critical Failures | 1 baseline | HOLD | HOLD | HOLD | HOLD | HOLD |
| New Critical Regression | N/A | HOLD | HOLD | HOLD | HOLD | HOLD |
| Request Errors | 0 baseline | HOLD | HOLD | HOLD | HOLD | HOLD |
| Financial Arithmetic | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| Structured Output | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| External Overall | 74.0% baseline | HOLD | HOLD | HOLD | HOLD | HOLD |
| FinQA | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| TAT-QA | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| FinanceBench | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| Auto Mean | 0.8556 baseline | HOLD | HOLD | HOLD | HOLD | HOLD |
| C1 Throughput | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| C2 Throughput | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| C4 Throughput | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| C1 E2E | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| Model Load Memory | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| KV Cache | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| 8K Concurrency | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| Free VRAM | Not executed in this run | HOLD | HOLD | HOLD | HOLD | HOLD |
| Startup/Endpoint | Existing baseline | HOLD: `127.0.0.1:8000` refused | HOLD | HOLD | HOLD | HOLD |
| Final Gate | BASELINE | HOLD | HOLD | HOLD | HOLD | HOLD |

## Runtime failure note

Runtime: NVIDIA L4, Python 3.12.3, vLLM 0.27.1, FlashInfer 0.6.16.post3, CUDA nvcc 13.3.73. The Python 3.12 environment reached FlashInfer JIT but failed with `CUDA compiler and CUDA toolkit headers are incompatible` and `RuntimeError: Ninja build failed`. The Python 3.11 environment separately failed with `TypeError: type 'array.array' is not subscriptable` in `flashinfer/comm/fd_exchange.py`. Endpoint health was not verified, so benchmark requests were not run.
