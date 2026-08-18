"""Outcome-conditioned population builder exposed to the Hermes research agent.

The breeder reads only persisted, source-backed formula leads and governed
adaptive experiment results.  It asks :mod:`formula_evolution_engine` for a
large typed population, then returns equation drafts and deterministic semantic
hints.  It does *not* write prose reports, fit coefficients, inspect a forward
lockbox, or persist candidates.  Hermes must supply the falsifiable economic
contract and submit selected children through ``factory_submit_evolved_formulas``.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping

_HERE = Path(__file__).resolve().parent
_CONTRACTS = _HERE.parent / "contracts"
_ROOT = _HERE.parents[2]
_PIPELINE = _ROOT / "departments" / "04-quant-backtest" / "pipeline"
for _path in (_HERE, _CONTRACTS, _PIPELINE):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from alpha_semantics import (  # noqa: E402
    L10_QUOTE_PRESSURE_FIELDS,
    L1_QUOTE_PRESSURE_FIELDS,
    QUOTE_PRESSURE_FIELDS,
    TAPE_PRESSURE_FIELDS,
)
from intraday_search_exposure_contract import (  # noqa: E402
    exposure_fingerprint as adaptive_search_exposure_fingerprint,
    has_exact_declaration as has_exact_search_exposure_declaration,
    strict_validation_error as search_exposure_validation_error,
)
from intraday_candidate_identity import (  # noqa: E402
    lineage_identity_matches,
)
from formula_evolution_engine import (  # noqa: E402
    EvolutionConfig,
    FormulaEvolutionEngine,
    FormulaOutcome,
    FormulaSeed,
    subtree_shape_fingerprint,
)
from formula_search_memory import build_formula_search_memory  # noqa: E402
import intraday_ast_contract as grammar  # noqa: E402
from stock_universe import (INTRADAY_REPORT_MANIFEST_VERSION,  # noqa: E402
                            governed_stock_evidence_sql)


MODULE_VERSION = "outcome-conditioned-formula-breeder-v4"
MIN_POPULATION = 8
MAX_POPULATION = 128
MAX_DELIVERY_CANDIDATES = 12
LEGACY_EVALUATOR_VERSION = "intraday-candidate-evaluator-v11"
EXPLICIT_EVALUATOR_VERSION = "intraday-candidate-evaluator-v12"
# Backward-compatible public default for direct/unit callers.  The production
# ``generate`` entry point explicitly targets V2 below.
ACTIVE_EVALUATOR_VERSION = LEGACY_EVALUATOR_VERSION
ACTIVE_COST_MODEL_VERSION = "krx-intraday-execution-v3"


def _evaluator_for_window_contract(contract_version: object) -> str:
    version = str(contract_version or grammar.LEGACY_FEATURE_WINDOW_CONTRACT)
    if version == grammar.LEGACY_FEATURE_WINDOW_CONTRACT:
        return LEGACY_EVALUATOR_VERSION
    if version == grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT:
        return EXPLICIT_EVALUATOR_VERSION
    raise ValueError(f"unknown feature-window contract {version!r}")

_GOVERNED_STOCK_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="m", hypothesis_alias="h")

# ``krx-intraday-events/v1`` is an authoritative live raw-event handle, not an
# immutable materialized panel.  Its ``universe_version_id`` and ``row_count``
# are therefore deliberately NULL; the exact stock/session/content slice is
# frozen in the append-only rung and exposure ledgers below.  Do not reuse the
# daily-manifest ``governed_stock_dataset_sql`` predicate here: it requires a
# universe_version_id and permanently filters every valid intraday rung.
#
# Keep this exception as narrow as the resolver's raw-source contract.  A
# different NULL-universe manifest cannot become outcome memory merely by
# claiming to be an intraday dataset.
_RAW_INTRADAY_DATASET = """
      m.name = 'krx-intraday-events'
      and m.version = 'v1'
      and m.universe_version_id is null
      and m.row_count is null
      and m.partitions = '[]'::jsonb
      and m.object_path =
          'timescaledb://market/{market_quotes,market_ticks}'
      and m.content_hash ~ '^[0-9a-f]{64}$'
      and m.source_versions->>'market_quotes' = 'ls-realtime-book-v1'
      and m.source_versions->>'market_ticks' = 'ls-realtime-trade-v1'
      and m.feature_spec_versions->>'intraday_microstructure' =
          'intraday-microstructure-v1'
      and m.feature_spec_versions->>'intraday_alpha_ast' =
          'intraday-alpha-ast-v1'
      and m.quality_summary->>'status' =
          'LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT'
      and m.quality_summary->>'missing_received_at' = 'reject'
      and m.point_in_time_policy->>'knowledge_clock' =
          'available_at=max(received_at,observed_at)'
      and m.point_in_time_policy->>'feature_cutoff' =
          'event_time<=decision_time and available_at<=decision_time'
      and m.point_in_time_policy->>'label_cutoff' = 'entry_time+horizon'
      and m.point_in_time_policy->>'instrument_isolation' = 'true'
      and m.schema_definition->'market_quotes'->'required' @>
          '["event_time","received_at","observed_at","instrument_id",'
          '"bid_prices","bid_sizes","ask_prices","ask_sizes",'
          '"source_event_id"]'::jsonb
      and m.schema_definition->'market_ticks'->'required' @>
          '["event_time","received_at","observed_at","instrument_id",'
          '"price","quantity","side","source_event_id"]'::jsonb
"""


def _raw_rung_content_sql(rung_alias: str) -> str:
    """Validate the frozen raw-source snapshot attached to one rung.

    The ledger table already constrains the JSON to a non-empty object.  This
    predicate additionally proves that the current producer recorded both raw
    sides under one recognized clock/content-hash contract before its outcomes
    can influence evolution.
    """

    if rung_alias not in {"calibration_rung", "search_rung"}:
        raise ValueError("unknown intraday rung SQL alias")
    rung = rung_alias
    return f"""
      {rung}.source_watermark->>'event_source' in
          ('LOCAL_RECEIPT_CLOCK','EXTERNAL_FDW_EVENT_TIME')
      and jsonb_typeof({rung}.source_watermark->'source_lineage') = 'array'
      and jsonb_array_length({rung}.source_watermark->'source_lineage') = 2
      and (
          select count(*) = 2
                 and count(distinct source_row->>'source') = 2
                 and bool_and(
                   jsonb_typeof(source_row) = 'object'
                   and coalesce(source_row->>'rows','') ~ '^[1-9][0-9]*$'
                   and coalesce(source_row->>'content_fingerprint','') ~
                       '^[0-9a-f]{{64}}$'
                   and case {rung}.source_watermark->>'event_source'
                     when 'EXTERNAL_FDW_EVENT_TIME' then
                       source_row->>'source' in
                           ('ext_src.quotes','ext_src.ticks')
                       and source_row->>'content_hash_contract' =
                           'external-daily-source-content-manifest-v2'
                     when 'LOCAL_RECEIPT_CLOCK' then
                       source_row->>'source' in
                           ('market.market_quotes','market.market_ticks')
                       and source_row->>'content_hash_contract' =
                           'postgres-jsonb-multiset-v1'
                     else false
                   end)
            from jsonb_array_elements(
                   {rung}.source_watermark->'source_lineage') source_row
      )
    """


def _frozen_slice_stock_sql(rung_alias: str) -> str:
    """Bind the producer's frozen stock slice to the durable rung UUID set."""

    if rung_alias not in {"calibration_rung", "search_rung"}:
        raise ValueError("unknown intraday rung SQL alias")
    rung = rung_alias
    return f"""
      e.config#>>'{{slice,product_filter}}' =
          'REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY'
      and e.config#>>'{{slice,product_filter_version}}' =
          'krx-stock-only-v3'
      and e.config#>>'{{slice,asset_scope}}' = 'KRX_ACTIVE_STOCK_ONLY'
      and e.config#>>'{{slice,stock_universe_contract_version}}' =
          'krx-active-stock-only-v1'
      and e.config#>>'{{slice,unknown_product_identity_policy}}' =
          'FAIL_CLOSED_EXCLUDE'
      and e.config#>>'{{slice,reference_identity_revalidated}}' = 'true'
      and coalesce(e.config#>>'{{slice,reference_identity_fingerprint}}','') ~
          '^[0-9a-f]{{64}}$'
      and jsonb_typeof(
            e.config#>'{{slice,reference_instrument_ids}}') = 'array'
      and jsonb_array_length(
            e.config#>'{{slice,reference_instrument_ids}}') =
          {rung}.planned_instrument_count
      and case
            when coalesce(e.config#>>
                 '{{slice,post_product_filter_instruments}}','') ~
                 '^[1-9][0-9]*$'
            then (e.config#>>
                  '{{slice,post_product_filter_instruments}}')::integer
            else -1 end = {rung}.planned_instrument_count
      and not exists (
          select 1
            from jsonb_array_elements_text(
                   e.config#>'{{slice,reference_instrument_ids}}') reference_id
           where reference_id !~
                 '^[0-9a-f]{{8}}-[0-9a-f]{{4}}-[0-9a-f]{{4}}-'
                 '[0-9a-f]{{4}}-[0-9a-f]{{12}}$'
              or not (reference_id = any(
                     {rung}.planned_instrument_ids::text[])))
      and not exists (
          select 1
            from unnest({rung}.planned_instrument_ids)
                 planned_instrument(instrument_id)
           where not exists (
                 select 1
                   from jsonb_array_elements_text(
                          e.config#>'{{slice,reference_instrument_ids}}')
                        reference_id
                  where reference_id = planned_instrument.instrument_id::text))
      and jsonb_typeof(e.config->'source_lineage') = 'array'
      and e.config->'source_lineage' =
          {rung}.source_watermark->'source_lineage'
      and e.config#>>'{{slice,event_source}}' =
          {rung}.source_watermark->>'event_source'
    """

_LEADS_SQL = """
select l.lead_id, l.claimed_edge, l.stated_mechanism, l.ast_contract,
       (exists (select 1 from research.experiment_proposals p
                 where l.lead_id = any(p.lead_ids)
                   and p.status in ('PUBLISHED','ACCEPTED'))
        or exists (select 1 from research.proposal_review_outcomes r
                    where l.lead_id = any(r.lead_ids))) as used
  from research.methodology_leads l
 where l.status = 'COMPLETE'
   and l.ast_contract->>'ast_readiness' = 'AST_READY'
   and l.ast_contract->>'research_lane' = 'INTRADAY_EVENT'
   and l.ast_contract->>'formula_discovery_version' = 'formula-discovery-v5'
   and coalesce((l.ast_contract->>'formula_contract_complete')::boolean, false)
   and coalesce((l.ast_contract->>'alpha_candidate_eligible')::boolean, false)
 order by l.created_at desc, l.lead_id
 limit 256
"""

