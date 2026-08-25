---
name: mandate-dynamic-risk-controls
description: Build or explain a versioned PAPER position risk plan from an approved mandate and authoritative portfolio/market snapshots. Use for stop-loss, take-profit, trailing-stop, quantity-cap, or risk-plan review requests; do not use for company research or direct order execution.
metadata:
  hermes:
    category: risk
    owner_profile: risk-management
    source: hgfinance-project-owned
    reuse_policy: risk-owned-read-only
    version: 1.0.0
---

# Mandate dynamic risk controls

Use this skill when a user asks Risk to calculate or explain position-level
loss budgets, quantity caps, stops, take-profit levels, trailing controls, or
review timing. Corporate and asset research remains with
`financial-risk-research`; this skill consumes Research/Quant observations as
read-only evidence.

## Required workflow

1. Require an approved `mandate_version_id`, authoritative portfolio and market
   snapshot IDs, their observation times, the task/trace IDs, and PAPER mode.
   Do not replace `unversioned`, stale, missing, or non-authoritative data with
   remembered values.
2. Call `POST /risk/v1/position-risk-plans/calculate` with the typed request.
   The deterministic `dynamic-position-risk-planner.v1` is the only source of
   stop price, take-profit price, loss budget, and quantity cap.
3. Preserve all numeric fields, IDs, timestamps, `input_hash`, calculation
   version, data quality, reason codes, and review triggers exactly. Explain
   their meaning without recomputing or editing them.
4. Describe `PROPOSE` as a Risk proposal, `DEFER` as no new entry plan, and
   `REDUCE_ONLY` as an exposure-reduction priority. Never describe any of them
   as an order or fill.
5. Activation requires the Risk-plan lifecycle validation, user approval or an
   already approved auto-policy, Trading conversion, and a final deterministic
   Risk Engine check. Only Trading may create the PAPER conditional rules.

## Safety invariants

- `quantity_cap × abs(entry_reference - stop_price)` must not exceed
  `position_risk_amount`.
- A wider stop must reduce quantity; never widen a losing position's stop or
  increase its loss budget without explicit user approval.
- If a closer downtrend take-profit target violates the minimum reward/risk
  ratio, keep the planner's `DEFER` result.
- Do not generate numeric levels when the planner returns missing, stale, or
  non-authoritative data quality.
- Do not treat Notion, Discord, LLM output, or Hermes memory as canonical Risk
  state. The Risk database and deterministic engine remain authoritative.

## User-facing result

Report the mandate version and current usage, regime and data time, quantity
cap, stop/take-profit/trailing values, loss-budget invariant, expiry/review
triggers, data gaps, and lifecycle/execution state. Clearly state that the plan
is PAPER-only and whether Trading has or has not activated a conditional rule.
