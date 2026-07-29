begin;

create table audit.traces (
  trace_id uuid primary key,
  fund_id uuid references accounting.funds(fund_id),
  case_id uuid references governance.cases(case_id),
  trace_type text not null,
  root_event_type text not null,
  root_event_id text,
  environment text not null,
  started_at timestamptz not null,
  ended_at timestamptz,
  status text not null check (status in ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', 'PARTIAL')),
  metadata jsonb not null default '{}'::jsonb,
  check (ended_at is null or ended_at >= started_at)
);

create table audit.artifact_versions (
  artifact_version_id uuid primary key default gen_random_uuid(),
  artifact_id uuid not null,
  artifact_type text not null,
  version text not null,
  content_hash text not null,
  object_path text,
  producer text not null,
  schema_version text,
  fund_id uuid references accounting.funds(fund_id),
  trace_id uuid,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  unique (artifact_id, version),
  unique (artifact_type, content_hash)
);

create table audit.artifact_lineage (
  parent_artifact_version_id uuid not null references audit.artifact_versions(artifact_version_id),
  child_artifact_version_id uuid not null references audit.artifact_versions(artifact_version_id),
  relation_type text not null,
  transform_version text,
  trace_id uuid,
  created_at timestamptz not null default now(),
  primary key (parent_artifact_version_id, child_artifact_version_id, relation_type),
  check (parent_artifact_version_id <> child_artifact_version_id)
);

create table audit.agent_runs (
  agent_run_id uuid primary key default gen_random_uuid(),
  trace_id uuid not null,
  case_id uuid references governance.cases(case_id),
  fund_id uuid references accounting.funds(fund_id),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  profile_version_id uuid not null references workforce.agent_profile_versions(profile_version_id),
  model_id uuid references workforce.models(model_id),
  input_hash text not null,
  output_artifact_version_id uuid references audit.artifact_versions(artifact_version_id),
  started_at timestamptz not null,
  ended_at timestamptz,
  status text not null check (status in ('RUNNING', 'COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED')),
  error_code text,
  token_usage jsonb not null default '{}'::jsonb,
  cost jsonb not null default '{}'::jsonb,
  trace_uri text,
  check (ended_at is null or ended_at >= started_at),
  unique (profile_version_id, input_hash, started_at)
);

create index agent_runs_trace_idx on audit.agent_runs (trace_id, started_at);

create table audit.tool_calls (
  tool_call_id uuid primary key default gen_random_uuid(),
  agent_run_id uuid not null references audit.agent_runs(agent_run_id),
  trace_id uuid not null,
  tool_id uuid references workforce.tools(tool_id),
  tool_name text not null,
  scope jsonb not null,
  input_hash text not null,
  output_hash text,
  status text not null check (status in ('REQUESTED', 'ALLOWED', 'DENIED', 'COMPLETED', 'FAILED', 'TIMED_OUT')),
  policy_version text,
  latency_ms integer check (latency_ms is null or latency_ms >= 0),
  error_code text,
  occurred_at timestamptz not null,
  completed_at timestamptz,
  metadata jsonb not null default '{}'::jsonb,
  check (completed_at is null or completed_at >= occurred_at)
);

create index tool_calls_run_time_idx on audit.tool_calls (agent_run_id, occurred_at);

create table audit.access_events (
  access_event_id uuid primary key default gen_random_uuid(),
  trace_id uuid,
  identity_type text not null,
  identity_id text not null,
  resource_type text not null,
  resource_id text not null,
  action text not null,
  decision text not null check (decision in ('ALLOW', 'DENY')),
  reason_code text,
  policy_version text,
  occurred_at timestamptz not null,
  metadata jsonb not null default '{}'::jsonb
);

create index access_events_identity_time_idx
  on audit.access_events (identity_type, identity_id, occurred_at desc);

create table audit.deployment_events (
  deployment_event_id uuid primary key default gen_random_uuid(),
  deployment_id uuid references strategy.deployments(deployment_id),
  artifact_type text not null,
  artifact_version_id uuid references audit.artifact_versions(artifact_version_id),
  environment text not null,
  event_type text not null,
  before_state jsonb,
  after_state jsonb not null,
  approvals jsonb not null default '[]'::jsonb,
  actor text not null,
  trace_id uuid not null,
  occurred_at timestamptz not null
);

create table audit.claim_checks (
  claim_check_id uuid primary key default gen_random_uuid(),
  artifact_version_id uuid not null references audit.artifact_versions(artifact_version_id),
  claim_index integer not null check (claim_index >= 0),
  claim text not null,
  evidence_chunk_ids uuid[] not null default '{}',
  result text not null check (result in ('SUPPORTED', 'PARTIAL', 'UNSUPPORTED', 'CONTRADICTED', 'NOT_APPLICABLE')),
  reason text,
  checker_version text not null,
  checked_at timestamptz not null default now(),
  unique (artifact_version_id, claim_index, checker_version)
);

create table audit.eval_sets (
  eval_set_id uuid primary key default gen_random_uuid(),
  role_code text not null,
  version integer not null check (version > 0),
  manifest_path text not null,
  content_hash text not null,
  approval_id uuid references governance.approvals(approval_id),
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'APPROVED', 'ACTIVE', 'RETIRED')),
  created_at timestamptz not null default now(),
  unique (role_code, version),
  unique (role_code, content_hash)
);

