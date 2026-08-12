begin;

-- Current runtime model catalog alignment (2026-08-04)
--
-- Do not rewrite 20260802001600: it may already be applied in shared
-- environments. This follow-up migration retires its legacy Nous/Laguna
-- catalog rows and aligns Risk/QA profile versions with the current Head.
-- LangGraph Workers are recorded separately as Ollama qwen3:1.7b.

insert into workforce.models (
    provider,
    model_name,
    model_version,
    capabilities,
    cost_policy,
    allowed_environments,
    data_policy,
    status
)
values (
    'openai-codex',
    'gpt-5.6-luna',
    'profile-head',
    '{"structured_output": true, "supervision": true}'::jsonb,
    '{"budget_class": "approved-profile"}'::jsonb,
    array['DEVELOPMENT', 'PAPER', 'PRODUCTION'],
    '{"orders": false, "risk_override": false, "ledger_write": false}'::jsonb,
    'ACTIVE'
)
on conflict (provider, model_name, model_version)
do update set
    capabilities = excluded.capabilities,
    cost_policy = excluded.cost_policy,
    allowed_environments = excluded.allowed_environments,
    data_policy = excluded.data_policy,
    status = 'ACTIVE';

insert into workforce.models (
    provider,
    model_name,
    model_version,
    capabilities,
    cost_policy,
    allowed_environments,
    data_policy,
    status
)
values (
    'ollama',
    'qwen3:1.7b',
    'worker-test',
    '{"structured_output": true, "worker_context": true}'::jsonb,
    '{"budget_class": "local-low-memory"}'::jsonb,
    array['DEVELOPMENT', 'PAPER'],
    '{"no_production_credentials": true, "binding_decision": false}'::jsonb,
    'ACTIVE'
)
on conflict (provider, model_name, model_version)
do update set
    capabilities = excluded.capabilities,
    cost_policy = excluded.cost_policy,
    allowed_environments = excluded.allowed_environments,
    data_policy = excluded.data_policy,
    status = 'ACTIVE';

do $$
declare
    current_head_model_id uuid;
begin
    select model_id
      into strict current_head_model_id
      from workforce.models
     where provider = 'openai-codex'
       and model_name = 'gpt-5.6-luna'
       and model_version = 'profile-head';

    update workforce.agent_profile_versions as profile_version
       set model_id = current_head_model_id
      from workforce.agent_profiles as profile
      join workforce.departments as department
        on department.department_id = profile.department_id
     where profile_version.agent_id = profile.agent_id
       and department.department_code in ('risk-management', 'qa-department')
       and profile_version.status in (
           'DRAFT', 'EVALUATING', 'APPROVED', 'ACTIVE', 'SUSPENDED'
       );

    update workforce.models
       set status = 'RETIRED'
     where provider = 'nous'
       and model_name in (
           'poolside/laguna-s-2.1:free',
           'poolside-laguna-s'
       );
end $$;

commit;
