begin;

-- Raw point-in-time inputs are retained for Risk projection/replay.  This
-- private control database is reached only by domain services; browser clients
-- have no direct table policy because the JSON payload can contain
-- portfolio details.
create table if not exists risk.input_snapshots (
  event_id uuid primary key,
  event_type text not null,
  source_stream text not null,
  trace_id uuid not null,
  occurred_at timestamptz not null,
  payload jsonb not null,
  received_at timestamptz not null default now()
);

create index if not exists risk_input_snapshots_stream_time_idx
  on risk.input_snapshots (source_stream, occurred_at desc);

alter table risk.input_snapshots enable row level security;

drop policy if exists risk_input_snapshots_service_role_all on risk.input_snapshots;

drop trigger if exists risk_input_snapshots_append_only on risk.input_snapshots;
create trigger risk_input_snapshots_append_only
  before update or delete on risk.input_snapshots
  for each row execute function governance.reject_append_only_change();

create table if not exists risk.derivative_snapshots (
  derivative_snapshot_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  trace_id uuid not null,
  as_of timestamptz not null,
  calculation_version text not null,
  input_hash text not null,
  aggregate_delta numeric(38, 12) not null,
  aggregate_gamma numeric(38, 12) not null,
  aggregate_vega_per_1pct numeric(38, 12) not null,
  stress_loss numeric(38, 12) not null,
  margin_requirement numeric(38, 12) not null,
  vol_surface_hash text not null,
  quality_status text not null check (quality_status in ('PASS', 'FAIL')),
  gate_decision text not null check (gate_decision in ('PASS', 'REJECT')),
  reason_codes jsonb not null default '[]'::jsonb,
  greeks jsonb not null,
  created_at timestamptz not null default now(),
  unique (fund_id, trace_id, as_of, calculation_version)
);

alter table risk.derivative_snapshots enable row level security;

drop policy if exists derivative_snapshots_fund_member_select on risk.derivative_snapshots;
create policy derivative_snapshots_fund_member_select
  on risk.derivative_snapshots
  for select using (governance.can_access_fund(fund_id));

drop trigger if exists derivative_snapshots_append_only on risk.derivative_snapshots;
create trigger derivative_snapshots_append_only
  before update or delete on risk.derivative_snapshots
  for each row execute function governance.reject_append_only_change();

-- Canonical worker model used by the current test/paper runtime.  Department
-- heads remain separate Hermes profiles and are not changed by this seed.
insert into workforce.models (
  provider, model_name, model_version, capabilities, cost_policy,
  allowed_environments, data_policy, status
)
values (
  'ollama', 'qwen3:1.7b', 'worker-test',
  '{"structured_output": true, "worker_context": true}'::jsonb,
  '{"budget_class": "local-low-memory"}'::jsonb,
  array['DEVELOPMENT', 'PAPER'],
  '{"no_production_credentials": true, "binding_decision": false}'::jsonb,
  'ACTIVE'
)
on conflict (provider, model_name, model_version) do update
set capabilities = excluded.capabilities,
    cost_policy = excluded.cost_policy,
    allowed_environments = excluded.allowed_environments,
    data_policy = excluded.data_policy,
    status = 'ACTIVE';

do $$
declare
  seed record;
  v_department_id uuid;
  v_role_id uuid;
  v_agent_id uuid;
  v_worker_model_id uuid;