create table audit.eval_runs (
  eval_run_id uuid primary key default gen_random_uuid(),
  eval_set_id uuid not null references audit.eval_sets(eval_set_id),
  candidate_profile_version_id uuid references workforce.agent_profile_versions(profile_version_id),
  candidate_strategy_version_id uuid references strategy.versions(strategy_version_id),
  champion_ref jsonb,
  config jsonb not null,
  status text not null check (status in ('QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED')),
  trace_id uuid not null,
  started_at timestamptz,
  ended_at timestamptz,
  created_at timestamptz not null default now(),
  check (candidate_profile_version_id is not null or candidate_strategy_version_id is not null),
  check (ended_at is null or started_at is null or ended_at >= started_at)
);

create table audit.eval_results (
  eval_result_id uuid primary key default gen_random_uuid(),
  eval_run_id uuid not null references audit.eval_runs(eval_run_id) on delete cascade,
  case_key text not null,
  metric text not null,
  score numeric(20, 10),
  passed boolean not null,
  evidence jsonb not null,
  error_code text,
  created_at timestamptz not null default now(),
  unique (eval_run_id, case_key, metric)
);

create table audit.qa_decisions (
  qa_decision_id uuid primary key default gen_random_uuid(),
  artifact_version_id uuid not null references audit.artifact_versions(artifact_version_id),
  gate text not null,
  decision text not null check (decision in ('PASS', 'WARN', 'FAIL', 'CONDITIONAL')),
  conditions jsonb not null default '{}'::jsonb,
  reason_codes text[] not null default '{}',
  expires_at timestamptz,
  decided_by text not null,
  trace_id uuid not null,
  decided_at timestamptz not null default now(),
  unique (artifact_version_id, gate)
);

create table audit.release_reviews (
  release_review_id uuid primary key default gen_random_uuid(),
  strategy_version_id uuid references strategy.versions(strategy_version_id),
  profile_version_id uuid references workforce.agent_profile_versions(profile_version_id),
  dataset_id uuid references quant.dataset_manifests(dataset_id),
  code_version text,
  model_artifact_id uuid references quant.model_artifacts(model_artifact_id),
  eval_run_id uuid references audit.eval_runs(eval_run_id),
  decision text not null check (decision in ('APPROVE', 'REJECT', 'DEFER', 'CONDITIONAL')),
  rollback_conditions jsonb not null,
  reviewer text not null,
  trace_id uuid not null,
  reviewed_at timestamptz not null default now(),
  check (strategy_version_id is not null or profile_version_id is not null)
);

create table audit.findings (
  finding_id uuid primary key default gen_random_uuid(),
  fund_id uuid references accounting.funds(fund_id),
  finding_type text not null,
  severity text not null check (severity in ('LOW', 'MEDIUM', 'HIGH', 'CRITICAL')),
  artifact_version_id uuid references audit.artifact_versions(artifact_version_id),
  control_id text,
  description text not null,
  owner text,
  due_at timestamptz,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'ACKNOWLEDGED', 'REMEDIATING', 'VERIFICATION', 'CLOSED', 'ACCEPTED')),
  opened_by text not null,
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  closed_at timestamptz
);

create table audit.finding_events (
  finding_event_id uuid primary key default gen_random_uuid(),
  finding_id uuid not null references audit.findings(finding_id),
  from_status text,
  to_status text not null,
  actor text not null,
  evidence jsonb not null default '{}'::jsonb,
  reason text,
  occurred_at timestamptz not null
);

