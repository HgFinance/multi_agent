begin;

-- Authenticated USER PAPER commands deliberately live outside strategy-owned
-- order_intents. They must never fabricate Alpha, TradeCase, or RiskDecision
-- evidence merely to satisfy strategy foreign keys.

do $paper_runtime_roles$
declare
  pool_login name := session_user;
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_trading_api') then
    create role svc_trading_api
      nologin nosuperuser nocreatedb nocreaterole noinherit
      noreplication nobypassrls;
  elsif exists (
    select 1 from pg_roles
     where rolname = 'svc_trading_api'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_trading_api role name is occupied by an unsafe role';
  end if;

  if not exists (select 1 from pg_roles where rolname = 'svc_accounting_ledger') then
    create role svc_accounting_ledger
      nologin nosuperuser nocreatedb nocreaterole noinherit
      noreplication nobypassrls;
  elsif exists (
    select 1 from pg_roles
     where rolname = 'svc_accounting_ledger'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_accounting_ledger role name is occupied by an unsafe role';
  end if;

  execute format(
    'grant svc_trading_api to %I with set true, inherit false', pool_login
  );
  execute format(
    'grant svc_accounting_ledger to %I with set true, inherit false', pool_login
  );
end
$paper_runtime_roles$;

revoke all on schema governance, accounting, reference, execution
  from svc_trading_api;
revoke all privileges on all tables in schema governance from svc_trading_api;
revoke all privileges on all tables in schema accounting from svc_trading_api;
revoke all privileges on all tables in schema reference from svc_trading_api;
revoke all privileges on all tables in schema execution from svc_trading_api;
revoke all privileges on all sequences in schema governance from svc_trading_api;
revoke all privileges on all sequences in schema accounting from svc_trading_api;
revoke all privileges on all sequences in schema reference from svc_trading_api;
revoke all privileges on all sequences in schema execution from svc_trading_api;

revoke all on schema accounting, execution, reference
  from svc_accounting_ledger;
revoke all privileges on all tables in schema accounting
  from svc_accounting_ledger;
revoke all privileges on all tables in schema execution
  from svc_accounting_ledger;
revoke all privileges on all tables in schema reference
  from svc_accounting_ledger;
revoke all privileges on all sequences in schema accounting
  from svc_accounting_ledger;
revoke all privileges on all sequences in schema execution
  from svc_accounting_ledger;
revoke all privileges on all sequences in schema reference
  from svc_accounting_ledger;

create table execution.user_directives (
  directive_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references governance.user_profiles(user_id),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null,
  action text not null check (action in ('PLACE_ORDER', 'CANCEL_ALL', 'SELL_ALL')),
  instruction_ref text not null check (length(instruction_ref) between 8 and 128),
  idempotency_key text not null check (
    length(idempotency_key) between 8 and 128
    and idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  payload jsonb not null default '{}'::jsonb check (jsonb_typeof(payload) = 'object'),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  proof_issuer text not null,
  proof_audience text not null,
  proof_issued_at timestamptz not null,
  proof_not_before timestamptz not null,
  proof_expires_at timestamptz not null,
  priority integer not null check (priority in (1000, 2000)),
  state text not null default 'RECEIVED'
    check (state in ('RECEIVED', 'RUNNING', 'IN_PROGRESS', 'PARTIAL',
                     'COMPLETED', 'FAILED', 'UNKNOWN')),
  error_code text,
  error_message text,
  version bigint not null default 1 check (version > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  unique (user_id, fund_id, book_id, idempotency_key),
  unique (directive_id, fund_id, book_id),
  check (proof_not_before >= proof_issued_at - interval '5 seconds'),
  check (proof_expires_at > proof_issued_at),
  check (proof_expires_at <= proof_issued_at + interval '5 minutes'),
  check (
    (action = 'PLACE_ORDER'
      and payload ?& array['symbol','instrument_id','side','quantity',
                            'order_type','limit_price','time_in_force']
      and (payload - array['symbol','instrument_id','side','quantity',
                            'order_type','limit_price','time_in_force']) = '{}'::jsonb
      and payload->>'symbol' ~ '^[0-9]{6}$'
      and payload->>'side' in ('BUY','SELL')
      and payload->>'order_type' in ('MARKET','LIMIT')
      and payload->>'time_in_force' = 'DAY'
      and jsonb_typeof(payload->'quantity') = 'string'
      and payload->>'quantity' ~ '^[0-9]+(\.[0-9]+)?$'
      and jsonb_typeof(payload->'instrument_id') in ('null','string')
      and (
        (payload->>'order_type'='MARKET'
          and jsonb_typeof(payload->'limit_price')='null')
        or
        (payload->>'order_type'='LIMIT'
          and jsonb_typeof(payload->'limit_price')='string'
          and payload->>'limit_price' ~ '^[0-9]+(\.[0-9]+)?$')
      ))
    or (action in ('CANCEL_ALL', 'SELL_ALL') and payload = '{}'::jsonb)
  ),
  check ((action = 'PLACE_ORDER' and priority = 1000)
      or (action in ('CANCEL_ALL', 'SELL_ALL') and priority = 2000)),
  check ((state = 'COMPLETED' and completed_at is not null)
      or state <> 'COMPLETED')
);

create index user_directives_scope_queue_idx
  on execution.user_directives (fund_id, book_id, priority desc, created_at)
  where state in ('RECEIVED', 'RUNNING', 'IN_PROGRESS', 'UNKNOWN');

create table execution.user_directive_proofs (
  proof_jti text primary key check (length(proof_jti) between 8 and 256),
  directive_id uuid not null references execution.user_directives(directive_id) on delete cascade,
  user_id uuid not null references governance.user_profiles(user_id),
  fund_id uuid not null,
  book_id uuid not null,
  action text not null check (action in ('PLACE_ORDER', 'CANCEL_ALL', 'SELL_ALL')),
  instruction_ref text not null,
  idempotency_key text not null check (
    length(idempotency_key) between 8 and 128
    and idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]*$'
  ),
  payload_sha256 text not null check (payload_sha256 ~ '^[0-9a-f]{64}$'),
  issued_at timestamptz not null,
  expires_at timestamptz not null,
  consumed_at timestamptz not null default now(),
  foreign key (directive_id, fund_id, book_id)
    references execution.user_directives(directive_id, fund_id, book_id),
  check (expires_at > issued_at)
);

create index user_directive_proofs_directive_idx
  on execution.user_directive_proofs (directive_id, consumed_at);

create table execution.user_directive_legs (
  leg_id uuid primary key default gen_random_uuid(),
  directive_id uuid not null references execution.user_directives(directive_id) on delete cascade,
  leg_index integer not null check (leg_index >= 0),
  instrument_id uuid references reference.instruments(instrument_id),
  symbol text check (symbol is null or symbol ~ '^[0-9]{6}$'),
  side text check (side is null or side in ('BUY', 'SELL')),
  order_type text check (order_type is null or order_type in ('MARKET', 'LIMIT')),
  time_in_force text check (time_in_force is null or time_in_force = 'DAY'),
  requested_quantity numeric(30, 10),
  limit_price numeric(30, 10),
  filled_quantity numeric(30, 10) not null default 0 check (filled_quantity >= 0),
  reduce_only boolean not null default false,
  state text not null default 'PENDING'
    check (state in ('PENDING', 'ACKNOWLEDGED', 'PARTIALLY_FILLED', 'FILLED',
                     'CANCELLED', 'REJECTED', 'EXPIRED', 'UNKNOWN', 'SKIPPED')),
  linked_order_id uuid references execution.orders(order_id),
  client_order_id text unique,
  broker_order_id text unique,
  broker_event_id text unique,
  error_code text,
  error_message text,
  target_filled_quantity numeric(30, 10) not null default 0
    check (target_filled_quantity >= 0),
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (directive_id, leg_index),
  unique (leg_id, directive_id),
  check (
    (side is null and order_type is null and time_in_force is null
      and requested_quantity is null and limit_price is null
      and filled_quantity = 0 and expires_at is null)
    or
    (side is not null and order_type is not null and time_in_force = 'DAY'
      and requested_quantity > 0 and filled_quantity <= requested_quantity
      and expires_at > created_at
      and ((order_type = 'LIMIT' and limit_price > 0)
        or (order_type = 'MARKET' and limit_price is null)))
  ),
  check (not reduce_only or side = 'SELL')
);

create index user_directive_legs_active_idx
  on execution.user_directive_legs (directive_id, state, leg_index)
  where state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN');
create index user_directive_legs_expiry_idx
  on execution.user_directive_legs (expires_at, directive_id)
  where state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED');

create table execution.paper_order_reservations (
  reservation_id uuid primary key default gen_random_uuid(),
  directive_id uuid not null references execution.user_directives(directive_id) on delete cascade,
  leg_id uuid not null unique references execution.user_directive_legs(leg_id) on delete cascade,
  fund_id uuid not null,
  book_id uuid not null,
  instrument_id uuid references reference.instruments(instrument_id),
  reservation_type text not null check (reservation_type in ('POSITION', 'CASH')),
  reserved_quantity numeric(30, 10),
  reserved_cash numeric(38, 10),
  currency text check (currency is null or currency ~ '^[A-Z]{3}$'),
  state text not null default 'ACTIVE' check (state in ('ACTIVE', 'RELEASED')),
  version bigint not null default 1 check (version > 0),
  created_at timestamptz not null default now(),
  released_at timestamptz,
  foreign key (directive_id, fund_id, book_id)
    references execution.user_directives(directive_id, fund_id, book_id),
  check (
    (reservation_type = 'POSITION' and instrument_id is not null
      and reserved_quantity > 0 and reserved_cash is null and currency is null)
    or
    (reservation_type = 'CASH' and instrument_id is null
      and reserved_quantity is null and reserved_cash > 0 and currency is not null)
  ),
  check ((state = 'RELEASED' and released_at is not null) or state = 'ACTIVE')
);

create index paper_order_reservations_position_idx
  on execution.paper_order_reservations (fund_id, book_id, instrument_id)
  where state = 'ACTIVE' and reservation_type = 'POSITION';
create index paper_order_reservations_cash_idx
  on execution.paper_order_reservations (fund_id, book_id, currency)
  where state = 'ACTIVE' and reservation_type = 'CASH';

-- Direct authenticated-user fills are deliberately separate from
-- execution.fills, whose required order_id is backed by strategy/Risk
-- evidence.  A USER command must never fabricate that evidence merely to be
-- account-able.  The canonical trading.fill.v1 envelope points here instead.
create table execution.paper_user_directive_fills (
  fill_id uuid primary key,
  leg_id uuid not null,
  directive_id uuid not null,
  quote_event_key text not null check (quote_event_key ~ '^[0-9a-f]{64}$'),
  broker_fill_id text not null unique,
  instrument_id uuid not null references reference.instruments(instrument_id),
  side text not null check (side in ('BUY','SELL')),
  quantity numeric(30, 10) not null check (quantity > 0),
  price numeric(30, 10) not null check (price > 0),
  gross_amount numeric(38, 10) not null check (gross_amount = quantity * price),
  fee_amount numeric(38, 10) not null default 0 check (fee_amount >= 0),
  tax_amount numeric(38, 10) not null default 0 check (tax_amount >= 0),
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  event_time timestamptz not null,
  received_at timestamptz not null,
  quote_source text not null check (length(quote_source) between 1 and 256),
  trace_id uuid not null,
  accounting_acknowledged_at timestamptz,
  created_at timestamptz not null default now(),
  foreign key (leg_id, directive_id)
    references execution.user_directive_legs(leg_id, directive_id),
  unique (leg_id, quote_event_key)
);

create index paper_user_directive_fills_accounting_idx
  on execution.paper_user_directive_fills (directive_id, created_at, fill_id)
  where accounting_acknowledged_at is null;

create unique index outbox_direct_user_fill_artifact_uidx
  on execution.outbox ((payload_ref->>'artifact_id'))
  where event_type='trading.fill.v1'
    and payload_ref->>'artifact_schema'='trading-user-directive-fill-v1';

create table execution.paper_directive_barriers (
  fund_id uuid not null,
  book_id uuid not null,
  active_directive_id uuid not null,
  priority integer not null check (priority in (1000, 2000)),
  mode text not null check (mode in ('USER_PRIORITY', 'REDUCE_ONLY')),
  activated_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  primary key (fund_id, book_id),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  foreign key (active_directive_id, fund_id, book_id)
    references execution.user_directives(directive_id, fund_id, book_id)
);

create or replace function execution.validate_user_directive_scope()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, governance, accounting
as $$
begin
  if not exists (
    select 1
      from governance.user_profiles profile
      join governance.fund_memberships membership
        on membership.user_id=profile.user_id
       and membership.fund_id=new.fund_id
      join accounting.funds fund
        on fund.fund_id=new.fund_id and fund.status='ACTIVE'
      join accounting.books book
        on book.book_id=new.book_id and book.fund_id=new.fund_id
       and book.status='ACTIVE'
     where profile.user_id=new.user_id and profile.status='ACTIVE'
       and membership.status='ACTIVE'
       and membership.role in ('OWNER','CIO','TRADER')
       and membership.effective_from <= now()
       and (membership.effective_to is null or membership.effective_to > now())
  ) then
    raise exception 'inactive or unauthorized USER PAPER directive scope';
  end if;
  return new;
end;
$$;
revoke all on function execution.validate_user_directive_scope() from public;

create trigger user_directives_validate_scope
before insert or update of user_id, fund_id, book_id
on execution.user_directives
for each row execute function execution.validate_user_directive_scope();

create trigger user_directives_touch_updated_at
before update on execution.user_directives
for each row execute function governance.touch_updated_at();
create trigger user_directive_legs_touch_updated_at
before update on execution.user_directive_legs
for each row execute function governance.touch_updated_at();
create trigger paper_directive_barriers_touch_updated_at
before update on execution.paper_directive_barriers
for each row execute function governance.touch_updated_at();

-- Close the no-row race between automated PAPER admission and USER priority.
-- Both this trigger and the directive service lock the same book key. Updates
-- toward cancellation/reconciliation are intentionally allowed through.
create or replace function execution.guard_automated_paper_order_admission()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, execution
as $$
declare
  target_fund uuid;
  target_book uuid;
  blocking_directive uuid;
begin
  if new.broker_adapter <> 'paper' then
    return new;
  end if;
  if tg_op = 'UPDATE' and new.state <> 'SUBMITTED' then
    return new;
  end if;

  select fund_id,book_id into target_fund,target_book
    from execution.order_intents
   where order_intent_id=new.order_intent_id;
  if target_fund is null or target_book is null then
    raise exception 'PAPER order intent has no canonical fund/book scope';
  end if;

  perform pg_advisory_xact_lock(
    hashtextextended('paper:' || target_fund::text || ':' || target_book::text, 0)
  );
  select active_directive_id into blocking_directive
    from execution.paper_directive_barriers
   where fund_id=target_fund and book_id=target_book;
  if blocking_directive is not null or exists (
    select 1 from execution.paper_order_reservations reservation
     where reservation.fund_id=target_fund
       and reservation.book_id=target_book
       and reservation.state='ACTIVE'
  ) then
    raise exception 'USER PAPER execution/reservation blocks automated PAPER admission (directive=%)',
      blocking_directive;
  end if;
  return new;
end;
$$;
revoke all on function execution.guard_automated_paper_order_admission()
  from public;

create trigger orders_guard_user_paper_priority
before insert or update of state on execution.orders
for each row execute function execution.guard_automated_paper_order_admission();

grant usage on schema governance, accounting, reference, execution
  to svc_trading_api;
grant select on governance.user_profiles, governance.fund_memberships
  to svc_trading_api;
grant select on accounting.funds, accounting.books, accounting.ledger_accounts,
  accounting.positions, accounting.cash_balances, accounting.journals
  to svc_trading_api;
grant select on reference.instruments, reference.instrument_symbols,
  reference.market_calendar_versions, reference.market_sessions to svc_trading_api;
grant select on execution.order_intents to svc_trading_api;
grant select on execution.orders, execution.order_events, execution.fills
  to svc_trading_api;
grant update (state,last_event_at,version) on execution.orders to svc_trading_api;
grant insert on execution.order_events to svc_trading_api;
grant select, insert on execution.user_directives,
  execution.user_directive_legs, execution.paper_order_reservations,
  execution.paper_user_directive_fills
  to svc_trading_api;
grant update (state,error_code,error_message,updated_at,completed_at,version)
  on execution.user_directives to svc_trading_api;
grant update (state,filled_quantity,broker_event_id,target_filled_quantity,
              error_code,error_message,updated_at)
  on execution.user_directive_legs to svc_trading_api;
grant update (state,reserved_quantity,reserved_cash,version,released_at)
  on execution.paper_order_reservations to svc_trading_api;
grant select, insert on execution.user_directive_proofs to svc_trading_api;
grant select, insert, update, delete on execution.paper_directive_barriers
  to svc_trading_api;
grant select, insert on execution.outbox to svc_trading_api;
grant select on execution.outbox_consumed to svc_trading_api;
grant usage, select on sequence execution.outbox_outbox_id_seq
  to svc_trading_api;

-- The ledger consumer runs through a dedicated NOLOGIN role selected with
-- SET LOCAL ROLE on every transaction.  Its surface is deliberately limited
-- to canonical PAPER fill intake, posting, projection, and acknowledgement.
grant usage on schema governance, execution, accounting, reference
  to svc_accounting_ledger;
grant execute on function governance.can_access_fund(uuid)
  to svc_trading_api, svc_accounting_ledger;
grant select on execution.outbox, execution.outbox_consumed,
  execution.fills, execution.orders, execution.order_intents,
  execution.paper_user_directive_fills, execution.user_directives,
  execution.user_directive_legs, execution.paper_order_reservations
  to svc_accounting_ledger;
grant insert on execution.outbox_consumed to svc_accounting_ledger;
grant update (accounting_acknowledged_at)
  on execution.paper_user_directive_fills to svc_accounting_ledger;
grant update (state,reserved_quantity,reserved_cash,version,released_at)
  on execution.paper_order_reservations to svc_accounting_ledger;
grant select on accounting.funds, accounting.books,
  accounting.ledger_accounts, accounting.journals,
  accounting.journal_lines, accounting.positions,
  accounting.cash_balances, accounting.portfolio_snapshots,
  accounting.nav_runs to svc_accounting_ledger;
grant insert on accounting.journals, accounting.journal_lines,
  accounting.positions, accounting.cash_balances,
  accounting.portfolio_snapshots to svc_accounting_ledger;
grant update (status) on accounting.journals to svc_accounting_ledger;
grant update (quantity,average_cost,realized_pnl,last_journal_id,as_of,version)
  on accounting.positions to svc_accounting_ledger;
grant update (settled_amount,unsettled_amount,last_journal_id,as_of,version)
  on accounting.cash_balances to svc_accounting_ledger;
grant select on reference.instrument_symbols to svc_accounting_ledger;

do $paper_trading_rls$
declare
  relation_name text;
begin
  foreach relation_name in array array[
    'user_directives','user_directive_proofs','user_directive_legs',
    'paper_order_reservations','paper_directive_barriers',
    'paper_user_directive_fills'
  ] loop
    execute format('alter table execution.%I enable row level security', relation_name);
    execute format(
      'create policy %I on execution.%I for all to svc_trading_api '
      'using (true) with check (true)',
      relation_name || '_svc_trading_api_all', relation_name
    );
  end loop;
end
$paper_trading_rls$;

create policy outbox_svc_trading_api_select on execution.outbox
  for select to svc_trading_api using (
    producer='trading-user-directive'
    or (
      event_type='trading.fill.v1'
      and payload_ref->>'artifact_type'='FILL'
    )
  );
create policy outbox_svc_trading_api_insert on execution.outbox
  for insert to svc_trading_api
  with check (
    producer='trading-user-directive'
    and event_type='trading.fill.v1'
    and schema_version='event-envelope-v1'
    and payload_ref ->> 'artifact_schema'='trading-user-directive-fill-v1'
  );
create policy outbox_consumed_svc_trading_api_select
  on execution.outbox_consumed for select to svc_trading_api
  using (consumer='accounting-ledger');
create policy paper_user_directive_fills_svc_accounting_ledger_select
  on execution.paper_user_directive_fills
  for select to svc_accounting_ledger using (true);
create policy paper_user_directive_fills_svc_accounting_ledger_ack
  on execution.paper_user_directive_fills
  for update to svc_accounting_ledger
  using (true) with check (accounting_acknowledged_at is not null);
create policy user_directive_legs_svc_accounting_ledger_select
  on execution.user_directive_legs
  for select to svc_accounting_ledger using (true);
create policy user_directives_svc_accounting_ledger_select
  on execution.user_directives
  for select to svc_accounting_ledger using (true);
create policy paper_order_reservations_svc_accounting_ledger_select
  on execution.paper_order_reservations
  for select to svc_accounting_ledger using (true);
create policy paper_order_reservations_svc_accounting_ledger_update
  on execution.paper_order_reservations
  for update to svc_accounting_ledger using (true) with check (true);
create policy outbox_svc_accounting_ledger_select on execution.outbox
  for select to svc_accounting_ledger using (event_type='trading.fill.v1');
create policy outbox_consumed_svc_accounting_ledger_select
  on execution.outbox_consumed for select to svc_accounting_ledger
  using (consumer='accounting-ledger');
create policy outbox_consumed_svc_accounting_ledger_insert
  on execution.outbox_consumed for insert to svc_accounting_ledger
  with check (consumer='accounting-ledger');
create policy fills_svc_accounting_ledger_select on execution.fills
  for select to svc_accounting_ledger using (true);
create policy orders_svc_accounting_ledger_select on execution.orders
  for select to svc_accounting_ledger using (broker_adapter='paper');
create policy order_intents_svc_accounting_ledger_select
  on execution.order_intents for select to svc_accounting_ledger using (true);

create policy funds_svc_accounting_ledger_select on accounting.funds
  for select to svc_accounting_ledger using (true);
create policy books_svc_accounting_ledger_select on accounting.books
  for select to svc_accounting_ledger using (true);
create policy ledger_accounts_svc_accounting_ledger_select
  on accounting.ledger_accounts for select to svc_accounting_ledger using (true);
create policy journals_svc_accounting_ledger_all on accounting.journals
  for all to svc_accounting_ledger using (true) with check (true);
create policy journal_lines_svc_accounting_ledger_all on accounting.journal_lines
  for all to svc_accounting_ledger using (true) with check (true);
create policy positions_svc_accounting_ledger_all on accounting.positions
  for all to svc_accounting_ledger using (true) with check (true);
create policy cash_balances_svc_accounting_ledger_all on accounting.cash_balances
  for all to svc_accounting_ledger using (true) with check (true);
create policy portfolio_snapshots_svc_accounting_ledger_all
  on accounting.portfolio_snapshots for all to svc_accounting_ledger
  using (true) with check (true);
create policy nav_runs_svc_accounting_ledger_select on accounting.nav_runs
  for select to svc_accounting_ledger using (true);
create policy instrument_symbols_svc_accounting_ledger_select
  on reference.instrument_symbols for select to svc_accounting_ledger using (true);

create policy user_profiles_svc_trading_api_select on governance.user_profiles
  for select to svc_trading_api using (true);
create policy fund_memberships_svc_trading_api_select on governance.fund_memberships
  for select to svc_trading_api using (true);
create policy funds_svc_trading_api_select on accounting.funds
  for select to svc_trading_api using (true);
create policy books_svc_trading_api_select on accounting.books
  for select to svc_trading_api using (true);
create policy ledger_accounts_svc_trading_api_select on accounting.ledger_accounts
  for select to svc_trading_api using (true);
create policy positions_svc_trading_api_select on accounting.positions
  for select to svc_trading_api using (true);
create policy cash_balances_svc_trading_api_select on accounting.cash_balances
  for select to svc_trading_api using (true);
create policy journals_svc_trading_api_select on accounting.journals
  for select to svc_trading_api using (true);
create policy instruments_svc_trading_api_select on reference.instruments
  for select to svc_trading_api using (true);
create policy instrument_symbols_svc_trading_api_select on reference.instrument_symbols
  for select to svc_trading_api using (true);
create policy market_calendars_svc_trading_api_select on reference.market_calendar_versions
  for select to svc_trading_api using (market='KRX');
create policy market_sessions_svc_trading_api_select on reference.market_sessions
  for select to svc_trading_api using (market='KRX');
create policy order_intents_svc_trading_api_select on execution.order_intents
  for select to svc_trading_api using (true);
create policy orders_svc_trading_api_select on execution.orders
  for select to svc_trading_api using (broker_adapter='paper');
create policy orders_svc_trading_api_update on execution.orders
  for update to svc_trading_api
  using (broker_adapter='paper') with check (broker_adapter='paper');
create policy order_events_svc_trading_api_select on execution.order_events
  for select to svc_trading_api using (broker_adapter='paper');
create policy order_events_svc_trading_api_insert on execution.order_events
  for insert to svc_trading_api
  with check (
    broker_adapter='paper'
    and exists (
      select 1 from execution.orders target
       where target.order_id=execution.order_events.order_id
         and target.broker_adapter='paper'
    )
  );
create policy fills_svc_trading_api_select on execution.fills
  for select to svc_trading_api
  using (
    exists (
      select 1 from execution.orders target
       where target.order_id=execution.fills.order_id
         and target.broker_adapter='paper'
    )
  );

do $paper_trading_role_audit$
declare
  pool_login name := session_user;
begin
  if not exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid=membership.roleid
      join pg_roles member_role on member_role.oid=membership.member
     where granted_role.rolname='svc_trading_api'
       and member_role.rolname=pool_login
       and membership.set_option and not membership.inherit_option
  ) then
    raise exception '% cannot explicitly reduce to svc_trading_api', pool_login;
  end if;
  if has_table_privilege('svc_trading_api','execution.orders','INSERT')
     or has_table_privilege('svc_trading_api','execution.orders','DELETE')
     or has_table_privilege('svc_trading_api','execution.order_intents','INSERT')
     or has_table_privilege('svc_trading_api','execution.fills','INSERT') then
    raise exception 'svc_trading_api exceeds the PAPER directive boundary';
  end if;
  if not has_table_privilege(
       'svc_trading_api','execution.user_directives','INSERT')
     or not has_column_privilege(
       'svc_trading_api','execution.orders','state','UPDATE')
     or not has_table_privilege(
       'svc_trading_api','reference.market_sessions','SELECT')
     or not has_table_privilege(
       'svc_trading_api','execution.paper_user_directive_fills','INSERT')
     or not has_table_privilege(
       'svc_trading_api','execution.outbox','INSERT') then
    raise exception 'svc_trading_api lacks a required PAPER directive privilege';
  end if;
end
$paper_trading_role_audit$;

comment on table execution.user_directives is
  'Authenticated user PAPER commands; never Alpha/Risk evidence.';
comment on table execution.user_directive_legs is
  'Durable PAPER ACK/cancel legs. ACK remains IN_PROGRESS until fills/accounting reconcile.';
comment on table execution.paper_order_reservations is
  'Trading-owned reservations; positions/cash change only from canonical fill accounting.';
comment on table execution.paper_user_directive_fills is
  'Immutable direct USER PAPER fill evidence; quote-event idempotent and independent of strategy/Risk intents.';
comment on table execution.paper_directive_barriers is
  'Per-book USER priority barrier checked atomically by automated PAPER order admission.';

commit;
