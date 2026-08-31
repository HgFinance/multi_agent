begin;

-- A RETURN_POINTS trail compares the highest and current return against the
-- same cost basis.  Preserve that basis with the existing durable trailing
-- state so a later manual cost-basis recalculation cannot alter an armed rule.
alter table execution.conditional_rule_trailing_states
  add column baseline_average_entry_price numeric(30,10)
  check (baseline_average_entry_price is null or baseline_average_entry_price > 0);

comment on column execution.conditional_rule_trailing_states.baseline_average_entry_price is
  'First trusted average-entry price for DRAWDOWN_MODE=RETURN_POINTS; null for legacy PRICE_RATIO trails.';

commit;
