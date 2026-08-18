begin;

-- quant's handoff INSERT invokes a SECURITY DEFINER trigger that appends the
-- canonical outbox and delivery row.  svc_quant must not bypass that function
-- by inheriting broad schema default privileges on newly-created tables.
revoke insert, update, delete, truncate on
  quant.intraday_forward_qa_outbox,
  quant.intraday_forward_qa_delivery_state,
  quant.intraday_forward_qa_dispatches
from svc_quant;
revoke all on sequence quant.intraday_forward_qa_outbox_outbox_id_seq
  from svc_quant;

-- QA owns acceptance and the reproduction queue.  Quant needs no direct
-- access to either audit table, even if a future default privilege grants it.
revoke all on
  audit.intraday_forward_reproduction_requests,
  audit.intraday_forward_reproduction_work_items
from svc_quant;

do $forward_qa_least_privilege_audit$
begin
  if has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_outbox', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_delivery_state', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_delivery_state', 'UPDATE')
     or has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_dispatches', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'audit.intraday_forward_reproduction_requests', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'audit.intraday_forward_reproduction_work_items', 'INSERT')
     or has_table_privilege(
       'svc_quant', 'audit.intraday_forward_reproduction_work_items', 'UPDATE')
  then
    raise exception
      'svc_quant retains a direct forward QA transport or audit write path';
  end if;

  if not has_table_privilege(
       'svc_quant', 'quant.intraday_forward_qa_outbox', 'SELECT')
     or not has_table_privilege(
       'service_role', 'quant.intraday_forward_qa_delivery_state', 'INSERT')
     or not has_table_privilege(
       'service_role', 'quant.intraday_forward_qa_delivery_state', 'UPDATE')
     or not has_table_privilege(
       'service_role', 'quant.intraday_forward_qa_dispatches', 'INSERT')
     or not has_table_privilege(
       'service_role', 'audit.intraday_forward_reproduction_requests', 'INSERT')
     or not has_table_privilege(
       'service_role', 'audit.intraday_forward_reproduction_work_items', 'INSERT')
     or not has_table_privilege(
       'service_role', 'audit.intraday_forward_reproduction_work_items', 'UPDATE')
  then
    raise exception
      'forward QA relay or acceptance role lacks its required privilege';
  end if;
end
$forward_qa_least_privilege_audit$;

comment on table quant.intraday_forward_qa_outbox is
  'Immutable transactional outbox appended only through the validated SECURITY DEFINER handoff trigger; svc_quant has no direct write privilege.';

commit;
