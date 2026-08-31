begin;

-- Bounded arm -> trigger/cancel rules must survive worker restarts.  The
-- immutable AST owns the three predicates and window size; this table owns
-- only the mutable progress for the exact confirmed rule version.
create table execution.conditional_rule_temporal_states (
  rule_id uuid not null,
  rule_version integer not null check (rule_version > 0),
  armed_at timestamptz not null,
  remaining_bars integer not null check (remaining_bars between 1 and 500),
  last_observed_at timestamptz not null,
  updated_at timestamptz not null default now(),
  primary key (rule_id, rule_version),
  foreign key (rule_id, rule_version)
    references execution.conditional_trade_rule_versions(rule_id, rule_version)
    on delete cascade
);

create trigger conditional_rule_temporal_states_touch_updated_at
before update on execution.conditional_rule_temporal_states
for each row execute function governance.touch_updated_at();

alter table execution.conditional_rule_temporal_states enable row level security;

grant select, insert, update, delete
  on execution.conditional_rule_temporal_states to svc_conditional_rule_worker;
grant select on execution.conditional_rule_temporal_states
  to svc_conditional_rule_orchestrator;

create policy conditional_rule_temporal_states_worker_all
  on execution.conditional_rule_temporal_states for all
  to svc_conditional_rule_worker using (true) with check (true);
create policy conditional_rule_temporal_states_orchestrator_select
  on execution.conditional_rule_temporal_states for select
  to svc_conditional_rule_orchestrator using (true);

comment on table execution.conditional_rule_temporal_states is
  'Worker-only progress for bounded confirmed PAPER temporal sequences; never an order authority.';

commit;
