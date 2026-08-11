---
name: financial-portfolio-assessment
description: Assess whether a security belongs in a portfolio using evidence, mandate, valuation, risk, and suitability checks. Advisory only; never place or approve an order.
version: 1.0.0
metadata:
  hermes:
    category: finance
    source: hgfinance-canonical
    reuse_policy: shared-read-only
---

# Financial portfolio assessment

Use this skill for an evidence-backed portfolio inclusion assessment. The
output is advisory and must not be treated as an order, risk approval, NAV
confirmation, ledger posting, or broker instruction.

## Required workflow

1. Restate the instrument, as-of time, user mandate, investment horizon, and
   the requested decision. If an essential input is missing, identify it as a
   blocker instead of inventing a value.
2. Gather point-in-time evidence for the business, financial condition,
   valuation, liquidity, material events, and relevant market regime. Separate
   facts from calculations, assumptions, and opinions. Record the source and
   timestamp for each material claim.
3. Assess portfolio fit: mandate/universe eligibility, concentration,
   correlation or overlap, liquidity, drawdown tolerance, and downside cases.
   State uncertainty and conflicting evidence explicitly.
4. Give a bounded recommendation such as `consider`, `hold`, `defer`, or
   `reject`, with conditions and evidence. Do not convert it into a quantity,
   order intent, or execution instruction.
5. If the request includes trading or execution, stop at the advisory
   boundary and state that Risk, QA, OMS, and user-approval gates remain
   required.

## Output contract

Return sections for `scope`, `as_of`, `evidence`, `portfolio_fit`, `risks`,
`uncertainties`, and `recommendation`. Mark unsupported claims as
`inconclusive` and escalate rather than filling gaps from memory.
