begin;

-- research.experiment_outcomes deliberately remains one row per experiment.
-- Independent forward evidence is a new immutable decision revision, not a
-- second legacy row that old all-row readers could mistake for a contradictory
-- peer decision.  Revision 0 is the referenced base outcome; the one allowed
-- FORWARD rung produces revision 1.
create table research.experiment_outcome_revisions (
  outcome_revision_id          uuid primary key,
  base_outcome_id              text not null
    references research.experiment_outcomes(outcome_id) on delete restrict,
  experiment_id                uuid not null
    references quant.experiments(experiment_id) on delete restrict,
  forward_confirmation_id      uuid not null
    references quant.intraday_forward_confirmations(forward_confirmation_id)
    on delete restrict,
  revision_number              integer not null,
  decision                     text not null,
  decided_at                   timestamptz not null,
  failed_criteria              text[] not null default '{}',
  oos_summary                  jsonb not null,
  lesson_codes                 text[] not null default '{}',
  notes                        text not null default '',
  outcome_revision_fingerprint text not null,
  revised_by                   text not null,
  revised_at                   timestamptz not null default now(),

  constraint uq_intraday_outcome_revision_confirmation unique
    (forward_confirmation_id),
  constraint uq_intraday_outcome_revision_number unique
    (experiment_id, revision_number),
  constraint uq_intraday_outcome_revision_identity unique
    (outcome_revision_id, forward_confirmation_id, experiment_id),
  constraint chk_intraday_outcome_revision_number check
    (revision_number = 1),
  constraint chk_intraday_outcome_revision_decision check
    (decision in ('REJECT', 'SUBMIT_TO_QA', 'GATE_HOLD')),
  constraint chk_intraday_outcome_revision_reason check (
    decision not in ('REJECT', 'GATE_HOLD')
    or cardinality(failed_criteria) > 0
    or cardinality(lesson_codes) > 0
  ),
  constraint chk_intraday_outcome_revision_payload check
    (jsonb_typeof(oos_summary) = 'object'
     and oos_summary <> '{}'::jsonb),
  constraint chk_intraday_outcome_revision_hash check
    (outcome_revision_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_outcome_revision_text check
    (btrim(base_outcome_id) <> '' and btrim(revised_by) <> '')
);

create or replace function quant.validate_intraday_outcome_revision()
returns trigger
language plpgsql
set search_path = pg_catalog, quant, research
as $$
begin
  if not exists (
    select 1
      from research.experiment_outcomes base
     where base.outcome_id = new.base_outcome_id
       and base.experiment_id = new.experiment_id::text
  ) then
    raise exception
      'forward outcome revision base outcome belongs to another experiment';
  end if;
  if not exists (
    select 1
      from quant.intraday_forward_confirmations confirmation
      join quant.intraday_experiment_rungs rung
        on rung.experiment_rung_id = confirmation.experiment_rung_id
     where confirmation.forward_confirmation_id =
           new.forward_confirmation_id
       and rung.experiment_id = new.experiment_id
       and rung.rung = 'FORWARD'
  ) then
    raise exception
      'forward outcome revision confirmation belongs to another experiment';
  end if;
  return new;
end
$$;

create trigger experiment_outcome_revision_identity_guard
before insert on research.experiment_outcome_revisions
for each row execute function quant.validate_intraday_outcome_revision();

-- A historical intraday report is immutable. Independent forward evidence is
-- therefore published as a content-addressed revision rather than updating
-- quant.intraday_report_manifests in place.
create table quant.intraday_forward_report_revisions (
  report_revision_id       uuid primary key default gen_random_uuid(),
  experiment_id            uuid not null
    references quant.experiments(experiment_id) on delete restrict,
  forward_confirmation_id  uuid not null
    references quant.intraday_forward_confirmations(forward_confirmation_id)
    on delete restrict,
  revision_number          integer not null,
  base_report_fingerprint  text not null,
  report_fingerprint       text not null,
  decision                 text not null,
  outcome_revision_id      uuid not null,
  hypothesis_status        text not null,
  report                    jsonb not null,
  lifecycle_request         jsonb not null,
  published_by              text not null,
  published_at              timestamptz not null default now(),

  constraint fk_intraday_forward_report_outcome foreign key
    (outcome_revision_id, forward_confirmation_id, experiment_id)
    references research.experiment_outcome_revisions
      (outcome_revision_id, forward_confirmation_id, experiment_id)
    on delete restrict,
  constraint uq_intraday_forward_report_confirmation unique
    (forward_confirmation_id),
  constraint uq_intraday_forward_report_revision unique
    (experiment_id, revision_number),
  constraint uq_intraday_forward_report_outcome unique
    (outcome_revision_id),
  constraint uq_intraday_forward_report_identity unique
    (report_revision_id, forward_confirmation_id, experiment_id),
  constraint chk_intraday_forward_report_revision check
    (revision_number = 1),
  constraint chk_intraday_forward_report_hashes check
    (base_report_fingerprint ~ '^[0-9a-f]{64}$'
     and report_fingerprint ~ '^[0-9a-f]{64}$'),
  constraint chk_intraday_forward_report_decision check
    (decision in ('PASS', 'FAIL', 'INCONCLUSIVE')),
  constraint chk_intraday_forward_report_status check
    (hypothesis_status in ('SUPPORTED', 'REJECTED', 'INCONCLUSIVE')),
  constraint chk_intraday_forward_report_payload check
    (jsonb_typeof(report) = 'object' and report <> '{}'::jsonb
     and jsonb_typeof(lifecycle_request) = 'object'),
  constraint chk_intraday_forward_report_text check
    (btrim(published_by) <> '')
);

-- A PASS asks QA to reproduce the result; it never promotes a strategy.  The
-- composite FK prevents a handoff from mixing identities across publications.
create table quant.intraday_forward_qa_handoffs (
  qa_handoff_id            uuid primary key default gen_random_uuid(),
  forward_confirmation_id  uuid not null,
  report_revision_id       uuid not null,
  experiment_id            uuid not null,
  status                   text not null default 'REQUESTED',
  next_owner               text not null default 'QA_REPRODUCTION',
  request_payload          jsonb not null,
  requested_by             text not null,
  requested_at             timestamptz not null default now(),
  constraint fk_intraday_forward_qa_report foreign key
    (report_revision_id, forward_confirmation_id, experiment_id)
    references quant.intraday_forward_report_revisions
      (report_revision_id, forward_confirmation_id, experiment_id)
    on delete restrict,
  constraint uq_intraday_forward_qa_confirmation unique
    (forward_confirmation_id),
  constraint uq_intraday_forward_qa_report unique (report_revision_id),
  constraint chk_intraday_forward_qa_status check (status = 'REQUESTED'),
  constraint chk_intraday_forward_qa_owner check
    (next_owner = 'QA_REPRODUCTION'),
  constraint chk_intraday_forward_qa_payload check
    (jsonb_typeof(request_payload) = 'object'
     and request_payload <> '{}'::jsonb),
  constraint chk_intraday_forward_qa_actor check
    (btrim(requested_by) <> '')
);

-- Mutable scheduling state is deliberately separate from append-only
-- scientific evidence. Workers claim due rows with SKIP LOCKED. WAITING and
-- RETRY carry durable schedules, and the token fences stale worker completion.
create table quant.intraday_forward_work_items (
  experiment_id          uuid primary key
    references quant.experiments(experiment_id) on delete restrict,
  candidate_lineage_id   uuid not null
    references quant.intraday_candidate_lineages(candidate_lineage_id)
    on delete restrict,
  status                 text not null default 'READY',
  next_attempt_at        timestamptz not null default now(),
  attempt_count          integer not null default 0,
  error_count            integer not null default 0,
  max_error_count        integer not null default 8,
  leased_at              timestamptz,
  lease_expires_at       timestamptz,
  leased_by              text,
  lease_token            uuid,
  last_result            text,
  last_error             text,
  created_at             timestamptz not null default now(),
  updated_at             timestamptz not null default now(),
  constraint chk_intraday_forward_work_status check
    (status in ('READY', 'LEASED', 'WAITING', 'RETRY', 'CONFIRMED',
                'NOT_NOMINATED', 'FAILED')),
  constraint chk_intraday_forward_work_attempts check
    (attempt_count >= 0 and error_count >= 0
     and max_error_count between 1 and 100
     and error_count <= max_error_count),
  constraint chk_intraday_forward_work_failed_reason check
    (status <> 'FAILED'
     or (error_count = max_error_count
         and last_error is not null and btrim(last_error) <> '')),
  constraint chk_intraday_forward_work_retry_budget check
    (status <> 'RETRY' or error_count < max_error_count),
  constraint chk_intraday_forward_work_lease check (
    (status = 'LEASED' and leased_at is not null
       and lease_expires_at is not null and leased_by is not null
       and lease_token is not null and lease_expires_at > leased_at)
    or
    (status <> 'LEASED' and leased_at is null
       and lease_expires_at is null and leased_by is null
       and lease_token is null)
  ),
  constraint chk_intraday_forward_work_schedule check (
    (status in ('READY', 'WAITING', 'RETRY') and next_attempt_at is not null)
    or
    (status in ('LEASED', 'CONFIRMED', 'NOT_NOMINATED', 'FAILED')
       and next_attempt_at is null)
  )
);

create index idx_intraday_forward_work_due
  on quant.intraday_forward_work_items
    (next_attempt_at, updated_at, created_at, experiment_id)
  where status in ('READY', 'WAITING', 'RETRY');
create index idx_intraday_forward_work_expired_lease
  on quant.intraday_forward_work_items (lease_expires_at, experiment_id)
  where status = 'LEASED';
create index idx_intraday_forward_revision_experiment
  on quant.intraday_forward_report_revisions
    (experiment_id, revision_number desc);
create index idx_intraday_outcome_revision_experiment
  on research.experiment_outcome_revisions
    (experiment_id, revision_number desc);

create trigger experiment_outcome_revisions_append_only
before update or delete on research.experiment_outcome_revisions
for each row execute function governance.reject_append_only_change();
create trigger intraday_forward_report_revisions_append_only
before update or delete on quant.intraday_forward_report_revisions
for each row execute function governance.reject_append_only_change();
create trigger intraday_forward_qa_handoffs_append_only
before update or delete on quant.intraday_forward_qa_handoffs
for each row execute function governance.reject_append_only_change();

-- This is the authoritative semantic projection. Legacy consumers can migrate
-- explicitly without ever seeing two peer rows for one experiment.
create view research.v_current_experiment_outcomes
with (security_barrier = true) as
with canonical_base as (
    -- Historical bugs allowed more than one base row for an experiment even
    -- though the application invariant is one. Prefer a revised base; absent
    -- a revision, choose the latest deterministically. The public projection
    -- therefore remains exactly one current semantic row per experiment.
    select distinct on (base.experiment_id) base.*
      from research.experiment_outcomes base
      left join research.experiment_outcome_revisions candidate_revision
        on candidate_revision.base_outcome_id = base.outcome_id
     order by base.experiment_id,
              (candidate_revision.outcome_revision_id is not null) desc,
              base.decided_at desc, base.created_at desc, base.outcome_id desc
)
select base.outcome_id,
       base.experiment_id,
       base.hypothesis_id,
       base.trial_family_id,
       base.trial_number,
       coalesce(revision.decision, base.decision) as decision,
       coalesce(revision.decided_at, base.decided_at) as decided_at,
       base.proposal_id,
       coalesce(revision.failed_criteria,
                base.failed_criteria) as failed_criteria,
       (base.oos_summary || coalesce(revision.oos_summary, '{}'::jsonb))
         as oos_summary,
       base.regime_concerns,
       coalesce(revision.lesson_codes, base.lesson_codes) as lesson_codes,
       coalesce(revision.notes, base.notes) as notes,
       coalesce(revision.revised_at, base.created_at) as created_at,
       base.root_cause,
       base.corrective_action,
       base.verification_state,
       base.verification_note,
       revision.outcome_revision_id,
       coalesce(revision.revision_number, 0) as revision_number,
       revision.forward_confirmation_id,
       revision.outcome_revision_fingerprint,
       (revision.outcome_revision_id is not null) as is_revised
  from canonical_base base
  left join lateral (
    select candidate.*
      from research.experiment_outcome_revisions candidate
     where candidate.base_outcome_id = base.outcome_id
     order by candidate.revision_number desc, candidate.decided_at desc,
              candidate.outcome_revision_id desc
     limit 1
  ) revision on true;

-- Refresh the already-published Library projections in this new migration;
-- never rewrite the applied 20260814090000 migration.  These views must not
-- keep presenting revision-0 GATE_HOLD after a forward PASS/FAIL is current.
create or replace view research.v_trial_family_status as
with last_out as (
    select distinct on (trial_family_id)
           trial_family_id, decision, decided_at, lesson_codes,
           root_cause, notes, oos_summary, experiment_id
      from research.v_current_experiment_outcomes
     where trial_family_id is not null
     order by trial_family_id, decided_at desc
), agg as (
    select trial_family_id,
           count(*) as outcomes,
           count(*) filter (where decision like 'REJECT%') as rejects,
           count(*) filter (where decision = 'GATE_HOLD') as holds,
           count(*) filter (where decision in
             ('PROMOTED', 'SUPPORTED', 'SUBMIT_TO_QA')) as advanced,
           min(decided_at) as first_decided,
           max(decided_at) as last_decided,
           array_agg(distinct lc) filter (where lc is not null) as all_lessons
      from research.v_current_experiment_outcomes,
           lateral unnest(coalesce(lesson_codes, '{}')) as lc
     where trial_family_id is not null
     group by trial_family_id
)
select aggregate.trial_family_id,
       aggregate.outcomes, aggregate.rejects, aggregate.holds,
       aggregate.advanced, aggregate.first_decided, aggregate.last_decided,
       aggregate.all_lessons,
       latest.decision as last_decision,
       latest.root_cause as last_root_cause,
       latest.lesson_codes as last_lessons,
       latest.notes as last_note,
       latest.oos_summary as last_metrics,
       latest.experiment_id as last_experiment_id
  from agg aggregate
  left join last_out latest
    on latest.trial_family_id = aggregate.trial_family_id;

create or replace view research.v_experiment_scorecard as
select outcome.experiment_id,
       outcome.trial_family_id,
       outcome.decision,
       outcome.decided_at,
       coalesce(hypothesis.expected_edge->>'type', '') as edge_type,
       coalesce(hypothesis.expected_edge->>'universe_key', '')
         as universe_key,
       nullif(experiment.config->>'top_n', '')::int as top_n,
       (outcome.oos_summary->>'excess_return_pct')::numeric
         as excess_return_pct,
       (outcome.oos_summary->>'information_ratio')::numeric
         as information_ratio,
       (outcome.oos_summary->>'max_drawdown_pct')::numeric
         as max_drawdown_pct,
       (outcome.oos_summary->>'deflated_sharpe')::numeric
         as deflated_sharpe,
       (outcome.oos_summary->>'pbo')::numeric as pbo,
       (outcome.oos_summary->>'m2_excess_ann_pct')::numeric
         as m2_excess_ann_pct,
       (outcome.oos_summary->>'alpha_ann_pct')::numeric as alpha_ann_pct,
       (outcome.oos_summary->>'appraisal_ratio')::numeric
         as appraisal_ratio,
       (outcome.oos_summary->>'strategy_ann_vol_pct')::numeric
         as strategy_ann_vol_pct,
       (outcome.oos_summary->>'benchmark_ann_vol_pct')::numeric
         as benchmark_ann_vol_pct,
       (outcome.oos_summary->>'signal_ic')::numeric as signal_ic,
       (outcome.oos_summary->>'signal_ic_t')::numeric as signal_ic_t,
       (outcome.oos_summary->>'turnover_total')::numeric as turnover_total,
       outcome.lesson_codes,
       outcome.root_cause,
       outcome.notes,
       hypothesis.mapping_loss,
       proposal.llm_model_id,
       nullif(experiment.config->>'max_drawdown_stop', '')::numeric
         as max_drawdown_stop,
       nullif(experiment.config->>'vol_target_annual', '')::numeric
         as vol_target_annual,
       nullif(experiment.config->>'max_exposure', '')::numeric
         as max_exposure,
       nullif(experiment.config->>'min_adv_krw', '')::numeric as min_adv_krw,
       (experiment.config ? 'max_drawdown_stop'
        or experiment.config ? 'vol_target_annual') as risk_controlled
  from research.v_current_experiment_outcomes outcome
  left join quant.experiments experiment
    on experiment.experiment_id::text = outcome.experiment_id
  left join quant.hypotheses hypothesis
    on hypothesis.hypothesis_id = experiment.hypothesis_id
  left join research.experiment_proposals proposal
    on proposal.proposal_id = hypothesis.proposal_id;

alter table research.experiment_outcome_revisions enable row level security;
alter table quant.intraday_forward_report_revisions enable row level security;
alter table quant.intraday_forward_qa_handoffs enable row level security;
alter table quant.intraday_forward_work_items enable row level security;

create policy experiment_outcome_revisions_svc_quant_select
  on research.experiment_outcome_revisions for select to svc_quant using (true);
create policy experiment_outcome_revisions_svc_quant_insert
  on research.experiment_outcome_revisions for insert to svc_quant
  with check (true);
create policy intraday_forward_report_revisions_svc_quant_select
  on quant.intraday_forward_report_revisions for select to svc_quant using (true);
create policy intraday_forward_report_revisions_svc_quant_insert
  on quant.intraday_forward_report_revisions for insert to svc_quant
  with check (true);
create policy intraday_forward_qa_handoffs_svc_quant_select
  on quant.intraday_forward_qa_handoffs for select to svc_quant using (true);
create policy intraday_forward_qa_handoffs_svc_quant_insert
  on quant.intraday_forward_qa_handoffs for insert to svc_quant
  with check (true);
create policy intraday_forward_work_items_svc_quant_select
  on quant.intraday_forward_work_items for select to svc_quant using (true);
create policy intraday_forward_work_items_svc_quant_insert
  on quant.intraday_forward_work_items for insert to svc_quant with check (true);
create policy intraday_forward_work_items_svc_quant_update
  on quant.intraday_forward_work_items for update to svc_quant
  using (true) with check (true);

-- The role already has grants on these core quant tables, but RLS otherwise
-- makes those grants inert. Restrict the missing policies to the exact actions
-- used by candidate discovery, metric repair, and lifecycle finalization.
create policy experiments_svc_quant_select
  on quant.experiments for select to svc_quant using (true);
create policy hypotheses_svc_quant_select
  on quant.hypotheses for select to svc_quant using (true);
create policy hypotheses_svc_quant_update
  on quant.hypotheses for update to svc_quant using (true) with check (true);
create policy experiment_metrics_svc_quant_select
  on quant.experiment_metrics for select to svc_quant using (true);
create policy experiment_metrics_svc_quant_insert
  on quant.experiment_metrics for insert to svc_quant with check (true);
create policy experiment_metrics_svc_quant_update
  on quant.experiment_metrics for update to svc_quant
  using (true) with check (true);

grant usage on schema research to svc_quant;
revoke all on function quant.validate_intraday_outcome_revision()
  from public;
grant execute on function quant.validate_intraday_outcome_revision()
  to svc_quant, service_role;
grant select on research.experiment_outcomes to svc_quant;
grant select, insert on research.experiment_outcome_revisions
  to svc_quant, service_role;
grant select on research.v_current_experiment_outcomes
  to svc_quant, service_role;
grant select, insert on
  quant.intraday_forward_report_revisions,
  quant.intraday_forward_qa_handoffs
to svc_quant, service_role;
grant select, insert, update on quant.intraday_forward_work_items
to svc_quant, service_role;
revoke update, delete, truncate on
  research.experiment_outcome_revisions,
  quant.intraday_forward_report_revisions,
  quant.intraday_forward_qa_handoffs
from svc_quant, service_role;
revoke delete, truncate on quant.intraday_forward_work_items
from svc_quant, service_role;

comment on table research.experiment_outcome_revisions is
  'Append-only forward revisions of the one-row-per-experiment legacy outcome. Query research.v_current_experiment_outcomes for current semantics.';
comment on table quant.intraday_forward_report_revisions is
  'Append-only authoritative report revision 1. Revision 0 remains immutable in intraday_report_manifests.';
comment on table quant.intraday_forward_qa_handoffs is
  'Idempotent QA reproduction requests created only by PASS; these convey no promotion authority.';
comment on table quant.intraday_forward_work_items is
  'Fair persistent scheduler for forward nominees. Mutable leases/backoff are operational state, never scientific evidence.';

commit;
