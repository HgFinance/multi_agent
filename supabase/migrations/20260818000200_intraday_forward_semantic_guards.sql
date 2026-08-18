begin;

-- A forward decision is represented in three vocabularies:
--
--   confirmation       report/outcome decision       hypothesis status
--   PASS               SUBMIT_TO_QA                  SUPPORTED
--   FAIL               REJECT                        REJECTED
--   INCONCLUSIVE       HOLD/GATE_HOLD                INCONCLUSIVE
--
-- Keep the mapping in the database.  Local CHECK constraints only constrain
-- each column's vocabulary; without these guards a writer could combine valid
-- words into an invalid authoritative publication.
create or replace function quant.validate_intraday_outcome_revision()
returns trigger
language plpgsql
set search_path = pg_catalog, quant, research
as $$
declare
  confirmation_decision text;
  expected_outcome_decision text;
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

  select confirmation.decision
    into confirmation_decision
    from quant.intraday_forward_confirmations confirmation
    join quant.intraday_experiment_rungs rung
      on rung.experiment_rung_id = confirmation.experiment_rung_id
   where confirmation.forward_confirmation_id = new.forward_confirmation_id
     and rung.experiment_id = new.experiment_id
     and rung.rung = 'FORWARD';

  if not found then
    raise exception
      'forward outcome revision confirmation belongs to another experiment';
  end if;

  expected_outcome_decision := case confirmation_decision
    when 'PASS' then 'SUBMIT_TO_QA'
    when 'FAIL' then 'REJECT'
    when 'INCONCLUSIVE' then 'GATE_HOLD'
    else null
  end;
  if expected_outcome_decision is null
     or new.decision is distinct from expected_outcome_decision then
    raise exception
      'forward outcome decision % conflicts with confirmation decision %',
      new.decision, confirmation_decision;
  end if;
  return new;
end
$$;

create or replace function quant.validate_intraday_forward_report_revision()
returns trigger
language plpgsql
set search_path = pg_catalog, quant, research
as $$
declare
  confirmation_decision text;
  outcome_decision text;
  expected_outcome_decision text;
  expected_hypothesis_status text;
  expected_report_decision text;
begin
  select confirmation.decision, outcome.decision
    into confirmation_decision, outcome_decision
    from quant.intraday_forward_confirmations confirmation
    join quant.intraday_experiment_rungs rung
      on rung.experiment_rung_id = confirmation.experiment_rung_id
     and rung.experiment_id = new.experiment_id
     and rung.rung = 'FORWARD'
    join research.experiment_outcome_revisions outcome
      on outcome.outcome_revision_id = new.outcome_revision_id
     and outcome.forward_confirmation_id = new.forward_confirmation_id
     and outcome.experiment_id = new.experiment_id
   where confirmation.forward_confirmation_id = new.forward_confirmation_id;

  if not found then
    raise exception
      'forward report revision lacks matching confirmation and outcome';
  end if;

  expected_outcome_decision := case confirmation_decision
    when 'PASS' then 'SUBMIT_TO_QA'
    when 'FAIL' then 'REJECT'
    when 'INCONCLUSIVE' then 'GATE_HOLD'
    else null
  end;
  expected_hypothesis_status := case confirmation_decision
    when 'PASS' then 'SUPPORTED'
    when 'FAIL' then 'REJECTED'
    when 'INCONCLUSIVE' then 'INCONCLUSIVE'
    else null
  end;
  expected_report_decision := case confirmation_decision
    when 'PASS' then 'SUBMIT_TO_QA'
    when 'FAIL' then 'REJECT'
    when 'INCONCLUSIVE' then 'HOLD'
    else null
  end;

  if expected_outcome_decision is null
     or new.decision is distinct from confirmation_decision
     or outcome_decision is distinct from expected_outcome_decision
     or new.hypothesis_status is distinct from expected_hypothesis_status
     or new.report->>'decision' is distinct from expected_report_decision then
    raise exception
      'forward report semantics conflict: confirmation=%, report=%, outcome=%, hypothesis=%',
      confirmation_decision, new.decision, outcome_decision,
      new.hypothesis_status;
  end if;
  return new;
end
$$;

create trigger intraday_forward_report_revision_semantic_guard
before insert on quant.intraday_forward_report_revisions
for each row execute function quant.validate_intraday_forward_report_revision();

create or replace function quant.validate_intraday_forward_qa_handoff()
returns trigger
language plpgsql
set search_path = pg_catalog, quant
as $$
begin
  if not exists (
    select 1
      from quant.intraday_forward_report_revisions report
     where report.report_revision_id = new.report_revision_id
       and report.forward_confirmation_id = new.forward_confirmation_id
       and report.experiment_id = new.experiment_id
       and report.decision = 'PASS'
       and report.hypothesis_status = 'SUPPORTED'
  ) then
    raise exception
      'QA reproduction handoff requires a matching PASS forward report';
  end if;
  return new;
end
$$;

create trigger intraday_forward_qa_handoff_pass_guard
before insert on quant.intraday_forward_qa_handoffs
for each row execute function quant.validate_intraday_forward_qa_handoff();