_PRIMARY_OUTCOMES_SQL = """
select e.experiment_id::text, e.config->'intraday_signal_expr',
       coalesce(o.decision, ''), coalesce(o.lesson_codes, '{}'::text[]),
       coalesce(o.oos_summary, '{}'::jsonb),
       coalesce(manifest.report->'score_calibration', '{}'::jsonb), h.title,
       coalesce(o.decided_at, manifest.created_at, e.created_at),
       e.config, h.lead_ids, jsonb_build_object(
         'candidate_lineage_id', primary_lineage.candidate_lineage_id,
         'root_lineage_id', primary_lineage.root_lineage_id,
         'candidate_identity_fingerprint',
            primary_lineage.candidate_identity_fingerprint,
         'candidate_ast_fingerprint',
            primary_lineage.candidate_ast_fingerprint,
         'semantic_plan_fingerprint',
            primary_lineage.semantic_plan_fingerprint,
         'baseline_ast_fingerprint',
            primary_lineage.baseline_ast_fingerprint,
         'feature_spec_fingerprint',
            primary_lineage.feature_spec_fingerprint,
         'label_spec_fingerprint', primary_lineage.label_spec_fingerprint,
         'model_spec_fingerprint', primary_lineage.model_spec_fingerprint,
         'economic_family_id', primary_lineage.economic_family_id,
         'evaluator_version', primary_lineage.evaluator_version,
         'cost_model_version', primary_lineage.cost_model_version)
  from quant.experiments e
  join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
  join quant.dataset_manifests m on m.dataset_id = e.dataset_id
  left join lateral (
    select decision, lesson_codes, oos_summary, decided_at
      from research.experiment_outcomes x
     where x.experiment_id = e.experiment_id::text
     order by x.decided_at desc, x.created_at desc, x.outcome_id desc limit 1
  ) o on true
  join quant.intraday_report_manifests manifest
    on manifest.experiment_id = e.experiment_id
  join quant.intraday_candidate_lineages primary_lineage
    on primary_lineage.candidate_lineage_id = case
         when coalesce(manifest.report#>>
              '{trial_lockbox,primary_candidate_lineage_id}', '') ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         then (manifest.report#>>
              '{trial_lockbox,primary_candidate_lineage_id}')::uuid
         else null end
   and primary_lineage.hypothesis_id = e.hypothesis_id
 where e.config ? 'intraday_signal_expr'
   and primary_lineage.evaluator_version = case
         when e.config->>'feature_window_contract_version' =
              'explicit-primitive-window-v2'
         then 'intraday-candidate-evaluator-v12'
         else 'intraday-candidate-evaluator-v11' end
   and primary_lineage.cost_model_version = 'krx-intraday-execution-v3'
   and """ + _GOVERNED_STOCK_EVIDENCE + """
 order by e.created_at desc
limit 256
"""

_CALIBRATION_FAILURES_SQL = """
select e.experiment_id::text, e.config->'intraday_signal_expr',
       'FAILED'::text, '{}'::text[], '{}'::jsonb,
       manifest.report->'score_calibration', h.title, manifest.created_at,
       e.config, h.lead_ids, jsonb_build_object(
         'candidate_lineage_id', primary_lineage.candidate_lineage_id,
         'root_lineage_id', primary_lineage.root_lineage_id,
         'candidate_identity_fingerprint',
            primary_lineage.candidate_identity_fingerprint,
         'candidate_ast_fingerprint',
            primary_lineage.candidate_ast_fingerprint,
         'semantic_plan_fingerprint',
            primary_lineage.semantic_plan_fingerprint,
         'baseline_ast_fingerprint',
            primary_lineage.baseline_ast_fingerprint,
         'feature_spec_fingerprint',
            primary_lineage.feature_spec_fingerprint,
         'label_spec_fingerprint', primary_lineage.label_spec_fingerprint,
         'model_spec_fingerprint', primary_lineage.model_spec_fingerprint,
         'economic_family_id', primary_lineage.economic_family_id,
         'evaluator_version', primary_lineage.evaluator_version,
         'cost_model_version', primary_lineage.cost_model_version)
  from quant.experiments e
  join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
  join quant.dataset_manifests m on m.dataset_id = e.dataset_id
  join quant.intraday_report_manifests manifest
    on manifest.experiment_id = e.experiment_id
  join quant.intraday_candidate_lineages primary_lineage
    on primary_lineage.candidate_lineage_id = case
         when coalesce(manifest.report#>>
              '{trial_lockbox,primary_candidate_lineage_id}', '') ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         then (manifest.report#>>
              '{trial_lockbox,primary_candidate_lineage_id}')::uuid
         else null end
   and primary_lineage.hypothesis_id = e.hypothesis_id
 where e.config ? 'intraday_signal_expr'
   and e.config->>'asset_scope' = 'REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY'
   and manifest.manifest_version = '""" + \
    INTRADAY_REPORT_MANIFEST_VERSION + """'
   and manifest.report->'score_calibration'->>'status' in
       ('NO_COST_FEASIBLE_ENTRY','NON_POSITIVE_DIRECTIONAL_RELATION')
   and primary_lineage.evaluator_version = case
         when e.config->>'feature_window_contract_version' =
              'explicit-primitive-window-v2'
         then 'intraday-candidate-evaluator-v12'
         else 'intraday-candidate-evaluator-v11' end
   and primary_lineage.cost_model_version = 'krx-intraday-execution-v3'
   and coalesce(manifest.report->'score_calibration'->>'observations','')
       ~ '^[1-9][0-9]*$'
   and """ + _RAW_INTRADAY_DATASET + """
   and exists (
       select 1
         from quant.intraday_experiment_rungs calibration_rung
        where calibration_rung.experiment_id = e.experiment_id
          and calibration_rung.dataset_id = e.dataset_id
          and calibration_rung.candidate_lineage_id =
              primary_lineage.candidate_lineage_id
          and calibration_rung.root_lineage_id =
              primary_lineage.root_lineage_id
          and calibration_rung.rung = 'CALIBRATION'
          and calibration_rung.evidence_purpose = 'ADAPTIVE_SEARCH'
          and calibration_rung.experiment_rung_id::text =
              manifest.report#>>'{trial_lockbox,rungs,calibration}'
          and calibration_rung.planned_session_count between 1 and 5
          and cardinality(calibration_rung.planned_session_dates) =
              calibration_rung.planned_session_count
          and cardinality(calibration_rung.planned_instrument_ids) =
              calibration_rung.planned_instrument_count
          and """ + _raw_rung_content_sql("calibration_rung") + """
          and """ + _frozen_slice_stock_sql("calibration_rung") + """
          and not exists (
              select 1
                from unnest(calibration_rung.planned_instrument_ids)
                     planned_instrument(instrument_id)
                cross join unnest(calibration_rung.planned_session_dates)
                     planned_session(session_date)
                left join quant.current_krx_stock_instrument_identity identity
                  on identity.instrument_id = planned_instrument.instrument_id
               where identity.instrument_id is null
                  or coalesce(upper(identity.instrument_type),'') <> 'STOCK'
                  or coalesce(upper(identity.asset_class),'') <> 'EQUITY'
                  or coalesce(upper(identity.market),'') <> 'KRX'
                  or coalesce(upper(identity.status),'') <> 'ACTIVE'
                  or coalesce(identity.is_spac, true)
                  or (identity.listed_from is not null and
                      identity.listed_from > planned_session.session_date)
                  or (identity.listed_to is not null and
                      identity.listed_to < planned_session.session_date))
          and (
              select count(distinct exposure.session_date)
                from quant.intraday_session_exposures exposure
               where exposure.root_lineage_id =
                     calibration_rung.root_lineage_id
                 and exposure.dataset_id = calibration_rung.dataset_id
                 and exposure.session_date = any(
                     calibration_rung.planned_session_dates)
                 and exposure.exposure_purpose = 'CALIBRATION'
                 and exposure.knowledge_clock_mode =
                     'EVENT_TIME_HISTORICAL_ONLY'
                 and exposure.knowledge_cutoff <=
                     calibration_rung.dataset_cutoff
                 and exposure.instrument_count =
                     calibration_rung.planned_instrument_count
                 and exposure.instrument_set_fingerprint =
                     calibration_rung.instrument_set_fingerprint
                 and exposure.instrument_ids <@
                     calibration_rung.planned_instrument_ids
                 and calibration_rung.planned_instrument_ids <@
                     exposure.instrument_ids
                 and exposure.session_content_fingerprint ~
                     '^[0-9a-f]{64}$'
                 and exposure.exposure_evidence_fingerprint ~
                     '^[0-9a-f]{64}$'
                 and jsonb_typeof(exposure.source_watermark) = 'object'
                 and exposure.source_watermark <> '{}'::jsonb
                 and exposure.quote_row_count > 0
                 and exposure.trade_row_count > 0
          ) = calibration_rung.planned_session_count)
 order by e.created_at desc
 limit 256
"""

