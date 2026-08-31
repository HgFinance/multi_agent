begin;

-- A fill-gated entry exit can explicitly survive multiple KRX sessions.  The
-- worker may only materialize the immutable, user-confirmed session count into
-- an expiry by reading the governed KRX calendar; it never receives authority
-- to create, delete, or alter a rule's AST/version.
grant usage on schema reference to svc_conditional_rule_worker;
grant select (
  calendar_version_id, market, version, effective_from, effective_to
) on reference.market_calendar_versions to svc_conditional_rule_worker;
grant select (
  calendar_version_id, market, trade_date, session_type, closes_at, is_trading_day
) on reference.market_sessions to svc_conditional_rule_worker;

do $conditional_rule_worker_calendar_policy$
begin
  if not exists (
    select 1 from pg_policies
     where schemaname='reference'
       and tablename='market_calendar_versions'
       and policyname='market_calendars_conditional_rule_worker_krx_select'
  ) then
    create policy market_calendars_conditional_rule_worker_krx_select
      on reference.market_calendar_versions for select
      to svc_conditional_rule_worker using (market='KRX');
  end if;
  if not exists (
    select 1 from pg_policies
     where schemaname='reference'
       and tablename='market_sessions'
       and policyname='market_sessions_conditional_rule_worker_krx_select'
  ) then
    create policy market_sessions_conditional_rule_worker_krx_select
      on reference.market_sessions for select
      to svc_conditional_rule_worker using (market='KRX');
  end if;
end
$conditional_rule_worker_calendar_policy$;

-- ``expires_at`` is a runtime materialisation of the already fingerprinted
-- activation_lifetime_trading_days.  This column grant is deliberately narrow.
grant update (expires_at) on execution.conditional_trade_rules
  to svc_conditional_rule_worker;

do $conditional_rule_worker_activation_lifetime_audit$
begin
  if not has_column_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'expires_at',
       'UPDATE'
     )
     or not has_column_privilege(
       'svc_conditional_rule_worker',
       'reference.market_sessions',
       'closes_at',
       'SELECT'
     )
     or not has_column_privilege(
       'svc_conditional_rule_worker',
       'reference.market_calendar_versions',
       'effective_to',
       'SELECT'
     ) then
    raise exception 'conditional-rule worker lacks bounded KRX lifetime authority';
  end if;
  if has_table_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rule_versions',
       'UPDATE'
     )
     or has_table_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'INSERT'
     )
     or has_table_privilege(
       'svc_conditional_rule_worker',
       'execution.conditional_trade_rules',
       'DELETE'
     ) then
    raise exception 'conditional-rule worker lifetime authority exceeded its boundary';
  end if;
end
$conditional_rule_worker_activation_lifetime_audit$;

comment on column execution.conditional_trade_rules.expires_at is
  'Runtime rule deadline. Fill-gated compound exits may materialize their immutable confirmed KRX-session lifetime here only after full entry fill.';

commit;
