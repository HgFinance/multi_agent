---
document_id: policy-concentration-001
document_type: policy
version: "1.0.0"
effective_from: "2026-01-01"
effective_to: null
status: SAMPLE_PLACEHOLDER
---

# Concentration and Diversification Policy (SAMPLE)

## Rules
- No single sector may exceed 25% of total NAV.
- No single issuer's securities (equity + derivatives combined) may exceed the per-symbol Mandate cap (see mandate.md).
- Correlated-position clusters (correlation > 0.8 over a trailing 60-day window) are treated as a single concentration bucket for limit purposes.

## Escalation
- A proposed order that would breach this policy must be flagged `RESIZE` or `REJECT` by the Risk department, with the specific rule and observed value cited in the decision.
- Ambiguous cases (e.g., correlation data stale or unavailable) default to conservative treatment: escalate to `risk-supervisor` rather than silently approving.
