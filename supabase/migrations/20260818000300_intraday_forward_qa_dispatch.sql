begin;

-- Terminal work items intentionally clear their schedule. The original NOT
-- NULL declaration contradicted chk_intraday_forward_work_schedule.
alter table quant.intraday_forward_work_items
  alter column next_attempt_at drop not null;

-- The stock-universe resolver joins symbols to the already-scoped instrument
-- master. Expose only the identity columns it reads and preserve the same
-- fail-closed STOCK boundary as reference.instruments.
grant select (
  instrument_id, provider, market, symbol, symbol_type, is_primary,
  valid_from, valid_to
) on reference.instrument_symbols to svc_quant;
drop policy if exists reference_instruments_svc_quant_stock_only_select
  on reference.instruments;
create policy reference_instruments_svc_quant_stock_only_select
  on reference.instruments for select to svc_quant
  using (
    upper(instrument_type) = 'STOCK'
    and upper(asset_class) = 'EQUITY'
    and upper(market) = 'KRX'
    and upper(status) = 'ACTIVE'
  );
create policy reference_instrument_symbols_svc_quant_stock_only_select
  on reference.instrument_symbols for select to svc_quant
  using (exists (
    select 1
      from reference.instruments instrument
     where instrument.instrument_id = instrument_symbols.instrument_id
       and upper(instrument.instrument_type) = 'STOCK'
       and upper(instrument.asset_class) = 'EQUITY'
       and upper(instrument.market) = 'KRX'
       and upper(instrument.status) = 'ACTIVE'
  ));

-- The application revalidates the universe, but the immutable rung is the
-- authoritative allocation. Reject a rung unless every instrument/session
-- pair is an ACTIVE KRX EQUITY/STOCK inside its listing interval. Count-exact
-- comparison makes missing reference metadata fail closed.
create or replace function quant.validate_intraday_stock_rung_scope()
returns trigger
language plpgsql
set search_path = pg_catalog, quant, reference
as $$
declare
  eligible_instruments integer;
  eligible_instrument_sessions bigint;
  expected_instrument_sessions bigint;
begin
  if new.rung not in (
    'CALIBRATION', 'DISCOVERY_6', 'VALIDATION_20', 'FULL_60', 'FORWARD'
  ) then
    return new;
  end if;

  select count(*)
    into eligible_instruments
    from unnest(new.planned_instrument_ids) planned(instrument_id)
    join reference.instruments instrument
      on instrument.instrument_id = planned.instrument_id
     and upper(instrument.instrument_type) = 'STOCK'
     and upper(instrument.asset_class) = 'EQUITY'
     and upper(instrument.market) = 'KRX'
     and upper(instrument.status) = 'ACTIVE';

  select count(*)
    into eligible_instrument_sessions
    from unnest(new.planned_instrument_ids) planned(instrument_id)
    cross join unnest(new.planned_session_dates) session(session_date)
    join reference.instruments instrument
      on instrument.instrument_id = planned.instrument_id
     and upper(instrument.instrument_type) = 'STOCK'
     and upper(instrument.asset_class) = 'EQUITY'
     and upper(instrument.market) = 'KRX'
     and upper(instrument.status) = 'ACTIVE'
     and (instrument.listed_from is null
          or instrument.listed_from <= session.session_date)
     and (instrument.listed_to is null
          or instrument.listed_to >= session.session_date);

  expected_instrument_sessions :=
    new.planned_instrument_count::bigint *
    new.planned_session_count::bigint;
  if eligible_instruments <> new.planned_instrument_count
     or eligible_instrument_sessions <> expected_instrument_sessions then
    raise exception
      'intraday rung requires count-exact ACTIVE KRX EQUITY/STOCK identity within every listing interval';
  end if;
  return new;
end
$$;

create trigger intraday_rung_stock_scope_guard
before insert on quant.intraday_experiment_rungs
for each row execute function quant.validate_intraday_stock_rung_scope();

do $intraday_stock_rung_audit$
begin
  if exists (
    select 1
      from quant.intraday_experiment_rungs rung
     where rung.rung in (
       'CALIBRATION', 'DISCOVERY_6', 'VALIDATION_20', 'FULL_60', 'FORWARD'
     )
       and (
         (select count(*)
            from unnest(rung.planned_instrument_ids) planned(instrument_id)
            join reference.instruments instrument
              on instrument.instrument_id = planned.instrument_id
             and upper(instrument.instrument_type) = 'STOCK'
             and upper(instrument.asset_class) = 'EQUITY'
             and upper(instrument.market) = 'KRX'
             and upper(instrument.status) = 'ACTIVE')
           <> rung.planned_instrument_count
         or
         (select count(*)
            from unnest(rung.planned_instrument_ids) planned(instrument_id)
            cross join unnest(rung.planned_session_dates)
              session(session_date)
            join reference.instruments instrument
              on instrument.instrument_id = planned.instrument_id
             and upper(instrument.instrument_type) = 'STOCK'
             and upper(instrument.asset_class) = 'EQUITY'
             and upper(instrument.market) = 'KRX'
             and upper(instrument.status) = 'ACTIVE'
             and (instrument.listed_from is null
                  or instrument.listed_from <= session.session_date)
             and (instrument.listed_to is null
                  or instrument.listed_to >= session.session_date))
           <> rung.planned_instrument_count::bigint *
              rung.planned_session_count::bigint
       )
  ) then
    raise exception
      'existing intraday rung violates ACTIVE KRX EQUITY/STOCK listing scope';
  end if;
end
$intraday_stock_rung_audit$;

