begin;

-- A forward PASS is a request for independent reproduction, not final support.
-- A hypothesis may own more than one experiment, so one result must never win
-- merely because its INSERT happened last.  Aggregate every PASS publication
-- with the fail-closed precedence FAIL > pending/INCONCLUSIVE > all PASS.
create or replace function
  audit.intraday_forward_qa_hypothesis_authority(p_hypothesis_id uuid)
returns table (
  status text,
  authority_at timestamptz,
  pass_report_count bigint,
  completed_result_count bigint
)
language sql
security definer
set search_path = pg_catalog, audit, quant
as $$
  with pass_reports as (
    select report.report_revision_id,
           report.outcome_revision_id,
           report.published_at
      from quant.intraday_forward_report_revisions report
      join quant.experiments experiment
        on experiment.experiment_id = report.experiment_id
     where experiment.hypothesis_id = p_hypothesis_id
       and report.decision = 'PASS'
  ), resolved as (
    select report.*,
           result.verdict,
           result.completed_at
      from pass_reports report
      left join lateral (
        select candidate.verdict, candidate.completed_at
          from audit.intraday_forward_reproduction_results candidate
         where candidate.report_revision_id = report.report_revision_id
           and candidate.outcome_revision_id = report.outcome_revision_id
         order by candidate.completed_at desc,
                  candidate.reproduction_result_id desc
         limit 1
      ) result on true
  )
  select case
           when count(*) filter (where verdict = 'FAIL') > 0
             then 'REJECTED'
           when count(*) filter (
                  where verdict is null or verdict = 'INCONCLUSIVE') > 0
             then 'INCONCLUSIVE'
           when count(*) filter (where verdict = 'PASS') = count(*)
             then 'SUPPORTED'
           else 'INCONCLUSIVE'
         end,
         max(greatest(published_at, completed_at)),
         count(*)::bigint,
         count(verdict)::bigint
    from resolved
  having count(*) > 0
$$;

-- Both a new PASS report (which creates a pending QA obligation) and a new QA
-- result re-evaluate the whole hypothesis.  Locking the lifecycle row before
-- the aggregate read serializes independent experiment verdicts for the same
-- hypothesis.
create or replace function audit.apply_intraday_forward_qa_authority()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, audit, quant
as $$
declare
  v_hypothesis_id uuid;
  v_current_status text;
  v_target_status text;
begin
  if tg_table_schema = 'quant'
     and tg_table_name = 'intraday_forward_report_revisions' then
    select experiment.hypothesis_id
      into v_hypothesis_id
      from quant.experiments experiment
     where experiment.experiment_id = new.experiment_id;
  else
    select experiment.hypothesis_id
      into v_hypothesis_id
      from quant.intraday_forward_report_revisions report
      join quant.experiments experiment
        on experiment.experiment_id = report.experiment_id
     where report.report_revision_id = new.report_revision_id
       and report.outcome_revision_id = new.outcome_revision_id
       and report.decision = 'PASS';
  end if;
  if not found then
    raise exception
      'QA authority event lacks its PASS forward publication';
  end if;

  select hypothesis.status
    into v_current_status
    from quant.hypotheses hypothesis
   where hypothesis.hypothesis_id = v_hypothesis_id
   for update;
  if not found then
    raise exception 'QA authority hypothesis no longer exists';
  end if;
  if v_current_status = 'ARCHIVED' then
    return new;
  end if;

  select authority.status
    into v_target_status
    from audit.intraday_forward_qa_hypothesis_authority(
           v_hypothesis_id) authority;
  if v_target_status is null then
    raise exception 'QA authority aggregate is unexpectedly empty';
  end if;

  update quant.hypotheses hypothesis
     set status = v_target_status,
         status_changed_at = clock_timestamp()
   where hypothesis.hypothesis_id = v_hypothesis_id
     and hypothesis.status is distinct from v_target_status;
  return new;
end
$$;

-- Old publishers projected report.hypothesis_status=SUPPORTED into the mutable
-- lifecycle after creating the handoff.  The report column remains immutable
-- historical schema, but no UPDATE may expose SUPPORTED while aggregate QA is
-- pending, inconclusive, or failed.
create or replace function audit.guard_intraday_forward_qa_support()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, audit, quant
as $$
declare
  v_authoritative_status text;
