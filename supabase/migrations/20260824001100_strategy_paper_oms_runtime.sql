begin;

-- The user-directive role is deliberately unable to create automated strategy
-- intents.  OMS mutations use a separate PAPER-only role, selected explicitly
-- by the trading runtime DSN.  This keeps the two PAPER admission planes
-- auditable and prevents a directive credential from becoming an OMS writer.
do $role_setup$
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_strategy_paper_executor') then
    create role svc_strategy_paper_executor nologin nosuperuser nocreatedb nocreaterole noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'hgfinance_trading_runtime') then
    raise exception 'hgfinance_trading_runtime must exist before the OMS runtime grant';
  end if;
  execute 'grant svc_strategy_paper_executor to hgfinance_trading_runtime with set true, inherit false';
end
$role_setup$;

grant usage on schema strategy, execution, governance, accounting, reference, risk
  to svc_strategy_paper_executor;

grant select on
  strategy.strategies,
  strategy.capability_profiles,
  strategy.versions,
  strategy.signals,
  strategy.signal_targets,
  execution.trade_cases,
  execution.intent_groups,
  execution.market_snapshots,
  execution.order_intents,
  execution.orders,
  execution.order_events,
  execution.fills,
  execution.outbox,
  governance.cases,
  accounting.funds,
  accounting.books,
  reference.instruments,
  risk.risk_decisions,
  risk.risk_requests,
  risk.risk_request_items
  to svc_strategy_paper_executor;

grant insert on execution.intent_groups, execution.market_snapshots,
  execution.order_intents, execution.orders, execution.order_events,
  execution.fills, execution.outbox
  to svc_strategy_paper_executor;

grant update (intent_status, risk_decision_id, quantity)
  on execution.order_intents to svc_strategy_paper_executor;
grant update (state, last_event_at, version, broker_order_id,
              filled_quantity, average_fill_price)
  on execution.orders to svc_strategy_paper_executor;
grant update (status, sent_at, attempts, last_error, available_at)
  on execution.outbox to svc_strategy_paper_executor;
grant usage, select on sequence execution.outbox_outbox_id_seq
  to svc_strategy_paper_executor;

-- Every table below has RLS enabled.  These policies are intentionally scoped
-- to this role; grants alone must never be enough to bypass the PAPER fence.
create policy strategies_svc_strategy_paper_executor_select
  on strategy.strategies for select to svc_strategy_paper_executor using (true);
create policy capability_profiles_svc_strategy_paper_executor_select
  on strategy.capability_profiles for select to svc_strategy_paper_executor using (true);
create policy versions_svc_strategy_paper_executor_select
  on strategy.versions for select to svc_strategy_paper_executor using (true);
create policy signals_svc_strategy_paper_executor_select
  on strategy.signals for select to svc_strategy_paper_executor using (true);
create policy signal_targets_svc_strategy_paper_executor_select
  on strategy.signal_targets for select to svc_strategy_paper_executor using (true);

create policy trade_cases_svc_strategy_paper_executor_select
  on execution.trade_cases for select to svc_strategy_paper_executor using (true);
create policy intent_groups_svc_strategy_paper_executor_all
  on execution.intent_groups for all to svc_strategy_paper_executor
  using (true) with check (true);
create policy market_snapshots_svc_strategy_paper_executor_all
  on execution.market_snapshots for all to svc_strategy_paper_executor
  using (true) with check (true);
create policy order_intents_svc_strategy_paper_executor_all
  on execution.order_intents for all to svc_strategy_paper_executor
  using (true) with check (true);
create policy orders_svc_strategy_paper_executor_paper
  on execution.orders for all to svc_strategy_paper_executor
  using (broker_adapter = 'paper') with check (broker_adapter = 'paper');
create policy order_events_svc_strategy_paper_executor_paper
  on execution.order_events for all to svc_strategy_paper_executor
  using (broker_adapter = 'paper')
  with check (
    broker_adapter = 'paper'
    and exists (
      select 1
        from execution.orders target
       where target.order_id = execution.order_events.order_id
         and target.broker_adapter = 'paper'
    )
  );
create policy fills_svc_strategy_paper_executor_paper
  on execution.fills for all to svc_strategy_paper_executor
  using (
    exists (
      select 1
        from execution.orders target
       where target.order_id = execution.fills.order_id
         and target.broker_adapter = 'paper'
    )
  )
  with check (
    exists (
      select 1
        from execution.orders target
       where target.order_id = execution.fills.order_id
         and target.broker_adapter = 'paper'
    )
  );
create policy outbox_svc_strategy_paper_executor_oms
  on execution.outbox for all to svc_strategy_paper_executor
  using (true)
  with check (
    producer = 'trading-oms'
    and schema_version = 'event-envelope-v1'
    and event_type in ('execution.order_state_changed.v1', 'trading.fill.v1')
  );

create policy cases_svc_strategy_paper_executor_select
  on governance.cases for select to svc_strategy_paper_executor using (true);
create policy funds_svc_strategy_paper_executor_select
  on accounting.funds for select to svc_strategy_paper_executor using (true);
create policy books_svc_strategy_paper_executor_select
  on accounting.books for select to svc_strategy_paper_executor using (true);
create policy instruments_svc_strategy_paper_executor_select
  on reference.instruments for select to svc_strategy_paper_executor using (true);
create policy risk_decisions_svc_strategy_paper_executor_select
  on risk.risk_decisions for select to svc_strategy_paper_executor using (true);
create policy risk_requests_svc_strategy_paper_executor_select
  on risk.risk_requests for select to svc_strategy_paper_executor using (true);
create policy risk_request_items_svc_strategy_paper_executor_select
  on risk.risk_request_items for select to svc_strategy_paper_executor using (true);

comment on role svc_strategy_paper_executor is
  'PAPER-only automated OMS runtime; selected explicitly by TRADING_OMS_DATABASE_URL';

commit;
