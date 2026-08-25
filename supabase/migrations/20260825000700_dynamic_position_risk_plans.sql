begin;

-- Risk-owned canonical presets.  The UI reads this projection; it does not
-- carry an independent numeric preset table.
create table if not exists risk.mandate_preset_versions (
  preset_version text primary key,
  status text not null check (status in ('DRAFT', 'ACTIVE', 'RETIRED')),
  presets jsonb not null,
  content_hash text not null unique,
  effective_from timestamptz not null,
  effective_to timestamptz,
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to > effective_from)
);

insert into risk.mandate_preset_versions (
  preset_version, status, presets, content_hash, effective_from
) values (
  'risk-mandate-presets.2026-08-25.v1',
  'ACTIVE',
  '[
    {"experience":"BEGINNER","mindset":"BALANCED","max_instrument_weight":"0.10","max_sector_weight":"0.25","max_gross_exposure":"1.00","max_concurrent_positions":5,"max_daily_loss_pct":"0.02","max_drawdown_pct":"0.15","trade_risk_budget_min_pct":"0.0025","trade_risk_budget_max_pct":"0.0050"},
    {"experience":"BEGINNER","mindset":"RISK_SEEKING","max_instrument_weight":"0.10","max_sector_weight":"0.25","max_gross_exposure":"1.00","max_concurrent_positions":5,"max_daily_loss_pct":"0.02","max_drawdown_pct":"0.15","trade_risk_budget_min_pct":"0.0025","trade_risk_budget_max_pct":"0.0050"},
    {"experience":"BEGINNER","mindset":"SAFETY_FIRST","max_instrument_weight":"0.10","max_sector_weight":"0.25","max_gross_exposure":"1.00","max_concurrent_positions":5,"max_daily_loss_pct":"0.02","max_drawdown_pct":"0.15","trade_risk_budget_min_pct":"0.0025","trade_risk_budget_max_pct":"0.0050"},
    {"experience":"EXPERIENCED","mindset":"BALANCED","max_instrument_weight":"0.15","max_sector_weight":"0.35","max_gross_exposure":"1.50","max_concurrent_positions":8,"max_daily_loss_pct":"0.03","max_drawdown_pct":"0.20","trade_risk_budget_min_pct":"0.0050","trade_risk_budget_max_pct":"0.0100"},
    {"experience":"EXPERIENCED","mindset":"RISK_SEEKING","max_instrument_weight":"0.25","max_sector_weight":"0.50","max_gross_exposure":"2.50","max_concurrent_positions":12,"max_daily_loss_pct":"0.05","max_drawdown_pct":"0.35","trade_risk_budget_min_pct":"0.0100","trade_risk_budget_max_pct":"0.0200"},
    {"experience":"EXPERIENCED","mindset":"SAFETY_FIRST","max_instrument_weight":"0.10","max_sector_weight":"0.25","max_gross_exposure":"1.00","max_concurrent_positions":5,"max_daily_loss_pct":"0.02","max_drawdown_pct":"0.15","trade_risk_budget_min_pct":"0.0025","trade_risk_budget_max_pct":"0.0050"},
    {"experience":"INTERMEDIATE","mindset":"BALANCED","max_instrument_weight":"0.15","max_sector_weight":"0.35","max_gross_exposure":"1.50","max_concurrent_positions":8,"max_daily_loss_pct":"0.03","max_drawdown_pct":"0.20","trade_risk_budget_min_pct":"0.0050","trade_risk_budget_max_pct":"0.0100"},
    {"experience":"INTERMEDIATE","mindset":"RISK_SEEKING","max_instrument_weight":"0.15","max_sector_weight":"0.35","max_gross_exposure":"1.50","max_concurrent_positions":8,"max_daily_loss_pct":"0.03","max_drawdown_pct":"0.20","trade_risk_budget_min_pct":"0.0050","trade_risk_budget_max_pct":"0.0100"},
    {"experience":"INTERMEDIATE","mindset":"SAFETY_FIRST","max_instrument_weight":"0.10","max_sector_weight":"0.25","max_gross_exposure":"1.00","max_concurrent_positions":5,"max_daily_loss_pct":"0.02","max_drawdown_pct":"0.15","trade_risk_budget_min_pct":"0.0025","trade_risk_budget_max_pct":"0.0050"}
  ]'::jsonb,
  'fea0244d39a13387d79cfc6876f63f690809b41e0e8c9c486b57d0adeb776b5d',
  '2026-08-25T00:00:00Z'::timestamptz
)
on conflict (preset_version) do nothing;

alter table risk.policies
  add column if not exists mandate_version_id uuid
    references governance.mandate_versions(mandate_version_id);

create unique index if not exists risk_policies_mandate_version_idx
  on risk.policies (mandate_version_id)
  where mandate_version_id is not null;

-- Historical UI metadata had no immutable version binding. Preserve it for
-- review, but label it so no runtime can treat it as reproducible authority.
update governance.mandates
set metadata = metadata || jsonb_build_object(
  'migration_status', 'REQUIRES_USER_REVIEW',
  'migration_reason', 'UNVERSIONED_MANDATE'
)
where current_version = 0 and metadata <> '{}'::jsonb;

create table if not exists risk.mandate_version_bindings (
  mandate_version_id uuid primary key
    references governance.mandate_versions(mandate_version_id),
  mandate_id uuid not null references governance.mandates(mandate_id),
  fund_id uuid not null references accounting.funds(fund_id),
  policy_id uuid not null unique references risk.policies(policy_id),
  mindset text not null,
  experience text not null,
  preset_version text not null references risk.mandate_preset_versions(preset_version),
  compiler_version text not null,
  input_hash text not null,
  content_hash text not null,
  trace_id text not null,
  activated_at timestamptz not null default now()
);

