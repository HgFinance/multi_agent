begin;

-- Authenticated standing PAPER rules are a separate authority from both
-- immediate USER_DIRECTIVE requests and automated strategy OrderIntents.
do $conditional_rule_roles$
declare
  pool_login name := session_user;
  role_name name;
begin
  foreach role_name in array array[
    'svc_conditional_rule_orchestrator'::name,
    'svc_conditional_rule_worker'::name
  ] loop
    if not exists (select 1 from pg_roles where rolname = role_name) then
      execute format(
        'create role %I nologin nosuperuser nocreatedb nocreaterole '
        'noinherit noreplication nobypassrls', role_name
      );
    elsif exists (
      select 1 from pg_roles
       where rolname = role_name
         and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
              or rolinherit or rolreplication or rolbypassrls)
    ) then
      raise exception 'conditional rule role % is unsafe', role_name;
    end if;
    execute format(
      'grant %I to %I with set true, inherit false', role_name, pool_login
    );
  end loop;
end
$conditional_rule_roles$;

revoke all on schema governance, accounting, reference, execution
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all tables in schema governance
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all tables in schema accounting
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all tables in schema reference
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all tables in schema execution
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all sequences in schema governance
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all sequences in schema accounting
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all sequences in schema reference
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
revoke all privileges on all sequences in schema execution
  from svc_conditional_rule_orchestrator, svc_conditional_rule_worker;

