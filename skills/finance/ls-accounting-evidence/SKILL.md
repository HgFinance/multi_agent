---
name: ls-accounting-evidence
description: Interpret attached LS OPEN API stock-account evidence for accounting reports, cash/settlement/position reconciliation, fees, taxes, performance, credit limits, margin capacity, and order or execution status. Use when a task contains accounting.broker-evidence.v1, LS account TR codes, or asks about broker account data. Read-only and advisory; the Accounting Engine remains the source of record.
metadata:
  hermes:
    category: finance
    source: hgfinance-canonical
    reuse_policy: shared-read-only
---

# LS accounting evidence

Use this skill to consume the server-attached `accounting.broker-evidence.v1`
contract. The attachment is already sanitized and bounded. Do not attempt to
retrieve LS credentials or call the broker yourself.

## Evidence workflow

1. Confirm `schema_version`, `as_of`, `environment`, masked account, period,
   and `coverage` before using any amount. State `PAPER` or `LIVE` explicitly.
2. Treat the Accounting Engine snapshot as the source of record. Treat every
   LS value as independent reconciliation and reporting evidence only because
   the contract is `authoritative: false` and `is_official: false`.
3. Read `coverage[TR].status`, `complete`, `truncated`, and `error`. Never turn
   `ERROR`, `EMPTY`, `UNAVAILABLE`, `NEEDS_PARAMETERS`, or an incomplete page
   chain into zero or "no activity."
4. Use `reporting_view` for a concise report, then use its detailed source
   sections to explain the number. Cite evidence as `ls-tr:<TR code>` and cite
   the attachment's `as_of` timestamp.
5. Compare LS observations with the Accounting Engine. Preserve every
   `exceptions[]` item and every mismatch in `account_cross_checks` or
   `position_reconciliation`. A mismatch is an open Break candidate, not a
   rounding adjustment or permission to change a journal.
6. Report scope, evidence time, coverage, cash and settlement, positions and
   cost basis, activity and costs, return, open orders, and exceptions. Mark
   missing sections explicitly.

## Interpretation rules

- `CSPAQ12300.positions[].unit_cost_bep` is the fee-adjusted BEP view;
  `t0424.position_check[].average_unit_price` is the average-price check. Label
  them separately and do not claim they should be identical.
- `CDPCQ04700` is period/settled account activity. `t0150` and `t0151` are
  trade-date journals that expose fees and taxes. Do not double-count the same
  activity merely because it appears in both sections.
- D+1 and D+2 deposits or expected settlements are timing buckets, not current
  withdrawable cash. Use `withdrawable` for withdrawals and `cash_orderable`
  for cash orders.
- `CSPAQ13700` describes order/execution history; `t0425` shows current
  execution/unexecuted state. An unexecuted order is not a position or a
  realized trade.
- `FOCCQ33600` is broker-reported period performance. It may explain a report
  but cannot override official Accounting Engine performance.
- `CSPAQ00600` and `CSPBQ00200` require a symbol and order price, plus their
  specific classification input. `NEEDS_PARAMETERS` is the correct default
  for an account-wide report; do not invent an instrument, price, side, or
  loan class.

For the complete mapping of all 12 TRs, read
`references/tr-mapping.md`.

## Hard boundary

Never place or recommend an order, post or edit a journal, close a Break,
confirm an official NAV, or expose an account number or credential. A journal
correction is proposed as a reversing entry and still requires the normal
approval path.
