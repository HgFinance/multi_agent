# LS stock-account TR to accounting mapping

All TRs use `POST /stock/accno`. The BFF follows LS `tr_cont` and
`tr_cont_key`; t-series calls also forward the CTS values shown below. Full
field definitions are maintained in
`docs/06-integrations/ls-openapi/03-stock/14-37d22d4d.md` in the repository.

| TR | Default request and continuation | Accounting use | Normalized section |
|---|---|---|---|
| `CDPCQ04700` | whole account, requested period, domestic stock; rate 1/s | settled period activity, commissions, taxes, realized PnL and cash movement | `activity.settled_period` |
| `CSPAQ00600` | requires loan class (`01/03/05/07`), symbol, order price; medium `41`; rate 1/s | instrument-specific credit/loan limit and collateral | `credit_limit` |
| `CSPAQ12200` | balance creation `0`; rate 1/s | deposits, orderable/withdrawable cash, valuation and settlement | `account_summary` and cross-checks |
| `CSPAQ12300` | balance `0`, commission `1`, D2 base `0`, unit price `1` (BEP); rate 1/s | BEP positions, valuation, settlement, margin/credit amounts | `positions`, `account_summary` |
| `CSPAQ13700` | all markets/sides/statuses, report date, all order types; rate 1/s | order and execution history | `activity.order_history` |
| `CSPAQ22200` | balance creation `0`; rate 1/s | second cash/orderable/valuation view for cross-check | `account_summary` and cross-checks |
| `CSPBQ00200` | requires side (`1` sell, `2` buy), symbol, order price; rate 1/s | instrument-specific margin-rate order capacity | `margin_capacity` |
| `FOCCQ33600` | requested period, daily term `1`; rate 1/s | broker period return and daily series | `performance` |
| `t0150` | blank CTS initially; rate 2/s | current trade-date journal, commissions and taxes | `activity.today`; CTS from `t0150OutBlock` |
| `t0151` | prior date, blank CTS initially; rate 2/s | prior trade-date journal, commissions and taxes | `activity.previous_day`; CTS from `t0151OutBlock` |
| `t0424` | average price `1`, execution basis `2`, include costs `1`; rate 2/s | position and average-cost cross-check | `position_check`; CTS `cts_expcode` |
| `t0425` | all symbols/statuses/sides, reverse order; rate 2/s | executed and unexecuted order state | `activity.execution_status`; CTS `cts_ordno` |

## Field groups used by reports

- Cash/liquidity: deposit, D+1/D+2 deposit, withdrawable amount, cash and
  substitute orderable amounts.
- Valuation: deposit-assets total, balance value, purchase amount, evaluation
  PnL, investment principal/PnL and rate.
- Settlement: prior/current buy and sell adjustments, D+1/D+2 expected
  settlement, commissions and taxes.
- Margin/credit: receivable, loans, credit orderability, required collateral,
  collateral shortfall and post-change ratio.
- Positions: symbol/name, balance and sellable quantity, BEP or average unit
  cost, current price, purchase/market value, PnL, unsettled/unexecuted and
  credit quantities.
- Activity: dates and identifiers, side/category, quantity and price,
  contract/settlement amounts, commission, taxes, realized PnL, dividends,
  interest and before/after cash.
- Orders: original/order number, order and executed quantities/prices,
  remaining quantity, status, time and channel.

The normalized contract removes passwords, account names, requester names and
full account numbers. Never request or reproduce those fields.
