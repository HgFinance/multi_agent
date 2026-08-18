# HgFinance-Internal50-v1 candidate design

Status: candidate-v1. This document and `internal50_candidate_v1.json` are a
held-out evaluation proposal, not an observed model result.

## Purpose and freeze boundary

This set is intended to compare Qwen2.5-14B FP8, AWQ, and a future AWQ+LoRA
variant under the same deterministic contract. The dataset and rubric must be
reviewed and frozen before any model output is collected. No inference,
scoring, or model comparison was performed while creating this candidate.

The set is held out from all future LoRA train/dev data. The cases use
synthetic, self-contained values and repository contract fixtures. They do not
reuse IDs, contexts, gold answers, or prompts from
`benchmarks/quantization/external50_v1.json`.

## Category allocation

| Category | Count | Critical |
|---|---:|---:|
| Financial numerical reasoning | 10 | 0 |
| Accounting / statements | 8 | 8 |
| Risk / compliance / safety | 8 | 8 |
| Portfolio / trading reasoning | 6 | 6 |
| Evidence-grounded research | 6 | 4 |
| Structured JSON / schema | 6 | 6 |
| Quant reasoning | 4 | 4 |
| Uncertainty / hallucination resistance | 2 | 2 |
| **Total** | **50** | **38** |

`critical=true` means that an unsafe financial action, fail-open risk result,
unsupported evidence claim, schema violation, or quant promotion bypass is a
release-blocking regression. A critical failure must not be averaged away by
the aggregate score.

## Case contract

Every case contains:

- `id`: stable `internal50-...` identifier.
- `category`: one of the eight allocation categories.
- `system_prompt`, `user_prompt`, `context`: deterministic inference inputs.
- `expected`: the expected value or structured rubric target.
- `scoring_type`: `exact`, `numeric`, or `json_schema` in this candidate.
- `critical`: release-blocking flag.
- `source_file`: repository provenance for the behavior under test.

`numeric` cases should use a documented tolerance appropriate to the unit;
the candidate values are exact decimal targets. `exact` cases compare the
normalized status/error token. `json_schema` cases require valid JSON and then
check the named schema plus the listed required values; they must not accept a
natural-language paraphrase as a substitute. `contains` and
`semantic_manual` remain available for later reviewed cases, but are not used
here because these 50 targets have deterministic contract assertions.

## Provenance and behavior coverage

The cases are derived from the following production-facing boundaries and
tests:

- Trading contract validation and signal/order separation:
  `departments/02-trading/contracts/contracts.py`
- Trading execution-rate safety:
  `departments/02-trading/execution/broker_rules.py`
- Deterministic pre-trade gates, reason codes, limits, and trading state:
  `departments/03-risk/engine/risk_engine.py` and
  `departments/03-risk/hermes/config.yaml`
- Accounting fill-to-journal-to-NAV-to-reconciliation behavior:
  `tests/e2e/test_accounting_close_loop.py` and
  `departments/05-accounting-portfolio/reporting/daily_report.py`
- Evidence access, numeric citation, point-in-time, contradiction, and QA
  decisions:
  `departments/06-ai-qa-audit/tests/test_evidence_qa_engine.py`
- Worker and inter-department JSON boundaries:
  `docs/02-engineering/contracts/agent-task-context.v1.json`,
  `docs/02-engineering/contracts/agent-task-result.v1.json`,
  `docs/02-engineering/contracts/worker-context.v1.json`,
  `docs/02-engineering/contracts/event-envelope.v1.json`, and
  `docs/02-engineering/contracts/qa-check.v1.json`
- Strategy selection, risk threshold, attribution, and boolean-gate safety:
  `tests/test_trading_alpha_strategy_workers.py`
- Quant preregistration state and timestamp requirements:
  `departments/04-quant-backtest/contracts/quant_v2.py`

## Evaluation handling rules

1. Freeze this JSON and this rubric before collecting FP8/AWQ/AWQ+LoRA output.
2. Keep the file outside every LoRA training and development manifest.
3. Run the same system/user/context contract for every model variant.
4. Validate JSON/schema cases before scoring semantic content.
5. Treat missing, malformed, unsupported, or uncertain outputs as failures or
   escalations according to the case rubric; never convert them into approval.
6. Report critical failures separately from pass rate and do not infer a model
   ranking from this candidate before results exist.

## Known limitations before freeze

- These are candidate fixtures, not a replacement for an approved production
  acceptance suite.
- The risk and accounting cases exercise contract behavior with synthetic
  identifiers and values; they do not assert live broker or database state.
- The final scorer must define numeric tolerance and JSON field-level matching
  without changing the existing External-50 scorer.
- A separate reviewer should confirm that the final train/dev manifests do not
  include this path or any copied case context.
