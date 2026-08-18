begin;

-- Runtime services connect through the managed transaction-pool credential,
-- then deliberately reduce privilege with SET ROLE before application work.
-- PostgreSQL 16+ separates membership from permission to SET ROLE; the
-- existing svc_quant membership intentionally had SET FALSE, so merely naming
-- the role in Compose could not enforce the boundary.

do $runtime_service_role_membership$
declare
  pool_login name := session_user;
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_quant') then
    raise exception 'svc_quant runtime role is missing';
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    raise exception 'service_role QA runtime role is missing';
  end if;

  -- This grants only permission to reduce the migration/session pool login
  -- session to svc_quant. INHERIT stays false, so the login does not silently
  -- acquire svc_quant semantics without an explicit SET ROLE.
  execute format(
    'grant svc_quant to %I with set true, inherit false', pool_login
  );

  if not exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid = membership.roleid
      join pg_roles member_role on member_role.oid = membership.member
     where granted_role.rolname = 'svc_quant'
       and member_role.rolname = pool_login
       and membership.set_option
       and not membership.inherit_option
  ) then
    raise exception '% cannot explicitly reduce to non-inherited svc_quant',
      pool_login;
  end if;

  -- On the private control database service_role is an inert NOLOGIN
  -- compatibility role created by the foundation migration.  Preserve the
  -- transitional relay boundary until migration 006 installs the dedicated
  -- svc_qa_worker role.
  execute format(
    'grant service_role to %I with set true, inherit false', pool_login
  );

  if not exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid = membership.roleid
      join pg_roles member_role on member_role.oid = membership.member
     where granted_role.rolname = 'service_role'
       and member_role.rolname = pool_login
       and membership.set_option
  ) then
    raise exception '% cannot explicitly reduce to service_role', pool_login;
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