-- Triggers protect future writes.  Fail the migration rather than blessing any
-- contradictory rows written in the interval after the preceding migration
-- was deployed and before these guards became active.
do $semantic_audit$
begin
  if exists (
    select 1
      from research.experiment_outcome_revisions outcome
      left join research.experiment_outcomes base
        on base.outcome_id = outcome.base_outcome_id
       and base.experiment_id = outcome.experiment_id::text
      left join quant.intraday_forward_confirmations confirmation
        on confirmation.forward_confirmation_id =
           outcome.forward_confirmation_id
      left join quant.intraday_experiment_rungs rung
        on rung.experiment_rung_id = confirmation.experiment_rung_id
       and rung.experiment_id = outcome.experiment_id
       and rung.rung = 'FORWARD'
     where base.outcome_id is null
        or confirmation.forward_confirmation_id is null
        or rung.experiment_rung_id is null
        or outcome.decision is distinct from case confirmation.decision
             when 'PASS' then 'SUBMIT_TO_QA'
             when 'FAIL' then 'REJECT'
             when 'INCONCLUSIVE' then 'GATE_HOLD'
             else null end
  ) then
    raise exception
      'existing forward outcome revision lacks complete semantic identity';
  end if;

  if exists (
    select 1
      from quant.intraday_forward_report_revisions report
      left join quant.intraday_forward_confirmations confirmation
        on confirmation.forward_confirmation_id =
           report.forward_confirmation_id
      left join quant.intraday_experiment_rungs rung
        on rung.experiment_rung_id = confirmation.experiment_rung_id
       and rung.experiment_id = report.experiment_id
       and rung.rung = 'FORWARD'
      left join research.experiment_outcome_revisions outcome
        on outcome.outcome_revision_id = report.outcome_revision_id
       and outcome.forward_confirmation_id = report.forward_confirmation_id
       and outcome.experiment_id = report.experiment_id
     where confirmation.forward_confirmation_id is null
        or rung.experiment_rung_id is null
        or outcome.outcome_revision_id is null
        or report.decision is distinct from confirmation.decision
        or outcome.decision is distinct from case confirmation.decision
             when 'PASS' then 'SUBMIT_TO_QA'
             when 'FAIL' then 'REJECT'
             when 'INCONCLUSIVE' then 'GATE_HOLD'
             else null end
        or report.hypothesis_status is distinct from case confirmation.decision
             when 'PASS' then 'SUPPORTED'
             when 'FAIL' then 'REJECTED'
             when 'INCONCLUSIVE' then 'INCONCLUSIVE'
             else null end
        or report.report->>'decision' is distinct from case confirmation.decision
             when 'PASS' then 'SUBMIT_TO_QA'
             when 'FAIL' then 'REJECT'
             when 'INCONCLUSIVE' then 'HOLD'
             else null end
  ) then
    raise exception
      'existing forward publication violates semantic decision mapping';
  end if;

  if exists (
    select 1
      from quant.intraday_forward_qa_handoffs handoff
      left join quant.intraday_forward_report_revisions report
        on report.report_revision_id = handoff.report_revision_id
       and report.forward_confirmation_id = handoff.forward_confirmation_id
       and report.experiment_id = handoff.experiment_id
     where report.report_revision_id is null
        or report.decision <> 'PASS'
        or report.hypothesis_status <> 'SUPPORTED'
  ) then
    raise exception
      'existing QA handoff is not backed by a PASS forward report';
  end if;
end
$semantic_audit$;

-- Preserve one aggregate contribution per current outcome even when it has no
-- lesson codes.  The previous CROSS JOIN removed an empty array entirely and
-- multiplied outcome counts when an outcome contained more than one lesson.
create or replace view research.v_trial_family_status as
with last_out as (
    select distinct on (trial_family_id)
           outcome_id, trial_family_id, decision, decided_at, lesson_codes,
           root_cause, notes, oos_summary, experiment_id
      from research.v_current_experiment_outcomes
     where trial_family_id is not null
     order by trial_family_id, decided_at desc, outcome_id desc
), agg as (
    select outcome.trial_family_id,
           count(distinct outcome.outcome_id) as outcomes,
           count(distinct outcome.outcome_id) filter (
             where outcome.decision like 'REJECT%') as rejects,
           count(distinct outcome.outcome_id) filter (
             where outcome.decision = 'GATE_HOLD') as holds,
           count(distinct outcome.outcome_id) filter (
             where outcome.decision in
               ('PROMOTED', 'SUPPORTED', 'SUBMIT_TO_QA')) as advanced,
           min(outcome.decided_at) as first_decided,
           max(outcome.decided_at) as last_decided,
           array_agg(distinct lesson.code) filter (
             where lesson.code is not null) as all_lessons
      from research.v_current_experiment_outcomes outcome
      left join lateral unnest(
        coalesce(outcome.lesson_codes, '{}'::text[])
      ) as lesson(code) on true
     where outcome.trial_family_id is not null
     group by outcome.trial_family_id
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

revoke all on function quant.validate_intraday_outcome_revision()
  from public;
revoke all on function quant.validate_intraday_forward_report_revision()
  from public;
revoke all on function quant.validate_intraday_forward_qa_handoff()
  from public;
grant execute on function quant.validate_intraday_outcome_revision()
  to svc_quant, service_role;
grant execute on function quant.validate_intraday_forward_report_revision()
  to svc_quant, service_role;
grant execute on function quant.validate_intraday_forward_qa_handoff()
  to svc_quant, service_role;

comment on function quant.validate_intraday_forward_report_revision() is
  'Rejects semantically inconsistent confirmation, outcome, report, and hypothesis decisions.';
comment on function quant.validate_intraday_forward_qa_handoff() is
  'Allows QA reproduction handoffs only for a matching PASS forward report.';

commit;