-- Match Python uuid.uuid5(uuid.NAMESPACE_URL, message_id). Redis Stream IDs
-- identify delivery attempts; this UUID identifies the immutable event.
create or replace function quant.intraday_forward_qa_event_id(
  p_qa_handoff_id uuid
)
returns uuid
language plpgsql
immutable
strict
set search_path = pg_catalog
as $$
declare
  event_name text :=
    'quant.intraday.forward.qa_requested.v1:' || p_qa_handoff_id::text;
  hash_bytes bytea;
  hash_hex text;
begin
  hash_bytes := substring(
    extensions.digest(
      uuid_send('6ba7b811-9dad-11d1-80b4-00c04fd430c8'::uuid)
        || convert_to(event_name, 'UTF8'),
      'sha1'
    )
    from 1 for 16
  );
  hash_bytes := set_byte(
    hash_bytes, 6, (get_byte(hash_bytes, 6) & 15) | 80
  );
  hash_bytes := set_byte(
    hash_bytes, 8, (get_byte(hash_bytes, 8) & 63) | 128
  );
  hash_hex := encode(hash_bytes, 'hex');
  return (
    substr(hash_hex, 1, 8) || '-' || substr(hash_hex, 9, 4) || '-' ||
    substr(hash_hex, 13, 4) || '-' || substr(hash_hex, 17, 4) || '-' ||
    substr(hash_hex, 21, 12)
  )::uuid;
end
$$;

