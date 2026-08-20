begin;

-- factory_bridge promotes an immutable published proposal into the quant
-- ledger atomically.  The research payload remains read-only to svc_quant;
-- only the lifecycle status may move from PUBLISHED to ACCEPTED/REJECTED.
grant update (status) on research.experiment_proposals to svc_quant;

do $factory_proposal_lifecycle_status_audit$
declare
  writable_column text;
begin
  if has_table_privilege(
       'svc_quant', 'research.experiment_proposals', 'UPDATE') then
    raise exception
      'svc_quant unexpectedly has table-wide proposal UPDATE';
  end if;

  if not has_column_privilege(
       'svc_quant', 'research.experiment_proposals', 'status', 'UPDATE') then
    raise exception
      'svc_quant cannot transition proposal lifecycle status';
  end if;

  select attribute.attname
    into writable_column
    from pg_catalog.pg_attribute attribute
   where attribute.attrelid =
           'research.experiment_proposals'::pg_catalog.regclass
     and attribute.attnum > 0
     and not attribute.attisdropped
     and attribute.attname <> 'status'
     and has_column_privilege(
           'svc_quant',
           'research.experiment_proposals',
           attribute.attname,
           'UPDATE')
   limit 1;

  if writable_column is not null then
    raise exception
      'svc_quant can mutate immutable proposal column %', writable_column;
  end if;
end
$factory_proposal_lifecycle_status_audit$;

commit;
