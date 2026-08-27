# Autonomous Research Artifacts

The lab stores human-readable summaries plus machine-readable artifacts. The machine-readable files are authoritative for validation; Markdown summaries are derived views.

## Objective

`objective.json` contains `goal`, `universe`, `horizon`, `constraints`, `created_at` and a schema version. Do not silently replace an objective in an existing lab. Start a new lab for a materially different objective.

## Hypothesis

Each file in `hypotheses/` contains a stable `hypothesis_id`, `statement`, `mechanism`, `expected_behavior`, at least one `falsifiers` entry, research `dimensions`, optional `parent_id`, and a role such as `explore`, `challenge` or `pivot`.

## Plan

Each file in `plans/` contains a stable `plan_id`, its `hypothesis_id`, objective, method, data requirements, split names, explicit `cost_model`, seed, representation `signature`, preregistration hash and status. A plan must name development, validation, out-of-sample and forward/paper observation stages unless a documented limitation explains why one is unavailable.

## Result

`results/<plan_id>.json` must contain:

- `plan_id` matching a registered plan;
- `status`: `COMPLETED`, `FAILED` or `BLOCKED`;
- boolean `cost_included`, `oos_evaluated` and `leakage_detected`;
- named boolean `robustness` checks;
- measured `metrics` and reproducible `artifacts` for completed results;
- `failure_modes`, `limitations`, and a concrete `failure_reason` for failed or blocked results.

Non-finite metrics, empty robustness evidence, mismatched plan IDs and fabricated zeroes are invalid. A valid result is still subject to the decision gates in the rubric.

## Candidate

`candidate.json` links the strategy thesis, mechanism, hypothesis lineage and plan to the evidence, failure modes and limitations. Its confidence must say that it is an evidence-gated candidate and not a live-trading approval.
