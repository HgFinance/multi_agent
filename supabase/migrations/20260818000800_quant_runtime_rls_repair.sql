begin;

-- The quant worker deliberately reduces its pooled login to svc_quant.  The
-- original role migration granted table privileges, but the foundational
-- quant tables already had RLS enabled and no svc_quant policies.  PostgreSQL
-- therefore returned an empty immutable catalog and blocked every later
-- experiment write.  Restore only the statements exercised by the factory
-- execution chain; dataset construction remains outside this runtime role.

do $quant_runtime_role_exists$
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_quant') then
    raise exception 'svc_quant runtime role is missing';
  end if;
end
$quant_runtime_role_exists$;

-- Immutable catalog input.  svc_quant must be able to inspect the complete
-- membership relation: hiding a non-stock member here could make a mixed
-- universe appear stock-only.  The existing reference.instruments RLS policy
-- exposes only KRX ACTIVE EQUITY/STOCK metadata, so missing, ETF, ETN, and
-- derivative members remain visible as unresolved rows and fail the runner's
-- exact stock-universe attestation.
drop policy if exists universe_versions_svc_quant_select
  on quant.universe_versions;
create policy universe_versions_svc_quant_select
  on quant.universe_versions for select to svc_quant using (true);

drop policy if exists universe_members_svc_quant_select
  on quant.universe_members;
create policy universe_members_svc_quant_select
  on quant.universe_members for select to svc_quant using (true);

drop policy if exists dataset_manifests_svc_quant_select
  on quant.dataset_manifests;
create policy dataset_manifests_svc_quant_select
  on quant.dataset_manifests for select to svc_quant using (true);

drop policy if exists dataset_partitions_svc_quant_select
  on quant.dataset_partitions;
create policy dataset_partitions_svc_quant_select
  on quant.dataset_partitions for select to svc_quant using (true);

grant select on
  quant.universe_versions,
  quant.universe_members,
  quant.dataset_manifests,
  quant.dataset_partitions
to svc_quant;

-- Dataset builders are administrative/manual factory-maintenance paths outside
-- the scoped experiment role.  This migration does not authorize them through
-- svc_quant.  Remove the broad legacy writes so an execution worker cannot
-- rewrite a frozen universe, manifest, or partition to change an experiment.
revoke insert, update, delete, truncate on
  quant.universe_versions,
  quant.universe_members,
  quant.dataset_manifests,
  quant.dataset_partitions
from svc_quant;

-- Mutable orchestration state.  These policies match the direct SQL in
-- factory_bridge, experiment_orchestrator, backtest_runner, and the intraday
-- runner.  Existing SELECT/UPDATE policies are retained where already present.
drop policy if exists hypotheses_svc_quant_insert on quant.hypotheses;
create policy hypotheses_svc_quant_insert
  on quant.hypotheses for insert to svc_quant with check (true);

drop policy if exists experiments_svc_quant_insert on quant.experiments;
create policy experiments_svc_quant_insert
  on quant.experiments for insert to svc_quant with check (true);

drop policy if exists experiments_svc_quant_update on quant.experiments;
create policy experiments_svc_quant_update
  on quant.experiments for update to svc_quant
  using (true) with check (true);

-- The role bootstrap granted table-wide UPDATE.  Replace it with the exact
-- lifecycle fields exercised by the runners.  Experimental identity and
-- preregistered inputs (notably config, dataset_id, and input_hash) stay sealed.
revoke update on
  quant.hypotheses,
  quant.experiments
from svc_quant;
grant select, insert on
  quant.hypotheses,
  quant.experiments
to svc_quant;
grant update (
  status,
  status_changed_at,
  expected_edge,
  preregistered_at,
  material_fingerprint
) on quant.hypotheses to svc_quant;
grant update (
  status,
  started_at,
  ended_at,
  trace_id,
  trial_family_id,
  trial_number
) on quant.experiments to svc_quant;
revoke delete, truncate on
  quant.hypotheses,
  quant.experiments
from svc_quant;

-- Backtest evidence is append-only from the runtime's perspective.  A worker
-- reads run existence for idempotent recovery, appends one run, and appends its
-- fills.  It never edits or deletes scientific evidence and it does not need a
-- read policy over the multi-million-row fill ledger.
drop policy if exists backtest_runs_svc_quant_select on quant.backtest_runs;
create policy backtest_runs_svc_quant_select
  on quant.backtest_runs for select to svc_quant using (true);

drop policy if exists backtest_runs_svc_quant_insert on quant.backtest_runs;
create policy backtest_runs_svc_quant_insert
  on quant.backtest_runs for insert to svc_quant with check (true);

drop policy if exists backtest_trades_svc_quant_insert on quant.backtest_trades;
create policy backtest_trades_svc_quant_insert
  on quant.backtest_trades for insert to svc_quant with check (true);

grant select, insert on quant.backtest_runs to svc_quant;
grant insert on quant.backtest_trades to svc_quant;
revoke update, delete, truncate on quant.backtest_runs from svc_quant;
revoke select, update, delete, truncate on quant.backtest_trades from svc_quant;

-- Deterministic finalization appends one feedback row after the experiment
-- result is sealed.  The current-outcome view remains the read surface; no
-- update/delete privilege is introduced and no QA transport table is touched.
grant select, insert on research.experiment_outcomes to svc_quant;
revoke update, delete, truncate on research.experiment_outcomes from svc_quant;

do $quant_runtime_rls_audit$
declare
  immutable_table text;
  required_policy text;
