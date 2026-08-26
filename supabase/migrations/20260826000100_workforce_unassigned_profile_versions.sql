begin;

-- Roster 프로필 미배정 13건에 Profile Version 배정 (2026-08-26)
--
-- 소유: 영주. 선행: 20260824000100_workforce_roster_full_reconcile.sql
--
-- ## 왜 필요한가
--
-- 20260824000100은 8개 부서 직원을 workforce.agent_profiles에 등재하면서
-- current_version=1로 세웠지만 대응하는 workforce.agent_profile_versions 행은
-- 만들지 않았다. roster 조회(_ROSTER_SELECT)는 pv.version = ap.current_version으로
-- left join하므로 그 직원들은 GET /workforce/v1/roster에서 current_profile_version
-- 이 null인 "프로필 미배정" 상태로 나온다.
--
-- HR-00은 반대로 v1(bedrock/claude-deep, DRAFT)이 있는데 current_version=0이라
-- 역시 join이 비어 미배정으로 보였다.
--
-- ## 모델 배정 규칙 (영주 지시, 2026-08-26)
--
--   부서장(HERMES)      -> openai-codex / gpt-5.6-luna / profile-head
--   LLM Worker(LANGGRAPH) -> vllm / qwen2.5:14b / qwen-awq-v1
--
-- vllm 행은 이 migration이 새로 만든다. 이름은 각 부서 hermes/config.yaml의
-- employee_runtime(provider: vllm-openai, model_default: qwen2.5-14b-instruct-awq,
-- model_profile: qwen-awq-v1)에서 가져왔다. 기존 ollama/qwen3:1.7b(worker-test)에
-- 묶인 risk/qa Worker 3명은 이 migration의 범위가 아니다 — 그 3명은 이미 배정돼
-- 있어 "미배정 건"이 아니고, 운영 모델 교체는 별도 결정이다.
--
-- ## 범위에서 제외한 것: 결정론 러너 5개
--
-- desk-runner / risk-runner / qa-runner / back-office-runner / ceo-runner는 각
-- config.yaml deterministic_workers 블록에 `llm: false`로 선언돼 있어 모델을
-- 부르지 않는다. agent_profile_versions.model_id는 not null이라 배정하려면
-- 실행되지 않는 모델을 참조시키거나 sentinel 모델 행을 새로 만들어야 하는데,
-- 둘 다 마스터 플랜에 없는 표현이라 정하지 않고 미배정으로 남긴다(영주, 2026-08-26).
--
-- ## Version status
--
-- 부서장은 DRAFT(RSK-00/QAA-00 선례) — PROBATION 상태이고 QA Eval·CEO 승인을
-- 아직 거치지 않았다. Worker는 ACTIVE(20260804000400 risk/qa Worker 선례) —
-- agent_profiles.employment_status가 이미 ACTIVE인 실행 중 Worker다.
-- 이 migration은 등록만 하며 QA 검증·CEO 활성화 승인을 대신하지 않는다.

