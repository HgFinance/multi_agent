begin;

create table governance.mandates (
  mandate_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  owner_user_id uuid not null references governance.user_profiles(user_id),
  name text not null,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'ACTIVE', 'SUSPENDED', 'RETIRED')),
  current_version integer not null default 0 check (current_version >= 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (fund_id, name)
);

create table governance.mandate_versions (
  mandate_version_id uuid primary key default gen_random_uuid(),
  mandate_id uuid not null references governance.mandates(mandate_id),
  version integer not null check (version > 0),
  objective_text text not null,
  objective jsonb not null,
  allowed_assets jsonb not null default '[]'::jsonb,
  forbidden_assets jsonb not null default '[]'::jsonb,
  universe_policy jsonb not null default '{}'::jsonb,
  risk_bounds jsonb not null,
  approval_rules jsonb not null,
  execution_rules jsonb not null default '{}'::jsonb,
  effective_from timestamptz not null,
  effective_to timestamptz,
  content_hash text not null,
  created_by uuid references governance.user_profiles(user_id),
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from),
  unique (mandate_id, version),
  unique (mandate_id, content_hash)
);

create table governance.mandate_decisions (
  mandate_decision_id uuid primary key default gen_random_uuid(),
  mandate_version_id uuid not null references governance.mandate_versions(mandate_version_id),
  decision text not null check (decision in ('APPROVE', 'REJECT', 'SUSPEND', 'RETIRE')),
  conditions jsonb not null default '{}'::jsonb,
  reason text,
  approved_by uuid references governance.user_profiles(user_id),
  trace_id uuid not null,
  decided_at timestamptz not null default now()
);

create table governance.cases (
  case_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  display_id text not null unique,
  case_type text not null,
  priority integer not null check (priority between 0 and 100),
  status text not null,
  owner_department text not null,
  due_at timestamptz,
  trace_id uuid not null unique,
  schema_version integer not null default 1 check (schema_version > 0),
  created_by text not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table governance.investment_cases (
  case_id uuid primary key references governance.cases(case_id) on delete cascade,
  case_version integer not null default 1 check (case_version > 0),
  trigger_type text not null,
  trigger_event_id uuid,
  primary_instrument_id uuid references reference.instruments(instrument_id),
  mandate_version_id uuid not null references governance.mandate_versions(mandate_version_id),
  universe_version_id uuid,
  feature_snapshot_id uuid,
  portfolio_snapshot_id uuid,
  decision_time timestamptz not null,
  committee_run_id uuid,
  evidence_pack_id uuid,
  decision_id uuid,
  strategy_version_id uuid,
  intent_group_id uuid,
  risk_decision_id uuid,
  user_approval_id uuid,
  evaluation_id uuid,
  terminal_reason text
);

create table governance.case_events (
  event_id uuid primary key default gen_random_uuid(),
  case_id uuid not null references governance.cases(case_id) on delete cascade,
  sequence bigint not null check (sequence > 0),
  event_type text not null,
  from_status text,
  to_status text not null,
  schema_version integer not null check (schema_version > 0),
  producer text not null,
  actor text not null,
  reason text,
  idempotency_key text not null unique,
  payload jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null,
  recorded_at timestamptz not null default now(),
  unique (case_id, sequence)
);

create index case_events_trace_idx
  on governance.case_events (case_id, occurred_at);

create table governance.case_artifacts (
  case_id uuid not null references governance.cases(case_id) on delete cascade,
  artifact_type text not null,
  artifact_id uuid not null,
  artifact_version text not null,
  producer text not null,
  content_hash text,
  created_at timestamptz not null default now(),
  primary key (case_id, artifact_type, artifact_id, artifact_version)
);

create table governance.committee_sessions (
  session_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  case_id uuid references governance.cases(case_id),
  committee_type text not null,
  quorum_policy jsonb not null,
  opened_at timestamptz not null,
  closed_at timestamptz,
  status text not null check (status in ('SCHEDULED', 'OPEN', 'DECIDED', 'CANCELLED')),
  trace_id uuid not null,
  check (closed_at is null or closed_at >= opened_at)
);

create table governance.committee_votes (
  vote_id uuid primary key default gen_random_uuid(),
  session_id uuid not null references governance.committee_sessions(session_id) on delete cascade,
  department text not null,
  voter_agent_id uuid,
  decision text not null check (decision in ('APPROVE', 'REJECT', 'ABSTAIN', 'CONDITIONAL')),
  conditions jsonb not null default '{}'::jsonb,
  artifact_ids uuid[] not null default '{}',
  rationale text,
  voted_at timestamptz not null default now(),
  unique (session_id, department, voter_agent_id)
);

create table governance.committee_decisions (
  committee_decision_id uuid primary key default gen_random_uuid(),
  session_id uuid not null unique references governance.committee_sessions(session_id),
  decision text not null check (decision in ('APPROVE', 'REJECT', 'DEFER', 'CONDITIONAL')),
  scope jsonb not null,
  conditions jsonb not null default '{}'::jsonb,
  valid_until timestamptz,
  dissent jsonb not null default '[]'::jsonb,
  approvals jsonb not null default '[]'::jsonb,
  decided_at timestamptz not null default now()
);

create table governance.approvals (
  approval_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  object_type text not null,
  object_id uuid not null,
  required_role text not null,
  decision text not null check (decision in ('PENDING', 'APPROVED', 'REJECTED', 'EXPIRED', 'REVOKED')),
  actor_user_id uuid references governance.user_profiles(user_id),
  actor_agent_id uuid,
  conditions jsonb not null default '{}'::jsonb,
  reason text,
  expires_at timestamptz,
  decided_at timestamptz,
  created_at timestamptz not null default now(),
  unique (object_type, object_id, required_role)
);

create table governance.escalations (
  escalation_id uuid primary key default gen_random_uuid(),
  case_id uuid not null references governance.cases(case_id),
  reason text not null,
  severity text not null check (severity in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
  target text not null,
  due_at timestamptz,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'ACKNOWLEDGED', 'RESOLVED', 'CANCELLED')),
  resolution text,
  created_at timestamptz not null default now(),
  resolved_at timestamptz
);