begin
  foreach immutable_table in array array[
    'quant.universe_versions',
    'quant.universe_members',
    'quant.dataset_manifests',
    'quant.dataset_partitions'
  ] loop
    if not has_table_privilege('svc_quant', immutable_table, 'SELECT') then
      raise exception 'svc_quant cannot read immutable catalog table %',
        immutable_table;
    end if;
    if has_table_privilege('svc_quant', immutable_table, 'INSERT')
       or has_table_privilege('svc_quant', immutable_table, 'UPDATE')
       or has_table_privilege('svc_quant', immutable_table, 'DELETE')
       or has_table_privilege('svc_quant', immutable_table, 'TRUNCATE') then
      raise exception 'svc_quant can mutate immutable catalog table %',
        immutable_table;
    end if;
  end loop;

  foreach required_policy in array array[
    'universe_versions_svc_quant_select',
    'universe_members_svc_quant_select',
    'dataset_manifests_svc_quant_select',
    'dataset_partitions_svc_quant_select',
    'hypotheses_svc_quant_insert',
    'hypotheses_svc_quant_select',
    'hypotheses_svc_quant_update',
    'experiments_svc_quant_insert',
    'experiments_svc_quant_select',
    'experiments_svc_quant_update',
    'experiment_metrics_svc_quant_select',
    'experiment_metrics_svc_quant_insert',
    'experiment_metrics_svc_quant_update',
    'backtest_runs_svc_quant_select',
    'backtest_runs_svc_quant_insert',
    'backtest_trades_svc_quant_insert'
  ] loop
    if not exists (
      select 1 from pg_policies where policyname = required_policy
        and 'svc_quant' = any(roles)
    ) then
      raise exception 'required svc_quant RLS policy is missing: %',
        required_policy;
    end if;
  end loop;

  if not has_table_privilege('svc_quant', 'quant.hypotheses', 'INSERT')
     or not has_column_privilege(
       'svc_quant', 'quant.hypotheses', 'status', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.hypotheses', 'status_changed_at', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.hypotheses', 'expected_edge', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.hypotheses', 'preregistered_at', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.hypotheses', 'material_fingerprint', 'UPDATE')
     or not has_table_privilege('svc_quant', 'quant.experiments', 'INSERT')
     or not has_column_privilege(
       'svc_quant', 'quant.experiments', 'status', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.experiments', 'started_at', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.experiments', 'ended_at', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.experiments', 'trace_id', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.experiments', 'trial_family_id', 'UPDATE')
     or not has_column_privilege(
       'svc_quant', 'quant.experiments', 'trial_number', 'UPDATE')
     or not has_table_privilege(
       'svc_quant', 'quant.experiment_metrics', 'SELECT')
     or not has_table_privilege(
       'svc_quant', 'quant.experiment_metrics', 'INSERT')
     or not has_table_privilege(
       'svc_quant', 'quant.experiment_metrics', 'UPDATE')
     or not has_table_privilege('svc_quant', 'quant.backtest_runs', 'SELECT')
     or not has_table_privilege('svc_quant', 'quant.backtest_runs', 'INSERT')
     or not has_table_privilege('svc_quant', 'quant.backtest_trades', 'INSERT')
     or not has_table_privilege(
       'svc_quant', 'research.experiment_outcomes', 'INSERT') then
    raise exception 'svc_quant lacks a required factory execution privilege';
  end if;

  if has_table_privilege('svc_quant', 'quant.hypotheses', 'UPDATE')
     or has_table_privilege('svc_quant', 'quant.experiments', 'UPDATE')
     or has_column_privilege(
       'svc_quant', 'quant.experiments', 'config', 'UPDATE')
     or has_column_privilege(
       'svc_quant', 'quant.experiments', 'dataset_id', 'UPDATE')
     or has_column_privilege(
       'svc_quant', 'quant.experiments', 'input_hash', 'UPDATE') then
    raise exception 'svc_quant can rewrite sealed experimental identity';
  end if;

  if has_table_privilege('svc_quant', 'quant.hypotheses', 'DELETE')
     or has_table_privilege('svc_quant', 'quant.experiments', 'DELETE')
     or has_table_privilege('svc_quant', 'quant.backtest_runs', 'DELETE')
     or has_table_privilege('svc_quant', 'quant.backtest_trades', 'DELETE')
     or has_table_privilege(
       'svc_quant', 'research.experiment_outcomes', 'DELETE') then
    raise exception 'svc_quant retains destructive scientific-table access';
  end if;

  -- Preserve the QA role split installed by migrations 004-007.  This repair
  -- must never turn a scientific worker into a relay or reproduction worker.
  if has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_outbox', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_delivery_state', 'UPDATE')
     or has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_dispatches', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'audit.intraday_forward_reproduction_work_items', 'UPDATE')
  then
    raise exception 'svc_quant exceeds the quant/QA separation boundary';
  end if;

  if not exists (
    select 1
      from pg_policies
     where schemaname = 'reference'
       and tablename = 'instruments'
       and policyname = 'reference_instruments_svc_quant_stock_only_select'
       and cmd = 'SELECT'
       and 'svc_quant' = any(roles)
  ) then
    raise exception 'the governed KRX ACTIVE STOCK reference boundary is missing';
  end if;
end
$quant_runtime_rls_audit$;

comment on policy dataset_manifests_svc_quant_select
  on quant.dataset_manifests is
  'Read-only immutable dataset catalog for svc_quant; executable rows still require the reference-instrument KRX ACTIVE EQUITY/STOCK attestation.';
comment on policy universe_members_svc_quant_select
  on quant.universe_members is
  'Complete immutable membership visibility prevents mixed-product universes from being made stock-only by row filtering; reference metadata remains stock-scoped by RLS.';
comment on policy backtest_trades_svc_quant_insert
  on quant.backtest_trades is
  'Append-only fill evidence for the quant runtime. svc_quant has no SELECT, UPDATE, DELETE, or TRUNCATE privilege on this ledger.';

commit;