insert into workforce.models (
  provider, model_name, model_version, capabilities, cost_policy,
  allowed_environments, data_policy, status
)
values (
  'vllm', 'qwen2.5:14b', 'qwen-awq-v1',
  '{"structured_output": true, "worker_context": true, "awq_quantized": true}'::jsonb,
  '{"budget_class": "self-hosted-worker"}'::jsonb,
  array['DEVELOPMENT', 'PAPER', 'PRODUCTION'],
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
  v_head_model_id uuid;
  v_worker_model_id uuid;
begin
  select model_id into strict v_head_model_id
    from workforce.models
   where provider = 'openai-codex'
     and model_name = 'gpt-5.6-luna'
     and model_version = 'profile-head';

  select model_id into strict v_worker_model_id
    from workforce.models
   where provider = 'vllm'
     and model_name = 'qwen2.5:14b'
     and model_version = 'qwen-awq-v1';

  -- skill_manifest / forbidden_actions는 20260824000100이 이미 채운
  -- workforce.role_templates(required_skills, forbidden_actions)를 그대로 쓴다.
  -- 여기서 다시 적는 값은 config.yaml에만 있고 role_templates에 없는 것뿐이다.
  create temporary table profile_seed (
    employee_code text primary key,
    version integer not null,
    kind text not null,                 -- HEAD | WORKER
    prompt_artifact_path text not null,
    tool_allowlist jsonb not null,
    data_scopes jsonb not null,
    memory_namespace text not null,
    worker_trigger text,                -- WORKER만
    output_contract text                -- WORKER만
  ) on commit drop;

  insert into profile_seed values
    -- 부서장 (HERMES) -> openai-codex / gpt-5.6-luna
    ('RES-00', 1, 'HEAD',
      'departments/01-research/hermes/config.yaml#agent.personalities.research-supervisor',
      '["case.read","case.delegate","research.outcomes.read"]'::jsonb,
      '["case","research"]'::jsonb, 'head:research-department:res-00', null, null),
    ('TRD-00', 1, 'HEAD',
      'departments/02-trading/hermes/config.yaml#agent.personalities.trading-supervisor',
      '["case.read","case.delegate"]'::jsonb,
      '["case","trading"]'::jsonb, 'head:trading-department:trd-00', null, null),
    ('QNT-00', 1, 'HEAD',
      'departments/04-quant-backtest/hermes/config.yaml#agent.personalities.quant-backtest-supervisor',
      '["case.read","case.delegate","quant.experiment.read"]'::jsonb,
      '["case","quant"]'::jsonb, 'head:quant-backtest-department:qnt-00', null, null),
    ('ACC-00', 1, 'HEAD',
      'departments/05-accounting-portfolio/hermes/config.yaml#agent.personalities.portfolio-control-supervisor',
      '["case.read","case.delegate"]'::jsonb,
      '["case","accounting"]'::jsonb, 'head:accounting-portfolio-department:acc-00', null, null),
    ('CEO-00', 1, 'HEAD',
      'departments/00-ceo-office/hermes/config.yaml#agent.personalities.executive-orchestrator',
      '["governance.mandate.read","governance.case.create","governance.case.decide","governance.approval.request","governance.committee.convene"]'::jsonb,
      '["case","governance"]'::jsonb, 'head:ceo-agent:ceo-00', null, null),
    -- HR-00만 v2다. v1(bedrock/claude-deep)은 seed.sql이 만든 DRAFT라
    -- 덮어쓰지 않고 아래에서 RETIRED로 마감한 뒤 v2를 새로 발행한다.
    ('HR-00', 2, 'HEAD',
      'departments/07-agent-workforce/hermes/config.yaml#agent.personalities.agent-workforce-supervisor',
      '["workforce.roster.read","workforce.scorecard.read","workforce.idle_agents.read","workforce.hiring_request.propose"]'::jsonb,
      '["case","workforce"]'::jsonb, 'head:hr-department:hr-00', null, null),

    -- LLM Worker (LANGGRAPH) -> vllm / qwen2.5:14b
    ('competing-explanation-worker', 1, 'WORKER',
      'departments/01-research/hermes/config.yaml#workers.competing-explanation-worker',
      '["research.outcomes.read","research.evidence.search"]'::jsonb,
      '["case","research","evidence"]'::jsonb,
      'worker:research-department:competing-explanation-worker',
      'proposal_draft', 'research.worker-context.v1'),
    ('holdings-analyst-worker', 1, 'WORKER',
      'departments/01-research/hermes/config.yaml#workers.holdings-analyst-worker',
      '["research.evidence.search","research.news.read","research.market_snapshot.read"]'::jsonb,
      '["case","research","market"]'::jsonb,
      'worker:research-department:holdings-analyst-worker',
      'holding_question', 'research.worker-context.v1'),
    ('strategy-author-worker', 1, 'WORKER',
      'departments/04-quant-backtest/hermes/config.yaml#workers.strategy-author-worker',
      '["quant.template_catalog.read","quant.vocabulary.read","quant.strategy_spec.propose"]'::jsonb,
      '["case","quant","strategy"]'::jsonb,
      'worker:quant-backtest-department:strategy-author-worker',
      'strategy_authoring', 'quant.worker-context.v1'),
    ('result-interpretation-worker', 1, 'WORKER',
      'departments/04-quant-backtest/hermes/config.yaml#workers.result-interpretation-worker',
      '["quant.experiment_card.read"]'::jsonb,
      '["case","quant","experiment"]'::jsonb,
      'worker:quant-backtest-department:result-interpretation-worker',
      'experiment_card', 'quant.worker-context.v1'),
    ('exception-investigation-worker', 1, 'WORKER',
      'departments/05-accounting-portfolio/hermes/config.yaml#workers.exception-investigation-worker',
      '["accounting.ledger.read","accounting.reconciliation.read","accounting.nav_close.read"]'::jsonb,
      '["case","accounting","reconciliation"]'::jsonb,
      'worker:accounting-portfolio-department:exception-investigation-worker',
      'always', 'accounting.worker-context.v1'),
    ('executive-briefing-worker', 1, 'WORKER',
      'departments/00-ceo-office/hermes/config.yaml#workers.executive-briefing-worker',
      '["ceo.department_reports.read"]'::jsonb,
      '["case","governance","department_reports"]'::jsonb,
      'worker:ceo-agent:executive-briefing-worker',
      'always', 'ceo.worker-context.v1'),
    ('profile-architecture-worker', 1, 'WORKER',
      'departments/07-agent-workforce/hermes/config.yaml#workers.profile-architecture-worker',
      '["workforce.hiring_request.read","workforce.profile_version.read","workforce.improvement.read","workforce.tool_catalog.read","workforce.policy_boundary.read"]'::jsonb,
      '["case","workforce"]'::jsonb,
      'worker:hr-department:profile-architecture-worker',
      'profile_architecture_request', 'workforce.worker-context.v1');

  -- HR-00 v1 마감: 같은 Agent에 살아있는 DRAFT가 둘이 되지 않게 이력으로 닫는다.
  update workforce.agent_profile_versions v
     set status = 'RETIRED',
         effective_to = now()
    from workforce.agent_profiles p
   where v.agent_id = p.agent_id
     and p.employee_code = 'HR-00'
     and v.version = 1
     and v.status <> 'RETIRED';

  insert into workforce.agent_profile_versions (
    agent_id, version, model_id, prompt_artifact_path, skill_manifest,
    tool_allowlist, data_scopes, memory_namespace, token_budget, sla,
    eval_requirements, forbidden_actions, artifact_hash, effective_from, status
  )
  select
    ap.agent_id,
    s.version,
    case when s.kind = 'HEAD' then v_head_model_id else v_worker_model_id end,
    s.prompt_artifact_path,
    case when s.kind = 'HEAD'
         then jsonb_build_object('skills', rt.required_skills)
         else jsonb_build_object('skills', rt.required_skills, 'trigger', s.worker_trigger)
    end,
    s.tool_allowlist,
    s.data_scopes,
    s.memory_namespace,
    case when s.kind = 'HEAD'
         then '{"max_input_tokens": 6000, "max_output_tokens": 1200}'::jsonb
         else '{"max_output_tokens": 900, "temperature": 0}'::jsonb
    end,
    case when s.kind = 'HEAD'
         then '{"timeout_seconds": 60, "max_attempts": 1}'::jsonb
         else '{"max_latency_seconds": 30, "max_attempts": 3}'::jsonb
    end,
    case when s.kind = 'HEAD'
         then jsonb_build_object('requires_qa_approval', true, 'required_suites',
                                 jsonb_build_array('golden', 'adversarial'))
         else jsonb_build_object('schema', s.output_contract, 'binding', false)
    end,
    rt.forbidden_actions,
    md5(s.employee_code || ':profile-v' || s.version || ':20260826'),
    now(),
    case when s.kind = 'HEAD' then 'DRAFT' else 'ACTIVE' end
  from profile_seed s
  join workforce.agent_profiles ap on ap.employee_code = s.employee_code
  join workforce.role_templates rt on rt.role_id = ap.role_id
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
      effective_to = null,
      status = excluded.status;

  -- roster join이 current_version을 따라가므로 HR-00만 0 -> 2로 올린다.
  -- 나머지 12명은 20260824000100이 이미 1로 세워 뒀다.
  update workforce.agent_profiles ap
     set current_version = s.version,
         updated_at = now()
    from profile_seed s
   where ap.employee_code = s.employee_code
     and ap.current_version <> s.version;
end;
$$;

commit;
