begin;

-- Dataset publication is an administrative factory capability. It is separate
-- from svc_quant so experiment workers cannot rewrite frozen dataset identity.
do $dataset_builder_role$
begin
  if not exists (
    select 1 from pg_roles where rolname = 'svc_dataset_builder'
  ) then
    create role svc_dataset_builder
      nologin nosuperuser nocreatedb nocreaterole noinherit
      noreplication nobypassrls;
  elsif exists (
    select 1
      from pg_roles
     where rolname = 'svc_dataset_builder'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_dataset_builder role name is occupied by an unsafe role';
  end if;

  if exists (
    select 1
      from pg_auth_members membership
      join pg_roles member_role on member_role.oid = membership.member
     where member_role.rolname = 'svc_dataset_builder'
  ) then
    raise exception 'svc_dataset_builder must not inherit or SET another role';
  end if;
end
$dataset_builder_role$;

grant usage on schema quant, reference to svc_dataset_builder;

revoke all on
  quant.universe_versions,
  quant.universe_members,
  quant.dataset_manifests,
  quant.dataset_partitions
from svc_dataset_builder;

grant select, insert on
  quant.universe_versions,
  quant.universe_members,
  quant.dataset_manifests,
  quant.dataset_partitions
to svc_dataset_builder;
grant delete on quant.dataset_partitions to svc_dataset_builder;
grant update (rules)
  on quant.universe_versions to svc_dataset_builder;
grant update (
  as_of, quality_summary, partitions, row_count, object_path, content_hash,
  source_versions, notional_unit
) on quant.dataset_manifests to svc_dataset_builder;
grant update (
  object_path, row_count, min_event_time, max_event_time, content_hash,
  quality_status
) on quant.dataset_partitions to svc_dataset_builder;

create policy universe_versions_svc_dataset_builder_select
  on quant.universe_versions for select to svc_dataset_builder using (true);
create policy universe_versions_svc_dataset_builder_insert
  on quant.universe_versions for insert to svc_dataset_builder with check (true);
create policy universe_versions_svc_dataset_builder_update
  on quant.universe_versions for update to svc_dataset_builder
  using (true) with check (true);
create policy universe_members_svc_dataset_builder_select
  on quant.universe_members for select to svc_dataset_builder using (true);
create policy universe_members_svc_dataset_builder_insert
  on quant.universe_members for insert to svc_dataset_builder with check (true);
create policy dataset_manifests_svc_dataset_builder_select
  on quant.dataset_manifests for select to svc_dataset_builder using (true);
create policy dataset_manifests_svc_dataset_builder_insert
  on quant.dataset_manifests for insert to svc_dataset_builder with check (true);
create policy dataset_manifests_svc_dataset_builder_update
  on quant.dataset_manifests for update to svc_dataset_builder
  using (true) with check (true);
create policy dataset_partitions_svc_dataset_builder_select
  on quant.dataset_partitions for select to svc_dataset_builder using (true);
create policy dataset_partitions_svc_dataset_builder_insert
  on quant.dataset_partitions for insert to svc_dataset_builder with check (true);
create policy dataset_partitions_svc_dataset_builder_update
  on quant.dataset_partitions for update to svc_dataset_builder
  using (true) with check (true);
create policy dataset_partitions_svc_dataset_builder_delete
  on quant.dataset_partitions for delete to svc_dataset_builder using (true);

revoke all on
  reference.instruments,
  reference.market_calendar_versions,
  reference.market_sessions,
  quant.current_krx_stock_instrument_identity
from svc_dataset_builder;
grant select on quant.current_krx_stock_instrument_identity
  to svc_dataset_builder;
grant select (
  calendar_version_id, market, version, published_at, effective_from,
  effective_to, content_hash, created_at
) on reference.market_calendar_versions to svc_dataset_builder;
grant select (
  calendar_version_id, market, trade_date, session_type, opens_at, closes_at,
  is_trading_day
) on reference.market_sessions to svc_dataset_builder;
create policy market_calendar_versions_svc_dataset_builder_krx_select
  on reference.market_calendar_versions for select to svc_dataset_builder
  using (market = 'KRX');
create policy market_sessions_svc_dataset_builder_krx_select
  on reference.market_sessions for select to svc_dataset_builder
  using (market = 'KRX');

do $dataset_builder_privilege_audit$
declare
  required_relation text;
  required_update record;
begin
  foreach required_relation in array array[
    'quant.universe_versions',
    'quant.universe_members',
    'quant.dataset_manifests',
    'quant.dataset_partitions'
  ] loop
    if not has_table_privilege(
         'svc_dataset_builder', required_relation, 'SELECT')
       or not has_table_privilege(
         'svc_dataset_builder', required_relation, 'INSERT') then
      raise exception 'dataset builder cannot publish %', required_relation;
    end if;
    if has_table_privilege(
         'svc_dataset_builder', required_relation, 'TRUNCATE') then
      raise exception 'dataset builder can truncate %', required_relation;
    end if;
  end loop;

  if not has_table_privilege(
       'svc_dataset_builder', 'quant.dataset_partitions', 'DELETE')
     or has_table_privilege(
       'svc_dataset_builder', 'quant.dataset_manifests', 'DELETE')
     or has_table_privilege(
       'svc_dataset_builder', 'quant.universe_versions', 'DELETE')
     or has_table_privilege(
       'svc_dataset_builder', 'quant.universe_members', 'DELETE') then
    raise exception 'dataset builder delete boundary is invalid';
  end if;

  for required_update in
    select * from (values
      ('quant.universe_versions', 'rules'),
      ('quant.dataset_manifests', 'as_of'),
      ('quant.dataset_manifests', 'content_hash'),
      ('quant.dataset_partitions', 'object_path'),
      ('quant.dataset_partitions', 'content_hash')
    ) as required(relation_name, column_name)
  loop
    if not has_column_privilege(
         'svc_dataset_builder', required_update.relation_name,
         required_update.column_name, 'UPDATE') then
      raise exception 'dataset builder cannot update %.%',
        required_update.relation_name, required_update.column_name;
    end if;
  end loop;

  if has_column_privilege(
       'svc_dataset_builder', 'quant.dataset_manifests', 'name', 'UPDATE')
     or has_column_privilege(
       'svc_dataset_builder', 'quant.dataset_manifests', 'version', 'UPDATE')
     or has_column_privilege(
       'svc_dataset_builder', 'quant.dataset_partitions',
       'dataset_id', 'UPDATE')
     or has_column_privilege(
       'svc_dataset_builder', 'quant.dataset_partitions',
       'partition_key', 'UPDATE')
     or has_table_privilege(
       'svc_dataset_builder', 'reference.instruments', 'SELECT') then
    raise exception 'dataset builder crosses sealed identity boundary';
  end if;

  if not has_table_privilege(
       'svc_dataset_builder',
       'quant.current_krx_stock_instrument_identity', 'SELECT')
     or not has_column_privilege(
       'svc_dataset_builder', 'reference.market_sessions',
       'trade_date', 'SELECT') then
    raise exception 'dataset builder lacks governed stock/calendar reads';
  end if;
end
$dataset_builder_privilege_audit$;

comment on role svc_dataset_builder is
  'SET ROLE capability for deterministic dataset/universe publication; no experiment execution or raw instrument metadata access.';

commit;
