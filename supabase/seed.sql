-- Supabase 운영 DB Seed (db reset 시 마이그레이션 이후 실행)
-- 소유 구획: 각 본부가 자기 도메인 seed 를 섹션으로 추가한다.
-- 근거: docs/database/README.md 7절(적용), TEAM_YOUNGJU_CEO_HR_GUIDE.md 12(Y3)
-- 멱등: 모든 insert 는 ON CONFLICT DO NOTHING (재적용 안전).

begin;

-- ===========================================================================
-- [Agent Workforce 인사팀] Y3 Workforce Registry — 카탈로그 + P0 3명 등록
-- 소유: 영주. 모델 티어는 HR_AGENT_PROFILES_DRAFT.md 의 추천 전략을 반영한다:
--   HR-00 판단 중심 -> Deep(Bedrock Claude) / HR-01·HR-04 결정론 인접 -> Quick(Ollama)
-- 에이전트는 CANDIDATE/DRAFT 로 등록한다. 활성화는 QA Eval + CEO 승인 필요(HR 자기활성화 금지).
-- 모델 벤더 최종 확정은 ADR 대상 — 여기 model_version='proposed' 는 티어 전략 반영이며
-- 정확한 Model ID 는 Config/ADR 에서 교체한다(agent_profile_versions 버전업으로).
-- ===========================================================================

-- 1) 부서
insert into workforce.departments (department_code, name, mission)
values (
  'AGENT-WORKFORCE',
  'Agent Workforce 인사팀',
  'CEO 직속 Shared Service. 6개 본부의 업무량·품질·비용·Skill Gap을 근거로 Agent 채용·평가·교육·이동·비활성화를 관리한다. 투자 본부가 아니다.'
)
on conflict (department_code) do nothing;

-- 2) 모델 카탈로그 (Deep / Quick / 현행 baseline)
insert into workforce.models (provider, model_name, model_version, capabilities, cost_policy, allowed_environments)
values
  ('bedrock', 'claude-deep', 'proposed',
   '{"tier":"deep","use":["judgment","design","eval"],"note":"정확 Model ID는 ADR/Config"}'::jsonb,
   '{"class":"high"}'::jsonb, array['PRODUCTION','SHADOW']),
  ('ollama', 'local-quick', 'proposed',
   '{"tier":"quick","use":["classify","summarize","draft"],"note":"정확 Model ID는 ADR/Config"}'::jsonb,
   '{"class":"low"}'::jsonb, array['DEVELOPMENT','SHADOW','PRODUCTION']),
  ('nous', 'poolside-laguna-s', '2.1-free',
   '{"tier":"baseline","use":["dev"]}'::jsonb,
   '{"class":"free"}'::jsonb, array['DEVELOPMENT'])
on conflict (provider, model_name, model_version) do nothing;

-- 3) 역할 템플릿 (HR-00~04) — AGENT_EMPLOYEE_PROFILES.md §5 기준
insert into workforce.role_templates (role_code, department_id, mission, required_skills, forbidden_actions, kpi)
select v.role_code, d.department_id, v.mission, v.required_skills::jsonb, v.forbidden_actions::jsonb, v.kpi::jsonb
from (values
  ('HR-00',
   '6개 본부 업무량·품질·비용·Skill Gap 기반 Agent 채용/교육/비활성화 결정안 작성',
   '["ORG-01","ORG-02","ORG-03","ORG-04","ORG-05","HR-01","HR-02","HR-03","HR-04","HR-05","HR-06","OPS-01","OPS-02","QAA-03","QAA-04","QAA-05"]',
   '["investment_decision","order","risk_approval","self_hiring","iam_direct_grant","qa_gate_bypass"]',
   '{"metrics":["unnecessary_agent_growth","critical_skill_gap_aging","post_probation_perf","cost_per_case","access_revocation_sla"]}'),
  ('HR-01',
   '본부별 수요·병목 측정 → 역할·동시성·Model Budget 산정',
   '["HR-01","HR-02","OPS-01","OPS-02"]',
   '["headcount_by_return_only","cut_risk_or_qa_staff"]',
   '{"metrics":["sla_forecast_error","over_under_staffing","cost_per_throughput","critical_role_coverage","hiring_forecast_accuracy"]}'),
  ('HR-02',
   '승인된 Skill Gap → Job Profile + 비교 후보 구성',
   '["HR-02","HR-03"]',
   '["change_eval_after_results","request_broad_tool_scope_for_convenience"]',
   '{"metrics":["profile_duplication","schema_completeness","candidate_eval_entry","cost_error","excess_permission_request"]}'),
  ('HR-03',
   '후보 선발 + 재직 Agent 반복실패를 교육/개선/역할변경',
   '["HR-04","HR-05","QAA-04"]',
   '["production_approve_on_own_eval","skip_qaa04_independent_gate"]',
   '{"metrics":["eval_to_production_corr","probation_fail_rate","repeat_finding_reduction","false_promotion"]}'),
  ('HR-04',
   '승인된 Agent 최소권한 온보딩 + 이동·퇴직 시 완전 회수',
   '["HR-06","QAA-03"]',
   '["iam_admin_direct","assign_self_as_approver"]',
   '{"metrics":["zero_unauthorized_activation","provisioning_lead_time","revocation_sla","zero_orphan_case","zero_dormant_identity"]}')
) as v(role_code, mission, required_skills, forbidden_actions, kpi)
cross join (select department_id from workforce.departments where department_code = 'AGENT-WORKFORCE') d
on conflict (role_code) do nothing;

