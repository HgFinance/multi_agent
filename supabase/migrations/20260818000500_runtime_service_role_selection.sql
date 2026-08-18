begin;

-- Runtime services connect through the managed transaction-pool credential,
-- then deliberately reduce privilege with SET ROLE before application work.
-- PostgreSQL 16+ separates membership from permission to SET ROLE; the
-- existing svc_quant membership intentionally had SET FALSE, so merely naming
-- the role in Compose could not enforce the boundary.

do $runtime_service_role_membership$
begin
  if not exists (select 1 from pg_roles where rolname = 'postgres') then
    raise exception 'postgres pool login role is missing';
  end if;
  if not exists (select 1 from pg_roles where rolname = 'svc_quant') then
    raise exception 'svc_quant runtime role is missing';
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    raise exception 'service_role QA runtime role is missing';
  end if;

  -- This grants only permission to reduce the already broader postgres pool
  -- session to svc_quant. INHERIT stays false, so the login does not silently
  -- acquire svc_quant semantics without an explicit SET ROLE.
  grant svc_quant to postgres with set true, inherit false;

  if not exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid = membership.roleid
      join pg_roles member_role on member_role.oid = membership.member
     where granted_role.rolname = 'svc_quant'
       and member_role.rolname = 'postgres'
       and membership.set_option
       and not membership.inherit_option
  ) then
    raise exception 'postgres cannot explicitly reduce to non-inherited svc_quant';
  end if;

  -- Supabase owns this standard membership. Do not mutate it here; fail the
  -- deployment if the pool credential cannot reduce to the QA service role.
  if not exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid = membership.roleid
      join pg_roles member_role on member_role.oid = membership.member
     where granted_role.rolname = 'service_role'
       and member_role.rolname = 'postgres'
       and membership.set_option
  ) then
    raise exception 'postgres cannot explicitly reduce to service_role';
  end if;
end
$runtime_service_role_membership$;

do $runtime_service_role_privilege_audit$
begin
  if has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_outbox', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_delivery_state', 'UPDATE')
     or has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_dispatches', 'INSERT') then
    raise exception 'svc_quant retains a direct QA transport write path';
  end if;
  if not has_table_privilege(
       'service_role', 'quant.intraday_forward_qa_outbox', 'SELECT')
     or not has_table_privilege(
       'service_role', 'quant.intraday_forward_qa_delivery_state', 'UPDATE')
     or not has_table_privilege(
       'service_role', 'quant.intraday_forward_qa_dispatches', 'INSERT') then
    raise exception 'service_role lacks the QA relay privileges';
  end if;
end
$runtime_service_role_privilege_audit$;

commit;
