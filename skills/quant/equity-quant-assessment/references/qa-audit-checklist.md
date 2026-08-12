# Public-equity QA/audit checklist

Use this reference after a quant screen and before handing a candidate to an
approval owner. It is a quality gate, not an approval workflow.

## 1. Recompute, then classify

- Recompute every headline percentage from the displayed raw inputs at full
  precision; round only the displayed result.
- Label each item as: sourced fact, independently corroborated fact, derived
  calculation, estimate/consensus, inference, or unknown.
- Keep units explicit (KRW versus trillion KRW; close versus adjusted close;
  percent versus percentage points).
- A mechanically correct calculation can still be decision-invalid if its inputs
  are not comparable. In particular, do not describe a forward consensus EPS
  divided by trailing EPS as realized growth or a same-basis YoY rate.

## 2. Date and source reconciliation

- Record the quote timestamp, exchange close date, history endpoint date, filing
  period, and consensus forecast period separately.
- Treat issuer conference decks as provisional when they state that external
  review/audit is incomplete. Do not silently upgrade preliminary figures to
  final audited results.
- Prefer the issuer/exchange/regulator for identity and financial facts. Use a
  dynamic market-data page for quote/valuation context, preserving its displayed
  timestamp and methodology. A DART/company-profile page can establish identity
  without establishing current financials.
- If providers disagree or one endpoint fails, name the exact endpoint and use a
  successful fallback. Do not report a generic provider failure that obscures
  which data were actually used.

## 3. Reproducible price-history checks

For a KRX/Naver daily chart endpoint, preserve the raw URL and state the field
used. The response is commonly XML declared as `EUC-KR`; decode that encoding
before parsing. Each item is conventionally:

`date|open|high|low|close|volume`

For maximum drawdown, specify unadjusted or adjusted close, lookback count,
endpoint date, and trading-day convention. A close-to-close drawdown is the
minimum of `close_t / running_peak_close - 1`; report the peak and trough dates.
Do not call “below the 52-week high” maximum drawdown.

## 4. Conditional opinion versus approval block

A CEO/approval owner may receive a clearly labeled conditional paper-candidate
opinion when the research is otherwise sound. Actual inclusion/approval remains
blocked until mandate eligibility, holdings/look-through, NAV/capital, issuer /
sector/country caps, loss limits, base currency/FX policy, tax/accounting,
custody/settlement, and liquidity inputs are available. Report those as open
findings; do not close them for the owner.

## 5. Side-effect verification

State explicitly that no order, broker connection, allocation, portfolio-ledger
change, NAV update, or order-state change occurred. Search the task artifacts and
execution logs for evidence, but do not claim that a local check proves the state
of an external broker. Research files and citation caches are not portfolio
ledger actions; distinguish them in the report.