begin
  if new.status = 'SUPPORTED' then
    select authority.status
      into v_authoritative_status
      from audit.intraday_forward_qa_hypothesis_authority(
             new.hypothesis_id) authority;
    if v_authoritative_status is not null
       and v_authoritative_status <> 'SUPPORTED' then
      raise exception
        'hypothesis support is blocked by aggregate forward QA status %',
        v_authoritative_status;
    end if;
  end if;
  return new;
end
$$;

revoke all on function
  audit.intraday_forward_qa_hypothesis_authority(uuid),
  audit.apply_intraday_forward_qa_authority(),
  audit.guard_intraday_forward_qa_support()
from public, anon, authenticated, service_role, svc_quant,
     svc_qa_worker, svc_audit_api, svc_qa_reproducer;

drop trigger if exists intraday_forward_reproduction_verdict_authority
  on audit.intraday_forward_reproduction_results;
drop function if exists audit.apply_intraday_forward_reproduction_verdict();
create trigger intraday_forward_reproduction_verdict_authority
after insert on audit.intraday_forward_reproduction_results
for each row execute function audit.apply_intraday_forward_qa_authority();

drop trigger if exists intraday_forward_report_qa_pending_authority
  on quant.intraday_forward_report_revisions;
create trigger intraday_forward_report_qa_pending_authority
after insert on quant.intraday_forward_report_revisions
for each row when (new.decision = 'PASS')
execute function audit.apply_intraday_forward_qa_authority();

drop trigger if exists intraday_forward_qa_support_guard
  on quant.hypotheses;
create trigger intraday_forward_qa_support_guard
before update of status on quant.hypotheses
for each row execute function audit.guard_intraday_forward_qa_support();

-- Repair all pre-trigger results and optimistic legacy PASS publications in
-- one deterministic row per hypothesis.  This also keeps any hypothesis with
-- one pending report INCONCLUSIVE even when another experiment already passed.
with governed_hypotheses as (
  select distinct experiment.hypothesis_id
    from quant.intraday_forward_report_revisions report
    join quant.experiments experiment
      on experiment.experiment_id = report.experiment_id
   where report.decision = 'PASS'
), authority as (
  select governed.hypothesis_id,
         aggregate.status,
         aggregate.authority_at
    from governed_hypotheses governed
    cross join lateral
      audit.intraday_forward_qa_hypothesis_authority(
        governed.hypothesis_id) aggregate
)
update quant.hypotheses hypothesis
   set status = authority.status,
       status_changed_at = greatest(
         hypothesis.status_changed_at, authority.authority_at)
  from authority
 where hypothesis.hypothesis_id = authority.hypothesis_id
   and hypothesis.status <> 'ARCHIVED'
   and hypothesis.status is distinct from authority.status;

