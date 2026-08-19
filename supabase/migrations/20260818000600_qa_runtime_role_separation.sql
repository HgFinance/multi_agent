begin;

-- The transitional compatibility service_role has grants across unrelated
-- domains. QA HTTP and relay processes instead select separate NOLOGIN roles
-- whose direct grants and row policies match their actual SQL surfaces.

do $qa_runtime_roles$
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_audit_api') then
    create role svc_audit_api
      nologin nosuperuser nocreatedb nocreaterole noinherit
      noreplication nobypassrls;
  elsif exists (
    select 1 from pg_roles
     where rolname = 'svc_audit_api'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_audit_api role name is occupied by an unsafe role';
  end if;

  if not exists (select 1 from pg_roles where rolname = 'svc_qa_worker') then
    create role svc_qa_worker
      nologin nosuperuser nocreatedb nocreaterole noinherit
      noreplication nobypassrls;
  elsif exists (
    select 1 from pg_roles
     where rolname = 'svc_qa_worker'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_qa_worker role name is occupied by an unsafe role';
  end if;

  if exists (
    select 1
      from pg_auth_members membership
      join pg_roles member_role on member_role.oid = membership.member
     where member_role.rolname in ('svc_audit_api', 'svc_qa_worker')
  ) then
    raise exception 'a QA runtime role is already a member of another role';
  end if;
end
$qa_runtime_roles$;

revoke all privileges on all tables in schema audit from svc_audit_api;
revoke all privileges on all tables in schema quant from svc_audit_api;
revoke all privileges on all sequences in schema audit from svc_audit_api;
revoke all privileges on all sequences in schema quant from svc_audit_api;
revoke all privileges on all tables in schema audit from svc_qa_worker;
revoke all privileges on all tables in schema quant from svc_qa_worker;
revoke all privileges on all sequences in schema audit from svc_qa_worker;
revoke all privileges on all sequences in schema quant from svc_qa_worker;
revoke all on schema audit, quant from svc_audit_api, svc_qa_worker;

do $qa_runtime_pool_memberships$
declare
  pool_login name := session_user;
begin
  execute format(
    'grant svc_audit_api to %I with set true, inherit false', pool_login
  );
  execute format(
    'grant svc_qa_worker to %I with set true, inherit false', pool_login
  );
end
$qa_runtime_pool_memberships$;

grant usage on schema audit to svc_audit_api;
grant select, insert on audit.domain_events to svc_audit_api;
grant insert on audit.qa_decisions to svc_audit_api;
grant select (qa_decision_id) on audit.qa_decisions to svc_audit_api;
grant insert on audit.claim_checks, audit.findings, audit.tool_calls,
  audit.incidents, audit.incident_events to svc_audit_api;
grant insert on audit.agent_runs to svc_audit_api;
grant select (agent_run_id) on audit.agent_runs to svc_audit_api;
grant update (
  status, ended_at, error_code, token_usage, cost,
  output_artifact_version_id, trace_uri
) on audit.agent_runs to svc_audit_api;
grant insert on audit.corrective_actions to svc_audit_api;
grant select (corrective_action_id) on audit.corrective_actions
  to svc_audit_api;
grant update (status, verification, verifier, completed_at)
  on audit.corrective_actions to svc_audit_api;
grant select, insert on audit.eval_sets, audit.eval_results,
  audit.eval_comparisons to svc_audit_api;
grant select, insert on audit.eval_runs to svc_audit_api;
grant update (status, ended_at) on audit.eval_runs to svc_audit_api;

grant usage on schema audit, quant to svc_qa_worker;
grant select on quant.intraday_forward_qa_outbox to svc_qa_worker;
grant select on quant.intraday_forward_qa_delivery_state to svc_qa_worker;
grant update (
  status, attempt_count, available_at, last_error, sent_at, updated_at
) on quant.intraday_forward_qa_delivery_state to svc_qa_worker;
grant insert on quant.intraday_forward_qa_dispatches to svc_qa_worker;
grant select, insert on audit.domain_events to svc_qa_worker;
grant select, insert on audit.intraday_forward_reproduction_requests
  to svc_qa_worker;
grant insert on audit.intraday_forward_reproduction_work_items
  to svc_qa_worker;

-- Existing fund-member policies were implicitly PUBLIC.  Keep the intended
-- browser/JWT audience while preventing them from being evaluated for the
-- new service roles.
drop policy if exists audit_traces_fund_member_select on audit.traces;
create policy audit_traces_fund_member_select on audit.traces
  for select to authenticated
  using (fund_id is not null and governance.can_access_fund(fund_id));
drop policy if exists audit_findings_fund_member_select on audit.findings;
create policy audit_findings_fund_member_select on audit.findings
  for select to authenticated
  using (fund_id is not null and governance.can_access_fund(fund_id));
drop policy if exists audit_incidents_fund_member_select on audit.incidents;
create policy audit_incidents_fund_member_select on audit.incidents
  for select to authenticated
  using (fund_id is not null and governance.can_access_fund(fund_id));
drop policy if exists audit_agent_runs_fund_member_select on audit.agent_runs;
create policy audit_agent_runs_fund_member_select on audit.agent_runs
  for select to authenticated
  using (fund_id is not null and governance.can_access_fund(fund_id));
drop policy if exists audit_tool_calls_fund_member_select on audit.tool_calls;
create policy audit_tool_calls_fund_member_select on audit.tool_calls
  for select to authenticated
  using (exists (
    select 1 from audit.agent_runs run
     where run.agent_run_id = tool_calls.agent_run_id
       and run.fund_id is not null
       and governance.can_access_fund(run.fund_id)
  ));

alter table audit.domain_events enable row level security;

drop policy if exists audit_domain_events_svc_audit_api_select
  on audit.domain_events;
create policy audit_domain_events_svc_audit_api_select on audit.domain_events
  for select to svc_audit_api
  using (
    event_type = 'risk.decision.v1'
    and source_department = 'risk-management'
    and status = 'PROCESSED'
  );
drop policy if exists audit_domain_events_svc_audit_api_insert
  on audit.domain_events;
create policy audit_domain_events_svc_audit_api_insert on audit.domain_events
  for insert to svc_audit_api
  with check (
    event_type = 'risk.decision.v1'
    and source_department = 'risk-management'
    and status = 'PROCESSED'
  );
drop policy if exists audit_domain_events_svc_qa_worker_select
  on audit.domain_events;
create policy audit_domain_events_svc_qa_worker_select on audit.domain_events
  for select to svc_qa_worker
  using (
    status = 'PROCESSED'
    and (
      (event_type = 'risk.decision.v1'
       and source_department = 'risk-management')
      or
      (event_type = 'quant.intraday.forward.qa_requested.v1'
       and source_department = 'quant-backtest-department')
    )
  );
drop policy if exists audit_domain_events_svc_qa_worker_insert
  on audit.domain_events;
create policy audit_domain_events_svc_qa_worker_insert on audit.domain_events
  for insert to svc_qa_worker
  with check (
    status = 'PROCESSED'
    and (
      (event_type = 'risk.decision.v1'
       and source_department = 'risk-management')
      or
      (event_type = 'quant.intraday.forward.qa_requested.v1'
       and source_department = 'quant-backtest-department')
    )
  );

do $qa_runtime_policies$
declare
  table_name text;
begin
  foreach table_name in array array[
    'qa_decisions', 'claim_checks', 'findings', 'agent_runs', 'tool_calls',
    'incidents', 'incident_events', 'corrective_actions', 'eval_sets',
    'eval_runs', 'eval_results', 'eval_comparisons'
  ] loop
    execute format('alter table audit.%I enable row level security', table_name);
    execute format('drop policy if exists %I on audit.%I',
                   'audit_' || table_name || '_svc_audit_api_all', table_name);
    execute format(
      'create policy %I on audit.%I for all to svc_audit_api '
      'using (true) with check (true)',
      'audit_' || table_name || '_svc_audit_api_all', table_name);
  end loop;
end
$qa_runtime_policies$;

drop policy if exists intraday_forward_qa_outbox_svc_qa_worker_select
  on quant.intraday_forward_qa_outbox;
create policy intraday_forward_qa_outbox_svc_qa_worker_select
  on quant.intraday_forward_qa_outbox
  for select to svc_qa_worker using (true);
drop policy if exists intraday_forward_qa_delivery_svc_qa_worker_all
  on quant.intraday_forward_qa_delivery_state;
create policy intraday_forward_qa_delivery_svc_qa_worker_all
  on quant.intraday_forward_qa_delivery_state
  for all to svc_qa_worker using (true) with check (true);
drop policy if exists intraday_forward_qa_dispatch_svc_qa_worker_insert
  on quant.intraday_forward_qa_dispatches;
create policy intraday_forward_qa_dispatch_svc_qa_worker_insert
  on quant.intraday_forward_qa_dispatches
  for insert to svc_qa_worker
  with check (dispatched_by = 'qa-worker/forward-dispatch-v2');

drop policy if exists intraday_forward_reproduction_svc_qa_worker_all
  on audit.intraday_forward_reproduction_requests;
create policy intraday_forward_reproduction_svc_qa_worker_all
  on audit.intraday_forward_reproduction_requests
  for all to svc_qa_worker
  using (accepted_by = 'qa-forward-consumer/v1')
  with check (
    accepted_by = 'qa-forward-consumer/v1'
    and decision = 'PASS'
    and hypothesis_status = 'SUPPORTED'
    and asset_class = 'EQUITY'
    and instrument_type = 'STOCK'
  );
drop policy if exists intraday_forward_work_svc_qa_worker_insert
  on audit.intraday_forward_reproduction_work_items;
create policy intraday_forward_work_svc_qa_worker_insert
  on audit.intraday_forward_reproduction_work_items
  for insert to svc_qa_worker
  with check (
    status = 'READY'
    and attempt_count = 0
    and leased_by is null
    and lease_expires_at is null
    and last_error is null
  );

do $qa_runtime_role_audit$
declare
  unsafe_role text;
  pool_login name := session_user;
begin
  select rolname into unsafe_role
    from pg_roles
   where rolname in ('svc_audit_api', 'svc_qa_worker')
     and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
          or rolinherit or rolreplication or rolbypassrls)
   limit 1;
  if unsafe_role is not null then
    raise exception 'unsafe QA runtime role attributes: %', unsafe_role;
  end if;

  if exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid = membership.roleid
      join pg_roles member_role on member_role.oid = membership.member
     where granted_role.rolname in ('svc_audit_api', 'svc_qa_worker')
       and member_role.rolname = pool_login
       and membership.inherit_option
  ) or (
    select count(distinct granted_role.rolname)
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid = membership.roleid
      join pg_roles member_role on member_role.oid = membership.member
     where granted_role.rolname in ('svc_audit_api', 'svc_qa_worker')
       and member_role.rolname = pool_login
       and membership.set_option and not membership.inherit_option
  ) <> 2 then
    raise exception '% QA runtime role selection is not fail-closed', pool_login;
  end if;

  if exists (
    select 1 from information_schema.role_table_grants
     where grantee = 'svc_audit_api' and table_schema <> 'audit'
  ) then
    raise exception 'svc_audit_api has a non-audit direct table grant';
  end if;
  if exists (
    select 1 from information_schema.role_table_grants
     where grantee = 'svc_qa_worker'
       and table_schema = 'audit'
       and table_name not in (
         'domain_events', 'intraday_forward_reproduction_requests',
         'intraday_forward_reproduction_work_items'
       )
  ) then
    raise exception 'svc_qa_worker has a QA decision/eval/incident grant';
  end if;
  if exists (
    select 1 from information_schema.role_table_grants
     where grantee in ('svc_audit_api', 'svc_qa_worker')
       and privilege_type in ('DELETE', 'TRUNCATE')
  ) then
    raise exception 'a QA runtime role has destructive table privilege';
  end if;
  if has_table_privilege(
       'svc_qa_worker', 'quant.intraday_forward_qa_outbox', 'INSERT')
     or has_table_privilege(
       'svc_qa_worker', 'quant.intraday_forward_qa_outbox', 'UPDATE')
     or has_sequence_privilege(
       'svc_qa_worker',
       'quant.intraday_forward_qa_outbox_outbox_id_seq', 'USAGE')
     or has_table_privilege(
       'svc_qa_worker',
       'audit.intraday_forward_reproduction_work_items', 'UPDATE') then
    raise exception 'svc_qa_worker exceeds its append/relay boundary';
  end if;

  if not has_table_privilege(
       'svc_audit_api', 'audit.qa_decisions', 'INSERT')
     or not has_table_privilege(
       'svc_qa_worker', 'quant.intraday_forward_qa_outbox', 'SELECT')
     or not has_column_privilege(
       'svc_qa_worker', 'quant.intraday_forward_qa_delivery_state',
       'status', 'UPDATE')
     or not has_table_privilege(
       'svc_qa_worker', 'quant.intraday_forward_qa_dispatches', 'INSERT')
     or not has_table_privilege(
       'svc_qa_worker',
       'audit.intraday_forward_reproduction_requests', 'INSERT') then
    raise exception 'a QA runtime role lacks a required privilege';
  end if;

  if exists (
    select 1
      from unnest(array[
        'domain_events', 'qa_decisions', 'claim_checks', 'findings',
        'agent_runs', 'tool_calls', 'incidents', 'incident_events',
        'corrective_actions', 'eval_sets', 'eval_runs', 'eval_results',
        'eval_comparisons', 'intraday_forward_reproduction_requests',
        'intraday_forward_reproduction_work_items'
      ]) required(table_name)
      left join pg_class relation on relation.relname = required.table_name
      left join pg_namespace namespace
        on namespace.oid = relation.relnamespace
       and namespace.nspname = 'audit'
     where relation.oid is null or not relation.relrowsecurity
  ) then
    raise exception 'an accessed audit table is missing RLS';
  end if;

  if (
    select count(*) from pg_policies
     where schemaname in ('audit', 'quant')
       and roles && array['svc_audit_api', 'svc_qa_worker']::name[]
  ) < 12 then
    raise exception 'QA runtime role policies are incomplete';
  end if;
end
$qa_runtime_role_audit$;

commit;
