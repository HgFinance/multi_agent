begin;

-- The notification consumer resolves a minimal rule -> admitted request
-- correlation before reading Trading status.  Some environments predate the
-- compound-order migration that first granted this read, so repair the exact
-- existing worker boundary without adding any write or submission authority.
grant usage on schema execution to svc_conditional_rule_worker;
grant select on execution.user_order_requests to svc_conditional_rule_worker;

do $policy$
begin
  if not exists (
    select 1
      from pg_policy p
      join pg_class c on c.oid = p.polrelid
      join pg_namespace n on n.oid = c.relnamespace
     where n.nspname = 'execution'
       and c.relname = 'user_order_requests'
       and p.polname = 'user_order_requests_conditional_worker_read'
  ) then
    create policy user_order_requests_conditional_worker_read
      on execution.user_order_requests for select
      to svc_conditional_rule_worker using (true);
  end if;
end
$policy$;

do $privilege_audit$
begin
  if not has_table_privilege(
    'svc_conditional_rule_worker',
    'execution.user_order_requests',
    'SELECT'
  ) then
    raise exception 'conditional notification context read privilege is missing';
  end if;
end
$privilege_audit$;

comment on policy user_order_requests_conditional_worker_read
  on execution.user_order_requests is
  'Read-only admitted-request correlation for conditional execution reporting';

commit;