_SCREEN_OUTCOMES_SQL = """
select e.experiment_id::text,
       e.config,
       coalesce(e.config->'screening_population', '[]'::jsonb),
       coalesce(manifest.report->'screening_candidates', '{}'::jsonb),
       coalesce(manifest.report->'discovery_rungs', '[]'::jsonb),
       coalesce(manifest.report->'trial_lockbox', '{}'::jsonb),
       h.title, manifest.created_at, governed.rungs, lineage.lineages
  from quant.experiments e
  join quant.hypotheses h on h.hypothesis_id = e.hypothesis_id
  join quant.dataset_manifests m on m.dataset_id = e.dataset_id
  join quant.intraday_report_manifests manifest
    on manifest.experiment_id = e.experiment_id
  join quant.intraday_candidate_lineages primary_lineage
    on primary_lineage.candidate_lineage_id = case
         when coalesce(manifest.report#>>
              '{trial_lockbox,primary_candidate_lineage_id}', '') ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         then (manifest.report#>>
              '{trial_lockbox,primary_candidate_lineage_id}')::uuid
         else null end
   and primary_lineage.hypothesis_id = e.hypothesis_id
   and primary_lineage.root_lineage_id = case
         when coalesce(manifest.report#>>'{trial_lockbox,root_lineage_id}', '') ~
              '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
         then (manifest.report#>>'{trial_lockbox,root_lineage_id}')::uuid
         else null end
   and primary_lineage.cost_model_version = e.cost_model_version
  join lateral (
    select coalesce(jsonb_object_agg(
               entry.key, jsonb_build_object(
                 'candidate_lineage_id', candidate.candidate_lineage_id,
                 'root_lineage_id', candidate.root_lineage_id,
                 'parent_lineage_id', candidate.parent_lineage_id,
                 'candidate_identity_fingerprint',
                    candidate.candidate_identity_fingerprint,
                 'candidate_ast_fingerprint',
                    candidate.candidate_ast_fingerprint,
                 'semantic_plan_fingerprint',
                    candidate.semantic_plan_fingerprint,
                 'baseline_ast_fingerprint',
                    candidate.baseline_ast_fingerprint,
                 'feature_spec_fingerprint',
                    candidate.feature_spec_fingerprint,
                 'label_spec_fingerprint',
                    candidate.label_spec_fingerprint,
                 'model_spec_fingerprint',
                    candidate.model_spec_fingerprint,
                 'economic_family_id', candidate.economic_family_id,
                 'evaluator_version', candidate.evaluator_version,
                 'cost_model_version', candidate.cost_model_version)
               order by entry.key) filter (
                 where candidate.candidate_lineage_id is not null
                   and entry.key ~ '^[0-9a-f]{16}$'), '{}'::jsonb) lineages,
           count(*) declared_count,
           count(*) filter (where entry.key ~ '^[0-9a-f]{16}$') valid_key_count,
           count(candidate.candidate_lineage_id) matched_count,
           count(distinct candidate.candidate_lineage_id) distinct_count,
           count(*) filter (
             where candidate.candidate_lineage_id =
                   primary_lineage.candidate_lineage_id) primary_count
      from jsonb_each_text(case
             when jsonb_typeof(manifest.report#>
                  '{trial_lockbox,registered_candidate_lineages}') = 'object'
             then manifest.report#>
                  '{trial_lockbox,registered_candidate_lineages}'
             else '{}'::jsonb end) entry
      left join quant.intraday_candidate_lineages candidate
        on candidate.candidate_lineage_id = case
             when entry.value ~
                  '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
             then entry.value::uuid else null end
       and candidate.hypothesis_id = e.hypothesis_id
       and candidate.root_lineage_id = primary_lineage.root_lineage_id
       and candidate.evaluator_version = primary_lineage.evaluator_version
       and candidate.cost_model_version = primary_lineage.cost_model_version
  ) lineage on lineage.declared_count =
                  1 + jsonb_array_length(case
                    when jsonb_typeof(e.config->'screening_population') = 'array'
                    then e.config->'screening_population' else '[]'::jsonb end)
           and lineage.valid_key_count = lineage.declared_count
           and lineage.matched_count = lineage.declared_count
           and lineage.distinct_count = lineage.declared_count
           and lineage.primary_count = 1
  join lateral (
    select coalesce(jsonb_agg(jsonb_build_object(
               'rung', search_rung.rung,
               'rung_plan_fingerprint', search_rung.rung_plan_fingerprint,
               'root_lineage_id', search_rung.root_lineage_id,
               'dataset_id', search_rung.dataset_id,
               'dataset_cutoff', search_rung.dataset_cutoff,
               'planned_session_dates', search_rung.planned_session_dates,
               'planned_session_count', search_rung.planned_session_count,
               'planned_instrument_ids', search_rung.planned_instrument_ids,
               'planned_instrument_count', search_rung.planned_instrument_count,
               'session_set_fingerprint', search_rung.session_set_fingerprint,
               'instrument_set_fingerprint',
                  search_rung.instrument_set_fingerprint,
               'source_watermark', search_rung.source_watermark,
               'session_evidence', coalesce((
                   select jsonb_agg(jsonb_build_object(
                              'session', exposure.session_date,
                              'session_content_fingerprint',
                                 exposure.session_content_fingerprint,
                              'quote_rows', exposure.quote_row_count,
                              'trade_rows', exposure.trade_row_count,
                              'source_watermark', exposure.source_watermark,
                              'instrument_count', exposure.instrument_count,
                              'instrument_set_fingerprint',
                                 exposure.instrument_set_fingerprint)
                              order by exposure.session_date)
                    from quant.intraday_session_exposures exposure
                   where exposure.root_lineage_id =
                         search_rung.root_lineage_id
                      and exposure.dataset_id = search_rung.dataset_id
                      and exposure.session_date = any(
                          search_rung.planned_session_dates)
                      and exposure.exposure_purpose = 'ADAPTIVE_SEARCH'
                      and exposure.knowledge_clock_mode =
                          'EVENT_TIME_HISTORICAL_ONLY'
                      and exposure.knowledge_cutoff <=
                          search_rung.dataset_cutoff
                      and exposure.instrument_count =
                          search_rung.planned_instrument_count
                      and exposure.instrument_set_fingerprint =
                          search_rung.instrument_set_fingerprint
                      and exposure.instrument_ids <@
                          search_rung.planned_instrument_ids
                      and search_rung.planned_instrument_ids <@
                          exposure.instrument_ids
                      and exposure.session_content_fingerprint ~
                          '^[0-9a-f]{64}$'
                      and exposure.exposure_evidence_fingerprint ~
                          '^[0-9a-f]{64}$'
                      and jsonb_typeof(exposure.source_watermark) = 'object'
                      and exposure.source_watermark <> '{}'::jsonb
                      and exposure.quote_row_count > 0
                      and exposure.trade_row_count > 0), '[]'::jsonb))
               order by search_rung.rung), '[]'::jsonb) as rungs
      from quant.intraday_experiment_rungs search_rung
     where search_rung.experiment_id = e.experiment_id
       and search_rung.dataset_id = e.dataset_id
       and search_rung.candidate_lineage_id =
           primary_lineage.candidate_lineage_id
       and search_rung.root_lineage_id = primary_lineage.root_lineage_id
       and search_rung.rung in ('DISCOVERY_6','VALIDATION_20')
       and search_rung.evidence_purpose = 'ADAPTIVE_SEARCH'
       and search_rung.experiment_rung_id::text = case search_rung.rung
             when 'DISCOVERY_6' then
               manifest.report#>>'{trial_lockbox,rungs,discovery}'
             when 'VALIDATION_20' then
               manifest.report#>>'{trial_lockbox,rungs,validation}'
             else null end
       and search_rung.planned_session_count > 0
       and cardinality(search_rung.planned_session_dates) =
           search_rung.planned_session_count
       and cardinality(search_rung.planned_instrument_ids) =
           search_rung.planned_instrument_count
       and """ + _raw_rung_content_sql("search_rung") + """
       and """ + _frozen_slice_stock_sql("search_rung") + """
       and not exists (
           select 1
             from unnest(search_rung.planned_instrument_ids)
                  planned_instrument(instrument_id)
             cross join unnest(search_rung.planned_session_dates)
                  planned_session(session_date)
             left join quant.current_krx_stock_instrument_identity identity
               on identity.instrument_id = planned_instrument.instrument_id
            where identity.instrument_id is null
               or coalesce(upper(identity.instrument_type),'') <> 'STOCK'
               or coalesce(upper(identity.asset_class),'') <> 'EQUITY'
               or coalesce(upper(identity.market),'') <> 'KRX'
               or coalesce(upper(identity.status),'') <> 'ACTIVE'
               or coalesce(identity.is_spac, true)
               or (identity.listed_from is not null and
                   identity.listed_from > planned_session.session_date)
               or (identity.listed_to is not null and
                   identity.listed_to < planned_session.session_date))
       and (
           select count(distinct exposure.session_date)
             from quant.intraday_session_exposures exposure
            where exposure.root_lineage_id = search_rung.root_lineage_id
              and exposure.dataset_id = search_rung.dataset_id
              and exposure.exposure_purpose = 'ADAPTIVE_SEARCH'
              and exposure.knowledge_clock_mode =
                  'EVENT_TIME_HISTORICAL_ONLY'
              and exposure.session_date = any(
                  search_rung.planned_session_dates)
              and exposure.instrument_count =
                  search_rung.planned_instrument_count
              and exposure.knowledge_cutoff <= search_rung.dataset_cutoff
              and exposure.instrument_set_fingerprint =
                  search_rung.instrument_set_fingerprint
              and exposure.instrument_ids <@
                  search_rung.planned_instrument_ids
              and search_rung.planned_instrument_ids <@
                  exposure.instrument_ids
              and exposure.session_content_fingerprint ~
                  '^[0-9a-f]{64}$'
              and exposure.exposure_evidence_fingerprint ~
                  '^[0-9a-f]{64}$'
              and jsonb_typeof(exposure.source_watermark) = 'object'
              and exposure.source_watermark <> '{}'::jsonb
              and exposure.quote_row_count > 0
              and exposure.trade_row_count > 0
       ) = search_rung.planned_session_count
  ) governed on jsonb_array_length(governed.rungs) > 0
 where e.config ? 'screening_population'
   and e.config->>'asset_scope' = 'REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY'
   and manifest.manifest_version = '""" + \
    INTRADAY_REPORT_MANIFEST_VERSION + """'
   and jsonb_typeof(manifest.report->'discovery_rungs') = 'array'
   and jsonb_array_length(manifest.report->'discovery_rungs') > 0
   and """ + _RAW_INTRADAY_DATASET + """
 order by e.created_at desc
 limit 128
"""


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _sequence(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    return list(value) if isinstance(value, (list, tuple)) else []


def _observed_at(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            return ""
    else:
        return ""
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return ""
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _calibration_memory(row: dict[str, Any]) -> tuple[tuple[str, ...],
                                                       dict[str, float]]:
    calibration = _mapping(row.get("score_calibration"))
    codes = {
        str(code).strip().upper()
        for code in row.get("lesson_codes") or () if str(code).strip()
    }
    status = str(calibration.get("status") or "").strip().upper()
    if status:
        codes.add(status)
    diagnostics: dict[str, float] = {}
    for target, key in (
        ("calibration_observations", "observations"),
        ("min_cost_hurdle_bps", "minimum_observed_entry_hurdle_bps"),
        ("max_calibrated_markout_bps",
         "maximum_calibrated_predicted_markout_bps"),
    ):
        number = _finite(calibration.get(key))
        if number is not None:
            diagnostics[target] = number
    return tuple(sorted(codes)), diagnostics


def _mechanism(contract: dict[str, Any], title: str,
               stated_mechanism: str = "") -> str:
    thesis = _mapping(contract.get("formula_thesis"))
    return str(
        thesis.get("identification") or stated_mechanism or title
        or "SOURCE_BACKED_MICROSTRUCTURE_MECHANISM")


def _source_baseline_ast_fingerprint(
        contract: Mapping[str, Any]) -> str | None:
    baseline = contract.get("source_baseline_expr")
    if baseline is None:
        return None
    if not isinstance(baseline, dict):
        raise ValueError("source_baseline_expr must be an AST object or null")
    return _stable_fingerprint(grammar.parse(baseline))


def _source_contract_fingerprint(lead: Mapping[str, Any]) -> str:
    """Return a recomputed baseline-aware executable parent identity."""
    expression = grammar.parse(lead.get("expression"))
    contract = _mapping(lead.get("contract"))
    plan = _mapping(contract.get("semantic_plan"))
    thesis = _mapping(contract.get("formula_thesis"))
    window_contract = str(
        contract.get("feature_window_contract_version") or
        grammar.LEGACY_FEATURE_WINDOW_CONTRACT)
    identity = {
        "candidate_ast": _stable_fingerprint(expression),
        "semantic_plan": _stable_fingerprint(plan),
        "baseline_ast": _source_baseline_ast_fingerprint(contract),
        "horizon_seconds": plan.get("horizon_seconds"),
        "execution": str(plan.get("execution") or "").upper(),
        "entry_policy": str(thesis.get("decision_rule") or "").upper(),
        "coefficient_policy": str(
            thesis.get("coefficient_policy") or "").upper(),
    }
    # Preserve every V1 parent identity byte-for-byte so historical measured
    # outcomes keep retiring/feeding the same source contracts.  V2 is a new
    # executable feature identity and therefore binds its explicit contract.
    if window_contract != grammar.LEGACY_FEATURE_WINDOW_CONTRACT:
        identity["feature_window_contract_version"] = window_contract
    computed = _stable_fingerprint(identity)
    # A cached value is an assertion, never authority over executable content.
    claimed = str(lead.get("source_contract_fingerprint") or "").strip()
    if claimed and claimed != computed:
        raise ValueError(
            "source_contract_fingerprint does not match lead contract")
    return computed


def _lead_records(rows: Iterable[tuple[Any, ...]]) -> list[dict[str, Any]]:
    records = []
    for lead_id, title, stated, raw_contract, used in rows:
        contract = _mapping(raw_contract)
        expression = contract.get("candidate_signal_expr")
        if not isinstance(expression, dict):
            continue
        plan = _mapping(contract.get("semantic_plan"))
        try:
            window_contract = str(
                contract.get("feature_window_contract_version") or
                grammar.LEGACY_FEATURE_WINDOW_CONTRACT)
            parsed = grammar.validate_feature_window_contract(
                expression, contract_version=window_contract)
            parsed = grammar.validate_completed_second_candidate(
                parsed,
                execution=plan.get("execution") or "TAKER")
            baseline_identity = _source_baseline_ast_fingerprint(contract)
        except (TypeError, ValueError, grammar.IntradayExprError):
            continue
        ast_identity = _stable_fingerprint(parsed)
        semantic_identity = _stable_fingerprint(plan)
        record = {
            "lead_id": str(lead_id),
            "title": str(title or ""),
            "expression": parsed,
            "fingerprint": grammar.fingerprint(parsed),
            "candidate_ast_fingerprint": ast_identity,
            "semantic_plan_fingerprint": semantic_identity,
            "baseline_ast_fingerprint": baseline_identity,
            "contract": contract,
            "feature_window_contract_version": window_contract,
            "used": bool(used),
            "economic_mechanism": _mechanism(
                contract, str(title or ""), str(stated or "")),
        }
        record["source_contract_fingerprint"] = \
            _source_contract_fingerprint(record)
        records.append(record)
    return records


_CALIBRATION_FAILURE_CODES = frozenset({
    "NO_COST_FEASIBLE_ENTRY", "NON_POSITIVE_DIRECTIONAL_RELATION",
    "CALIBRATION_COST_INFEASIBLE",
    "CALIBRATION_DIRECTION_NON_POSITIVE",
})
_NO_EVIDENCE_CODES = frozenset({
    "NO_EXECUTABLE_OBSERVATIONS", "INFRA_FAILURE",
    "FORWARD_RUNTIME_ARTIFACT_UNAVAILABLE",
})
_MEASURED_FORMULA_FAILURE_CODES = frozenset({
    "CAUSALITY_NOT_PASS", "NET_EDGE_NOT_POSITIVE",
    "SESSION_BOOTSTRAP_CI_CROSSES_ZERO", "WALK_FORWARD_FOLDS_FRAGILE",
    "OVERFIT_DSR", "OVERFIT_PBO", "INSTRUMENT_COVERAGE_BELOW_MINIMUM",
    "OPPORTUNITIES_BELOW_MINIMUM", "PASSIVE_FILL_RATE_TOO_LOW",
    "SCORE_CALIBRATION_NOT_USABLE",
}) | _CALIBRATION_FAILURE_CODES


def _codes(*values: Any) -> tuple[str, ...]:
    result: set[str] = set()
    for raw in values:
        if isinstance(raw, Mapping):
            raw = [raw.get("classification"), raw.get("calibration_status")]
        elif isinstance(raw, str):
            raw = raw.replace("|", ",").split(",")
        if not isinstance(raw, (list, tuple, set, frozenset)):
            raw = [raw]
        result.update(str(value).strip().upper() for value in raw
                      if value not in (None, "") and str(value).strip())
    return tuple(sorted(result))


def _candidate_index(config: dict[str, Any], population: list[Any],
                     lineages: dict[str, Any], trial_lockbox: dict[str, Any]
                     ) -> dict[str, dict[str, Any]]:
    """Return only ASTs whose persisted key agrees with their exact content."""
    indexed: dict[str, dict[str, Any]] = {}

    def add(key: str, payload: Mapping[str, Any], *, primary: bool) -> None:
        expression = payload.get("intraday_signal_expr")
        if not isinstance(expression, dict):
            return
        try:
            window_contract = str(
                payload.get("feature_window_contract_version") or
                config.get("feature_window_contract_version") or
                grammar.LEGACY_FEATURE_WINDOW_CONTRACT)
            parsed = grammar.validate_feature_window_contract(
                expression, contract_version=window_contract)
            configured_baseline = _source_baseline_ast_fingerprint(payload)
        except (TypeError, ValueError, grammar.IntradayExprError):
            return
        exact = grammar.fingerprint(parsed)
        declared = str(payload.get("ast_fingerprint") or "").strip()
        if declared and declared != exact:
            return
        plan = _mapping(payload.get("semantic_plan"))
        raw_horizon = payload.get("horizon_seconds")
        plan_horizon = plan.get("horizon_seconds")
        raw_execution = str(payload.get("execution") or "").strip().upper()
        plan_execution = str(plan.get("execution") or "").strip().upper()
        if (isinstance(raw_horizon, bool)
                or not isinstance(raw_horizon, int)
                or raw_horizon <= 0
                or plan_horizon != raw_horizon
                or not raw_execution
                or plan_execution != raw_execution):
            return
        lineage = _mapping(lineages.get(exact))
        expected_evaluator = _evaluator_for_window_contract(window_contract)
        if (not lineage
                or not lineage_identity_matches(lineage)
                or lineage.get("candidate_ast_fingerprint") !=
                _stable_fingerprint(parsed)
                or lineage.get("semantic_plan_fingerprint") !=
                _stable_fingerprint(plan)
                or lineage.get("baseline_ast_fingerprint") !=
                configured_baseline
                or lineage.get("evaluator_version") != expected_evaluator
                or lineage.get("cost_model_version") !=
                ACTIVE_COST_MODEL_VERSION
                or not str(lineage.get("economic_family_id") or "").strip()):
            return
        indexed["PRIMARY" if primary else exact] = {
            "expression": parsed,
            "fingerprint": exact,
            "semantic_plan": plan,
            "horizon_seconds": raw_horizon,
            "execution": raw_execution,
            "feature_window_contract_version": window_contract,
            "lineage": lineage,
            "candidate_identity_fingerprint": str(
                lineage["candidate_identity_fingerprint"]),
            "candidate_ast_fingerprint": str(
                lineage["candidate_ast_fingerprint"]),
            "semantic_plan_fingerprint": str(
                lineage["semantic_plan_fingerprint"]),
            "baseline_ast_fingerprint": (
                str(lineage["baseline_ast_fingerprint"])
                if lineage.get("baseline_ast_fingerprint") is not None
                else None),
            "candidate_lineage_id": str(lineage["candidate_lineage_id"]),
            "root_lineage_id": str(lineage["root_lineage_id"]),
            "source_lead_ids": sorted({
                str(value) for value in _sequence(
                    payload.get("source_lead_ids")) if str(value).strip()}),
            "economic_family_id": str(lineage["economic_family_id"]),
            "payload": dict(payload),
        }

    add("PRIMARY", config, primary=True)
    source = _sequence(config.get("screening_population")) or population
    for candidate in source:
        if isinstance(candidate, Mapping):
            add(str(candidate.get("ast_fingerprint") or ""), candidate,
                primary=False)
    expected_lineage_keys = {
        row["fingerprint"] for row in indexed.values()}
    registered = _mapping(trial_lockbox.get(
        "registered_candidate_lineages"))
    primary = indexed.get("PRIMARY")
    roots = {str(row["lineage"].get("root_lineage_id") or "")
             for row in indexed.values()}
    if (primary is None or set(lineages) != expected_lineage_keys
            or set(registered) != expected_lineage_keys
            or len(indexed) != 1 + len(source)
            or len(roots) != 1 or "" in roots
            or next(iter(roots)) != str(
                trial_lockbox.get("root_lineage_id") or "")
            or str(primary["lineage"].get("candidate_lineage_id") or "") !=
            str(trial_lockbox.get("primary_candidate_lineage_id") or "")
            or any(str(row["lineage"].get("candidate_lineage_id") or "") !=
                   str(registered.get(row["fingerprint"]) or "")
                   for row in indexed.values())):
        return {}
    return indexed


def _primary_lineage_memory(
        expression: dict[str, Any], config: dict[str, Any], raw_lineage: Any,
        raw_source_lead_ids: Any) -> dict[str, Any]:
    """Verify the active primary's durable identity before using its memory."""

    lineage = _mapping(raw_lineage)
    plan = _mapping(config.get("semantic_plan"))
    try:
        window_contract = str(
            config.get("feature_window_contract_version") or
            grammar.LEGACY_FEATURE_WINDOW_CONTRACT)
        parsed = grammar.validate_feature_window_contract(
            expression, contract_version=window_contract)
        configured_baseline = _source_baseline_ast_fingerprint(config)
    except (TypeError, ValueError, grammar.IntradayExprError):
        return {}
    expected_evaluator = _evaluator_for_window_contract(window_contract)
    horizon = config.get("horizon_seconds")
    execution = str(config.get("execution") or "").strip().upper()
    if (not lineage_identity_matches(lineage)
            or lineage.get("candidate_ast_fingerprint") !=
            _stable_fingerprint(parsed)
            or lineage.get("semantic_plan_fingerprint") !=
            _stable_fingerprint(plan)
            or lineage.get("baseline_ast_fingerprint") != configured_baseline
            or lineage.get("evaluator_version") != expected_evaluator
            or lineage.get("cost_model_version") != ACTIVE_COST_MODEL_VERSION
            or isinstance(horizon, bool) or not isinstance(horizon, int)
            or horizon <= 0 or plan.get("horizon_seconds") != horizon
            or not execution
            or str(plan.get("execution") or "").upper() != execution
            or not str(lineage.get("candidate_lineage_id") or "")
            or not str(lineage.get("root_lineage_id") or "")
            or not str(lineage.get("economic_family_id") or "")):
        return {}
    return {
        "candidate_identity_fingerprint": str(
            lineage["candidate_identity_fingerprint"]),
        "candidate_ast_fingerprint": str(
            lineage["candidate_ast_fingerprint"]),
        "semantic_plan_fingerprint": str(
            lineage["semantic_plan_fingerprint"]),
        "baseline_ast_fingerprint": (
            str(lineage["baseline_ast_fingerprint"])
            if lineage.get("baseline_ast_fingerprint") is not None else None),
        "candidate_lineage_id": str(lineage["candidate_lineage_id"]),
        "root_lineage_id": str(lineage["root_lineage_id"]),
        "source_lead_ids": sorted({
            str(value) for value in _sequence(raw_source_lead_ids)
            if str(value).strip()}),
        "economic_family_id": str(lineage["economic_family_id"]),
        "evaluator_version": str(lineage["evaluator_version"]),
        "cost_model_version": str(lineage["cost_model_version"]),
        "horizon_seconds": horizon,
        "execution": execution,
        "feature_window_contract_version": window_contract,
    }


def _search_exposure_fingerprint(
        rung: dict[str, Any], candidates: dict[str, dict[str, Any]],
        *, rung_name: str, evidence_scope: str) -> tuple[str, str]:
    exposure = _mapping(rung.get("search_exposure"))
    claimed = str(rung.get("search_exposure_fingerprint") or "")
    if (not has_exact_search_exposure_declaration(exposure)
            or search_exposure_validation_error(exposure) is not None
            or exposure.get("adaptive_search_only") is not True
            or exposure.get("promotion_authority") is not False
            or exposure.get("rung") != rung_name
            or exposure.get("search_exposure_fingerprint") != claimed
            or len(claimed) != 64
            or any(char not in "0123456789abcdef" for char in claimed)):
        return "", ""
    # The producer hashes the complete scientific identity, including the
    # fingerprint contract and the declaration of which volatile identifiers
    # were excluded.  Only the self-referential digest is removed here.
    if adaptive_search_exposure_fingerprint(exposure) != claimed:
        return "", ""
    evaluator = _mapping(exposure.get("evaluator_contract"))
    cost = _mapping(exposure.get("cost_contract"))
    evaluation = _mapping(exposure.get("evaluation"))
    measurement_scope = str(evaluation.get("measurement_scope") or "")
    expected_evaluators = {
        _evaluator_for_window_contract(
            candidate.get("feature_window_contract_version"))
        for candidate in candidates.values()
    }
    if (len(expected_evaluators) != 1
            or evaluator.get("evaluator_version") !=
            next(iter(expected_evaluators))
            or cost.get("cost_model_version") != ACTIVE_COST_MODEL_VERSION
            or measurement_scope not in {
                "ADAPTIVE_RUNG_MEASURED",
                "CALIBRATION_ONLY_RESOURCE_STOP",
            }):
        return "", ""
    raw_contracts = _sequence(evaluator.get("candidate_contracts"))
    contracts = {str(row.get("candidate") or ""): _mapping(row)
                 for row in raw_contracts if isinstance(row, Mapping)}
    if (len(contracts) != len(raw_contracts)
            or set(contracts) != set(candidates)
            or evaluator.get("candidate_set_fingerprint") !=
            _stable_fingerprint(raw_contracts)):
        return "", ""
    for key, candidate in candidates.items():
        contract = contracts.get(key, {})
        lineage = candidate["lineage"]
        if (contract.get("ast_fingerprint") != candidate["fingerprint"]
                or contract.get("semantic_plan_fingerprint") !=
                _stable_fingerprint(candidate["semantic_plan"])
                or contract.get("horizon_seconds") !=
                candidate["horizon_seconds"]
                or str(contract.get("execution") or "").upper() !=
                candidate["execution"]
                or contract.get("clock_domains") != sorted(
                    grammar.effective_clock_domains_of(
                        candidate["expression"]))
                or _stable_fingerprint(contract.get("feature_contract")) !=
                lineage.get("feature_spec_fingerprint")
                or _stable_fingerprint(contract.get("label_contract")) !=
                lineage.get("label_spec_fingerprint")
                or _stable_fingerprint(contract.get("model_contract")) !=
                lineage.get("model_spec_fingerprint")):
            return "", ""
    if evidence_scope not in {"F1", "F2"}:
        return "", ""
    return claimed, measurement_scope


def _governed_search_exposure_matches(
        exposure: Mapping[str, Any], governed: Mapping[str, Any],
        trial_lockbox: Mapping[str, Any]) -> bool:
    """Cross-check report identity against append-only rung/exposure rows."""

    dataset = _mapping(exposure.get("dataset"))
    evaluation = _mapping(exposure.get("evaluation"))
    content = _mapping(exposure.get("content_evidence"))
    source = _mapping(exposure.get("source_contract"))
    governed_source = _mapping(governed.get("source_watermark"))
    planned_sessions = [str(value) for value in _sequence(
        governed.get("planned_session_dates"))]
    planned_instruments = sorted(str(value) for value in _sequence(
        governed.get("planned_instrument_ids")))
    evidence_rows = [_mapping(value) for value in _sequence(
        governed.get("session_evidence"))]
    content_rows = [_mapping(value) for value in _sequence(
        content.get("per_session"))]
    if (not planned_sessions or not planned_instruments
            or str(governed.get("root_lineage_id") or "") != str(
                trial_lockbox.get("root_lineage_id") or "")
            or str(governed.get("dataset_id") or "") != str(
                dataset.get("dataset_id") or "")
            or _observed_at(governed.get("dataset_cutoff")) !=
            _observed_at(dataset.get("dataset_cutoff"))
            or governed.get("planned_session_count") != len(planned_sessions)
            or governed.get("planned_instrument_count") !=
            len(planned_instruments)
            or evaluation.get("planned_sessions") != planned_sessions
            or evaluation.get("planned_session_count") != len(planned_sessions)
            or evaluation.get("session_set_fingerprint") !=
            str(governed.get("session_set_fingerprint") or "")
            or _stable_fingerprint(planned_sessions) !=
            str(governed.get("session_set_fingerprint") or "")
            or evaluation.get("full_universe_instrument_count") !=
            len(planned_instruments)
            or evaluation.get("full_universe_reference_set_fingerprint") !=
            _stable_fingerprint(planned_instruments)
            or _stable_fingerprint(planned_instruments) !=
            str(governed.get("instrument_set_fingerprint") or "")
            or str(source.get("event_source") or "") != str(
                governed_source.get("event_source") or "")
            or _sequence(source.get("source_lineage")) !=
            _sequence(governed_source.get("source_lineage"))
            or len(evidence_rows) != len(planned_sessions)
            or len(content_rows) != len(planned_sessions)):
        return False
    for session, report_row, ledger_row in zip(
            planned_sessions, content_rows, evidence_rows):
        if (str(ledger_row.get("session") or "") != session
                or report_row.get("session") != session
                or report_row.get("session_content_fingerprint") !=
                ledger_row.get("session_content_fingerprint")
                or report_row.get("quote_rows") != ledger_row.get("quote_rows")
                or report_row.get("trade_rows") != ledger_row.get("trade_rows")
                or _mapping(report_row.get("source_watermark")) !=
                _mapping(ledger_row.get("source_watermark"))
                or ledger_row.get("instrument_count") !=
                len(planned_instruments)
                or ledger_row.get("instrument_set_fingerprint") !=
                governed.get("instrument_set_fingerprint")):
            return False
    return True


def _screen_memory_records(
    *, experiment_id: str, config: dict[str, Any], population: list[Any],
    artifacts: dict[str, Any], discovery_rungs: list[Any],
    trial_lockbox: dict[str, Any], governed_rungs: list[Any],
    lineages: dict[str, Any],
    title: str, created_at: Any,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    candidates = _candidate_index(
        config, population, lineages, trial_lockbox)
    exposures = {
        str(row.get("rung") or "").strip().upper(): _mapping(row)
        for row in _sequence(trial_lockbox.get("exposures"))
        if isinstance(row, Mapping)
    }
    authorized = {
        str(row.get("rung") or "").strip().upper(): _mapping(row)
        for row in governed_rungs if isinstance(row, Mapping)
    }
    history: list[dict[str, Any]] = []
    fidelity = {"DISCOVERY_6": "F1", "VALIDATION_20": "F2"}
    for raw_rung in discovery_rungs:
        rung = _mapping(raw_rung)
        rung_name = str(rung.get("rung") or "").strip().upper()
        scope = fidelity.get(rung_name)
        if scope is None:
            continue
        exposure = exposures.get(rung_name, {})
        governed = authorized.get(rung_name, {})
        if (not governed
                or str(governed.get("rung_plan_fingerprint") or "") != str(
                    exposure.get("rung_plan_fingerprint") or "")):
            continue
        evidence_rows = [_mapping(value) for value in _sequence(
            rung.get("candidate_evidence"))]
        evidence_keys = [str(row.get("candidate") or "")
                         for row in evidence_rows]
        if (rung.get("candidate_count") != len(evidence_rows)
                or len(evidence_keys) != len(set(evidence_keys))
                or evidence_keys.count("PRIMARY") != 1
                or any(key not in candidates for key in evidence_keys)):
            continue
        rung_candidates = {key: candidates[key] for key in evidence_keys}
        search_exposure_fp, measurement_scope = _search_exposure_fingerprint(
            rung, rung_candidates, rung_name=rung_name, evidence_scope=scope)
        completed_at = _observed_at(rung.get("completed_at"))
        if (not search_exposure_fp or not completed_at
                or not _governed_search_exposure_matches(
                    _mapping(rung.get("search_exposure")), governed,
                    trial_lockbox)):
            continue
        if any(
                evidence.get("evidence_scope") != scope
                or evidence.get("adaptive_search_only") is not True
                or evidence.get("promotion_authority") is not False
                or evidence.get("measurement_scope") != measurement_scope
                or evidence.get("search_exposure_fingerprint") !=
                search_exposure_fp
                or _observed_at(evidence.get("observed_at")) != completed_at
                for evidence in evidence_rows):
            continue
        exposure_evaluation = _mapping(_mapping(
            rung.get("search_exposure")).get("evaluation"))
        expected_evaluated_sessions = exposure_evaluation.get(
            "evaluated_session_count")
        if any(
                _mapping(evidence.get("search_objectives")).get("complete")
                is True
                and _mapping(evidence.get("search_objectives")).get(
                    "sessions") != expected_evaluated_sessions
                for evidence in evidence_rows):
            continue
        # ``survivors`` is the explicit measured futility gate.  Successive
        # halving is a separate resource budget; at VALIDATION_20 that budget
        # intentionally reserves the sole FULL slot for PRIMARY and therefore
        # must not turn every linked formula into an economic failure.
        survivors = {str(value) for value in _sequence(rung.get("survivors"))}
        primary_pass = rung.get("primary_pass")
        if (not isinstance(primary_pass, bool)
                or ("PRIMARY" in survivors) != primary_pass
                or not survivors.issubset(set(evidence_keys))):
            continue
        selection = _mapping(rung.get("next_rung_selection"))
        selected_next = {str(value) for value in _sequence(
            selection.get("selected_linked_ast_fingerprints"))}
        eliminated_next = {str(value) for value in _sequence(
            selection.get("eliminated_linked_ast_fingerprints"))}
        linked_keys = set(evidence_keys) - {"PRIMARY"}
        if selection and (
                not selected_next.issubset(survivors)
                or selected_next & eliminated_next
                or selected_next | eliminated_next != linked_keys):
            continue
        for evidence in evidence_rows:
            key = str(evidence.get("candidate") or "")
            candidate = candidates.get(key)
            if candidate is None:
                continue
            evidence_observed_at = _observed_at(evidence.get("observed_at"))
            expression = candidate["expression"]
            objectives = _mapping(evidence.get("search_objectives"))
            summary = _mapping(evidence.get("summary"))
            failure_memory = _mapping(evidence.get("adaptive_failure_memory"))
            failure_codes = _codes(
                evidence.get("failed_criteria"), failure_memory)
            sessions = objectives.get("sessions", summary.get("sessions", 0))
            opportunities = objectives.get(
                "opportunities", summary.get("opportunities", 0))
            explicit_survivor = key in survivors
            if set(failure_codes) & _CALIBRATION_FAILURE_CODES:
                decision = "FAILED"
                evidence_status = "CALIBRATION_MEASURED"
            elif set(failure_codes) & _NO_EVIDENCE_CODES:
                decision = "NO_EVIDENCE"
                evidence_status = "NO_EVIDENCE"
            elif explicit_survivor:
                decision = "SCREENING_ONLY"
                evidence_status = (
                    "MEASURED" if objectives.get("complete") is True
                    else "INVALID")
            elif objectives.get("complete") is True:
                decision = "FUTILITY_GATE_REJECTED"
                evidence_status = "MEASURED"
            else:
                decision = "NO_EVIDENCE"
                evidence_status = "INVALID"
            if selection:
                resource_status = (
                    "SELECTED_NEXT_RUNG" if key == "PRIMARY" or
                    key in selected_next else "NOT_SELECTED_BUDGET")
            else:
                resource_status = "TERMINAL_NO_NEXT_RUNG"
            history.append({
                "archive_history": True,
                "expression": expression,
                "decision": decision,
                "observation_id": (
                    f"{experiment_id}:{rung_name}:{key}"),
                "experiment_id": experiment_id,
                "title": f"{title} [{rung_name} {key[:8]}]",
                "created_at": completed_at,
                "observed_at": evidence_observed_at,
                "evidence_scope": scope,
                "explicit_survivor": explicit_survivor,
                "evidence_status": evidence_status,
                "measurement_scope": measurement_scope,
                "futility_gate_status": (
                    "SURVIVED" if explicit_survivor else
                    "NOT_EVALUABLE" if evidence_status in {
                        "NO_EVIDENCE", "INVALID"} else "REJECTED"),
                "resource_allocation_status": resource_status,
                "promotion_authority": False,
                "exposure_fingerprint": search_exposure_fp,
                "candidate_identity_fingerprint": candidate[
                    "candidate_identity_fingerprint"],
                "candidate_ast_fingerprint": candidate[
                    "candidate_ast_fingerprint"],
                "semantic_plan_fingerprint": candidate[
                    "semantic_plan_fingerprint"],
                "baseline_ast_fingerprint": candidate[
                    "baseline_ast_fingerprint"],
                "candidate_lineage_id": candidate["candidate_lineage_id"],
                "root_lineage_id": candidate["root_lineage_id"],
                "source_lead_ids": list(candidate["source_lead_ids"]),
                "economic_family_id": candidate["economic_family_id"],
                "evaluator_version": candidate["lineage"][
                    "evaluator_version"],
                "cost_model_version": candidate["lineage"][
                    "cost_model_version"],
                "horizon_seconds": candidate["horizon_seconds"],
                "clock_domains": sorted(
                    grammar.effective_clock_domains_of(expression)),
                "sessions": sessions,
                "opportunities": opportunities,
                "search_objectives": objectives,
                "failure_codes": list(failure_codes),
                "lesson_codes": list(failure_codes),
                "calibration_status": str(
                    failure_memory.get("calibration_status") or ""),
                "diagnostics": {
                    "calibration_observations": failure_memory.get(
                        "observations"),
                    "min_cost_hurdle_bps": failure_memory.get(
                        "minimum_observed_entry_hurdle_bps"),
                    "max_calibrated_markout_bps": failure_memory.get(
                        "maximum_calibrated_predicted_markout_bps"),
                },
            })

    final_rows: list[dict[str, Any]] = []
    for key, candidate in sorted(candidates.items()):
        if key == "PRIMARY":
            continue
        artifact = _mapping(artifacts.get(candidate["fingerprint"]))
        if not artifact:
            continue
        calibration = _mapping(artifact.get("score_calibration"))
        failure_codes = _codes(
            artifact.get("failed_criteria"),
            calibration.get("status"),
            artifact.get("adaptive_failure_memory"),
        )
        if set(failure_codes) & _NO_EVIDENCE_CODES:
            decision = "NO_EVIDENCE"
        elif set(failure_codes) & _MEASURED_FORMULA_FAILURE_CODES:
            decision = "FAILED"
        else:
            decision = "SCREENING_ONLY"
        row = {
            "expression": candidate["expression"],
            "decision": decision,
            "lesson_codes": list(failure_codes),
            "oos_summary": _mapping(artifact.get("summary")),
            "score_calibration": calibration,
            "observation_id": (
                f"{experiment_id}:FINAL_SCREEN:{candidate['fingerprint']}"),
            "experiment_id": experiment_id,
            "title": f"{title} [final screen {candidate['fingerprint'][:8]}]",
            "created_at": _observed_at(created_at),
            "evidence_scope": "ADAPTIVE_SCREENING",
            "candidate_identity_fingerprint": candidate[
                "candidate_identity_fingerprint"],
            "candidate_ast_fingerprint": candidate[
                "candidate_ast_fingerprint"],
            "semantic_plan_fingerprint": candidate[
                "semantic_plan_fingerprint"],
            "baseline_ast_fingerprint": candidate[
                "baseline_ast_fingerprint"],
            "candidate_lineage_id": candidate["candidate_lineage_id"],
            "root_lineage_id": candidate["root_lineage_id"],
            "source_lead_ids": list(candidate["source_lead_ids"]),
            "economic_family_id": candidate["economic_family_id"],
            "evaluator_version": candidate["lineage"]["evaluator_version"],
            "cost_model_version": candidate["lineage"]["cost_model_version"],
            "horizon_seconds": candidate["horizon_seconds"],
        }
        codes, diagnostics = _calibration_memory(row)
        row["lesson_codes"] = list(codes)
        if set(codes) & _CALIBRATION_FAILURE_CODES:
            row["decision"] = "FAILED"
        row["diagnostics"] = diagnostics
        final_rows.append(row)
    return history, final_rows


def _outcome_records(primary_rows: Iterable[tuple[Any, ...]],
                     screen_rows: Iterable[tuple[Any, ...]]) -> list[dict[str, Any]]:
    primary_records: dict[str, dict[str, Any]] = {}
    for raw_primary in primary_rows:
        if len(raw_primary) == 11:
            (experiment_id, expression, decision, lesson_codes, summary,
             calibration, title, created_at, raw_config,
             raw_source_lead_ids, raw_lineage) = raw_primary
            config = _mapping(raw_config)
            identity = _primary_lineage_memory(
                expression, config, raw_lineage, raw_source_lead_ids)
            if not identity:
                continue
        elif len(raw_primary) == 8:  # in-process legacy fixture: audit only
            (experiment_id, expression, decision, lesson_codes, summary,
             calibration, title, created_at) = raw_primary
            identity = {}
        else:
            continue
        if not isinstance(expression, dict):
            continue
        row = {
            "expression": expression,
            "decision": str(decision or "UNRESOLVED"),
            "lesson_codes": list(lesson_codes or ()),
            "oos_summary": _mapping(summary),
            "score_calibration": _mapping(calibration),
            "observation_id": f"{experiment_id}:PRIMARY",
            "experiment_id": str(experiment_id),
            "title": str(title or ""),
            "created_at": _observed_at(created_at),
            "evidence_scope": "PRIMARY_DISCOVERY",
            **identity,
        }
        codes, diagnostics = _calibration_memory(row)
        row["lesson_codes"] = list(codes)
        if set(codes) & {
                "NO_COST_FEASIBLE_ENTRY",
                "NON_POSITIVE_DIRECTIONAL_RELATION"}:
            # A measured calibration failure is selection-visible search
            # memory even when an older/partial outcome writer left the
            # high-level decision blank.  Never let it masquerade as an
            # unresolved seed and re-enter the population.
            row["decision"] = "FAILED"
        row["diagnostics"] = diagnostics
        primary_records[row["observation_id"]] = row

    histories: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []
    for raw in screen_rows:
        if len(raw) == 10:
            (experiment_id, raw_config, raw_population, raw_artifacts,
             raw_rungs, raw_lockbox, title, created_at,
             raw_governed_rungs, raw_lineages) = raw
            config = _mapping(raw_config)
            population = _sequence(raw_population)
            artifacts = _mapping(raw_artifacts)
            rungs = _sequence(raw_rungs)
            lockbox = _mapping(raw_lockbox)
            governed_rungs = _sequence(raw_governed_rungs)
            lineages = _mapping(raw_lineages)
        elif len(raw) == 8:  # pre-v2 in-process caller, not the DB loader
            (experiment_id, raw_config, raw_population, raw_artifacts,
             raw_rungs, raw_lockbox, title, created_at) = raw
            config = _mapping(raw_config)
            population = _sequence(raw_population)
            artifacts = _mapping(raw_artifacts)
            rungs = _sequence(raw_rungs)
            lockbox = _mapping(raw_lockbox)
            governed_rungs = _sequence(lockbox.get("exposures"))
            lineages = {}
        elif len(raw) == 9:  # pre-lineage-memory caller: fail closed
            (experiment_id, raw_config, raw_population, raw_artifacts,
             raw_rungs, raw_lockbox, title, created_at,
             raw_governed_rungs) = raw
            config = _mapping(raw_config)
            population = _sequence(raw_population)
            artifacts = _mapping(raw_artifacts)
            rungs = _sequence(raw_rungs)
            lockbox = _mapping(raw_lockbox)
            governed_rungs = _sequence(raw_governed_rungs)
            lineages = {}
        elif len(raw) == 5:  # legacy unit fixture / pre-v2 caller
            experiment_id, raw_population, raw_artifacts, title, created_at = raw
            population = _sequence(raw_population)
            config = {"screening_population": population}
            artifacts = _mapping(raw_artifacts)
            rungs, lockbox, governed_rungs, lineages = [], {}, [], {}
        else:
            continue
        history, final = _screen_memory_records(
            experiment_id=str(experiment_id), config=config,
            population=population, artifacts=artifacts,
            discovery_rungs=rungs, trial_lockbox=lockbox,
            governed_rungs=governed_rungs,
            lineages=lineages,
            title=str(title or ""), created_at=created_at)
        histories.extend(history)
        final_rows.extend(final)
    # Rung memory is chronological adaptive evidence.  Final full-screen and
    # primary outcomes are appended afterwards so a later measured failure can
    # retire an earlier resource-gate survivor for the same exact AST.
    return histories + final_rows + list(primary_records.values())


def load_records(conn) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load source-backed seeds and governed adaptive memory in one snapshot."""
    with conn.cursor() as cur:
        cur.execute(_LEADS_SQL)
        leads = _lead_records(cur.fetchall())
        cur.execute(_PRIMARY_OUTCOMES_SQL)
        primary = cur.fetchall()
        cur.execute(_CALIBRATION_FAILURES_SQL)
        primary.extend(cur.fetchall())
        cur.execute(_SCREEN_OUTCOMES_SQL)
        screening = cur.fetchall()
    return leads, _outcome_records(primary, screening)


def _event_for(fields: set[str]) -> str:
    if fields & (QUOTE_PRESSURE_FIELDS | TAPE_PRESSURE_FIELDS):
        return "ORDER_FLOW"
    if "microprice_offset_bps" in fields:
        return "MICROPRICE_DISLOCATION"
    if "realized_volatility_bps" in fields:
        return "VOLATILITY_BURST"
    if fields & {"spread_bps", "book_depth_l1", "book_depth_l10"}:
        return "LIQUIDITY_SHOCK"
    return "QUOTE_IMBALANCE"


def _term_role(field: str) -> str:
    if field in (QUOTE_PRESSURE_FIELDS | TAPE_PRESSURE_FIELDS | {
            "microprice_offset_bps", "queue_imbalance_l1",
            "queue_imbalance_l10", "depth_imbalance_slope"}):
        return "PRESSURE"
    if field == "spread_bps":
        return "LIQUIDITY"
    if field == "realized_volatility_bps":
        return "VOLATILITY"
    if field == "quote_age_ms":
        return "FRESHNESS"
    if field in {"trade_count", "quote_count", "trade_intensity",
                 "trade_volume"}:
        return "ACTIVITY"
    if field in {"book_depth_l1", "book_depth_l10", "bid_depth_l1",
                 "ask_depth_l1"}:
        return "CAPACITY"
    if field == "trade_side_known_ratio":
        return "CONFIRMATION"
    return "STATE"


def _operator_contract(operation: str, fields: set[str]) -> list[str]:
    name = str(operation).upper()
    if "DIRECTION_INVERSION" in name:
        return ["FAILURE_MODE_INVERSION"]
    if name.startswith("STATE_GATE"):
        return ["STATE_CONDITION", "EXECUTION_AWARE"]
    if name.startswith("CROSS_SCALE"):
        return ["CLOCK_CHANGE", "CROSS_SCALE_DISAGREEMENT"]
    if name.startswith(("PRIMITIVE_WINDOW", "FEATURE_WINDOW_UPGRADE")):
        return ["PRIMITIVE_WINDOW_MIGRATION", "CLOCK_CHANGE"]
    if name.startswith(("ROLLING_", "EWMA_", "DELTA_", "TEMPORAL_")):
        return ["CLOCK_CHANGE"]
    if fields & L1_QUOTE_PRESSURE_FIELDS and fields & L10_QUOTE_PRESSURE_FIELDS:
        return ["L1_L10_DIVERGENCE"]
    if fields & QUOTE_PRESSURE_FIELDS and fields & TAPE_PRESSURE_FIELDS:
        return ["QUOTE_TAPE_CONFIRMATION"]
    if name.startswith("SAME_UNIT_FIELD_SWAP"):
        return ["TARGET_CHANGE"]
    return ["MECHANISM_INTERACTION"]


def _semantic_hint(expression: dict, parent_contract: dict[str, Any],
                   operation: str) -> tuple[dict[str, Any], dict[str, Any]]:
    fields = set(grammar.fields_of(expression))
    operators = set(grammar.operators_of(expression))
    temporal_clocks = sorted(grammar.temporal_windows_of(expression))
    primitive_windows = sorted(grammar.primitive_windows_of(expression))
    clocks = sorted(set(temporal_clocks) | set(primitive_windows))
    parent_plan = _mapping(parent_contract.get("semantic_plan"))
    contexts = ["ALL"]
    if "where" in operators and "spread_bps" in fields:
        contexts = ["TIGHT_SPREAD"]
    elif "where" in operators and "realized_volatility_bps" in fields:
        contexts = ["LOW_VOLATILITY"]
    elif "where" in operators and fields & {
            "trade_count", "quote_count", "trade_intensity", "trade_volume"}:
        contexts = ["HIGH_ACTIVITY"]
    qualities = []
    if "where" in operators:
        qualities.append("STATE_CONDITIONAL")
    if operators & grammar.TEMPORAL_OPS:
        qualities.append("PERSISTENCE")
    if fields & L1_QUOTE_PRESSURE_FIELDS and fields & L10_QUOTE_PRESSURE_FIELDS:
        qualities.append("L1_L10_DIVERGENCE")
    if (fields & QUOTE_PRESSURE_FIELDS and fields & TAPE_PRESSURE_FIELDS
            and operators & {"mul", "min", "max", "where"}):
        qualities.append("QUOTE_TAPE_CONFIRMATION")
    if len(fields) >= 2 and operators & {"mul", "div", "where"}:
        qualities.append("CROSS_SIGNAL_INTERACTION")
    if not qualities:
        qualities = ["LEVEL"]
    horizon = int(parent_plan.get("horizon_seconds") or 30)
    # A raw-event primitive window is an input aggregation choice, not the
    # future return horizon.  Only an explicit temporal AST transform may
    # lengthen the parent's prediction/label horizon.
    if temporal_clocks:
        horizon = max(horizon, min(max(temporal_clocks), 900))
    direction = str(parent_plan.get("direction") or "FOLLOW").upper()
    if "DIRECTION_INVERSION" in str(operation).upper():
        direction = {"FOLLOW": "REVERT", "REVERT": "FOLLOW"}.get(
            direction, "CONDITIONAL")
    plan = {
        "event": _event_for(fields),
        "context": contexts,
        "qualities": sorted(set(qualities)),
        "direction": direction,
        "output": "TAKER_NET_PNL",
        "execution": "TAKER",
        "horizon_seconds": horizon,
    }
    niche = "MONOTONE"
    if "where" in operators:
        niche = "STATE_CONDITIONAL"
    elif len(clocks) >= 2:
        niche = "CROSS_SCALE"
    elif operators & {"mul", "div"} and len(fields) >= 2:
        niche = "INTERACTION"
    if "DIRECTION_INVERSION" in str(operation).upper():
        niche = "REVERSAL"
    output_unit = grammar.unit_of(expression)
    parent_thesis = _mapping(parent_contract.get("formula_thesis"))
    coefficient_policy = str(
        parent_thesis.get("coefficient_policy") or "STRUCTURE_ONLY").upper()
    if output_unit != "BPS":
        coefficient_policy = "STRUCTURE_ONLY"
    thesis = {
        "target": "TAKER_NET_PNL",
        "functional_form": niche,
        "expected_sign": str(
            parent_thesis.get("expected_sign") or "STATE_DEPENDENT").upper(),
        "coefficient_policy": coefficient_policy,
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {field: _term_role(field) for field in sorted(fields)},
        "identification": "REQUIRES_HERMES_FALSIFIABLE_ECONOMIC_IDENTIFICATION",
    }
    return plan, thesis


def _lead_matches_measured_candidate(lead: Mapping[str, Any],
                                     row: Mapping[str, Any]) -> bool:
    source_ids = {str(value) for value in _sequence(
        row.get("source_lead_ids"))}
    contract = _mapping(lead.get("contract"))
    measured_ast_fingerprint = str(
        row.get("ast_fingerprint")
        or row.get("candidate_ast_fingerprint")
        or "")
    try:
        return (
            str(lead.get("lead_id") or "") in source_ids
            and _stable_fingerprint(grammar.parse(lead.get("expression"))) ==
            measured_ast_fingerprint
            and _stable_fingerprint(_mapping(contract.get("semantic_plan"))) ==
            str(row.get("semantic_plan_fingerprint") or "")
            and _source_baseline_ast_fingerprint(contract) ==
            (str(row["baseline_ast_fingerprint"])
             if row.get("baseline_ast_fingerprint") is not None else None)
        )
    except (TypeError, ValueError, grammar.IntradayExprError):
        return False


def _parent_provenance(
        candidate: Any,
        *,
        lead_by_id: Mapping[str, Mapping[str, Any]],
        canonical_lead_by_contract: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Separate genetic contracts from their one-or-many lead citations."""
    source_ids = sorted({
        str(value) for value in candidate.parent_seed_ids
        if str(value).strip()
    })
    mapped_contracts: set[str] = set()
    mapped_source_ids: list[str] = []
    for lead_id in source_ids:
        lead = lead_by_id.get(lead_id)
        if lead is None:
            continue
        try:
            mapped_contracts.add(_source_contract_fingerprint(lead))
        except (TypeError, ValueError, grammar.IntradayExprError):
            continue
        mapped_source_ids.append(lead_id)
    declared_contracts = {
        str(value) for value in
        candidate.parent_source_contract_fingerprints if str(value).strip()
    }
    mismatch = (
        mapped_source_ids != source_ids
        or not declared_contracts
        or declared_contracts != mapped_contracts
        or any(contract not in canonical_lead_by_contract
               for contract in declared_contracts)
    )
    canonical_ids = sorted({
        str(canonical_lead_by_contract[contract].get("lead_id") or "")
        for contract in declared_contracts
        if contract in canonical_lead_by_contract
        and str(canonical_lead_by_contract[contract].get("lead_id") or "")
    })
    return {
        "source_lead_ids": source_ids,
        "parent_lead_ids": canonical_ids,
        "parent_source_contract_fingerprints": sorted(declared_contracts),
        "parent_contract_count": len(declared_contracts),
        "provenance_mismatch": mismatch,
    }


def _submission_blocker(operation: str,
                        provenance: Mapping[str, Any]) -> str:
    if str(operation).upper() == "SEED":
        return "ALREADY_PERSISTED_SEED"
    if provenance.get("provenance_mismatch"):
        return "PARENT_CONTRACT_PROVENANCE_MISMATCH"
    parent_contract_count = int(provenance.get("parent_contract_count") or 0)
    if parent_contract_count > 1:
        return "MULTI_PARENT_PROVENANCE_REQUIRES_REVIEW"
    if parent_contract_count == 0:
        return "NO_SOURCE_PARENT_MAPPING"
    return ""


def generate_from_records(*, leads: list[dict[str, Any]],
                          outcome_rows: list[dict[str, Any]],
                          population_size: int = 64,
                          generation: int = 1,
                          feature_window_contract_version: str = (
                              grammar.LEGACY_FEATURE_WINDOW_CONTRACT),
                          ) -> dict[str, Any]:
    size = int(population_size)
    if size < MIN_POPULATION or size > MAX_POPULATION:
        raise ValueError(
            f"population_size must be in [{MIN_POPULATION}, {MAX_POPULATION}]")
    if generation < 1:
        raise ValueError("generation must be positive")
    window_contract = str(feature_window_contract_version or "").strip()
    active_evaluator_version = _evaluator_for_window_contract(window_contract)
    if not leads:
        return {
            "ok": False, "module_version": MODULE_VERSION,
            "error": "NO_SOURCE_BACKED_INTRADAY_FORMULA_SEEDS",
            "candidates": [],
        }

    lead_by_contract: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for lead in leads:
        try:
            lead_by_contract[_source_contract_fingerprint(lead)].append(lead)
        except (TypeError, ValueError, grammar.IntradayExprError):
            continue
    valid_leads = sorted(
        (lead for rows in lead_by_contract.values() for lead in rows),
        key=lambda lead: str(lead.get("lead_id") or ""))
    if not valid_leads:
        return {
            "ok": False, "module_version": MODULE_VERSION,
            "error": "NO_CONTRACT_VALID_INTRADAY_FORMULA_SEEDS",
            "source_leads": len(leads), "candidates": [],
        }
    lead_by_id = {
        str(lead.get("lead_id") or ""): lead for lead in valid_leads
        if str(lead.get("lead_id") or "")}
    canonical_lead_by_contract = {
        contract_fp: sorted(
            rows, key=lambda lead: (bool(lead.get("used")),
                                    str(lead.get("lead_id") or "")))[0]
        for contract_fp, rows in sorted(lead_by_contract.items())
    }
    seeds = [FormulaSeed(
        expression=lead["expression"], seed_id=lead["lead_id"],
        source="PERSISTED_LEAD",
        economic_mechanism=lead["economic_mechanism"],
        semantic_plan_fingerprint=_stable_fingerprint(
            _mapping(lead.get("contract")).get("semantic_plan") or {}),
        source_contract_fingerprint=contract_fp,
        source_lead_ids=tuple(sorted(
            str(alias.get("lead_id") or "")
            for alias in lead_by_contract[contract_fp]
            if str(alias.get("lead_id") or ""))),
    ) for contract_fp, lead in canonical_lead_by_contract.items()]
    archive_history = [row for row in outcome_rows
                       if row.get("archive_history") is True]
    search_memory = build_formula_search_memory(
        archive_history,
        active_evaluator_version=active_evaluator_version,
        active_cost_model_version=ACTIVE_COST_MODEL_VERSION)
    elite_candidates = search_memory.get("elite_candidates") or {}
    source_backed_elites: dict[tuple[str, str, str], dict[str, Any]] = {}
    for identity, elite in elite_candidates.items():
        matched = sorted((lead for lead in valid_leads
                          if _lead_matches_measured_candidate(lead, elite)),
                         key=lambda lead: (bool(lead.get("used")),
                                           str(lead.get("lead_id") or "")))
        if matched:
            elite_key = (
                str(elite.get("candidate_identity_fingerprint") or identity),
                str(elite.get("root_lineage_id") or ""),
                str(elite.get("exposure_fingerprint") or ""),
            )
            source_backed_elites[elite_key] = {
                **deepcopy(elite),
                "source_lead_ids": [str(lead["lead_id"]) for lead in matched],
                "source_contract_fingerprints": sorted({
                    _source_contract_fingerprint(lead) for lead in matched}),
            }
    outcomes = []
    for row in outcome_rows:
        expression = row.get("expression")
        if not isinstance(expression, dict):
            continue
        if window_contract == grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT:
            # V2 has no pre-versioned compatibility mode: every memory row must
            # prove that this exact AST was measured by v12 under the active
            # cost model.  A legacy AST must enter as a seed migration and earn
            # a new trial, never be relabelled as measured V2 memory.
            if (row.get("evaluator_version") != active_evaluator_version
                    or row.get("cost_model_version") !=
                    ACTIVE_COST_MODEL_VERSION):
                continue
        elif ((row.get("evaluator_version") and
               row.get("evaluator_version") != active_evaluator_version)
              or (row.get("cost_model_version") and
                  row.get("cost_model_version") !=
                  ACTIVE_COST_MODEL_VERSION)):
            continue
        try:
            expression = grammar.validate_feature_window_contract(
                expression, contract_version=window_contract)
        except (TypeError, ValueError, grammar.IntradayExprError):
            continue
        identity = str(row.get("candidate_identity_fingerprint") or "")
        verified_leads = sorted((
            lead for lead in valid_leads
            if _lead_matches_measured_candidate(lead, row)),
            key=lambda lead: (bool(lead.get("used")),
                              str(lead.get("lead_id") or "")))
        verified_contracts = sorted({
            _source_contract_fingerprint(lead) for lead in verified_leads})
        elite_key = (
            identity,
            str(row.get("root_lineage_id") or ""),
            str(row.get("exposure_fingerprint") or ""),
        )
        mechanism = ((verified_leads[0].get("economic_mechanism")
                      if verified_leads else None)
                     or row.get("title") or "MEASURED_FORMULA")
        adapted = {
            "expression": expression,
            "decision": (
                "SURVIVED" if row.get("archive_history") is True
                and row.get("explicit_survivor") is True
                and elite_key in source_backed_elites
                else row.get("decision")),
            "observation_id": row.get("observation_id"),
            "lesson_codes": row.get("lesson_codes") or (),
            "diagnostics": row.get("diagnostics") or {},
            "economic_mechanism": mechanism,
            "observed_at": row.get("observed_at") or row.get("created_at") or "",
            "candidate_identity_fingerprint": identity,
            "semantic_plan_fingerprint": row.get(
                "semantic_plan_fingerprint") or "",
            "economic_family_id": row.get("economic_family_id") or "",
            "source_lead_ids": [str(lead["lead_id"])
                                for lead in verified_leads],
            "source_contract_fingerprints": verified_contracts,
            "root_lineage_id": row.get("root_lineage_id") or "",
            "exposure_fingerprint": row.get("exposure_fingerprint") or "",
        }
        try:
            outcomes.append(FormulaOutcome.from_result_row(
                adapted,
                search_score=(source_backed_elites.get(
                    elite_key, {}).get("quality_score")
                              if adapted["decision"] == "SURVIVED" else None),
                evidence_scope=(
                    "ADAPTIVE_SCREENING"
                    if row.get("archive_history") is True else str(
                        row.get("evidence_scope") or
                        "ADAPTIVE_SCREENING"))))
        except (TypeError, ValueError, grammar.IntradayExprError):
            continue

    engine = FormulaEvolutionEngine(EvolutionConfig(
        population_size=size, exploration_fraction=0.6,
        deterministic_seed=20260818, enable_crossover=False,
        feature_window_contract_version=window_contract))
    batch = engine.generate_population(
        seeds=seeds, outcomes=outcomes, population_size=size,
        generation=int(generation),
        # Every source seed is already persisted.  Keep it available as a
        # mutation parent without spending population slots re-emitting the
        # same AST or a parameter-only shape.
        known_exact_fingerprints={
            grammar.fingerprint(lead["expression"]) for lead in valid_leads},
        known_shape_fingerprints={
            subtree_shape_fingerprint(
                lead["expression"], contract_version=window_contract)
            for lead in valid_leads},
    )

    drafts = []
    for candidate in batch.candidates:
        provenance = _parent_provenance(
            candidate, lead_by_id=lead_by_id,
            canonical_lead_by_contract=canonical_lead_by_contract)
        parent_leads = provenance["parent_lead_ids"]
        primary_parent = next(
            (lead_by_id[lead_id] for lead_id in parent_leads
             if lead_id in lead_by_id), None)
        parent_contract = (primary_parent or {}).get("contract") or {}
        plan, thesis = _semantic_hint(
            candidate.expression, parent_contract, candidate.operation)
        fields = set(grammar.fields_of(candidate.expression))
        submission_blocker = _submission_blocker(
            candidate.operation, provenance)
        drafts.append({
            **candidate.to_dict(),
            **provenance,
            "submission_ready": submission_blocker == "",
            "submission_blocker": submission_blocker,
            "suggested_evolution_operators": _operator_contract(
                candidate.operation, fields),
            "semantic_plan_hint": plan,
            "formula_thesis_skeleton": thesis,
            "required_agent_enrichment": [
                "economic_mechanism", "expected_increment", "ablations",
                "novelty_rationale", "formula_thesis.identification",
            ],
        })
    submission_ready = [row for row in drafts if row["submission_ready"]]
    niches = sorted({row["niche"]["key"] for row in submission_ready})
    return {
        "ok": bool(submission_ready),
        "module_version": MODULE_VERSION,
        "engine_version": batch.engine_version,
        "feature_window_contract_version": window_contract,
        "evaluator_version": active_evaluator_version,
        "generation": int(generation),
        "source_leads": len(leads),
        "unique_source_formulas": len(canonical_lead_by_contract),
        "outcome_memory_rows": len(outcomes),
        "failure_memory_rows": sum(bool(row.lesson_codes)
                                   for row in outcomes),
        "requested_population": size,
        "emitted_population": len(drafts),
        "submission_ready_count": len(submission_ready),
        "submission_ready_niches": len(niches),
        "kpi": batch.to_dict()["kpi"],
        "audit": {
            **deepcopy(batch.audit),
            "search_memory": deepcopy(search_memory["audit"]),
            "source_backed_search_elites": len(
                source_backed_elites),
            "search_memory_rejected_rows": deepcopy(
                search_memory["rejected_rows"][:32]),
            "search_memory_state_fingerprint": hashlib.sha256(json.dumps(
                search_memory["state_snapshot"], sort_keys=True,
                separators=(",", ":")).encode()).hexdigest(),
            "forward_lockbox_used_for_generation": False,
            "coefficients_fitted_by_breeder": False,
            "cost_hurdle_modified_by_breeder": False,
            "agent_output_contract": "FORMULAS_NOT_REPORT",
            "feature_window_contract_version": window_contract,
            "target_evaluator_version": active_evaluator_version,
        },
        "candidates": drafts,
        "batch_fingerprint": hashlib.sha256(json.dumps(
            [row["candidate_id"] for row in drafts],
            separators=(",", ":")).encode()).hexdigest(),
    }


def generate(conn, *, population_size: int = 64,
             generation: int = 1) -> dict[str, Any]:
    leads, outcomes = load_records(conn)
    return generate_from_records(
        leads=leads, outcome_rows=outcomes,
        population_size=population_size, generation=generation,
        feature_window_contract_version=(
            grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT))


def _delivery_order(candidates: Iterable[Mapping[str, Any]]) -> list[dict]:
    """Rank valid single-parent drafts by deterministic niche coverage.

    The evolution engine intentionally emits a large population.  Sending all
    of it through MCP, however, can exceed an agent's tool-result budget before
    the first AST is visible.  This greedy order keeps the search population
    large while putting structurally different, submission-safe drafts first.
    """
    remaining = [deepcopy(dict(row)) for row in candidates
                 if row.get("submission_ready") is True
                 and len(row.get("parent_lead_ids") or ()) == 1]
    remaining.sort(key=lambda row: str(row.get("candidate_id") or ""))
    ordered: list[dict] = []
    seen_niches: set[str] = set()
    seen_dimensions: dict[str, set[str]] = {
        key: set() for key in (
            "pressure_source", "mechanism", "regime", "clock_bucket",
            "output_unit")
    }
    seen_parents: set[str] = set()
    seen_operations: set[str] = set()

    while remaining:
        def score(row: Mapping[str, Any]) -> int:
            niche = row.get("niche") or {}
            value = 32 * (str(niche.get("key") or "") not in seen_niches)
            value += sum(
                str(niche.get(key) or "") not in seen_dimensions[key]
                for key in seen_dimensions)
            parent = str((row.get("parent_lead_ids") or [""])[0])
            value += 2 * (parent not in seen_parents)
            value += str(row.get("operation") or "") not in seen_operations
            return int(value)

        best = min(
            remaining,
            key=lambda row: (-score(row), str(row.get("candidate_id") or "")))
        remaining.remove(best)
        ordered.append(best)
        niche = best.get("niche") or {}
        seen_niches.add(str(niche.get("key") or ""))
        for key in seen_dimensions:
            seen_dimensions[key].add(str(niche.get(key) or ""))
        seen_parents.add(str((best.get("parent_lead_ids") or [""])[0]))
        seen_operations.add(str(best.get("operation") or ""))
    return ordered


def _submission_template(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return the exact intake shape while keeping the generated AST immutable."""
    thesis = deepcopy(candidate.get("formula_thesis_skeleton") or {})
    thesis["identification"] = \
        "REQUIRES_HERMES_FALSIFIABLE_ECONOMIC_IDENTIFICATION"
    niche = candidate.get("niche") or {}
    mechanism = str(niche.get("mechanism") or "formula").replace("_", " ")
    candidate_id = str(candidate.get("candidate_id") or "")
    operators = deepcopy(candidate.get("suggested_evolution_operators") or [])
    return {
        "title": f"{mechanism.title()} child {candidate_id[:8]}",
        "candidate_signal_expr": deepcopy(candidate.get("expression")),
        "feature_window_contract_version": str(
            candidate.get("feature_window_contract_version") or ""),
        "semantic_plan": deepcopy(candidate.get("semantic_plan_hint") or {}),
        "formula_thesis": thesis,
        "evolution_operators": operators,
        "derivation_transforms": deepcopy(operators),
        "expected_increment":
            "REQUIRES_HERMES_EXPECTED_NET_INCREMENT_AFTER_FULL_COSTS",
        "ablations": [
            "REQUIRES_HERMES_STRUCTURAL_ABLATION_ONE",
            "REQUIRES_HERMES_STRUCTURAL_ABLATION_TWO",
        ],
        "economic_mechanism":
            "REQUIRES_HERMES_CONCRETE_ECONOMIC_MECHANISM",
        "novelty_rationale":
            "REQUIRES_HERMES_STRUCTURAL_NOVELTY_VERSUS_PARENT_AND_LIBRARY",
    }


def delivery_view(batch: Mapping[str, Any], *,
                  limit: int = MAX_DELIVERY_CANDIDATES) -> dict[str, Any]:
    """Bound a full population for reliable MCP delivery without shrinking it.

    Internal callers retain the complete ``candidates`` population.  The MCP
    surface receives only diverse, exact, ready-to-enrich submission templates,
    keeping the response comfortably below common tool-result truncation limits.
    """
    if not isinstance(limit, int) or isinstance(limit, bool):
        raise TypeError("delivery limit must be an integer")
    if not 1 <= limit <= MAX_DELIVERY_CANDIDATES:
        raise ValueError(
            f"delivery limit must be in [1, {MAX_DELIVERY_CANDIDATES}]")
    candidates = batch.get("candidates") or []
    if not isinstance(candidates, list):
        raise TypeError("batch candidates must be a list")
    ordered = _delivery_order(candidates)
    selected = ordered[:limit]
    summary_keys = (
        "ok", "module_version", "engine_version",
        "feature_window_contract_version", "evaluator_version", "generation",
        "source_leads", "unique_source_formulas", "outcome_memory_rows",
        "failure_memory_rows", "requested_population", "emitted_population",
        "submission_ready_count", "submission_ready_niches", "kpi",
        "batch_fingerprint",
    )
    result = {key: deepcopy(batch.get(key)) for key in summary_keys}
    full_audit = batch.get("audit") or {}
    result["audit"] = {key: deepcopy(full_audit.get(key)) for key in (
        "source_backed_search_elites", "search_memory_state_fingerprint",
        "forward_lockbox_used_for_generation", "coefficients_fitted_by_breeder",
        "cost_hurdle_modified_by_breeder", "agent_output_contract",
        "feature_window_contract_version", "target_evaluator_version",
    )}
    delivery_candidates = [{
        "candidate_id": row.get("candidate_id"),
        "parent_lead_id": (row.get("parent_lead_ids") or [None])[0],
        "parent_lead_ids": deepcopy(row.get("parent_lead_ids") or []),
        "niche": deepcopy(row.get("niche") or {}),
        "arm": row.get("arm"),
        "operation": row.get("operation"),
        "economic_mechanism_hint": row.get("economic_mechanism"),
        "adaptive_selection": row.get("adaptive_selection"),
        "promotion_authority": row.get("promotion_authority"),
        "requires_preregistered_evaluation": row.get(
            "requires_preregistered_evaluation"),
        "submission_template": _submission_template(row),
    } for row in selected]
    result.update({
        "candidate_payload_scope": "DIVERSE_SUBMISSION_READY_SLICE",
        "full_population_count": len(candidates),
        "delivery_candidate_count": len(selected),
        "delivery_candidate_limit": limit,
        "delivery_candidates": delivery_candidates,
        "delivery_fingerprint": hashlib.sha256(json.dumps(
            delivery_candidates, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest(),
        "agent_copy_rule": (
            "Copy each submission_template exactly. Replace only string values "
            "beginning REQUIRES_HERMES; never rewrite candidate_signal_expr, "
            "semantic_plan, feature-window contract, or evolution operators."),
    })
    return result


__all__ = [
    "MAX_DELIVERY_CANDIDATES", "MAX_POPULATION", "MIN_POPULATION",
    "MODULE_VERSION", "delivery_view", "generate", "generate_from_records",
    "load_records",
]