create table governance.department_handoffs (
  handoff_id uuid primary key default gen_random_uuid(),
  case_id uuid not null references governance.cases(case_id),
  from_department text not null,
  to_department text not null,
  purpose text not null,
  input_artifact_ids uuid[] not null default '{}',
  required_output_schema jsonb not null,
  data_as_of timestamptz not null,
  due_at timestamptz,
  priority integer not null check (priority between 0 and 100),
  escalation_policy jsonb not null default '{}'::jsonb,
  status text not null default 'REQUESTED'
    check (status in ('REQUESTED', 'ACCEPTED', 'COMPLETED', 'REJECTED', 'EXPIRED')),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  check (from_department <> to_department)
);

create table governance.capital_priorities (
  priority_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  objective jsonb not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  status text not null check (status in ('DRAFT', 'APPROVED', 'ACTIVE', 'RETIRED')),
  approval_id uuid references governance.approvals(approval_id),
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from)
);

create table governance.capital_allocations (
  allocation_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid references accounting.books(book_id),
  strategy_id uuid,
  amount numeric(30, 10) not null check (amount >= 0),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  risk_budget_id uuid,
  effective_from timestamptz not null,
  effective_to timestamptz,
  approval_id uuid not null references governance.approvals(approval_id),
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from)
);

create table governance.report_runs (
  report_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  report_type text not null,
  as_of timestamptz not null,
  source_snapshot_ids uuid[] not null default '{}',
  template_version text not null,
  object_path text,
  content_hash text,
  status text not null check (status in ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED')),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  completed_at timestamptz
);

create table governance.notifications (
  notification_id uuid primary key default gen_random_uuid(),
  fund_id uuid references accounting.funds(fund_id),
  event_type text not null,
  recipient text not null,
  channel text not null,
  payload jsonb not null,
  dedup_key text not null unique,
  status text not null default 'PENDING'
    check (status in ('PENDING', 'SENT', 'FAILED', 'SUPPRESSED')),
  sent_at timestamptz,
  created_at timestamptz not null default now()
);

create table workforce.departments (
  department_id uuid primary key default gen_random_uuid(),
  department_code text not null unique,
  name text not null,
  mission text not null,
  supervisor_agent_id uuid,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'RETIRED')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table workforce.skills (
  skill_id uuid primary key default gen_random_uuid(),
  skill_code text not null,
  name text not null,
  version text not null,
  input_schema jsonb not null,
  output_schema jsonb not null,
  owner_department_id uuid references workforce.departments(department_id),
  timeout_seconds integer check (timeout_seconds is null or timeout_seconds > 0),
  retry_policy jsonb not null default '{}'::jsonb,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'ACTIVE', 'DEPRECATED', 'RETIRED')),
  artifact_hash text not null,
  created_at timestamptz not null default now(),
  unique (skill_code, version)
);

create table workforce.tools (
  tool_id uuid primary key default gen_random_uuid(),
  tool_code text not null unique,
  name text not null,
  scope jsonb not null,
  risk_level text not null check (risk_level in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
  owner_department_id uuid references workforce.departments(department_id),
  schema_version text not null,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'RETIRED')),
  created_at timestamptz not null default now()
);

