begin;

-- 담당자: 영주 (Agent Workforce 인사팀)
-- 근거: TEAM_YOUNGJU_CEO_HR_GUIDE.md 4.3(probation_periods/performance_actions/quality_snapshots/
--       workforce_plans), 6.2(Hiring Workflow의 Shadow Probation), 6.4(Mover의 Learning/PIP/Role Change),
--       GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3.4(Scorecard quality), 7절(상태표: quality_snapshots·
--       workforce_plans 저장소 미구현으로 명시), database/README.md 6절/9절
--
-- 팀 가이드 4.3에 문서화됐지만 지금까지 Migration에 없던 4개 Table을 추가한다.
-- 기존 8개 Migration 은 수정하지 않고 8번째로 append 한다.
-- begin; 은 파일 최상단이어야 한다 (계약 Test test_migration_sequence_is_complete).

-- ▶ Probation: 채용 Workflow의 Shadow/Paper 수습 단계. selection_reviews(채용 결정 자체)와는
--   다른 대상이다 — 이건 채용된 이후의 관찰 기간 자체를 추적한다.
create table workforce.probation_periods (
  probation_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  profile_version_id uuid not null references workforce.agent_profile_versions(profile_version_id),
  stage text not null check (stage in ('SHADOW', 'PAPER')),
  started_at timestamptz not null,
  ended_at timestamptz,
  success_metrics jsonb not null default '{}'::jsonb,
  result text check (result in ('PASSED', 'FAILED', 'EXTENDED')),
  created_at timestamptz not null default now(),
  check (ended_at is null or ended_at > started_at),
  -- 종료된 수습은 결과가 있어야 한다 (관찰만 하고 판정을 미루지 않는다).
  check (ended_at is null or result is not null)
);

create index probation_periods_agent_idx
  on workforce.probation_periods (agent_id, profile_version_id);

-- ▶ Performance Action: 6.4 Mover의 Learning/PIP/Role Change, 6.5 조직 재귀적 자기 개선의
--   조치 결과. performance_reviews(평가 자체)와 분리한다 — 리뷰는 평가, Action은 그 뒤의 조치.
create table workforce.performance_actions (
  action_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  review_id uuid references workforce.performance_reviews(review_id),
  action_type text not null check (action_type in ('LEARNING', 'PIP', 'ROLE_CHANGE', 'DEACTIVATION')),
  plan jsonb not null,
  due_at timestamptz not null,
  verification jsonb,
  status text not null default 'OPEN'
    check (status in ('OPEN', 'IN_PROGRESS', 'VERIFIED', 'CANCELLED', 'OVERDUE')),
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  -- 완료 처리는 검증 근거가 있어야 한다 (계획만 세우고 검증 없이 닫지 않는다).
  check (status <> 'VERIFIED' or verification is not null)
);

create index performance_actions_agent_status_idx
  on workforce.performance_actions (agent_id, status);

-- ▶ Quality Snapshot: API 설계서 3.4 get_department_scorecard의 quality 블록을 채우는 저장소.
--   eval_score 원본은 QA/감사본부 소유(audit.eval_runs)이므로 값을 복제하지 않고 eval_run_id로만
--   참조한다 — 여기 채우는 값은 인사팀이 집계하는 finding_count/rework_rate 뿐이다.
create table workforce.quality_snapshots (
  snapshot_id uuid primary key default gen_random_uuid(),
  department_id uuid references workforce.departments(department_id),
  agent_id uuid references workforce.agent_profiles(agent_id),
  profile_version_id uuid references workforce.agent_profile_versions(profile_version_id),
  window_start timestamptz not null,
  window_end timestamptz not null,
  eval_run_id uuid references audit.eval_runs(eval_run_id),
  finding_count bigint,
  rework_rate numeric(12, 8),
  role_kpi jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  check (window_end > window_start),
  check (department_id is not null or agent_id is not null)
);

create index quality_snapshots_department_window_idx
  on workforce.quality_snapshots (department_id, window_start);
create index quality_snapshots_agent_window_idx
  on workforce.quality_snapshots (agent_id, window_start);

-- ▶ Workforce Plan: 6.5/10.3의 주간·월간 인력·비용 계획. Skill Gap과 채용/재교육 Action을
--   Version으로 남긴다 — 계획을 덮어쓰지 않고 새 행을 추가한다.
create table workforce.workforce_plans (
  plan_id uuid primary key default gen_random_uuid(),
  department_id uuid references workforce.departments(department_id),
  period_start timestamptz not null,
  period_end timestamptz not null,
  skill_gaps jsonb not null default '{}'::jsonb,
  actions jsonb not null default '[]'::jsonb,
  budget jsonb not null default '{}'::jsonb,
  assumptions jsonb not null default '{}'::jsonb,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'APPROVED', 'ACTIVE', 'RETIRED')),
  approval_id uuid references governance.approvals(approval_id),
  created_at timestamptz not null default now(),
  check (period_end > period_start)
);

create index workforce_plans_department_period_idx
  on workforce.workforce_plans (department_id, period_start);

-- 내부 Table 은 Data API 에 직접 노출하지 않는다 (TEAM_YOUNGJU 7.2).
alter table workforce.probation_periods enable row level security;
alter table workforce.performance_actions enable row level security;
alter table workforce.quality_snapshots enable row level security;
alter table workforce.workforce_plans enable row level security;

commit;
