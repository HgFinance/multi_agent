# Corrected Hybrid routing A/B comparison — 2026-08-24

This comparison uses the exact Hybrid invocation from `/tmp/hgfinance_final7axis_run.sh` for the final Unit/Scale baseline:

```text
benchmarks/quantization/run_hybrid_sixaxis.py --stage structured_fewshot_consensus --only all
```

The A/B candidates changed only the external routing and were run with `--only external`; Internal/Structured/Critical metrics therefore reference the same contemporaneous `structured_fewshot_consensus` baseline. Existing final seven-axis artifacts and the earlier incorrectly staged A/B artifacts were not overwritten.

| Metric | Corrected baseline: structured_fewshot_consensus | A: Finance scoped context | B: FinQA numeric-only |
|---|---:|---:|---:|
| Internal Quality | **92%** | 92% (unchanged route) | 92% (unchanged route) |
| Critical Failures | **0** | 0 (unchanged route) | 0 (unchanged route) |
| Structured Output | **80%** | 80% (unchanged route) | 80% (unchanged route) |
| FinQA | **70%** | 70% | 65% |
| TAT-QA | **93.3%** | 93.3% | 93.3% |
| FinanceBench diagnostic | 58.5% | 49.2% | **62.1%** |
| Auto Mean | **0.7992** | 0.7992 | 0.7706 |
| Verdict | BASELINE | REJECT | REJECT |

## Decision

- A `finance_scoped_context_rag` is rejected because FinanceBench diagnostic falls from 58.5% to 49.2%.
- B `finqa_numeric_routing` improves FinanceBench diagnostic from 58.5% to 62.1%, but FinQA falls from 70% to 65% and Auto Mean falls from 0.7992 to 0.7706. It is rejected by the primary automatic gate.
- No routing candidate is promoted. The final seven-axis Unit/Scale baseline remains the reference implementation.

FinanceBench values are automatic diagnostic means; official FinanceBench accuracy still requires manual adjudication. The historical final-table FinanceBench value of 62.2% is retained as a prior run reference, not mixed into this paired comparison.

Artifacts:

- `structured-baseline/`
- `A-finance-scoped/`
- `B-finqa-numeric/`
