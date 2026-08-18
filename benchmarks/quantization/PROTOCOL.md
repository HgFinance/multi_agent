# HgFinance Model Optimization Evaluation Protocol

Version: v1
Frozen date: 2026-08-15

## Comparison columns

1. FP8
2. AWQ
3. AWQ + LoRA

AWQ + LoRA is evaluated only after AWQ passes the production promotion gate.

## External Evaluation

Benchmark:
HgFinance-External50-v1

Sampling:
seed = 42

Composition:
- FinQA public test: 20
- TAT-QA dev: 15
- FinanceBench OPEN_SOURCE: 15
- Total: 50

The exact sampled IDs are frozen in external50.json.

This is a benchmark-derived regression subset.
It is NOT an official FinQA/TAT-QA/FinanceBench leaderboard evaluation.

Purpose:
Measure relative model-quality regression under identical inference conditions.

## Internal Evaluation

Benchmark:
HgFinance-Internal50-v1

Purpose:
Evaluate production-specific financial reasoning, quantitative reasoning,
accounting, risk, portfolio, grounding, structured output and uncertainty.

The evaluation set must never be included in LoRA training data.

## Inference Conditions

- temperature: 0
- identical system prompt
- identical user prompt
- identical context
- identical max_tokens
- identical model serving API
- same dataset IDs
- same scoring implementation/version

## External Scoring

### FinQA

Natural-language answer adaptation.

Numerical answers are normalized for:
- decimal ratio vs percentage representation
- commas/currency symbols
- numerical tolerance

Example:
gold = 0.9
prediction = 90%
=> equivalent

This is NOT official FinQA program accuracy.

### TAT-QA

Answer-level evaluation adapted from the dataset answer representation.

Normalize:
- accounting-negative parentheses
- number formatting
- scale/unit representations
- textual/multi-span answers

This is NOT the official full TAT-QA leaderboard protocol.

### FinanceBench

Gold answer, justification and supplied evidence are used.

Because the original FinanceBench evaluation includes human correctness review,
ambiguous natural-language answers are manually adjudicated as:

CORRECT
INCORRECT

Manual adjudication must be performed using the same rubric for all model variants.

## Promotion Gate: AWQ vs FP8

AWQ promotion requires:

- External mean quality relative degradation <= 3%
- Internal mean quality relative degradation <= 3%
- No new critical internal failures
- Throughput improvement over FP8
- No worse OOM/CUDA/startup reliability

## Future LoRA Evaluation

If AWQ is promoted:

AWQ Base
vs
AWQ + LoRA

must use exactly the same frozen External-50 and Internal-50.

The evaluation samples must be excluded from LoRA train/dev data.

## Result Table

| Metric | FP8 | AWQ | AWQ + LoRA |
|---|---:|---:|---:|
| External-50 | | | |
| FinQA | | | |
| TAT-QA | | | |
| FinanceBench | | | |
| Internal-50 | | | |
| Critical failures | | | |
| C1 throughput | | | |
| C2 throughput | | | |
| C4 throughput | | | |
| TTFT | | | |
| E2E latency | | | |
| KV cache | | | |
| 8K concurrency | | | |
| VRAM | | | |
| Startup/restarts | | | |
