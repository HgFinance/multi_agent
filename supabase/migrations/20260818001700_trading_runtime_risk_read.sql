begin;

-- The durable Trading readiness probe and canonical risk-evidence validation
-- join these three Risk-owned relations.  The dedicated Trading capability
-- remains read-only in Risk: it may verify an upstream decision, but it can
-- never author, alter, or delete one.
do $trading_risk_role$
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_trading_api') then
    raise exception 'svc_trading_api role is missing';
  end if;
  if exists (
    select 1 from pg_roles
     where rolname = 'svc_trading_api'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_trading_api role is unsafe';
  end if;
end
$trading_risk_role$;

grant usage on schema risk to svc_trading_api;
grant select on risk.risk_decisions, risk.risk_requests,
  risk.risk_request_items to svc_trading_api;

drop policy if exists risk_decisions_svc_trading_api_select
  on risk.risk_decisions;
create policy risk_decisions_svc_trading_api_select on risk.risk_decisions
  for select to svc_trading_api using (true);

drop policy if exists risk_requests_svc_trading_api_select
  on risk.risk_requests;
create policy risk_requests_svc_trading_api_select on risk.risk_requests
  for select to svc_trading_api using (true);

drop policy if exists risk_request_items_svc_trading_api_select
  on risk.risk_request_items;
create policy risk_request_items_svc_trading_api_select
  on risk.risk_request_items
  for select to svc_trading_api using (true);

do $trading_risk_audit$
declare
  relation_name text;
begin
  if not has_schema_privilege('svc_trading_api', 'risk', 'USAGE') then
    raise exception 'svc_trading_api lacks Risk schema usage';
  end if;
  foreach relation_name in array array[
    'risk_decisions', 'risk_requests', 'risk_request_items'
  ] loop
    if not has_table_privilege(
      'svc_trading_api', 'risk.' || relation_name, 'SELECT'
    ) then
      raise exception 'svc_trading_api lacks required Risk evidence read';
    end if;
    if has_table_privilege(
      'svc_trading_api', 'risk.' || relation_name, 'INSERT'
    ) or has_table_privilege(
      'svc_trading_api', 'risk.' || relation_name, 'UPDATE'
    ) or has_table_privilege(
      'svc_trading_api', 'risk.' || relation_name, 'DELETE'
    ) then
      raise exception 'svc_trading_api exceeds the Risk evidence boundary';
    end if;
  end loop;
end
$trading_risk_audit$;

commit;
