begin;

-- A compound PAPER request is only a durable composition of the existing
-- authenticated immediate-order request and conditional-rule aggregates.  It
-- is not a second order ledger.  The conditional rule remains pending until
-- the immediate request is durably COMPLETED.
create table execution.user_paper_order_bundles (
  bundle_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references governance.user_profiles(user_id),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  client_request_id text not null check (length(client_request_id) between 8 and 128),
  raw_instruction text not null check (length(raw_instruction) between 1 and 2000),
  immediate_order_request_id uuid not null unique,
  conditional_rule_id uuid unique,
  required_quantity numeric(30,10) not null check (required_quantity > 0),
  state text not null default 'RECEIVED' check (state in (
    'RECEIVED',
    'WAITING_FOR_IMMEDIATE_FILL',
    'CONDITIONAL_ACTIVE',
    'FAILED',
    'COMPLETED'
  )),
  error_code text,
  error_message text,
  version bigint not null default 1 check (version > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  foreign key (book_id, fund_id)
    references accounting.books(book_id, fund_id),
  foreign key (immediate_order_request_id, user_id, fund_id, book_id)
    references execution.user_order_requests(
      order_request_id, user_id, fund_id, book_id
    ),
  foreign key (conditional_rule_id)
    references execution.conditional_trade_rules(rule_id),
  unique (user_id, client_request_id),
  unique (bundle_id, user_id, fund_id, book_id),
  check ((state in ('FAILED','COMPLETED') and completed_at is not null)
      or (state not in ('FAILED','COMPLETED') and completed_at is null))
);

create index user_paper_order_bundles_state_idx
  on execution.user_paper_order_bundles (state, updated_at, bundle_id);
create index user_paper_order_bundles_rule_idx
  on execution.user_paper_order_bundles (conditional_rule_id)
  where conditional_rule_id is not null;

create trigger user_paper_order_bundles_touch_updated_at
before update on execution.user_paper_order_bundles
for each row execute function governance.touch_updated_at();

alter table execution.user_paper_order_bundles enable row level security;

grant usage on schema execution to svc_order_orchestrator, svc_conditional_rule_worker;
grant select, insert on execution.user_paper_order_bundles to svc_order_orchestrator;
grant update (
  conditional_rule_id, state, error_code, error_message, version, completed_at
) on execution.user_paper_order_bundles to svc_order_orchestrator;
grant select, update (
  state, error_code, error_message, version, completed_at
) on execution.user_paper_order_bundles to svc_conditional_rule_worker;
grant select on execution.user_order_requests to svc_conditional_rule_worker;

create policy user_paper_order_bundles_orchestrator_all
  on execution.user_paper_order_bundles for all
  to svc_order_orchestrator using (true) with check (true);
create policy user_paper_order_bundles_worker_read_update
  on execution.user_paper_order_bundles for select
  to svc_conditional_rule_worker using (true);
create policy user_paper_order_bundles_worker_update
  on execution.user_paper_order_bundles for update
  to svc_conditional_rule_worker using (true) with check (true);

do $compound_paper_bundle_privilege_audit$
begin
  if not has_table_privilege(
       'svc_order_orchestrator',
       'execution.user_paper_order_bundles',
       'INSERT'
     )
     or not has_column_privilege(
       'svc_order_orchestrator',
       'execution.user_paper_order_bundles',
       'conditional_rule_id',
       'UPDATE'
     )
     or not has_table_privilege(
       'svc_conditional_rule_worker',
       'execution.user_paper_order_bundles',
       'SELECT'
     )
     or not has_table_privilege(
       'svc_conditional_rule_worker',
       'execution.user_order_requests',
       'SELECT'
     ) then
    raise exception 'compound PAPER bundle privilege boundary is incomplete';
  end if;
end
$compound_paper_bundle_privilege_audit$;

comment on table execution.user_paper_order_bundles is
  'Durable composition of one authenticated immediate PAPER order and one deferred conditional rule; no LIVE authority.';

commit;
