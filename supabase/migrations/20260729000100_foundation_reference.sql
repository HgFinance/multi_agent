begin;

-- Hosted Supabase Auth is an external identity provider in production.  The
-- private control database therefore has no auth schema/bootstrap.  Keep the
-- historical PostgREST grant targets as inert NOLOGIN compatibility roles so
-- the same migration chain can be replayed on an empty ordinary PostgreSQL
-- cluster; applications never receive credentials for these roles.
do $external_auth_compatibility_roles$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit nosuperuser nocreatedb nocreaterole
      noreplication nobypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit nosuperuser nocreatedb
      nocreaterole noreplication nobypassrls;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit nosuperuser nocreatedb
      nocreaterole noreplication nobypassrls;
  end if;
end
$external_auth_compatibility_roles$;

create schema if not exists extensions;
create extension if not exists pgcrypto with schema extensions;
create extension if not exists vector with schema extensions;
create extension if not exists btree_gist with schema extensions;

create schema if not exists governance;
create schema if not exists workforce;
create schema if not exists reference;
create schema if not exists research;
create schema if not exists quant;
create schema if not exists strategy;
create schema if not exists execution;
create schema if not exists risk;
create schema if not exists accounting;
create schema if not exists audit;
create schema if not exists api;

revoke all on schema governance, workforce, reference, research, quant,
  strategy, execution, risk, accounting, audit from public;

create or replace function governance.touch_updated_at()
returns trigger
language plpgsql
set search_path = pg_catalog
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

create or replace function governance.current_user_id()
returns uuid
language sql
stable
set search_path = pg_catalog
as $$
  select nullif(current_setting('request.jwt.claim.sub', true), '')::uuid;
$$;

create or replace function governance.current_jwt_role()
returns text
language sql
stable
set search_path = pg_catalog
as $$
  select nullif(current_setting('request.jwt.claim.role', true), '');
$$;

create table accounting.funds (
  fund_id uuid primary key default gen_random_uuid(),
  fund_code text not null unique,
  name text not null,
  base_currency text not null check (base_currency ~ '^[A-Z]{3}$'),
  inception_date date not null,
  status text not null default 'DRAFT'
    check (status in ('DRAFT', 'ACTIVE', 'SUSPENDED', 'CLOSED')),
  legal_entity jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table accounting.books (
  book_id uuid primary key default gen_random_uuid(),
  fund_id uuid not null references accounting.funds(fund_id),
  book_code text not null,
  name text not null,
  book_type text not null,
  manager text,
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'CLOSED')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (fund_id, book_code),
  unique (book_id, fund_id)
);

create table governance.user_profiles (
  -- Hosted Supabase Auth is external to the private control DB.  The verified
  -- JWT sub is projected here by portfolio-bff, so a fresh ordinary Postgres
  -- bootstrap must not require Supabase's private auth schema to exist.
  user_id uuid primary key,
  display_name text not null,
  timezone text not null default 'Asia/Seoul',
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'CLOSED')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table governance.user_preferences (
  user_id uuid primary key references governance.user_profiles(user_id) on delete cascade,
  report_schedule jsonb not null default '{}'::jsonb,
  notification jsonb not null default '{}'::jsonb,
  explanation_level text not null default 'STANDARD'
    check (explanation_level in ('BRIEF', 'STANDARD', 'DETAILED')),
  version integer not null default 1 check (version > 0),
  updated_at timestamptz not null default now()
);

create table governance.fund_memberships (
  fund_id uuid not null references accounting.funds(fund_id) on delete cascade,
  user_id uuid not null references governance.user_profiles(user_id) on delete cascade,
  role text not null check (role in ('OWNER', 'CIO', 'RISK', 'TRADER', 'RESEARCHER', 'AUDITOR', 'VIEWER')),
  status text not null default 'ACTIVE' check (status in ('ACTIVE', 'SUSPENDED', 'REVOKED')),
  effective_from timestamptz not null default now(),
  effective_to timestamptz,
  created_at timestamptz not null default now(),
  primary key (fund_id, user_id, role),
  check (effective_to is null or effective_to > effective_from)
);

