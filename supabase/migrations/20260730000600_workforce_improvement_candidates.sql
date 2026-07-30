begin;

-- 담당자: 영주 (Agent Workforce 인사팀)
-- 근거: HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F19(승인형 Hermes 자기 개선),
--       TEAM_YOUNGJU_CEO_HR_GUIDE.md 6.5, HEDGE_FUND_MASTER_PLAN.md 5.10,
--       database/README.md 6절(새 Timestamp Migration 추가) / 9절(변경 규칙)
--
-- F19 ImprovementCandidate 를 workforce 스키마에 추가한다. 기존 5개 Migration 은
-- 수정하지 않고 6번째 Migration 으로 append 한다 (database/README.md 6절).
-- begin; 은 파일 최상단이어야 한다 (계약 Test test_migration_sequence_is_complete).
-- 앱 레이어 계약은 departments/07-agent-workforce/improvements/{candidate,workflow}.py 다.
--
--   improvement_candidates       : 개선 후보 (근거·대상·위험·롤백)
--   improvement_candidate_events : 후보 생명주기 전이 (Append-only, 같은 candidate_id 추적)

create table workforce.improvement_candidates (
  candidate_id uuid primary key default gen_random_uuid(),

  -- 개선 대상 (6.5 후보 유형). target 은 유형별로 달라 polymorphic 참조(governance.approvals
  -- 의 object_type/id 패턴과 동일)로 두고 target_ref 텍스트로 식별한다.
  target_type text not null
    check (target_type in ('SKILL', 'PROFILE', 'WORKFLOW', 'AGENT')),
  target_ref text not null,
  target_current_version integer not null check (target_current_version > 0),
  rollback_target_version integer not null check (rollback_target_version > 0),

  -- 후보를 만든 주체. author 는 workflow 의 자기승인 차단 기준이다. 가능하면 실제
  -- Agent Profile 을 참조한다(부서 Hermes 가 아직 Registry 에 없으면 null).
  author text not null,
  author_agent_id uuid references workforce.agent_profiles(agent_id),

  evidence_ids jsonb not null default '[]'::jsonb,   -- 근거 ID (Case/Incident/Eval/사용자 교정)
  expected_effect text not null,
  risk_class text not null check (risk_class in ('LOW', 'MEDIUM', 'HIGH')),

  -- 배포가 만든 새 Profile Version (PROFILE 대상일 때). 배포 전에는 null.
  deployed_profile_version_id uuid
    references workforce.agent_profile_versions(profile_version_id),

  status text not null default 'PROPOSED'
    check (status in (
      'PROPOSED', 'EVALUATING', 'SHADOW', 'PENDING_APPROVAL', 'APPROVED',
      'REJECTED', 'DEPLOYED', 'OBSERVING', 'KEPT', 'ROLLED_BACK', 'RETIRED'
    )),

  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  -- 근거 없는 후보 금지 (F19: 근거 ID 기록). jsonb 배열이 비어있지 않아야 한다.
  check (jsonb_typeof(evidence_ids) = 'array' and jsonb_array_length(evidence_ids) > 0),
  -- 롤백 대상은 현재 Version 이하의 실재 Version.
  check (rollback_target_version <= target_current_version)
);

create table workforce.improvement_candidate_events (
  event_id uuid primary key default gen_random_uuid(),
  candidate_id uuid not null
    references workforce.improvement_candidates(candidate_id),
  sequence integer not null check (sequence > 0),
  from_status text,
  to_status text not null,
  -- 전이 수행자. 승인(APPROVED) Event 에서는 author 와 달라야 한다(권한 분리는 Service
  -- 계층 workflow.py 가 강제하고, 여기서는 근거를 Append-only 로 남긴다).
  actor text not null,
  reason text,
  -- 승인 근거: QA Eval (audit.eval_runs). 승인 외 전이에서는 null.
  qa_eval_run_id uuid references audit.eval_runs(eval_run_id),
  occurred_at timestamptz not null,
  recorded_at timestamptz not null default now(),
  unique (candidate_id, sequence)
);

-- Append-only: 전이 기록은 수정·삭제 불가 (case_events / lifecycle_events 와 동일 패턴).
create trigger improvement_candidate_events_append_only
before update or delete on workforce.improvement_candidate_events
for each row execute function governance.reject_append_only_change();

-- updated_at 자동 갱신.
create trigger improvement_candidates_touch_updated_at
before update on workforce.improvement_candidates
for each row execute function governance.touch_updated_at();

-- 내부 Table 은 Data API 에 직접 노출하지 않는다 (TEAM_YOUNGJU 7.2). RLS 활성화,
-- authenticated 정책은 두지 않아 service_role/Domain Service 로만 접근한다.
alter table workforce.improvement_candidates enable row level security;
alter table workforce.improvement_candidate_events enable row level security;

commit;
