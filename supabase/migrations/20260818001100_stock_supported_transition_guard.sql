begin;

-- A QA result is immutable evidence about the bundle that the reproducer
-- claimed.  Promotion, however, is a decision made now.  Re-check the exact
-- FORWARD rung against the current reference plane at that decision boundary
-- so a product reclassification after claim cannot become stock alpha.
create or replace function
  audit.intraday_forward_rung_has_current_stock_scope(
    p_experiment_rung_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, audit, quant, reference
as $$
  select coalesce((
    select forward_rung.rung = 'FORWARD'
       and forward_rung.evidence_purpose = 'INDEPENDENT_FORWARD'
       and forward_rung.planned_session_count >= 20
       and cardinality(forward_rung.planned_session_dates) =
           forward_rung.planned_session_count
       and cardinality(forward_rung.planned_session_dates) > 0
       and (
         select count(distinct planned_session.session_date)
           from unnest(forward_rung.planned_session_dates)
                planned_session(session_date)
       ) = forward_rung.planned_session_count
       and forward_rung.planned_instrument_count >= 1
       and cardinality(forward_rung.planned_instrument_ids) =
           forward_rung.planned_instrument_count
       and (
         select count(distinct planned_instrument.instrument_id)
           from unnest(forward_rung.planned_instrument_ids)
                planned_instrument(instrument_id)
       ) = forward_rung.planned_instrument_count
       and not exists (
         select 1
           from unnest(forward_rung.planned_instrument_ids)
                planned_instrument(instrument_id)
           cross join unnest(forward_rung.planned_session_dates)
                planned_session(session_date)
           left join reference.instruments instrument
             on instrument.instrument_id = planned_instrument.instrument_id
          where instrument.instrument_id is null
             or coalesce(upper(instrument.instrument_type), '') <> 'STOCK'
             or coalesce(upper(instrument.asset_class), '') <> 'EQUITY'
             or coalesce(upper(instrument.market), '') <> 'KRX'
             or coalesce(upper(instrument.status), '') <> 'ACTIVE'
             or lower(coalesce(instrument.metadata->>'is_spac', 'false')) =
                'true'
             or (instrument.listed_from is not null
                 and instrument.listed_from > planned_session.session_date)
             or (instrument.listed_to is not null
                 and instrument.listed_to < planned_session.session_date)
       )
      from quant.intraday_experiment_rungs forward_rung
     where forward_rung.experiment_rung_id = p_experiment_rung_id
  ), false)
$$;

-- Lane detection is deliberately broader than one mutable JSON label.  Once a
-- hypothesis owns an intraday AST experiment or rung, it cannot evade the
-- independent forward-QA authority by dropping/misspelling research_lane.
create or replace function audit.hypothesis_uses_intraday_lane(
  p_hypothesis_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, audit, quant
as $$
  select coalesce((
           select upper(coalesce(
                    nullif(hypothesis.expected_edge->>'research_lane', ''),
                    '')) = 'INTRADAY_EVENT'
             from quant.hypotheses hypothesis
            where hypothesis.hypothesis_id = p_hypothesis_id
         ), false)
         or exists (
           select 1
             from quant.experiments experiment
            where experiment.hypothesis_id = p_hypothesis_id
              and (
                experiment.config ? 'intraday_signal_expr'
                or upper(coalesce(
                     nullif(experiment.config->>'research_lane', ''), '')) =
                   'INTRADAY_EVENT'
                or exists (
                  select 1
                    from quant.intraday_experiment_rungs rung
                   where rung.experiment_id = experiment.experiment_id
                )
              )
         )
$$;

-- Python freezes daily plans and evidence identities with
-- json.dumps(..., sort_keys=True, separators=(',', ':'), ensure_ascii=True)
-- before hashing the UTF-8 bytes.  Reproduce Python's string escaping,
-- including UTF-16 surrogate pairs for non-BMP characters, before recursively
-- serializing JSONB without insignificant spaces.
create or replace function audit.python_ascii_json_string(p_value text)
returns text
language plpgsql
immutable
strict
security invoker
set search_path = pg_catalog, audit
as $$
declare
  v_result text := '"';
  v_character text;
  v_codepoint integer;
  v_non_bmp integer;
  v_index integer;
begin
  for v_index in 1..char_length(p_value) loop
    v_character := substr(p_value, v_index, 1);
    v_codepoint := ascii(v_character);
    case v_codepoint
      when 8 then v_result := v_result || chr(92) || 'b';
      when 9 then v_result := v_result || chr(92) || 't';
      when 10 then v_result := v_result || chr(92) || 'n';
      when 12 then v_result := v_result || chr(92) || 'f';
      when 13 then v_result := v_result || chr(92) || 'r';
      when 34 then v_result := v_result || chr(92) || '"';
      when 92 then v_result := v_result || chr(92) || chr(92);
      else
        if v_codepoint between 32 and 126 then
          v_result := v_result || v_character;
        elsif v_codepoint <= 65535 then
          v_result := v_result || chr(92) || 'u' ||
            lpad(lower(to_hex(v_codepoint)), 4, '0');
        else
          v_non_bmp := v_codepoint - 65536;
          v_result := v_result || chr(92) || 'u' ||
            lpad(lower(to_hex(55296 + v_non_bmp / 1024)), 4, '0') ||
            chr(92) || 'u' ||
            lpad(lower(to_hex(56320 + v_non_bmp % 1024)), 4, '0');
        end if;
    end case;
  end loop;
  return v_result || '"';
end
$$;

create or replace function audit.canonical_jsonb_text(p_value jsonb)
returns text
language plpgsql
immutable
strict
security invoker
set search_path = pg_catalog, audit
as $$
declare
  v_result text;
begin
  case jsonb_typeof(p_value)
    when 'object' then
      select '{' || coalesce(string_agg(
               audit.python_ascii_json_string(member.key) || ':' ||
               audit.canonical_jsonb_text(member.value),
               ',' order by member.key), '') || '}'
        into v_result
        from jsonb_each(p_value) member(key, value);
      return v_result;
    when 'array' then
      select '[' || coalesce(string_agg(
               audit.canonical_jsonb_text(element.value),
               ',' order by element.ordinality), '') || ']'
        into v_result
        from jsonb_array_elements(p_value) with ordinality
             element(value, ordinality);
      return v_result;
    when 'string' then
      return audit.python_ascii_json_string(p_value #>> '{}');
    else
      return p_value::text;
  end case;
end
$$;

create or replace function audit.canonical_jsonb_sha256(p_value jsonb)
returns text
language sql
immutable
strict
security invoker
set search_path = pg_catalog, audit
as $$
  select encode(
           sha256(convert_to(audit.canonical_jsonb_text(p_value), 'UTF8')),
           'hex')
$$;

-- Daily support requires reusable performance evidence, not merely a mutable
-- lifecycle label.  Bind every expected walk-forward window to one complete
-- metric identity and an immutable universe whose *current* members are all
-- ordinary active KRX equities.  Missing/legacy identity fails closed.
create or replace function audit.experiment_has_governed_daily_stock_evidence(
  p_experiment_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, audit, quant, reference
as $$
  select exists (
    select 1
      from quant.experiments experiment
      join quant.dataset_manifests dataset
        on dataset.dataset_id = experiment.dataset_id
      cross join lateral (
        select audit.canonical_jsonb_sha256(
                 experiment.split_policy -
                   'evaluation_plan_fingerprint')
                 as evaluation_plan_fingerprint,
               audit.canonical_jsonb_sha256(coalesce((
                 select jsonb_agg(
                          jsonb_build_object(
                            'window', coalesce(
                              planned_window.window_spec->>'window', ''),
                            'start_session', coalesce(
                              planned_window.window_spec->>'test_start', ''),
                            'end_session', coalesce(
                              planned_window.window_spec->>'test_end', ''))
                          order by planned_window.ordinality)
                   from jsonb_array_elements(
                          case
                            when jsonb_typeof(
                                   experiment.split_policy->'windows') =
                                 'array'
                              then experiment.split_policy->'windows'
                            else '[]'::jsonb
                          end) with ordinality
                        planned_window(window_spec, ordinality)
               ), '[]'::jsonb)) as session_boundary_fingerprint
      ) frozen_plan_identity
      cross join lateral (
        select audit.canonical_jsonb_sha256(coalesce(
                 jsonb_agg(to_jsonb(governed_member.instrument_id::text)
                           order by governed_member.instrument_id),
                 '[]'::jsonb)) as instrument_ids_fingerprint
          from quant.universe_members governed_member
         where governed_member.universe_version_id =
               dataset.universe_version_id
      ) governed_universe_identity
      cross join lateral (
        select audit.canonical_jsonb_sha256(jsonb_build_object(
                 'dataset_id', experiment.dataset_id::text,
                 'dataset_content_hash', dataset.content_hash,
                 'universe_version_id', dataset.universe_version_id::text,
                 'cost_model_version', experiment.cost_model_version,
                 'evaluation_scope', 'DAILY_WALK_FORWARD',
                 'evaluation_plan_fingerprint',
                   frozen_plan_identity.evaluation_plan_fingerprint,
                 'source_content_fingerprint', dataset.content_hash,
                 'instrument_ids_fingerprint',
                   governed_universe_identity.instrument_ids_fingerprint,
                 'session_boundary_fingerprint',
                   frozen_plan_identity.session_boundary_fingerprint,
                 'asset_class', 'EQUITY',
                 'asset_scope', 'KRX_ACTIVE_STOCK_ONLY',
                 'stock_universe_contract_version',
                   'krx-active-stock-only-v1')) as evaluation_fingerprint
      ) governed_evaluation_identity
     where experiment.experiment_id = p_experiment_id
       and experiment.status = 'COMPLETED'
       and not (
         experiment.config ? 'intraday_signal_expr'
         or upper(coalesce(
              nullif(experiment.config->>'research_lane', ''), '')) =
            'INTRADAY_EVENT'
       )
       and experiment.split_policy->>'policy' =
           'walk-forward-rolling-6m'
       and experiment.split_policy->>'plan_version' =
           'daily-walk-forward-plan-v1'
       and experiment.split_policy->>'evaluation_scope' =
           'DAILY_WALK_FORWARD'
       and experiment.split_policy->>'evaluation_plan_fingerprint' ~
           '^[0-9a-f]{64}$'
       and experiment.split_policy->>'evaluation_plan_fingerprint' =
           frozen_plan_identity.evaluation_plan_fingerprint
       and experiment.split_policy->>'session_boundary_fingerprint' ~
           '^[0-9a-f]{64}$'
       and experiment.split_policy->>'session_boundary_fingerprint' =
           frozen_plan_identity.session_boundary_fingerprint
       and experiment.split_policy->>'dataset_content_hash' =
           dataset.content_hash
       and experiment.split_policy->>'cost_model_version' =
           experiment.cost_model_version
       and experiment.split_policy->>'asset_scope' =
           'KRX_ACTIVE_STOCK_ONLY'
       and experiment.split_policy->>'stock_universe_contract_version' =
           'krx-active-stock-only-v1'
       and nullif(
             btrim(experiment.split_policy->>'walk_forward_code_version'),
             '') is not null
       and jsonb_typeof(experiment.split_policy->'cost_model') = 'object'
       and experiment.split_policy->'cost_model' <> '{}'::jsonb
       and experiment.split_policy->'cost_model'->>'version' =
           experiment.cost_model_version
       and dataset.universe_version_id is not null
       and dataset.content_hash ~ '^[0-9a-f]{64}$'
       and exists (
         select 1
           from quant.universe_members eligible_member
          where eligible_member.universe_version_id =
                dataset.universe_version_id
       )
       and not exists (
         select 1
           from quant.universe_members governed_member
           left join reference.instruments instrument
             on instrument.instrument_id = governed_member.instrument_id
          where governed_member.universe_version_id =
                dataset.universe_version_id
            and (
              instrument.instrument_id is null
              or coalesce(upper(instrument.instrument_type), '') <> 'STOCK'
              or coalesce(upper(instrument.asset_class), '') <> 'EQUITY'
              or coalesce(upper(instrument.market), '') <> 'KRX'
              or coalesce(upper(instrument.status), '') <> 'ACTIVE'
              or lower(coalesce(
                   instrument.metadata->>'is_spac', 'false')) = 'true'
            )
       )
       and jsonb_typeof(experiment.split_policy->'windows') = 'array'
       and jsonb_array_length(
             case
               when jsonb_typeof(experiment.split_policy->'windows') =
                    'array'
                 then experiment.split_policy->'windows'
               else '[]'::jsonb
             end) > 0
       and (
         select count(*) = count(distinct window_spec->>'window')
                and bool_and(
                  jsonb_typeof(window_spec) = 'object'
                  and nullif(btrim(window_spec->>'window'), '') is not null
                  and window_spec->>'window' <> 'SUMMARY'
                  and coalesce(window_spec->>'test_start', '') ~
                      '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                  and coalesce(window_spec->>'test_end', '') ~
                      '^[0-9]{4}-[0-9]{2}-[0-9]{2}$'
                  and pg_input_is_valid(window_spec->>'test_start', 'date')
                  and pg_input_is_valid(window_spec->>'test_end', 'date')
                  and case
                        when pg_input_is_valid(
                               window_spec->>'test_start', 'date')
                             and pg_input_is_valid(
                               window_spec->>'test_end', 'date')
                          then (window_spec->>'test_start')::date <=
                               (window_spec->>'test_end')::date
                        else false
                      end)
           from jsonb_array_elements(
                  case
                    when jsonb_typeof(
                           experiment.split_policy->'windows') = 'array'
                      then experiment.split_policy->'windows'
                    else '[]'::jsonb
                  end) expected_window(window_spec)
       )
       and not exists (
         select 1
           from quant.universe_members governed_member
           join reference.instruments instrument
             on instrument.instrument_id = governed_member.instrument_id
           cross join jsonb_array_elements(
             case
               when jsonb_typeof(experiment.split_policy->'windows') =
                    'array'
                 then experiment.split_policy->'windows'
               else '[]'::jsonb
             end) expected_window(window_spec)
           cross join lateral (
             select case
                      when pg_input_is_valid(
                             expected_window.window_spec->>'test_start',
                             'date')
                        then (expected_window.window_spec->>'test_start')::date
                      else null::date
                    end as test_start,
                    case
                      when pg_input_is_valid(
                             expected_window.window_spec->>'test_end',
                             'date')
                        then (expected_window.window_spec->>'test_end')::date
                      else null::date
                    end as test_end
           ) expected_bounds
          where governed_member.universe_version_id =
                dataset.universe_version_id
            and (
              expected_bounds.test_start is null
              or expected_bounds.test_end is null
              or (instrument.listed_from is not null
                  and instrument.listed_from > expected_bounds.test_start)
              or (instrument.listed_to is not null
                  and instrument.listed_to < expected_bounds.test_end)
            )
       )
       and not exists (
         select 1
           from jsonb_array_elements(
                  case
                    when jsonb_typeof(
                           experiment.split_policy->'windows') = 'array'
                      then experiment.split_policy->'windows'
                    else '[]'::jsonb
                  end)
                expected_window(window_spec)
          where jsonb_typeof(expected_window.window_spec) <> 'object'
             or nullif(expected_window.window_spec->>'window', '') is null
             or nullif(expected_window.window_spec->>'test_start', '') is null
             or nullif(expected_window.window_spec->>'test_end', '') is null
             or (
               select count(*)
                 from quant.experiment_metrics evidence_metric
                where evidence_metric.experiment_id = experiment.experiment_id
                  and evidence_metric.split = 'WALK_FORWARD'
                  and evidence_metric.metric = 'total_return'
                  and evidence_metric.value is not null
                  and evidence_metric.value::text not in
                      ('NaN', 'Infinity', '-Infinity')
                  and evidence_metric.cost_model_version =
                      experiment.cost_model_version
                  and evidence_metric.dimensions->>'evaluation_scope' =
                      'DAILY_WALK_FORWARD'
                  and evidence_metric.dimensions->>
                        'evaluation_identity_complete' = 'true'
                  and evidence_metric.dimensions->>'asset_class' = 'EQUITY'
                  and evidence_metric.dimensions->>'asset_scope' =
                      'KRX_ACTIVE_STOCK_ONLY'
                  and evidence_metric.dimensions->>
                        'stock_universe_contract_version' =
                      'krx-active-stock-only-v1'
                  and evidence_metric.dimensions->>'cost_model_version' =
                      experiment.cost_model_version
                  and evidence_metric.dimensions->>'dataset_id' =
                      experiment.dataset_id::text
                  and evidence_metric.dimensions->>'dataset_content_hash' =
                      dataset.content_hash
                  and evidence_metric.dimensions->>
                        'source_content_fingerprint' = dataset.content_hash
                  and evidence_metric.dimensions->>'universe_version_id' =
                      dataset.universe_version_id::text
                  and evidence_metric.dimensions->>'window' =
                      expected_window.window_spec->>'window'
                  and evidence_metric.dimensions->>'start_session' =
                      expected_window.window_spec->>'test_start'
                  and evidence_metric.dimensions->>'end_session' =
                      expected_window.window_spec->>'test_end'
                  and evidence_metric.dimensions->>'evaluation_fingerprint' ~
                      '^[0-9a-f]{64}$'
                  and evidence_metric.dimensions->>'evaluation_fingerprint' =
                      governed_evaluation_identity.evaluation_fingerprint
                  and evidence_metric.dimensions->>
                        'evaluation_plan_fingerprint' =
                      frozen_plan_identity.evaluation_plan_fingerprint
                  and evidence_metric.dimensions->>
                        'instrument_ids_fingerprint' =
                      governed_universe_identity.instrument_ids_fingerprint
                  and evidence_metric.dimensions->>
                        'session_boundary_fingerprint' =
                      frozen_plan_identity.session_boundary_fingerprint
             ) <> 1
       )
       and (
         select count(*)
           from quant.experiment_metrics claimed_daily_metric
          where claimed_daily_metric.experiment_id =
                experiment.experiment_id
            and claimed_daily_metric.split = 'WALK_FORWARD'
            and claimed_daily_metric.metric = 'total_return'
            and claimed_daily_metric.dimensions->>'evaluation_scope' =
                'DAILY_WALK_FORWARD'
            and nullif(
                  claimed_daily_metric.dimensions->>'window', '') is not null
            and claimed_daily_metric.dimensions->>'window' <> 'SUMMARY'
       ) = jsonb_array_length(
             case
               when jsonb_typeof(experiment.split_policy->'windows') =
                    'array'
                 then experiment.split_policy->'windows'
               else '[]'::jsonb
             end)
       and (
         select count(*)
           from quant.experiment_metrics evidence_metric
          where evidence_metric.experiment_id = experiment.experiment_id
            and evidence_metric.split = 'WALK_FORWARD'
            and evidence_metric.metric = 'total_return'
            and evidence_metric.value is not null
            and evidence_metric.value::text not in
                ('NaN', 'Infinity', '-Infinity')
            and evidence_metric.cost_model_version =
                experiment.cost_model_version
            and evidence_metric.dimensions->>'evaluation_scope' =
                'DAILY_WALK_FORWARD'
            and evidence_metric.dimensions->>
                  'evaluation_identity_complete' = 'true'
            and evidence_metric.dimensions->>'asset_class' = 'EQUITY'
            and evidence_metric.dimensions->>'asset_scope' =
                'KRX_ACTIVE_STOCK_ONLY'
            and evidence_metric.dimensions->>
                  'stock_universe_contract_version' =
                'krx-active-stock-only-v1'
            and evidence_metric.dimensions->>'cost_model_version' =
                experiment.cost_model_version
            and evidence_metric.dimensions->>'dataset_id' =
                experiment.dataset_id::text
            and evidence_metric.dimensions->>'dataset_content_hash' =
                dataset.content_hash
            and evidence_metric.dimensions->>'source_content_fingerprint' =
                dataset.content_hash
            and evidence_metric.dimensions->>'universe_version_id' =
                dataset.universe_version_id::text
            and evidence_metric.dimensions->>'evaluation_fingerprint' ~
                '^[0-9a-f]{64}$'
            and evidence_metric.dimensions->>'evaluation_fingerprint' =
                governed_evaluation_identity.evaluation_fingerprint
            and evidence_metric.dimensions->>'evaluation_plan_fingerprint' =
                frozen_plan_identity.evaluation_plan_fingerprint
            and evidence_metric.dimensions->>'instrument_ids_fingerprint' =
                governed_universe_identity.instrument_ids_fingerprint
            and evidence_metric.dimensions->>'session_boundary_fingerprint' =
                frozen_plan_identity.session_boundary_fingerprint
       ) = jsonb_array_length(
             case
               when jsonb_typeof(experiment.split_policy->'windows') =
                    'array'
                 then experiment.split_policy->'windows'
               else '[]'::jsonb
             end)
       and (
         select count(distinct concat_ws(
                  '|',
                  evidence_metric.dimensions->>'evaluation_fingerprint',
                  evidence_metric.dimensions->>
                    'evaluation_plan_fingerprint',
                  evidence_metric.dimensions->>'instrument_ids_fingerprint',
                  evidence_metric.dimensions->>'session_boundary_fingerprint'))
           from quant.experiment_metrics evidence_metric
          where evidence_metric.experiment_id = experiment.experiment_id
            and evidence_metric.split = 'WALK_FORWARD'
            and evidence_metric.metric = 'total_return'
            and evidence_metric.cost_model_version =
                experiment.cost_model_version
            and evidence_metric.dimensions->>'evaluation_scope' =
                'DAILY_WALK_FORWARD'
            and evidence_metric.dimensions->>
                  'evaluation_identity_complete' = 'true'
            and evidence_metric.dimensions->>'asset_scope' =
                'KRX_ACTIVE_STOCK_ONLY'
            and evidence_metric.dimensions->>'dataset_id' =
                experiment.dataset_id::text
            and evidence_metric.dimensions->>'dataset_content_hash' =
                dataset.content_hash
            and evidence_metric.dimensions->>'universe_version_id' =
                dataset.universe_version_id::text
       ) = 1
  )
$$;

-- Promotion is hypothesis-scoped, but the reusable-evidence API above must be
-- experiment-scoped.  Otherwise one valid sibling experiment could authorize
-- selection of a different malformed experiment under the same hypothesis.
create or replace function audit.hypothesis_has_governed_daily_stock_evidence(
  p_hypothesis_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, audit, quant
as $$
  select exists (
    select 1
      from quant.experiments experiment
     where experiment.hypothesis_id = p_hypothesis_id
       and audit.experiment_has_governed_daily_stock_evidence(
             experiment.experiment_id)
  )
$$;

-- svc_quant intentionally has no USAGE on the audit schema.  Expose only the
-- exact experiment-scoped boolean through its existing quant schema instead
-- of widening audit-schema visibility to unrelated functions.
create or replace function quant.experiment_has_governed_daily_stock_evidence(
  p_experiment_id uuid)
returns boolean
language sql
stable
security definer
set search_path = pg_catalog, audit, quant
as $$
  select audit.experiment_has_governed_daily_stock_evidence(
           p_experiment_id)
$$;

-- Replace the 010 aggregate without changing its public return contract.
-- Every PASS publication stays in the denominator even if its identity graph
-- is malformed; malformed/current-non-stock rows become INCONCLUSIVE rather
-- than silently disappearing through an inner join.
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
set search_path = pg_catalog, audit, quant, reference
as $$
  with pass_reports as (
    select report.report_revision_id,
           report.outcome_revision_id,
           report.published_at,
           forward_rung.experiment_rung_id,
           coalesce(
             confirmation.decision = 'PASS'
             and outcome_revision.decision = 'SUBMIT_TO_QA'
             and audit.intraday_forward_rung_has_current_stock_scope(
                   forward_rung.experiment_rung_id),
             false) as current_stock_scope_valid
      from quant.intraday_forward_report_revisions report
      join quant.experiments experiment
        on experiment.experiment_id = report.experiment_id
      left join quant.intraday_forward_confirmations confirmation
        on confirmation.forward_confirmation_id =
           report.forward_confirmation_id
      left join quant.intraday_experiment_rungs forward_rung
        on forward_rung.experiment_rung_id =
           confirmation.experiment_rung_id
       and forward_rung.experiment_id = report.experiment_id
       and forward_rung.rung = 'FORWARD'
      left join research.experiment_outcome_revisions outcome_revision
        on outcome_revision.outcome_revision_id = report.outcome_revision_id
       and outcome_revision.forward_confirmation_id =
           report.forward_confirmation_id
       and outcome_revision.experiment_id = report.experiment_id
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
                  where not current_stock_scope_valid
                     or verdict is null
                     or verdict = 'INCONCLUSIVE') > 0
             then 'INCONCLUSIVE'
           when count(*) filter (
                  where verdict = 'PASS'
                    and current_stock_scope_valid) = count(*)
             then 'SUPPORTED'
           else 'INCONCLUSIVE'
         end,
         max(greatest(published_at, completed_at)),
         count(*)::bigint,
         count(verdict)::bigint
    from resolved
  having count(*) > 0
$$;

-- This one trigger is the final lifecycle authority for both lanes.  NULL QA
-- authority is denial, not absence of a restriction.  Daily support likewise
-- requires complete governed evidence.  The same function protects raw UPDATE
-- and INSERT paths while allowing legitimate non-SUPPORTED lifecycle writes.
create or replace function audit.guard_intraday_forward_qa_support()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, audit, quant, reference
as $$
declare
  v_authoritative_status text;
  v_is_intraday boolean;
begin
  if new.status <> 'SUPPORTED' then
    return new;
  end if;

  if tg_op = 'UPDATE'
     and old.status = 'SUPPORTED'
     and (
       new.expected_edge is distinct from old.expected_edge
       or new.preregistered_at is distinct from old.preregistered_at
       or new.material_fingerprint is distinct from old.material_fingerprint
     ) then
    raise exception
      'SUPPORTED hypothesis scientific identity is immutable; demote before revision';
  end if;

  v_is_intraday :=
    upper(coalesce(
      nullif(new.expected_edge->>'research_lane', ''), '')) =
      'INTRADAY_EVENT'
    or new.expected_edge ? 'intraday_signal_expr'
    or audit.hypothesis_uses_intraday_lane(new.hypothesis_id);

  if v_is_intraday then
    select authority.status
      into v_authoritative_status
      from audit.intraday_forward_qa_hypothesis_authority(
             new.hypothesis_id) authority;

    if v_authoritative_status is distinct from 'SUPPORTED' then
      raise exception
        'hypothesis support requires all-QA-PASS and current exact KRX ACTIVE STOCK/EQUITY FORWARD scope; authority=%',
        coalesce(v_authoritative_status, 'NULL');
    end if;
  elsif not audit.hypothesis_has_governed_daily_stock_evidence(
              new.hypothesis_id) then
    raise exception
      'daily hypothesis support requires complete governed KRX ACTIVE STOCK/EQUITY walk-forward evidence';
  end if;

  return new;
end
$$;

-- A daily total-return row is part of the frozen scientific record as soon as
-- it is inserted.  Normal workers use INSERT ... ON CONFLICT DO NOTHING;
-- rewrites and deletes would let a completed or already-supported result be
-- changed without changing its experiment identity.  Once support exists,
-- adding another daily total-return claim is denied for the same reason.
create or replace function audit.guard_daily_total_return_immutability()
returns trigger
language plpgsql
security definer
set search_path = pg_catalog, audit, quant
as $$
declare
  v_is_old_governed boolean := false;
  v_is_new_governed boolean := false;
begin
  if tg_op in ('UPDATE', 'DELETE') then
    v_is_old_governed :=
      old.split = 'WALK_FORWARD'
      and old.metric = 'total_return'
      and old.dimensions->>'evaluation_scope' = 'DAILY_WALK_FORWARD';
  end if;

  if tg_op in ('INSERT', 'UPDATE') then
    v_is_new_governed :=
      new.split = 'WALK_FORWARD'
      and new.metric = 'total_return'
      and new.dimensions->>'evaluation_scope' = 'DAILY_WALK_FORWARD';
  end if;

  if (tg_op in ('UPDATE', 'DELETE') and v_is_old_governed)
     or (tg_op = 'UPDATE' and v_is_new_governed) then
    raise exception
      'governed DAILY_WALK_FORWARD total_return evidence is immutable';
  end if;

  if tg_op = 'INSERT' and v_is_new_governed and exists (
    select 1
      from quant.experiments experiment
      join quant.hypotheses hypothesis
        on hypothesis.hypothesis_id = experiment.hypothesis_id
     where experiment.experiment_id = new.experiment_id
       and hypothesis.status = 'SUPPORTED'
  ) then
    raise exception
      'cannot append DAILY_WALK_FORWARD total_return evidence after support';
  end if;

  if tg_op = 'DELETE' then
    return old;
  end if;
  return new;
end
$$;

revoke all on function
  audit.python_ascii_json_string(text),
  audit.canonical_jsonb_text(jsonb),
  audit.canonical_jsonb_sha256(jsonb),
  audit.intraday_forward_rung_has_current_stock_scope(uuid),
  audit.hypothesis_uses_intraday_lane(uuid),
  audit.experiment_has_governed_daily_stock_evidence(uuid),
  audit.hypothesis_has_governed_daily_stock_evidence(uuid),
  audit.intraday_forward_qa_hypothesis_authority(uuid),
  audit.guard_intraday_forward_qa_support(),
  audit.guard_daily_total_return_immutability(),
  quant.experiment_has_governed_daily_stock_evidence(uuid)
from public, anon, authenticated, service_role, svc_quant,
     svc_qa_worker, svc_audit_api, svc_qa_reproducer;

revoke usage on schema audit from svc_quant;

-- The quant selector gets one read-only answer for the exact experiment it is
-- considering through the schema it already uses.  It does not receive audit
-- schema visibility, canonical helpers, or the hypothesis promotion aggregate.
grant execute on function
  quant.experiment_has_governed_daily_stock_evidence(uuid)
to svc_quant;

drop trigger if exists intraday_forward_qa_support_guard
  on quant.hypotheses;
create trigger intraday_forward_qa_support_guard
before update of status, expected_edge, preregistered_at, material_fingerprint
on quant.hypotheses
for each row execute function audit.guard_intraday_forward_qa_support();

drop trigger if exists stock_supported_insert_guard
  on quant.hypotheses;
create trigger stock_supported_insert_guard
before insert on quant.hypotheses
for each row when (new.status = 'SUPPORTED')
execute function audit.guard_intraday_forward_qa_support();

drop trigger if exists daily_total_return_immutability_guard
  on quant.experiment_metrics;
create trigger daily_total_return_immutability_guard
before insert or update or delete on quant.experiment_metrics
for each row execute function audit.guard_daily_total_return_immutability();

-- Do not install a stricter transition authority while silently grandfathering
-- unsupported historical labels.  This is a read-only, fail-closed preflight:
-- operators must repair or demote invalid rows explicitly in a separate,
-- reviewed change rather than this migration rewriting scientific history.
do $stock_supported_preflight$
begin
  if exists (
    select 1
      from quant.hypotheses hypothesis
     where hypothesis.status = 'SUPPORTED'
       and not coalesce(
         case
           when audit.hypothesis_uses_intraday_lane(
                  hypothesis.hypothesis_id)
             then (
               select authority.status = 'SUPPORTED'
                 from audit.intraday_forward_qa_hypothesis_authority(
                        hypothesis.hypothesis_id) authority
             )
           else audit.hypothesis_has_governed_daily_stock_evidence(
                  hypothesis.hypothesis_id)
         end,
         false)
  ) then
    raise exception
      'pre-existing SUPPORTED hypothesis lacks current governed stock evidence';
  end if;
end
$stock_supported_preflight$;

-- Keep the one-time 009 repair durable.  NOT VALID avoids taking an access
-- exclusive validation scan while the constraint is installed; validation is
-- explicit and performs no row rewrite.  The live preflight fails closed if a
-- source regression already reintroduced an inconsistent identity.
do $spac_invariant_install$
begin
  if exists (
    select 1
      from reference.instruments instrument
     where lower(coalesce(instrument.metadata->>'is_spac', 'false')) = 'true'
       and coalesce(upper(instrument.instrument_type), '') <> 'SPAC'
  ) then
    raise exception
      'existing is_spac=true instrument is not classified as SPAC';
  end if;

  if not exists (
    select 1
      from pg_catalog.pg_constraint constraint_row
     where constraint_row.conrelid =
           'reference.instruments'::regclass
       and constraint_row.conname =
           'chk_reference_instruments_spac_identity'
  ) then
    alter table reference.instruments
      add constraint chk_reference_instruments_spac_identity
      check (
        lower(coalesce(metadata->>'is_spac', 'false')) <> 'true'
        or coalesce(upper(instrument_type), '') = 'SPAC'
      ) not valid;
  end if;
end
$spac_invariant_install$;

alter table reference.instruments
  validate constraint chk_reference_instruments_spac_identity;

comment on function
  audit.intraday_forward_rung_has_current_stock_scope(uuid) is
  'Fail-closed current reference validation of the exact planned FORWARD sessions and instruments.';
comment on function audit.hypothesis_uses_intraday_lane(uuid) is
  'Classifies intraday hypotheses from hypothesis metadata, experiment config, or durable rung ownership.';
comment on function
  audit.experiment_has_governed_daily_stock_evidence(uuid) is
  'Read-only reusable-evidence decision bound to exactly one daily experiment.';
comment on function
  audit.hypothesis_has_governed_daily_stock_evidence(uuid) is
  'Promotion aggregate requiring at least one exactly governed daily experiment.';
comment on function
  quant.experiment_has_governed_daily_stock_evidence(uuid) is
  'Least-privilege svc_quant facade for one exact governed daily experiment decision.';
comment on function
  audit.intraday_forward_qa_hypothesis_authority(uuid) is
  'Aggregates every PASS report with FAIL > invalid-scope/pending/INCONCLUSIVE > all-QA-PASS, revalidating exact current stock scope.';
comment on function audit.guard_intraday_forward_qa_support() is
  'Fail-closed final SUPPORTED transition guard for intraday QA and governed daily stock evidence.';
comment on function audit.canonical_jsonb_text(jsonb) is
  'Compact key-sorted canonical JSON serializer matching Python ensure_ascii output.';
comment on function audit.python_ascii_json_string(text) is
  'Python-compatible ensure_ascii JSON string serializer for governed evidence hashes.';
comment on function audit.canonical_jsonb_sha256(jsonb) is
  'SHA-256 of compact canonical governed JSON, matching the Python evidence contract.';
comment on function audit.guard_daily_total_return_immutability() is
  'Blocks mutation/deletion of daily total-return evidence and append after support.';
comment on constraint chk_reference_instruments_spac_identity
  on reference.instruments is
  'Rows flagged metadata.is_spac=true must use instrument_type=SPAC.';

do $stock_supported_guard_audit$
begin
  if not exists (
    select 1
      from pg_catalog.pg_trigger trigger_row
     where trigger_row.tgrelid = 'quant.hypotheses'::regclass
       and trigger_row.tgname = 'intraday_forward_qa_support_guard'
       and not trigger_row.tgisinternal
  ) or not exists (
    select 1
      from pg_catalog.pg_trigger trigger_row
     where trigger_row.tgrelid = 'quant.hypotheses'::regclass
       and trigger_row.tgname = 'stock_supported_insert_guard'
       and not trigger_row.tgisinternal
  ) or not exists (
    select 1
      from pg_catalog.pg_trigger trigger_row
     where trigger_row.tgrelid = 'quant.experiment_metrics'::regclass
       and trigger_row.tgname = 'daily_total_return_immutability_guard'
       and not trigger_row.tgisinternal
  ) then
    raise exception 'stock evidence authority trigger is missing';
  end if;

  if not exists (
    select 1
      from pg_catalog.pg_constraint constraint_row
     where constraint_row.conrelid = 'reference.instruments'::regclass
       and constraint_row.conname =
           'chk_reference_instruments_spac_identity'
       and constraint_row.convalidated
  ) then
    raise exception 'SPAC identity invariant is missing or unvalidated';
  end if;

  if not pg_catalog.has_function_privilege(
           'svc_quant',
           'quant.experiment_has_governed_daily_stock_evidence(uuid)',
           'EXECUTE')
     or pg_catalog.has_function_privilege(
           'svc_quant',
           'audit.experiment_has_governed_daily_stock_evidence(uuid)',
           'EXECUTE')
     or pg_catalog.has_function_privilege(
           'svc_quant',
           'audit.hypothesis_has_governed_daily_stock_evidence(uuid)',
           'EXECUTE')
     or pg_catalog.has_function_privilege(
           'svc_quant', 'audit.canonical_jsonb_text(jsonb)', 'EXECUTE')
     or pg_catalog.has_function_privilege(
           'svc_quant', 'audit.canonical_jsonb_sha256(jsonb)', 'EXECUTE')
     or pg_catalog.has_schema_privilege(
           'svc_quant', 'audit', 'USAGE') then
    raise exception
      'svc_quant stock evidence function privileges are not least-privilege';
  end if;
end
$stock_supported_guard_audit$;

commit;
