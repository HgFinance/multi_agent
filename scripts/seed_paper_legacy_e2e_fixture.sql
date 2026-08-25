-- Seed the durable, PAPER-only owner chain used by the operational legacy E2E.
--
-- This is deliberately a named fixture, not a strategy promotion.  It never
-- creates a LIVE row or an external broker order.  The trading API still has
-- to pass its normal Case/Signal/Strategy/Capability/Fund/Book/instrument
-- validation before an intent can be accepted.

begin;

do $$
declare
  v_fund_id uuid;
  v_book_id uuid;
  v_instrument_id uuid;
begin
  select f.fund_id, b.book_id
    into strict v_fund_id, v_book_id
    from accounting.funds f
    join accounting.books b using (fund_id)
   where f.fund_code = 'ACC01-PAPER'
     and f.status = 'ACTIVE'
     and b.book_code = 'MAIN'
     and b.status = 'ACTIVE';

  select i.instrument_id
    into strict v_instrument_id
    from reference.instruments i
    join reference.instrument_symbols s using (instrument_id)
   where i.asset_class = 'EQUITY'
     and i.status = 'ACTIVE'
     and s.symbol = '005930'
     and s.is_primary
   limit 1;

  insert into governance.cases
      (case_id, fund_id, display_id, case_type, priority, status,
       owner_department, due_at, trace_id, schema_version, created_by)
  values
      ('0e2e0000-0000-4000-8000-000000000001', v_fund_id,
       'E2E-PAPER-LEGACY-001', 'PAPER_E2E_FIXTURE', 0, 'OPEN',
       'quant-backtest-department', '2099-12-31 23:59:59+00',
       '0e2e0000-0000-4000-8000-000000000007', 1,
       'ops:e2e-paper-fixture')
  on conflict (case_id) do update set
      fund_id = excluded.fund_id,
      status = 'OPEN',
      due_at = excluded.due_at,
      updated_at = now();

  insert into governance.case_events
      (case_id, sequence, event_type, from_status, to_status, schema_version,
       producer, actor, reason, idempotency_key, payload, occurred_at)
  values
      ('0e2e0000-0000-4000-8000-000000000001', 1,
       'PAPER_E2E_FIXTURE_CREATED', null, 'OPEN', 1,
       'seed_paper_legacy_e2e_fixture', 'ops:e2e-paper-fixture',
       'Durable PAPER legacy E2E owner-chain fixture',
       'paper-e2e-fixture:case:created',
       jsonb_build_object('fixture', true, 'live_orders', false), now())
  on conflict (idempotency_key) do nothing;

  insert into strategy.strategies
      (strategy_id, strategy_code, name, family, directionality,
       owner_department, status, current_version)
  values
      ('0e2e0000-0000-4000-8000-000000000002', 'E2E_PAPER_LEGACY',
       'Operational PAPER legacy E2E fixture', 'e2e_validation', 'LONG',
       'quant-backtest-department', 'PAPER', 1)
  on conflict (strategy_id) do update set
      status = 'PAPER',
      current_version = 1,
      updated_at = now();

  insert into strategy.capability_profiles
      (capability_profile_id, profile_code, version,
       required_data_products, required_instruments, execution_capabilities,
       risk_capabilities, accounting_capabilities, environment_status,
       status, content_hash)
  values
      ('0e2e0000-0000-4000-8000-000000000003', 'E2E_PAPER_EQUITY', 1,
       '["krx-basket-daily/v1"]'::jsonb, '["EQUITY"]'::jsonb,
       '["limit", "market"]'::jsonb, '["position_limit"]'::jsonb,
       '["double_entry"]'::jsonb,
       '{"PAPER":{"status":"READY","broker_adapter":"paper","live_orders":false},"LIVE":{"status":"DISABLED"}}'::jsonb,
       'ACTIVE', 'e2e-paper-equity-capability-v1')
  on conflict (capability_profile_id) do update set
      status = 'ACTIVE',
      environment_status = excluded.environment_status;

  insert into strategy.versions
      (strategy_version_id, strategy_id, version, capability_profile_id,
       signal_schema, target_portfolio_schema, config, artifact_path,
       artifact_hash, code_version, effective_from, deployment_state)
  values
      ('0e2e0000-0000-4000-8000-000000000004',
       '0e2e0000-0000-4000-8000-000000000002', 1,
       '0e2e0000-0000-4000-8000-000000000003',
       '{"schema":"paper-e2e-signal.v1"}'::jsonb,
       '{"schema":"single-equity-target.v1","max_weight":0.01}'::jsonb,
       '{"fixture":true,"execution_mode":"PAPER","live_orders":false}'::jsonb,
       'fixture://paper-legacy-e2e/v1',
       'e2e-paper-legacy-strategy-v1', 'e2e-fixture-20260824', now(), 'PAPER')
  on conflict (strategy_version_id) do update set
      deployment_state = 'PAPER',
      effective_to = null;

  insert into strategy.deployments
      (deployment_id, strategy_version_id, environment, status,
       runtime_config, deployed_artifact_hash, deployed_at, trace_id)
  values
      ('0e2e0000-0000-4000-8000-000000000005',
       '0e2e0000-0000-4000-8000-000000000004', 'PAPER', 'ACTIVE',
       '{"fixture":true,"broker_adapter":"paper","live_orders":false}'::jsonb,
       'e2e-paper-legacy-strategy-v1', now(),
       '0e2e0000-0000-4000-8000-000000000008')
  on conflict (deployment_id) do update set
      status = 'ACTIVE',
      stopped_at = null,
      runtime_config = excluded.runtime_config;

  insert into strategy.signals
      (signal_id, case_id, fund_id, strategy_version_id, signal_type,
       directionality, strength, confidence, as_of, valid_until, payload,
       input_hash, schema_version, trace_id)
  values
      ('0e2e0000-0000-4000-8000-000000000006',
       '0e2e0000-0000-4000-8000-000000000001', v_fund_id,
       '0e2e0000-0000-4000-8000-000000000004', 'ENTRY', 'LONG',
       0.01, 1.0, now(), '2099-12-31 23:59:59+00',
       '{"fixture":true,"purpose":"operational legacy E2E only"}'::jsonb,
       'e2e-paper-legacy-signal-v1', 1,
       '0e2e0000-0000-4000-8000-000000000009')
  on conflict (signal_id) do update set
      valid_until = excluded.valid_until,
      payload = excluded.payload;

  insert into strategy.signal_targets
      (signal_id, instrument_id, leg_index, role, target_weight, currency, metadata)
  values
      ('0e2e0000-0000-4000-8000-000000000006', v_instrument_id, 0,
       'PRIMARY', 0.01, 'KRW',
       '{"fixture":true,"live_orders":false}'::jsonb)
  on conflict (signal_id, leg_index) do update set
      instrument_id = excluded.instrument_id,
      role = excluded.role,
      target_weight = excluded.target_weight,
      currency = excluded.currency,
      metadata = excluded.metadata;

  insert into execution.trade_cases
      (trade_case_id, fund_id, book_id, strategy_version_id, strategy_family,
       primary_instrument_id, signal_id, case_status, thesis, invalidation,
       expires_at, created_by, trace_id)
  values
      ('0e2e0000-0000-4000-8000-000000000001', v_fund_id, v_book_id,
       '0e2e0000-0000-4000-8000-000000000004', 'e2e_validation',
       v_instrument_id, '0e2e0000-0000-4000-8000-000000000006', 'OPEN',
       '{"fixture":true,"thesis":"operational legacy E2E wiring"}'::jsonb,
       '{"fixture":true,"invalidation":"fixture only; never promote to LIVE"}'::jsonb,
       '2099-12-31 23:59:59+00', 'ops:e2e-paper-fixture',
       '0e2e0000-0000-4000-8000-00000000000a')
  on conflict (trade_case_id) do update set
      fund_id = excluded.fund_id,
      book_id = excluded.book_id,
      strategy_version_id = excluded.strategy_version_id,
      primary_instrument_id = excluded.primary_instrument_id,
      signal_id = excluded.signal_id,
      case_status = 'OPEN',
      expires_at = excluded.expires_at;
end $$;

commit;

select
  s.strategy_code,
  s.status as strategy_status,
  v.version,
  v.deployment_state,
  tc.trade_case_id,
  tc.case_status,
  cs.display_id,
  i.symbol
from strategy.strategies s
join strategy.versions v on v.strategy_id = s.strategy_id
join execution.trade_cases tc on tc.strategy_version_id = v.strategy_version_id
join governance.cases cs on cs.case_id = tc.trade_case_id
join strategy.signal_targets st on st.signal_id = tc.signal_id and st.leg_index = 0
join lateral (
  select symbol from reference.instrument_symbols
   where instrument_id = st.instrument_id and is_primary
   order by symbol limit 1
) i on true
where s.strategy_code = 'E2E_PAPER_LEGACY';
