begin;

-- Independent forward reproduction is a scientific workload, not part of the
-- Redis transport worker.  The pool login must explicitly reduce to this role
-- before it can claim or complete work.
do $qa_reproducer_role$
begin
  if not exists (
    select 1 from pg_roles where rolname = 'svc_qa_reproducer'
  ) then
    create role svc_qa_reproducer
      nologin nosuperuser nocreatedb nocreaterole noinherit
      noreplication nobypassrls;
  elsif exists (
    select 1
      from pg_roles
     where rolname = 'svc_qa_reproducer'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_qa_reproducer role name is occupied by an unsafe role';
  end if;

  if exists (
    select 1
      from pg_auth_members membership
      join pg_roles member_role on member_role.oid = membership.member
     where member_role.rolname = 'svc_qa_reproducer'
  ) then
    raise exception 'svc_qa_reproducer must not inherit or SET another role';
  end if;
end
$qa_reproducer_role$;

do $qa_reproducer_pool_membership$
declare
  pool_login name := session_user;
begin
  execute format(
    'grant svc_qa_reproducer to %I with set true, inherit false', pool_login
  );
end
$qa_reproducer_pool_membership$;

-- A composite key lets the immutable result prove that its request and work
-- identifiers came from the same queue row, not merely from two valid rows.
alter table audit.intraday_forward_reproduction_work_items
  add constraint uq_intraday_forward_reproduction_work_identity
  unique (work_item_id, reproduction_request_id);

-- One immutable result is permitted for each accepted reproduction request.
-- FAIL is a valid scientific verdict and therefore completes the work item;
-- audit.fail_intraday_forward_reproduction_work is reserved for infrastructure
-- failures that did not produce a verdict.
create table audit.intraday_forward_reproduction_results (
  reproduction_result_id             uuid primary key
    default gen_random_uuid(),
  reproduction_request_id            uuid not null unique
    references audit.intraday_forward_reproduction_requests(
      reproduction_request_id) on delete restrict,
  work_item_id                       uuid not null unique
    references audit.intraday_forward_reproduction_work_items(work_item_id)
    on delete restrict,
  completion_lease_token             uuid not null,
  verdict                            text not null,
  result_schema_version              text not null
    default 'intraday-forward-qa-reproduction-result-v1',
  worker_version                     text not null,
  report_revision_id                 uuid not null,
  outcome_revision_id                uuid not null,
  request_payload_fingerprint        text not null,
  report_fingerprint                 text not null,
  outcome_revision_fingerprint       text not null,
  instrument_set_fingerprint         text not null,
  session_set_fingerprint            text not null,
  rung_plan_fingerprint              text not null,
  confirmation_evidence_fingerprint  text not null,
  result_evidence                    jsonb not null,
  result_document                    jsonb not null,
  result_fingerprint                 text not null unique,
  promotion_authority                boolean not null default false,
  reproduced_by                      text not null,
  completed_at                       timestamptz not null default now(),

  constraint fk_intraday_forward_reproduction_result_work_request
    foreign key (work_item_id, reproduction_request_id)
    references audit.intraday_forward_reproduction_work_items
      (work_item_id, reproduction_request_id) on delete restrict,
  constraint chk_intraday_forward_reproduction_result_verdict check
    (verdict in ('PASS', 'FAIL', 'INCONCLUSIVE')),
  constraint chk_intraday_forward_reproduction_result_version check
    (result_schema_version =
       'intraday-forward-qa-reproduction-result-v1'
     and btrim(worker_version) <> '' and btrim(reproduced_by) <> ''),
  constraint chk_intraday_forward_reproduction_result_hashes check
    (request_payload_fingerprint ~ '^[0-9a-f]{64}$'
     and report_fingerprint ~ '^[0-9a-f]{64}$'
     and outcome_revision_fingerprint ~ '^[0-9a-f]{64}$'
     and instrument_set_fingerprint ~ '^[0-9a-f]{64}$'
     and session_set_fingerprint ~ '^[0-9a-f]{64}$'
     and rung_plan_fingerprint ~ '^[0-9a-f]{64}$'
     and confirmation_evidence_fingerprint ~ '^[0-9a-f]{64}$'
     and result_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_forward_reproduction_result_evidence check
    (jsonb_typeof(result_evidence) = 'object'
     and result_evidence <> '{}'::jsonb),
  constraint chk_intraday_forward_reproduction_result_no_promotion check
    (promotion_authority = false),
  constraint chk_intraday_forward_reproduction_result_document check (
    jsonb_typeof(result_document) = 'object'
    and result_document ?& array[
      'result_schema_version', 'work_item_id', 'reproduction_request_id',
      'verdict', 'worker_version', 'reproduced_by', 'source_fingerprints',
      'result_evidence', 'promotion_authority'
    ]
    and result_document - array[
      'result_schema_version', 'work_item_id', 'reproduction_request_id',
      'verdict', 'worker_version', 'reproduced_by', 'source_fingerprints',
      'result_evidence', 'promotion_authority'
    ] = '{}'::jsonb
    and result_document->>'result_schema_version' = result_schema_version
    and result_document->>'work_item_id' = work_item_id::text
    and result_document->>'reproduction_request_id' =
      reproduction_request_id::text
    and result_document->>'verdict' = verdict
    and result_document->>'worker_version' = worker_version
    and result_document->>'reproduced_by' = reproduced_by
    and result_document->'result_evidence' = result_evidence
    and result_document->'promotion_authority' = 'false'::jsonb
    and jsonb_typeof(result_document->'source_fingerprints') = 'object'
    and result_document->'source_fingerprints' ?& array[
      'report_revision_id', 'outcome_revision_id',
      'request_payload_fingerprint', 'report_fingerprint',
      'outcome_revision_fingerprint', 'instrument_set_fingerprint',
      'session_set_fingerprint', 'rung_plan_fingerprint',
      'confirmation_evidence_fingerprint'
    ]
    and (result_document->'source_fingerprints') - array[
      'report_revision_id', 'outcome_revision_id',
      'request_payload_fingerprint', 'report_fingerprint',
      'outcome_revision_fingerprint', 'instrument_set_fingerprint',
      'session_set_fingerprint', 'rung_plan_fingerprint',
      'confirmation_evidence_fingerprint'
    ] = '{}'::jsonb
    and result_document->'source_fingerprints'->>'report_revision_id' =
      report_revision_id::text
    and result_document->'source_fingerprints'->>'outcome_revision_id' =
      outcome_revision_id::text
    and result_document->'source_fingerprints'->>
      'request_payload_fingerprint' = request_payload_fingerprint
    and result_document->'source_fingerprints'->>'report_fingerprint' =
      report_fingerprint
    and result_document->'source_fingerprints'->>
      'outcome_revision_fingerprint' = outcome_revision_fingerprint
    and result_document->'source_fingerprints'->>
      'instrument_set_fingerprint' = instrument_set_fingerprint
    and result_document->'source_fingerprints'->>
      'session_set_fingerprint' = session_set_fingerprint
    and result_document->'source_fingerprints'->>'rung_plan_fingerprint' =
      rung_plan_fingerprint
    and result_document->'source_fingerprints'->>
      'confirmation_evidence_fingerprint' =
      confirmation_evidence_fingerprint
  ),
  constraint chk_intraday_forward_reproduction_result_fingerprint check
    (result_fingerprint = encode(
      extensions.digest(
        convert_to(result_document::text, 'UTF8'), 'sha256'
      ), 'hex'
    ))
);

create trigger intraday_forward_reproduction_results_append_only
before update or delete on audit.intraday_forward_reproduction_results
for each row execute function governance.reject_append_only_change();

alter table audit.intraday_forward_reproduction_results
  enable row level security;
alter table audit.intraday_forward_reproduction_results
  force row level security;

create policy intraday_forward_reproduction_results_reproducer_select
  on audit.intraday_forward_reproduction_results
  for select to svc_qa_reproducer using (true);
-- FORCE RLS also applies to the table owner.  The SECURITY DEFINER completion
-- path runs as the migration owner and therefore needs this policy; it does not grant any
-- table privilege to a runtime role.
do $qa_reproducer_definer_policy$
begin
  execute format(
    'create policy intraday_forward_reproduction_results_definer_all '
    'on audit.intraday_forward_reproduction_results '
    'for all to %I using (true) with check (true)',
    current_user
  );
end
$qa_reproducer_definer_policy$;

-- Claim at most one item and return every immutable input needed by the
-- independent worker.  No follow-up metadata reads are required.  Invalid or
-- incomplete identity graphs are skipped rather than partially exposed.
create or replace function audit.claim_intraday_forward_reproduction_work(
  p_worker text,
  p_lease_seconds integer default 900
)
returns jsonb
language plpgsql
security definer
set search_path = pg_catalog, audit, quant, research, reference, extensions
as $$
declare
  v_now timestamptz := clock_timestamp();
  v_input jsonb;
begin
  if p_worker is null or btrim(p_worker) = '' then
    raise exception 'QA reproducer worker identity is required';
  end if;
  if p_lease_seconds is null
     or p_lease_seconds not between 30 and 7200 then
    raise exception 'QA reproduction lease seconds must be between 30 and 7200';
  end if;

  -- An expired lease is an infrastructure failure.  Recover a bounded batch
  -- without waiting on another claimant, retaining the consumed attempt.
  with expired as (
    select work.work_item_id
      from audit.intraday_forward_reproduction_work_items work
     where work.status = 'LEASED'
       and work.lease_expires_at <= v_now
       and not exists (
         select 1
           from audit.intraday_forward_reproduction_results result
          where result.work_item_id = work.work_item_id
       )
     order by work.lease_expires_at, work.created_at, work.work_item_id
     for update of work skip locked
     limit 100
  )
  update audit.intraday_forward_reproduction_work_items work
     set status = case
           when work.attempt_count >= work.max_attempts then 'FAILED'
           else 'RETRY'
         end,
         next_attempt_at = case
           when work.attempt_count >= work.max_attempts then null
           else v_now + make_interval(secs => least(
             3600,
             30 * (1 << least(greatest(work.attempt_count - 1, 0), 7))
           ))
         end,
         leased_at = null,
         lease_expires_at = null,
         leased_by = null,
         lease_token = null,
         last_error = left(
           coalesce(nullif(work.last_error, '') || '; ', '') ||
             'lease expired before reproduction verdict',
           4000
         )
    from expired
   where work.work_item_id = expired.work_item_id;

  with candidate_work as (
    select work.work_item_id
      from audit.intraday_forward_reproduction_work_items work
      join audit.intraday_forward_reproduction_requests request
        on request.reproduction_request_id = work.reproduction_request_id
      join audit.domain_events domain_event
        on domain_event.event_id = request.event_id
      join quant.intraday_forward_qa_outbox outbox
        on outbox.outbox_id = request.outbox_id
       and outbox.event_id = request.event_id
       and outbox.qa_handoff_id = request.qa_handoff_id
      join quant.intraday_forward_qa_handoffs handoff
        on handoff.qa_handoff_id = request.qa_handoff_id
       and handoff.forward_confirmation_id = request.forward_confirmation_id
       and handoff.report_revision_id = request.report_revision_id
       and handoff.experiment_id = request.experiment_id
      join quant.intraday_forward_report_revisions report_revision
        on report_revision.report_revision_id = request.report_revision_id
       and report_revision.forward_confirmation_id =
         request.forward_confirmation_id
       and report_revision.experiment_id = request.experiment_id
      join research.experiment_outcome_revisions outcome_revision
        on outcome_revision.outcome_revision_id =
          report_revision.outcome_revision_id
       and outcome_revision.forward_confirmation_id =
          request.forward_confirmation_id
       and outcome_revision.experiment_id = request.experiment_id
      join quant.experiments experiment
        on experiment.experiment_id = request.experiment_id
       and experiment.hypothesis_id = request.hypothesis_id
      join quant.intraday_experiment_rungs forward_rung
        on forward_rung.experiment_id = experiment.experiment_id
       and forward_rung.dataset_id = experiment.dataset_id
       and forward_rung.rung = 'FORWARD'
      join quant.intraday_candidate_lineages candidate
        on candidate.candidate_lineage_id =
          forward_rung.candidate_lineage_id
       and candidate.root_lineage_id = forward_rung.root_lineage_id
       and candidate.hypothesis_id = experiment.hypothesis_id
      join quant.intraday_forward_confirmations confirmation
        on confirmation.forward_confirmation_id =
          request.forward_confirmation_id
       and confirmation.experiment_rung_id =
          forward_rung.experiment_rung_id
       and confirmation.candidate_lineage_id =
          candidate.candidate_lineage_id
       and confirmation.root_lineage_id = candidate.root_lineage_id
     where work.status in ('READY', 'RETRY')
       and work.next_attempt_at <= v_now
       and work.attempt_count < work.max_attempts
       and not exists (
         select 1
           from audit.intraday_forward_reproduction_results result
          where result.reproduction_request_id =
                request.reproduction_request_id
             or result.work_item_id = work.work_item_id
       )
       and domain_event.event_type =
         'quant.intraday.forward.qa_requested.v1'
       and domain_event.source_department = 'quant-backtest-department'
       and domain_event.status = 'PROCESSED'
       and domain_event.payload = request.event_payload
       and outbox.event_payload = request.event_payload
       and outbox.payload_fingerprint = request.payload_fingerprint
       and outbox.reproduction_contract = request.reproduction_contract
       and request.decision = 'PASS'
       and request.hypothesis_status = 'SUPPORTED'
       and request.asset_class = 'EQUITY'
       and request.instrument_type = 'STOCK'
       and report_revision.decision = 'PASS'
       and report_revision.hypothesis_status = 'SUPPORTED'
       and outcome_revision.decision = 'SUBMIT_TO_QA'
       and confirmation.decision = 'PASS'
       and candidate.cost_model_version = experiment.cost_model_version
       and jsonb_typeof(
         report_revision.report->'reproduction_runtime') = 'object'
       and report_revision.report->'reproduction_runtime'->>'version' =
         'intraday-forward-reproduction-runtime-v1'
       and jsonb_typeof(report_revision.report->'reproduction_runtime'->
         'frozen_config') = 'object'
       and report_revision.report->'reproduction_runtime'->
         'frozen_config' <> '{}'::jsonb
       and report_revision.report->'reproduction_runtime'->>
         'frozen_config_fingerprint' ~ '^[0-9a-f]{64}$'
       and coalesce(experiment.input_hash ~ '^[0-9a-f]{64}$', false)
       and coalesce(
         report_revision.report->'reproduction_runtime'->>
           'experiment_input_hash' = experiment.input_hash,
         false
       )
       and coalesce(btrim(experiment.code_version) <> '', false)
       and report_revision.report->'reproduction_runtime'->>'code_version' =
         experiment.code_version
       and report_revision.report->'reproduction_runtime'->>
         'evaluator_version' = candidate.evaluator_version
       and report_revision.report->'reproduction_runtime'->>
         'cost_model_version' = experiment.cost_model_version
       and report_revision.report->'reproduction_runtime'->>
         'runtime_manifest_fingerprint' ~ '^[0-9a-f]{64}$'
       and jsonb_typeof(report_revision.report->'reproduction_runtime'->
         'source_manifest') = 'object'
       and report_revision.report->'reproduction_runtime'->
         'source_manifest' <> '{}'::jsonb
       and report_revision.report->'reproduction_runtime'->
         'source_manifest'->>'version' =
         'intraday-forward-reproduction-source-set-v1'
       and jsonb_typeof(report_revision.report->'reproduction_runtime'->
         'source_manifest'->'files') = 'object'
       and report_revision.report->'reproduction_runtime'->
         'source_manifest'->'files' <> '{}'::jsonb
       and report_revision.report->'reproduction_runtime'->
         'source_manifest'->>'source_fingerprint' ~ '^[0-9a-f]{64}$'
       and request.reproduction_contract->>'experiment_id' =
         experiment.experiment_id::text
       and request.reproduction_contract->>'hypothesis_id' =
         experiment.hypothesis_id::text
       and request.reproduction_contract->>'forward_confirmation_id' =
         confirmation.forward_confirmation_id::text
       and request.reproduction_contract->>'report_revision_id' =
         report_revision.report_revision_id::text
       and request.reproduction_contract->>'report_fingerprint' =
         report_revision.report_fingerprint
       and request.reproduction_contract->>'outcome_revision_id' =
         outcome_revision.outcome_revision_id::text
       and request.reproduction_contract->>'outcome_revision_fingerprint' =
         outcome_revision.outcome_revision_fingerprint
       and request.reproduction_contract->>'instrument_count' =
         forward_rung.planned_instrument_count::text
       and request.reproduction_contract->>'instrument_set_fingerprint' =
         forward_rung.instrument_set_fingerprint
       and request.reproduction_contract->>'session_count' =
         forward_rung.planned_session_count::text
       and request.reproduction_contract->>'session_set_fingerprint' =
         forward_rung.session_set_fingerprint
       and request.reproduction_contract->>'rung_plan_fingerprint' =
         forward_rung.rung_plan_fingerprint
       and request.reproduction_contract->>
         'confirmation_evidence_fingerprint' =
         confirmation.confirmation_evidence_fingerprint
       and request.payload_ref->>'artifact_id' =
         report_revision.report_revision_id::text
       and request.payload_ref->>'content_hash' =
         'sha256:' || report_revision.report_fingerprint
       and cardinality(forward_rung.planned_session_dates) =
         forward_rung.planned_session_count
       and cardinality(forward_rung.planned_instrument_ids) =
         forward_rung.planned_instrument_count
       and forward_rung.planned_session_count >= 20
       and forward_rung.planned_instrument_count > 0
       and not exists (
         select 1
           from unnest(forward_rung.planned_session_dates)
             planned_session(session_date)
           cross join unnest(forward_rung.planned_instrument_ids)
             planned_instrument(instrument_id)
           left join reference.instruments instrument
             on instrument.instrument_id = planned_instrument.instrument_id
          where instrument.instrument_id is null
             or coalesce(upper(instrument.instrument_type), '') <> 'STOCK'
             or coalesce(upper(instrument.asset_class), '') <> 'EQUITY'
             or coalesce(upper(instrument.market), '') <> 'KRX'
             or coalesce(upper(instrument.status), '') <> 'ACTIVE'
             or (instrument.listed_from is not null
                 and instrument.listed_from > planned_session.session_date)
             or (instrument.listed_to is not null
                 and instrument.listed_to < planned_session.session_date)
       )
       and (
         select count(*)
           from quant.intraday_session_exposures exposure
          where exposure.experiment_rung_id =
                forward_rung.experiment_rung_id
            and exposure.candidate_lineage_id =
                forward_rung.candidate_lineage_id
            and exposure.root_lineage_id = forward_rung.root_lineage_id
            and exposure.dataset_id = forward_rung.dataset_id
            and exposure.exposure_purpose = 'FORWARD_CONFIRMATION'
            and exposure.knowledge_clock_mode = 'ARRIVAL_TIME_CAUSAL'
            and exposure.session_date =
                any(forward_rung.planned_session_dates)
            and exposure.instrument_ids =
                forward_rung.planned_instrument_ids
            and exposure.instrument_count =
                forward_rung.planned_instrument_count
            and exposure.instrument_set_fingerprint =
                forward_rung.instrument_set_fingerprint
            and exposure.session_content_fingerprint ~ '^[0-9a-f]{64}$'
            and exposure.exposure_evidence_fingerprint ~ '^[0-9a-f]{64}$'
            and exposure.quote_row_count > 0
            and exposure.trade_row_count > 0
       ) = forward_rung.planned_session_count
       and (
         select array_agg(exposure.session_date order by exposure.session_date)
           from quant.intraday_session_exposures exposure
          where exposure.experiment_rung_id =
                forward_rung.experiment_rung_id
            and exposure.candidate_lineage_id =
                forward_rung.candidate_lineage_id
            and exposure.root_lineage_id = forward_rung.root_lineage_id
            and exposure.dataset_id = forward_rung.dataset_id
            and exposure.exposure_purpose = 'FORWARD_CONFIRMATION'
            and exposure.knowledge_clock_mode = 'ARRIVAL_TIME_CAUSAL'
            and exposure.instrument_ids =
                forward_rung.planned_instrument_ids
            and exposure.instrument_count =
                forward_rung.planned_instrument_count
            and exposure.instrument_set_fingerprint =
                forward_rung.instrument_set_fingerprint
            and exposure.quote_row_count > 0
            and exposure.trade_row_count > 0
       ) = forward_rung.planned_session_dates
     order by work.next_attempt_at, work.created_at, work.work_item_id
     for update of work skip locked
     limit 1
  ), leased as (
    update audit.intraday_forward_reproduction_work_items work
       set status = 'LEASED',
           next_attempt_at = null,
           attempt_count = work.attempt_count + 1,
           leased_at = v_now,
           lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
           leased_by = btrim(p_worker),
           lease_token = gen_random_uuid()
      from candidate_work
     where work.work_item_id = candidate_work.work_item_id
     returning work.*
  )
  select jsonb_build_object(
      'contract_version', 'intraday-forward-qa-reproduction-input-v1',
      'work_item', jsonb_build_object(
        'work_item_id', leased.work_item_id,
        'reproduction_request_id', leased.reproduction_request_id,
        'lease_token', leased.lease_token,
        'attempt_count', leased.attempt_count,
        'max_attempts', leased.max_attempts
      ),
      'request', jsonb_build_object(
        'payload_fingerprint', request.payload_fingerprint,
        'reproduction_contract', request.reproduction_contract
      ),
      'experiment', jsonb_build_object(
        'experiment_id', experiment.experiment_id,
        'hypothesis_id', experiment.hypothesis_id,
        'input_hash', experiment.input_hash,
        'code_version', experiment.code_version,
        'cost_model_version', experiment.cost_model_version
      ),
      'candidate', jsonb_build_object(
        'candidate_lineage_id', candidate.candidate_lineage_id,
        'root_lineage_id', candidate.root_lineage_id,
        'candidate_identity_fingerprint',
          candidate.candidate_identity_fingerprint,
        'candidate_ast_fingerprint', candidate.candidate_ast_fingerprint,
        'semantic_plan_fingerprint', candidate.semantic_plan_fingerprint,
        'feature_spec_fingerprint', candidate.feature_spec_fingerprint,
        'label_spec_fingerprint', candidate.label_spec_fingerprint,
        'model_spec_fingerprint', candidate.model_spec_fingerprint,
        'evaluator_version', candidate.evaluator_version,
        'cost_model_version', candidate.cost_model_version
      ),
      'forward_rung', jsonb_build_object(
        'experiment_rung_id', forward_rung.experiment_rung_id,
        'candidate_lineage_id', forward_rung.candidate_lineage_id,
        'root_lineage_id', forward_rung.root_lineage_id,
        'dataset_id', forward_rung.dataset_id,
        'planned_session_dates', forward_rung.planned_session_dates,
        'planned_session_count', forward_rung.planned_session_count,
        'planned_instrument_ids', forward_rung.planned_instrument_ids,
        'planned_instrument_count', forward_rung.planned_instrument_count,
        'session_set_fingerprint', forward_rung.session_set_fingerprint,
        'instrument_set_fingerprint', forward_rung.instrument_set_fingerprint,
        'rung_plan_fingerprint', forward_rung.rung_plan_fingerprint,
        'dataset_cutoff', forward_rung.dataset_cutoff,
        'forward_test_index', forward_rung.forward_test_index
      ),
      'report_revision', jsonb_build_object(
        'report_revision_id', report_revision.report_revision_id,
        'report_fingerprint', report_revision.report_fingerprint,
        'report', report_revision.report
      ),
      'confirmation', jsonb_build_object(
        'forward_confirmation_id', confirmation.forward_confirmation_id,
        'decision', confirmation.decision,
        'gate_version', confirmation.gate_version,
        'gate_statistics', confirmation.gate_statistics,
        'gate_failures', confirmation.gate_failures,
        'confirmation_evidence_fingerprint',
          confirmation.confirmation_evidence_fingerprint
      ),
      'session_exposures', exposure_bundle.session_exposures
    )
    into v_input
    from leased
    join audit.intraday_forward_reproduction_requests request
      on request.reproduction_request_id = leased.reproduction_request_id
    join quant.experiments experiment
      on experiment.experiment_id = request.experiment_id
    join quant.intraday_experiment_rungs forward_rung
      on forward_rung.experiment_id = experiment.experiment_id
     and forward_rung.rung = 'FORWARD'
    join quant.intraday_candidate_lineages candidate
      on candidate.candidate_lineage_id = forward_rung.candidate_lineage_id
     and candidate.root_lineage_id = forward_rung.root_lineage_id
    join quant.intraday_forward_confirmations confirmation
      on confirmation.forward_confirmation_id =
        request.forward_confirmation_id
     and confirmation.experiment_rung_id =
        forward_rung.experiment_rung_id
    join quant.intraday_forward_report_revisions report_revision
      on report_revision.report_revision_id = request.report_revision_id
     and report_revision.forward_confirmation_id =
        confirmation.forward_confirmation_id
    cross join lateral (
      select jsonb_agg(
        jsonb_build_object(
          'session_date', exposure.session_date,
          'session_content_fingerprint',
            exposure.session_content_fingerprint,
          'quote_row_count', exposure.quote_row_count,
          'trade_row_count', exposure.trade_row_count,
          'instrument_set_fingerprint',
            exposure.instrument_set_fingerprint,
          'instrument_count', exposure.instrument_count
        ) order by exposure.session_date
      ) as session_exposures
        from quant.intraday_session_exposures exposure
       where exposure.experiment_rung_id =
             forward_rung.experiment_rung_id
         and exposure.candidate_lineage_id =
             forward_rung.candidate_lineage_id
         and exposure.root_lineage_id = forward_rung.root_lineage_id
         and exposure.dataset_id = forward_rung.dataset_id
         and exposure.exposure_purpose = 'FORWARD_CONFIRMATION'
         and exposure.knowledge_clock_mode = 'ARRIVAL_TIME_CAUSAL'
         and exposure.instrument_ids = forward_rung.planned_instrument_ids
         and exposure.instrument_count =
             forward_rung.planned_instrument_count
         and exposure.instrument_set_fingerprint =
             forward_rung.instrument_set_fingerprint
         and exposure.session_date = any(forward_rung.planned_session_dates)
    ) exposure_bundle;

  return v_input;
end
$$;

create or replace function audit.heartbeat_intraday_forward_reproduction_work(
  p_work_item_id uuid,
  p_lease_token uuid,
  p_worker text,
  p_lease_seconds integer default 900
)
returns boolean
language plpgsql
security definer
set search_path = pg_catalog, audit
as $$
declare
  v_now timestamptz := clock_timestamp();
begin
  if p_work_item_id is null or p_lease_token is null
     or p_worker is null or btrim(p_worker) = '' then
    return false;
  end if;
  if p_lease_seconds is null
     or p_lease_seconds not between 30 and 7200 then
    raise exception 'QA reproduction lease seconds must be between 30 and 7200';
  end if;

  update audit.intraday_forward_reproduction_work_items work
     set lease_expires_at = v_now + make_interval(secs => p_lease_seconds)
   where work.work_item_id = p_work_item_id
     and work.status = 'LEASED'
     and work.lease_token = p_lease_token
     and work.leased_by = btrim(p_worker)
     and work.lease_expires_at > v_now;
  return found;
end
$$;

create or replace function audit.complete_intraday_forward_reproduction_work(
  p_work_item_id uuid,
  p_lease_token uuid,
  p_worker text,
  p_verdict text,
  p_result_evidence jsonb,
  p_worker_version text
)
returns uuid
language plpgsql
security definer
set search_path = pg_catalog, audit, extensions
as $$
declare
  v_now timestamptz := clock_timestamp();
  v_verdict text := upper(btrim(p_verdict));
  v_worker text := btrim(p_worker);
  v_worker_version text := btrim(p_worker_version);
  v_work record;
  v_existing record;
  v_result_document jsonb;
  v_result_fingerprint text;
  v_result_id uuid;
begin
  if p_work_item_id is null or p_lease_token is null
     or v_worker is null or v_worker = '' then
    raise exception 'QA reproduction completion identity is required';
  end if;
  if v_verdict is null
     or v_verdict not in ('PASS', 'FAIL', 'INCONCLUSIVE') then
    raise exception 'invalid QA reproduction verdict: %', p_verdict;
  end if;
  if v_worker_version is null or v_worker_version = '' then
    raise exception 'QA reproducer worker version is required';
  end if;
  if jsonb_typeof(p_result_evidence) is distinct from 'object'
     or p_result_evidence = '{}'::jsonb then
    raise exception 'QA reproduction result evidence must be a non-empty object';
  end if;

  select work.*,
         request.payload_fingerprint as request_payload_fingerprint,
         (request.reproduction_contract->>'report_revision_id')::uuid
           as report_revision_id,
         (request.reproduction_contract->>'outcome_revision_id')::uuid
           as outcome_revision_id,
         request.reproduction_contract->>'report_fingerprint'
           as report_fingerprint,
         request.reproduction_contract->>'outcome_revision_fingerprint'
           as outcome_revision_fingerprint,
         request.reproduction_contract->>'instrument_set_fingerprint'
           as instrument_set_fingerprint,
         request.reproduction_contract->>'session_set_fingerprint'
           as session_set_fingerprint,
         request.reproduction_contract->>'rung_plan_fingerprint'
           as rung_plan_fingerprint,
         request.reproduction_contract->>'confirmation_evidence_fingerprint'
           as confirmation_evidence_fingerprint
    into v_work
    from audit.intraday_forward_reproduction_work_items work
    join audit.intraday_forward_reproduction_requests request
      on request.reproduction_request_id = work.reproduction_request_id
   where work.work_item_id = p_work_item_id
   for update of work;
  if not found then
    raise exception 'QA reproduction work item does not exist: %',
      p_work_item_id;
  end if;

  v_result_document := jsonb_build_object(
    'result_schema_version',
      'intraday-forward-qa-reproduction-result-v1',
    'work_item_id', v_work.work_item_id,
    'reproduction_request_id', v_work.reproduction_request_id,
    'verdict', v_verdict,
    'worker_version', v_worker_version,
    'reproduced_by', v_worker,
    'source_fingerprints', jsonb_build_object(
      'report_revision_id', v_work.report_revision_id,
      'outcome_revision_id', v_work.outcome_revision_id,
      'request_payload_fingerprint', v_work.request_payload_fingerprint,
      'report_fingerprint', v_work.report_fingerprint,
      'outcome_revision_fingerprint',
        v_work.outcome_revision_fingerprint,
      'instrument_set_fingerprint', v_work.instrument_set_fingerprint,
      'session_set_fingerprint', v_work.session_set_fingerprint,
      'rung_plan_fingerprint', v_work.rung_plan_fingerprint,
      'confirmation_evidence_fingerprint',
        v_work.confirmation_evidence_fingerprint
    ),
    'result_evidence', p_result_evidence,
    'promotion_authority', false
  );
  v_result_fingerprint := encode(
    extensions.digest(
      convert_to(v_result_document::text, 'UTF8'), 'sha256'
    ), 'hex'
  );

  select result.*
    into v_existing
    from audit.intraday_forward_reproduction_results result
   where result.work_item_id = p_work_item_id
      or result.reproduction_request_id = v_work.reproduction_request_id
   for update;
  if found then
    if v_work.status <> 'COMPLETED'
       or v_existing.completion_lease_token <> p_lease_token
       or v_existing.reproduced_by <> v_worker
       or v_existing.verdict <> v_verdict
       or v_existing.worker_version <> v_worker_version
       or v_existing.result_evidence is distinct from p_result_evidence
       or v_existing.result_fingerprint <> v_result_fingerprint then
      raise exception
        'QA reproduction completion conflicts with immutable result';
    end if;
    return v_existing.reproduction_result_id;
  end if;

  if v_work.status <> 'LEASED'
     or v_work.lease_token <> p_lease_token
     or v_work.leased_by <> v_worker
     or v_work.lease_expires_at <= v_now then
    raise exception 'QA reproduction lease is stale or owned by another worker';
  end if;

  insert into audit.intraday_forward_reproduction_results (
    reproduction_request_id, work_item_id, completion_lease_token,
    verdict, worker_version, report_revision_id, outcome_revision_id,
    request_payload_fingerprint, report_fingerprint,
    outcome_revision_fingerprint, instrument_set_fingerprint,
    session_set_fingerprint, rung_plan_fingerprint,
    confirmation_evidence_fingerprint, result_evidence, result_document,
    result_fingerprint, promotion_authority, reproduced_by, completed_at
  ) values (
    v_work.reproduction_request_id, v_work.work_item_id, p_lease_token,
    v_verdict, v_worker_version, v_work.report_revision_id,
    v_work.outcome_revision_id, v_work.request_payload_fingerprint,
    v_work.report_fingerprint, v_work.outcome_revision_fingerprint,
    v_work.instrument_set_fingerprint, v_work.session_set_fingerprint,
    v_work.rung_plan_fingerprint,
    v_work.confirmation_evidence_fingerprint, p_result_evidence,
    v_result_document, v_result_fingerprint, false, v_worker, v_now
  )
  returning reproduction_result_id into v_result_id;

  update audit.intraday_forward_reproduction_work_items work
     set status = 'COMPLETED',
         next_attempt_at = null,
         leased_at = null,
         lease_expires_at = null,
         leased_by = null,
         lease_token = null,
         last_error = null
   where work.work_item_id = p_work_item_id
     and work.status = 'LEASED'
     and work.lease_token = p_lease_token
     and work.leased_by = v_worker
     and work.lease_expires_at > v_now;
  if not found then
    raise exception 'QA reproduction lease was lost during completion';
  end if;

  return v_result_id;
end
$$;

create or replace function audit.fail_intraday_forward_reproduction_work(
  p_work_item_id uuid,
  p_lease_token uuid,
  p_worker text,
  p_error text
)
returns text
language plpgsql
security definer
set search_path = pg_catalog, audit
as $$
declare
  v_now timestamptz := clock_timestamp();
  v_status text;
begin
  if p_work_item_id is null or p_lease_token is null
     or p_worker is null or btrim(p_worker) = '' then
    raise exception 'QA reproduction failure identity is required';
  end if;
  if p_error is null or btrim(p_error) = '' then
    raise exception 'QA reproduction infrastructure error is required';
  end if;

  update audit.intraday_forward_reproduction_work_items work
     set status = case
           when work.attempt_count >= work.max_attempts then 'FAILED'
           else 'RETRY'
         end,
         next_attempt_at = case
           when work.attempt_count >= work.max_attempts then null
           else v_now + make_interval(secs => least(
             3600,
             30 * (1 << least(greatest(work.attempt_count - 1, 0), 7))
           ))
         end,
         leased_at = null,
         lease_expires_at = null,
         leased_by = null,
         lease_token = null,
         last_error = left(btrim(p_error), 4000)
   where work.work_item_id = p_work_item_id
     and work.status = 'LEASED'
     and work.lease_token = p_lease_token
     and work.leased_by = btrim(p_worker)
     and work.lease_expires_at > v_now
     and not exists (
       select 1
         from audit.intraday_forward_reproduction_results result
        where result.work_item_id = work.work_item_id
     )
  returning work.status into v_status;
  if not found then
    raise exception 'QA reproduction lease is stale or already completed';
  end if;

  return v_status;
end
$$;

-- Remove every direct path first.  The reproducer can read its immutable
-- results and can mutate queue/results only through the four fenced functions.
revoke all privileges on all tables in schema audit from svc_qa_reproducer;
revoke all privileges on all tables in schema quant from svc_qa_reproducer;
revoke all privileges on all tables in schema research from svc_qa_reproducer;
revoke all privileges on all tables in schema reference from svc_qa_reproducer;
revoke all privileges on all sequences in schema audit from svc_qa_reproducer;
revoke all privileges on all sequences in schema quant from svc_qa_reproducer;
revoke all on schema audit, quant, research, reference
  from svc_qa_reproducer;

revoke all on audit.intraday_forward_reproduction_results
  from public, anon, authenticated, service_role, svc_quant,
       svc_qa_worker, svc_audit_api, svc_qa_reproducer;
grant usage on schema audit to svc_qa_reproducer;
grant select on audit.intraday_forward_reproduction_results
  to svc_qa_reproducer;

revoke all on function
  audit.claim_intraday_forward_reproduction_work(text, integer),
  audit.heartbeat_intraday_forward_reproduction_work(
    uuid, uuid, text, integer),
  audit.complete_intraday_forward_reproduction_work(
    uuid, uuid, text, text, jsonb, text),
  audit.fail_intraday_forward_reproduction_work(uuid, uuid, text, text)
from public, anon, authenticated, service_role, svc_quant,
     svc_qa_worker, svc_audit_api;
grant execute on function
  audit.claim_intraday_forward_reproduction_work(text, integer),
  audit.heartbeat_intraday_forward_reproduction_work(
    uuid, uuid, text, integer),
  audit.complete_intraday_forward_reproduction_work(
    uuid, uuid, text, text, jsonb, text),
  audit.fail_intraday_forward_reproduction_work(uuid, uuid, text, text)
to svc_qa_reproducer;

do $qa_reproducer_privilege_audit$
declare
  unsafe_grantee oid;
  pool_login name := session_user;
begin
  if exists (
    select 1
      from pg_roles
     where rolname = 'svc_qa_reproducer'
       and (rolcanlogin or rolsuper or rolcreatedb or rolcreaterole
            or rolinherit or rolreplication or rolbypassrls)
  ) then
    raise exception 'svc_qa_reproducer has unsafe role attributes';
  end if;

  if not exists (
    select 1
      from pg_auth_members membership
      join pg_roles granted_role on granted_role.oid = membership.roleid
      join pg_roles member_role on member_role.oid = membership.member
     where granted_role.rolname = 'svc_qa_reproducer'
       and member_role.rolname = pool_login
       and membership.set_option
       and not membership.inherit_option
  ) then
    raise exception '% cannot explicitly reduce to svc_qa_reproducer', pool_login;
  end if;

  if exists (
    select 1
      from information_schema.role_table_grants
     where grantee = 'svc_qa_reproducer'
       and privilege_type <> 'SELECT'
  ) or exists (
    select 1
      from information_schema.role_table_grants
     where grantee = 'svc_qa_reproducer'
       and privilege_type = 'SELECT'
       and (table_schema, table_name) <>
           ('audit', 'intraday_forward_reproduction_results')
  ) then
    raise exception 'svc_qa_reproducer has an unexpected direct table grant';
  end if;

  if not has_table_privilege(
       'svc_qa_reproducer',
       'audit.intraday_forward_reproduction_results', 'SELECT')
     or has_table_privilege(
       'svc_qa_reproducer',
       'audit.intraday_forward_reproduction_results', 'INSERT')
     or has_table_privilege(
       'svc_qa_reproducer',
       'audit.intraday_forward_reproduction_work_items', 'UPDATE')
     or has_table_privilege(
       'svc_qa_worker',
       'audit.intraday_forward_reproduction_results', 'INSERT')
     or has_table_privilege(
       'svc_qa_worker',
       'audit.intraday_forward_reproduction_work_items', 'UPDATE') then
    raise exception 'QA reproducer or transport worker crosses its boundary';
  end if;

  if not exists (
    select 1
      from pg_class relation
      join pg_namespace namespace on namespace.oid = relation.relnamespace
     where namespace.nspname = 'audit'
       and relation.relname = 'intraday_forward_reproduction_results'
       and relation.relrowsecurity
       and relation.relforcerowsecurity
  ) then
    raise exception 'QA reproduction results must enable and force RLS';
  end if;

  if not has_function_privilege(
       'svc_qa_reproducer',
       'audit.claim_intraday_forward_reproduction_work(text,integer)',
       'EXECUTE')
     or not has_function_privilege(
       'svc_qa_reproducer',
       'audit.complete_intraday_forward_reproduction_work(uuid,uuid,text,text,jsonb,text)',
       'EXECUTE')
     or has_function_privilege(
       'svc_qa_worker',
       'audit.claim_intraday_forward_reproduction_work(text,integer)',
       'EXECUTE')
     or has_function_privilege(
       'service_role',
       'audit.complete_intraday_forward_reproduction_work(uuid,uuid,text,text,jsonb,text)',
       'EXECUTE') then
    raise exception 'QA reproduction function execution grants are unsafe';
  end if;

  select expanded.grantee
    into unsafe_grantee
    from pg_proc procedure
    join pg_namespace namespace on namespace.oid = procedure.pronamespace
    cross join lateral aclexplode(
      coalesce(procedure.proacl, acldefault('f', procedure.proowner))
    ) expanded
   where namespace.nspname = 'audit'
     and procedure.proname in (
       'claim_intraday_forward_reproduction_work',
       'heartbeat_intraday_forward_reproduction_work',
       'complete_intraday_forward_reproduction_work',
       'fail_intraday_forward_reproduction_work'
     )
     and expanded.privilege_type = 'EXECUTE'
     and expanded.grantee <> procedure.proowner
     and expanded.grantee <>
       (select oid from pg_roles where rolname = 'svc_qa_reproducer')
   limit 1;
  if unsafe_grantee is not null then
    raise exception 'a non-reproducer role can execute a QA reproduction function';
  end if;
end
$qa_reproducer_privilege_audit$;

comment on table audit.intraday_forward_reproduction_results is
  'Immutable one-per-request independent reproduction verdict. FAIL is a completed scientific result; promotion_authority is always false.';
comment on function audit.claim_intraday_forward_reproduction_work(text, integer)
  is 'Atomically leases one validated ACTIVE KRX STOCK forward request and returns a zero-follow-up-read reproduction input v1 bundle.';

commit;
