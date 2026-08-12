---
name: equity-quant-assessment
description: "Use for public-equity portfolio screens."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [equities, valuation, quant, portfolio-screening, KRX, citations]
    category: research
---

# Equity Quantitative Assessment

## Purpose

Assess a public equity using only data that can be verified during the task.
The output is a quantitative screen, not investment advice and not an order.
Keep sourced facts, calculations, estimates, and unknowns visibly separate.

## When to Use

Use when asked for a stock/ETF price check, valuation screen, backtest-lite
assessment, portfolio-candidate review, or delegated quant summary—especially
when the user asks for current/recent price, transparent scenarios, and explicit
data gaps.

## Procedure

1. **Define the security and as-of point.** Confirm ticker, exchange, currency,
   and the date/time represented by the quote. Prefer a primary exchange or
   reputable public quote page. Do not silently mix currencies, adjusted and
   unadjusted prices, or different close dates.
2. **Retrieve a price and history.** Obtain the current/recent close plus daily
   history from a public endpoint. Record the source URL at retrieval time.
   If a provider returns an authentication/rate-limit error, record it and use a
   second public provider rather than treating the failure as missing market
   data. See `references/krx-public-data-fallbacks.md` for a tested KRX pattern.
3. **Build a small, reproducible input table.** Include current/recent price,
   prior close, recent trading-day returns (for example 5/20/60 sessions), and
   the history window. If useful and supported by the data, calculate a simple
   close-to-close maximum drawdown and state its exact peak/trough dates.
4. **Collect valuation inputs.** Prefer reported trailing EPS/PER and BPS/PBR.
   You may include sector/industry multiples and forward/consensus EPS, but
   label consensus figures as estimates and preserve their stated period/source.
   Do not treat a forecast as audited earnings.
5. **Calculate transparent scenarios.** Use simple equations such as:
   - implied price = EPS × assumed P/E;
   - implied price = BPS × assumed P/B;
   - return = current price / reference price − 1;
   - drawdown = trough close / prior peak close − 1.
   Show the assumptions and round only the displayed result. Scenario multiples
   are sensitivities, not target prices or recommendations.
6. **Assess candidate status conservatively.** Explain whether the simple screen
   is supportive, mixed, or insufficient. If valuation depends on an unusually
   large forecast-EPS jump, state that explicitly and do not promote it to a
   robust conclusion without independent corroboration.
7. **Declare data gaps.** At minimum check whether you have verified: latest
   filings, cash flow, debt/net cash, segment drivers, forecast provenance and
   revisions, dividend/fees/tax/FX treatment, and the relevant benchmark or
   risk-free assumption. Missing data must remain missing; never fill it from
   memory.
8. **Respect execution boundaries.** Unless separately authorized, do not place
   trades or alter positions, portfolio ledgers, NAV, or order state. State this
   explicitly in the result.
9. **Run an independent QA/audit gate before concluding.** Recompute every
   headline percentage from displayed inputs using full precision, then check
   rounding and units. Classify each statement as sourced fact, independently
   verified fact, derived calculation, estimate/consensus, inference, or unknown.
   Reconcile the data date and timestamp across quote, history, filings, and
   consensus sources; do not silently mix preliminary issuer material with final
   filings or trailing EPS with forward EPS. For a portfolio-inclusion request,
   distinguish “conditional paper candidate” from “approved inclusion”: missing
   mandate, holdings/look-through, NAV/capital, caps, loss limits, base currency,
   tax/accounting/custody, or FX inputs block approval but need not block a clearly
   labeled conditional research opinion. Report open audit findings to the
   decision owner; do not close them on the owner’s behalf.

## Output contract

For delegated or machine-consumed work, use these headings/keys:

- `summary`
- `result`
- `methods`
- `inputs`
- `calculations`
- `evidence`
- `uncertainties/data gaps`
- `error`
- `block_reason`

A compact Korean report is appropriate when the user requests Korean. Cite each
externally sourced claim inline and finish with a generated `Sources:` block.
For financial claims, attach verbatim evidence where the citation workflow
supports it; retain a faithful raw or normalized copy for structured feeds.

## Quality gates

- Every quoted current-price/valuation figure has a dated, inspectable source.
- Every return or scenario can be recomputed from displayed inputs.
- Forecast values are clearly distinguished from trailing/reported values.
- The report names the history window and any data-provider mismatch.
- No trading or portfolio-state side effect occurred.
- Provider errors are reported honestly; a successful fallback is described.

## Pitfalls

- Do not cite search-result snippets as if they were the fetched page.
- Do not use a failed quote-summary API as proof that no quote exists.
- Do not compare a current P/E with a sector P/E without stating the EPS basis.
- Do not call a sensitivity output a target price.
- Do not hide missing filings, forecast revisions, or total-return assumptions.
- Do not modify ledger/NAV state during an assessment.

## Related guidance

- Use the protected `grounded-citations` skill for citation-ledger mechanics and
  evidence verification; this skill adds the equity-screening workflow.
- See `references/krx-public-data-fallbacks.md` for the KRX/Naver retrieval notes
  and the exact transparent calculations used as a reproducible pattern.
- See `references/qa-audit-checklist.md` for the independent arithmetic,
  provenance, preliminary-filing, conditional-opinion, and no-side-effect QA gate.
