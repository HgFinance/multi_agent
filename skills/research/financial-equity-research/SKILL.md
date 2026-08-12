---
name: financial-equity-research
description: "Use when researching a public company for portfolio fit."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [equity-research, financial-analysis, valuation, portfolio]
    category: research
---

# Financial Equity Research

## When to Use

Use for a current, source-backed assessment of a public company as a potential portfolio constituent. This skill produces research, not trade execution, order placement, or individualized investment advice.

## Trigger

Use when the user asks whether a listed company belongs in a portfolio, requests a current equity brief, or delegates company research to an agent.

## Core principles

1. **Current and dated:** State the as-of date/time and distinguish market-close data, company-reported periods, estimates, and forecasts.
2. **Primary-first:** Start with the company IR site, regulatory filings, earnings releases, and investor presentations. Use market-data aggregators only for timestamped quotes, valuation snapshots, and independent cross-checks.
3. **Fact versus interpretation:** Label company guidance as management outlook; separately identify analyst interpretation, calculations, and uncertainties. Do not turn a catalyst into a guaranteed outcome.
4. **Balanced decision frame:** Cover business mix, revenue/earnings drivers, valuation context, catalysts, material negatives, data gaps, and what would invalidate the thesis.
5. **No execution:** Explicitly state that no order, broker connection, or trade action was attempted.
6. **Citations while drafting:** Register every retrieved source before prose drafting and cite each external factual claim inline. Use the protected `grounded-citations` workflow when available.

## Workflow

### 1. Define the research question

Record ticker/exchange, company, portfolio role under consideration, as-of date, requested currency, and the boundary between research and execution. If mandate details are missing, say so rather than inventing position size, horizon, volatility tolerance, or existing exposure.

### 2. Build the source set

Retrieve, in this order where available:

- official investor-relations earnings release/presentation for the latest quarter;
- latest annual report/business report and regulatory filing;
- official segment tables and management outlook;
- timestamped exchange or reputable quote source for price, market cap, 52-week range, and ownership;
- one independent market-data source for cross-checking historical financials or quote metadata;
- reputable secondary reporting only for material developments not covered by primary sources.

If the global IR site is blocked or dynamically rendered, check the company’s local-language IR site and inspect its HTML for downloadable PDF links. Treat the PDF itself as the primary evidence and record the canonical URL.

### 3. Extract and normalize evidence

Capture exact period labels and units. For segment analysis, record revenue and operating profit by segment and note whether segment revenue includes intersegment sales. Keep reported figures separate from derived percentages. For valuation, record the metric definition (trailing, forward/consensus, fiscal-year basis), timestamp, and peer comparison basis.

For each source, retain a short verbatim evidence quote when the work is financial or otherwise high-stakes. Quotes must come from the fetched page/PDF text, not from a search snippet or memory.

### 4. Analyze the business

Cover:

- segment/business mix and concentration;
- cyclical versus structural revenue drivers;
- margin and cash-flow drivers;
- customer, product, geography, commodity, or platform concentration;
- balance-sheet and capital-allocation context;
- management guidance and the assumptions behind it.

Calculate derived shares only with a tool, and show the numerator/denominator. Flag internal-sales or consolidation caveats.

### 5. Frame valuation without false precision

Present current price and market cap, trailing and/or forward PER, PBR/EV metrics where relevant, dividend yield, and a peer or own-history context. Explain whether the valuation depends on cyclical peak, normalized, or consensus earnings. Do not use a broker target price as a recommendation; label it explicitly as consensus/estimate data.

### 6. Identify catalysts and negatives

Catalysts must be observable events or operating milestones, such as product qualification, capacity ramp, pricing improvement, cost reduction, regulatory approval, or capital return. Material negatives must include downside scenarios, not just generic risks. For each, state the metric or event that should be monitored and the way it could affect revenue, margin, cash flow, or valuation.

### 7. Produce a conditional conclusion

Use a clear outcome such as `candidate`, `conditional candidate`, `watchlist`, or `not supported by current evidence`. Tie the outcome to explicit conditions and monitoring items. Avoid execution instructions, target weights, entry prices, or timing unless the user separately requests a permitted non-execution scenario analysis.

### 8. QA before handoff

Verify:

- every external factual claim has an inline citation or an explicit `[unverified]` marker;
- primary-source claims are corroborated where practical;
- units, currencies, fiscal periods, and as-of dates are consistent;
- derived arithmetic was performed with a tool;
- valuation definitions are not mixed;
- the report includes `summary`, `result`, `evidence`, `uncertainties/data gaps`, `error`, and `block_reason` when requested;
- no order or execution action occurred;
- citation ledger verification passes and the Sources block is rendered mechanically.

## Delegated research and citation ledgers

Parallel research workers may share a filesystem or profile despite having separate conversational contexts. Use a unique ledger path per worker (for example, `/tmp/<task>-<assignee>-citations.json`) rather than the default ledger. Render the worker’s Sources block before handoff. When merging into a parent report, re-register URLs in the parent’s canonical ledger or preserve explicit source URLs; never copy citation numbers blindly across ledgers. See `references/delegated-citation-ledgers.md`.

## Report template

```text
TERMINAL REPORT
parent_task: ...
subject: ...
as_of: ...
execution: NONE

SUMMARY
...

RESULT
- Business mix and drivers
- Valuation context
- Catalysts
- Material negatives
- Conditional research view

EVIDENCE
- Primary sources and cross-checks

UNCERTAINTIES / DATA GAPS
...

ERROR
...

BLOCK_REASON
...

Sources:
[1] ...
```

## Pitfalls

- Treating a search snippet, aggregator estimate, or management forecast as audited fact.
- Reporting a quarterly segment figure without preserving the period and units.
- Calling a high forward-PER discount or a low consensus PER “cheap” without checking the earnings assumptions.
- Ignoring that one cyclical segment can dominate consolidated profit even when revenue is diversified.
- Using a shared default citation ledger in parallel-agent work and handing the parent invalid or colliding citation numbers.
- Conflating a research conclusion with a buy/sell or execution instruction.
- Claiming “no news” after a source failure; record the coverage gap instead.