create or replace function governance.can_access_fund(target_fund_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, governance
as $$
  select governance.current_jwt_role() = 'service_role'
    or exists (
      select 1
      from governance.fund_memberships membership
      where membership.fund_id = target_fund_id
        and membership.user_id = governance.current_user_id()
        and membership.status = 'ACTIVE'
        and membership.effective_from <= now()
        and (membership.effective_to is null or membership.effective_to > now())
    );
$$;

create table reference.data_sources (
  source_id uuid primary key default gen_random_uuid(),
  source_code text not null unique,
  name text not null,
  source_type text not null,
  owner text not null,
  license_terms jsonb not null default '{}'::jsonb,
  retention_policy jsonb not null default '{}'::jsonb,
  allowed_uses text[] not null default '{}',
  prohibited_uses text[] not null default '{}',
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'SUSPENDED', 'RETIRED')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table reference.issuers (
  issuer_id uuid primary key default gen_random_uuid(),
  corp_code text,
  legal_name text not null,
  display_name text not null,
  country_code text not null default 'KR' check (country_code ~ '^[A-Z]{2}$'),
  industry_code text,
  fiscal_month smallint check (fiscal_month between 1 and 12),
  status text not null default 'ACTIVE'
    check (status in ('ACTIVE', 'INACTIVE', 'MERGED', 'LIQUIDATED')),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique nulls not distinct (corp_code)
);

create table reference.instruments (
  instrument_id uuid primary key default gen_random_uuid(),
  issuer_id uuid references reference.issuers(issuer_id),
  instrument_type text not null,
  asset_class text not null,
  market text not null,
  venue text,
  currency text not null check (currency ~ '^[A-Z]{3}$'),
  display_name text not null,
  isin text,
  listed_from date,
  listed_to date,
  status text not null default 'ACTIVE'
    check (status in ('PENDING', 'ACTIVE', 'HALTED', 'DELISTED', 'EXPIRED')),
  price_scale integer not null default 0 check (price_scale between 0 and 12),
  quantity_scale integer not null default 0 check (quantity_scale between 0 and 12),
  lot_size numeric(30, 10) not null default 1 check (lot_size > 0),
  tick_size numeric(30, 10) check (tick_size is null or tick_size > 0),
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  check (listed_to is null or listed_from is null or listed_to >= listed_from),
  unique nulls not distinct (isin)
);

create table reference.instrument_symbols (
  instrument_symbol_id uuid primary key default gen_random_uuid(),
  instrument_id uuid not null references reference.instruments(instrument_id) on delete cascade,
  provider text not null,
  market text not null,
  symbol text not null,
  symbol_type text not null default 'TRADING',
  valid_from timestamptz not null,
  valid_to timestamptz,
  is_primary boolean not null default false,
  created_at timestamptz not null default now(),
  check (valid_to is null or valid_to > valid_from),
  unique (provider, market, symbol, valid_from)
);

create index instrument_symbols_lookup_idx
  on reference.instrument_symbols (provider, market, symbol, valid_from desc);
create index instrument_symbols_instrument_idx
  on reference.instrument_symbols (instrument_id, valid_from desc);

create table reference.derivative_contracts (
  instrument_id uuid primary key references reference.instruments(instrument_id) on delete cascade,
  underlying_instrument_id uuid references reference.instruments(instrument_id),
  contract_kind text not null check (contract_kind in ('FUTURE', 'CALL', 'PUT', 'WARRANT')),
  expiry_date date not null,
  strike_price numeric(30, 10),
  contract_multiplier numeric(30, 10) not null check (contract_multiplier > 0),
  settlement_type text not null check (settlement_type in ('CASH', 'PHYSICAL')),
  exercise_style text check (exercise_style in ('AMERICAN', 'EUROPEAN', 'ASIAN', 'OTHER')),
  margin_currency text check (margin_currency is null or margin_currency ~ '^[A-Z]{3}$'),
  metadata jsonb not null default '{}'::jsonb,
  check ((contract_kind in ('CALL', 'PUT') and strike_price is not null) or contract_kind not in ('CALL', 'PUT'))
);

create table reference.market_calendar_versions (
  calendar_version_id uuid primary key default gen_random_uuid(),
  market text not null,
  version integer not null check (version > 0),
  source_id uuid references reference.data_sources(source_id),
  published_at timestamptz,
  effective_from date not null,
  effective_to date,
  content_hash text not null,
  created_at timestamptz not null default now(),
  check (effective_to is null or effective_to >= effective_from),
  unique (market, version),
  unique (market, content_hash)
);

create table reference.market_sessions (
  calendar_version_id uuid not null references reference.market_calendar_versions(calendar_version_id) on delete cascade,
  market text not null,
  trade_date date not null,
  session_type text not null,
  opens_at timestamptz,
  closes_at timestamptz,
  is_trading_day boolean not null,
  metadata jsonb not null default '{}'::jsonb,
  primary key (calendar_version_id, market, trade_date, session_type),
  check (closes_at is null or opens_at is null or closes_at > opens_at)
);

create table reference.corporate_actions (
  action_id uuid primary key default gen_random_uuid(),
  instrument_id uuid not null references reference.instruments(instrument_id),
  source_id uuid not null references reference.data_sources(source_id),
  external_id text,
  action_type text not null,
  announced_at timestamptz,
  ex_date date,
  record_date date,
  effective_at timestamptz,
  ratio numeric(30, 12),
  cash_amount numeric(30, 10),
  currency text check (currency is null or currency ~ '^[A-Z]{3}$'),
  revision integer not null default 1 check (revision > 0),
  corrects_action_id uuid references reference.corporate_actions(action_id),
  status text not null default 'ANNOUNCED'
    check (status in ('ANNOUNCED', 'CONFIRMED', 'APPLIED', 'CANCELLED')),
  payload jsonb not null default '{}'::jsonb,
  observed_at timestamptz not null,
  created_at timestamptz not null default now(),
  unique nulls not distinct (source_id, external_id, revision)
);

create table reference.fx_pairs (
  fx_pair_id uuid primary key default gen_random_uuid(),
  base_currency text not null check (base_currency ~ '^[A-Z]{3}$'),
  quote_currency text not null check (quote_currency ~ '^[A-Z]{3}$'),
  instrument_id uuid references reference.instruments(instrument_id),
  status text not null default 'ACTIVE' check (status in ('ACTIVE', 'INACTIVE')),
  unique (base_currency, quote_currency),
  check (base_currency <> quote_currency)
);

create trigger funds_touch_updated_at
before update on accounting.funds
for each row execute function governance.touch_updated_at();

create trigger books_touch_updated_at
before update on accounting.books
for each row execute function governance.touch_updated_at();

create trigger user_profiles_touch_updated_at
before update on governance.user_profiles
for each row execute function governance.touch_updated_at();

create trigger data_sources_touch_updated_at
before update on reference.data_sources
for each row execute function governance.touch_updated_at();

create trigger issuers_touch_updated_at
before update on reference.issuers
for each row execute function governance.touch_updated_at();

create trigger instruments_touch_updated_at
before update on reference.instruments
for each row execute function governance.touch_updated_at();

alter table accounting.funds enable row level security;
alter table accounting.books enable row level security;
alter table governance.user_profiles enable row level security;
alter table governance.user_preferences enable row level security;
alter table governance.fund_memberships enable row level security;
alter table reference.data_sources enable row level security;
alter table reference.issuers enable row level security;
alter table reference.instruments enable row level security;
alter table reference.instrument_symbols enable row level security;
alter table reference.derivative_contracts enable row level security;
alter table reference.market_calendar_versions enable row level security;
alter table reference.market_sessions enable row level security;
alter table reference.corporate_actions enable row level security;
alter table reference.fx_pairs enable row level security;

create policy user_profiles_select_own
on governance.user_profiles for select
using (user_id = governance.current_user_id());

create policy user_preferences_all_own
on governance.user_preferences for all
using (user_id = governance.current_user_id())
with check (user_id = governance.current_user_id());

create policy fund_memberships_select_own
on governance.fund_memberships for select
using (user_id = governance.current_user_id());

create policy funds_select_member
on accounting.funds for select
using (governance.can_access_fund(fund_id));

create policy books_select_member
on accounting.books for select
using (governance.can_access_fund(fund_id));

commit;
