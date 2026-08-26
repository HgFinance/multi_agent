begin;

-- 현재 roster의 Ollama Worker profile을 운영 vLLM Worker model로 version-up한다.
--
-- 대상은 현재 current_version이 ollama/qwen3:1.7b/worker-test를 가리키는
-- profile뿐이다. 이미 RETIRED인 roster 행도 이력의 일관성을 위해 v2를 만들고
-- RETIRED 상태를 보존한다. Ollama model catalog는 local fallback 용도로
-- 계속 유지한다.

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
  v_worker_model_id uuid;
  v_target_count integer;
begin
  select model_id into strict v_worker_model_id
    from workforce.models
   where provider = 'vllm'
     and model_name = 'qwen2.5:14b'
     and model_version = 'qwen-awq-v1';

  create temporary table worker_model_seed (
    agent_id uuid primary key,
    employee_code text not null,
    old_version integer not null,
    profile_status text not null
  ) on commit drop;

  insert into worker_model_seed (agent_id, employee_code, old_version, profile_status)
  select ap.agent_id, ap.employee_code, ap.current_version, pv.status
    from workforce.agent_profiles ap
    join workforce.agent_profile_versions pv
      on pv.agent_id = ap.agent_id
     and pv.version = ap.current_version
    join workforce.models old_model
      on old_model.model_id = pv.model_id
   where old_model.provider = 'ollama'
     and old_model.model_name = 'qwen3:1.7b'
     and old_model.model_version = 'worker-test';

  select count(*) into v_target_count from worker_model_seed;
  if v_target_count = 0 then
    raise exception 'no current Ollama Worker profiles found for model migration';
  end if;

  -- 현재 version의 유효기간을 닫고, prompt/권한/context 계약은 그대로 복사한다.
  update workforce.agent_profile_versions old_version
     set effective_to = now()
    from worker_model_seed seed
   where old_version.agent_id = seed.agent_id
     and old_version.version = seed.old_version
     and old_version.effective_to is null;

  insert into workforce.agent_profile_versions (
    agent_id, version, model_id, prompt_artifact_path, skill_manifest,
    tool_allowlist, data_scopes, memory_namespace, token_budget, sla,
    eval_requirements, forbidden_actions, artifact_hash, effective_from,
    effective_to, status
  )
  select
    seed.agent_id,
    2,
    v_worker_model_id,
    old_version.prompt_artifact_path,
    old_version.skill_manifest,
    old_version.tool_allowlist,
    old_version.data_scopes,
    old_version.memory_namespace,
    old_version.token_budget,
    old_version.sla,
    old_version.eval_requirements,
    old_version.forbidden_actions,
    md5(seed.employee_code || ':profile-v2:qwen-awq-v1:20260826'),
    now(),
    null,
    seed.profile_status
  from worker_model_seed seed
  join workforce.agent_profile_versions old_version
    on old_version.agent_id = seed.agent_id
   and old_version.version = seed.old_version
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
      effective_to = excluded.effective_to,
      status = excluded.status;

  update workforce.agent_profiles ap
     set current_version = 2,
         updated_at = now()
    from worker_model_seed seed
   where ap.agent_id = seed.agent_id;
end;
$$;

commit;