begin
  select model_id into strict v_worker_model_id
  from workforce.models
  where provider = 'ollama'
    and model_name = 'qwen3:1.7b'
    and model_version = 'worker-test';

  for seed in
    select * from (values
      ('market-liquidity-worker', 'risk-management', 'Market and Liquidity Worker',
       'Market and liquidity risk context', '["market_snapshot", "liquidity_metrics"]'::jsonb,
       '["risk.trading_state.read", "risk.p1.snapshot"]'::jsonb,
       '["fund", "market", "case"]'::jsonb, 'always'),
      ('pre-trade-risk-worker', 'risk-management', 'Pre-trade Risk Worker',
       'Pre-trade risk gate context', '["pre_trade", "limit_checks"]'::jsonb,
       '["risk.case.check"]'::jsonb,
       '["fund", "order_intent", "case"]'::jsonb, 'always'),
      ('compliance-policy-worker', 'risk-management', 'Compliance Policy Worker',
       'Point-in-time policy evidence context', '["pit_policy", "citations"]'::jsonb,
       '["risk.compliance.check"]'::jsonb,
       '["fund", "policy", "case"]'::jsonb, 'when_compliance_evidence_exists'),
      ('derivatives-counterparty-worker', 'risk-management', 'Derivatives Counterparty Worker',
       'Derivatives and counterparty exposure context', '["greeks", "margin", "counterparty"]'::jsonb,
       '["risk.trading_state.record.read"]'::jsonb,
       '["fund", "derivatives", "counterparty", "case"]'::jsonb, 'when_counterparty_or_derivatives_signal_exists'),
      ('evidence-qa-worker', 'qa-department', 'Evidence QA Worker',
       'Evidence and citation QA context', '["evidence", "citations"]'::jsonb,
       '["qa.evidence.check"]'::jsonb,
       '["case", "evidence"]'::jsonb, 'always'),
      ('hallucination-critic-worker', 'qa-department', 'Hallucination Critic Worker',
       'Unsupported and contradicted claim context', '["hallucination", "contradiction"]'::jsonb,
       '["qa.evidence.rag"]'::jsonb,
       '["case", "evidence"]'::jsonb, 'when_unsupported_claim_exists'),
      ('model-and-internal-audit-worker', 'qa-department', 'Model and Internal Audit Worker',
       'Model risk and internal audit context', '["model_risk", "internal_audit"]'::jsonb,
       '["qa.model_risk.evaluate", "qa.internal_audit.evaluate"]'::jsonb,
       '["case", "audit", "model"]'::jsonb, 'when_audit_input_exists'),
      ('ops-and-permission-worker', 'qa-department', 'Operations and Permission Worker',
       'Agent operations and tool permission context', '["ops", "permissions"]'::jsonb,
       '["qa.ops.evaluate", "qa.tool_permission.check"]'::jsonb,
       '["case", "audit", "permissions"]'::jsonb, 'when_ops_input_exists'),
      ('incident-postmortem-worker', 'qa-department', 'Incident Postmortem Worker',
       'Incident and corrective-action context', '["incident", "postmortem"]'::jsonb,
       '["qa.incident.record"]'::jsonb,
       '["case", "incident", "audit"]'::jsonb, 'when_incident_exists')
    ) as seed_values(
      employee_code, department_code, display_name, mission, skills,
      tools, scopes, trigger
    )
  loop
    select d.department_id into strict v_department_id
    from workforce.departments d
    where d.department_code = seed.department_code;

    insert into workforce.role_templates (
      role_code, department_id, mission, required_skills,
      forbidden_actions, kpi, status
    )
    values (
      seed.employee_code, v_department_id, seed.mission, seed.skills,
      '["oms.submit", "ledger.write", "risk.override", "qa.verdict.override"]'::jsonb,
      jsonb_build_object('trigger', seed.trigger, 'output_contract', 'worker-context.v1'),
      'ACTIVE'
    )
    on conflict (role_code) do update
    set department_id = excluded.department_id,
        mission = excluded.mission,
        required_skills = excluded.required_skills,
        forbidden_actions = excluded.forbidden_actions,
        kpi = excluded.kpi,
        status = 'ACTIVE'
    returning role_id into v_role_id;

    insert into workforce.agent_profiles (
      employee_code, department_id, role_id, display_name, runtime,
      employment_status, current_version
    )
    values (
      seed.employee_code, v_department_id, v_role_id, seed.display_name,
      'LANGGRAPH', 'ACTIVE', 1
    )
    on conflict (employee_code) do update
    set department_id = excluded.department_id,
        role_id = excluded.role_id,
        display_name = excluded.display_name,
        runtime = 'LANGGRAPH',
        employment_status = 'ACTIVE',
        current_version = 1,
        updated_at = now()
    returning agent_id into v_agent_id;

    insert into workforce.agent_profile_versions (
      agent_id, version, model_id, prompt_artifact_path, skill_manifest,
      tool_allowlist, data_scopes, memory_namespace, token_budget, sla,
      eval_requirements, forbidden_actions, artifact_hash, effective_from, status
    )
    values (
      v_agent_id, 1, v_worker_model_id,
      'departments/' || case when seed.department_code = 'risk-management' then '03-risk' else '06-ai-qa-audit' end
        || '/hermes/config.yaml#worker.' || seed.employee_code,
      jsonb_build_object('skills', seed.skills, 'trigger', seed.trigger),
      seed.tools,
      seed.scopes,
      'worker:' || seed.department_code || ':' || seed.employee_code,
      '{"max_output_tokens": 900, "temperature": 0}'::jsonb,
      '{"max_latency_seconds": 30, "max_attempts": 3}'::jsonb,
      '{"schema": "worker-context.v1", "binding": false}'::jsonb,
      '["oms.submit", "ledger.write", "risk.override", "qa.verdict.override"]'::jsonb,
      md5(seed.employee_code || ':worker-context-v1'),
      now(),
      'ACTIVE'
    )
    on conflict (agent_id, version) do update
    set model_id = excluded.model_id,
        prompt_artifact_path = excluded.prompt_artifact_path,
        skill_manifest = excluded.skill_manifest,
        tool_allowlist = excluded.tool_allowlist,
        data_scopes = excluded.data_scopes,
        memory_namespace = excluded.memory_namespace,
        token_budget = excluded.token_budget,
        sla = excluded.sla,
        eval_requirements = excluded.eval_requirements,
        forbidden_actions = excluded.forbidden_actions,
        artifact_hash = excluded.artifact_hash,
        effective_from = excluded.effective_from,
        status = 'ACTIVE';
  end loop;
end;
$$;

commit;