create table execution.conditional_trade_rules (
  rule_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references governance.user_profiles(user_id),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  instrument_id uuid not null references reference.instruments(instrument_id),
  symbol text not null check (symbol ~ '^[0-9A-Z]{6}$'),
  client_request_id text not null check (length(client_request_id) between 8 and 128),
  state text not null default 'DRAFT' check (
    state in (
      'DRAFT','NEEDS_CLARIFICATION','VALIDATED','PENDING_CONFIRMATION',
      'ACTIVE','TRIGGERED','EXECUTION_PENDING','COMPLETED','PAUSED',
      'EXPIRED','CANCELLED','FAILED'
    )
  ),
  current_version integer not null default 1 check (current_version > 0),
  execution_mode text not null default 'PAPER' check (execution_mode = 'PAPER'),
  repeat_policy text not null default 'ONCE' check (repeat_policy = 'ONCE'),
  evaluation_clock text not null check (evaluation_clock in ('BAR_CLOSE','QUOTE')),
  primary_timeframe text check (
    primary_timeframe is null or primary_timeframe in ('1M','5M','15M','1H','1D')
  ),
  market_closed_policy text not null default 'REJECT_TRIGGER'
    check (market_closed_policy = 'REJECT_TRIGGER'),
  expires_at timestamptz not null,
  confirmation_sha256 text check (
    confirmation_sha256 is null or confirmation_sha256 ~ '^[0-9a-f]{64}$'
  ),
  confirmed_at timestamptz,
  version bigint not null default 1 check (version > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique (user_id, client_request_id),
  unique (rule_id, user_id, fund_id, book_id),
  check (
    (evaluation_clock='BAR_CLOSE' and primary_timeframe is not null)
    or (evaluation_clock='QUOTE' and primary_timeframe is null)
  ),
  check (
    state not in ('ACTIVE','TRIGGERED','EXECUTION_PENDING','COMPLETED')
    or (confirmation_sha256 is not null and confirmed_at is not null)
  ),
  check (confirmed_at is null or confirmed_at >= created_at),
  check (completed_at is null or state in ('COMPLETED','EXPIRED','CANCELLED','FAILED'))
);

create table execution.conditional_trade_rule_versions (
  rule_id uuid not null references execution.conditional_trade_rules(rule_id),
  rule_version integer not null check (rule_version > 0),
  schema_version text not null check (schema_version='conditional-trade-rule.v1'),
  spec jsonb not null check (jsonb_typeof(spec)='object'),
  spec_sha256 text not null check (spec_sha256 ~ '^[0-9a-f]{64}$'),
  raw_instruction text not null check (length(raw_instruction) between 1 and 4000),
  raw_instruction_sha256 text not null check (raw_instruction_sha256 ~ '^[0-9a-f]{64}$'),
  parser_source text not null check (parser_source in ('HERMES','DETERMINISTIC')),
  created_at timestamptz not null default now(),
  primary key (rule_id, rule_version),
  unique (rule_id, spec_sha256)
);

create table execution.conditional_trade_rule_events (
  event_id text primary key check (length(event_id) between 8 and 160),
  rule_id uuid not null references execution.conditional_trade_rules(rule_id),
  rule_version integer not null,
  event_type text not null check (length(event_type) between 1 and 64),
  from_state text,
  to_state text not null,
  payload jsonb not null default '{}'::jsonb check (jsonb_typeof(payload)='object'),
  created_at timestamptz not null default now(),
  foreign key (rule_id, rule_version)
    references execution.conditional_trade_rule_versions(rule_id, rule_version)
);

create table execution.conditional_rule_evaluations (
  evaluation_id text primary key check (length(evaluation_id) between 16 and 160),
  rule_id uuid not null references execution.conditional_trade_rules(rule_id),
  rule_version integer not null,
  evaluation_key text not null check (length(evaluation_key) between 8 and 256),
  evaluation_clock text not null check (evaluation_clock in ('BAR_CLOSE','QUOTE')),
  condition_result boolean,
  outcome text not null check (outcome in ('FALSE','TRUE','ERROR')),
  context_sha256 text not null check (context_sha256 ~ '^[0-9a-f]{64}$'),
  data_watermark timestamptz not null,
  error_code text,
  error_message text,
  created_at timestamptz not null default now(),
  foreign key (rule_id, rule_version)
    references execution.conditional_trade_rule_versions(rule_id, rule_version),
  unique (rule_id, rule_version, evaluation_key),
  check (
    (outcome='TRUE' and condition_result is true and error_code is null)
    or (outcome='FALSE' and condition_result is false and error_code is null)
    or (outcome='ERROR' and condition_result is null and error_code is not null)
  )
);

create table execution.conditional_rule_triggers (
  trigger_id text primary key check (length(trigger_id) between 16 and 160),
  rule_id uuid not null references execution.conditional_trade_rules(rule_id),
  rule_version integer not null,
  evaluation_id text not null unique
    references execution.conditional_rule_evaluations(evaluation_id),
  condition_sha256 text not null check (condition_sha256 ~ '^[0-9a-f]{64}$'),
  state text not null default 'CLAIMED' check (
    state in (
      'CLAIMED','GUARD_REJECTED','EXECUTION_PENDING','SUBMITTED',
      'COMPLETED','FAILED'
    )
  ),
  guard_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  foreign key (rule_id, rule_version)
    references execution.conditional_trade_rule_versions(rule_id, rule_version),
  unique (rule_id, rule_version, evaluation_id, condition_sha256)
);

create table execution.conditional_rule_executions (
  rule_execution_id uuid primary key default gen_random_uuid(),
  trigger_id text not null unique references execution.conditional_rule_triggers(trigger_id),
  rule_id uuid not null references execution.conditional_trade_rules(rule_id),
  rule_version integer not null,
  state text not null default 'PENDING' check (
    state in ('PENDING','GUARD_REJECTED','SUBMITTING','SUBMITTED','COMPLETED','FAILED')
  ),
  side text not null check (side in ('BUY','SELL')),
  quantity numeric(30,10) check (quantity is null or quantity > 0),
  directive_id uuid unique references execution.user_directives(directive_id),
  idempotency_key text not null unique check (
    length(idempotency_key) between 8 and 128
    and idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  guard_code text,
  error_code text,
  error_message text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  foreign key (rule_id, rule_version)
    references execution.conditional_trade_rule_versions(rule_id, rule_version),
  check (state not in ('SUBMITTED','COMPLETED') or directive_id is not null)
);

create table execution.conditional_rule_outbox (
  event_id text primary key check (length(event_id) between 16 and 160),
  aggregate_type text not null default 'CONDITIONAL_RULE'
    check (aggregate_type='CONDITIONAL_RULE'),
  aggregate_id text not null,
  event_type text not null check (length(event_type) between 1 and 64),
  payload jsonb not null check (jsonb_typeof(payload)='object'),
  created_at timestamptz not null default now(),
  published_at timestamptz,
  attempts integer not null default 0 check (attempts >= 0),
  last_error text
);

create index conditional_trade_rules_active_idx
  on execution.conditional_trade_rules (state, evaluation_clock, primary_timeframe, expires_at)
  where state='ACTIVE';
create index conditional_rule_evaluations_rule_idx
  on execution.conditional_rule_evaluations (rule_id, rule_version, data_watermark desc);
create index conditional_rule_triggers_state_idx
  on execution.conditional_rule_triggers (state, created_at);
create index conditional_rule_outbox_pending_idx
  on execution.conditional_rule_outbox (created_at, event_id)
  where published_at is null;

create trigger conditional_trade_rules_touch_updated_at
before update on execution.conditional_trade_rules
for each row execute function governance.touch_updated_at();
create trigger conditional_rule_triggers_touch_updated_at
before update on execution.conditional_rule_triggers
for each row execute function governance.touch_updated_at();
create trigger conditional_rule_executions_touch_updated_at
before update on execution.conditional_rule_executions
for each row execute function governance.touch_updated_at();

create or replace function execution.guard_conditional_rule_state_transition()
returns trigger
language plpgsql
set search_path = pg_catalog, execution
as $$
begin
  if new.state = old.state then
    return new;
  end if;
  if not (
    (old.state='DRAFT' and new.state in ('NEEDS_CLARIFICATION','VALIDATED','CANCELLED'))
    or (old.state='NEEDS_CLARIFICATION' and new.state in ('DRAFT','CANCELLED'))
    or (old.state='VALIDATED' and new.state in ('PENDING_CONFIRMATION','CANCELLED'))
    or (old.state='PENDING_CONFIRMATION' and new.state in ('ACTIVE','EXPIRED','CANCELLED'))
    or (old.state='ACTIVE' and new.state in ('PAUSED','TRIGGERED','EXPIRED','CANCELLED','FAILED'))
    or (old.state='PAUSED' and new.state in ('ACTIVE','EXPIRED','CANCELLED'))
    or (old.state='TRIGGERED' and new.state in ('EXECUTION_PENDING','FAILED'))
    or (old.state='EXECUTION_PENDING' and new.state in ('COMPLETED','FAILED'))
  ) then
    raise exception 'invalid conditional rule transition % -> %', old.state, new.state;
  end if;
  return new;
end
$$;

revoke all on function execution.guard_conditional_rule_state_transition()
  from public, anon, authenticated, service_role,
       svc_conditional_rule_orchestrator, svc_conditional_rule_worker,
       svc_trading_api;

create trigger conditional_trade_rules_state_guard
before update of state on execution.conditional_trade_rules
for each row execute function execution.guard_conditional_rule_state_transition();

alter table execution.conditional_trade_rules enable row level security;
alter table execution.conditional_trade_rule_versions enable row level security;
alter table execution.conditional_trade_rule_events enable row level security;
alter table execution.conditional_rule_evaluations enable row level security;
alter table execution.conditional_rule_triggers enable row level security;
alter table execution.conditional_rule_executions enable row level security;
alter table execution.conditional_rule_outbox enable row level security;

grant usage on schema execution
  to svc_conditional_rule_orchestrator, svc_conditional_rule_worker;
grant select, insert on execution.conditional_trade_rules,
  execution.conditional_trade_rule_versions,
  execution.conditional_trade_rule_events
  to svc_conditional_rule_orchestrator;
grant update (
  state,current_version,confirmation_sha256,confirmed_at,version,completed_at
) on execution.conditional_trade_rules to svc_conditional_rule_orchestrator;
grant select on execution.conditional_rule_evaluations,
  execution.conditional_rule_triggers, execution.conditional_rule_executions
  to svc_conditional_rule_orchestrator;

grant select on execution.conditional_trade_rules,
  execution.conditional_trade_rule_versions
  to svc_conditional_rule_worker, svc_trading_api;
grant select, insert on execution.conditional_trade_rule_events,
  execution.conditional_rule_evaluations, execution.conditional_rule_triggers,
  execution.conditional_rule_executions, execution.conditional_rule_outbox
  to svc_conditional_rule_worker;
grant update (state,version,completed_at)
  on execution.conditional_trade_rules to svc_conditional_rule_worker;
grant update (state,guard_code)
  on execution.conditional_rule_triggers to svc_conditional_rule_worker;
grant update (
  state,quantity,directive_id,guard_code,error_code,error_message,completed_at
) on execution.conditional_rule_executions to svc_conditional_rule_worker;
grant update (published_at,attempts,last_error)
  on execution.conditional_rule_outbox to svc_conditional_rule_worker;
grant select on execution.conditional_rule_triggers,
  execution.conditional_rule_executions to svc_trading_api;

create policy conditional_rules_orchestrator_all
  on execution.conditional_trade_rules for all
  to svc_conditional_rule_orchestrator using (true) with check (true);
create policy conditional_rule_versions_orchestrator_all
  on execution.conditional_trade_rule_versions for all
  to svc_conditional_rule_orchestrator using (true) with check (true);
create policy conditional_rule_events_orchestrator_all
  on execution.conditional_trade_rule_events for all
  to svc_conditional_rule_orchestrator using (true) with check (true);
create policy conditional_rule_evaluations_orchestrator_select
  on execution.conditional_rule_evaluations for select
  to svc_conditional_rule_orchestrator using (true);
create policy conditional_rule_triggers_orchestrator_select
  on execution.conditional_rule_triggers for select
  to svc_conditional_rule_orchestrator using (true);
create policy conditional_rule_executions_orchestrator_select
  on execution.conditional_rule_executions for select
  to svc_conditional_rule_orchestrator using (true);

create policy conditional_rules_worker_select
  on execution.conditional_trade_rules for select
  to svc_conditional_rule_worker using (true);
create policy conditional_rules_worker_update
  on execution.conditional_trade_rules for update
  to svc_conditional_rule_worker using (true) with check (true);
create policy conditional_rule_versions_worker_select
  on execution.conditional_trade_rule_versions for select
  to svc_conditional_rule_worker using (true);
create policy conditional_rule_events_worker_all
  on execution.conditional_trade_rule_events for all
  to svc_conditional_rule_worker using (true) with check (true);
create policy conditional_rule_evaluations_worker_all
  on execution.conditional_rule_evaluations for all
  to svc_conditional_rule_worker using (true) with check (true);
create policy conditional_rule_triggers_worker_all
  on execution.conditional_rule_triggers for all
  to svc_conditional_rule_worker using (true) with check (true);
create policy conditional_rule_executions_worker_all
  on execution.conditional_rule_executions for all
  to svc_conditional_rule_worker using (true) with check (true);
create policy conditional_rule_outbox_worker_all
  on execution.conditional_rule_outbox for all
  to svc_conditional_rule_worker using (true) with check (true);

create policy conditional_rules_trading_select
  on execution.conditional_trade_rules for select
  to svc_trading_api using (execution_mode='PAPER');
create policy conditional_rule_versions_trading_select
  on execution.conditional_trade_rule_versions for select
  to svc_trading_api using (true);
create policy conditional_rule_triggers_trading_select
  on execution.conditional_rule_triggers for select
  to svc_trading_api using (true);
create policy conditional_rule_executions_trading_select
  on execution.conditional_rule_executions for select
  to svc_trading_api using (true);

do $conditional_rule_privilege_audit$
begin
  if has_table_privilege(
       'svc_conditional_rule_worker','execution.user_directives','INSERT')
     or has_table_privilege(
       'svc_conditional_rule_worker','accounting.positions','SELECT')
     or has_table_privilege(
       'svc_conditional_rule_orchestrator','execution.user_directives','INSERT')
     or has_table_privilege(
       'svc_conditional_rule_orchestrator','governance.user_profiles','SELECT') then
    raise exception 'conditional rule roles exceed their authority boundary';
  end if;
  if not has_table_privilege(
       'svc_conditional_rule_worker','execution.conditional_rule_evaluations','INSERT')
     or not has_table_privilege(
       'svc_conditional_rule_orchestrator','execution.conditional_trade_rules','INSERT')
     or not has_table_privilege(
       'svc_trading_api','execution.conditional_rule_executions','SELECT') then
    raise exception 'conditional rule roles lack required privileges';
  end if;
end
$conditional_rule_privilege_audit$;

comment on table execution.conditional_trade_rules is
  'Authenticated, explicitly confirmed, PAPER-only standing rules; never strategy alpha authority.';
comment on table execution.conditional_rule_evaluations is
  'Append-only deterministic condition evaluations keyed by completed bar or quote clock.';
comment on table execution.conditional_rule_triggers is
  'Exactly-once trigger claims; unique evaluation lineage prevents duplicate orders.';

commit;