create table audit.control_tests (
  control_test_id uuid primary key default gen_random_uuid(),
  control_id text not null,
  control_version text not null,
  sample_manifest jsonb not null,
  result text not null check (result in ('PASS', 'FAIL', 'PARTIAL', 'NOT_RUN')),
  workpaper_path text,
  tester text not null,
  trace_id uuid not null,
  tested_at timestamptz not null default now()
);

create table audit.incidents (
  incident_id uuid primary key default gen_random_uuid(),
  fund_id uuid references accounting.funds(fund_id),
  incident_code text not null unique,
  severity text not null check (severity in ('SEV4', 'SEV3', 'SEV2', 'SEV1')),
  title text not null,
  impact jsonb not null,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'INVESTIGATING', 'MITIGATED', 'RESOLVED', 'CLOSED')),
  started_at timestamptz not null,
  detected_at timestamptz not null,
  mitigated_at timestamptz,
  resolved_at timestamptz,
  commander text,
  trace_id uuid not null,
  check (detected_at >= started_at),
  check (resolved_at is null or resolved_at >= detected_at)
);

create table audit.incident_events (
  incident_event_id uuid primary key default gen_random_uuid(),
  incident_id uuid not null references audit.incidents(incident_id),
  source text not null,
  entry_type text not null check (entry_type in ('FACT', 'INFERENCE', 'ACTION', 'DECISION')),
  summary text not null,
  evidence jsonb not null default '{}'::jsonb,
  occurred_at timestamptz not null,
  recorded_at timestamptz not null default now(),
  recorded_by text not null
);

create table audit.corrective_actions (
  corrective_action_id uuid primary key default gen_random_uuid(),
  incident_id uuid references audit.incidents(incident_id),
  finding_id uuid references audit.findings(finding_id),
  owner text not null,
  action_plan jsonb not null,
  due_at timestamptz not null,
  verification jsonb,
  verifier text,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'IN_PROGRESS', 'VERIFYING', 'COMPLETED', 'CANCELLED')),
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  check (incident_id is not null or finding_id is not null)
);

alter table workforce.selection_reviews
  add constraint selection_reviews_eval_run_fk
  foreign key (eval_run_id) references audit.eval_runs(eval_run_id);

alter table governance.investment_cases
  add constraint investment_cases_committee_run_fk
  foreign key (committee_run_id) references governance.committee_sessions(session_id),
  add constraint investment_cases_evidence_pack_fk
  foreign key (evidence_pack_id) references research.research_packets(research_packet_id),
  add constraint investment_cases_user_approval_fk
  foreign key (user_approval_id) references governance.approvals(approval_id);

create trigger artifact_versions_append_only
before update or delete on audit.artifact_versions
for each row execute function governance.reject_append_only_change();

create trigger artifact_lineage_append_only
before update or delete on audit.artifact_lineage
for each row execute function governance.reject_append_only_change();

create trigger tool_calls_append_only
before update or delete on audit.tool_calls
for each row execute function governance.reject_append_only_change();

create trigger access_events_append_only
before update or delete on audit.access_events
for each row execute function governance.reject_append_only_change();

create trigger deployment_events_append_only
before update or delete on audit.deployment_events
for each row execute function governance.reject_append_only_change();

create trigger finding_events_append_only
before update or delete on audit.finding_events
for each row execute function governance.reject_append_only_change();

create trigger incident_events_append_only
before update or delete on audit.incident_events
for each row execute function governance.reject_append_only_change();

do $$
declare
  table_name text;
begin
  foreach table_name in array array[
    'traces', 'artifact_versions', 'artifact_lineage', 'agent_runs', 'tool_calls',
    'access_events', 'deployment_events', 'claim_checks', 'eval_sets', 'eval_runs',
    'eval_results', 'qa_decisions', 'release_reviews', 'findings', 'finding_events',
    'control_tests', 'incidents', 'incident_events', 'corrective_actions'
  ] loop
    execute format('alter table audit.%I enable row level security', table_name);
  end loop;
end;
$$;

create policy audit_traces_fund_member_select
on audit.traces for select
using (fund_id is not null and governance.can_access_fund(fund_id));

create policy audit_findings_fund_member_select
on audit.findings for select
using (fund_id is not null and governance.can_access_fund(fund_id));

create policy audit_incidents_fund_member_select
on audit.incidents for select
using (fund_id is not null and governance.can_access_fund(fund_id));

create policy investment_cases_fund_member_select
on governance.investment_cases for select
using (
  exists (
    select 1
    from governance.cases root_case
    where root_case.case_id = investment_cases.case_id
      and governance.can_access_fund(root_case.fund_id)
  )
);

