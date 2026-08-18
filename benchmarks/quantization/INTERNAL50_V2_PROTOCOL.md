# HgFinance Internal-50 v2 — Employee Reasoning

## Purpose

Measure the base reasoning capability of the employee-level finance LLM
before HgFinance domain fine-tuning.

This benchmark is NOT intended to test memorization of internal source-code
identifiers, exception names, enums, or undocumented company policies.

## Dataset

- File: benchmarks/quantization/internal50_v2_reasoning.json
- SHA256: ad2bdaf5ea381c2fc151fce1f1859f7f925b86fd03b830319cd97af17709e978
- Cases: 50
- Critical cases: 19
- Status: FROZEN_PRE_INFERENCE

## Coverage

- Financial arithmetic: 10
- Accounting reasoning: 6
- Risk reasoning: 7
- Portfolio/trading reasoning: 6
- Quant reasoning: 6
- Evidence reasoning: 6
- Uncertainty/fail-closed reasoning: 4
- Structured output: 5

## Fairness principles

1. Every task-specific rule required to answer is provided in the prompt.
2. No HgFinance-internal enum or source-code memorization is required.
3. Numerical mistakes are failures.
4. Choice questions expose the valid answer labels.
5. Structured-output schemas are explicitly provided in the prompt.
6. FP8 and AWQ use the identical dataset, runner, scorer, generation settings,
   serving context length, GPU utilization target, and KV-cache dtype.
7. Dataset, runner, and scorer must not be modified after model inference begins.
8. This benchmark must remain held out from future LoRA/SFT train and dev data.

## Generation

- OpenAI-compatible /v1/chat/completions
- temperature = 0
- max_tokens = 256
- stream = false
- sequential execution
- max_model_len = 8192
- gpu_memory_utilization = 0.85
- kv_cache_dtype = fp8

## Scoring

- numeric: deterministic numerical comparison with predefined tolerance
- choice: one of the labels explicitly supplied in the question
- json_exact: semantic JSON parsing plus exact requested key/value structure
- request errors: failure

## Base quantization promotion gate

Primary quality benchmarks:

1. External-50
2. Internal-50 v2 Employee Reasoning

AWQ target:

- Internal-50 v2 relative quality degradation <= 3% vs FP8
- External-50 relative degradation <= 3% on the designated primary quality metric
- no new critical reasoning failures
- no request reliability regression
- meaningful latency / throughput improvement
- meaningful GPU-memory / KV-cache improvement

Internal-50 v1 is retained separately as a production contract-adherence
benchmark, primarily for evaluating domain fine-tuning rather than base-model
quantization quality.

## Fine-tuning isolation

External-50, Internal-50 v1, and Internal-50 v2 must not be used in
LoRA/SFT train or dev data.
