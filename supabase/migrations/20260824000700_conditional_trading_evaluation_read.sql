begin;

-- Trading admission revalidates that a durable condition evaluation is still
-- within the user-confirmed freshness window. The role remains read-only and
-- can observe evaluations only for PAPER rules.
grant select on execution.conditional_rule_evaluations to svc_trading_api;

drop policy if exists conditional_rule_evaluations_trading_select
  on execution.conditional_rule_evaluations;
create policy conditional_rule_evaluations_trading_select
  on execution.conditional_rule_evaluations for select
  to svc_trading_api
  using (
    exists (
      select 1
        from execution.conditional_trade_rules rule
       where rule.rule_id = conditional_rule_evaluations.rule_id
         and rule.execution_mode = 'PAPER'
    )
  );

commit;
