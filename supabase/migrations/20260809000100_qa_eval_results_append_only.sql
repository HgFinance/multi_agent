begin;

-- EvalRun is an audit-owned record.  Keep the legacy Workforce/strategy
-- identity columns available for old rows, but materialize the QA identity
-- before making the new columns mandatory.  A migration against a populated
-- table must fail closed rather than manufacture an identity.
alter table audit.eval_runs
  add column if not exists candidate_id text,
  add column if not exists candidate_profile_version text,
  add column if not exists eval_set_version integer,
  add column if not exists eval_set_hash text,
  add column if not exists environment text,
  add column if not exists mock_tool_manifest jsonb,
  add column if not exists model_version text,
  add column if not exists adapter_version text,
  add column if not exists evidence_hash text;

update audit.eval_runs
   set candidate_id = coalesce(
         nullif(btrim(candidate_id), ''),
         candidate_profile_version_id::text,
         candidate_strategy_version_id::text
       ),
       candidate_profile_version = coalesce(
         nullif(btrim(candidate_profile_version), ''),
         candidate_profile_version_id::text,
         candidate_strategy_version_id::text
       )
 where candidate_id is null
    or nullif(btrim(candidate_id), '') is null
    or candidate_profile_version is null
    or nullif(btrim(candidate_profile_version), '') is null;
do $$
begin
  if exists (
    select 1
      from audit.eval_runs r
      left join audit.eval_sets s on s.eval_set_id = r.eval_set_id
     where r.eval_set_version is null
        or nullif(btrim(r.eval_set_hash), '') is null
        or r.eval_set_version is distinct from s.version
        or r.eval_set_hash is distinct from s.content_hash
        or r.mock_tool_manifest is null
        or nullif(btrim(r.model_version), '') is null
        or nullif(btrim(r.adapter_version), '') is null
        or nullif(btrim(r.evidence_hash), '') is null
  ) then
    raise exception using
      message = 'audit.eval_runs contains unverifiable EvalSet or execution provenance; reconcile legacy rows before applying QA constraints';
  end if;
end;
$$;

do $$
begin
  if exists (
    select 1
      from audit.eval_runs
     where nullif(btrim(candidate_id), '') is null
        or nullif(btrim(candidate_profile_version), '') is null
  ) then
    raise exception using
      message = 'audit.eval_runs contains rows without candidate identity; backfill candidate_id and candidate_profile_version before applying QA constraints';
  end if;
end;
$$;

-- The old row-level candidate-source check prevents QA-only candidates from
-- being inserted.  The new textual identity is the audit boundary instead of
-- a Workforce foreign key.
alter table audit.eval_runs
  drop constraint if exists eval_runs_candidate_profile_version_id_fkey,
  drop constraint if exists eval_runs_check,
  drop constraint if exists eval_runs_candidate_identity_check;
alter table audit.eval_runs
  add constraint eval_runs_candidate_identity_check
  check (
    nullif(btrim(candidate_id), '') is not null
    and nullif(btrim(candidate_profile_version), '') is not null
  );
alter table audit.eval_runs
  alter column candidate_id set not null,
  alter column candidate_profile_version set not null;

-- QA execution is deliberately limited to deterministic Shadow/Mock runs.  A
-- null legacy value is the safe Shadow default; unknown values abort the
-- migration instead of being silently rewritten.
alter table audit.eval_runs
  alter column environment set default 'SHADOW';
update audit.eval_runs
   set environment = 'SHADOW'
 where environment is null;
do $$
begin
  if exists (
    select 1
      from audit.eval_runs
     where environment is null
        or environment not in ('SHADOW', 'MOCK')
  ) then
    raise exception using
      message = 'audit.eval_runs.environment must be SHADOW or MOCK';
  end if;
end;
$$;
alter table audit.eval_runs
  drop constraint if exists eval_runs_environment_check;
alter table audit.eval_runs
  add constraint eval_runs_environment_check
  check (environment in ('SHADOW', 'MOCK'));
alter table audit.eval_runs
  alter column environment set not null;