create table workforce.models (
  model_id uuid primary key default gen_random_uuid(),
  provider text not null,
  model_name text not null,
  model_version text not null,
  capabilities jsonb not null,
  cost_policy jsonb not null,
  allowed_environments text[] not null,
  data_policy jsonb not null default '{}'::jsonb,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'RETIRED')),
  created_at timestamptz not null default now(),
  unique (provider, model_name, model_version)
);

create table workforce.role_templates (
  role_id uuid primary key default gen_random_uuid(),
  role_code text not null unique,
  department_id uuid not null references workforce.departments(department_id),
  mission text not null,
  required_skills jsonb not null,
  forbidden_actions jsonb not null default '[]'::jsonb,
  kpi jsonb not null,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'DEPRECATED', 'RETIRED')),
  created_at timestamptz not null default now()
);

create table workforce.agent_profiles (
  agent_id uuid primary key default gen_random_uuid(),
  employee_code text not null unique,
  department_id uuid not null references workforce.departments(department_id),
  role_id uuid not null references workforce.role_templates(role_id),
  display_name text not null,
  runtime text not null default 'HERMES',
  employment_status text not null default 'CANDIDATE'
    check (employment_status in ('CANDIDATE', 'PROBATION', 'ACTIVE', 'SUSPENDED', 'RETIRED')),
  current_version integer not null default 0 check (current_version >= 0),
  owner_user_id uuid references governance.user_profiles(user_id),
  backup_owner_user_id uuid references governance.user_profiles(user_id),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table workforce.departments
  add constraint departments_supervisor_agent_fk
  foreign key (supervisor_agent_id) references workforce.agent_profiles(agent_id);

alter table governance.committee_votes
  add constraint committee_votes_voter_agent_fk
  foreign key (voter_agent_id) references workforce.agent_profiles(agent_id);

alter table governance.approvals
  add constraint approvals_actor_agent_fk
  foreign key (actor_agent_id) references workforce.agent_profiles(agent_id);

create table workforce.agent_profile_versions (
  profile_version_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  version integer not null check (version > 0),
  model_id uuid not null references workforce.models(model_id),
  prompt_artifact_path text not null,
  skill_manifest jsonb not null,
  tool_allowlist jsonb not null,
  data_scopes jsonb not null,
  memory_namespace text not null,
  token_budget jsonb not null,
  sla jsonb not null,
  eval_requirements jsonb not null,
  forbidden_actions jsonb not null,
  artifact_hash text not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'EVALUATING', 'APPROVED', 'ACTIVE', 'SUSPENDED', 'RETIRED')),
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from),
  unique (agent_id, version),
  unique (agent_id, artifact_hash)
);

create table workforce.agent_skill_assignments (
  profile_version_id uuid not null references workforce.agent_profile_versions(profile_version_id) on delete cascade,
  skill_id uuid not null references workforce.skills(skill_id),
  required boolean not null default true,
  config jsonb not null default '{}'::jsonb,
  primary key (profile_version_id, skill_id)
);

create table workforce.agent_tool_permissions (
  permission_id uuid primary key default gen_random_uuid(),
  profile_version_id uuid not null references workforce.agent_profile_versions(profile_version_id) on delete cascade,
  tool_id uuid not null references workforce.tools(tool_id),
  permission_verb text not null,
  scope jsonb not null,
  environment text not null,
  effective_from timestamptz not null,
  effective_to timestamptz,
  approval_id uuid references governance.approvals(approval_id),
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'REVOKED', 'EXPIRED')),
  check (effective_to is null or effective_to > effective_from),
  unique (profile_version_id, tool_id, permission_verb, environment, effective_from)
);

create table workforce.hiring_requests (
  request_id uuid primary key default gen_random_uuid(),
  department_id uuid not null references workforce.departments(department_id),
  business_problem text not null,
  evidence jsonb not null,
  required_capabilities jsonb not null,
  budget jsonb not null,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'OPEN', 'EVALUATING', 'APPROVED', 'REJECTED', 'CLOSED')),
  trace_id uuid not null,
  created_at timestamptz not null default now()
);

create table workforce.candidates (
  candidate_id uuid primary key default gen_random_uuid(),
  request_id uuid not null references workforce.hiring_requests(request_id),
  profile_config jsonb not null,
  expected_cost jsonb not null,
  artifact_hash text not null,
  status text not null default 'PROPOSED'
    check (status in ('PROPOSED', 'EVALUATING', 'SHORTLISTED', 'REJECTED', 'HIRED')),
  created_at timestamptz not null default now()
);

create table workforce.selection_reviews (
  review_id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null references workforce.candidates(candidate_id),
  eval_run_id uuid,
  champion_comparison jsonb not null,
  decision text not null check (decision in ('HIRE', 'REJECT', 'RETEST', 'CONDITIONAL')),
  conditions jsonb not null default '{}'::jsonb,
  reviewer text not null,
  reviewed_at timestamptz not null default now()
);

