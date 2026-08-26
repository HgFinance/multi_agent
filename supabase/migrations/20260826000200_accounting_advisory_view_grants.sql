begin;

-- The Accounting API runs as the narrow svc_accounting_ledger capability.
-- Its advisory read model uses security-invoker views in api, so granting the
-- underlying accounting tables alone is insufficient: the role must also be
-- able to enter api, select the four views, and read both reference relations
-- used by api.position_holdings.
grant usage on schema api to svc_accounting_ledger;
grant select on api.portfolio_snapshot_latest, api.position_holdings,
  api.ledger_balances, api.open_breaks to svc_accounting_ledger;
grant select on reference.instruments, reference.instrument_symbols
  to svc_accounting_ledger;

-- api.position_holdings is security_invoker. SELECT grants alone still
-- produce zero labels when reference.instruments RLS has no policy for the
-- Accounting role, which made every held symbol/display_name appear missing.
drop policy if exists reference_instruments_svc_accounting_ledger_select
  on reference.instruments;
create policy reference_instruments_svc_accounting_ledger_select
  on reference.instruments for select to svc_accounting_ledger using (true);

drop policy if exists instrument_symbols_svc_accounting_ledger_select
  on reference.instrument_symbols;
create policy instrument_symbols_svc_accounting_ledger_select
  on reference.instrument_symbols for select to svc_accounting_ledger using (true);

commit;
