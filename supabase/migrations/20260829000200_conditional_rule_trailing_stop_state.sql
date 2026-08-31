begin;

-- A trailing exit has mutable state that cannot live in the immutable
-- confirmed AST or in process memory: a worker restart must retain the highest
-- fresh quote it already observed.  The row is keyed by the exact immutable
-- rule version and disappears with that version during retention.
create table execution.conditional_rule_trailing_states (
  rule_id uuid not null,
  rule_version integer not null check (rule_version > 0),
  high_price numeric(30,10) not null check (high_price > 0),
  armed_at timestamptz,
  last_observed_at timestamptz not null,
  updated_at timestamptz not null default now(),
  primary key (rule_id, rule_version),
  foreign key (rule_id, rule_version)
    references execution.conditional_trade_rule_versions(rule_id, rule_version)
    on delete cascade
);

create trigger conditional_rule_trailing_states_touch_updated_at
before update on execution.conditional_rule_trailing_states
for each row execute function governance.touch_updated_at();

alter table execution.conditional_rule_trailing_states enable row level security;

grant select, insert, update on execution.conditional_rule_trailing_states
  to svc_conditional_rule_worker;
grant select on execution.conditional_rule_trailing_states
  to svc_conditional_rule_orchestrator;

create policy conditional_rule_trailing_states_worker_all
  on execution.conditional_rule_trailing_states for all
  to svc_conditional_rule_worker using (true) with check (true);
create policy conditional_rule_trailing_states_orchestrator_select
  on execution.conditional_rule_trailing_states for select
  to svc_conditional_rule_orchestrator using (true);

comment on table execution.conditional_rule_trailing_states is
  'Worker-only mutable high-water state for confirmed PAPER trailing SELL exits; never an order authority.';

commit;