create policy case_events_fund_member_select
on governance.case_events for select
using (
  exists (
    select 1
    from governance.cases root_case
    where root_case.case_id = case_events.case_id
      and governance.can_access_fund(root_case.fund_id)
  )
);

create policy orders_fund_member_select
on execution.orders for select
using (
  exists (
    select 1
    from execution.order_intents intent
    where intent.order_intent_id = orders.order_intent_id
      and governance.can_access_fund(intent.fund_id)
  )
);

create policy strategies_allocated_fund_member_select
on strategy.strategies for select
using (
  exists (
    select 1
    from governance.capital_allocations allocation
    where allocation.strategy_id = strategies.strategy_id
      and governance.can_access_fund(allocation.fund_id)
  )
);

create policy strategy_versions_allocated_fund_member_select
on strategy.versions for select
using (
  exists (
    select 1
    from governance.capital_allocations allocation
    where allocation.strategy_id = versions.strategy_id
      and governance.can_access_fund(allocation.fund_id)
  )
);

create policy departments_authenticated_select
on workforce.departments for select
using (governance.current_user_id() is not null);

create policy role_templates_authenticated_select
on workforce.role_templates for select
using (governance.current_user_id() is not null);

create policy agent_profiles_owner_select
on workforce.agent_profiles for select
using (
  owner_user_id = governance.current_user_id()
  or backup_owner_user_id = governance.current_user_id()
);

create index cases_fund_status_idx on governance.cases (fund_id, status, priority desc);
create index case_artifacts_artifact_idx on governance.case_artifacts (artifact_type, artifact_id);
create index research_packets_case_idx on research.research_packets (case_id, as_of desc);
create index agent_decisions_case_idx on research.agent_decisions (case_id, created_at desc);
create index signals_case_idx on strategy.signals (case_id, created_at desc);
create index intent_groups_case_idx on execution.intent_groups (trade_case_id, created_at desc);
create index order_intents_group_idx on execution.order_intents (intent_group_id, leg_index);
create index orders_intent_idx on execution.orders (order_intent_id, created_at);
create index fills_order_time_idx on execution.fills (order_id, event_time);
create index risk_decisions_request_idx on risk.risk_decisions (risk_request_id, created_at desc);
create index positions_fund_book_idx on accounting.positions (fund_id, book_id, instrument_id);
create index valuations_fund_time_idx on accounting.valuations (fund_id, as_of desc);
create index journals_fund_date_idx on accounting.journals (fund_id, accounting_date, effective_at);
create index findings_status_idx on audit.findings (status, severity, due_at);
create index incidents_status_idx on audit.incidents (status, severity, detected_at desc);

create or replace view api.investment_cases
with (security_invoker = true)
as
select
  cases.case_id,
  cases.fund_id,
  cases.display_id,
  cases.case_type,
  cases.priority,
  cases.status,
  cases.owner_department,
  cases.due_at,
  cases.trace_id,
  cases.created_at,
  cases.updated_at,
  investment.trigger_type,
  investment.primary_instrument_id,
  investment.decision_time,
  investment.strategy_version_id,
  investment.intent_group_id,
  investment.risk_decision_id,
  investment.terminal_reason
from governance.cases cases
join governance.investment_cases investment using (case_id);

create or replace view api.open_orders
with (security_invoker = true)
as
select
  orders.order_id,
  intents.fund_id,
  intents.book_id,
  intents.intent_group_id,
  intents.instrument_id,
  intents.side,
  intents.position_effect,
  orders.client_order_id,
  orders.broker_order_id,
  orders.state,
  orders.requested_quantity,
  orders.filled_quantity,
  orders.average_fill_price,
  orders.last_event_at,
  orders.trace_id
from execution.orders orders
join execution.order_intents intents using (order_intent_id)
where orders.state not in ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED');

create or replace view api.positions
with (security_invoker = true)
as
select
  position_id,
  fund_id,
  book_id,
  strategy_version_id,
  instrument_id,
  quantity,
  average_cost,
  cost_currency,
  realized_pnl,
  as_of,
  version
from accounting.positions;

create or replace view api.risk_status
with (security_invoker = true)
as
select distinct on (snapshots.fund_id, snapshots.book_id)
  snapshots.risk_snapshot_id,
  snapshots.fund_id,
  snapshots.book_id,
  snapshots.as_of,
  snapshots.gross_exposure,
  snapshots.net_exposure,
  snapshots.value_at_risk,
  snapshots.drawdown,
  snapshots.margin_used,
  snapshots.quality_status
