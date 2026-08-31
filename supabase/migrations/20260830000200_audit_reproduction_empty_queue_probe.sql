begin;

-- The reproducer role intentionally has no direct table access.  This
-- SECURITY DEFINER probe preserves that boundary and lets an idle worker
-- avoid the expensive immutable-graph claim joins.
create or replace function audit.has_intraday_forward_reproduction_work()
returns boolean
language sql
security definer
set search_path = pg_catalog, audit
as $function$
  select exists (
    select 1
      from audit.intraday_forward_reproduction_work_items work
     where (
         work.status in ('READY', 'RETRY')
         and work.next_attempt_at <= clock_timestamp()
         and work.attempt_count < work.max_attempts
     ) or (
         work.status = 'LEASED'
         and work.lease_expires_at <= clock_timestamp()
         and not exists (
             select 1
               from audit.intraday_forward_reproduction_results result
              where result.work_item_id = work.work_item_id
         )
     )
  )
$function$;

revoke all on function audit.has_intraday_forward_reproduction_work()
  from public, anon, authenticated, service_role, svc_quant,
       svc_qa_worker, svc_audit_api;
grant execute on function audit.has_intraday_forward_reproduction_work()
  to svc_qa_reproducer;

commit;
