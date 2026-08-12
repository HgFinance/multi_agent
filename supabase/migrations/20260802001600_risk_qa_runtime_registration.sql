begin;

-- Risk/QA Domain Owner: 동규
--
-- 이 migration은 Risk/QA Agent Profile을 등록만 한다.
-- 등록은 구현 완료·운영 활성화를 의미하지 않는다.
-- 모든 프로필은 PROBATION/DRAFT로 시작하며, 실제 활성화는
-- Workforce 승인·QA 검증·환경별 rollout 절차에서 별도로 수행한다.

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
begin
    insert into workforce.departments (department_code, name, mission, status)
    values
        ('risk-management', 'Risk Management Department',
            'Independent pre-trade risk and compliance protective controls', 'ACTIVE'),
        ('qa-department', 'AI QA Audit Department',
            'Independent evidence, trace, permission and incident verification', 'ACTIVE')
    on conflict (department_code) do update
        set name = excluded.name,
            mission = excluded.mission,
            status = 'ACTIVE',
            updated_at = now();

    select department_id into strict risk_department_id
      from workforce.departments
     where department_code = 'risk-management';

    select department_id into strict qa_department_id
      from workforce.departments
     where department_code = 'qa-department';

    insert into workforce.models (
        provider, model_name, model_version, capabilities, cost_policy,
        allowed_environments, data_policy, status
    )
    values (
        'nous', 'poolside/laguna-s-2.1:free', 'profile-baseline',
        '{"structured_output": true}'::jsonb,
        '{"budget_class": "baseline"}'::jsonb,
        array['DEVELOPMENT', 'PAPER'],
        '{"no_production_credentials": true}'::jsonb,
        'ACTIVE'
    )
    on conflict (provider, model_name, model_version) do update
        set capabilities = excluded.capabilities,
            cost_policy = excluded.cost_policy,
            allowed_environments = excluded.allowed_environments,
            data_policy = excluded.data_policy,
            status = 'ACTIVE'
    returning model_id into baseline_model_id;

    if baseline_model_id is null then
        select model_id into strict baseline_model_id
          from workforce.models
         where provider = 'nous'
           and model_name = 'poolside/laguna-s-2.1:free'
           and model_version = 'profile-baseline';
    end if;

    create temporary table risk_qa_agent_seed (
        employee_code text primary key,
        department_code text not null,
        role_code text not null,
        display_name text not null,
        prompt_path text not null,
        required_skills jsonb not null,
        tools jsonb not null,
        data_scopes jsonb not null,
        forbidden_actions jsonb not null,
        implemented_baseline boolean not null
    ) on commit drop;

    insert into risk_qa_agent_seed values
        ('RSK-00', 'risk-management', 'RSK-00', 'Risk Supervisor',
            'departments/03-risk/hermes/config.yaml#personalities.risk-supervisor',
            '["risk_aggregation"]', '["case.read", "case.delegate"]',
            '["risk"]', '["oms.submit", "ledger.write", "risk.trading_state.write"]', false),
        ('RSK-01', 'risk-management', 'RSK-01', 'Pre-trade Risk Analyst',
            'departments/03-risk/hermes/config.yaml#personalities.pre-trade-risk-analyst',
            '["pre_trade_risk"]', '["case.read", "risk.case.check"]',
            '["risk", "execution"]', '["oms.submit", "ledger.write"]', true),
        ('RSK-02', 'risk-management', 'RSK-02', 'Market Liquidity Risk Agent',
            'departments/03-risk/hermes/config.yaml#personalities.market-liquidity-risk-agent',
            '["market_liquidity"]', '["case.read", "risk.trading_state.read"]',
            '["risk", "market"]', '["oms.submit", "ledger.write", "risk.limit.write"]', false),
        ('RSK-04', 'risk-management', 'RSK-04', 'Compliance Policy Agent',
            'departments/03-risk/hermes/config.yaml#personalities.compliance-policy-agent',
            '["policy_retrieval", "citation_validation"]',
            '["case.read", "risk.compliance.check"]', '["risk", "policy"]',
            '["oms.submit", "ledger.write"]', true),
        ('RSK-05', 'risk-management', 'RSK-05', 'Derivatives Margin Risk Agent',
            'departments/03-risk/hermes/config.yaml#personalities.derivatives-margin-risk-agent',
            '["derivatives_margin"]', '["case.read"]', '["risk", "derivatives"]',
            '["oms.submit", "ledger.write"]', false),
        ('RSK-06', 'risk-management', 'RSK-06', 'Operational Counterparty Risk Agent',
            'departments/03-risk/hermes/config.yaml#personalities.operational-counterparty-risk-agent',
            '["counterparty_risk"]', '["case.read", "risk.trading_state.record.read"]',
            '["risk", "counterparty"]',
            '["oms.submit", "ledger.write", "risk.trading_state.write"]', true),
        ('QAA-00', 'qa-department', 'QAA-00', 'QA Audit Supervisor',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.qa-audit-supervisor',
            '["qa_aggregation"]', '["case.read", "case.delegate"]',
            '["qa", "audit"]', '["oms.submit", "ledger.write", "risk.limit.write"]', false),
        ('QAA-01', 'qa-department', 'QAA-01', 'Evidence QA Agent',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.evidence-qa-agent',
            '["evidence_qa", "pit_validation"]',
            '["case.read", "qa.case.check", "qa.evidence.check"]',
            '["qa", "evidence"]', '["oms.submit", "ledger.write"]', true),
        ('QAA-02', 'qa-department', 'QAA-02', 'Hallucination Critic',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.hallucination-critic',
            '["hallucination_review"]', '["case.read"]', '["qa", "evidence"]',
            '["oms.submit", "ledger.write"]', true),
        ('QAA-03', 'qa-department', 'QAA-03', 'Tool Permission Security Reviewer',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.tool-permission-security-reviewer',
            '["tool_permission_audit"]',
            '["case.read", "qa.tool_permission.check", "qa.tool_permission.unauthorized_count.read"]',
            '["qa", "permissions"]',
            '["oms.submit", "ledger.write", "risk.limit.write"]', true),
        ('QAA-04', 'qa-department', 'QAA-04', 'Model Risk Agent',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.model-risk-agent',
            '["model_reproducibility"]', '["case.read"]', '["qa", "model"]',
            '["oms.submit", "ledger.write"]', false),
        ('QAA-05', 'qa-department', 'QAA-05', 'Agent Ops Monitor',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.agent-ops-monitor',
            '["agent_observability"]', '["case.read", "qa.ops.evaluate"]',
            '["qa", "operations"]', '["oms.submit", "ledger.write"]', true),
        ('QAA-06', 'qa-department', 'QAA-06', 'Internal Audit Agent',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.internal-audit-agent',
            '["internal_audit"]', '["case.read"]', '["qa", "audit"]',
            '["oms.submit", "ledger.write", "risk.limit.write"]', false),
        ('QAA-07', 'qa-department', 'QAA-07', 'Incident Postmortem Agent',
            'departments/06-ai-qa-audit/hermes/config.yaml#personalities.incident-postmortem-agent',
            '["incident_postmortem"]',
            '["case.read", "qa.incident.event.write", "qa.incident.timeline.read",'
            ' "qa.corrective_action.open", "qa.corrective_action.start",'
            ' "qa.corrective_action.submit_for_verification",'
            ' "qa.corrective_action.verify_and_close", "qa.corrective_action.cancel"]',
            '["qa", "incidents"]', '["oms.submit", "ledger.write"]', true);

    insert into workforce.role_templates (
        role_code, department_id, mission, required_skills,
        forbidden_actions, kpi, status
    )
    select s.role_code,
           d.department_id,
           s.display_name || ' controlled test runtime role',
           s.required_skills,
           s.forbidden_actions,
           jsonb_build_object(
               'registered_by', '20260802001600',
               'implemented_baseline', s.implemented_baseline
           ),
           'ACTIVE'
      from risk_qa_agent_seed s
      join workforce.departments d using (department_code)
    on conflict (role_code) do update
        set department_id = excluded.department_id,
            mission = excluded.mission,
            required_skills = excluded.required_skills,
            forbidden_actions = excluded.forbidden_actions,
            kpi = excluded.kpi,
            status = 'ACTIVE';

    insert into workforce.agent_profiles (
        employee_code, department_id, role_id, display_name,
        runtime, employment_status, current_version
    )
    select s.employee_code,
           d.department_id,
           r.role_id,
           s.display_name,
           'HERMES',
           'PROBATION',
           1
      from risk_qa_agent_seed s
      join workforce.departments d using (department_code)
      join workforce.role_templates r on r.role_code = s.role_code
    on conflict (employee_code) do update
        set department_id = excluded.department_id,
            role_id = excluded.role_id,
            display_name = excluded.display_name,
            runtime = 'HERMES',
            employment_status = 'PROBATION',
            current_version = 1,
            updated_at = now();

    insert into workforce.agent_profile_versions (
        agent_id, version, model_id, prompt_artifact_path, skill_manifest,
        tool_allowlist, data_scopes, memory_namespace, token_budget, sla,
        eval_requirements, forbidden_actions, artifact_hash, effective_from, status
    )
    select p.agent_id,
           1,
           baseline_model_id,
           s.prompt_path,
           jsonb_build_object('skills', s.required_skills, 'test_only', true),
           s.tools,
           s.data_scopes,
           'risk-qa:test:' || lower(s.employee_code),
           '{"max_input_tokens": 6000, "max_output_tokens": 1200}'::jsonb,
           '{"timeout_seconds": 60, "max_attempts": 1}'::jsonb,
           jsonb_build_object(
               'requires_qa_approval', true,
               'implemented_baseline', s.implemented_baseline
           ),
           s.forbidden_actions,
           md5(s.employee_code || ':test-profile-v1'),
           now(),
           'DRAFT'
      from risk_qa_agent_seed s
      join workforce.agent_profiles p using (employee_code)
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
            status = 'DRAFT';

    update workforce.departments d
       set supervisor_agent_id = p.agent_id,
           updated_at = now()
      from workforce.agent_profiles p
     where (d.department_code, p.employee_code) in (
         ('risk-management', 'RSK-00'),
         ('qa-department', 'QAA-00')
     );
end;
$$;

commit;
