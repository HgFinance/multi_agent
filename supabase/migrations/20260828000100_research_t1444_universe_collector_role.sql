begin;

-- The daily t1444 collector publishes only governed PIT-universe manifests.
-- It may append/update its own manifest rows, but it must not delete or alter
-- the historical membership ledger.
grant usage on schema quant to svc_research_collector;
grant select, insert on quant.universe_versions to svc_research_collector;
grant select, insert on quant.universe_members to svc_research_collector;

drop policy if exists quant_universe_versions_svc_research_collector_select
  on quant.universe_versions;
create policy quant_universe_versions_svc_research_collector_select
  on quant.universe_versions for select to svc_research_collector
  using (true);

drop policy if exists quant_universe_versions_svc_research_collector_insert
  on quant.universe_versions;
create policy quant_universe_versions_svc_research_collector_insert
  on quant.universe_versions for insert to svc_research_collector
  with check (true);

drop policy if exists quant_universe_members_svc_research_collector_select
  on quant.universe_members;
create policy quant_universe_members_svc_research_collector_select
  on quant.universe_members for select to svc_research_collector
  using (true);

drop policy if exists quant_universe_members_svc_research_collector_insert
  on quant.universe_members;
create policy quant_universe_members_svc_research_collector_insert
  on quant.universe_members for insert to svc_research_collector
  with check (true);

comment on table quant.universe_versions is
  'Immutable universe versions; t1444 collector appends forward-only observation snapshots.';

commit;
