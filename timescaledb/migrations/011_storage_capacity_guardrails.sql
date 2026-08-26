-- Keep the hot raw market window within this single-disk deployment's measured
-- capacity.  Deletion still requires the existing verified-archive gate in
-- collectors/retention_enforcer.py; this migration alone deletes nothing.

update market.retention_registry
   set hot_retention = interval '7 days',
       deletion_enabled = true,
       approved_by = 'operator-directive-2026-08-26-storage-capacity',
       approved_at = now(),
       policy_version = 'single-disk-raw-7d-v1',
       updated_at = now()
 where source_table in ('market.market_ticks', 'market.market_quotes');

do $$
begin
  if (select count(*) from market.retention_registry
       where source_table in ('market.market_ticks', 'market.market_quotes')
         and hot_retention = interval '7 days'
         and deletion_enabled
         and archive_required) <> 2 then
    raise exception 'raw market retention guardrail was not applied exactly twice';
  end if;
end $$;