-- A plan row is immutable.  Market-driven recalculation inserts a new row and
-- the lifecycle event marks the old row SUPERSEDED.
create table if not exists risk.position_risk_plans (
  risk_plan_id uuid primary key,
  fund_id uuid not null references accounting.funds(fund_id),
  instrument_id text not null,
  mandate_version_id uuid not null references governance.mandate_versions(mandate_version_id),
  portfolio_snapshot_id text not null,
  market_snapshot_id text not null,
  as_of timestamptz not null,
  expires_at timestamptz not null,
  regime text check (regime is null or regime in ('NORMAL', 'CAUTION', 'DOWNTREND', 'STRESS')),
  action text not null check (action in ('PROPOSE', 'DEFER', 'REDUCE_ONLY')),
  state text not null default 'PROPOSED' check (state in (
    'PROPOSED', 'VALIDATED', 'USER_APPROVED', 'AUTO_POLICY_APPROVED',
    'ACTIVE', 'SUPERSEDED', 'EXPIRED', 'TRIGGERED'
  )),
  entry_reference numeric(38, 12),
  stop_price numeric(38, 12),
  take_profit_price numeric(38, 12),
  trailing_activation_price numeric(38, 12),
  trailing_distance numeric(38, 12),
  position_risk_amount numeric(38, 12),
  quantity_cap numeric(38, 12),
  current_quantity numeric(38, 12) not null default 0 check (current_quantity >= 0),
  reward_risk_ratio numeric(38, 12),
  liquidation_stages jsonb not null default '[]'::jsonb,
  calculation_version text not null,
  input_hash text not null,
  data_quality text not null check (data_quality in (
    'VALID', 'STALE', 'NON_AUTHORITATIVE', 'MISSING'
  )),
  reason_codes jsonb not null default '[]'::jsonb,
  review_triggers jsonb not null default '[]'::jsonb,
  execution_mode text not null check (execution_mode = 'PAPER'),
  trace_id text not null,
  task_id text not null,
  created_at timestamptz not null default now(),
  unique (fund_id, instrument_id, input_hash, calculation_version),
  check (expires_at >= as_of),
  check (
    action <> 'PROPOSE'
    or (regime is not null and entry_reference is not null
        and stop_price is not null and take_profit_price is not null
        and position_risk_amount is not null and quantity_cap is not null)
  ),
  check (
    action <> 'PROPOSE'
    or quantity_cap * abs(entry_reference - stop_price) <= position_risk_amount
  )
);

create index if not exists position_risk_plans_fund_state_idx
  on risk.position_risk_plans (fund_id, state, as_of desc);
create index if not exists position_risk_plans_instrument_idx
  on risk.position_risk_plans (fund_id, instrument_id, as_of desc);

create table if not exists risk.position_risk_plan_events (
  event_id uuid primary key default gen_random_uuid(),
  risk_plan_id uuid not null references risk.position_risk_plans(risk_plan_id),
  from_state text,
  to_state text not null,
  actor_type text not null check (actor_type in ('RISK', 'USER', 'AUTO_POLICY', 'TRADING', 'SYSTEM')),
  actor_id text not null,
  reason text not null,
  trace_id text not null,
  task_id text not null,
  idempotency_key text not null unique,
  occurred_at timestamptz not null default now()
);

create index if not exists position_risk_plan_events_plan_time_idx
  on risk.position_risk_plan_events (risk_plan_id, occurred_at);

-- Delivery/read-back is recorded per target so a successful Notion HTTP call
-- is distinguishable from an actually readable projection.
create table if not exists risk.position_risk_plan_projections (
  projection_id uuid primary key default gen_random_uuid(),
  risk_plan_id uuid not null references risk.position_risk_plans(risk_plan_id),
  target text not null check (target in ('DISCORD', 'NOTION')),
  projection_version text not null,
  payload_hash text not null,
  external_id text,
  delivery_status text not null check (delivery_status in ('PENDING', 'DELIVERED', 'FAILED')),
  readback_status text not null default 'NOT_CHECKED'
    check (readback_status in ('NOT_CHECKED', 'VERIFIED', 'FAILED')),
  error_code text,
  task_id text not null,
  trace_id text not null,
  created_at timestamptz not null default now(),
  delivered_at timestamptz,
  readback_at timestamptz,
  unique (risk_plan_id, target, projection_version)
);

alter table risk.run_log_events
  add column if not exists task_id text,
  add column if not exists risk_plan_id uuid,
  add column if not exists mandate_version_id uuid,
  add column if not exists algorithm_version text,
  add column if not exists status text;

create index if not exists risk_run_log_events_task_idx
  on risk.run_log_events (task_id) where task_id is not null;
create index if not exists risk_run_log_events_plan_idx
  on risk.run_log_events (risk_plan_id) where risk_plan_id is not null;

alter table risk.mandate_preset_versions enable row level security;
alter table risk.mandate_version_bindings enable row level security;
alter table risk.position_risk_plans enable row level security;
alter table risk.position_risk_plan_events enable row level security;
alter table risk.position_risk_plan_projections enable row level security;

drop policy if exists mandate_preset_versions_authenticated_read
  on risk.mandate_preset_versions;
create policy mandate_preset_versions_authenticated_read
  on risk.mandate_preset_versions for select to authenticated
  using (status = 'ACTIVE');

drop policy if exists position_risk_plans_fund_member_read
  on risk.position_risk_plans;
create policy position_risk_plans_fund_member_read
  on risk.position_risk_plans for select to authenticated
  using (governance.can_access_fund(fund_id));

commit;