-- Immutable transactional outbox. An AFTER INSERT trigger below writes this
-- row inside the exact transaction that creates the PASS QA handoff.
create table quant.intraday_forward_qa_outbox (
  outbox_id               bigserial primary key,
  event_id                uuid not null unique,
  qa_handoff_id           uuid not null
    references quant.intraday_forward_qa_handoffs(qa_handoff_id)
    on delete restrict,
  message_id              text not null unique,
  event_type              text not null
    default 'quant.intraday.forward.qa_requested.v1',
  schema_version          text not null default 'event-envelope-v1',
  case_id                 uuid references governance.cases(case_id)
    on delete restrict,
  trace_id                uuid not null,
  producer                text not null default 'quant-backtest-department',
  occurred_at             timestamptz not null,
  idempotency_key         text not null unique,
  payload_ref             jsonb not null,
  reproduction_contract  jsonb not null,
  event_payload          jsonb not null,
  payload_fingerprint     text not null,
  created_at              timestamptz not null default now(),

  constraint uq_intraday_forward_qa_outbox_handoff unique (qa_handoff_id),
  constraint chk_intraday_forward_qa_outbox_event check
    (event_type = 'quant.intraday.forward.qa_requested.v1'
     and schema_version = 'event-envelope-v1'
     and producer = 'quant-backtest-department'),
  constraint chk_intraday_forward_qa_outbox_event_id check
    (event_id = quant.intraday_forward_qa_event_id(qa_handoff_id)),
  constraint chk_intraday_forward_qa_outbox_message_id check
    (message_id = event_type || ':' || qa_handoff_id::text),
  constraint chk_intraday_forward_qa_outbox_ref check
    (jsonb_typeof(payload_ref) = 'object'
     and payload_ref ?& array[
       'artifact_type', 'artifact_id', 'artifact_schema', 'content_hash'
     ]
     and payload_ref - array[
       'artifact_type', 'artifact_id', 'artifact_schema', 'content_hash'
     ] = '{}'::jsonb
     and payload_ref->>'artifact_type' =
       'INTRADAY_FORWARD_REPORT_REVISION'
     and payload_ref->>'artifact_id' ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and payload_ref->>'artifact_schema' =
       'intraday-forward-report-revision-v1'
     and payload_ref->>'content_hash' ~ '^sha256:[0-9a-f]{64}$'),
  constraint chk_intraday_forward_qa_outbox_contract check
    (jsonb_typeof(reproduction_contract) = 'object'
     and reproduction_contract ?& array[
       'qa_handoff_id', 'forward_confirmation_id', 'report_revision_id',
       'report_fingerprint', 'outcome_revision_id',
       'outcome_revision_fingerprint', 'experiment_id', 'hypothesis_id',
       'decision', 'hypothesis_status', 'requested_action',
       'promotion_authority', 'asset_class', 'instrument_type',
       'asset_scope', 'product_filter', 'instrument_count',
       'instrument_set_fingerprint',
       'session_count', 'session_set_fingerprint', 'rung_plan_fingerprint',
       'confirmation_evidence_fingerprint', 'request_payload'
     ]
     and reproduction_contract - array[
       'qa_handoff_id', 'forward_confirmation_id', 'report_revision_id',
       'report_fingerprint', 'outcome_revision_id',
       'outcome_revision_fingerprint', 'experiment_id', 'hypothesis_id',
       'decision', 'hypothesis_status', 'requested_action',
       'promotion_authority', 'asset_class', 'instrument_type',
       'asset_scope', 'product_filter', 'instrument_count',
       'instrument_set_fingerprint',
       'session_count', 'session_set_fingerprint', 'rung_plan_fingerprint',
       'confirmation_evidence_fingerprint', 'request_payload'
     ] = '{}'::jsonb
     and reproduction_contract->>'decision' = 'PASS'
     and reproduction_contract->>'hypothesis_status' = 'SUPPORTED'
     and reproduction_contract->>'asset_class' = 'EQUITY'
     and reproduction_contract->>'instrument_type' = 'STOCK'
     and reproduction_contract->>'asset_scope' =
       'KRX_ACTIVE_STOCK_ONLY'
     and reproduction_contract->>'product_filter' =
       'REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY'
     and reproduction_contract->>'requested_action' =
       'INDEPENDENT_QA_REPRODUCTION'
     and reproduction_contract->'promotion_authority' = 'false'::jsonb
     and reproduction_contract->>'qa_handoff_id' = qa_handoff_id::text
     and reproduction_contract->>'qa_handoff_id' ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and reproduction_contract->>'forward_confirmation_id' ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and reproduction_contract->>'report_revision_id' ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and reproduction_contract->>'outcome_revision_id' ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and reproduction_contract->>'experiment_id' ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and reproduction_contract->>'hypothesis_id' ~
       '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
     and reproduction_contract->>'report_fingerprint' ~ '^[0-9a-f]{64}$'
     and reproduction_contract->>'outcome_revision_fingerprint' ~
       '^[0-9a-f]{64}$'
     and reproduction_contract->>'instrument_set_fingerprint' ~
       '^[0-9a-f]{64}$'
     and reproduction_contract->>'session_set_fingerprint' ~
       '^[0-9a-f]{64}$'
     and reproduction_contract->>'rung_plan_fingerprint' ~
       '^[0-9a-f]{64}$'
     and reproduction_contract->>'confirmation_evidence_fingerprint' ~
       '^[0-9a-f]{64}$'
     and jsonb_typeof(reproduction_contract->'instrument_count') = 'number'
     and reproduction_contract->>'instrument_count' ~ '^[1-9][0-9]*$'
     and (reproduction_contract->>'instrument_count')::integer > 0
     and jsonb_typeof(reproduction_contract->'session_count') = 'number'
     and reproduction_contract->>'session_count' ~ '^[1-9][0-9]*$'
     and (reproduction_contract->>'session_count')::integer >= 20
     and jsonb_typeof(reproduction_contract->'request_payload') = 'object'
     and payload_ref->>'artifact_id' =
       reproduction_contract->>'report_revision_id'
     and payload_ref->>'content_hash' = (
       'sha256:' || (reproduction_contract->>'report_fingerprint')
     )),
  constraint chk_intraday_forward_qa_outbox_payload check
    (jsonb_typeof(event_payload) = 'object'
     and event_payload ?& array[
       'message_id', 'envelope', 'reproduction_contract'
     ]
     and event_payload - array[
       'message_id', 'envelope', 'reproduction_contract'
     ] = '{}'::jsonb
     and jsonb_typeof(event_payload->'envelope') = 'object'
     and event_payload->'envelope' ?& array[
       'event_id', 'event_type', 'schema_version', 'case_id', 'trace_id',
       'producer', 'occurred_at', 'idempotency_key', 'payload_ref'
     ]
     and (event_payload->'envelope') - array[
       'event_id', 'event_type', 'schema_version', 'case_id', 'trace_id',
       'producer', 'occurred_at', 'idempotency_key', 'payload_ref'
     ] = '{}'::jsonb
     and event_payload->>'message_id' = message_id
     and event_payload->'envelope'->>'event_id' = event_id::text
     and event_payload->'envelope'->>'event_type' = event_type
     and event_payload->'envelope'->>'schema_version' = schema_version
     and event_payload->'envelope'->>'trace_id' = trace_id::text
     and event_payload->'envelope'->>'producer' = producer
     and event_payload->'envelope'->'case_id' is not distinct from
       coalesce(to_jsonb(case_id::text), 'null'::jsonb)
     and event_payload->'envelope'->'occurred_at' = to_jsonb(occurred_at)
     and event_payload->'envelope'->>'idempotency_key' = idempotency_key
     and event_payload->'envelope'->'payload_ref' = payload_ref
     and event_payload->'reproduction_contract' = reproduction_contract),
  constraint chk_intraday_forward_qa_outbox_fingerprint check
    (payload_fingerprint ~ '^[0-9a-f]{64}$'
     and payload_fingerprint = encode(
       extensions.digest(
         convert_to(event_payload::text, 'UTF8'), 'sha256'
       ), 'hex'
     )),
  constraint chk_intraday_forward_qa_outbox_text check
    (btrim(message_id) <> '' and btrim(idempotency_key) <> '')
);

-- Mutable relay state is deliberately separate from the immutable outbox.
-- FAILED rows retry with backoff; poison/unrecoverable rows retain a DLQ fact.
create table quant.intraday_forward_qa_delivery_state (
  outbox_id          bigint primary key
    references quant.intraday_forward_qa_outbox(outbox_id)
    on delete restrict,
  status             text not null default 'PENDING',
  attempt_count      integer not null default 0,
  max_attempts       integer not null default 5,
  available_at       timestamptz,
  last_error         text,
  sent_at            timestamptz,
  updated_at         timestamptz not null default now(),
  constraint chk_intraday_forward_qa_delivery_status check
    (status in ('PENDING', 'FAILED', 'SENT', 'DLQ')),
  constraint chk_intraday_forward_qa_delivery_attempts check
    (attempt_count >= 0 and max_attempts between 1 and 100
     and attempt_count <= max_attempts),
  constraint chk_intraday_forward_qa_delivery_schedule check (
    (status in ('PENDING', 'FAILED') and available_at is not null
      and sent_at is null)
    or (status = 'SENT' and available_at is null and sent_at is not null)
    or (status = 'DLQ' and available_at is null and sent_at is null
      and last_error is not null and btrim(last_error) <> '')
  )
);

