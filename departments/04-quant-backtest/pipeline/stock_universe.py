"""Fail-closed KRX stock-universe controls shared by every backtest lane.

Raw stores may contain ETFs, ETNs, indices, and derivatives.  A dataset or
experiment is eligible for backtesting only when every referenced instrument
is an active KRX equity with ``instrument_type=STOCK``.  Listing dates, when
present, are also enforced against each row/session date.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable, Mapping, Sequence


STOCK_UNIVERSE_VERSION = "krx-active-stock-only-v1"
STOCK_ASSET_SCOPE = "KRX_ACTIVE_STOCK_ONLY"
INTRADAY_LANE = "INTRADAY_EVENT"
INTRADAY_FULL_EVIDENCE_POLICY = "all-stock-full-replay-v1"
INTRADAY_REPORT_MANIFEST_VERSION = "intraday-governance-report-v7"


def governed_stock_dataset_sql(*, dataset_alias: str = "m",
                                require_audit: bool = False) -> str:
    """Return a fail-closed predicate for an immutable all-stock universe.

    Dataset selection may validate the authoritative universe membership even
    for a legacy manifest. Reusing its performance additionally requires the
    modern embedded audit evidence, requested with ``require_audit=True``.
    """

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", dataset_alias):
        raise ValueError("SQL aliases must be simple identifiers")
    dataset = dataset_alias
    audit = ""
    if require_audit:
        audit = f"""
      and {dataset}.quality_summary->'stock_universe'->>'asset_scope' =
          '{STOCK_ASSET_SCOPE}'
      and {dataset}.quality_summary->'stock_universe'->>
          'unknown_identity_policy' = 'FAIL_CLOSED'
      and {dataset}.quality_summary->'stock_universe'->>
          'listing_interval_policy' = 'ENFORCE_WHEN_PRESENT'
        """
    return f"""
      {dataset}.universe_version_id is not null
      {audit}
      and exists (
        select 1
          from quant.universe_members eligible_member
         where eligible_member.universe_version_id =
               {dataset}.universe_version_id
      )
      and not exists (
        select 1
          from quant.universe_members governed_member
          left join quant.current_krx_stock_instrument_identity
               governed_instrument
            on governed_instrument.instrument_id =
               governed_member.instrument_id
         where governed_member.universe_version_id =
               {dataset}.universe_version_id
           and (
             governed_instrument.instrument_id is null
             or coalesce(upper(governed_instrument.instrument_type), '') <>
                'STOCK'
             or coalesce(upper(governed_instrument.asset_class), '') <>
                'EQUITY'
             or coalesce(upper(governed_instrument.market), '') <> 'KRX'
             or coalesce(upper(governed_instrument.status), '') <> 'ACTIVE'
             or coalesce(governed_instrument.is_spac, true)
           )
      )
    """


def governed_stock_evidence_sql(*, experiment_alias: str = "e",
                                dataset_alias: str = "m",
                                hypothesis_alias: str = "h") -> str:
    """Return the fail-closed SQL predicate for reusable performance evidence.

    Trial pressure is intentionally outside this predicate.  Every attempted
    formula, including a historical mixed ETF/ETN run, remains part of the
    multiple-testing denominator.  This predicate is only for consumers that
    learn a direction, rank performance, allocate another run, or block a
    promotion from a prior outcome.

    Daily evidence needs a complete evaluation identity bound to an immutable
    universe whose current reference members are all ACTIVE KRX EQUITY/STOCK.
    Intraday evidence additionally needs the modern all-stock FULL_60 lockbox,
    all sixty durable session exposures, and a complete FULL_60 metric identity.
    Missing or legacy metadata therefore returns false instead of being
    interpreted as stock-only.
    """
    aliases = (experiment_alias, dataset_alias, hypothesis_alias)
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", alias)
           for alias in aliases):
        raise ValueError("SQL aliases must be simple identifiers")
    experiment, dataset, hypothesis = aliases
    intraday = (
        f"({experiment}.config ? 'intraday_signal_expr' or "
        f"upper(coalesce(nullif({experiment}.config->>'research_lane', ''), "
        f"nullif({hypothesis}.expected_edge->>'research_lane', ''), '')) = "
        f"'{INTRADAY_LANE}')"
    )
    # The immutable v1/v2 daily manifests predate the embedded stock audit.
    # Do not mutate them or force a byte-identical copy: the authoritative
    # universe membership plus a newly complete per-experiment evaluation
    # identity proves scope without accepting legacy metrics.
    daily_dataset = governed_stock_dataset_sql(dataset_alias=dataset)
    daily = f"""
      quant.experiment_has_governed_daily_stock_evidence(
        {experiment}.experiment_id)
      and {daily_dataset}
      and {dataset}.content_hash ~ '^[0-9a-f]{{64}}$'
      and jsonb_typeof({experiment}.split_policy->'windows') = 'array'
      and {experiment}.split_policy->>'policy' =
          'walk-forward-rolling-6m'
      and {experiment}.split_policy->>'plan_version' =
          'daily-walk-forward-plan-v1'
      and {experiment}.split_policy->>'evaluation_scope' =
          'DAILY_WALK_FORWARD'
      and {experiment}.split_policy->>'asset_scope' =
          '{STOCK_ASSET_SCOPE}'
      and {experiment}.split_policy->>'stock_universe_contract_version' =
          '{STOCK_UNIVERSE_VERSION}'
      and nullif({experiment}.split_policy->>
                 'walk_forward_code_version', '') is not null
      and {experiment}.split_policy->>'cost_model_version' =
          {experiment}.cost_model_version
      and jsonb_typeof({experiment}.split_policy->'cost_model') = 'object'
      and {experiment}.split_policy->'cost_model'->>'version' =
          {experiment}.cost_model_version
      and {experiment}.split_policy->>'evaluation_plan_fingerprint' ~
          '^[0-9a-f]{{64}}$'
      and {experiment}.split_policy->>'session_boundary_fingerprint' ~
          '^[0-9a-f]{{64}}$'
      and {experiment}.split_policy->>'dataset_content_hash' =
          {dataset}.content_hash
      and jsonb_array_length(
            case when jsonb_typeof({experiment}.split_policy->'windows') =
                           'array'
                 then {experiment}.split_policy->'windows'
                 else '[]'::jsonb end) > 0
      and (
        select count(*) = jsonb_array_length(
                 case when jsonb_typeof(
                                  {experiment}.split_policy->'windows') =
                                 'array'
                      then {experiment}.split_policy->'windows'
                      else '[]'::jsonb end)
               and count(distinct expected_window->>'window') =
                   jsonb_array_length(
                     case when jsonb_typeof(
                                      {experiment}.split_policy->'windows') =
                                     'array'
                          then {experiment}.split_policy->'windows'
                          else '[]'::jsonb end)
               and bool_and(
                     jsonb_typeof(expected_window) = 'object'
                     and nullif(expected_window->>'window', '') is not null
                     and expected_window->>'window' <> 'SUMMARY'
                     and coalesce(expected_window->>'test_start', '') ~
                         '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                     and coalesce(expected_window->>'test_end', '') ~
                         '^[0-9]{{4}}-[0-9]{{2}}-[0-9]{{2}}$'
                     and pg_input_is_valid(
                           coalesce(expected_window->>'test_start', ''),
                           'date')
                     and pg_input_is_valid(
                           coalesce(expected_window->>'test_end', ''),
                           'date')
                     and case
                           when pg_input_is_valid(
                                  coalesce(expected_window->>'test_start', ''),
                                  'date')
                            and pg_input_is_valid(
                                  coalesce(expected_window->>'test_end', ''),
                                  'date')
                           then (expected_window->>'test_start')::date <=
                                (expected_window->>'test_end')::date
                           else false
                         end)
          from jsonb_array_elements(
                 case when jsonb_typeof(
                                  {experiment}.split_policy->'windows') =
                                 'array'
                      then {experiment}.split_policy->'windows'
                      else '[]'::jsonb end) expected_window
      )
      and not exists (
        select 1
          from quant.universe_members daily_member
          cross join lateral jsonb_array_elements(
            case when jsonb_typeof(
                             {experiment}.split_policy->'windows') = 'array'
                 then {experiment}.split_policy->'windows'
                 else '[]'::jsonb end) daily_window
          left join quant.current_krx_stock_instrument_identity
               daily_instrument
            on daily_instrument.instrument_id = daily_member.instrument_id
         where daily_member.universe_version_id =
               {dataset}.universe_version_id
           and (
             daily_instrument.instrument_id is null
             or case
                  when pg_input_is_valid(
                         coalesce(daily_window->>'test_start', ''), 'date')
                   and pg_input_is_valid(
                         coalesce(daily_window->>'test_end', ''), 'date')
                  then (
                    (daily_instrument.listed_from is not null and
                     daily_instrument.listed_from >
                       (daily_window->>'test_start')::date)
                    or
                    (daily_instrument.listed_to is not null and
                     daily_instrument.listed_to <
                       (daily_window->>'test_end')::date)
                  )
                  else true
                end
           )
      )
      and (
        select count(*)
          from quant.experiment_metrics claimed_daily_metric
         where claimed_daily_metric.experiment_id =
               {experiment}.experiment_id
           and claimed_daily_metric.split = 'WALK_FORWARD'
           and claimed_daily_metric.metric = 'total_return'
           and claimed_daily_metric.dimensions->>'evaluation_scope' =
               'DAILY_WALK_FORWARD'
           and nullif(claimed_daily_metric.dimensions->>'window', '')
               is not null
           and claimed_daily_metric.dimensions->>'window' <> 'SUMMARY'
      ) = jsonb_array_length(
            case when jsonb_typeof({experiment}.split_policy->'windows') =
                           'array'
                 then {experiment}.split_policy->'windows'
                 else '[]'::jsonb end)
      and (
        select count(*) = jsonb_array_length(
                 case when jsonb_typeof(
                                  {experiment}.split_policy->'windows') =
                                 'array'
                      then {experiment}.split_policy->'windows'
                      else '[]'::jsonb end)
               and count(distinct
                         evidence_metric.experiment_metric_id) =
                   jsonb_array_length(
                     case when jsonb_typeof(
                                      {experiment}.split_policy->'windows') =
                                     'array'
                          then {experiment}.split_policy->'windows'
                          else '[]'::jsonb end)
               and count(distinct expected_window->>'window') =
                   jsonb_array_length(
                     case when jsonb_typeof(
                                      {experiment}.split_policy->'windows') =
                                     'array'
                          then {experiment}.split_policy->'windows'
                          else '[]'::jsonb end)
               and count(distinct
                         evidence_metric.dimensions->>
                         'evaluation_fingerprint') = 1
               and count(distinct
                         evidence_metric.dimensions->>
                         'evaluation_plan_fingerprint') = 1
               and count(distinct
                         evidence_metric.dimensions->>
                         'session_boundary_fingerprint') = 1
               and count(distinct
                         evidence_metric.dimensions->>
                         'instrument_ids_fingerprint') = 1
               and count(distinct
                         evidence_metric.dimensions->>
                         'source_content_fingerprint') = 1
               and bool_and(
                     evidence_metric.value is not null
                     and evidence_metric.value::text not in
                         ('NaN', 'Infinity', '-Infinity')
                     and evidence_metric.cost_model_version =
                         {experiment}.cost_model_version
                     and evidence_metric.dimensions->>
                           'evaluation_identity_complete' = 'true'
                     and evidence_metric.dimensions->>'asset_class' =
                         'EQUITY'
                     and evidence_metric.dimensions->>'asset_scope' =
                         '{STOCK_ASSET_SCOPE}'
                     and evidence_metric.dimensions->>
                           'stock_universe_contract_version' =
                         '{STOCK_UNIVERSE_VERSION}'
                     and evidence_metric.dimensions->>'cost_model_version' =
                         {experiment}.cost_model_version
                     and evidence_metric.dimensions->>'dataset_id' =
                         {experiment}.dataset_id::text
                     and evidence_metric.dimensions->>
                           'dataset_content_hash' = {dataset}.content_hash
                     and evidence_metric.dimensions->>'universe_version_id' =
                         {dataset}.universe_version_id::text
                     and coalesce(evidence_metric.dimensions->>
                                  'evaluation_fingerprint', '') ~
                         '^[0-9a-f]{{64}}$'
                     and coalesce(evidence_metric.dimensions->>
                                  'evaluation_plan_fingerprint', '') ~
                         '^[0-9a-f]{{64}}$'
                     and coalesce(evidence_metric.dimensions->>
                                  'session_boundary_fingerprint', '') ~
                         '^[0-9a-f]{{64}}$'
                     and coalesce(evidence_metric.dimensions->>
                                  'instrument_ids_fingerprint', '') ~
                         '^[0-9a-f]{{64}}$'
                     and coalesce(evidence_metric.dimensions->>
                                  'source_content_fingerprint', '') ~
                         '^[0-9a-f]{{64}}$'
                     and evidence_metric.dimensions->>
                           'source_content_fingerprint' =
                         {dataset}.content_hash
                     and evidence_metric.dimensions->>
                           'evaluation_plan_fingerprint' =
                         {experiment}.split_policy->>
                           'evaluation_plan_fingerprint'
                     and evidence_metric.dimensions->>
                           'session_boundary_fingerprint' =
                         {experiment}.split_policy->>
                           'session_boundary_fingerprint')
          from jsonb_array_elements(
                 case when jsonb_typeof(
                                  {experiment}.split_policy->'windows') =
                                 'array'
                      then {experiment}.split_policy->'windows'
                      else '[]'::jsonb end) expected_window
          join quant.experiment_metrics evidence_metric
            on evidence_metric.experiment_id = {experiment}.experiment_id
           and evidence_metric.split = 'WALK_FORWARD'
           and evidence_metric.metric = 'total_return'
           and evidence_metric.dimensions->>'evaluation_scope' =
               'DAILY_WALK_FORWARD'
           and evidence_metric.dimensions->>'window' =
               expected_window->>'window'
           and evidence_metric.dimensions->>'start_session' =
               expected_window->>'test_start'
           and evidence_metric.dimensions->>'end_session' =
               expected_window->>'test_end'
      )
    """
    intraday_full = f"""
      {experiment}.config->>'asset_scope' =
          'REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY'
      and exists (
        select 1
          from quant.intraday_experiment_rungs full_rung
         where full_rung.experiment_id = {experiment}.experiment_id
           and full_rung.dataset_id = {experiment}.dataset_id
           and full_rung.rung = 'FULL_60'
           and full_rung.evidence_purpose = 'ADAPTIVE_SEARCH'
           and full_rung.selection_policy_version =
               '{INTRADAY_FULL_EVIDENCE_POLICY}'
           and full_rung.planned_session_count = 60
           and cardinality(full_rung.planned_session_dates) = 60
           and full_rung.planned_instrument_count >= 1
           and cardinality(full_rung.planned_instrument_ids) =
               full_rung.planned_instrument_count
           and not exists (
             select 1
               from unnest(full_rung.planned_instrument_ids)
                    planned_instrument(instrument_id)
               cross join unnest(full_rung.planned_session_dates)
                    planned_session(session_date)
               left join quant.current_krx_stock_instrument_identity
                    governed_instrument
                 on governed_instrument.instrument_id =
                    planned_instrument.instrument_id
              where governed_instrument.instrument_id is null
                 or coalesce(upper(governed_instrument.instrument_type), '') <>
                    'STOCK'
                 or coalesce(upper(governed_instrument.asset_class), '') <>
                    'EQUITY'
                 or coalesce(upper(governed_instrument.market), '') <> 'KRX'
                 or coalesce(upper(governed_instrument.status), '') <>
                    'ACTIVE'
                 or coalesce(governed_instrument.is_spac, true)
                 or (governed_instrument.listed_from is not null and
                     governed_instrument.listed_from >
                     planned_session.session_date)
                 or (governed_instrument.listed_to is not null and
                     governed_instrument.listed_to <
                     planned_session.session_date)
           )
           and (
             select count(distinct exposure.session_date)
               from quant.intraday_session_exposures exposure
              where exposure.root_lineage_id = full_rung.root_lineage_id
                and exposure.exposure_purpose = 'ADAPTIVE_SEARCH'
                and exposure.knowledge_clock_mode =
                    'EVENT_TIME_HISTORICAL_ONLY'
                and exposure.session_date = any(
                    full_rung.planned_session_dates)
                and exposure.instrument_count =
                    full_rung.planned_instrument_count
                and exposure.instrument_set_fingerprint =
                    full_rung.instrument_set_fingerprint
                and exposure.instrument_ids <@
                    full_rung.planned_instrument_ids
                and full_rung.planned_instrument_ids <@
                    exposure.instrument_ids
           ) = 60
           and exists (
        select 1
          from quant.experiment_metrics evidence_metric
          join quant.intraday_report_manifests evidence_manifest
            on evidence_manifest.experiment_id =
               evidence_metric.experiment_id
         where evidence_metric.experiment_id = {experiment}.experiment_id
           and evidence_metric.split = 'WALK_FORWARD'
           and evidence_metric.metric = 'total_return'
           and evidence_metric.cost_model_version =
               {experiment}.cost_model_version
           and evidence_metric.dimensions->>'evaluation_scope' = 'FULL_60'
           and evidence_metric.dimensions->>'evaluation_identity_complete' =
               'true'
           and evidence_metric.dimensions->>'cost_model_version' =
               {experiment}.cost_model_version
           and not (evidence_metric.dimensions ? 'screening_candidate')
           and evidence_metric.dimensions @> jsonb_build_object(
                 'evaluation_scope', 'FULL_60',
                 'evaluation_identity_complete', true,
                 'cost_model_version', {experiment}.cost_model_version,
                 'experiment_rung_id', full_rung.experiment_rung_id::text,
                 'instrument_count', full_rung.planned_instrument_count,
                 'instrument_ids_fingerprint',
                    full_rung.instrument_set_fingerprint,
                 'session_set_fingerprint',
                    full_rung.session_set_fingerprint,
                 'rung_plan_fingerprint', full_rung.rung_plan_fingerprint,
                 'primary_fold_count', 4)
           and evidence_metric.dimensions->>'experiment_rung_id' =
               full_rung.experiment_rung_id::text
           and evidence_metric.dimensions->>'instrument_count' =
               full_rung.planned_instrument_count::text
           and evidence_metric.dimensions->>'instrument_ids_fingerprint' =
               full_rung.instrument_set_fingerprint
           and evidence_metric.dimensions->>'session_set_fingerprint' =
               full_rung.session_set_fingerprint
           and evidence_metric.dimensions->>'rung_plan_fingerprint' =
               full_rung.rung_plan_fingerprint
           and evidence_metric.dimensions->>'primary_fold_count' = '4'
           and evidence_metric.dimensions->>'primary_fold_set_fingerprint' ~
               '^[0-9a-f]{{64}}$'
           and evidence_metric.dimensions->>'window' like
               'INTRADAY_FOLD_%%'
           and nullif(evidence_metric.dimensions->>'start_session', '')
               is not null
           and nullif(evidence_metric.dimensions->>'end_session', '')
               is not null
           and evidence_manifest.manifest_version =
               '{INTRADAY_REPORT_MANIFEST_VERSION}'
           and jsonb_typeof(
                 evidence_manifest.report->'reproduction_runtime') = 'object'
           and evidence_manifest.report->'reproduction_runtime'->>'version' =
               'intraday-forward-reproduction-runtime-v1'
           and jsonb_typeof(evidence_manifest.report->
                 'reproduction_runtime'->'frozen_config') = 'object'
           and evidence_manifest.report->'reproduction_runtime'->>
                 'frozen_config_fingerprint' ~ '^[0-9a-f]{{64}}$'
           and evidence_manifest.report->'reproduction_runtime'->>
                 'experiment_input_hash' = {experiment}.input_hash
           and evidence_manifest.report->'reproduction_runtime'->>
                 'code_version' = {experiment}.code_version
           and evidence_manifest.report->'reproduction_runtime'->>
                 'cost_model_version' = {experiment}.cost_model_version
           and evidence_manifest.report->'reproduction_runtime'->>
                 'runtime_manifest_fingerprint' ~ '^[0-9a-f]{{64}}$'
           and evidence_manifest.report->'reproduction_runtime'->
                 'source_manifest'->>'source_fingerprint' ~
               '^[0-9a-f]{{64}}$'
           and jsonb_typeof(
                 evidence_manifest.report->'evaluation_identity') = 'object'
           and evidence_manifest.report->'evaluation_identity'->>
                 'evaluation_identity_complete' = 'true'
           and evidence_manifest.report->'evaluation_identity' @>
               jsonb_build_object(
                 'evaluation_scope', 'FULL_60',
                 'evaluation_identity_complete', true,
                 'cost_model_version', {experiment}.cost_model_version,
                 'experiment_rung_id', full_rung.experiment_rung_id::text,
                 'instrument_count', full_rung.planned_instrument_count,
                 'instrument_ids_fingerprint',
                    full_rung.instrument_set_fingerprint,
                 'session_set_fingerprint',
                    full_rung.session_set_fingerprint,
                 'rung_plan_fingerprint', full_rung.rung_plan_fingerprint,
                 'primary_fold_count', 4)
           and evidence_manifest.report->'evaluation_identity'->>
                 'evaluation_fingerprint' =
               evidence_metric.dimensions->>'evaluation_fingerprint'
           and evidence_manifest.report->'evaluation_identity'->>
                 'primary_fold_set_fingerprint' =
               evidence_metric.dimensions->>'primary_fold_set_fingerprint'
           and jsonb_typeof(evidence_manifest.report->'primary_folds') =
               'array'
           and evidence_manifest.report->'primary_folds' =
               jsonb_build_array(
                 jsonb_build_object(
                   'fold', 1, 'window', 'INTRADAY_FOLD_1',
                   'start_session', full_rung.planned_session_dates[1]::text,
                   'end_session', full_rung.planned_session_dates[15]::text,
                   'sessions', 15),
                 jsonb_build_object(
                   'fold', 2, 'window', 'INTRADAY_FOLD_2',
                   'start_session', full_rung.planned_session_dates[16]::text,
                   'end_session', full_rung.planned_session_dates[30]::text,
                   'sessions', 15),
                 jsonb_build_object(
                   'fold', 3, 'window', 'INTRADAY_FOLD_3',
                   'start_session', full_rung.planned_session_dates[31]::text,
                   'end_session', full_rung.planned_session_dates[45]::text,
                   'sessions', 15),
                 jsonb_build_object(
                   'fold', 4, 'window', 'INTRADAY_FOLD_4',
                   'start_session', full_rung.planned_session_dates[46]::text,
                   'end_session', full_rung.planned_session_dates[60]::text,
                   'sessions', 15)
               )
           and jsonb_array_length(
                 evidence_manifest.report->'primary_folds') = 4
           and (
             select count(*) = 4
                    and count(distinct
                              complete_metric.dimensions->>'window') = 4
               from quant.experiment_metrics complete_metric
               cross join lateral jsonb_array_elements(
                 evidence_manifest.report->'primary_folds') expected_fold
              where complete_metric.experiment_id =
                    evidence_metric.experiment_id
                and complete_metric.split = 'WALK_FORWARD'
                and complete_metric.metric = 'total_return'
                and complete_metric.cost_model_version =
                    evidence_metric.cost_model_version
                and not (
                    complete_metric.dimensions ? 'screening_candidate')
                and complete_metric.dimensions @> jsonb_build_object(
                      'evaluation_scope', 'FULL_60',
                      'evaluation_identity_complete', true,
                      'cost_model_version', {experiment}.cost_model_version,
                      'experiment_rung_id',
                         full_rung.experiment_rung_id::text,
                      'instrument_count',
                         full_rung.planned_instrument_count,
                      'instrument_ids_fingerprint',
                         full_rung.instrument_set_fingerprint,
                      'session_set_fingerprint',
                         full_rung.session_set_fingerprint,
                      'rung_plan_fingerprint',
                         full_rung.rung_plan_fingerprint,
                      'primary_fold_count', 4)
                and complete_metric.dimensions->>'evaluation_fingerprint' =
                    evidence_metric.dimensions->>'evaluation_fingerprint'
                and complete_metric.dimensions->>
                      'primary_fold_set_fingerprint' =
                    evidence_metric.dimensions->>
                      'primary_fold_set_fingerprint'
                and expected_fold->>'window' =
                    complete_metric.dimensions->>'window'
                and expected_fold->>'start_session' =
                    complete_metric.dimensions->>'start_session'
                and expected_fold->>'end_session' =
                    complete_metric.dimensions->>'end_session'
           )
      )
      )
    """
    return (
        "(\n"
        f"  ({intraday} and ({intraday_full}))\n"
        f"  or (not {intraday} and ({daily}))\n"
        ")"
    )


def _fingerprint(value: object) -> str:
    payload = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stock_session_boundary_fingerprint(
        windows: Sequence[Mapping[str, object]]) -> str:
    """Fingerprint ordered daily evaluation window triples.

    Frozen split policies use ``test_start``/``test_end`` while metric
    dimensions use ``start_session``/``end_session``.  Normalizing both forms
    here gives the writer and terminal SQL one canonical boundary identity.
    """

    normalized = [{
        "window": str(window.get("window") or ""),
        "start_session": str(
            window.get("start_session") or window.get("test_start") or ""),
        "end_session": str(
            window.get("end_session") or window.get("test_end") or ""),
    } for window in windows]
    if not normalized or any(not all(window.values())
                             for window in normalized):
        raise RuntimeError("stock evaluation windows require exact boundaries")
    if len({window["window"] for window in normalized}) != len(normalized):
        raise RuntimeError("stock evaluation window labels must be unique")
    return _fingerprint(normalized)


def build_stock_evaluation_identity(*, dataset_id: str,
                                    dataset_content_hash: str,
                                    universe_version_id: str,
                                    instrument_ids: Iterable[str],
                                    windows: Sequence[Mapping[str, object]],
                                    cost_model_version: str,
                                    evaluation_scope: str,
                                    evaluation_plan_fingerprint: str) -> dict:
    """Bind comparable metrics to one exact stock dataset and window plan.

    PBO/CSCV must never combine results merely because their strategy-family
    names match.  This identity deliberately includes the immutable dataset,
    universe, exact sorted UUID membership, test-session boundaries, and cost
    model.  Missing evidence fails before a metric can be marked comparable.
    """
    ids = sorted({str(value) for value in instrument_ids if value is not None})
    normalized_windows = [{
        "window": str(window.get("window") or ""),
        "start_session": str(window.get("start_session") or ""),
        "end_session": str(window.get("end_session") or ""),
    } for window in windows]
    required = {
        "dataset_id": str(dataset_id or ""),
        "dataset_content_hash": str(dataset_content_hash or ""),
        "universe_version_id": str(universe_version_id or ""),
        "cost_model_version": str(cost_model_version or ""),
        "evaluation_scope": str(evaluation_scope or ""),
        "evaluation_plan_fingerprint": str(
            evaluation_plan_fingerprint or ""),
    }
    missing = sorted(key for key, value in required.items() if not value)
    if missing or not ids or not normalized_windows:
        raise RuntimeError(
            "incomplete stock evaluation identity: "
            f"missing={missing} instruments={len(ids)} "
            f"windows={len(normalized_windows)}")
    if re.fullmatch(r"[0-9a-f]{64}",
                    required["dataset_content_hash"]) is None:
        raise RuntimeError(
            "stock evaluation dataset content hash must be lowercase SHA-256")
    if re.fullmatch(r"[0-9a-f]{64}",
                    required["evaluation_plan_fingerprint"]) is None:
        raise RuntimeError(
            "stock evaluation plan fingerprint must be lowercase SHA-256")
    boundary_fingerprint = stock_session_boundary_fingerprint(
        normalized_windows)

    identity = {
        **required,
        "source_content_fingerprint": required["dataset_content_hash"],
        "instrument_ids_fingerprint": _fingerprint(ids),
        "session_boundary_fingerprint": boundary_fingerprint,
        "asset_class": "EQUITY",
        "asset_scope": STOCK_ASSET_SCOPE,
        "stock_universe_contract_version": STOCK_UNIVERSE_VERSION,
    }
    identity["evaluation_fingerprint"] = _fingerprint(identity)
    identity["evaluation_identity_complete"] = True
    return identity


def _day(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _metadata_flag(value: object) -> bool:
    return value is True or str(value or "").strip().lower() in {
        "1", "t", "true", "yes",
    }


def _valid_stock(metadata: Mapping[str, object]) -> bool:
    return (
        str(metadata.get("instrument_type") or "").upper() == "STOCK"
        and str(metadata.get("asset_class") or "").upper() == "EQUITY"
        and str(metadata.get("market") or "").upper() == "KRX"
        and str(metadata.get("status") or "").upper() == "ACTIVE"
        and not _metadata_flag(metadata.get("is_spac"))
    )


def _listed_on(metadata: Mapping[str, object], session: date) -> bool:
    listed_from = metadata.get("listed_from")
    listed_to = metadata.get("listed_to")
    return (
        (listed_from is None or _day(listed_from) <= session)
        and (listed_to is None or _day(listed_to) >= session)
    )


def load_instrument_metadata(connection, instrument_ids: Iterable[str]
                             ) -> dict[str, dict]:
    """Load the exact product identity used by the stock-only decision."""
    ids = sorted({str(value) for value in instrument_ids if value is not None})
    if not ids:
        return {}
    with connection.cursor() as cursor:
        cursor.execute("""
            select instrument_id::text, instrument_type, asset_class, market,
                   status, listed_from, listed_to,
                   is_spac
              from quant.current_krx_stock_instrument_identity
             where instrument_id = any(%s::uuid[])
        """, (ids,))
        return {str(row[0]): {
            "instrument_type": str(row[1]),
            "asset_class": str(row[2]),
            "market": str(row[3]),
            "status": str(row[4]),
            "listed_from": row[5],
            "listed_to": row[6],
            "is_spac": row[7],
        } for row in cursor.fetchall()}


def assert_stock_instrument_ids(connection, instrument_ids: Iterable[str], *,
                                first_session: date | None = None,
                                last_session: date | None = None) -> dict:
    """Validate an explicit UUID allowlist before an analytical read.

    This is used by tools that read the market database directly and therefore
    cannot join the reference plane in their own SQL statement.
    """
    ids = sorted({str(value) for value in instrument_ids if value is not None})
    if not ids:
        raise RuntimeError("stock instrument allowlist is required")
    metadata = load_instrument_metadata(connection, ids)
    missing = sorted(set(ids) - set(metadata))
    invalid = sorted(
        instrument_id for instrument_id in ids
        if instrument_id in metadata and not _valid_stock(metadata[instrument_id]))
    outside = []
    if first_session is not None or last_session is not None:
        first = _day(first_session or last_session)
        last = _day(last_session or first_session)
        outside = sorted(
            instrument_id for instrument_id in ids
            if instrument_id in metadata
            and (not _listed_on(metadata[instrument_id], first)
                 or not _listed_on(metadata[instrument_id], last)))
    if missing or invalid or outside:
        raise RuntimeError(
            "stock instrument allowlist failed reference validation: "
            f"missing={len(missing)} invalid={len(invalid)} "
            f"outside_listing_interval={len(outside)}")
    return {
        "version": STOCK_UNIVERSE_VERSION,
        "asset_scope": STOCK_ASSET_SCOPE,
        "instrument_count": len(ids),
        "instrument_ids_fingerprint": _fingerprint(ids),
        "unknown_identity_policy": "FAIL_CLOSED",
    }


@dataclass(frozen=True)
class StockUniverseAudit:
    version: str
    asset_scope: str
    requested_instruments: int
    accepted_instruments: int
    accepted_rows: int
    excluded_rows: int
    excluded: dict[str, int]

    def as_dict(self) -> dict:
        return {
            "version": self.version,
            "asset_scope": self.asset_scope,
            "requested_instruments": self.requested_instruments,
            "accepted_instruments": self.accepted_instruments,
            "accepted_rows": self.accepted_rows,
            "excluded_rows": self.excluded_rows,
            "excluded": dict(sorted(self.excluded.items())),
            "unknown_identity_policy": "FAIL_CLOSED",
            "listing_interval_policy": "ENFORCE_WHEN_PRESENT",
        }


def filter_stock_rows(connection, rows: Sequence[Mapping], *,
                      instrument_key: str = "instrument_id",
                      session_key: str = "trade_date") -> tuple[list, dict]:
    """Return only governed KRX active-stock rows plus an auditable summary."""
    ids = {str(row[instrument_key]) for row in rows}
    metadata = load_instrument_metadata(connection, ids)
    excluded = {
        "MISSING_REFERENCE_METADATA": 0,
        "NON_STOCK": 0,
        "NON_EQUITY": 0,
        "NON_KRX": 0,
        "INACTIVE": 0,
        "SPAC": 0,
        "OUTSIDE_LISTING_INTERVAL": 0,
    }
    kept = []
    accepted_ids: set[str] = set()
    for row in rows:
        instrument_id = str(row[instrument_key])
        identity = metadata.get(instrument_id)
        if identity is None:
            excluded["MISSING_REFERENCE_METADATA"] += 1
            continue
        if str(identity["instrument_type"]).upper() != "STOCK":
            excluded["NON_STOCK"] += 1
            continue
        if str(identity["asset_class"]).upper() != "EQUITY":
            excluded["NON_EQUITY"] += 1
            continue
        if str(identity["market"]).upper() != "KRX":
            excluded["NON_KRX"] += 1
            continue
        if str(identity["status"]).upper() != "ACTIVE":
            excluded["INACTIVE"] += 1
            continue
        if _metadata_flag(identity.get("is_spac")):
            excluded["SPAC"] += 1
            continue
        if not _listed_on(identity, _day(row[session_key])):
            excluded["OUTSIDE_LISTING_INTERVAL"] += 1
            continue
        kept.append(row)
        accepted_ids.add(instrument_id)
    audit = StockUniverseAudit(
        version=STOCK_UNIVERSE_VERSION,
        asset_scope=STOCK_ASSET_SCOPE,
        requested_instruments=len(ids),
        accepted_instruments=len(accepted_ids),
        accepted_rows=len(kept),
        excluded_rows=len(rows) - len(kept),
        excluded=excluded,
    ).as_dict()
    return kept, audit


def load_universe_members(connection, universe_version_id: str
                          ) -> tuple[set[str], dict[str, dict]]:
    with connection.cursor() as cursor:
        cursor.execute("""
            select member.instrument_id::text,
                   instrument.instrument_type, instrument.asset_class,
                   instrument.market, instrument.status,
                   instrument.listed_from, instrument.listed_to,
                   instrument.is_spac
              from quant.universe_members member
              left join quant.current_krx_stock_instrument_identity instrument
                on instrument.instrument_id = member.instrument_id
             where member.universe_version_id = %s::uuid
             order by member.instrument_id
        """, (str(universe_version_id),))
        rows = cursor.fetchall()
    members = {str(row[0]) for row in rows}
    metadata = {
        str(row[0]): ({
            "instrument_type": str(row[1]),
            "asset_class": str(row[2]),
            "market": str(row[3]),
            "status": str(row[4]),
            "listed_from": row[5],
            "listed_to": row[6],
            "is_spac": row[7],
        } if row[1] is not None else {})
        for row in rows
    }
    return members, metadata


def assert_stock_only_universe(connection, universe_version_id: str, *,
                               row_instrument_ids: Iterable[str] | None = None,
                               row_dates: Mapping[str, tuple[date, date]] | None = None
                               ) -> dict:
    """Reject an empty, mismatched, or non-stock immutable universe."""
    members, metadata = load_universe_members(connection, universe_version_id)
    if not members:
        raise RuntimeError("dataset universe is empty or not visible")
    invalid = sorted(
        instrument_id for instrument_id in members
        if not _valid_stock(metadata.get(instrument_id) or {}))
    if invalid:
        raise RuntimeError(
            "dataset universe is not KRX ACTIVE STOCK only: "
            f"invalid={len(invalid)} sample={invalid[:5]}")
    if row_instrument_ids is not None:
        observed = {str(value) for value in row_instrument_ids}
        missing = sorted(members - observed)
        unexpected = sorted(observed - members)
        if missing or unexpected:
            raise RuntimeError(
                "dataset rows and immutable stock universe disagree: "
                f"missing={len(missing)} unexpected={len(unexpected)}")
    if row_dates is not None:
        unknown_date_ids = sorted(
            str(value) for value in row_dates if str(value) not in members)
        if unknown_date_ids:
            raise RuntimeError(
                "dataset date evidence contains instruments outside the "
                "immutable stock universe: "
                f"unexpected={len(unknown_date_ids)} "
                f"sample={unknown_date_ids[:5]}")
        outside = []
        for instrument_id, bounds in row_dates.items():
            identity = metadata.get(str(instrument_id)) or {}
            first, last = (_day(bounds[0]), _day(bounds[1]))
            if (not _listed_on(identity, first)
                    or not _listed_on(identity, last)):
                outside.append(str(instrument_id))
        if outside:
            raise RuntimeError(
                "dataset rows fall outside reference listing intervals: "
                f"invalid={len(outside)} sample={sorted(outside)[:5]}")
    return {
        "version": STOCK_UNIVERSE_VERSION,
        "asset_scope": STOCK_ASSET_SCOPE,
        "member_count": len(members),
        "member_ids": members,
        "unknown_identity_policy": "FAIL_CLOSED",
    }
