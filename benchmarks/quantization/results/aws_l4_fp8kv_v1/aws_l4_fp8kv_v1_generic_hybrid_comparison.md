# AWS L4-fp8KV-v1 — Generic Hybrid Comparison

This table is the new Hybrid candidate after removing benchmark-specific answer fallbacks. Arithmetic uses LLM expression generation plus safe AST evaluation. Structured output uses guided JSON, generic JSON Schema validation, and an LLM semantic audit. FinanceBench uses question-only glossary retrieval. No gold answer is sent to the model and no case-ID answer rule is used.

| Metric | FP8 | AWQ | AWQ+Finetune | AWQ+Reasoning | AWQ+RAG | Historical Selective Hybrid | Generic Hybrid | Verdict |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| Internal Quality | 70.0% | 72.0% | 76.0% | 36.0% | 70.0% | 88.0% (44/50) | **90.0% (45/50)** | Measured |
| Financial Arithmetic | 30.0% | 20.0% | 40.0% | 30.0% | 20.0% | 60.0% (6/10) | **80.0% (8/10)** | Measured |
| Structured Output | 40.0% | 40.0% | 40.0% | 40.0% | 40.0% | 100.0% (5/5; 3 deterministic fallbacks) | **40.0% (2/5; no fallback)** | HOLD semantic coverage |
| Critical Failures | 1 | 1 | 1 | 13 | 2 | 0 | **1** | HOLD |
| Request Errors | 0 | 0 | 0 | 0 | 0 | 0 | **0** | Measured |
| FinQA | 75.0% | 65.0% | 75.0% | 55.0% | 80.0% | 80.0% | **70.0%** | Measured |
| TAT-QA | 100.0% | 93.3% | 80.0% | 80.0% | 86.7% | 93.3% | **86.7%** | Measured |
| FinanceBench Diagnostic | 58.7% | 59.0% | 38.5% | 50.1% | **62.1%** | 58.5% | 49.0% | Diagnostic only |
| External Overall | N/A | N/A | N/A | N/A | N/A | N/A | N/A | FinanceBench manual pending |
| Auto Mean | 0.8556 | 0.7709 | 0.7612 | 0.6555 | 0.8310 | **0.8563** | 0.7657 | Secondary only |
| C1/C2/C4 Throughput | N/A | N/A | N/A | N/A | N/A | 20.87/41.37/80.78 tok/s | N/A | New pipeline not performance-rerun |
| Full Pipeline Latency | N/A | N/A | N/A | N/A | N/A | 2.505/2.534/2.453s avg | N/A | New pipeline not performance-rerun |
| Final Gate | BASELINE | HOLD | HOLD | HOLD | HOLD | HOLD | **HOLD** | Critical failure, FinanceBench manual, performance pending |

## Generic Hybrid errors

- Internal remaining errors: `v2-004`, `v2-009`, `v2-046`, `v2-047`, `v2-049`.
- `v2-004` and `v2-009` are LLM expression unit/percentage mistakes; the AST executed the expression correctly and did not repair it.
- `v2-046`, `v2-047`, and `v2-049` are semantically wrong but schema-valid JSON. They require a complete application contract or stronger model reasoning; a generic validator cannot infer hidden gold semantics.
- FinanceBench glossary RAG is active, but the approved BOK glossary has only one exact question hit among the 15 FinanceBench questions. The old RAG diagnostic `62.1%` is therefore not automatically transferable to this generic pipeline.