create index idx_intraday_forward_qa_delivery_due
  on quant.intraday_forward_qa_delivery_state
    (available_at, outbox_id)
  where status in ('PENDING', 'FAILED');

-- One immutable successful transport receipt per logical handoff. A crash
-- after XADD and before this insert may publish twice, but event_id/message_id
-- remain identical and QA acceptance is exact-content idempotent.
create table quant.intraday_forward_qa_dispatches (
  event_id               uuid primary key,
  outbox_id               bigint not null unique
    references quant.intraday_forward_qa_outbox(outbox_id)
    on delete restrict,
  qa_handoff_id           uuid not null
    references quant.intraday_forward_qa_handoffs(qa_handoff_id)
    on delete restrict,
  message_id              text not null unique,
  event_type              text not null,
  source_department       text not null,
  trace_id                uuid not null,
  transport_stream        text not null,
  transport_message_id    text not null,
  payload                 jsonb not null,
  payload_fingerprint     text not null,
  dispatched_by           text not null,
  dispatched_at           timestamptz not null default now(),
  constraint uq_intraday_forward_qa_dispatch_handoff
    unique (qa_handoff_id),
  constraint uq_intraday_forward_qa_dispatch_transport
    unique (transport_stream, transport_message_id),
  constraint chk_intraday_forward_qa_dispatch_event check
    (event_type = 'quant.intraday.forward.qa_requested.v1'
     and source_department = 'quant-backtest-department'),
  constraint chk_intraday_forward_qa_dispatch_payload check
    (jsonb_typeof(payload) = 'object' and payload <> '{}'::jsonb
     and payload_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_forward_qa_dispatch_text check
    (btrim(transport_stream) <> ''
     and btrim(transport_message_id) <> ''
     and btrim(dispatched_by) <> '')
);

-- QA acceptance facts and the separate, durable long-running reproduction
-- queue. Acceptance never executes a backtest and grants no promotion.
create table audit.intraday_forward_reproduction_requests (
  reproduction_request_id       uuid primary key,
  event_id                      uuid not null unique
    references audit.domain_events(event_id) on delete restrict,
  outbox_id                     bigint not null unique
    references quant.intraday_forward_qa_outbox(outbox_id)
    on delete restrict,
  qa_handoff_id                 uuid not null unique
    references quant.intraday_forward_qa_handoffs(qa_handoff_id)
    on delete restrict,
  forward_confirmation_id       uuid not null,
  report_revision_id            uuid not null,
  experiment_id                 uuid not null,
  hypothesis_id                 uuid not null,
  decision                      text not null,
  hypothesis_status             text not null,
  asset_class                   text not null,
  instrument_type               text not null,
  instrument_count              integer not null,
  instrument_set_fingerprint    text not null,
  session_count                 integer not null,
  session_set_fingerprint       text not null,
  payload_ref                   jsonb not null,
  reproduction_contract        jsonb not null,
  event_payload                 jsonb not null,
  payload_fingerprint           text not null,
  requested_at                  timestamptz not null,
  accepted_by                   text not null,
  accepted_at                   timestamptz not null default now(),
  constraint chk_intraday_forward_reproduction_identity check
    (reproduction_request_id = event_id),
  constraint chk_intraday_forward_reproduction_scope check
    (decision = 'PASS' and hypothesis_status = 'SUPPORTED'
     and asset_class = 'EQUITY' and instrument_type = 'STOCK'
     and instrument_count > 0 and session_count >= 20),
  constraint chk_intraday_forward_reproduction_hashes check
    (instrument_set_fingerprint ~ '^[0-9a-f]{64}$'
     and session_set_fingerprint ~ '^[0-9a-f]{64}$'
     and payload_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_forward_reproduction_json check
    (jsonb_typeof(payload_ref) = 'object'
     and jsonb_typeof(reproduction_contract) = 'object'
     and jsonb_typeof(event_payload) = 'object'),
  constraint chk_intraday_forward_reproduction_actor check
    (btrim(accepted_by) <> '')
);

create table audit.intraday_forward_reproduction_work_items (
  work_item_id             uuid primary key default gen_random_uuid(),
  reproduction_request_id  uuid not null unique
    references audit.intraday_forward_reproduction_requests(
      reproduction_request_id) on delete restrict,
  status                   text not null default 'READY',
  next_attempt_at          timestamptz default now(),
  attempt_count            integer not null default 0,
  max_attempts             integer not null default 5,
  leased_at                timestamptz,
  lease_expires_at         timestamptz,
  leased_by                text,
  lease_token              uuid,
  last_error               text,
  created_at               timestamptz not null default now(),
  updated_at               timestamptz not null default now(),
  constraint chk_intraday_forward_reproduction_work_status check
    (status in ('READY', 'LEASED', 'RETRY', 'COMPLETED', 'FAILED')),
  constraint chk_intraday_forward_reproduction_work_attempts check
    (attempt_count >= 0 and max_attempts between 1 and 100
     and attempt_count <= max_attempts),
  constraint chk_intraday_forward_reproduction_work_lease check (
    (status = 'LEASED' and leased_at is not null
      and lease_expires_at is not null and leased_by is not null
      and lease_token is not null and lease_expires_at > leased_at)
    or (status <> 'LEASED' and leased_at is null
      and lease_expires_at is null and leased_by is null
      and lease_token is null)
  ),
  constraint chk_intraday_forward_reproduction_work_schedule check (
    (status in ('READY', 'RETRY') and next_attempt_at is not null)
    or (status in ('LEASED', 'COMPLETED', 'FAILED')
      and next_attempt_at is null)
  )
);

create index idx_intraday_forward_reproduction_work_due
  on audit.intraday_forward_reproduction_work_items
    (next_attempt_at, created_at, work_item_id)
  where status in ('READY', 'RETRY');

-- Validate the full scientific identity once and build the canonical event.
-- SECURITY DEFINER lets svc_quant's handoff trigger append the outbox without
-- granting that role arbitrary direct INSERT access to transport tables.
create or replace function quant.enqueue_intraday_forward_qa_handoff(
  p_qa_handoff_id uuid
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, quant, research, reference, governance
as $$
declare
  expected record;
  existing record;
  stock_count integer;
  stock_session_count bigint;
  v_event_id uuid;
  v_message_id text;
  v_idempotency_key text;
  v_payload_ref jsonb;
  v_contract jsonb;
  v_envelope jsonb;
  v_payload jsonb;
  v_payload_fingerprint text;
  v_outbox_id bigint;
begin
  select handoff.qa_handoff_id,
         handoff.forward_confirmation_id,
         handoff.report_revision_id,
         handoff.experiment_id,
         handoff.requested_at,
         handoff.request_payload,
         report.report_fingerprint,
         report.outcome_revision_id,
         report.decision,
         report.hypothesis_status,
         outcome.outcome_revision_fingerprint,
         confirmation.decision as confirmation_decision,
         confirmation.confirmation_evidence_fingerprint,
         confirmation.gate_statistics,
         rung.planned_instrument_ids,
         rung.planned_session_dates,
         rung.planned_instrument_count,
         rung.instrument_set_fingerprint,
         rung.planned_session_count,
         rung.session_set_fingerprint,
         rung.rung_plan_fingerprint,
         experiment.trace_id,
         experiment.hypothesis_id,
         hypothesis.case_id
    into expected
    from quant.intraday_forward_qa_handoffs handoff
    join quant.intraday_forward_report_revisions report
      on report.report_revision_id = handoff.report_revision_id
     and report.forward_confirmation_id = handoff.forward_confirmation_id
     and report.experiment_id = handoff.experiment_id
    join research.experiment_outcome_revisions outcome
      on outcome.outcome_revision_id = report.outcome_revision_id
     and outcome.forward_confirmation_id = handoff.forward_confirmation_id
     and outcome.experiment_id = handoff.experiment_id
    join quant.intraday_forward_confirmations confirmation
      on confirmation.forward_confirmation_id = handoff.forward_confirmation_id
    join quant.intraday_experiment_rungs rung
      on rung.experiment_rung_id = confirmation.experiment_rung_id
     and rung.experiment_id = handoff.experiment_id
     and rung.rung = 'FORWARD'
    join quant.experiments experiment
      on experiment.experiment_id = handoff.experiment_id
    join quant.hypotheses hypothesis
      on hypothesis.hypothesis_id = experiment.hypothesis_id
   where handoff.qa_handoff_id = p_qa_handoff_id
   for share of handoff, report, outcome, confirmation, rung,
                experiment, hypothesis;

  if not found then
    raise exception 'forward QA outbox lacks a complete authoritative handoff';
  end if;
  if expected.decision <> 'PASS'
     or expected.hypothesis_status <> 'SUPPORTED'
     or expected.confirmation_decision <> 'PASS' then
    raise exception 'forward QA outbox requires PASS/SUPPORTED evidence';
  end if;

  select count(*)
    into stock_count
    from unnest(expected.planned_instrument_ids) planned(instrument_id)
    join reference.instruments instrument
      on instrument.instrument_id = planned.instrument_id
     and upper(instrument.instrument_type) = 'STOCK'
     and upper(instrument.asset_class) = 'EQUITY'
     and upper(instrument.market) = 'KRX'
     and upper(instrument.status) = 'ACTIVE';
  select count(*)
    into stock_session_count
    from unnest(expected.planned_instrument_ids) planned(instrument_id)
    cross join unnest(expected.planned_session_dates) session(session_date)
    join reference.instruments instrument
      on instrument.instrument_id = planned.instrument_id
     and upper(instrument.instrument_type) = 'STOCK'
     and upper(instrument.asset_class) = 'EQUITY'
     and upper(instrument.market) = 'KRX'
     and upper(instrument.status) = 'ACTIVE'
     and (instrument.listed_from is null
          or instrument.listed_from <= session.session_date)
     and (instrument.listed_to is null
          or instrument.listed_to >= session.session_date);
  if stock_count <> expected.planned_instrument_count
     or expected.planned_instrument_count
          <> cardinality(expected.planned_instrument_ids)
     or expected.planned_session_count
          <> cardinality(expected.planned_session_dates)
     or stock_session_count <>
          expected.planned_instrument_count::bigint *
          expected.planned_session_count::bigint then
    raise exception
      'forward QA outbox contains a non-ACTIVE-KRX-EQUITY/STOCK universe';
  end if;
  if expected.gate_statistics->>'stock_universe_fingerprint'
       is distinct from expected.instrument_set_fingerprint
     or (expected.gate_statistics->>'stock_universe_count')::integer
       is distinct from expected.planned_instrument_count
     or expected.gate_statistics->>'product_filter'
       is distinct from 'REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY' then
    raise exception 'forward QA outbox universe fingerprint is inconsistent';
  end if;

  v_event_id := quant.intraday_forward_qa_event_id(expected.qa_handoff_id);
  v_message_id :=
    'quant.intraday.forward.qa_requested.v1:' || expected.qa_handoff_id::text;
  v_idempotency_key :=
    'quant:intraday-forward:qa-requested:v1:' ||
    expected.forward_confirmation_id::text;
  v_payload_ref := jsonb_build_object(
    'artifact_type', 'INTRADAY_FORWARD_REPORT_REVISION',
    'artifact_id', expected.report_revision_id::text,
    'artifact_schema', 'intraday-forward-report-revision-v1',
    'content_hash', 'sha256:' || expected.report_fingerprint
  );
  v_contract := jsonb_build_object(
    'qa_handoff_id', expected.qa_handoff_id::text,
    'forward_confirmation_id', expected.forward_confirmation_id::text,
    'report_revision_id', expected.report_revision_id::text,
    'report_fingerprint', expected.report_fingerprint,
    'outcome_revision_id', expected.outcome_revision_id::text,
    'outcome_revision_fingerprint',
      expected.outcome_revision_fingerprint,
    'experiment_id', expected.experiment_id::text,
    'hypothesis_id', expected.hypothesis_id::text,
    'decision', 'PASS',
    'hypothesis_status', 'SUPPORTED',
    'requested_action', 'INDEPENDENT_QA_REPRODUCTION',
    'promotion_authority', false,
    'asset_class', 'EQUITY',
    'instrument_type', 'STOCK',
    'asset_scope', 'KRX_ACTIVE_STOCK_ONLY',
    'product_filter', 'REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY',
    'instrument_count', expected.planned_instrument_count,
    'instrument_set_fingerprint', expected.instrument_set_fingerprint,
    'session_count', expected.planned_session_count,
    'session_set_fingerprint', expected.session_set_fingerprint,
    'rung_plan_fingerprint', expected.rung_plan_fingerprint,
    'confirmation_evidence_fingerprint',
      expected.confirmation_evidence_fingerprint,
    'request_payload', expected.request_payload
  );
  v_envelope := jsonb_build_object(
    'event_id', v_event_id::text,
    'event_type', 'quant.intraday.forward.qa_requested.v1',
    'schema_version', 'event-envelope-v1',
    'case_id', case when expected.case_id is null then null
                    else to_jsonb(expected.case_id::text) end,
    'trace_id', expected.trace_id::text,
    'producer', 'quant-backtest-department',
    'occurred_at', to_jsonb(expected.requested_at),
    'idempotency_key', v_idempotency_key,
    'payload_ref', v_payload_ref
  );
  v_payload := jsonb_build_object(
    'message_id', v_message_id,
    'envelope', v_envelope,
    'reproduction_contract', v_contract
  );
  v_payload_fingerprint := encode(
    extensions.digest(convert_to(v_payload::text, 'UTF8'), 'sha256'), 'hex'
  );

  insert into quant.intraday_forward_qa_outbox (
    event_id, qa_handoff_id, message_id, event_type, schema_version,
    case_id, trace_id, producer, occurred_at, idempotency_key,
    payload_ref, reproduction_contract, event_payload, payload_fingerprint
  ) values (
    v_event_id, expected.qa_handoff_id, v_message_id,
    'quant.intraday.forward.qa_requested.v1', 'event-envelope-v1',
    expected.case_id, expected.trace_id, 'quant-backtest-department',
    expected.requested_at, v_idempotency_key, v_payload_ref, v_contract,
    v_payload, v_payload_fingerprint
  )
  on conflict (qa_handoff_id) do nothing
  returning outbox_id into v_outbox_id;

  if v_outbox_id is null then
    select * into existing
      from quant.intraday_forward_qa_outbox outbox
     where outbox.qa_handoff_id = expected.qa_handoff_id;
    if existing.event_id is distinct from v_event_id
       or existing.message_id is distinct from v_message_id
       or existing.event_type is distinct from
          'quant.intraday.forward.qa_requested.v1'
       or existing.schema_version is distinct from 'event-envelope-v1'
       or existing.case_id is distinct from expected.case_id
       or existing.trace_id is distinct from expected.trace_id
       or existing.producer is distinct from 'quant-backtest-department'
       or existing.occurred_at is distinct from expected.requested_at
       or existing.idempotency_key is distinct from v_idempotency_key
       or existing.payload_ref is distinct from v_payload_ref
       or existing.reproduction_contract is distinct from v_contract
       or existing.event_payload is distinct from v_payload
       or existing.payload_fingerprint is distinct from
          v_payload_fingerprint then
      raise exception 'forward QA outbox idempotency key changed content';
    end if;
    v_outbox_id := existing.outbox_id;
  end if;

  insert into quant.intraday_forward_qa_delivery_state (
    outbox_id, status, available_at
  ) values (v_outbox_id, 'PENDING', now())
  on conflict (outbox_id) do nothing;
  return v_event_id;
end
$$;

create or replace function quant.enqueue_intraday_forward_qa_handoff_trigger()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, quant
as $$
begin
  perform quant.enqueue_intraday_forward_qa_handoff(new.qa_handoff_id);
  return new;
end
$$;

create trigger intraday_forward_qa_handoff_transactional_outbox
after insert on quant.intraday_forward_qa_handoffs
for each row execute function
  quant.enqueue_intraday_forward_qa_handoff_trigger();

-- Backfill every PASS handoff that predates the trigger while preserving the
-- original requested_at as event occurred_at.
do $forward_qa_backfill$
declare
  handoff_row record;
begin
  for handoff_row in
    select handoff.qa_handoff_id
      from quant.intraday_forward_qa_handoffs handoff
      left join quant.intraday_forward_qa_outbox outbox
        on outbox.qa_handoff_id = handoff.qa_handoff_id
     where outbox.outbox_id is null
     order by handoff.requested_at, handoff.qa_handoff_id
  loop
    perform quant.enqueue_intraday_forward_qa_handoff(
      handoff_row.qa_handoff_id
    );
  end loop;
end
$forward_qa_backfill$;

create or replace function quant.validate_intraday_forward_qa_dispatch()
returns trigger
language plpgsql
set search_path = pg_catalog, quant
as $$
declare
  expected record;
begin
  select * into expected
    from quant.intraday_forward_qa_outbox outbox
   where outbox.outbox_id = new.outbox_id
     and outbox.event_id = new.event_id
     and outbox.qa_handoff_id = new.qa_handoff_id;
  if not found then
    raise exception 'QA dispatch lacks its immutable outbox event';
  end if;
  if new.message_id is distinct from expected.message_id
     or new.event_type is distinct from expected.event_type
     or new.source_department is distinct from expected.producer
     or new.trace_id is distinct from expected.trace_id
     or new.payload is distinct from expected.event_payload
     or new.payload_fingerprint is distinct from
        expected.payload_fingerprint then
    raise exception 'QA dispatch conflicts with immutable outbox content';
  end if;
  return new;
end
$$;

create trigger intraday_forward_qa_dispatch_semantic_guard
before insert on quant.intraday_forward_qa_dispatches
for each row execute function quant.validate_intraday_forward_qa_dispatch();

create or replace function audit.validate_intraday_forward_reproduction_request()
returns trigger
language plpgsql
set search_path = pg_catalog, audit, quant
as $$
declare
  expected record;
begin
  select outbox.*,
         domain.event_type as domain_event_type,
         domain.source_department as domain_source_department,
         domain.trace_id as domain_trace_id,
         domain.payload as domain_payload,
         domain.occurred_at as domain_occurred_at,
         domain.status as domain_status
    into expected
    from quant.intraday_forward_qa_outbox outbox
    join audit.domain_events domain on domain.event_id = outbox.event_id
   where outbox.outbox_id = new.outbox_id
     and outbox.event_id = new.event_id
     and outbox.qa_handoff_id = new.qa_handoff_id;
  if not found then
    raise exception 'QA reproduction request lacks accepted outbox event';
  end if;
  if expected.domain_event_type is distinct from expected.event_type
     or expected.domain_status is distinct from 'PROCESSED'
     or expected.domain_source_department is distinct from expected.producer
     or expected.domain_trace_id is distinct from expected.trace_id
     or expected.domain_payload is distinct from new.event_payload
     or expected.domain_payload is distinct from expected.event_payload
     or expected.domain_occurred_at is distinct from expected.occurred_at
     or new.payload_ref is distinct from expected.payload_ref
     or new.reproduction_contract is distinct from
        expected.reproduction_contract
     or new.payload_fingerprint is distinct from
        expected.payload_fingerprint
     or new.forward_confirmation_id::text is distinct from
        expected.reproduction_contract->>'forward_confirmation_id'
     or new.report_revision_id::text is distinct from
        expected.reproduction_contract->>'report_revision_id'
     or new.experiment_id::text is distinct from
        expected.reproduction_contract->>'experiment_id'
     or new.hypothesis_id::text is distinct from
        expected.reproduction_contract->>'hypothesis_id'
     or new.instrument_count is distinct from
        (expected.reproduction_contract->>'instrument_count')::integer
     or new.instrument_set_fingerprint is distinct from
        expected.reproduction_contract->>'instrument_set_fingerprint'
     or new.session_count is distinct from
        (expected.reproduction_contract->>'session_count')::integer
     or new.session_set_fingerprint is distinct from
        expected.reproduction_contract->>'session_set_fingerprint'
     or new.requested_at is distinct from expected.occurred_at then
    raise exception 'QA reproduction request changed canonical event content';
  end if;
  return new;
end
$$;

create trigger intraday_forward_reproduction_request_semantic_guard
before insert on audit.intraday_forward_reproduction_requests
for each row execute function
  audit.validate_intraday_forward_reproduction_request();

create or replace function quant.validate_intraday_forward_qa_delivery_state()
returns trigger
language plpgsql
set search_path = pg_catalog, quant
as $$
begin
  if new.status = 'SENT' and not exists (
    select 1
      from quant.intraday_forward_qa_dispatches dispatch
     where dispatch.outbox_id = new.outbox_id
  ) then
    raise exception
      'QA delivery cannot become SENT without an immutable dispatch receipt';
  end if;
  return new;
end
$$;

create trigger intraday_forward_qa_delivery_sent_guard
before update on quant.intraday_forward_qa_delivery_state
for each row execute function
  quant.validate_intraday_forward_qa_delivery_state();

create or replace function audit.reject_intraday_forward_domain_event_change()
returns trigger
language plpgsql
set search_path = pg_catalog, audit
as $$
begin
  if old.event_type = 'quant.intraday.forward.qa_requested.v1'
     or (tg_op = 'UPDATE'
         and new.event_type = 'quant.intraday.forward.qa_requested.v1') then
    raise exception 'accepted intraday forward QA domain events are immutable';
  end if;
  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

create trigger intraday_forward_qa_domain_event_append_only
before update or delete on audit.domain_events
for each row execute function
  audit.reject_intraday_forward_domain_event_change();

create trigger intraday_forward_qa_outbox_append_only
before update or delete on quant.intraday_forward_qa_outbox
for each row execute function governance.reject_append_only_change();
create trigger intraday_forward_qa_dispatches_append_only
before update or delete on quant.intraday_forward_qa_dispatches
for each row execute function governance.reject_append_only_change();
create trigger intraday_forward_reproduction_requests_append_only
before update or delete on audit.intraday_forward_reproduction_requests
for each row execute function governance.reject_append_only_change();
create trigger intraday_forward_qa_delivery_state_touch_updated_at
before update on quant.intraday_forward_qa_delivery_state
for each row execute function governance.touch_updated_at();
create trigger intraday_forward_reproduction_work_touch_updated_at
before update on audit.intraday_forward_reproduction_work_items
for each row execute function governance.touch_updated_at();

alter table quant.intraday_forward_qa_outbox enable row level security;
alter table quant.intraday_forward_qa_delivery_state enable row level security;
alter table quant.intraday_forward_qa_dispatches enable row level security;
alter table audit.intraday_forward_reproduction_requests
  enable row level security;
alter table audit.intraday_forward_reproduction_work_items
  enable row level security;

create policy intraday_forward_qa_outbox_svc_quant_select
  on quant.intraday_forward_qa_outbox for select to svc_quant using (true);
create policy intraday_forward_qa_delivery_state_svc_quant_select
  on quant.intraday_forward_qa_delivery_state for select to svc_quant
  using (true);
create policy intraday_forward_qa_dispatches_svc_quant_select
  on quant.intraday_forward_qa_dispatches for select to svc_quant using (true);

revoke all on function quant.intraday_forward_qa_event_id(uuid) from public;
revoke all on function quant.validate_intraday_stock_rung_scope() from public;
revoke all on function quant.enqueue_intraday_forward_qa_handoff(uuid)
  from public;
revoke all on function quant.enqueue_intraday_forward_qa_handoff_trigger()
  from public;
revoke all on function quant.validate_intraday_forward_qa_dispatch()
  from public;
revoke all on function audit.validate_intraday_forward_reproduction_request()
  from public;
revoke all on function quant.validate_intraday_forward_qa_delivery_state()
  from public;
revoke all on function audit.reject_intraday_forward_domain_event_change()
  from public;
grant execute on function quant.intraday_forward_qa_event_id(uuid)
  to svc_quant, service_role;
grant execute on function quant.validate_intraday_stock_rung_scope()
  to svc_quant, service_role;
grant execute on function quant.enqueue_intraday_forward_qa_handoff(uuid)
  to svc_quant, service_role;
grant execute on function quant.enqueue_intraday_forward_qa_handoff_trigger()
  to svc_quant, service_role;
grant execute on function quant.validate_intraday_forward_qa_dispatch()
  to service_role;
grant execute on function audit.validate_intraday_forward_reproduction_request()
  to service_role;
grant execute on function quant.validate_intraday_forward_qa_delivery_state()
  to service_role;
grant execute on function audit.reject_intraday_forward_domain_event_change()
  to service_role;

grant usage, select on sequence quant.intraday_forward_qa_outbox_outbox_id_seq
  to service_role;
grant usage on schema audit to service_role;
grant select on
  quant.intraday_forward_qa_outbox,
  quant.intraday_forward_qa_delivery_state,
  quant.intraday_forward_qa_dispatches
to svc_quant;
grant select on quant.intraday_forward_qa_handoffs,
  quant.intraday_forward_qa_outbox,
  quant.intraday_forward_report_revisions,
  quant.intraday_forward_confirmations,
  quant.intraday_experiment_rungs,
  quant.experiments,
  quant.hypotheses
to service_role;
grant select, insert on audit.domain_events to service_role;
grant select, insert on quant.intraday_forward_qa_dispatches
  to service_role;
grant select, insert, update on quant.intraday_forward_qa_delivery_state
  to service_role;
grant select, insert on audit.intraday_forward_reproduction_requests
  to service_role;
grant select, insert, update on audit.intraday_forward_reproduction_work_items
  to service_role;
revoke update, delete, truncate on
  quant.intraday_forward_qa_outbox,
  quant.intraday_forward_qa_dispatches,
  audit.intraday_forward_reproduction_requests
from svc_quant, service_role;
revoke delete, truncate on
  quant.intraday_forward_qa_delivery_state,
  audit.intraday_forward_reproduction_work_items
from svc_quant, service_role;

comment on table quant.intraday_forward_qa_outbox is
  'Immutable transactional outbox appended with the PASS QA handoff. The event references, rather than copies, the authoritative report.';
comment on table quant.intraday_forward_qa_delivery_state is
  'Mutable relay retry/backoff state; DLQ retains poison-event failure evidence.';
comment on table quant.intraday_forward_qa_dispatches is
  'Immutable successful transport receipt. Logical identity is deterministic; the Redis Stream ID identifies one delivery attempt.';
comment on table audit.intraday_forward_reproduction_requests is
  'Immutable exact-content QA acceptance for an EQUITY/STOCK forward PASS. It conveys no promotion authority.';
comment on table audit.intraday_forward_reproduction_work_items is
  'Durable queue for later independent reproduction; QA event acceptance never runs the long backtest inline.';
comment on policy reference_instrument_symbols_svc_quant_stock_only_select
  on reference.instrument_symbols is
  'Read-only symbol identity for the same fail-closed ACTIVE KRX EQUITY/STOCK universe visible through reference.instruments.';
comment on policy reference_instruments_svc_quant_stock_only_select
  on reference.instruments is
  'Fail-closed ACTIVE KRX EQUITY/STOCK universe boundary for svc_quant; every product, venue, and lifecycle predicate is enforced by RLS.';

commit;