-- 4) Agent Roster (P0 3명: HR-00, HR-01, HR-04) — CANDIDATE 상태
insert into workforce.agent_profiles (employee_code, department_id, role_id, display_name, runtime, employment_status)
select v.employee_code, d.department_id, r.role_id, v.display_name, 'HERMES', 'CANDIDATE'
from (values
  ('HR-00', 'HR-00', 'agent-workforce-supervisor'),
  ('HR-01', 'HR-01', 'workforce-planning-agent'),
  ('HR-04', 'HR-04', 'lifecycle-coordinator')
) as v(employee_code, role_code, display_name)
join workforce.departments d on d.department_code = 'AGENT-WORKFORCE'
join workforce.role_templates r on r.role_code = v.role_code
on conflict (employee_code) do nothing;

-- 5) Agent Profile Version v1 (DRAFT) — 모델 티어 전략 반영
insert into workforce.agent_profile_versions
  (agent_id, version, model_id, prompt_artifact_path, skill_manifest, tool_allowlist,
   data_scopes, memory_namespace, token_budget, sla, eval_requirements, forbidden_actions,
   artifact_hash, effective_from, status)
select
  ap.agent_id, 1, m.model_id,
  'departments/07-agent-workforce/hermes/config.yaml#' || ap.employee_code,
  v.skill_manifest::jsonb, v.tool_allowlist::jsonb, v.data_scopes::jsonb, v.memory_ns,
  v.token_budget::jsonb, v.sla::jsonb, v.eval_req::jsonb, v.forbidden::jsonb,
  md5(ap.employee_code || ':v1'), now(), 'DRAFT'
from (values
  -- HR-00 -> Deep(Bedrock)
  ('HR-00', 'bedrock', 'claude-deep',
   '{"required":["ORG-01","HR-01","HR-02","HR-03","HR-04","OPS-01","QAA-03"]}',
   '{"read":["hiring_requests","capacity_snapshots","cost_snapshots","performance_reviews","agent_profiles","agent_profile_versions","agent_tool_permissions"],"propose":["hiring_requests","lifecycle_events"]}',
   '{"workforce":"read","audit":"read-via-api"}',
   'workforce/hr-00',
   '{"per_case_tokens":200000,"daily_tokens":2000000}',
   '{"decision_latency_hours":24}',
   '{"status":"PENDING_QA","owner":"qa-department","required_suites":["golden","adversarial"]}',
   '["investment_decision","order","risk_approval","self_hiring","iam_direct_grant","qa_gate_bypass"]'),
  -- HR-01 -> Quick(Ollama)
  ('HR-01', 'ollama', 'local-quick',
   '{"required":["HR-01","HR-02","OPS-01","OPS-02"]}',
   '{"read":["capacity_snapshots","cost_snapshots","performance_reviews","agent_profiles","hiring_requests"],"propose":[]}',
   '{"workforce":"read"}',
   'workforce/hr-01',
   '{"per_case_tokens":60000,"daily_tokens":1500000}',
   '{"report_cadence":"daily+weekly"}',
   '{"status":"PENDING_QA","owner":"qa-department","required_suites":["golden","adversarial"]}',
   '["headcount_by_return_only","cut_risk_or_qa_staff"]'),
  -- HR-04 -> Quick(Ollama)
  ('HR-04', 'ollama', 'local-quick',
   '{"required":["HR-06","QAA-03"]}',
   '{"read":["agent_profiles","agent_profile_versions","agent_tool_permissions","agent_skill_assignments"],"propose":["lifecycle_events"]}',
   '{"workforce":"read"}',
   'workforce/hr-04',
   '{"per_case_tokens":40000,"daily_tokens":800000}',
   '{"provisioning_lead_time_hours":4}',
   '{"status":"PENDING_QA","owner":"qa-department","required_suites":["golden","adversarial"]}',
   '["iam_admin_direct","assign_self_as_approver"]')
) as v(employee_code, model_provider, model_name, skill_manifest, tool_allowlist, data_scopes, memory_ns, token_budget, sla, eval_req, forbidden)
join workforce.agent_profiles ap on ap.employee_code = v.employee_code
join workforce.models m on m.provider = v.model_provider and m.model_name = v.model_name
on conflict (agent_id, version) do nothing;

commit;
