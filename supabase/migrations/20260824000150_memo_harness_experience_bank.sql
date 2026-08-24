begin;

-- MemoHarness-lite D5 durable memory.  This table contains structured,
-- payload-free workflow outcomes only; it is not a prompt, transcript, or
-- user-profile store.  The generic runtime reaches it through service_role,
-- while D5 remains disabled until MEMOHARNESS_D5_MODE is explicitly enabled.
create schema if not exists experience;
revoke all on schema experience from public;

create table if not exists experience.workflow_experiences (
  experience_id uuid primary key default gen_random_uuid(),
  experience_identity text not null unique check (length(experience_identity) between 3 and 160),
  case_type text not null check (length(case_type) between 1 and 64),
  binding boolean not null,
  primary_departments text[] not null default '{}',
  orchestration_policy text not null check (
    orchestration_policy in ('analysis_parallel', 'binding_qa_gate')
  ),
  success boolean not null,
  failure_codes text[] not null default '{}',
  latency_ms integer check (latency_ms is null or latency_ms >= 0),
  qa_enabled boolean not null default true,
  qa_blocks_response boolean not null default false,
  lesson text not null default '' check (length(lesson) <= 240),
  source_run_id text,
  created_at timestamptz not null default now()
);

create index if not exists workflow_experiences_lookup_idx
  on experience.workflow_experiences (case_type, binding, created_at desc);

create index if not exists workflow_experiences_success_idx
  on experience.workflow_experiences (case_type, binding, success, created_at desc);

grant usage on schema experience to service_role;
grant select, insert on experience.workflow_experiences to service_role;

comment on table experience.workflow_experiences is
  'Payload-free MemoHarness-lite D5 workflow experience bank; advisory only.';
comment on column experience.workflow_experiences.failure_codes is
  'Structured observed failures; operational provider failures are not routing policy.';

commit;