from risk.snapshots snapshots
order by snapshots.fund_id, snapshots.book_id, snapshots.as_of desc;

create or replace view api.strategy_registry
with (security_invoker = true)
as
select
  strategies.strategy_id,
  strategies.strategy_code,
  strategies.name,
  strategies.family,
  strategies.directionality,
  strategies.status,
  versions.strategy_version_id,
  versions.version,
  versions.deployment_state,
  versions.effective_from,
  versions.effective_to,
  versions.artifact_hash
from strategy.strategies strategies
left join strategy.versions versions
  on versions.strategy_id = strategies.strategy_id
 and versions.version = strategies.current_version;

create or replace view api.agent_registry
with (security_invoker = true)
as
select
  agents.agent_id,
  agents.employee_code,
  agents.display_name,
  departments.department_code,
  departments.name as department_name,
  roles.role_code,
  agents.runtime,
  agents.employment_status,
  agents.current_version,
  agents.updated_at
from workforce.agent_profiles agents
join workforce.departments departments using (department_id)
join workforce.role_templates roles using (role_id);

create or replace function api.match_evidence_chunks(
  query_embedding extensions.vector(1536),
  query_as_of timestamptz,
  allowed_license_scopes text[],
  match_count integer default 20,
  minimum_similarity double precision default 0.0
)
returns table (
  chunk_id uuid,
  document_version_id uuid,
  content text,
  published_at timestamptz,
  observed_at timestamptz,
  similarity double precision
)
language sql
stable
security definer
set search_path = pg_catalog, research, extensions
as $$
  select
    chunks.chunk_id,
    chunks.document_version_id,
    chunks.content,
    chunks.published_at,
    chunks.observed_at,
    1 - (chunks.embedding <=> query_embedding) as similarity
  from research.evidence_chunks chunks
  where chunks.embedding is not null
    and chunks.observed_at <= query_as_of
    and (chunks.published_at is null or chunks.published_at <= query_as_of)
    and chunks.license_scope = any(allowed_license_scopes)
    and 1 - (chunks.embedding <=> query_embedding) >= minimum_similarity
  order by chunks.embedding <=> query_embedding
  limit greatest(1, least(match_count, 100));
$$;

create or replace function api.get_case_timeline(target_case_id uuid)
returns table (
  sequence bigint,
  event_type text,
  from_status text,
  to_status text,
  actor text,
  reason text,
  occurred_at timestamptz,
  payload jsonb
)
language sql
stable
security definer
set search_path = pg_catalog, governance
as $$
  select
    events.sequence,
    events.event_type,
    events.from_status,
    events.to_status,
    events.actor,
    events.reason,
    events.occurred_at,
    events.payload
  from governance.case_events events
  join governance.cases cases using (case_id)
  where events.case_id = target_case_id
    and governance.can_access_fund(cases.fund_id)
  order by events.sequence;
$$;

revoke all on all tables in schema governance, workforce, reference, research, quant,
  strategy, execution, risk, accounting, audit from anon, authenticated;
revoke all on all functions in schema governance, workforce, reference, research, quant,
  strategy, execution, risk, accounting, audit from public;
revoke all on schema governance, workforce, reference, research, quant,
  strategy, execution, risk, accounting, audit from anon, authenticated;

grant execute on function governance.current_user_id() to authenticated;
grant execute on function governance.current_jwt_role() to authenticated;
grant execute on function governance.can_access_fund(uuid) to authenticated;

grant usage on schema api to authenticated;
grant usage on schema api to service_role;
grant select on api.investment_cases, api.open_orders, api.positions,
  api.risk_status, api.strategy_registry, api.agent_registry to authenticated;
grant execute on function api.get_case_timeline(uuid) to authenticated;

grant select on governance.cases, governance.investment_cases, governance.case_events,
  governance.capital_allocations
  to authenticated;
grant select on execution.order_intents, execution.orders to authenticated;
grant select on accounting.positions to authenticated;
grant select on risk.snapshots to authenticated;
grant select on strategy.strategies, strategy.versions to authenticated;
grant select on workforce.departments, workforce.role_templates, workforce.agent_profiles
  to authenticated;

revoke execute on function api.match_evidence_chunks(
  extensions.vector, timestamptz, text[], integer, double precision
) from public, anon, authenticated;
grant execute on function api.match_evidence_chunks(
  extensions.vector, timestamptz, text[], integer, double precision
) to service_role;

commit;
