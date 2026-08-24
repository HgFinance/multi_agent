# SUPERSEDED — Hybrid routing A/B comparison using the wrong stage

This file is retained for audit only. It used `selective_unit_scale`, while the final seven-axis Unit/Scale baseline used `structured_fewshot_consensus`. Do not compare these values with the final seven-axis table. Use `../ab_20260824_corrected/ab_comparison.md` instead.

# Hybrid routing A/B comparison — 2026-08-24

All candidates used the existing `hgfinance-vllm-runtime-20260822` endpoint, one NVIDIA L4, the frozen Internal-50 v2 and External-50 v1 datasets, and the frozen scorers. Existing final 7-axis artifacts were not overwritten.

The historical final-table values are shown separately from the paired rerun. The paired rerun is required because `temperature=0` did not produce identical outputs across repeated vLLM calls.

| Metric | Historical final baseline | Paired baseline | A: Finance scoped context | B: Finance typed routing | C: FinQA numeric-only |
|---|---:|---:|---:|---:|---:|
| Internal Quality | 92% | 88% | 86% | 86% | 88% |
| Critical Failures | 0 | 1 | 1 | 1 | 1 |
| Request Errors | 0 | 0 | 0 | 0 | 0 |
| Structured Output | 80% | 40% | 40% | 40% | 40% |
| FinQA | 70% | 70% | 65% | 70% | **75%** |
| TAT-QA | 93.3% | 93.3% | 93.1% | 93.1% | 93.1% |
| FinanceBench diagnostic | 62.2% | 50.8% | 49.2% | 44.9% | 50.6% |
| Auto Mean | 0.7992 | 0.7992 | 0.7706 | 0.7992 | **0.8277** |
| Verdict | historical reference | paired reference | REJECT | REJECT | HOLD — replicate |

## Decisions

- A `finance_scoped_context_rag`: rejected. Internal, Structured Output, FinQA, and FinanceBench all regressed against the paired baseline.
- B `finance_typed_routing`: rejected. Internal and Structured Output regressed, and FinanceBench diagnostic fell to 44.9%.
- C `finqa_numeric_routing`: promising. FinQA improved from 70% to 75% and Auto Mean from 0.7992 to 0.8277 while Internal, Structured Output, TAT-QA, Critical Failures, and Request Errors matched the paired baseline. FinanceBench was 50.6% versus 50.8%, so it is not promoted yet.

The C candidate applies the arithmetic adapter only after an LLM-generated, guided-JSON FinQA route says `CALCULATION`; non-calculation FinQA and all TAT-QA cases keep the frozen text contract. No gold answer, case ID, or deterministic answer fallback is used.

## Reproducibility note

The historical baseline was `92% / Structured 80% / FinanceBench 62.2%`. A same-profile paired baseline rerun immediately before/after C was `88% / Structured 40% / FinanceBench 50.8%`. This confirms runtime/model generation variance and is why C is marked `HOLD — replicate` rather than promoted from a single run.

Artifacts:

- `A-finance-scoped/`
- `B-finance-typed/`
- `C-finqa-numeric/`
- `paired-baseline/`
