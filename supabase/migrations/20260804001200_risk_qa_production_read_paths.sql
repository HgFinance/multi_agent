begin;
-- Migration sequence is after the already-applied 20260804001000 revision.

-- Production Risk/QA read paths: PIT lookups only. No data or state mutation.
create index if not exists risk_policies_active_pit_idx
    on risk.policies (fund_id, effective_from desc)
    where status = 'ACTIVE';

create index if not exists risk_limits_active_pit_idx
    on risk.limits (fund_id, effective_from desc)
    where status = 'ACTIVE';

create index if not exists risk_restricted_items_active_pit_idx
    on risk.restricted_items (fund_id, instrument_id, effective_from desc)
    where status = 'ACTIVE';

create index if not exists accounting_portfolio_snapshots_pit_idx
    on accounting.portfolio_snapshots (fund_id, book_id, as_of desc)
    where quality_status in ('PASS', 'WARN');

create index if not exists accounting_positions_pit_idx
    on accounting.positions (fund_id, book_id, as_of desc);

create index if not exists accounting_cash_balances_pit_idx
    on accounting.cash_balances (fund_id, book_id, as_of desc);

create index if not exists accounting_pnl_snapshots_pit_idx
    on accounting.pnl_snapshots (fund_id, book_id, as_of desc);

create index if not exists accounting_valuations_pit_idx
    on accounting.valuations (fund_id, book_id, as_of desc)
    where quality_status in ('PASS', 'WARN');

create index if not exists execution_market_snapshots_pit_idx
    on execution.market_snapshots (instrument_id, as_of desc)
    where quality_status in ('PASS', 'WARN');

create index if not exists risk_counterparties_observed_pit_idx
    on risk.counterparties (counterparty_code, observed_at desc);

create index if not exists workforce_active_profile_lookup_idx
    on workforce.agent_profiles (employment_status, agent_id);

create index if not exists workforce_active_profile_version_lookup_idx
    on workforce.agent_profile_versions (agent_id, status);

commit;
