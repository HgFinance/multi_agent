-- Query attribution for the single-node market/control PostgreSQL instance.
-- ``shared_preload_libraries`` is pinned in docker-compose.yml; applying this
-- file before that restart fails visibly instead of pretending the collector
-- bottleneck can be attributed.

create extension if not exists pg_stat_statements;

do $$
begin
  if current_setting('shared_preload_libraries')
       not like '%pg_stat_statements%' then
    raise exception 'pg_stat_statements is not preloaded';
  end if;
end $$;
