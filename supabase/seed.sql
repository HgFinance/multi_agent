-- Supabase local/Preview reset seed (Production에서는 실행하지 않는다)
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
  'hr-department',
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
  ('openai-codex', 'gpt-5.6-luna', 'profile-head',
   '{"tier":"head","use":["orchestration","supervision"]}'::jsonb,
   '{"class":"approved-profile"}'::jsonb, array['DEVELOPMENT','PAPER','PRODUCTION']),
  ('ollama', 'qwen3:1.7b', 'worker-test',
   '{"tier":"worker","use":["context","classification","summary"]}'::jsonb,
   '{"class":"local-low-memory"}'::jsonb, array['DEVELOPMENT','PAPER'])
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
cross join (select department_id from workforce.departments where department_code = 'hr-department') d
on conflict (role_code) do nothing;

-- 4) Agent Roster (P0 3명 + P1 2명) — CANDIDATE 상태
insert into workforce.agent_profiles (employee_code, department_id, role_id, display_name, runtime, employment_status)
select v.employee_code, d.department_id, r.role_id, v.display_name, 'HERMES', 'CANDIDATE'
from (values
  -- P0
  ('HR-00', 'HR-00', 'agent-workforce-supervisor'),
  ('HR-01', 'HR-01', 'workforce-planning-agent'),
  ('HR-04', 'HR-04', 'lifecycle-coordinator'),
  -- P1
  ('HR-02', 'HR-02', 'profile-architect'),
  ('HR-03', 'HR-03', 'selection-performance-agent')
) as v(employee_code, role_code, display_name)
join workforce.departments d on d.department_code = 'hr-department'
join workforce.role_templates r on r.role_code = v.role_code
on conflict (employee_code) do nothing;

-- 5) Agent Profile Version v1 (DRAFT) — 모델 티어 전략 반영
insert into workforce.agent_profile_versions
  (agent_id, version, model_id, prompt_artifact_path, skill_manifest, tool_allowlist,
   data_scopes, memory_namespace, token_budget, sla, eval_requirements, forbidden_actions,
   artifact_hash, effective_from, status)
select
  ap.agent_id, 1, m.model_id,
  'departments/07-agent-workforce/hermes/config.yaml#' || ap.display_name,
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
   '["iam_admin_direct","assign_self_as_approver"]'),
  -- HR-02 -> Deep(Bedrock). Profile/Prompt/Tool 설계가 산출물이라 판단 품질이 필요하다.
  ('HR-02', 'bedrock', 'claude-deep',
   '{"required":["HR-02","HR-03"]}',
   '{"read":["role_templates","skills","tools","models","agent_profiles","agent_profile_versions"],"propose":["candidates","agent_profile_versions","improvement_candidates"]}',
   '{"workforce":"read"}',
   'workforce/hr-02',
   '{"per_case_tokens":150000,"daily_tokens":1500000}',
   '{"profile_draft_latency_hours":12}',
   '{"status":"PENDING_QA","owner":"qa-department","required_suites":["golden","adversarial"]}',
   '["change_eval_after_results","request_broad_tool_scope_for_convenience"]'),
  -- HR-03 -> Deep(Bedrock). Eval 판정·Calibration 이 Critic 성격이라 강한 모델이 필요하다.
  ('HR-03', 'bedrock', 'claude-deep',
   '{"required":["HR-04","HR-05","QAA-04"]}',
   '{"read":["candidates","selection_reviews","performance_reviews","capacity_snapshots","agent_profile_versions"],"propose":["selection_reviews","performance_reviews","improvement_candidates"]}',
   '{"workforce":"read","audit":"read-via-api"}',
   'workforce/hr-03',
   '{"per_case_tokens":150000,"daily_tokens":1500000}',
   '{"eval_turnaround_hours":24}',
   '{"status":"PENDING_QA","owner":"qa-department","required_suites":["golden","adversarial"]}',
   '["production_approve_on_own_eval","skip_qaa04_independent_gate"]')
) as v(employee_code, model_provider, model_name, skill_manifest, tool_allowlist, data_scopes, memory_ns, token_budget, sla, eval_req, forbidden)
join workforce.agent_profiles ap on ap.employee_code = v.employee_code
join workforce.models m on m.provider = v.model_provider and m.model_name = v.model_name
on conflict (agent_id, version) do nothing;

-- ===========================================================================
-- [CEO Office] GOV-02 2단계 — 플레이스홀더 회원 1건
-- 소유: 영주. 근거: supabase/migrations/20260729000200_governance_workforce.sql
--   (governance.mandates.owner_user_id NOT NULL -> governance.user_profiles -> auth.users)
--
-- CEO Office 부서·Agent Roster 등재는 **의도적으로 하지 않는다**(2026-08-04 팀 결정).
-- 전체 Prototype이 나올 때까지 각 부서 직원 변동이 계속 예상되므로 workforce.agent_profiles
-- 등재는 뒤로 미루고, 그때까지는 departments/<n>/hermes/config.yaml이 Agent 정의의 기준이다.
-- 그 결과로 감수하는 것: governance.approvals.actor_agent_id가 workforce.agent_profiles FK라
-- 미등재 Agent의 결정은 그 칸을 채울 수 없다. 대신 결정 주체 부서를 conditions._decider에
-- 기록한다(approval.py decide() 참고) — approvals에는 부서 칸 자체가 없어서 Roster 등재
-- 여부와 무관하게 필요한 보완이다.
-- ===========================================================================

