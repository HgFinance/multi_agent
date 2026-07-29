---
document_id: policy-mandate-001
document_type: mandate
version: "1.0.0"
effective_from: "2026-01-01"
effective_to: null
status: SAMPLE_PLACEHOLDER
---

# Investment Mandate (SAMPLE — replace with the real user Mandate before production use)

## Objective
Capital preservation first; growth second. Paper-trading phase only (HEDGE_FUND_MASTER_PLAN.md 2.4).

## Allowed Assets
- Listed equities on the primary exchange (KRX), and its representative index derivatives, per HEDGE_FUND_MASTER_PLAN.md 2.3.
- No OTC or exotic derivatives (2.2 excluded scope).

## Position Limits
- Per-symbol target weight cap: 3% of NAV (HEDGE_FUND_MASTER_PLAN.md 2.3).
- Total gross exposure cap: 20% of NAV, subject to revision after validation (2.3).
- Maximum single-order step: no more than a 20 percentage-point change in target weight per decision (see risk-supervisor step-size backlog item — not yet enforced, referenced here for future retrieval testing).

## Forbidden Actions
- No short selling in the initial Long-only MVP (12.5).
- No leverage beyond what the Risk Engine's approved margin rules allow.
- No trading during `ENTRY_BLOCKED` or `HALTED` Kill Switch states (11.3).

## Approval Requirements
- Any change to this Mandate requires a new Version with `effective_from`; the previous Version is never edited in place.
- Mandate-exceeding orders require CEO + Risk department approval (5.6 권한 분리 원칙).
