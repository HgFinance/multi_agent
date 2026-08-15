-- Bound Timescale compression/columnstore jobs so an old analytical read
-- cannot queue an AccessExclusiveLock forever and stall the market API.
--
-- The 2026-08-14 incident showed five market policies with max_runtime=0.
-- A long FDW read held AccessShareLock; the compression policy waited for an
-- exclusive lock, and PostgreSQL lock fairness queued later readers behind it.
-- The live jobs were repaired to 20 minutes. Persist the same setting for
-- fresh databases and restored environments.

do $$
declare
  compression_job record;
begin
  for compression_job in
    select job_id
      from timescaledb_information.jobs
     where hypertable_schema = 'market'
       and hypertable_name in (
         'market_ticks',
         'market_quotes',
         'market_bars',
         'microstructure_features',
         'derivative_snapshots'
       )
       and (proc_name like '%compress%' or proc_name like '%columnstore%')
  loop
    perform public.alter_job(
      compression_job.job_id,
      max_runtime => interval '20 minutes'
    );
  end loop;
end
$$;