-- 플레이스홀더 회원 1건 — 회원가입 기능이 붙기 전까지의 임시 데이터
--
-- 왜 필요한가: governance.mandates.owner_user_id가 NOT NULL이면서 governance.user_profiles
-- FK다. user_profiles는 다시 auth.users FK라 2단계 삽입이 필요하다. 회원이 0건이면
-- Mandate를 만들 수 없다(GOV-01 작업 때 실제로 막혔던 지점).
--
-- 안전장치:
--   - email은 RFC 2606이 예약한 `.invalid` TLD를 써서 절대 실제 주소가 될 수 없게 한다.
--   - encrypted_password를 넣지 않아 이 계정으로는 로그인이 불가능하다.
--   - display_name에 PLACEHOLDER를 박아 조회 결과만 봐도 임시 데이터임이 드러난다.
--   - **자동 기본값으로 쓰지 않는다.** 승인자를 비워 보냈을 때 이 회원으로 조용히 채우면
--     감사 기록에 '사람이 승인했다'고 남는데 실제로는 아무도 승인하지 않은 상태가 된다
--     (approval.py decide() 주석과 같은 원칙). 호출자가 명시적으로 지정할 때만 쓴다.
-- 제거 조건: 회원가입/인증 기능이 붙으면 이 두 행을 실제 사용자로 교체한다.
insert into auth.users (id, aud, role, email)
values (
  '00000000-0000-4000-8000-00000000cec0',
  'authenticated', 'authenticated', 'placeholder-ceo-owner@hedgefund.invalid'
)
on conflict (id) do nothing;

insert into governance.user_profiles (user_id, display_name, timezone, status)
values (
  '00000000-0000-4000-8000-00000000cec0',
  'PLACEHOLDER Fund Owner (회원가입 전 임시)', 'Asia/Seoul', 'ACTIVE'
)
on conflict (user_id) do nothing;

-- 플레이스홀더 회원 2건 추가 (2026-08-12) — 프론트엔드 계정 전환 테스트용
--
-- 왜 3명인가: 온보딩·Mandate·적합성 프로필이 전부 `user_id` 기준으로 갈라지는데
-- 회원이 1명이면 "다른 사용자에게는 안 보인다"를 검증할 수 없다. 프론트엔드는
-- 이 3개 UUID를 하드코딩해 계정 전환 버튼으로 `X-User-Id`를 바꿔 보낸다.
--
-- **이건 인증이 아니다.** `X-User-Id`는 서명이 없어 누구나 아무 UUID나 보낼 수
-- 있다(apps/api/current_user.py 머리말). 폐쇄망 팀 테스트 전제이며, 공개 배포
-- 전에 실제 인증으로 교체해야 한다.
--
-- 위 1건과 같은 안전장치를 그대로 적용한다: `.invalid` TLD(RFC 2606),
-- 비밀번호 없음(로그인 불가), display_name에 PLACEHOLDER 표시.
-- UUID는 `...cec1`/`...cec2`로 기존 `...cec0` 다음 번호를 이어 붙여, 조회 결과에서
-- 세 계정이 한 묶음임이 드러나게 한다.
insert into auth.users (id, aud, role, email)
values
  ('00000000-0000-4000-8000-00000000cec1',
   'authenticated', 'authenticated', 'placeholder-user-2@hedgefund.invalid'),
  ('00000000-0000-4000-8000-00000000cec2',
   'authenticated', 'authenticated', 'placeholder-user-3@hedgefund.invalid')
on conflict (id) do nothing;

insert into governance.user_profiles (user_id, display_name, timezone, status)
values
  ('00000000-0000-4000-8000-00000000cec1',
   'PLACEHOLDER User 2 (계정 전환 테스트용)', 'Asia/Seoul', 'ACTIVE'),
  ('00000000-0000-4000-8000-00000000cec2',
   'PLACEHOLDER User 3 (계정 전환 테스트용)', 'Asia/Seoul', 'ACTIVE')
on conflict (user_id) do nothing;

