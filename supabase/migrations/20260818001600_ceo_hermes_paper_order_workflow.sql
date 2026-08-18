begin;

-- Durable user authority for CEO -> Kanban -> Trading Hermes -> PAPER OMS.
--
-- Hermes never receives a browser token, service JWT, database credential, or
-- broker credential.  It writes only a non-authoritative interpretation.  The
-- trusted order orchestrator re-checks the current Fund/Book grant and mints a
-- fresh short-lived, payload-bound proof immediately before Trading admission.

do $$
declare
  pool_login name := session_user;
begin
  if not exists (select 1 from pg_roles where rolname = 'svc_order_orchestrator') then
    create role svc_order_orchestrator
      nologin nosuperuser nocreatedb nocreaterole noinherit
      noreplication nobypassrls;
  elsif exists (
    select 1 from pg_roles
     where rolname = 'svc_order_orchestrator'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_order_orchestrator role name is occupied by an unsafe role';
  end if;
  execute format(
    'grant svc_order_orchestrator to %I with set true, inherit false', pool_login
  );
end
$$;

-- A pre-created but otherwise safe NOLOGIN role must not carry stale grants.
-- Rebuild its exact surface before granting the workflow permissions below.
revoke all on schema governance, accounting, reference, execution
  from svc_order_orchestrator;
revoke all privileges on all tables in schema governance
  from svc_order_orchestrator;
revoke all privileges on all tables in schema accounting
  from svc_order_orchestrator;
revoke all privileges on all tables in schema reference
  from svc_order_orchestrator;
revoke all privileges on all tables in schema execution
  from svc_order_orchestrator;
revoke all privileges on all sequences in schema governance
  from svc_order_orchestrator;
revoke all privileges on all sequences in schema accounting
  from svc_order_orchestrator;
revoke all privileges on all sequences in schema reference
  from svc_order_orchestrator;
revoke all privileges on all sequences in schema execution
  from svc_order_orchestrator;

-- Give both directions of the request/directive link a scope-bearing key.
-- This prevents an orchestrator bug from linking one user's request to a
-- directive admitted for a different user, Fund, or Book.
alter table execution.user_directives
  add constraint user_directives_authority_identity_unique
  unique (directive_id, user_id, fund_id, book_id);

create table execution.user_order_requests (
  order_request_id uuid primary key default gen_random_uuid(),
  user_id uuid not null references governance.user_profiles(user_id),
  fund_id uuid not null references accounting.funds(fund_id),
  book_id uuid not null references accounting.books(book_id),
  client_request_id text not null check (length(client_request_id) between 8 and 128),
  mode text not null default 'PAPER' check (mode = 'PAPER'),
  raw_instruction text not null check (length(raw_instruction) between 1 and 2000),
  normalized_instruction text not null check (length(normalized_instruction) between 1 and 2000),
  raw_instruction_sha256 text not null check (raw_instruction_sha256 ~ '^[0-9a-f]{64}$'),
  normalizer_version text not null default 'user-order-language.v1',
  ceo_root_task_id text unique,
  trading_task_id text unique,
  state text not null default 'RECEIVED' check (
    state in (
      'RECEIVED','KANBAN_QUEUED','INTERPRETING','INTERPRETED',
      'CLARIFICATION_REQUIRED','NOT_ORDER','REJECTED','SUBMITTED',
      'IN_PROGRESS','ACCOUNTING_PENDING','COMPLETED','FAILED','UNKNOWN'
    )
  ),
  action text check (action is null or action in ('PLACE_ORDER','SELL_ALL','CANCEL_ALL')),
  canonical_payload jsonb check (
    canonical_payload is null or jsonb_typeof(canonical_payload) = 'object'
  ),
  payload_sha256 text check (payload_sha256 is null or payload_sha256 ~ '^[0-9a-f]{64}$'),
  directive_id uuid unique,
  clarification_code text,
  error_code text,
  error_message text,
  version integer not null default 1 check (version > 0),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  completed_at timestamptz,
  unique (user_id, client_request_id),
  unique (order_request_id, user_id, fund_id, book_id),
  foreign key (book_id, fund_id) references accounting.books(book_id, fund_id),
  foreign key (directive_id, user_id, fund_id, book_id)
    references execution.user_directives(directive_id, user_id, fund_id, book_id),
  check (
    (canonical_payload is null and payload_sha256 is null)
    or (canonical_payload is not null and payload_sha256 is not null)
  )
);

create index user_order_requests_state_idx
  on execution.user_order_requests (state, created_at, order_request_id);
create index user_order_requests_scope_idx
  on execution.user_order_requests (user_id, fund_id, book_id, created_at desc);

create table execution.user_order_interpretations (
  interpretation_id uuid primary key default gen_random_uuid(),
  order_request_id uuid not null references execution.user_order_requests(order_request_id) on delete cascade,
  interpretation_version integer not null check (interpretation_version > 0),
  source text not null check (source in ('HERMES','DETERMINISTIC')),
  trading_task_id text not null,
  raw_instruction_sha256 text not null check (raw_instruction_sha256 ~ '^[0-9a-f]{64}$'),
  interpretation jsonb not null check (jsonb_typeof(interpretation) = 'object'),
  interpretation_sha256 text not null check (interpretation_sha256 ~ '^[0-9a-f]{64}$'),
  created_at timestamptz not null default now(),
  unique (order_request_id, interpretation_version, source),
  unique (order_request_id, interpretation_sha256)
);

create table execution.user_order_request_events (
  event_id text primary key check (length(event_id) between 8 and 128),
  order_request_id uuid not null references execution.user_order_requests(order_request_id) on delete cascade,
  event_type text not null check (length(event_type) between 1 and 64),
  from_state text,
  to_state text not null,
  payload jsonb not null default '{}'::jsonb
    check (jsonb_typeof(payload) = 'object'),
  created_at timestamptz not null default now()
);

create index user_order_request_events_order_idx
  on execution.user_order_request_events (order_request_id, created_at, event_id);

alter table execution.user_directives
  add column source_order_request_id uuid unique,
  add constraint user_directives_source_order_scope_fkey
    foreign key (source_order_request_id, user_id, fund_id, book_id)
    references execution.user_order_requests(
      order_request_id, user_id, fund_id, book_id
    );

create trigger user_order_requests_touch_updated_at
before update on execution.user_order_requests
for each row execute function governance.touch_updated_at();

grant usage on schema execution to svc_order_orchestrator;
grant select, insert on execution.user_order_requests
  to svc_order_orchestrator;
grant update (
  ceo_root_task_id, trading_task_id, state, action, canonical_payload,
  payload_sha256, directive_id, clarification_code, error_code, error_message,
  version, completed_at
) on execution.user_order_requests to svc_order_orchestrator;
grant select, insert on execution.user_order_interpretations,
  execution.user_order_request_events to svc_order_orchestrator;
grant select on execution.user_directives to svc_order_orchestrator;
grant update (source_order_request_id) on execution.user_directives
  to svc_order_orchestrator;

alter table execution.user_order_requests enable row level security;
alter table execution.user_order_interpretations enable row level security;
alter table execution.user_order_request_events enable row level security;

create policy user_order_requests_svc_order_orchestrator_all
  on execution.user_order_requests for all to svc_order_orchestrator
  using (true) with check (true);
create policy user_order_interpretations_svc_order_orchestrator_all
  on execution.user_order_interpretations for all to svc_order_orchestrator
  using (true) with check (true);
create policy user_order_request_events_svc_order_orchestrator_all
  on execution.user_order_request_events for all to svc_order_orchestrator
  using (true) with check (true);
create policy user_directives_svc_order_orchestrator_select
  on execution.user_directives for select to svc_order_orchestrator
  using (true);
create policy user_directives_svc_order_orchestrator_source_update
  on execution.user_directives for update to svc_order_orchestrator
  using (true) with check (true);

do $order_orchestrator_role_audit$
declare
  pool_login name := session_user;
begin
  if not exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid=membership.roleid
      join pg_roles member_role on member_role.oid=membership.member
     where granted_role.rolname='svc_order_orchestrator'
       and member_role.rolname=pool_login
       and membership.set_option and not membership.inherit_option
  ) then
    raise exception '% cannot explicitly reduce to svc_order_orchestrator', pool_login;
  end if;
  if has_column_privilege(
       'svc_order_orchestrator','execution.user_order_requests','user_id','UPDATE'
     )
     or has_column_privilege(
       'svc_order_orchestrator','execution.user_order_requests','fund_id','UPDATE'
     )
     or has_column_privilege(
       'svc_order_orchestrator','execution.user_order_requests','book_id','UPDATE'
     )
     or has_table_privilege(
       'svc_order_orchestrator','execution.user_order_interpretations','UPDATE'
     )
     or has_table_privilege(
       'svc_order_orchestrator','execution.user_order_request_events','DELETE'
     )
     or has_table_privilege(
       'svc_order_orchestrator','execution.user_directives','INSERT'
     )
     or has_table_privilege(
       'svc_order_orchestrator','governance.user_profiles','SELECT'
     ) then
    raise exception 'svc_order_orchestrator exceeds its PAPER workflow boundary';
  end if;
  if not has_table_privilege(
       'svc_order_orchestrator','execution.user_order_requests','INSERT'
     )
     or not has_column_privilege(
       'svc_order_orchestrator','execution.user_order_requests','state','UPDATE'
     )
     or not has_table_privilege(
       'svc_order_orchestrator','execution.user_order_interpretations','INSERT'
     )
     or not has_column_privilege(
       'svc_order_orchestrator','execution.user_directives',
       'source_order_request_id','UPDATE'
     ) then
    raise exception 'svc_order_orchestrator lacks a required PAPER workflow privilege';
  end if;
end
$order_orchestrator_role_audit$;

comment on table execution.user_order_requests is
  'Authenticated PAPER-only authority admitted before CEO/Kanban/Hermes interpretation; never stores bearer tokens.';
comment on table execution.user_order_interpretations is
  'Append-only non-authoritative Hermes/deterministic interpretations bound to the exact raw instruction digest.';
comment on table execution.user_order_request_events is
  'Append-only state transition audit for the CEO-to-PAPER-order workflow.';

commit;
