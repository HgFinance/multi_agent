begin;

-- Least-privilege control-plane role for the existing Research batch collector.
-- Market observations remain on the separate market DB connection; this role
-- can only maintain reference objects and bounded Research snapshots already
-- owned by collector_scheduler.py.

do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_research_collector') then
    create role svc_research_collector nologin noinherit;
  end if;
end;
$$;

grant svc_research_collector to hgfinance_runtime;
grant usage on schema reference, research to svc_research_collector;

grant select, insert, update on
  reference.instruments,
  reference.instrument_symbols,
  reference.derivative_contracts
to svc_research_collector;

grant select on
  reference.market_calendar_versions,
  reference.market_sessions
to svc_research_collector;

grant select, insert, update, delete on
  research.symbol_restrictions,
  research.symbol_restriction_runs
to svc_research_collector;

grant select, insert, update on
  research.collector_runs,
  research.daily_labels
to svc_research_collector;

drop policy if exists reference_instruments_svc_research_collector_all
  on reference.instruments;
create policy reference_instruments_svc_research_collector_all
  on reference.instruments for all to svc_research_collector
  using (true) with check (true);

drop policy if exists reference_instrument_symbols_svc_research_collector_all
  on reference.instrument_symbols;
create policy reference_instrument_symbols_svc_research_collector_all
  on reference.instrument_symbols for all to svc_research_collector
  using (true) with check (true);

drop policy if exists reference_derivative_contracts_svc_research_collector_all
  on reference.derivative_contracts;
create policy reference_derivative_contracts_svc_research_collector_all
  on reference.derivative_contracts for all to svc_research_collector
  using (true) with check (true);

drop policy if exists research_symbol_restrictions_svc_research_collector_all
  on research.symbol_restrictions;
create policy research_symbol_restrictions_svc_research_collector_all
  on research.symbol_restrictions for all to svc_research_collector
  using (true) with check (true);

drop policy if exists research_symbol_restriction_runs_svc_research_collector_all
  on research.symbol_restriction_runs;
create policy research_symbol_restriction_runs_svc_research_collector_all
  on research.symbol_restriction_runs for all to svc_research_collector
  using (true) with check (true);

drop policy if exists research_collector_runs_svc_research_collector_all
  on research.collector_runs;
create policy research_collector_runs_svc_research_collector_all
  on research.collector_runs for all to svc_research_collector
  using (true) with check (true);

drop policy if exists research_daily_labels_svc_research_collector_all
  on research.daily_labels;
create policy research_daily_labels_svc_research_collector_all
  on research.daily_labels for all to svc_research_collector
  using (true) with check (true);

comment on role svc_research_collector is
  'Existing Research batch collector control-plane writer; no order, risk, QA, or market raw-data authority.';

commit;