-- ===========================================================================
-- [CEO Office] 테스트 계정 3개의 Fund + 소유 관계 (2026-08-18)
-- 소유: 영주. 근거: supabase/migrations/20260729000100_foundation_reference.sql
--   (accounting.funds, governance.fund_memberships, governance.can_access_fund)
--
-- ## 왜 이제야 넣나 (놓쳤던 것)
--
-- 위 플레이스홀더 회원 3명(2026-08-12)은 seed에 들어왔는데, 그 회원이 소유하는
-- Fund 3개는 라이브 DB에 직접 만들고 seed에는 넣지 않았다(2026-08-13). 그래서
-- `supabase db reset` 이후 프론트엔드가 하드코딩한 fund_id 3개
-- (ai-office/app/lib/currentAccount.ts)가 전부 존재하지 않는 행을 가리켰고,
-- `POST /ui/ceo/ask`의 Mandate 조회와 `POST /ui/investor-profiles`가 FK 위반으로
-- 실패한다. 회원만 심고 그 아래 체인(Fund -> 소유 관계)을 안 심은 것이 원인이다.
--
-- ## 왜 fund_memberships가 필요한가
--
-- 지금 서버에는 `user_id -> fund_id` 역참조 경로가 없다. 그래서 프론트엔드가
-- `fund_id`를 짝으로 들고 다니고(currentAccount.ts), BFF는 그걸 요청 body로
-- 받는다(apps/api/ceo.py CeoAsk.fund_id). 이 표가 채워지면 그 우회가 필요 없어지고,
-- migration에 이미 있는 `governance.can_access_fund()` RLS 함수도 그제서야
-- 실제로 동작한다(지금은 membership이 0건이라 service_role 외 전부 false).
--
-- **이 seed는 그 우회를 자동으로 걷어내지 않는다.** BFF가 이 표를 읽도록 바꾸는
-- 것은 별도 작업이고, 그 전까지 프론트엔드 하드코딩은 그대로 둔다.
--
-- ## 이건 인증이 아니다
--
-- `X-User-Id`는 서명이 없어 누구나 아무 UUID나 보낼 수 있다
-- (apps/api/current_user.py 머리말). 여기 심는 소유 관계는 "표시·조회 필터의
-- 근거"이지 접근 통제가 아니다. 공개망 노출 전에 실제 인증으로 교체해야 한다.
-- ===========================================================================

-- 1) 테스트 Fund 3개
--
-- fund_id는 프론트엔드(currentAccount.ts)와 CEO Office 모듈 __main__ 자체 점검
-- (approval.py / case_root.py / committee.py의 fund = "b13f5cd1-...")이 이미
-- 하드코딩한 값이다. **여기서 바꾸면 그쪽이 전부 깨진다** — 함께 고쳐야 한다.
--
-- 아래 값은 **추정이 아니라 실 DB 조회 결과**다(2026-08-18, 새 Supabase 프로젝트
-- 에서 `select fund_id, fund_code, name, base_currency, inception_date, status
-- from accounting.funds`로 확인). 이 파일이 실 DB와 어긋나 있던 것이 원래 문제였으므로,
-- 여기 값을 바꿀 때는 반드시 실 DB와 대조한다(scripts/check_test_user_wiring.py).
--
-- 같은 DB에 있는 `ACC01-PAPER`(KRW) Fund는 회계본부 소유라 이 섹션에 넣지 않는다 -
-- 테스트 계정 초기화 범위가 아니다.
insert into accounting.funds (fund_id, fund_code, name, base_currency, inception_date, status)
values
  ('b13f5cd1-5df0-4025-92cf-9be03b1a0296', 'TEST-CEO-MANDATE',
   'CEO Mandate Contract Test Fund', 'USD', date '2026-01-01', 'ACTIVE'),
  ('50a3c28c-6cee-4bcf-ab07-fa97093dca8e', 'TEST-USER2-MANDATE',
   'User 2 Test Fund', 'USD', date '2026-08-13', 'ACTIVE'),
  ('3838f7d6-0c7c-4e54-85f3-316a451e7eeb', 'TEST-USER3-MANDATE',
   'User 3 Test Fund', 'USD', date '2026-08-13', 'ACTIVE')
on conflict (fund_id) do nothing;

-- 2) 소유 관계 3행 — 각 계정이 자기 Fund의 OWNER
--
-- 역할을 OWNER 하나만 넣는 이유: 세 계정은 서로 다른 Mandate를 가진 **각자의**
-- Fund 소유자다. 교차 역할(CIO/VIEWER 등)을 임의로 넣으면 "다른 사용자에게는
-- 안 보인다"를 검증하려고 만든 계정 구분이 흐려진다. 교차 접근이 실제로 필요해질
-- 때 그 요구사항과 함께 추가한다(개발 원칙 9).
--
-- effective_to는 비운다(무기한 ACTIVE). can_access_fund()가
-- `effective_from <= now() and (effective_to is null or effective_to > now())`로
-- 판정하므로 기본값 그대로면 즉시 유효하다.
insert into governance.fund_memberships (fund_id, user_id, role, status)
values
  ('b13f5cd1-5df0-4025-92cf-9be03b1a0296',
   '00000000-0000-4000-8000-00000000cec0', 'OWNER', 'ACTIVE'),
  ('50a3c28c-6cee-4bcf-ab07-fa97093dca8e',
   '00000000-0000-4000-8000-00000000cec1', 'OWNER', 'ACTIVE'),
  ('3838f7d6-0c7c-4e54-85f3-316a451e7eeb',
   '00000000-0000-4000-8000-00000000cec2', 'OWNER', 'ACTIVE')
on conflict (fund_id, user_id, role) do nothing;

commit;