create table workforce.performance_reviews (
  review_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  profile_version_id uuid not null references workforce.agent_profile_versions(profile_version_id),
  period_start timestamptz not null,
  period_end timestamptz not null,
  role_metrics jsonb not null,
  cost jsonb not null,
  findings jsonb not null,
  decision text not null,
  created_at timestamptz not null default now(),
  check (period_end > period_start),
  unique (agent_id, profile_version_id, period_start, period_end)
);

create table workforce.lifecycle_events (
  event_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  event_type text not null,
  from_status text,
  to_status text not null,
  approvals jsonb not null default '[]'::jsonb,
  reason text,
  trace_id uuid not null,
  occurred_at timestamptz not null,
  recorded_at timestamptz not null default now()
);

create table workforce.capacity_snapshots (
  snapshot_id uuid primary key default gen_random_uuid(),
  department_id uuid references workforce.departments(department_id),
  agent_id uuid references workforce.agent_profiles(agent_id),
  window_start timestamptz not null,
  window_end timestamptz not null,
  arrivals bigint not null default 0,
  queue_p95_ms numeric(20, 4),
  duration_p95_ms numeric(20, 4),
  retry_rate numeric(12, 8),
  error_rate numeric(12, 8),
  utilization numeric(12, 8),
  metadata jsonb not null default '{}'::jsonb,
  check (window_end > window_start),
  check (department_id is not null or agent_id is not null)
);

create table workforce.cost_snapshots (
  snapshot_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  profile_version_id uuid not null references workforce.agent_profile_versions(profile_version_id),
  window_start timestamptz not null,
  window_end timestamptz not null,
  input_tokens bigint not null default 0,
  output_tokens bigint not null default 0,
  model_cost numeric(20, 8) not null default 0,
  tool_cost numeric(20, 8) not null default 0,
  infra_cost numeric(20, 8) not null default 0,
  case_count bigint not null default 0,
  currency text not null default 'USD' check (currency ~ '^[A-Z]{3}$'),
  check (window_end > window_start)
);

create or replace function governance.reject_append_only_change()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  raise exception '% is append-only', tg_table_schema || '.' || tg_table_name;
end;
$$;

create trigger case_events_append_only
before update or delete on governance.case_events
for each row execute function governance.reject_append_only_change();

create trigger workforce_lifecycle_events_append_only
before update or delete on workforce.lifecycle_events
for each row execute function governance.reject_append_only_change();

create trigger mandates_touch_updated_at
before update on governance.mandates
for each row execute function governance.touch_updated_at();

create trigger cases_touch_updated_at
before update on governance.cases
for each row execute function governance.touch_updated_at();

create trigger departments_touch_updated_at
before update on workforce.departments
for each row execute function governance.touch_updated_at();

create trigger agents_touch_updated_at
before update on workforce.agent_profiles
for each row execute function governance.touch_updated_at();

do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'mandates', 'mandate_versions', 'mandate_decisions', 'cases', 'investment_cases',
    'case_events', 'case_artifacts', 'committee_sessions', 'committee_votes',
    'committee_decisions', 'approvals', 'escalations', 'department_handoffs',
    'capital_priorities', 'capital_allocations', 'report_runs', 'notifications'
  ] loop
    execute format('alter table governance.%I enable row level security', table_name);
  end loop;

  foreach table_name in array array[
    'departments', 'skills', 'tools', 'models', 'role_templates', 'agent_profiles',
    'agent_profile_versions', 'agent_skill_assignments', 'agent_tool_permissions',
    'hiring_requests', 'candidates', 'selection_reviews', 'performance_reviews',
    'lifecycle_events', 'capacity_snapshots', 'cost_snapshots'
  ] loop
    execute format('alter table workforce.%I enable row level security', table_name);
  end loop;
end;
$$;

create policy mandates_fund_member_select
on governance.mandates for select
using (governance.can_access_fund(fund_id));

create policy cases_fund_member_select
on governance.cases for select
using (governance.can_access_fund(fund_id));

create policy committee_sessions_fund_member_select
on governance.committee_sessions for select
using (governance.can_access_fund(fund_id));

create policy approvals_fund_member_select
on governance.approvals for select
using (governance.can_access_fund(fund_id));

create policy capital_priorities_fund_member_select
on governance.capital_priorities for select
using (governance.can_access_fund(fund_id));

create policy capital_allocations_fund_member_select
on governance.capital_allocations for select
using (governance.can_access_fund(fund_id));

create policy reports_fund_member_select
on governance.report_runs for select
using (governance.can_access_fund(fund_id));

commit;
