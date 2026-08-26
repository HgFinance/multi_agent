begin;

-- Research realtime capture only needs the current symbol set, never rule
-- instructions, user identity, order authority, or lifecycle mutation rights.
create or replace view execution.active_conditional_subscription_symbols
with (security_barrier = true, security_invoker = false) as
select distinct rule.symbol
  from execution.conditional_trade_rules rule
 where rule.state = 'ACTIVE'
   and rule.execution_mode = 'PAPER'
   and rule.repeat_policy = 'ONCE'
   and rule.expires_at > now();

revoke all on execution.active_conditional_subscription_symbols from public;
grant usage on schema execution to svc_research_collector;
grant select on execution.active_conditional_subscription_symbols
  to svc_research_collector;

comment on view execution.active_conditional_subscription_symbols is
  'Least-privilege symbol projection used by the existing LS realtime collector; no rule payload or trading authority.';

commit;