-- Mask forward performance until QA is authoritative. BLOCKED is deliberately
-- neither a positive nor negative scientific label: deployment-local runtime
-- loss must not teach the evolutionary memory that the formula won or lost.
create or replace view research.v_current_experiment_outcomes
with (security_barrier = true) as
with canonical_base as (
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
       case
         when revision.decision = 'SUBMIT_TO_QA' then
           case qa_result.verdict
             when 'PASS' then 'SUBMIT_TO_QA'
             when 'FAIL' then 'REJECT'
             else 'BLOCKED'
           end
         else coalesce(revision.decision, base.decision)
       end as decision,
       case
         when revision.decision = 'SUBMIT_TO_QA'
              and qa_result.completed_at is not null
           then qa_result.completed_at
         else coalesce(revision.decided_at, base.decided_at)
       end as decided_at,
       base.proposal_id,
       case
         when revision.decision = 'SUBMIT_TO_QA'
              and qa_result.verdict = 'FAIL'
           then array_append(
             coalesce(revision.failed_criteria, base.failed_criteria),
             'QA_REPRODUCTION_FAILED')
         when revision.decision = 'SUBMIT_TO_QA'
              and qa_result.verdict = 'INCONCLUSIVE'
           then array_append(
             coalesce(revision.failed_criteria, base.failed_criteria),
             'QA_REPRODUCTION_INCONCLUSIVE')
         when revision.decision = 'SUBMIT_TO_QA'
              and qa_result.verdict is null
           then array_append(
             coalesce(revision.failed_criteria, base.failed_criteria),
             'QA_REPRODUCTION_PENDING')
         else coalesce(revision.failed_criteria, base.failed_criteria)
       end as failed_criteria,
       case
         when revision.decision = 'SUBMIT_TO_QA' then
           (case
              when qa_result.verdict = 'PASS'
                then base.oos_summary || coalesce(
                  revision.oos_summary, '{}'::jsonb)
              else (base.oos_summary || coalesce(
                  revision.oos_summary, '{}'::jsonb))
                   - 'mean_net_bps_per_opportunity'
                   - 'mean_mid_markout_bps'
                   - 'sharpe'
                   - 'deflated_sharpe'
            end)
           || jsonb_build_object(
             'qa_reproduction', jsonb_build_object(
               'status', coalesce(qa_result.verdict, 'PENDING'),
               'qa_verified', coalesce(qa_result.verdict = 'PASS', false),
               'result_id', qa_result.reproduction_result_id,
               'result_fingerprint', qa_result.result_fingerprint,
               'completed_at', qa_result.completed_at,
               'promotion_authority', false
             )
           )
         else base.oos_summary || coalesce(
           revision.oos_summary, '{}'::jsonb)
       end as oos_summary,
       base.regime_concerns,
       case
         when revision.decision = 'SUBMIT_TO_QA'
              and qa_result.verdict = 'FAIL'
           then array_append(
             coalesce(revision.lesson_codes, base.lesson_codes),
             'QA_REPRODUCTION_FAILED')
         when revision.decision = 'SUBMIT_TO_QA'
              and qa_result.verdict = 'INCONCLUSIVE'
           then array_append(
             coalesce(revision.lesson_codes, base.lesson_codes),
             'QA_REPRODUCTION_INCONCLUSIVE')
         else coalesce(revision.lesson_codes, base.lesson_codes)
       end as lesson_codes,
       coalesce(revision.notes, base.notes)
         || case
              when revision.decision <> 'SUBMIT_TO_QA' then ''
              when qa_result.verdict = 'PASS'
                then '; independent QA reproduction PASS'
              when qa_result.verdict = 'FAIL'
                then '; independent QA reproduction FAIL'
              when qa_result.verdict = 'INCONCLUSIVE'
                then '; independent QA reproduction INCONCLUSIVE'
              else '; independent QA reproduction PENDING'
            end as notes,
       coalesce(
         qa_result.completed_at,
         revision.revised_at,
         base.created_at) as created_at,
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
  ) revision on true
  left join lateral (
    select result.*
      from audit.intraday_forward_reproduction_results result
     where result.outcome_revision_id = revision.outcome_revision_id
       and result.report_revision_id = (
         select report.report_revision_id
           from quant.intraday_forward_report_revisions report
          where report.outcome_revision_id = revision.outcome_revision_id
          limit 1
       )
     order by result.completed_at desc, result.reproduction_result_id desc
     limit 1
  ) qa_result on true;

comment on function
  audit.intraday_forward_qa_hypothesis_authority(uuid) is
  'Aggregates every PASS forward report for one hypothesis with deterministic FAIL > pending/INCONCLUSIVE > all PASS precedence.';
comment on function audit.apply_intraday_forward_qa_authority() is
  'Reconciles a non-archived hypothesis after a PASS report or immutable independent QA result; ARCHIVED remains monotonic.';
comment on function audit.guard_intraday_forward_qa_support() is
  'Rejects optimistic SUPPORTED lifecycle updates while aggregate independent forward QA is not fully PASS.';
comment on view research.v_current_experiment_outcomes is
  'Current outcome projection. Forward PASS remains BLOCKED and alpha-performance-masked until independent QA PASS; QA FAIL becomes REJECT and remains performance-masked; QA INCONCLUSIVE remains BLOCKED.';

commit;
