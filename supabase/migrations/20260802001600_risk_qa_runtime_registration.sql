begin;

-- Risk/QA Domain Owner: 동규
-- 이 Migration은 다른 부서의 실행 코드나 권한을 변경하지 않고,
-- Risk·QA Runtime Profile과 Domain Event 수신 이력만 등록한다.

create table if not exists audit.domain_events (
  event_id uuid primary key,
  event_type text not null,
  source_department text not null,
  trace_id uuid not null,
  payload jsonb not null,
  occurred_at timestamptz not null,
  received_at timestamptz not null default now(),
  status text not null default 'PROCESSED'
    check (status in ('RECEIVED', 'PROCESSED', 'FAILED'))
);

create index if not exists domain_events_trace_idx
  on audit.domain_events (trace_id, occurred_at);

do $$
declare
  risk_department_id uuid;
  qa_department_id uuid;
  baseline_model_id uuid;
  agent record;
begin
  insert into workforce.departments (department_code, name, mission, status)
  values
    ('risk-management', 'Risk Management Department',
     'Independent pre-trade risk, compliance and protective controls', 'ACTIVE'),
    ('qa-department', 'AI QA and Audit Department',
     'Independent evidence, trace, permission and incident verification', 'ACTIVE')
  on conflict (department_code) do update
    set name = excluded.name,
        mission = excluded.mission,
        status = 'ACTIVE',
        updated_at = now();

  select department_id into risk_department_id
    from workforce.departments where department_code = 'risk-management';
  select department_id into qa_department_id
    from workforce.departments where department_code = 'qa-department';

  insert into workforce.models (
    provider, model_name, model_version, capabilities, cost_policy,
    allowed_environments, data_policy, status
  ) values (
    'nous', 'poolside/laguna-s-2.1:free', 'profile-baseline',
    '{"structured_output": true}'::jsonb,
    '{"budget_class": "baseline"}'::jsonb,
    array['DEVELOPMENT', 'PAPER'],
    '{"external_data": "restricted"}'::jsonb,
    'ACTIVE'
  )
  on conflict (provider, model_name, model_version) do update
    set status = 'ACTIVE';

  select model_id into baseline_model_id
    from workforce.models
   where provider = 'nous'
     and model_name = 'poolside/laguna-s-2.1:free'
     and model_version = 'profile-baseline';

  -- Role templates are the contract identity. Runtime prompts remain in Hermes
  -- config.yaml and are referenced by prompt_artifact_path below.
  insert into workforce.role_templates (
    role_code, department_id, mission, required_skills,
    forbidden_actions, kpi, status
  ) values
    ('RSK-00', risk_department_id, 'Aggregate independent risk findings and escalate conflicts',
     '["RSK-01", "RSK-02", "RSK-03", "RSK-04", "RSK-06"]'::jsonb,
     '["oms.submit", "ledger.write", "risk.trading_state.write"]'::jsonb,
     '{"decision_reproducibility": true}'::jsonb, 'ACTIVE'),
    ('RSK-01', risk_department_id, 'Run deterministic pre-trade risk checks',
     '["RSK-01", "RSK-02"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"stale_input_block": true}'::jsonb, 'ACTIVE'),
    ('RSK-02', risk_department_id, 'Interpret market exposure and liquidity concentration',
     '["RSK-02", "RSK-03"]'::jsonb,
     '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb,
     '{"exposure_explanation": true}'::jsonb, 'ACTIVE'),
    ('RSK-04', risk_department_id, 'Check versioned mandate and restricted-list policy',
     '["RSK-04"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"citation_required": true}'::jsonb, 'ACTIVE'),
    ('RSK-05', risk_department_id, 'Assess derivatives margin and tail risk',
     '["RSK-05"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"greeks_evidence_required": true}'::jsonb, 'ACTIVE'),
    ('RSK-06', risk_department_id, 'Assess counterparty failure and protective action',
     '["RSK-06"]'::jsonb,
     '["oms.submit", "ledger.write", "risk.trading_state.write"]'::jsonb,
     '{"unknown_broker_escalation": true}'::jsonb, 'ACTIVE'),
    ('QAA-00', qa_department_id, 'Aggregate independent QA findings and maintain the gate',
     '["QAA-01", "QAA-02", "QAA-03", "QAA-05"]'::jsonb,
     '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb,
     '{"finding_independence": true}'::jsonb, 'ACTIVE'),
    ('QAA-01', qa_department_id, 'Verify claim evidence and point-in-time citations',
     '["QAA-01"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"unsupported_claim_escape": 0}'::jsonb, 'ACTIVE'),
    ('QAA-02', qa_department_id, 'Detect unsupported and contradictory agent claims',
     '["QAA-02"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"contradiction_escape": 0}'::jsonb, 'ACTIVE'),
    ('QAA-03', qa_department_id, 'Review tool and data permission separation',
     '["QAA-03", "QAA-05"]'::jsonb,
     '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb,
     '{"unauthorized_tool_call": 0}'::jsonb, 'ACTIVE'),
    ('QAA-04', qa_department_id, 'Validate model and strategy reproducibility',
     '["QAA-04"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"regression_escape": 0}'::jsonb, 'ACTIVE'),
    ('QAA-05', qa_department_id, 'Monitor agent, feed, queue and model health',
     '["OPS-01", "OPS-02"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"critical_incident_detection": true}'::jsonb, 'ACTIVE'),
    ('QAA-06', qa_department_id, 'Audit segregation of duties and control evidence',
     '["QAA-06"]'::jsonb,
     '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb,
     '{"open_finding_age": true}'::jsonb, 'ACTIVE'),
    ('QAA-07', qa_department_id, 'Reconstruct incidents and verify corrective actions',
     '["QAA-06", "OPS-03"]'::jsonb,
     '["oms.submit", "ledger.write"]'::jsonb,
     '{"fact_inference_separation": true}'::jsonb, 'ACTIVE')
  on conflict (role_code) do update
    set department_id = excluded.department_id,
        mission = excluded.mission,
        required_skills = excluded.required_skills,
        forbidden_actions = excluded.forbidden_actions,
        kpi = excluded.kpi,
        status = 'ACTIVE';

  for agent in
    select * from (values
      ('RSK-00', 'RSK-00', 'Risk Supervisor', 'departments/03-risk/hermes/config.yaml#personalities.risk-supervisor', '["case.read", "case.delegate"]'::jsonb, '["risk", "portfolio"]'::jsonb, '["oms.submit", "ledger.write", "risk.trading_state.write"]'::jsonb),
      ('RSK-01', 'RSK-01', 'Pre-Trade Risk Analyst', 'departments/03-risk/hermes/config.yaml#personalities.pre-trade-risk-analyst', '["case.read", "risk.case.check"]'::jsonb, '["risk", "execution"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb),
      ('RSK-02', 'RSK-02', 'Market and Liquidity Risk Analyst', 'departments/03-risk/hermes/config.yaml#personalities.market-liquidity-risk-agent', '["case.read", "risk.trading_state.read"]'::jsonb, '["risk", "portfolio"]'::jsonb, '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb),
      ('RSK-04', 'RSK-04', 'Compliance Policy Agent', 'departments/03-risk/hermes/config.yaml#personalities.compliance-policy-agent', '["case.read", "risk.compliance.check"]'::jsonb, '["risk", "policy"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb),
      ('RSK-05', 'RSK-05', 'Derivatives and Margin Risk Agent', 'departments/03-risk/hermes/config.yaml#personalities.derivatives-margin-risk-agent', '["case.read"]'::jsonb, '["risk", "derivatives"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb),
      ('RSK-06', 'RSK-06', 'Operational Counterparty Risk Agent', 'departments/03-risk/hermes/config.yaml#personalities.operational-counterparty-risk-agent', '["case.read", "risk.trading_state.record.read"]'::jsonb, '["risk", "counterparty"]'::jsonb, '["oms.submit", "ledger.write", "risk.trading_state.write"]'::jsonb),
      ('QAA-00', 'QAA-00', 'QA Audit Supervisor', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.qa-audit-supervisor', '["case.read", "case.delegate"]'::jsonb, '["qa", "audit"]'::jsonb, '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb),
      ('QAA-01', 'QAA-01', 'Evidence QA Agent', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.evidence-qa-agent', '["case.read", "qa.case.check", "qa.evidence.check"]'::jsonb, '["qa", "evidence"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb),
      ('QAA-02', 'QAA-02', 'Hallucination and Contradiction Critic', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.hallucination-critic', '["case.read"]'::jsonb, '["qa", "evidence"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb),
      ('QAA-03', 'QAA-03', 'Tool Permission Security Reviewer', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.tool-permission-security-reviewer', '["case.read", "qa.tool_permission.check", "qa.tool_permission.unauthorized_count.read"]'::jsonb, '["qa", "audit", "workforce"]'::jsonb, '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb),
      ('QAA-04', 'QAA-04', 'Model Risk and Evaluation Validator', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.model-risk-agent', '[]'::jsonb, '["qa", "quant", "release"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb),
      ('QAA-05', 'QAA-05', 'Agent Ops and SRE Monitor', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.agent-ops-monitor', '["case.read", "qa.ops.evaluate"]'::jsonb, '["qa", "ops"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb),
      ('QAA-06', 'QAA-06', 'Internal Audit Agent', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.internal-audit-agent', '[]'::jsonb, '["qa", "audit"]'::jsonb, '["oms.submit", "ledger.write", "risk.limit.write"]'::jsonb),
      ('QAA-07', 'QAA-07', 'Incident and Postmortem Analyst', 'departments/06-ai-qa-audit/hermes/config.yaml#personalities.incident-postmortem-agent', '["case.read", "qa.incident.event.write", "qa.incident.timeline.read", "qa.corrective_action.open", "qa.corrective_action.start", "qa.corrective_action.submit_for_verification", "qa.corrective_action.verify_and_close", "qa.corrective_action.cancel"]'::jsonb, '["qa", "incident", "audit"]'::jsonb, '["oms.submit", "ledger.write"]'::jsonb)
    ) as x(employee_code, role_code, display_name, prompt_path, tools, scopes, forbidden)
  loop
    insert into workforce.agent_profiles (
      employee_code, department_id, role_id, display_name, runtime,
      employment_status, current_version
    ) values (
      agent.employee_code,
      case when agent.employee_code like 'RSK-%' then risk_department_id else qa_department_id end,
      (select role_id from workforce.role_templates where role_code = agent.role_code),
      agent.display_name, 'HERMES', 'ACTIVE', 1
    )
    on conflict (employee_code) do update
      set department_id = excluded.department_id,
          role_id = excluded.role_id,
          display_name = excluded.display_name,
          runtime = 'HERMES',
          employment_status = 'ACTIVE',
          current_version = 1,
          updated_at = now();

    insert into workforce.agent_profile_versions (
      agent_id, version, model_id, prompt_artifact_path, skill_manifest,
      tool_allowlist, data_scopes, memory_namespace, token_budget, sla,
      eval_requirements, forbidden_actions, artifact_hash, effective_from, status
    ) values (
      (select agent_id from workforce.agent_profiles where employee_code = agent.employee_code),
      1, baseline_model_id, agent.prompt_path,
      jsonb_build_object('role_code', agent.role_code, 'config_source', agent.prompt_path),
      agent.tools, agent.scopes, 'risk-qa:' || lower(agent.employee_code),
      '{"max_turns": 100}'::jsonb, '{"timeout_seconds": 60}'::jsonb,
      '{"required": ["schema_validation", "permission_boundary"]}'::jsonb,
      agent.forbidden, md5(agent.employee_code || ':profile:v1'), now(), 'ACTIVE'
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

-- Profile registration does not imply implementation completion.  Keep only the
-- currently executable deterministic/RAG/operational baselines ACTIVE; prompt-only
-- personas remain registered but require implementation before activation.
update workforce.agent_profiles
set employment_status = case
  when employee_code in ('RSK-01', 'RSK-04', 'QAA-01', 'QAA-03', 'QAA-05', 'QAA-07')
    then 'ACTIVE'
  else 'PROBATION'
end,
updated_at = now()
where employee_code in (
  'RSK-00', 'RSK-01', 'RSK-02', 'RSK-04', 'RSK-05', 'RSK-06',
  'QAA-00', 'QAA-01', 'QAA-02', 'QAA-03', 'QAA-04', 'QAA-05', 'QAA-06', 'QAA-07'
);

update workforce.agent_profile_versions v
set status = case
  when p.employee_code in ('RSK-01', 'RSK-04', 'QAA-01', 'QAA-03', 'QAA-05', 'QAA-07')
    then 'ACTIVE'
  else 'DRAFT'
end
from workforce.agent_profiles p
where v.agent_id = p.agent_id
  and p.employee_code in (
    'RSK-00', 'RSK-01', 'RSK-02', 'RSK-04', 'RSK-05', 'RSK-06',
    'QAA-00', 'QAA-01', 'QAA-02', 'QAA-03', 'QAA-04', 'QAA-05', 'QAA-06', 'QAA-07'
  );

update workforce.departments d
     set supervisor_agent_id = p.agent_id, updated_at = now()
    from workforce.agent_profiles p
   where d.department_code = 'risk-management'
     and p.employee_code = 'RSK-00';
  update workforce.departments d
     set supervisor_agent_id = p.agent_id, updated_at = now()
    from workforce.agent_profiles p
   where d.department_code = 'qa-department'
     and p.employee_code = 'QAA-00';
end $$;

commit;
