# Hybrid Error Analysis

This report distinguishes the historical selective Hybrid (which used three structured deterministic fallbacks) from the new generic Hybrid (which has no deterministic answer fallback).

## Historical selective Hybrid vs paired AWQ

| Item | Cases |
|---|---:|
| Paired AWQ failures | 14 |
| Corrected by selective Hybrid | 8 |
| Still wrong in selective Hybrid | 6 |

### Corrected by selective Hybrid

- `v2-002` financial_arithmetic: `196000` → `268000`; Calculate realized PnL in KRW.
- `v2-003` financial_arithmetic: `199895
` → `64999895`; Calculate projected cash after settlement.
- `v2-005` financial_arithmetic: `9.94` → `10`; Calculate the percentage return.
- `v2-009` financial_arithmetic: `4.2` → `3.6`; Calculate the portfolio return percentage.
- `v2-014` accounting_reasoning: `4230` → `42300`; Calculate the net cash received in KRW.
- `v2-046` structured_output: `{
  "decision": "REJECT",
  "reason": "snapshot_age_exceeds_limit"
}` → `{"decision":"REJECT","reason":"stale_snapshot"}`; Return the requested structured decision.
- `v2-047` structured_output: `{
  "pnl": 232000,
  "profitable": true
}` → `{"pnl":268000,"profitable":true}`; Return the calculation as JSON.
- `v2-049` structured_output: `{
  "action": "APPROVE",
  "max_additional_notional": 8400000
}` → `{"action":"RESIZE","max_additional_notional":1500000}`; Return the permitted action and maximum additional notional.

### Still wrong in selective Hybrid

- `v2-001` financial_arithmetic: expected `30.0`, Hybrid `45`; Calculate the gross margin percentage.
- `v2-004` financial_arithmetic: expected `23400`, Hybrid `2340000`; Calculate the total transaction cost in KRW.
- `v2-006` financial_arithmetic: expected `300.0`, Hybrid `300000`; Calculate EPS in KRW per share.
- `v2-010` financial_arithmetic: expected `50150.3006012`, Hybrid `50502.51256281407`; Calculate the break-even sale price in KRW.
- `v2-016` accounting_reasoning: expected `640000`, Hybrid `-5159920`; Calculate realized PnL in KRW.
- `v2-029` portfolio_trading_reasoning: expected `200`, Hybrid `1198000`; How many additional shares should be bought?

## Generic Hybrid without deterministic answer fallback

- Internal: `45/50 = 90.0%`.
- Financial Arithmetic: `8/10 = 80.0%`.
- Structured Output: `2/5 = 40.0%`.
- Critical failures: `1` (`v2-046`).
- External Auto Mean: `0.7657` (`27/35`).
- FinanceBench diagnostic: `0.490`; manual adjudication remains required.

### Remaining Internal-50 errors

- `v2-004` financial_arithmetic: expected `23400`, got `2340000`; source `llm_expression_ast_semantic_audit`; Calculate the total transaction cost in KRW.
- `v2-009` financial_arithmetic: expected `3.6`, got `0.036`; source `llm_expression_ast_semantic_audit`; Calculate the portfolio return percentage.
- `v2-046` structured_output: expected `{'decision': 'REJECT', 'reason': 'stale_snapshot'}`, got `{
  "decision": "REJECT",
  "reason": "snapshot_too_old"
}`; source `guided_json_semantic_audit`; Return the requested structured decision.
- `v2-047` structured_output: expected `{'pnl': 268000, 'profitable': True}`, got `{
  "pnl": 26000,
  "profitable": true
}`; source `guided_json_semantic_audit`; Return the calculation as JSON.
- `v2-049` structured_output: expected `{'action': 'RESIZE', 'max_additional_notional': 1500000}`, got `{
  "action": "APPROVE",
  "max_additional_notional": 8750000
}`; source `guided_json_semantic_audit`; Return the permitted action and maximum additional notional.

### External Auto failures

- `finqa:ETFC/2011/page_144.pdf-2` (FinQA): `38.6`; as of december 31 , 2010 , what was the ratio of collateral pledged to the bank by its derivatives counterparties to overnight and other short-term borrowings
- `finqa:C/2008/page_44.pdf-2` (FinQA): `-250.515463918`; what was the percentage change in non-interest revenue from 2007 to 2008?
- `finqa:MRO/2003/page_45.pdf-2` (FinQA): `1056000`; what were total distillates sales in millions for the three year period ? 365 346 345
- `finqa:HWM/2018/page_96.pdf-2` (FinQA): `258.8`; considering the average exercise price of options , what is the increase in the total value of stock options observed during 2016 and 2017 , in millions of dollars?
- `finqa:IP/2009/page_45.pdf-1` (FinQA): `19.6%`; what percentage of contractual obligations for future payments under existing debt and lease commitments and purchase obligations at december 31 , 2009 due in 2011 are maturities of long-term debt?
- `finqa:ILMN/2008/page_86.pdf-4` (FinQA): `1200000`; what was the change in millions of company contributions to the employee benefit plans retirement plan between 2007 and 2008?
- `tatqa:0f032004-ec01-40a0-831b-aac3f7e1b5c7` (TAT-QA): `annually`; How often does the company review the actuarial assumptions which the periodic benefit cost and the actuarial present value of projected benefit obligations are based on?
- `tatqa:9c364cfe-84e4-479d-b3ca-dab2b412e8c4` (TAT-QA): `-1022`; What is the average financing costs between 2018 and 2019?

## Interpretation

- Arithmetic is now generic tool use: the LLM emits an expression, and the server evaluates only safe AST nodes. There are no case IDs, expected values, or finance-specific answer rules.
- Structured output is schema-generic: guided JSON, strict schema validation, and one LLM semantic audit. If the schema or business semantic is not explicit in the request, the system cannot safely invent it; it returns the model result or HOLD/error, never a deterministic answer.
- FinanceBench glossary RAG is applied only to FinanceBench questions using exact query-term matching. The current BOK glossary supplies a relevant hit for only one of the 15 questions, so the old RAG score must not be treated as proof that this glossary generalizes to all FinanceBench accounting concepts.