-- EvalRun lifecycle transitions are guarded in the database as well as in the
-- repository.  New runs start QUEUED; only the state-machine transitions below
-- are accepted, and terminal transitions must carry an end timestamp.
create or replace function audit.validate_eval_run_transition()
returns trigger
language plpgsql
set search_path = pg_catalog
as $function$
begin
  if tg_op = 'INSERT' then
    if new.status <> 'QUEUED' or new.ended_at is not null then
      raise exception using
        errcode = '23514',
        message = 'EvalRun must be inserted in QUEUED status without ended_at';
    end if;
    return new;
  end if;
  if new.eval_run_id is distinct from old.eval_run_id
     or new.eval_set_id is distinct from old.eval_set_id
     or new.candidate_profile_version_id is distinct from old.candidate_profile_version_id
     or new.candidate_strategy_version_id is distinct from old.candidate_strategy_version_id
     or new.candidate_id is distinct from old.candidate_id
     or new.candidate_profile_version is distinct from old.candidate_profile_version
     or new.eval_set_version is distinct from old.eval_set_version
     or new.eval_set_hash is distinct from old.eval_set_hash
     or new.champion_ref is distinct from old.champion_ref
     or new.config is distinct from old.config
     or new.trace_id is distinct from old.trace_id
     or new.environment is distinct from old.environment
     or new.mock_tool_manifest is distinct from old.mock_tool_manifest
     or new.model_version is distinct from old.model_version
     or new.adapter_version is distinct from old.adapter_version
     or new.evidence_hash is distinct from old.evidence_hash
     or new.created_at is distinct from old.created_at then
    raise exception using
      errcode = '23514',
      message = 'EvalRun payload columns are immutable';
  end if;


  if new.status is not distinct from old.status then
    if new.started_at is distinct from old.started_at
       or new.ended_at is distinct from old.ended_at then
      raise exception using
        errcode = '23514',
        message = 'EvalRun lifecycle timestamps may only change with a status transition';
    end if;
    return new;
  end if;

  if old.started_at is not null and new.started_at is distinct from old.started_at then
    raise exception using
      errcode = '23514',
      message = 'EvalRun started_at is immutable after it is set';
  end if;

  if not (
    (old.status = 'QUEUED' and new.status in ('RUNNING', 'CANCELLED'))
    or (old.status = 'RUNNING' and new.status in ('COMPLETED', 'FAILED', 'CANCELLED'))
  ) then
    raise exception using
      errcode = '23514',
      message = format('invalid EvalRun status transition: %s -> %s', old.status, new.status);
  end if;

  if new.status = 'RUNNING' then
    if new.started_at is null or new.ended_at is not null then
      raise exception using
        errcode = '23514',
        message = 'RUNNING EvalRun requires started_at and no ended_at';
    end if;
  elsif new.ended_at is null then
    raise exception using
      errcode = '23514',
      message = 'terminal EvalRun transition requires ended_at';
  end if;

  if new.ended_at is not null
     and new.started_at is not null
     and new.ended_at < new.started_at then
    raise exception using
      errcode = '23514',
      message = 'EvalRun ended_at cannot precede started_at';
  end if;

  return new;
end;
$function$;

drop trigger if exists eval_runs_lifecycle_guard on audit.eval_runs;
create trigger eval_runs_lifecycle_guard
before insert or update on audit.eval_runs
for each row execute function audit.validate_eval_run_transition();
create or replace function audit.reject_eval_run_delete()
returns trigger
language plpgsql
set search_path = pg_catalog
as $function$
begin
  raise exception using
    errcode = '23514',
    message = 'EvalRun audit rows are append-only and cannot be deleted';
end;
$function$;

drop trigger if exists eval_runs_append_only_delete on audit.eval_runs;
create trigger eval_runs_append_only_delete
before delete on audit.eval_runs
for each row execute function audit.reject_eval_run_delete();

-- QA audit tables are service/domain-owned.  RLS is enabled at the audit
-- boundary, with no broad authenticated policy (service_role bypasses RLS;
-- authenticated callers cannot read or write raw audit evidence directly).
alter table audit.eval_runs enable row level security;
alter table audit.eval_results enable row level security;

-- QA Eval results are immutable audit evidence.  EvalRun lifecycle status
-- remains mutable only through the guarded transition trigger above.
drop trigger if exists eval_results_append_only on audit.eval_results;
create trigger eval_results_append_only
before update or delete on audit.eval_results
for each row execute function governance.reject_append_only_change();

-- Prevent parent deletion from cascading away audit evidence.
alter table audit.eval_results
  drop constraint if exists eval_results_eval_run_id_fkey;
alter table audit.eval_results
  add constraint eval_results_eval_run_id_fkey
  foreign key (eval_run_id) references audit.eval_runs(eval_run_id)
  on delete restrict;

create table if not exists audit.eval_comparisons (
  eval_run_id uuid primary key,
  status text not null,
  error_code text,
  champion_run_id uuid,
  metrics jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

-- Reassert both comparison references as RESTRICT even when this migration is
-- applied to a database where the table already existed.
alter table audit.eval_comparisons
  drop constraint if exists eval_comparisons_eval_run_id_fkey,
  drop constraint if exists eval_comparisons_champion_run_id_fkey;
alter table audit.eval_comparisons
  add constraint eval_comparisons_eval_run_id_fkey
  foreign key (eval_run_id) references audit.eval_runs(eval_run_id)
  on delete restrict,
  add constraint eval_comparisons_champion_run_id_fkey
  foreign key (champion_run_id) references audit.eval_runs(eval_run_id)
  on delete restrict;

alter table audit.eval_comparisons enable row level security;

drop trigger if exists eval_comparisons_append_only on audit.eval_comparisons;
create trigger eval_comparisons_append_only
before update or delete on audit.eval_comparisons
for each row execute function governance.reject_append_only_change();

commit;
