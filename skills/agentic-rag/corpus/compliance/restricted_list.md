---
document_id: policy-restricted-list-001
document_type: restricted_list
version: "1.0.0"
effective_from: "2026-01-01"
effective_to: null
status: SAMPLE_PLACEHOLDER
---

# Restricted List (SAMPLE — replace with the real, sourced Restricted List before production use)

## Fully Restricted (no new entry, existing positions reduce-only)
- SYMBOL_A — under insider-information blackout, effective 2026-01-01 to 2026-12-31.
- SYMBOL_B — pending litigation flagged by Compliance, effective 2026-02-01 to 2026-12-31.

## Watch List (entry allowed with elevated review)
- SYMBOL_C — elevated short-interest and crowding risk noted by Risk department.

## Sector Restrictions
- No new positions in issuers primarily engaged in prohibited sectors as defined by the user's Mandate (see mandate.md).

## Source and Review
- This list must be reviewed and re-approved by Risk + Compliance monthly, or immediately on a material Compliance event.
- Any Compliance Policy Agent citing this list must reference `effective_from`/`effective_to` and confirm the query's `as_of` date falls within that window (Point-in-Time rule, HEDGE_FUND_MASTER_PLAN.md 9.3).
