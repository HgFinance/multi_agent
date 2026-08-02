begin;

-- Risk와 QA의 Hermes(L1) 오케스트레이션 및 LangGraph(L2) 직원 실행 원장.
-- 원문(raw)과 사람용 summary를 분리하고, 주문/체결은 별도 event_type으로
-- 기록한다. RLS 정책을 추가하지 않아 authenticated client에는 기본 차단되며,
-- domain service의 service-role/repository만 적재·Projection한다.

create table risk.run_log_events (
  event_id uuid primary key default gen_random_uuid(),
  run_id text not null,
  trace_id text not null,
  department text not null check (department = 'risk-management'),
  hermes_profile text not null,
  employee_profile text not null,
  executor text not null check (executor = 'langgraph'),
  event_type text not null check (event_type in (
    'InputSnapshot', 'AgentOutput', 'Validation', 'Decision', 'Order', 'Fill'
  )),
  as_of timestamptz,
  occurred_at timestamptz not null default now(),
  asset text,
  inputs_hash text not null,
  schema_id text,
  schema_valid boolean,
  domain_valid boolean,
  failed_rule text,
  retry_count integer not null default 0 check (retry_count >= 0),
  fallback_reason text,
  model_version text,
  prompt_version text,
  parameter_version text,
  rationale text,
  evidence_refs jsonb not null default '[]'::jsonb,
  constraints_applied jsonb not null default '[]'::jsonb,
  order_id text,
  fill_id text,
  output_hash text,
  summary text not null default '',
  raw jsonb not null default '{}'::jsonb,
  check (event_type <> 'Order' or order_id is not null),
  check (event_type <> 'Fill' or fill_id is not null)
);

create index if not exists risk_run_log_events_run_time_idx
  on risk.run_log_events (run_id, occurred_at);
create index if not exists risk_run_log_events_input_hash_idx
  on risk.run_log_events (inputs_hash);
create index if not exists risk_run_log_events_fallback_idx
  on risk.run_log_events (fallback_reason)
  where fallback_reason is not null;
alter table risk.run_log_events enable row level security;

create table audit.run_log_events (
  event_id uuid primary key default gen_random_uuid(),
  run_id text not null,
  trace_id text not null,
  department text not null check (department = 'qa-department'),
  hermes_profile text not null,
  employee_profile text not null,
  executor text not null check (executor = 'langgraph'),
  event_type text not null check (event_type in (
    'InputSnapshot', 'AgentOutput', 'Validation', 'Decision', 'Order', 'Fill'
  )),
  as_of timestamptz,
  occurred_at timestamptz not null default now(),
  asset text,
  inputs_hash text not null,
  schema_id text,
  schema_valid boolean,
  domain_valid boolean,
  failed_rule text,
  retry_count integer not null default 0 check (retry_count >= 0),
  fallback_reason text,
  model_version text,
  prompt_version text,
  parameter_version text,
  rationale text,
  evidence_refs jsonb not null default '[]'::jsonb,
  constraints_applied jsonb not null default '[]'::jsonb,
  order_id text,
  fill_id text,
  output_hash text,
  summary text not null default '',
  raw jsonb not null default '{}'::jsonb,
  check (event_type <> 'Order' or order_id is not null),
  check (event_type <> 'Fill' or fill_id is not null)
);

create index if not exists audit_run_log_events_run_time_idx
  on audit.run_log_events (run_id, occurred_at);
create index if not exists audit_run_log_events_input_hash_idx
  on audit.run_log_events (inputs_hash);
create index if not exists audit_run_log_events_fallback_idx
  on audit.run_log_events (fallback_reason)
  where fallback_reason is not null;
alter table audit.run_log_events enable row level security;

commit;
