begin;

-- 담당자: 영주 (Agent Workforce 인사팀)
-- 근거: TEAM_YOUNGJU_CEO_HR_GUIDE.md 4.3(access_requests/access_assignments), 6.4(Joiner/Mover/Leaver),
--       GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 3.5(request_access), database/README.md 6절/9절
--
-- Y4 Access Lifecycle. 기존 5+1개 Migration 은 수정하지 않고 7번째로 append 한다.
-- begin; 은 파일 최상단이어야 한다 (계약 Test test_migration_sequence_is_complete).
--
-- ▶ 기존 workforce.agent_tool_permissions 와 역할이 다르다. 중복 저장하지 않는다.
--     agent_tool_permissions : Profile Version 이 "가질 수 있는" 도구 권한 선언 (설계)
--     access_requests        : 권한을 달라는 요청과 승인 워크플로 (절차)
--     access_assignments     : Platform/IAM 이 "실제로 부여·회수한" 사실 기록 (증거)
--
--   인사팀은 요청까지만 한다. 실제 Identity·권한 생성은 Platform/IAM Service 만 하며,
--   그 결과를 provisioning_ref 로 되받아 기록한다 (CLAUDE.md 권한 분리).

create table workforce.access_requests (
  request_id uuid primary key default gen_random_uuid(),
  agent_id uuid not null references workforce.agent_profiles(agent_id),
  profile_version_id uuid references workforce.agent_profile_versions(profile_version_id),

  -- 요청 대상. TOOL 이면 tool_id 를 채우고, DATA/ENVIRONMENT 는 resource_ref 로 식별한다.
  resource_kind text not null check (resource_kind in ('TOOL', 'DATA', 'ENVIRONMENT')),
  tool_id uuid references workforce.tools(tool_id),
  resource_ref text not null,
  scope jsonb not null default '{}'::jsonb,
  environment text not null check (environment in ('DEVELOPMENT', 'SHADOW', 'PAPER', 'PRODUCTION')),

  justification text not null,
  requested_by text not null,

  -- 만료 없는 권한 요청을 금지한다 (TEAM_YOUNGJU 10.1: effective_from/to 없는 활성 정책 금지).
  expires_at timestamptz not null,

  -- 승인 근거. governance.approvals 를 참조하며, 승인자는 요청자와 달라야 한다(Service 계층 강제).
  approval_id uuid references governance.approvals(approval_id),
  approvals jsonb not null default '[]'::jsonb,

  status text not null default 'REQUESTED'
    check (status in ('REQUESTED', 'APPROVED', 'REJECTED', 'PROVISIONED', 'CANCELLED', 'EXPIRED')),
  trace_id uuid not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  check (resource_kind <> 'TOOL' or tool_id is not null)
);

create table workforce.access_assignments (
  assignment_id uuid primary key default gen_random_uuid(),
  request_id uuid not null references workforce.access_requests(request_id),
  agent_id uuid not null references workforce.agent_profiles(agent_id),

  resource_kind text not null check (resource_kind in ('TOOL', 'DATA', 'ENVIRONMENT')),
  resource_ref text not null,
  scope jsonb not null default '{}'::jsonb,
  environment text not null check (environment in ('DEVELOPMENT', 'SHADOW', 'PAPER', 'PRODUCTION')),

  -- TOOL 부여는 agent_tool_permissions 행을 실체로 가리킨다. 권한 내용을 복제하지 않는다.
  tool_permission_id uuid references workforce.agent_tool_permissions(permission_id),

  -- Platform/IAM 이 발급한 외부 식별자. 인사팀이 만들지 않는다.
  provisioning_ref text not null,
  provisioned_by text not null,

  effective_from timestamptz not null,
  effective_to timestamptz not null,

  -- 회수 증거 (6.4 Leaver: Revocation Evidence 와 종료 시각 기록).
  revoked_at timestamptz,
  revocation_evidence jsonb,

  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'EXPIRED', 'REVOKED')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),

  check (effective_to > effective_from),
  check (status <> 'REVOKED' or (revoked_at is not null and revocation_evidence is not null)),
  check (resource_kind <> 'TOOL' or tool_permission_id is not null)
);

create index access_requests_agent_status_idx
  on workforce.access_requests (agent_id, status);
create index access_assignments_agent_status_idx
  on workforce.access_assignments (agent_id, status);

create trigger access_requests_touch_updated_at
before update on workforce.access_requests
for each row execute function governance.touch_updated_at();

create trigger access_assignments_touch_updated_at
before update on workforce.access_assignments
for each row execute function governance.touch_updated_at();

-- 내부 Table 은 Data API 에 직접 노출하지 않는다 (TEAM_YOUNGJU 7.2).
alter table workforce.access_requests enable row level security;
alter table workforce.access_assignments enable row level security;

commit;
