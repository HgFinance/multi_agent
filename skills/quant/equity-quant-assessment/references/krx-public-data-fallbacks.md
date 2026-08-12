# KRX public-data fallback pattern

Validated during a Samsung Electronics (005930) screen on 2026-08-11.

## Endpoints

- Quote/valuation page: `https://finance.naver.com/item/main.naver?code=005930`
- Daily history XML: `https://fchart.stock.naver.com/sise.nhn?symbol=005930&timeframe=day&count=100&requestType=0`
- Yahoo chart history fallback: `https://query1.finance.yahoo.com/v8/finance/chart/005930.KS?range=1y&interval=1d&events=history`

Naver's quote page returned current price, prior-day change, trailing EPS/PER,
BPS/PBR, sector PER, and an explicitly labelled forward/consensus EPS/PER.
The chart feed contains records in the form:
`YYYYMMDD|open|high|low|close|volume`. Parse the numeric fields directly and
keep the raw response for evidence. The chart feed's XML declaration says
EUC-KR, but the numeric records are ASCII; decode defensively and verify the
resulting dates/values.

## Provider fallback behavior

In the validated run, Yahoo's chart endpoint returned historical data, while
Yahoo quote and quoteSummary endpoints returned HTTP 401. Record that error,
but do not conclude that the market data is unavailable: Naver's public quote
and chart endpoints supplied a working fallback.

## Evidence handling

A single XML `<item>` record can be too short for evidence tooling's minimum
quote length. Preserve the raw XML and, if needed, create a faithful normalized
line retaining the exact date, OHLC, and volume fields; attach a quote spanning
at least three words/fields and keep it traceable to the raw record. Never
invent a quote or cite a search snippet as page content.

## Reproducible calculations

For closes `C_t` and `C_{t-n}`:

`return_n = C_t / C_{t-n} - 1`

For a simple valuation sensitivity:

`implied_price = reported_EPS × assumed_PE`

Always report the observation window, units (KRW), and whether values are
trailing, reported, or consensus-estimated. A scenario multiple is a
sensitivity, not a target price.
