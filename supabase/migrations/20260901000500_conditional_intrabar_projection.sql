begin;

-- INTRABAR is a distinct, quote-keyed projection of the active intraday
-- candle.  Keep BAR_CLOSE and QUOTE contracts intact; only the existing
-- conditional-rule tables receive the additive clock value.
alter table execution.conditional_trade_rules
  drop constraint if exists conditional_trade_rules_evaluation_clock_check;
alter table execution.conditional_trade_rules
  add constraint conditional_trade_rules_evaluation_clock_check
  check (evaluation_clock in ('BAR_CLOSE', 'INTRABAR', 'QUOTE'));

alter table execution.conditional_trade_rules
  drop constraint if exists conditional_trade_rules_check;
alter table execution.conditional_trade_rules
  drop constraint if exists conditional_trade_rules_clock_primary_timeframe_check;
alter table execution.conditional_trade_rules
  add constraint conditional_trade_rules_clock_primary_timeframe_check
  check (
    (evaluation_clock in ('BAR_CLOSE', 'INTRABAR') and primary_timeframe is not null)
    or (evaluation_clock = 'QUOTE' and primary_timeframe is null)
  );

alter table execution.conditional_rule_evaluations
  drop constraint if exists conditional_rule_evaluations_evaluation_clock_check;
alter table execution.conditional_rule_evaluations
  add constraint conditional_rule_evaluations_evaluation_clock_check
  check (evaluation_clock in ('BAR_CLOSE', 'INTRABAR', 'QUOTE'));

commit;
