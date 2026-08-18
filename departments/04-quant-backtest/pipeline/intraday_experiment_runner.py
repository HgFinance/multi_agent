"""Governed runtime adapter for the intraday alpha lane.

The scientific evaluator is pure (`intraday_candidate`).  This adapter only
selects a preregistered, bounded Timescale slice and writes immutable lineage and
numeric evidence to the shared quant experiment ledger.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, time, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from intraday_alpha_ast import (
    EXPLICIT_FEATURE_WINDOW_CONTRACT,
    LEGACY_FEATURE_WINDOW_CONTRACT,
    clocks_of,
    count_nodes,
    effective_clock_domains_of,
    field_window_bindings_of,
    fields_of,
    fingerprint,
    parse as parse_expr,
    primitive_windows_of,
    structural_similarity,
    temporal_windows_of,
    unit_of,
    validate_directional_quality_paths,
    validate_feature_window_contract,
)
from intraday_search_exposure_contract import (
    ADAPTIVE_SEARCH_EXPOSURE_VERSION,
    FINGERPRINT_CONTRACT as SEARCH_EXPOSURE_FINGERPRINT_CONTRACT,
    IDENTIFIER_EXCLUSIONS as SEARCH_EXPOSURE_IDENTIFIER_EXCLUSIONS,
    assert_strict_exposure,
    exposure_fingerprint as search_exposure_fingerprint,
)
from alpha_semantics import validate as validate_semantic_plan
from intraday_candidate import (CandidateAccumulator,
                                 CandidatePopulationAccumulator,
                                 COEFFICIENT_POLICIES,
                                 DEFAULT_CRITERIA,
                                 EVALUATOR_VERSION,
                                 EXPLICIT_WINDOW_EVALUATOR_VERSION)
from intraday_microstructure import (COMPLETED_SECOND_POLICY,
                                      EXTERNAL_EVENT_SOURCE,
                                      FeatureCubeSpec,
                                      LOCAL_EVENT_SOURCE, IntradayLaneSpec,
                                      STRICT_TIMESTAMP_POLICY,
                                      build_sample_batch, build_samples,
                                      effective_purge_gap,
                                      load_instrument_events_batch, manifest,
                                      source_quality_batch)
from microstructure_builder import (EXTERNAL_CONTENT_WINDOW_CONTRACT,
                                    external_content_fingerprints,
                                    external_session_content_window)
from intraday_ablation import INTRADAY_SCREENING_COHORT_VERSION
from intraday_sample_cache import (SampleCache, identity as cache_identity,
                                   prune as prune_sample_cache)
from intraday_multiple_testing import (MODULE_VERSION as MULTIPLE_TESTING_VERSION,
                                       paired_session_deltas,
                                       spa_reality_check)
from intraday_supervised import (EXPLICIT_WINDOW_TEACHER_VERSION,
                                 LABEL_VERSION as SUPERVISED_LABEL_VERSION,
                                 TEACHER_VERSION,
                                 explicit_feature_cube_spec_hash,
                                 explicit_feature_names,
                                 explicit_feature_spec_hash, feature_names,
                                 feature_spec_hash)
from intraday_trial_ledger import (ADAPTIVE_SEARCH, ARRIVAL_TIME_CAUSAL,
                                   CALIBRATION, DISCOVERY_6,
                                   EVENT_TIME_HISTORICAL_ONLY, FORWARD,
                                   FORWARD_CONFIRMATION, FULL_60,
                                   MODULE_VERSION as TRIAL_LEDGER_VERSION,
                                   VALIDATION_20, allocate_experiment_rung,
                                   find_latest_candidate_lineage,
                                   load_candidate_lineage,
                                   load_experiment_rung,
                                   record_forward_confirmation,
                                   record_session_access,
                                   record_session_exposure,
                                   register_candidate_lineage,
                                   stable_fingerprint)
from overfit_stats import deflated_sharpe, stationary_bootstrap_indices
from stock_universe import (INTRADAY_REPORT_MANIFEST_VERSION,
                            STOCK_ASSET_SCOPE, STOCK_UNIVERSE_VERSION,
                            assert_stock_instrument_ids,
                            governed_stock_evidence_sql)


RUNNER_VERSION = "intraday-experiment-runner-v17"
FORWARD_RUNNER_VERSION = "intraday-forward-confirmation-v6"
FORWARD_GATE_VERSION = "intraday-forward-fixed-horizon-gate-v1"
FORWARD_RAW_DIGEST_VERSION = "ACTUAL_RAW_REPLAY_V1"
EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT = \
    "pg-composite-row-xor0-sum1-sha256-v1"
LOCAL_SOURCE_CONTENT_HASH_CONTRACT = "pg-composite-row-xor0-sum1-v1"
FAST_SCREEN_VERSION = "intraday-fast-discovery-screen-v3"
SEARCH_OBJECTIVES_VERSION = "intraday-search-objectives-v1"
EXTERNAL_RAW_REPLAY_CONTENT_VERSION = "external-raw-replay-content-v3"
EXTERNAL_REPLAY_MAX_HORIZON_SECONDS = 600
COST_MODEL_VERSION = "krx-intraday-execution-v3"
REPORT_MANIFEST_VERSION = INTRADAY_REPORT_MANIFEST_VERSION
FORWARD_REPORT_REVISION_VERSION = "intraday-forward-report-revision-v1"
QA_REPRODUCTION_RUNTIME_VERSION = \
    "intraday-forward-reproduction-runtime-v1"
QA_REPRODUCTION_SOURCE_VERSION = \
    "intraday-forward-reproduction-source-set-v1"
FORWARD_WORK_LEASE_MINUTES = 180
FORWARD_WAIT_RETRY_HOURS = 6
FORWARD_ERROR_BACKOFF_MINUTES = 15
FORWARD_ERROR_BACKOFF_MAX_HOURS = 24
FORWARD_MAX_ERROR_COUNT = 8
# experiment_metrics has a B-tree UNIQUE key containing dimensions::jsonb.
# PostgreSQL rejects an oversized index tuple near 2.7KiB, so keep a margin
# for JSONB/index representation overhead and store complete artifacts only in
# quant.intraday_report_manifests.
MAX_INDEXED_DIMENSIONS_JSON_BYTES = 1_800
# 2026 listed equities incur 20bp on the sale (KOSPI 5bp STT + 15bp rural,
# KOSDAQ 20bp STT).  A representative online commission is 1.5bp each side.
# Expressing the sale-only tax as 10bp/side keeps long/short evaluation symmetric:
# 10 + 1.5 = 11.5bp per side, 23bp for a round trip.
DEFAULT_FEE_BPS_PER_SIDE = 11.5
MIN_EQUITY_FEE_BPS_PER_SIDE = 10.0
MIN_DSR_DISPERSION_FORMULAS = 4
DSR_TRIAL_SHARPE_STD_FLOOR = 1.0
DATASET = ("krx-intraday-events", "v1")
EXTERNAL_DATASET = ("krx-intraday-completed-second", "v1")
DATASET_BY_EVENT_SOURCE = {
    LOCAL_EVENT_SOURCE: DATASET,
    EXTERNAL_EVENT_SOURCE: EXTERNAL_DATASET,
}
KST = ZoneInfo("Asia/Seoul")

INTRADAY_LANE = "INTRADAY_EVENT"
TRIAL_RESERVATION_KEY = "_trial_family_reservation_v1"

# The proposal bridge may expose only these knobs to the research agent.  Keep
# this set here, next to the decoder that actually consumes the values, so a
# bridge/runner drift cannot silently discard a proposed experiment parameter.
INTRADAY_PROPOSAL_PARAMETER_KEYS = frozenset({
    "intraday_signal_expr", "source_baseline_expr",
    "parent_ast_fingerprint", "parent_candidate_identity_fingerprint",
    "horizon_seconds", "sample_interval_seconds",
    "feature_lookback_seconds", "order_latency_ms", "max_quote_age_seconds",
    "fee_bps_per_side", "maker_fee_bps_per_side", "execution", "threshold",
    "entry_policy", "coefficient_policy", "minimum_predicted_edge_bps",
    "evaluation_days", "instrument_shard_size", "position_mode",
    "fast_screen_enabled", "fast_screen_sessions", "fast_screen_instruments",
    "fast_screen_min_opportunities", "fast_screen_min_net_bps",
    "screening_population", "screening_cohort_version",
    "feature_window_contract_version",
    "migration_parent_ast_fingerprint",
    "migration_parent_feature_window_contract_version",
})

# These additional fields are controlled execution contracts rather than LLM
# proposal knobs.  They are still legitimate inputs to the current explicit-V2
# decoder and therefore belong to the production admission surface.
CURRENT_EXPLICIT_V2_EXECUTION_KEYS = frozenset({
    "type", "research_lane", "universe_key", "semantic_plan",
    *INTRADAY_PROPOSAL_PARAMETER_KEYS,
    "feature_cube_spec",
    "intermediate_screen_enabled", "intermediate_screen_sessions",
    "intermediate_screen_instruments", "intermediate_candidate_budget",
    "successive_halving_eta", "forward_confirmation_min_new_sessions",
})

# Only trusted pipeline components may stamp these fields.  They are accepted
# by admission but are not candidate formula knobs and must never be copied
# into the evaluator config unless config_from_edge explicitly binds them.
INTRADAY_SYSTEM_METADATA_KEYS = frozenset({
    "semantic_fingerprint", "data_source", "resolved_data_contract",
    "primary_attempts_before", "historical_exact_screening_exposures",
    TRIAL_RESERVATION_KEY,
})

CURRENT_EXPLICIT_V2_ALLOWED_EDGE_KEYS = frozenset(
    CURRENT_EXPLICIT_V2_EXECUTION_KEYS | INTRADAY_SYSTEM_METADATA_KEYS)

# Every sidecar is executable during adaptive screening.  Unknown nested keys
# are therefore just as dangerous as unknown primary keys: config_from_edge
# preserves sidecar provenance with ``**raw`` and would otherwise make a typo
# look as though it had affected the replay.
CURRENT_EXPLICIT_V2_SCREENING_CANDIDATE_KEYS = frozenset({
    "candidate_role", "source_lead_ids", "title", "ast_fingerprint",
    "intraday_signal_expr", "semantic_plan", "entry_policy",
    "coefficient_policy", "source_baseline_expr",
    "feature_window_contract_version", "evolution_role",
    "evolution_operators", "parent_ast_fingerprint",
    "parent_candidate_identity_fingerprint",
    "parent_feature_window_contract_version", "parent_of_ast_fingerprint",
    "screening_cohort_version", "ablation_operator", "ablation_path",
    "ablation_of_ast_fingerprint", "ablation_version",
})

# Historical V1/V11 configs remain decodable below for audit replay and as
# migration parents. Production entry points use this separate preflight so a
# missing contract can never silently select the legacy evaluator for a new
# experiment.
SUPERSEDED_INTRADAY_FEATURE_WINDOW_CONTRACT = \
    "SUPERSEDED_INTRADAY_FEATURE_WINDOW_CONTRACT"


def current_intraday_execution_contract_rejection(edge: dict) -> str:
    """Return a typed reason when a production intraday edge is not V2.

    This deliberately does not call :func:`config_from_edge`: the decoder must
    keep accepting frozen legacy evidence. Gate 0, the worker, and the direct
    orchestrator CLI call this preflight before data access or trial
    reservation. Every populated screening row is executable in the adaptive
    cohort, so sidecars may not inherit the primary's contract implicitly.
    """
    if not isinstance(edge, dict):
        return ""
    if str(edge.get("research_lane") or "").strip().upper() != INTRADAY_LANE:
        return ""

    primary_contract = str(
        edge.get("feature_window_contract_version") or "").strip()
    if primary_contract != EXPLICIT_FEATURE_WINDOW_CONTRACT:
        return (
            f"{SUPERSEDED_INTRADAY_FEATURE_WINDOW_CONTRACT}: primary requires "
            f"feature_window_contract_version="
            f"{EXPLICIT_FEATURE_WINDOW_CONTRACT!r}; got "
            f"{primary_contract or '(missing)'!r}")

    screening = edge.get("screening_population")
    if screening in (None, []):
        return ""
    if not isinstance(screening, list):
        return (
            f"{SUPERSEDED_INTRADAY_FEATURE_WINDOW_CONTRACT}: "
            "screening_population must be a list")
    for index, candidate in enumerate(screening):
        if not isinstance(candidate, dict):
            return (
                f"{SUPERSEDED_INTRADAY_FEATURE_WINDOW_CONTRACT}: "
                f"screening_population[{index}] must be an object")
        candidate_contract = str(
            candidate.get("feature_window_contract_version") or "").strip()
        if candidate_contract != EXPLICIT_FEATURE_WINDOW_CONTRACT:
            return (
                f"{SUPERSEDED_INTRADAY_FEATURE_WINDOW_CONTRACT}: "
                f"screening_population[{index}] requires "
                f"feature_window_contract_version="
                f"{EXPLICIT_FEATURE_WINDOW_CONTRACT!r}; got "
                f"{candidate_contract or '(missing)'!r}")
    return ""


def current_explicit_v2_unknown_edge_keys(edge: dict) -> list[str]:
    """Return unbound primary and sidecar keys for production V2 admission."""
    if not isinstance(edge, dict):
        return ["expected_edge"]
    unknown = [
        str(key) for key in edge
        if key not in CURRENT_EXPLICIT_V2_ALLOWED_EDGE_KEYS
    ]
    screening = edge.get("screening_population")
    if isinstance(screening, list):
        for index, candidate in enumerate(screening):
            if not isinstance(candidate, dict):
                continue
            unknown.extend(
                f"screening_population[{index}].{key}"
                for key in candidate
                if key not in CURRENT_EXPLICIT_V2_SCREENING_CANDIDATE_KEYS)
    return sorted(unknown)


def validate_current_explicit_v2_execution_edge(
        edge: dict) -> tuple[dict, IntradayLaneSpec]:
    """Fail closed, then decode one current production intraday edge.

    Historical configs continue to call :func:`config_from_edge` directly so
    frozen V1 evidence remains reproducible.  New production admissions use
    this stricter wrapper and cannot silently ignore a misspelled field.
    """
    if not isinstance(edge, dict):
        raise ValueError("intraday expected_edge must be an object")
    unknown = current_explicit_v2_unknown_edge_keys(edge)
    if unknown:
        raise ValueError(
            "current explicit-v2 intraday edge contains unsupported keys: "
            f"{unknown}; allowed={sorted(CURRENT_EXPLICIT_V2_ALLOWED_EDGE_KEYS)}")
    contract_rejection = current_intraday_execution_contract_rejection(edge)
    if contract_rejection:
        raise ValueError(contract_rejection)
    validate_directional_quality_paths(edge.get("intraday_signal_expr"))
    for index, candidate in enumerate(edge.get("screening_population") or []):
        try:
            validate_directional_quality_paths(
                candidate.get("intraday_signal_expr"))
        except ValueError as exc:
            raise ValueError(
                f"screening_population[{index}] violates the directional "
                f"quality-path contract: {exc}") from exc
    return config_from_edge(edge)


def _feature_window_contract(config: dict) -> str:
    raw = config.get("feature_window_contract_version")
    version = (LEGACY_FEATURE_WINDOW_CONTRACT
               if raw is None else str(raw))
    if version not in {
            LEGACY_FEATURE_WINDOW_CONTRACT,
            EXPLICIT_FEATURE_WINDOW_CONTRACT}:
        raise ValueError(
            f"unsupported feature-window contract {version!r}")
    return version


def _evaluator_version(config: dict) -> str:
    return (EXPLICIT_WINDOW_EVALUATOR_VERSION
            if _feature_window_contract(config) ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT else EVALUATOR_VERSION)


def _feature_cube_spec(config: dict) -> FeatureCubeSpec | None:
    """Restore the exact frozen cube contract used by an explicit replay."""
    explicit = (_feature_window_contract(config) ==
                EXPLICIT_FEATURE_WINDOW_CONTRACT)
    raw = config.get("feature_cube_spec")
    if not explicit:
        if raw is not None:
            raise ValueError(
                "feature_cube_spec requires the explicit-window contract")
        return None
    if raw is None:
        raise ValueError(
            "explicit-window replay requires a frozen feature_cube_spec")
    try:
        return FeatureCubeSpec.from_dict(raw)
    except ValueError as exc:
        raise ValueError("frozen feature_cube_spec is invalid") from exc


def _teacher_runtime_identity(config: dict) -> dict:
    """Return model identity for the config's exact feature contract.

    The legacy object is intentionally byte-for-byte identical to the v3
    identity.  Explicit-window candidates bind the v4 teacher to the canonical
    cube spec that is also used for replay and cache identity.
    """
    if (_feature_window_contract(config) !=
            EXPLICIT_FEATURE_WINDOW_CONTRACT):
        return {
            "version": TEACHER_VERSION,
            "feature_spec_hash": feature_spec_hash(),
            "features": feature_names(),
            "feature_cube_spec": None,
            "feature_cube_spec_hash": None,
        }
    cube_spec = _feature_cube_spec(config)
    cube_payload = {
        "version": cube_spec.version,
        "feature_window_contract_version":
            cube_spec.feature_window_contract_version,
        "windows_seconds": cube_spec.windows_seconds,
        "windowed_fields": cube_spec.windowed_fields,
        "boundary": cube_spec.boundary,
    }
    encoded = json.dumps(
        cube_payload, sort_keys=True, separators=(",", ":"))
    cube_hash = hashlib.sha256(encoded.encode()).hexdigest()
    # The v4 teacher is frozen against this exact public cube.  A new cube is a
    # new teacher contract, never an in-place config tweak.
    if cube_hash != explicit_feature_cube_spec_hash():
        raise ValueError("feature cube and teacher v4 contracts differ")
    return {
        "version": EXPLICIT_WINDOW_TEACHER_VERSION,
        "feature_spec_hash": explicit_feature_spec_hash(),
        "features": explicit_feature_names(),
        "feature_cube_spec": cube_spec.as_dict(),
        "feature_cube_spec_hash": cube_hash,
    }

# Keep every forward boundary on the same fail-closed evidence definition used
# by allocation, PBO, and terminal factory promotion.  Forward confirmation is
# a new sample, not permission to rehabilitate an incomplete or legacy FULL_60
# run that would be ineligible everywhere else in the factory.
_GOVERNED_FORWARD_STOCK_EVIDENCE = governed_stock_evidence_sql(
    experiment_alias="e", dataset_alias="dataset",
    hypothesis_alias="hypothesis")

_SQL_ASSERT_GOVERNED_FORWARD_STOCK_EVIDENCE = f"""
select exists (
  select 1
    from quant.experiments e
    join quant.dataset_manifests dataset
      on dataset.dataset_id = e.dataset_id
    join quant.hypotheses hypothesis
      on hypothesis.hypothesis_id = e.hypothesis_id
   where e.experiment_id = %s::uuid
     and {_GOVERNED_FORWARD_STOCK_EVIDENCE}
)
"""

_QA_REPRODUCTION_SOURCE_PATHS = (
    "departments/01-research/contracts/intraday_ast_contract.py",
    "departments/01-research/contracts/alpha_semantics.py",
    "departments/01-research/contracts/intraday_ablation.py",
    "departments/04-quant-backtest/pipeline/intraday_experiment_runner.py",
    "departments/04-quant-backtest/pipeline/intraday_candidate.py",
    "departments/04-quant-backtest/pipeline/intraday_microstructure.py",
    "departments/04-quant-backtest/pipeline/intraday_supervised.py",
    "departments/04-quant-backtest/pipeline/intraday_multiple_testing.py",
    "departments/04-quant-backtest/pipeline/microstructure_builder.py",
    "departments/04-quant-backtest/pipeline/overfit_stats.py",
    "departments/04-quant-backtest/pipeline/intraday_alpha_ast.py",
    "departments/04-quant-backtest/pipeline/intraday_sample_cache.py",
    "departments/04-quant-backtest/pipeline/intraday_trial_ledger.py",
    "departments/04-quant-backtest/pipeline/stock_universe.py",
)


def _qa_reproduction_source_manifest() -> dict:
    """Fingerprint every source file that can change forward replay output."""

    root = Path(__file__).resolve().parents[3]
    files = {}
    for relative in _QA_REPRODUCTION_SOURCE_PATHS:
        path = root / relative
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RuntimeError(
                f"QA reproduction source is unavailable: {relative}") from exc
        normalized = source.replace("\r\n", "\n").replace("\r", "\n")
        files[relative] = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    identity = {
        "version": QA_REPRODUCTION_SOURCE_VERSION,
        "files": files,
    }
    return {**identity, "source_fingerprint": stable_fingerprint(identity)}


def _qa_current_runtime_versions(config: dict | None = None) -> dict[str, str]:
    teacher = _teacher_runtime_identity(config or {})
    return {
        "code_version": RUNNER_VERSION,
        "forward_runner_version": FORWARD_RUNNER_VERSION,
        "forward_gate_version": FORWARD_GATE_VERSION,
        "raw_digest_version": FORWARD_RAW_DIGEST_VERSION,
        "evaluator_version": _evaluator_version(config or {}),
        "cost_model_version": COST_MODEL_VERSION,
        "teacher_version": teacher["version"],
        "supervised_label_version": SUPERVISED_LABEL_VERSION,
        "multiple_testing_version": MULTIPLE_TESTING_VERSION,
        "stock_universe_version": STOCK_UNIVERSE_VERSION,
        "report_manifest_version": REPORT_MANIFEST_VERSION,
    }


def _forward_runtime_artifact_attestation(governance_report: dict) -> dict:
    """Prove that the current process can execute the frozen QA runtime.

    The present deployment has no durable scheduler that can launch an older
    OCI digest.  Exact current-source equality is therefore the only executable
    route.  A deployment-spanning mismatch is a scientific ``INCONCLUSIVE``
    condition, never an infrastructure retry and never a QA-positive outcome.
    """

    runtime = (governance_report or {}).get("reproduction_runtime") or {}
    mismatches: list[str] = []
    if not isinstance(runtime, dict):
        mismatches.append("runtime_manifest")
        runtime = {}
    elif runtime.get("version") != QA_REPRODUCTION_RUNTIME_VERSION:
        mismatches.append("runtime_manifest")
    source_manifest = runtime.get("source_manifest") or {}
    if not isinstance(source_manifest, dict):
        mismatches.append("source_manifest")
        source_manifest = {}
    elif source_manifest.get("version") != QA_REPRODUCTION_SOURCE_VERSION:
        mismatches.append("source_manifest")

    frozen_config = (runtime.get("frozen_config") or {}
                     if isinstance(runtime, dict) else {})
    if (not isinstance(frozen_config, dict) or not frozen_config
            or stable_fingerprint(frozen_config) !=
            runtime.get("frozen_config_fingerprint")):
        mismatches.append("frozen_config_fingerprint")
    if isinstance(runtime, dict):
        runtime_identity = {
            key: value for key, value in runtime.items()
            if key != "runtime_manifest_fingerprint"
        }
        if stable_fingerprint(runtime_identity) != runtime.get(
                "runtime_manifest_fingerprint"):
            mismatches.append("runtime_manifest_fingerprint")
    if isinstance(source_manifest, dict):
        source_identity = {
            key: value for key, value in source_manifest.items()
            if key != "source_fingerprint"
        }
        if stable_fingerprint(source_identity) != source_manifest.get(
                "source_fingerprint"):
            mismatches.append("source_manifest_fingerprint")

    for key, current in _qa_current_runtime_versions(frozen_config).items():
        if current != runtime.get(key):
            mismatches.append(f"runtime_{key}")
    try:
        current_source = _qa_reproduction_source_manifest()
    except RuntimeError:
        current_source = {}
        mismatches.append("current_source_unavailable")
    if current_source != source_manifest:
        mismatches.append("runtime_source_set")

    mismatches = sorted(set(mismatches))
    return {
        "version": "intraday-forward-runtime-artifact-attestation-v1",
        "status": ("CURRENT_RUNTIME_EXACT" if not mismatches else
                   "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"),
        "reproduction_route_available": not mismatches,
        "route": "CURRENT_IMAGE_SOURCE_SET" if not mismatches else "NONE",
        "durability": "DEPLOYMENT_LOCAL_ONLY",
        "immutable_oci_route_available": False,
        "frozen_source_fingerprint": str(
            source_manifest.get("source_fingerprint") or ""),
        "current_source_fingerprint": str(
            current_source.get("source_fingerprint") or ""),
        "mismatches": mismatches,
        "promotion_authority": False,
    }


class StaleIntradayCohortError(ValueError):
    """A persisted screening cohort predates the executable formula contract.

    This is a non-retryable input-version rejection, not evidence that the
    economic hypothesis failed. Keeping it distinct from ordinary ValueError
    lets the queue retire the immutable old hypothesis without spending more
    full-universe replay attempts.
    """

_SESSION_DATES_SQL = """
select distinct (event_time at time zone 'Asia/Seoul')::date as session_date
  from market.market_quotes
 where received_at is not null
   and event_time >= %s
   and greatest(received_at, observed_at) <= %s
   and event_time < (
         date_trunc('day', %s at time zone 'Asia/Seoul')
         at time zone 'Asia/Seoul'
       )
 order by session_date desc
 limit %s
"""

_LIQUID_UNIVERSE_SQL = """
with causal_quotes as (
  select instrument_id::text as instrument_id, count(*) as quote_events
    from market.market_quotes
   where event_time >= %s and event_time < %s
     and received_at is not null
     and greatest(received_at, observed_at) <= %s
     and bid_prices[1] > 0 and ask_prices[1] > 0
     and ask_prices[1] >= bid_prices[1]
   group by instrument_id
), causal_trades as (
  select distinct instrument_id::text as instrument_id
    from market.market_ticks
   where event_time >= %s and event_time < %s
     and received_at is not null
     and greatest(received_at, observed_at) <= %s
)
select q.instrument_id, q.quote_events
  from causal_quotes q
  join causal_trades t using (instrument_id)
 order by q.instrument_id
"""

_LINEAGE_SQL = """
with quote_rows as (
  select event_time, observed_at,
         greatest(received_at, observed_at) as available_at,
         hashtextextended(jsonb_build_array(
           instrument_id::text, event_time, received_at, observed_at,
           source_event_id, bid_prices, bid_sizes, ask_prices, ask_sizes
         )::text, 0) as h0,
         hashtextextended(jsonb_build_array(
           instrument_id::text, event_time, received_at, observed_at,
           source_event_id, bid_prices, bid_sizes, ask_prices, ask_sizes
         )::text, 1) as h1,
         hashtextextended(jsonb_build_array(
           instrument_id::text, event_time, received_at, observed_at,
           source_event_id, bid_prices, bid_sizes, ask_prices, ask_sizes
         )::text, 2)::numeric as h2
    from market.market_quotes
   where instrument_id = any(%s::uuid[])
     and event_time >= %s and event_time < %s
     and received_at is not null
     and greatest(received_at, observed_at) <= %s
), trade_rows as (
  select event_time, observed_at,
         greatest(received_at, observed_at) as available_at,
         hashtextextended(jsonb_build_array(
           instrument_id::text, event_time, received_at, observed_at,
           source_event_id, price, quantity, side
         )::text, 0) as h0,
         hashtextextended(jsonb_build_array(
           instrument_id::text, event_time, received_at, observed_at,
           source_event_id, price, quantity, side
         )::text, 1) as h1,
         hashtextextended(jsonb_build_array(
           instrument_id::text, event_time, received_at, observed_at,
           source_event_id, price, quantity, side
         )::text, 2)::numeric as h2
    from market.market_ticks
   where instrument_id = any(%s::uuid[])
     and event_time >= %s and event_time < %s
     and received_at is not null
     and greatest(received_at, observed_at) <= %s
)
select 'market_quotes' as source, count(*)::bigint,
       min(event_time), max(event_time), max(observed_at), max(available_at),
       coalesce(bit_xor(h0)::text, 'EMPTY'),
       coalesce(bit_xor(h1)::text, 'EMPTY'),
       coalesce(sum(h2)::text, 'EMPTY')
  from quote_rows
union all
select 'market_ticks' as source, count(*)::bigint,
       min(event_time), max(event_time), max(observed_at), max(available_at),
       coalesce(bit_xor(h0)::text, 'EMPTY'),
       coalesce(bit_xor(h1)::text, 'EMPTY'),
       coalesce(sum(h2)::text, 'EMPTY')
  from trade_rows
order by source
"""

# The daily feature ledger is tiny relative to the 92GB raw FDW source.  It is
# used only to discover immutable sessions and the causally collected universe;
# candidate features and labels are still rebuilt from ext_src L10/tape rows.
_EXTERNAL_SESSION_DATES_SQL = """
select distinct (event_time at time zone 'Asia/Seoul')::date as session_date
  from market.microstructure_features
 where feature_set_version = 'ms-daily-v5'
   and values->>'origin' = 'external'
   and event_time >= %s
   and event_time < (
         date_trunc('day', %s at time zone 'Asia/Seoul')
         at time zone 'Asia/Seoul'
       )
   and coalesce((values->>'n_quotes')::bigint, 0) > 0
   and coalesce((values->>'n_ticks')::bigint, 0) > 0
 order by session_date desc
 limit %s
"""

_EXTERNAL_UNIVERSE_SQL = """
select sm.symbol,
       sum(coalesce((mf.values->>'n_quotes')::bigint, 0)) as quote_events,
       avg(mf.spread_bps) filter (where mf.spread_bps is not null) as spread_bps,
       avg(mf.trade_intensity) filter (
         where mf.trade_intensity is not null) as trade_intensity,
       avg(mf.book_depth_notional_l1) filter (
         where mf.book_depth_notional_l1 is not null) as depth_notional_l1
  from market.microstructure_features mf
  join market.symbol_map sm using (instrument_id)
 where mf.feature_set_version = 'ms-daily-v5'
   and mf.values->>'origin' = 'external'
   and (mf.event_time at time zone 'Asia/Seoul')::date = any(%s)
 group by sm.symbol
having sum(coalesce((mf.values->>'n_quotes')::bigint, 0)) > 0
   and sum(coalesce((mf.values->>'n_ticks')::bigint, 0)) > 0
 order by sm.symbol
"""

_EXTERNAL_LINEAGE_SQL = """
select 'ext_src.quotes' as source,
       sum(coalesce((mf.values->>'n_quotes')::bigint, 0))::bigint,
       min(mf.event_time), max(mf.event_time), max(mf.input_watermark),
       null::timestamptz
  from market.microstructure_features mf
  join market.symbol_map sm using (instrument_id)
 where mf.feature_set_version = 'ms-daily-v5'
   and mf.values->>'origin' = 'external'
   and sm.symbol = any(%s)
   and (mf.event_time at time zone 'Asia/Seoul')::date = any(%s)
union all
select 'ext_src.ticks' as source,
       sum(coalesce((mf.values->>'n_ticks')::bigint, 0))::bigint,
       min(mf.event_time), max(mf.event_time), max(mf.input_watermark),
       null::timestamptz
  from market.microstructure_features mf
  join market.symbol_map sm using (instrument_id)
 where mf.feature_set_version = 'ms-daily-v5'
   and mf.values->>'origin' = 'external'
   and sm.symbol = any(%s)
   and (mf.event_time at time zone 'Asia/Seoul')::date = any(%s)
order by source
"""

_EXTERNAL_SESSION_EVIDENCE_SQL = """
select sm.symbol,
       coalesce((mf.values->>'n_quotes')::bigint, 0) as n_quotes,
       coalesce((mf.values->>'n_ticks')::bigint, 0) as n_ticks,
       mf.values->>'source_content_fingerprint' as source_content_fingerprint,
       mf.values->>'source_content_hash_contract' as source_content_hash_contract,
       mf.input_hash,
       mf.input_watermark
  from market.microstructure_features mf
  join market.symbol_map sm using (instrument_id)
 where mf.feature_set_version = 'ms-daily-v5'
   and mf.values->>'origin' = 'external'
   and (mf.event_time at time zone 'Asia/Seoul')::date = %s
   and sm.symbol = any(%s)
order by sm.symbol
"""

_EXTERNAL_SLICE_EVIDENCE_SQL = """
select (mf.event_time at time zone 'Asia/Seoul')::date as session_date,
       sm.symbol,
       coalesce((mf.values->>'n_quotes')::bigint, 0) as n_quotes,
       coalesce((mf.values->>'n_ticks')::bigint, 0) as n_ticks,
       mf.values->>'source_content_fingerprint' as source_content_fingerprint,
       mf.values->>'source_content_hash_contract' as source_content_hash_contract,
       mf.input_hash,
       mf.input_watermark
  from market.microstructure_features mf
  join market.symbol_map sm using (instrument_id)
 where mf.feature_set_version = 'ms-daily-v5'
   and mf.values->>'origin' = 'external'
   and sm.symbol = any(%s)
   and (mf.event_time at time zone 'Asia/Seoul')::date = any(%s)
 order by session_date, sm.symbol
"""

_LOCAL_SESSION_EVIDENCE_SQL = """
with quote_rows as (
    select greatest(received_at, observed_at) as available_at,
           hash_record_extended(quotes, 0) as h0,
           hash_record_extended(quotes, 1)::numeric as h1
      from market.market_quotes quotes
     where instrument_id = any(%s::uuid[])
       and event_time >= %s and event_time < %s
       and received_at is not null
       and greatest(received_at, observed_at) <= %s
), trade_rows as (
    select greatest(received_at, observed_at) as available_at,
           hash_record_extended(ticks, 0) as h0,
           hash_record_extended(ticks, 1)::numeric as h1
      from market.market_ticks ticks
     where instrument_id = any(%s::uuid[])
       and event_time >= %s and event_time < %s
       and received_at is not null
       and greatest(received_at, observed_at) <= %s
)
select 'market.market_quotes'::text as source, count(*)::bigint,
       max(available_at), coalesce(bit_xor(h0)::text, 'EMPTY'),
       coalesce(sum(h1)::text, 'EMPTY')
  from quote_rows
union all
select 'market.market_ticks'::text as source, count(*)::bigint,
       max(available_at), coalesce(bit_xor(h0)::text, 'EMPTY'),
       coalesce(sum(h1)::text, 'EMPTY')
  from trade_rows
order by source
"""

# Choose forward dates from the versioned KRX calendar, never from quote/trade
# presence.  Otherwise an exchange-wide collection outage would disappear from
# the test cohort.  The version itself is selected as known at the immutable
# dataset cutoff, so a later calendar revision cannot change a retry.
_FORWARD_CALENDAR_SESSIONS_SQL = """
with chosen_calendar as (
  select calendar_version_id, version, content_hash,
         coalesce(published_at, created_at) as known_at
    from reference.market_calendar_versions
   where market = 'KRX'
     and coalesce(published_at, created_at) <= %s
     and effective_from <= %s
     and (effective_to is null or effective_to >= %s)
   order by version desc
   limit 1
)
select session.trade_date, session.opens_at, session.closes_at,
       calendar.calendar_version_id::text, calendar.version,
       calendar.content_hash, calendar.known_at
  from chosen_calendar calendar
  join reference.market_sessions session
    on session.calendar_version_id = calendar.calendar_version_id
 where session.market = 'KRX'
   and session.session_type = 'REGULAR'
   and session.is_trading_day
   and session.trade_date >= %s
   and session.trade_date < %s
   and session.closes_at is not null
   and session.closes_at <= %s
 order by session.trade_date
 limit %s
"""

_FORWARD_CANDIDATES_SQL = f"""
select e.experiment_id::text, e.dataset_id::text, e.config,
       m.report, greatest(m.created_at, full_rung.allocated_at) as frozen_at,
       full_rung.candidate_lineage_id::text,
       coalesce(forward_rung.lockbox_cutoff_session_date,
                root_access.latest_access_date) as search_cutoff,
       gate.dimensions as final_gate,
       coalesce(m.report->'score_calibration',
                calibration.dimensions) as score_calibration,
       forward_rung.experiment_rung_id::text,
       forward_rung.dataset_cutoff
  from quant.experiments e
  join quant.dataset_manifests dataset
    on dataset.dataset_id = e.dataset_id
  join quant.hypotheses hypothesis
    on hypothesis.hypothesis_id = e.hypothesis_id
  join quant.intraday_report_manifests m using (experiment_id)
  join quant.intraday_experiment_rungs full_rung
    on full_rung.experiment_id = e.experiment_id
   and full_rung.rung = 'FULL_60'
  left join quant.intraday_experiment_rungs forward_rung
    on forward_rung.experiment_id = e.experiment_id
   and forward_rung.candidate_lineage_id = full_rung.candidate_lineage_id
   and forward_rung.rung = 'FORWARD'
  left join quant.intraday_forward_confirmations confirmation
    on confirmation.experiment_rung_id = forward_rung.experiment_rung_id
  left join lateral (
    select max(access.session_date) as latest_access_date,
           max(access.accessed_at) as latest_accessed_at
      from quant.intraday_session_accesses access
     where access.root_lineage_id = full_rung.root_lineage_id
  ) root_access on true
  left join lateral (
    select dimensions
      from quant.experiment_metrics metric
     where metric.experiment_id = e.experiment_id
       and metric.split = 'WALK_FORWARD'
       and metric.metric = 'intraday_gate_pass'
     order by metric.experiment_metric_id desc
     limit 1
  ) gate on true
  left join lateral (
    select dimensions
      from quant.experiment_metrics metric
     where metric.experiment_id = e.experiment_id
       and metric.split = 'WALK_FORWARD'
       and metric.metric = 'intraday_score_calibration'
       and not (metric.dimensions ? 'screening_candidate')
     limit 1
  ) calibration on true
 where e.status = 'COMPLETED'
   and m.report->>'evidence_tier' = 'SEARCH_EXPOSED_HISTORICAL_SUPPORT'
   and gate.dimensions->>'decision' = 'HOLD'
   and gate.dimensions->'failed_criteria' =
       '["INDEPENDENT_FORWARD_CONFIRMATION_PENDING"]'::jsonb
   and root_access.latest_access_date is not null
   and confirmation.forward_confirmation_id is null
   and {_GOVERNED_FORWARD_STOCK_EVIDENCE}
 order by m.created_at, e.experiment_id
 limit %s
"""

_FORWARD_CANDIDATES_BY_ID_SQL = _FORWARD_CANDIDATES_SQL.replace(
    " order by m.created_at, e.experiment_id\n limit %s",
    " and e.experiment_id = any(%s::uuid[])\n"
    " order by m.created_at, e.experiment_id",
)

_FORWARD_ENQUEUE_SQL = f"""
insert into quant.intraday_forward_work_items
  (experiment_id, candidate_lineage_id, status, next_attempt_at)
select e.experiment_id, full_rung.candidate_lineage_id, 'READY', now()
  from quant.experiments e
  join quant.hypotheses hypothesis using (hypothesis_id)
  join quant.dataset_manifests dataset
    on dataset.dataset_id = e.dataset_id
  join quant.intraday_report_manifests manifest using (experiment_id)
  join quant.intraday_experiment_rungs full_rung
    on full_rung.experiment_id = e.experiment_id
   and full_rung.rung = 'FULL_60'
  left join quant.intraday_experiment_rungs forward_rung
    on forward_rung.experiment_id = e.experiment_id
   and forward_rung.candidate_lineage_id = full_rung.candidate_lineage_id
   and forward_rung.rung = 'FORWARD'
  left join quant.intraday_forward_confirmations confirmation
    on confirmation.experiment_rung_id = forward_rung.experiment_rung_id
  join lateral (
    select dimensions
      from quant.experiment_metrics metric
     where metric.experiment_id = e.experiment_id
       and metric.split = 'WALK_FORWARD'
       and metric.metric = 'intraday_gate_pass'
     order by metric.experiment_metric_id desc
     limit 1
  ) gate on true
 where e.status = 'COMPLETED'
   and hypothesis.status = 'INCONCLUSIVE'
   and manifest.report->>'evidence_tier' =
       'SEARCH_EXPOSED_HISTORICAL_SUPPORT'
   and gate.dimensions->>'decision' = 'HOLD'
   and gate.dimensions->'failed_criteria' =
       '["INDEPENDENT_FORWARD_CONFIRMATION_PENDING"]'::jsonb
   and confirmation.forward_confirmation_id is null
   and exists (
     select 1 from quant.intraday_session_accesses access
      where access.root_lineage_id = full_rung.root_lineage_id
   )
   and {_GOVERNED_FORWARD_STOCK_EVIDENCE}
on conflict (experiment_id) do nothing
"""

_FORWARD_LEASE_SQL = """
with due as (
  select work.experiment_id
    from quant.intraday_forward_work_items work
   where work.status in ('READY', 'WAITING', 'RETRY')
     and work.next_attempt_at <= %s::timestamptz
     and not exists (
       select 1
         from quant.intraday_experiment_rungs forward_rung
         join quant.intraday_forward_confirmations confirmation
           on confirmation.experiment_rung_id =
              forward_rung.experiment_rung_id
        where forward_rung.experiment_id = work.experiment_id
          and forward_rung.rung = 'FORWARD'
     )
   order by coalesce(work.next_attempt_at, work.lease_expires_at),
            work.attempt_count, work.created_at, work.experiment_id
   for update skip locked
   limit %s
), claimed as (
  update quant.intraday_forward_work_items work
     set status = 'LEASED', next_attempt_at = null,
         attempt_count = work.attempt_count + 1,
         leased_at = %s::timestamptz,
         lease_expires_at = %s::timestamptz +
           interval '1 minute' * %s,
         leased_by = %s, lease_token = gen_random_uuid(),
         updated_at = %s::timestamptz
    from due
   where work.experiment_id = due.experiment_id
  returning work.experiment_id::text, work.lease_token::text,
            work.attempt_count, work.error_count, work.max_error_count
)
select experiment_id, lease_token, attempt_count, error_count, max_error_count
  from claimed
"""


def _bounded_int(edge: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(edge.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None
    if not lo <= value <= hi:
        raise ValueError(f"{key}={value} outside [{lo}, {hi}]")
    return value


def _bounded_float(edge: dict, key: str, default: float,
                   lo: float, hi: float) -> float:
    try:
        value = float(edge.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be numeric") from None
    if not lo <= value <= hi:
        raise ValueError(f"{key}={value} outside [{lo}, {hi}]")
    return value


def _strict_bool(edge: dict, key: str, default: bool) -> bool:
    value = edge.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"{key} must be boolean")
    return value


def _expected_resolved_data_contract(event_source: str) -> dict:
    event_source = str(event_source or "").upper()
    dataset = DATASET_BY_EVENT_SOURCE.get(event_source)
    if dataset is None:
        raise RuntimeError(f"unsupported resolved event source: {event_source!r}")
    if event_source == EXTERNAL_EVENT_SOURCE:
        return {
            "dataset": "/".join(dataset),
            "event_source": EXTERNAL_EVENT_SOURCE,
            "timestamp_policy": COMPLETED_SECOND_POLICY,
            "physical_sources": {
                "market_quotes": "ext_src.quotes",
                "market_ticks": "ext_src.ticks",
            },
            "source_versions": {
                "market_quotes": "trading-bot-completed-second-book-v1",
                "market_ticks": "trading-bot-completed-second-trade-v1",
            },
            "knowledge_clock": "EVENT_TIME_ONLY_NO_RECEIPT_CLOCK",
            "evidence_scope": "HISTORICAL_SEARCH_ONLY",
            "content_window": {
                "timezone": "Asia/Seoul",
                "start": "09:00:00",
                "end_exclusive": "15:30:00",
            },
            "maximum_horizon_seconds": EXTERNAL_REPLAY_MAX_HORIZON_SECONDS,
            "execution": "TAKER_ONLY",
        }
    return {
        "dataset": "/".join(dataset),
        "event_source": LOCAL_EVENT_SOURCE,
        "timestamp_policy": STRICT_TIMESTAMP_POLICY,
        "physical_sources": {
            "market_quotes": "market.market_quotes",
            "market_ticks": "market.market_ticks",
        },
        "source_versions": {
            "market_quotes": "ls-realtime-book-v1",
            "market_ticks": "ls-realtime-trade-v1",
        },
        "knowledge_clock": "AVAILABLE_AT_RECEIPT_CLOCK",
        "evidence_scope": "ARRIVAL_TIME_CAUSAL",
    }


def _assert_resolved_data_contract(config: dict, *, selected: dict | None = None,
                                   require: bool = True) -> dict:
    """Fail closed if resolver, runner source, dataset, or clock diverge."""

    contract = config.get("resolved_data_contract")
    if not isinstance(contract, dict) or not contract:
        if require:
            raise RuntimeError(
                "intraday replay requires a resolver-approved data contract")
        return {}
    event_source = str(config.get("data_source") or "").upper()
    expected = _expected_resolved_data_contract(event_source)
    mismatches = [key for key, value in expected.items()
                  if contract.get(key) != value]
    if config.get("timestamp_policy") not in (None, expected["timestamp_policy"]):
        mismatches.append("runtime_timestamp_policy")
    if selected is not None and str(selected.get("event_source") or "").upper() \
            != event_source:
        mismatches.append("selected_event_source")
    if mismatches:
        raise RuntimeError(
            "resolved intraday dataset/source/clock contract mismatch: "
            + ",".join(sorted(set(mismatches))))
    return contract


def config_from_edge(edge: dict) -> tuple[dict, IntradayLaneSpec]:
    """Bind only controlled knobs; never silently inherit daily defaults."""
    if str(edge.get("research_lane") or "").strip().upper() != INTRADAY_LANE:
        raise ValueError("intraday runner requires research_lane=INTRADAY_EVENT")
    if str(edge.get("universe_key") or "krx_all").lower() != "krx_all":
        raise ValueError("intraday runner currently requires universe_key=krx_all")
    data_source = str(edge.get("data_source") or "AUTO").upper()
    if data_source not in {"AUTO", LOCAL_EVENT_SOURCE,
                           EXTERNAL_EVENT_SOURCE}:
        raise ValueError(f"unsupported intraday data_source={data_source!r}")
    resolved_data_contract = edge.get("resolved_data_contract")
    if resolved_data_contract not in (None, {}) and not isinstance(
            resolved_data_contract, dict):
        raise ValueError("resolved_data_contract must be an object")
    raw_feature_window_contract = edge.get(
        "feature_window_contract_version")
    feature_window_contract = (
        LEGACY_FEATURE_WINDOW_CONTRACT
        if raw_feature_window_contract is None else
        str(raw_feature_window_contract))
    if feature_window_contract not in {
            LEGACY_FEATURE_WINDOW_CONTRACT,
            EXPLICIT_FEATURE_WINDOW_CONTRACT}:
        raise ValueError(
            "unsupported feature-window contract "
            f"{feature_window_contract!r}")
    expression = validate_feature_window_contract(
        edge.get("intraday_signal_expr"),
        contract_version=feature_window_contract)
    raw_source_baseline = edge.get("source_baseline_expr")
    source_baseline = (
        parse_expr(raw_source_baseline)
        if raw_source_baseline not in (None, "") else None)
    migration_parent_fp = str(
        edge.get("migration_parent_ast_fingerprint") or "")
    migration_parent_contract = str(
        edge.get("migration_parent_feature_window_contract_version") or "")
    if bool(migration_parent_fp) != bool(migration_parent_contract):
        raise ValueError(
            "migration parent fingerprint and feature-window contract "
            "must be declared together")
    if migration_parent_fp:
        if (feature_window_contract != EXPLICIT_FEATURE_WINDOW_CONTRACT
                or migration_parent_contract !=
                LEGACY_FEATURE_WINDOW_CONTRACT):
            raise ValueError(
                "migration parent must be a legacy-to-explicit audit edge")
        if edge.get("parent_ast_fingerprint") not in (None, ""):
            raise ValueError(
                "cross-contract migration parent cannot be an in-cohort parent")
        if (len(migration_parent_fp) != 16
                or any(char not in "0123456789abcdef"
                       for char in migration_parent_fp)):
            raise ValueError("migration parent AST fingerprint is invalid")
    semantic_plan = validate_semantic_plan(edge.get("semantic_plan") or {})
    output = str(semantic_plan.get("output") or "").upper()
    entry_policy = str(edge.get("entry_policy") or "").upper()
    coefficient_policy = str(edge.get("coefficient_policy") or
                             "PREREGISTERED_NO_OOS_FIT").upper()
    if coefficient_policy not in COEFFICIENT_POLICIES:
        raise ValueError(
            f"unsupported coefficient_policy={coefficient_policy!r}")
    if output in {"TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL"}:
        if (coefficient_policy != "STRUCTURE_ONLY"
                and unit_of(expression) != "BPS"):
            raise ValueError(
                "fixed/preregistered net-PnL formulas must predict BPS")
        if coefficient_policy == "STRUCTURE_ONLY" and unit_of(expression) == "BOOL":
            raise ValueError("STRUCTURE_ONLY formula must emit a numeric score")
        if entry_policy != "PREDICTED_MARKOUT_CLEARS_COST":
            raise ValueError(
                "net-PnL intraday formulas require "
                "entry_policy=PREDICTED_MARKOUT_CLEARS_COST")
    elif not entry_policy:
        entry_policy = "POSITIVE_SCORE"
    horizon = _bounded_int(edge, "horizon_seconds", 5, 1, 3600)
    if int(semantic_plan["horizon_seconds"]) != horizon:
        raise ValueError("semantic_plan horizon must match horizon_seconds")
    execution = str(edge.get("execution") or "TAKER").upper()
    if execution not in {"TAKER", "PASSIVE_FIFO_LOWER_BOUND"}:
        raise ValueError(f"unsupported execution={execution!r}")
    if str(semantic_plan["execution"]).upper() != execution:
        raise ValueError("semantic_plan execution must match execution")
    position_mode = str(edge.get("position_mode") or "LONG_ONLY").upper()
    if position_mode != "LONG_ONLY":
        raise ValueError(
            "position_mode must be LONG_ONLY until point-in-time borrow availability, "
            "borrow fees, and short-sale execution constraints are available")
    sample_interval = _bounded_int(
        edge, "sample_interval_seconds", 5, 1, 300)
    requested_lookback = _bounded_int(
        edge, "feature_lookback_seconds", 30, 1, 3600)
    screening = edge.get("screening_population") or []
    if not isinstance(screening, list) or len(screening) > 7:
        raise ValueError("screening_population must contain at most seven candidates")
    cohort_version = str(edge.get("screening_cohort_version") or "")
    if screening and cohort_version != INTRADAY_SCREENING_COHORT_VERSION:
        raise StaleIntradayCohortError(
            "populated intraday screening cohort must use "
            f"{INTRADAY_SCREENING_COHORT_VERSION}; got "
            f"{cohort_version or '(missing)'}. Reassemble it under the current "
            "formula and structural-ablation contract before replay")
    parsed_screening = []
    known = {fingerprint(expression)}
    all_clocks = set(clocks_of(expression))
    all_horizons = {horizon}
    executions = {execution}
    for index, raw in enumerate(screening):
        if not isinstance(raw, dict):
            raise ValueError(f"screening_population[{index}] must be an object")
        raw_candidate_contract = raw.get(
            "feature_window_contract_version")
        candidate_contract = (
            feature_window_contract
            if raw_candidate_contract is None else
            str(raw_candidate_contract))
        if candidate_contract != feature_window_contract:
            raise ValueError(
                f"screening_population[{index}] feature-window contract "
                "differs from the primary")
        candidate_expr = validate_feature_window_contract(
            raw.get("intraday_signal_expr"),
            contract_version=candidate_contract)
        candidate_fp = fingerprint(candidate_expr)
        if candidate_fp in known:
            raise ValueError(
                f"screening_population[{index}] duplicates another candidate")
        if raw.get("ast_fingerprint") not in (None, "", candidate_fp):
            raise ValueError(
                f"screening_population[{index}] fingerprint does not match AST")
        known.add(candidate_fp)
        plan = validate_semantic_plan(raw.get("semantic_plan") or {})
        candidate_horizon = int(plan["horizon_seconds"])
        candidate_execution = str(plan["execution"]).upper()
        candidate_output = str(plan["output"]).upper()
        policy = str(raw.get("entry_policy") or "").upper()
        candidate_coefficient_policy = str(
            raw.get("coefficient_policy") or
            "PREREGISTERED_NO_OOS_FIT").upper()
        if candidate_coefficient_policy not in COEFFICIENT_POLICIES:
            raise ValueError(
                f"screening_population[{index}] has unsupported coefficient_policy")
        if candidate_output in {
                "TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL"}:
            if (candidate_coefficient_policy != "STRUCTURE_ONLY"
                    and unit_of(candidate_expr) != "BPS"):
                raise ValueError(
                    f"screening_population[{index}] fixed AST must output BPS")
            if (candidate_coefficient_policy == "STRUCTURE_ONLY"
                    and unit_of(candidate_expr) == "BOOL"):
                raise ValueError(
                    f"screening_population[{index}] must emit a numeric score")
            if policy != "PREDICTED_MARKOUT_CLEARS_COST":
                raise ValueError(
                    f"screening_population[{index}] lacks the cost hurdle")
        elif not policy:
            policy = "POSITIVE_SCORE"
        all_horizons.add(candidate_horizon)
        executions.add(candidate_execution)
        all_clocks.update(clocks_of(candidate_expr))
        parsed_screening.append({
            **raw,
            **({"feature_window_contract_version": candidate_contract}
               if feature_window_contract ==
               EXPLICIT_FEATURE_WINDOW_CONTRACT else {}),
            "ast_fingerprint": candidate_fp,
            "intraday_signal_expr": candidate_expr,
            "semantic_plan": plan,
            "horizon_seconds": candidate_horizon,
            "execution": candidate_execution,
            "entry_policy": policy,
            "coefficient_policy": candidate_coefficient_policy,
            "source_baseline_expr": (
                parse_expr(raw["source_baseline_expr"])
                if raw.get("source_baseline_expr") not in (None, "")
                else None),
            "screening_only": True,
        })
    feature_lookback = (
        requested_lookback
        if feature_window_contract == EXPLICIT_FEATURE_WINDOW_CONTRACT else
        max([requested_lookback, *all_clocks]))
    if feature_lookback > 3600:
        raise ValueError("population feature lookback exceeds 3600 seconds")
    population_execution = (
        "PASSIVE_FIFO_LOWER_BOUND"
        if "PASSIVE_FIFO_LOWER_BOUND" in executions else "TAKER")
    raw_cube_spec = edge.get("feature_cube_spec")
    if feature_window_contract == EXPLICIT_FEATURE_WINDOW_CONTRACT:
        frozen_cube_spec = (
            FeatureCubeSpec()
            if raw_cube_spec is None else
            FeatureCubeSpec.from_dict(raw_cube_spec))
    else:
        if raw_cube_spec is not None:
            raise ValueError(
                "feature_cube_spec requires the explicit-window contract")
        frozen_cube_spec = None
    config = {
        "research_lane": "INTRADAY_EVENT",
        "data_source": data_source,
        "resolved_data_contract": dict(resolved_data_contract or {}),
        "semantic_plan": semantic_plan,
        "semantic_fingerprint": edge.get("semantic_fingerprint"),
        "intraday_signal_expr": expression,
        "source_baseline_expr": source_baseline,
        "parent_ast_fingerprint": str(
            edge.get("parent_ast_fingerprint") or ""),
        "parent_candidate_identity_fingerprint": edge.get(
            "parent_candidate_identity_fingerprint"),
        "horizon_seconds": horizon,
        "sample_interval_seconds": sample_interval,
        "feature_lookback_seconds": feature_lookback,
        "order_latency_ms": _bounded_int(edge, "order_latency_ms", 250, 0, 10_000),
        "max_quote_age_seconds": _bounded_float(
            edge, "max_quote_age_seconds", 5.0, 0.001, 60.0),
        "fee_bps_per_side": _bounded_float(
            edge, "fee_bps_per_side", DEFAULT_FEE_BPS_PER_SIDE,
            MIN_EQUITY_FEE_BPS_PER_SIDE, 100.0),
        "maker_fee_bps_per_side": _bounded_float(
            edge, "maker_fee_bps_per_side", DEFAULT_FEE_BPS_PER_SIDE,
            MIN_EQUITY_FEE_BPS_PER_SIDE, 100.0),
        "execution": execution,
        "position_mode": position_mode,
        "threshold": _bounded_float(edge, "threshold", 0.0, 0.0, 1_000_000.0),
        "entry_policy": entry_policy,
        "coefficient_policy": coefficient_policy,
        "minimum_predicted_edge_bps": _bounded_float(
            edge, "minimum_predicted_edge_bps", 0.0, 0.0, 10_000.0),
        "evaluation_days": _bounded_int(edge, "evaluation_days", 60, 60, 60),
        # Raw external L10/tape validation spans roughly 92GB.  Most generated
        # equations must first clear a deterministic, non-promoting discovery
        # panel; only a primary not shown futile proceeds to all collected
        # instruments. A six-session point estimate need not itself be positive.
        "fast_screen_enabled": _strict_bool(
            edge, "fast_screen_enabled", True),
        "fast_screen_sessions": _bounded_int(
            edge, "fast_screen_sessions", 6, 3, 20),
        "fast_screen_instruments": _bounded_int(
            edge, "fast_screen_instruments", 16, 8, 128),
        "fast_screen_min_opportunities": _bounded_int(
            edge, "fast_screen_min_opportunities", 100, 1, 1_000_000),
        "fast_screen_min_net_bps": _bounded_float(
            edge, "fast_screen_min_net_bps", 0.0, -100.0, 1_000.0),
        # The default six-session screen is a futility test, not an estimate
        # precise enough to demand a positive point return.  Preserve a
        # deliberately preregistered point floor when the proposal explicitly
        # supplied one, while keeping the default path CI-based.
        "fast_screen_hard_net_floor_enabled": (
            edge.get("fast_screen_min_net_bps") is not None),
        "intermediate_screen_enabled": _strict_bool(
            edge, "intermediate_screen_enabled", True),
        "intermediate_screen_sessions": _bounded_int(
            edge, "intermediate_screen_sessions", 20, 10, 40),
        "intermediate_screen_instruments": _bounded_int(
            edge, "intermediate_screen_instruments", 64, 16, 256),
        "intermediate_candidate_budget": _bounded_int(
            edge, "intermediate_candidate_budget", 3, 1, 8),
        "successive_halving_eta": _bounded_int(
            edge, "successive_halving_eta", 3, 2, 8),
        "forward_confirmation_min_new_sessions": _bounded_int(
            edge, "forward_confirmation_min_new_sessions", 20, 20, 120),
        # A shard is only a bounded execution unit. The scientific universe is
        # every causally observed calibration instrument, never a top-N sample.
        "universe_mode": "ALL_CAUSALLY_COLLECTED",
        "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "instrument_shard_size": _bounded_int(
            edge, "instrument_shard_size", 8, 2, 64),
        "screening_population": parsed_screening,
        "screening_cohort_version": cohort_version or None,
        "screening_trial_exposure": len(parsed_screening),
        # Trusted system metadata stamped by factory_bridge from two disjoint
        # DB queries.  It is deliberately separate from the current synchronous
        # population: eliminated/old sidecars still remain in the DSR denominator.
        "primary_attempts_before": _bounded_int(
            edge, "primary_attempts_before", 0, 0, 1_000_000),
        "historical_exact_screening_exposures": _bounded_int(
            edge, "historical_exact_screening_exposures", 0, 0, 1_000_000),
        "population_execution_model": population_execution,
    }
    if feature_window_contract == EXPLICIT_FEATURE_WINDOW_CONTRACT:
        config.update({
            "feature_window_contract_version": feature_window_contract,
            "feature_cube_spec": frozen_cube_spec.as_dict(),
            "evaluator_version": EXPLICIT_WINDOW_EVALUATOR_VERSION,
        })
    if migration_parent_fp:
        config.update({
            "migration_parent_ast_fingerprint": migration_parent_fp,
            "migration_parent_feature_window_contract_version":
                migration_parent_contract,
        })
    if edge.get("instrument_count") is not None:
        config["legacy_instrument_count_ignored"] = int(edge["instrument_count"])
    if config["fast_screen_sessions"] != 6:
        raise ValueError("DISCOVERY_6 requires exactly six sessions")
    if config["intermediate_screen_sessions"] != 20:
        raise ValueError("VALIDATION_20 requires exactly twenty sessions")
    if config["intermediate_screen_enabled"]:
        if (config["intermediate_screen_sessions"] <=
                config["fast_screen_sessions"]):
            raise ValueError(
                "intermediate_screen_sessions must exceed fast_screen_sessions")
        if (config["intermediate_screen_sessions"] >=
                config["evaluation_days"]):
            if edge.get("intermediate_screen_sessions") is None:
                config["intermediate_screen_enabled"] = False
            else:
                raise ValueError(
                    "intermediate_screen_sessions must be below evaluation_days")
    spec = IntradayLaneSpec(
        sample_interval_seconds=config["sample_interval_seconds"],
        feature_lookback_seconds=config["feature_lookback_seconds"],
        horizons_seconds=tuple(sorted(all_horizons)),
        order_latency_ms=config["order_latency_ms"],
        max_quote_age_seconds=config["max_quote_age_seconds"],
        fee_bps_per_side=config["fee_bps_per_side"],
        maker_fee_bps_per_side=config["maker_fee_bps_per_side"],
    )
    if config["resolved_data_contract"]:
        _assert_resolved_data_contract(config)
    return config, spec


def _session_bounds(day) -> tuple[datetime, datetime]:
    return (datetime.combine(day, time(9, 0), KST).astimezone(timezone.utc),
            datetime.combine(day, time(15, 20), KST).astimezone(timezone.utc))


def _build_runtime_samples(config: dict, quotes, trades,
                           spec: IntradayLaneSpec, *, start: datetime,
                           end: datetime):
    """Select the versioned sample contract without changing legacy replay."""
    kwargs = {
        "start": start,
        "end": end,
        "execution_model": config["population_execution_model"],
        "timestamp_policy": config["timestamp_policy"],
    }
    if (_feature_window_contract(config) ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT):
        return build_sample_batch(
            quotes, trades, spec, cube_spec=_feature_cube_spec(config),
            **kwargs)
    return build_samples(quotes, trades, spec, **kwargs)


def _session_day(value) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value))


def _stratified(values, count: int) -> list:
    """Deterministically cover the beginning, middle and end of a population."""
    rows = list(values)
    if count >= len(rows):
        return rows
    if count <= 1:
        return [rows[len(rows) // 2]] if rows else []
    indices = [round(index * (len(rows) - 1) / (count - 1))
               for index in range(count)]
    return [rows[index] for index in dict.fromkeys(indices)]


def _nested_session_prefix(values, count: int) -> list:
    """Outcome-blind nested session set, returned in chronological order."""
    sessions = sorted({_session_day(value) for value in values})
    ordered = sorted(sessions, key=lambda session: (
        hashlib.sha256(
            f"krx-session-bracket-v2|{session.isoformat()}".encode()
        ).hexdigest(),
        session,
    ))
    chosen = set(ordered[:min(int(count), len(ordered))])
    return [session for session in sessions if session in chosen]


def _stock_only_slice(meta_conn, market_conn, selected: dict) -> dict:
    """Intersect the frozen raw universe with the governed STOCK master.

    Product identity lives in the reference plane while external replay keys
    are symbols in TimescaleDB.  Missing mappings fail closed.  The exact kept
    and excluded counts become part of the experiment input hash through the
    returned slice manifest.
    """
    if selected.get("status") != "PASS":
        return selected
    instruments = [str(value) for value in selected.get("instruments") or []]
    if not instruments:
        return {**selected, "status": "INSUFFICIENT_INSTRUMENTS"}
    external = selected.get("event_source") == EXTERNAL_EVENT_SOURCE
    if external:
        with market_conn.cursor() as cur:
            cur.execute("""
                select symbol, instrument_id::text
                  from market.symbol_map
                 where symbol = any(%s)
            """, (instruments,))
            identity = {str(symbol): str(instrument_id)
                        for symbol, instrument_id in cur.fetchall()}
    else:
        identity = {instrument: instrument for instrument in instruments}
    mapped_ids = sorted(set(identity.values()))
    selected_days = [_session_day(value) for value in [
        *(selected.get("calibration_sessions") or []),
        *(selected.get("sessions") or []),
    ]]
    if not selected_days:
        raise RuntimeError(
            "intraday PASS slice has no calibration or evaluation sessions")
    symbol_as_of = datetime.combine(
        max(selected_days), time(15, 30), KST).astimezone(timezone.utc)
    with meta_conn.cursor() as cur:
        cur.execute("""
            select i.instrument_id::text, i.instrument_type, i.market,
                   i.venue, i.status, i.listed_from, i.listed_to,
                   i.asset_class, i.is_spac
              from quant.current_krx_stock_instrument_identity i
             where i.instrument_id = any(%s::uuid[])
        """, (mapped_ids,))
        metadata = {str(row[0]): {
            "instrument_type": str(row[1]), "market": str(row[2]),
            "venue": None if row[3] is None else str(row[3]),
            "status": str(row[4]),
            "listed_from": row[5], "listed_to": row[6],
            "asset_class": str(row[7]),
            "is_spac": bool(row[8]),
            "valid_symbols": set(),
        } for row in cur.fetchall()}
        symbol_owners: dict[str, set[str]] = {}
        if external:
            cur.execute("""
                select symbol.instrument_id::text, symbol.symbol
                  from reference.instrument_symbols symbol
                  join quant.current_krx_stock_instrument_identity instrument
                    on instrument.instrument_id = symbol.instrument_id
                 where upper(symbol.provider) = 'LS'
                   and upper(symbol.market) = 'KRX'
                   and upper(symbol.symbol_type) = 'TRADING'
                   and symbol.is_primary
                   and symbol.valid_from <= %s
                   and (symbol.valid_to is null or symbol.valid_to > %s)
                   and (
                     symbol.instrument_id = any(%s::uuid[])
                     or symbol.symbol = any(%s)
                   )
            """, (symbol_as_of, symbol_as_of, mapped_ids, instruments))
            for raw_id, raw_symbol in cur.fetchall():
                instrument_id = str(raw_id)
                trading_symbol = str(raw_symbol)
                symbol_owners.setdefault(trading_symbol, set()).add(
                    instrument_id)
                if instrument_id in metadata:
                    metadata[instrument_id]["valid_symbols"].add(
                        trading_symbol)
    kept = []
    kept_reference_ids = []
    excluded = {"NON_STOCK": 0, "NON_EQUITY": 0, "NON_KRX": 0,
                "INACTIVE": 0, "SPAC": 0,
                "OUTSIDE_LISTING_INTERVAL": 0,
                "INVALID_TRADING_SYMBOL_FORMAT": 0,
                "AMBIGUOUS_VALID_SYMBOL_IDENTITY": 0,
                "MISSING_VALID_SYMBOL_IDENTITY": 0,
                "MISSING_SYMBOL_MAP": 0, "MISSING_REFERENCE_METADATA": 0}
    venues: dict[str, int] = {}
    for instrument in instruments:
        instrument_id = identity.get(instrument)
        if instrument_id is None:
            excluded["MISSING_SYMBOL_MAP"] += 1
            continue
        row = metadata.get(instrument_id)
        if row is None:
            excluded["MISSING_REFERENCE_METADATA"] += 1
            continue
        if row["instrument_type"].upper() != "STOCK":
            excluded["NON_STOCK"] += 1
            continue
        if row["asset_class"].upper() != "EQUITY":
            excluded["NON_EQUITY"] += 1
            continue
        if row["market"].upper() != "KRX":
            excluded["NON_KRX"] += 1
            continue
        if row["status"].upper() != "ACTIVE":
            excluded["INACTIVE"] += 1
            continue
        if row["is_spac"]:
            excluded["SPAC"] += 1
            continue
        if (row["listed_from"] is not None
                and row["listed_from"] > min(selected_days)) or (
                row["listed_to"] is not None
                and row["listed_to"] < max(selected_days)):
            excluded["OUTSIDE_LISTING_INTERVAL"] += 1
            continue
        if external:
            if (len(instrument) != 6 or not instrument.isascii() or
                    not all("0" <= char <= "9" or "A" <= char <= "Z"
                            for char in instrument)):
                excluded["INVALID_TRADING_SYMBOL_FORMAT"] += 1
                continue
            if instrument not in row["valid_symbols"]:
                excluded["MISSING_VALID_SYMBOL_IDENTITY"] += 1
                continue
            if symbol_owners.get(instrument) != {instrument_id}:
                excluded["AMBIGUOUS_VALID_SYMBOL_IDENTITY"] += 1
                continue
        kept.append(instrument)
        kept_reference_ids.append(instrument_id)
        venue = row.get("venue") or row.get("market") or "UNKNOWN"
        venues[venue] = venues.get(venue, 0) + 1
    status = "PASS" if len(kept) >= 2 else "INSUFFICIENT_INSTRUMENTS"
    profiles = selected.get("instrument_profiles") or {}
    return {
        **selected,
        "status": status,
        "instruments": kept,
        # Kept in the same order as ``instruments``.  The runtime uses these
        # governed UUIDs when freezing exact rung universes; raw replay still
        # uses the external symbol keys expected by ext_src.
        "reference_instrument_ids": kept_reference_ids,
        "reference_identity_fingerprint": stable_fingerprint([{
            "symbol": symbol,
            "instrument_id": instrument_id,
            "instrument_type": metadata[instrument_id]["instrument_type"],
            "asset_class": metadata[instrument_id]["asset_class"],
            "market": metadata[instrument_id]["market"],
            "status": metadata[instrument_id]["status"],
            "is_spac": metadata[instrument_id]["is_spac"],
            "listed_from": metadata[instrument_id]["listed_from"],
            "listed_to": metadata[instrument_id]["listed_to"],
            "symbol_identity_as_of": symbol_as_of.isoformat(),
        } for symbol, instrument_id in zip(kept, kept_reference_ids)]),
        "instrument_profiles": {
            key: value for key, value in profiles.items() if key in set(kept)},
        "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "product_filter_version": "krx-stock-only-v3",
        "asset_scope": STOCK_ASSET_SCOPE,
        "stock_universe_contract_version": STOCK_UNIVERSE_VERSION,
        "pre_product_filter_instruments": len(instruments),
        "post_product_filter_instruments": len(kept),
        "product_filter_excluded": excluded,
        "stock_venues": dict(sorted(venues.items())),
        "symbol_identity_as_of": symbol_as_of.isoformat(),
        "symbol_valid_time_required": external,
        "historical_listing_interval_verified": all(
            metadata[instrument_id]["listed_from"] is not None
            for instrument_id in kept_reference_ids),
        "historical_listing_interval_policy": (
            "REFERENCE_LISTED_DATES_WHEN_AVAILABLE; OTHERWISE_CURRENT_IDENTITY_"
            "AT_LAST_SEARCH_SESSION_AND_FORWARD_LOCKBOX_REQUIRED"),
        "unknown_product_identity_policy": "FAIL_CLOSED_EXCLUDE",
    }


def _assert_stock_selection_evidence(selected: dict) -> None:
    """Reject a PASS slice that bypassed the governed reference intersection."""
    if selected.get("status") != "PASS":
        return
    instruments = [str(value) for value in
                   selected.get("instruments") or []]
    reference_ids = [str(value) for value in
                     selected.get("reference_instrument_ids") or []]
    sessions = list(selected.get("sessions") or [])
    required = {
        "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "product_filter_version": "krx-stock-only-v3",
        "asset_scope": STOCK_ASSET_SCOPE,
        "stock_universe_contract_version": STOCK_UNIVERSE_VERSION,
        "unknown_product_identity_policy": "FAIL_CLOSED_EXCLUDE",
    }
    mismatched = {
        key: {"expected": expected, "actual": selected.get(key)}
        for key, expected in required.items()
        if selected.get(key) != expected
    }
    if (mismatched or not instruments or not sessions
            or len(reference_ids) != len(instruments)
            or len(set(reference_ids)) != len(reference_ids)
            or selected.get("post_product_filter_instruments") != len(
                instruments)
            or not selected.get("reference_identity_fingerprint")):
        raise RuntimeError(
            "intraday PASS slice lacks exact governed stock-scope evidence: "
            f"mismatched={sorted(mismatched)} "
            f"instruments={len(instruments)} "
            f"reference_ids={len(reference_ids)} sessions={len(sessions)}")


def _profiled_panel(selected: dict, count: int) -> tuple[list[str], dict]:
    """Return a prefix of one pre-OOS, nested instrument ordering.

    Odd positions use calibration-only information richness; even positions
    use an identity-hash guard over the full stock universe.  Consequently the
    16-name panel is a strict subset of the 64-name panel and neither ranking
    can see evaluation-period liquidity.  Missing calibration profiles are not
    guessed; they enter only through the representative hash guard.
    """
    instruments = [str(value) for value in selected.get("instruments") or []]
    profiles = selected.get("instrument_profiles") or {}

    def numeric(instrument: str, key: str, default: float) -> float:
        value = (profiles.get(instrument) or {}).get(key)
        return (float(value) if isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(float(value))
                else default)

    profiled = [instrument for instrument in instruments
                if instrument in profiles and
                numeric(instrument, "quote_events", 0.0) > 0.0]
    # The tuple is explicit and stable. Lower spread is more informative after
    # costs; activity/depth avoid zero-opportunity panels. Every input is from
    # the sessions named by ``instrument_profile_sessions``.
    information_order = sorted(profiled, key=lambda instrument: (
        -numeric(instrument, "quote_events", 0.0),
        -numeric(instrument, "trade_intensity", 0.0),
        numeric(instrument, "spread_bps", float("inf")),
        -numeric(instrument, "depth_notional_l1", 0.0),
        instrument,
    ))
    # The representative guard also starts from calibration-observed names;
    # otherwise a symbol first seen late in the evaluation period could enter
    # the early bracket merely because its future membership was known.  Only
    # an explicitly labelled insufficiency fallback may draw from the rest.
    fallback_required = len(profiled) < min(int(count), len(instruments))
    representative_order = sorted(profiled, key=lambda instrument: (
        hashlib.sha256(
            f"krx-stock-panel-guard-v2|{instrument}".encode()).hexdigest(),
        instrument,
    ))
    ordered: list[str] = []
    roles: dict[str, str] = {}
    seen: set[str] = set()
    information_index = representative_index = 0
    while len(ordered) < len(profiled):
        while (information_index < len(information_order)
               and information_order[information_index] in seen):
            information_index += 1
        if information_index < len(information_order):
            instrument = information_order[information_index]
            information_index += 1
            ordered.append(instrument)
            seen.add(instrument)
            roles[instrument] = "CALIBRATION_INFORMATION_RICH"
        while (representative_index < len(representative_order)
               and representative_order[representative_index] in seen):
            representative_index += 1
        if representative_index < len(representative_order):
            instrument = representative_order[representative_index]
            representative_index += 1
            ordered.append(instrument)
            seen.add(instrument)
            roles[instrument] = "IDENTITY_HASH_REPRESENTATIVE_GUARD"
    unprofiled = sorted(
        (value for value in instruments if value not in seen),
        key=lambda instrument: (
            hashlib.sha256(
                f"krx-stock-panel-fallback-v2|{instrument}".encode()
            ).hexdigest(), instrument))
    for instrument in unprofiled:
        ordered.append(instrument)
        roles[instrument] = "INSUFFICIENT_CALIBRATION_MEMBERSHIP_FALLBACK"
    panel = ordered[:min(int(count), len(ordered))]
    information = [value for value in panel
                   if roles[value] == "CALIBRATION_INFORMATION_RICH"]
    representative = [value for value in panel
                      if roles[value] != "CALIBRATION_INFORMATION_RICH"]
    return panel, {
        "version": "krx-profiled-discovery-panel-v2",
        "mode": ("FULL_AVAILABLE_UNIVERSE" if len(panel) == len(instruments)
                 else "NESTED_INFORMATION_AND_IDENTITY_HASH_GUARD"),
        "profile_source": "STRICTLY_PRE_EVALUATION_CALIBRATION_SUMMARIES",
        "profile_sessions": list(selected.get("instrument_profile_sessions") or []),
        "ranking_fields": [
            "quote_events", "trade_intensity", "spread_bps",
            "depth_notional_l1"],
        "information_rich": information,
        "representative_guard": representative,
        "ordered_universe_fingerprint": stable_fingerprint(ordered),
        "nested_prefix_contract": True,
        "missing_profile_policy": "IDENTITY_HASH_GUARD_ONLY",
        "calibration_observed_pool_size": len(profiled),
        "insufficient_calibration_membership_fallback": fallback_required,
        "promotion_authority": False,
    }


def _fast_screen_gate(report: dict, config: dict) -> dict:
    """Decide whether the preregistered primary merits the 92GB replay.

    This is a resource gate, never alpha evidence. Linked candidates that pass
    remain screening-only evolutionary nominations and cannot cause a different
    primary equation to be silently substituted into the confirmatory run.
    """
    candidates = [("PRIMARY", report)] + [
        (row.get("ast_fingerprint") or "", row)
        for row in report.get("screening_population") or []]

    def numeric(value) -> bool:
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(float(value)))

    def qualifies(row: dict) -> bool:
        summary = row.get("summary") or {}
        opportunities = summary.get("opportunities")
        net = summary.get("mean_net_bps_per_opportunity")
        session_ci_high = summary.get("session_net_ci_high_bps")
        # A malformed/non-finite count must fail the resource gate instead of
        # raising from ``int(nan)`` and aborting the entire discovery run.
        if (not numeric(opportunities)
                or float(opportunities) <
                config["fast_screen_min_opportunities"]):
            return False
        if not numeric(net) or not numeric(session_ci_high):
            return False
        # With only six sessions, a negative point estimate is not by itself
        # sufficient evidence of futility.  Allocate more replay only while the
        # preregistered resampling UCB still admits positive cost-net performance.
        # This gate never promotes a candidate; the 60-session evaluator still
        # requires a positive lower CI, DSR, PBO, coverage and cost survival.
        if float(session_ci_high) <= 0:
            return False
        if config.get("fast_screen_hard_net_floor_enabled", False):
            return float(net) > config["fast_screen_min_net_bps"]
        return True

    survivors = [key for key, row in candidates if qualifies(row)]
    return {
        "version": FAST_SCREEN_VERSION,
        "primary_pass": "PRIMARY" in survivors,
        "survivors": survivors,
        "linked_survivor_count": sum(key != "PRIMARY" for key in survivors),
        "criteria": {
            "minimum_opportunities": config["fast_screen_min_opportunities"],
            "futility_rule": (
                "small_sample_resampling_ucb_must_exceed_zero"),
            "confidence_claim": "RESOURCE_HEURISTIC_NOT_95PCT_EVIDENCE",
            "positive_point_estimate_required_by_default": False,
            "hard_mean_net_floor_enabled": bool(config.get(
                "fast_screen_hard_net_floor_enabled", False)),
            "hard_mean_net_bps_per_opportunity_exclusive": (
                config["fast_screen_min_net_bps"]
                if config.get("fast_screen_hard_net_floor_enabled", False)
                else None),
        },
        "boundary": "DISCOVERY_RESOURCE_GATE_ONLY",
        "promotion_authority": False,
    }


def select_slice(market_conn, config: dict, *, cutoff: datetime) -> dict:
    """Choose a causal calibration universe strictly before the OOS slice.

    Five calibration sessions are preferred, not required.  A newly started
    live feed can therefore produce an explicitly underpowered diagnostic as
    soon as two causal sessions exist.  Statistical promotion thresholds stay
    unchanged in ``evaluate_candidate``.
    """
    calibration_days = 5
    # A LIMIT after DISTINCT/ORDER BY does not bound a hypertable scan: without
    # a lower partition-key predicate Timescale must visit every compressed
    # chunk to prove which sessions are latest. Three calendar days per desired
    # KRX session is conservative across weekends/holidays while keeping the
    # scan finite. The returned sessions, not this calendar window, determine
    # statistical sufficiency.
    oldest_possible = cutoff - timedelta(
        days=max(30, config["evaluation_days"] * 3))
    requested_source = str(
        config.get("data_source") or LOCAL_EVENT_SOURCE).upper()
    if requested_source == "AUTO":
        raise RuntimeError(
            "AUTO is a resolver-only policy; runner requires one approved "
            "intraday event source")
    event_source = requested_source
    if event_source not in {LOCAL_EVENT_SOURCE, EXTERNAL_EVENT_SOURCE}:
        raise ValueError(f"unsupported intraday data_source={requested_source!r}")

    with market_conn.cursor() as cur:
        if event_source == EXTERNAL_EVENT_SOURCE:
            cur.execute(_EXTERNAL_SESSION_DATES_SQL,
                        (oldest_possible, cutoff,
                         config["evaluation_days"] + calibration_days))
        else:
            cur.execute(_SESSION_DATES_SQL,
                        (oldest_possible, cutoff, cutoff,
                         config["evaluation_days"] + calibration_days))
        days = sorted(row[0] for row in cur.fetchall())
    common = {
        "causal_sessions_available": len(days),
        "requested_evaluation_sessions": config["evaluation_days"],
        "universe_mode": (
            "ALL_COLLECTED_DYNAMIC_FIRST_OBSERVED"
            if event_source == EXTERNAL_EVENT_SOURCE else
            "ALL_CAUSALLY_COLLECTED"),
        "event_source": event_source,
        "arrival_clock_pit": event_source == LOCAL_EVENT_SOURCE,
        "historical_replay_only": event_source == EXTERNAL_EVENT_SOURCE,
        "membership_clock": (
            "first raw quote+trade observation; no pre-observation backfill"
            if event_source == EXTERNAL_EVENT_SOURCE else
            "causally available calibration sessions"),
    }
    if len(days) < 2:
        return {"status": "INSUFFICIENT_SESSIONS", "sessions": [],
                "instruments": [], "calibration_sessions": [],
                "statistical_readiness": "NEEDS_DATA", **common}

    # Retain at least one earlier session for a point-in-time universe.  With
    # enough history this is exactly five calibration + N requested OOS days;
    # with short live history it becomes one calibration + the remaining OOS
    # days instead of fabricating arrival timestamps for legacy backfills.
    evaluation_count = min(config["evaluation_days"], len(days) - 1)
    eval_days = days[-evaluation_count:]
    preceding = days[:-evaluation_count]
    calibration = preceding[-calibration_days:]
    with market_conn.cursor() as cur:
        if event_source == EXTERNAL_EVENT_SOURCE:
            # Include later listings/collection starts without fabricating their
            # earlier membership. Missing pre-first-observation days yield no
            # events, while the fixed union makes coverage and sharding auditable.
            # This first query is membership only.  Its evaluation-period
            # aggregates MUST NOT rank the discovery panel.
            cur.execute(_EXTERNAL_UNIVERSE_SQL,
                        ([*calibration, *eval_days],))
            universe_rows = cur.fetchall()
            instruments = [str(row[0]) for row in universe_rows]
            # Panel profiles are frozen strictly before OOS.  With the imported
            # 61-session store this is intentionally only the one preceding
            # session; missing profiles fall back to identity-hash coverage.
            cur.execute(_EXTERNAL_UNIVERSE_SQL, (calibration,))
            profile_rows = cur.fetchall()
        else:
            calibration_start, _ = _session_bounds(calibration[0])
            _, calibration_end = _session_bounds(calibration[-1])
            cur.execute(
                _LIQUID_UNIVERSE_SQL,
                (calibration_start, calibration_end + timedelta(hours=1),
                 cutoff,
                 calibration_start, calibration_end + timedelta(hours=1),
                 cutoff))
            universe_rows = cur.fetchall()
            instruments = [str(row[0]) for row in universe_rows]
            profile_rows = universe_rows
        instrument_profiles = {
            str(row[0]): {
                "quote_events": int(row[1] or 0),
                "spread_bps": (float(row[2]) if len(row) > 2
                               and row[2] is not None else None),
                "trade_intensity": (float(row[3]) if len(row) > 3
                                    and row[3] is not None else None),
                "depth_notional_l1": (float(row[4]) if len(row) > 4
                                      and row[4] is not None else None),
            }
            for row in profile_rows
        }
    readiness = ("FULL" if evaluation_count >= config["evaluation_days"]
                 else "SHORT_DIAGNOSTIC")
    return {"status": "PASS" if len(instruments) >= 2 else "INSUFFICIENT_INSTRUMENTS",
            "selection_rule": (
                "union of every instrument with raw external quote and trade "
                "counts in the frozen 61-session slice; each joins only from its "
                "first observed session; event-time replay only"
                if event_source == EXTERNAL_EVENT_SOURCE else
                "all instruments with valid causally available quotes and trades "
                "in up to five pre-evaluation sessions"),
            "calibration_sessions": [str(day) for day in calibration],
            "calibration_session_count": len(calibration),
            "evaluation_session_count": len(eval_days),
            "statistical_readiness": readiness,
            "sessions": eval_days, "instruments": instruments,
            "instrument_profiles": instrument_profiles,
            "instrument_profile_clock": "STRICTLY_PRE_EVALUATION_CALIBRATION_ONLY",
            "instrument_profile_sessions": [str(day) for day in calibration],
            **common}


def _external_source_content_identity(source_fingerprint, hash_contract,
                                      input_hash, *, context: str) -> dict:
    """Validate the builder's typed full-row source hash; legacy rows fail."""
    fingerprint_value = str(source_fingerprint or "")
    contract_value = str(hash_contract or "")
    input_value = str(input_hash or "")
    if (len(fingerprint_value) != 64
            or any(char not in "0123456789abcdef"
                   for char in fingerprint_value)):
        raise RuntimeError(
            "external source evidence lacks a valid full-row SHA256 "
            f"fingerprint: {context}")
    if contract_value != EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT:
        raise RuntimeError(
            "external source evidence uses an unknown content hash contract: "
            f"{context}={contract_value or 'MISSING'}")
    if input_value != fingerprint_value:
        raise RuntimeError(
            "external feature input_hash is not bound to source content: "
            f"{context}")
    return {
        "source_content_fingerprint": fingerprint_value,
        "source_content_hash_contract": contract_value,
        "input_hash": input_value,
    }


def _all_symbolic_candidates_cost_infeasible(
        calibration_reports: dict[str, dict]) -> bool:
    """Prove that no calibrated symbolic candidate can clear its cheapest entry.

    This is a resource decision over the calibration slice, not alpha evidence.
    A mixed population continues to replay so a cost-feasible linked formula is
    not discarded merely because the primary equation was infeasible.
    """

    reports = list((calibration_reports or {}).values())

    def infeasible(report: dict) -> bool:
        if str((report or {}).get(
                "coefficient_policy") or "").upper() != "STRUCTURE_ONLY":
            return False
        status = str((report or {}).get("status") or "").upper()
        return (status in {
            "NO_COST_FEASIBLE_ENTRY",
            "NON_POSITIVE_DIRECTIONAL_RELATION",
        } and int((report or {}).get("observations") or 0) > 0)

    return bool(reports) and all(infeasible(report) for report in reports)


def _annotate_calibration_only_failures(
        candidate_reports: dict[str, dict],
        calibration_reports: dict[str, dict]) -> None:
    """Keep measured calibration failure distinct from missing raw data.

    The candidate has no OOS observations because the resource gate deliberately
    skipped replay.  Its calibration relationship and cost-capacity gap are still
    valid adaptive search memory, but never promotion evidence.
    """
    for candidate_key, candidate_report in candidate_reports.items():
        candidate_calibration = calibration_reports.get(candidate_key) or {}
        calibration_status = str(
            candidate_calibration.get("status") or "").upper()
        failure_code = {
            "NO_COST_FEASIBLE_ENTRY": "CALIBRATION_COST_INFEASIBLE",
            "NON_POSITIVE_DIRECTIONAL_RELATION":
                "CALIBRATION_DIRECTION_NON_POSITIVE",
        }.get(calibration_status, "CALIBRATION_SYMBOLIC_INFEASIBLE")
        failed = list(candidate_report.get("failed_criteria") or [])
        if failure_code not in failed:
            failed.append(failure_code)
        candidate_report["failed_criteria"] = failed
        candidate_report["adaptive_failure_memory"] = {
            "classification": failure_code,
            "calibration_status": calibration_status,
            "evidence_scope": "CALIBRATION_ONLY_ADAPTIVE_SEARCH",
            "observations": candidate_calibration.get("observations"),
            "minimum_observed_entry_hurdle_bps": candidate_calibration.get(
                "minimum_observed_entry_hurdle_bps"),
            "maximum_calibrated_predicted_markout_bps":
                candidate_calibration.get(
                    "maximum_calibrated_predicted_markout_bps"),
            "evaluation_skipped": True,
            "evaluation_skip_reason":
                "ALL_SYMBOLIC_CANDIDATES_INFEASIBLE",
            "raw_data_absence_inferred": False,
            "adaptive_selection": True,
            "promotion_authority": False,
        }


def _external_raw_replay_row(day: date, instrument: str,
                             evidence: dict) -> dict:
    """Canonicalize hashes observed in the actual FDW replay statement."""
    quote = evidence.get("quotes") or {}
    trade = evidence.get("ticks") or {}
    n_quotes = int(quote.get("row_count") or 0)
    n_ticks = int(trade.get("row_count") or 0)

    def components(state: dict, count: int):
        if count == 0:
            return None, None
        return state.get("xor_seed_0"), state.get("sum_seed_1")

    quote_components = components(quote, n_quotes)
    trade_components = components(trade, n_ticks)
    _quote_fp, _trade_fp, source_fp = external_content_fingerprints(
        day, instrument, n_ticks, n_quotes,
        quote_components[0], quote_components[1],
        trade_components[0], trade_components[1])
    return {
        "session": day.isoformat(),
        "instrument": str(instrument).strip(),
        "quote_rows": n_quotes,
        "trade_rows": n_ticks,
        "source_content_fingerprint": source_fp,
    }


def _external_content_window(day: date) -> tuple[datetime, datetime]:
    """Fixed half-open raw identity window shared with the feature builder."""

    start, end = external_session_content_window(day)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _external_content_end(day: date) -> datetime:
    """Exclusive end of the immutable completed-second source contract."""

    return _external_content_window(day)[1]


def _replay_load_end(day: date, event_source: str,
                     sample_load_end: datetime) -> datetime:
    """Keep sample reach separate from the immutable raw identity window."""

    if event_source != EXTERNAL_EVENT_SOURCE:
        return sample_load_end
    start, content_end = _external_content_window(day)
    if not start < sample_load_end <= content_end:
        raise RuntimeError(
            "external sample reach falls outside the frozen content window")
    return content_end


def _external_replay_cell_keys(selected: dict) -> list[tuple[str, str]]:
    sessions = [*(selected.get("calibration_sessions") or []),
                *(selected.get("sessions") or [])]
    normalized_sessions = [_session_day(value).isoformat()
                           for value in sessions]
    instruments = [str(value).strip()
                   for value in (selected.get("instruments") or [])]
    if (not normalized_sessions or not instruments
            or len(set(normalized_sessions)) != len(normalized_sessions)
            or len(set(instruments)) != len(instruments)
            or any(not value for value in instruments)):
        raise RuntimeError(
            "external raw replay requires unique sessions and instruments")
    return [(session, instrument) for session in normalized_sessions
            for instrument in instruments]


def _external_replay_manifest(rows: list[dict], selected: dict) -> dict:
    """Reconcile the exact session/instrument cells before hashing content."""

    canonical = []
    for row in rows:
        quote_rows = row.get("quote_rows")
        trade_rows = row.get("trade_rows")
        content_fingerprint = str(
            row.get("source_content_fingerprint") or "")
        if (isinstance(quote_rows, bool) or not isinstance(quote_rows, int)
                or quote_rows < 0 or isinstance(trade_rows, bool)
                or not isinstance(trade_rows, int) or trade_rows < 0
                or len(content_fingerprint) != 64
                or any(char not in "0123456789abcdef"
                       for char in content_fingerprint)):
            raise RuntimeError(
                "external raw replay row lacks valid counts/content hash")
        canonical.append({
            "session": str(row.get("session") or ""),
            "instrument": str(row.get("instrument") or "").strip(),
            "quote_rows": quote_rows,
            "trade_rows": trade_rows,
            "source_content_fingerprint": content_fingerprint,
        })
    canonical.sort(key=lambda row: (row["session"], row["instrument"]))
    expected_keys = sorted(_external_replay_cell_keys(selected))
    observed_keys = [(row["session"], row["instrument"])
                     for row in canonical]
    if observed_keys != expected_keys:
        raise RuntimeError(
            "external raw replay cell set differs from frozen "
            "calibration+evaluation panel")
    identity = {
        "version": EXTERNAL_RAW_REPLAY_CONTENT_VERSION,
        "content_window_contract": EXTERNAL_CONTENT_WINDOW_CONTRACT,
        "content_window": {
            "timezone": "Asia/Seoul",
            "start": "09:00:00",
            "end_exclusive": "15:30:00",
            "interval": "HALF_OPEN",
        },
        "rows": canonical,
    }
    return {
        "rows": canonical,
        "manifest_rows": len(canonical),
        "fingerprint": stable_fingerprint(identity),
        "identity": identity,
    }


def _lineage(market_conn, selected: dict, cutoff: datetime) -> list[dict]:
    if not selected["sessions"] or not selected["instruments"]:
        return []
    external = selected.get("event_source") == EXTERNAL_EVENT_SOURCE
    calibration = selected.get("calibration_sessions") or []
    with market_conn.cursor() as cur:
        if external:
            sessions = [*calibration, *selected["sessions"]]
            params = (selected["instruments"], sessions)
            cur.execute(_EXTERNAL_LINEAGE_SQL, params + params)
            rows = cur.fetchall()
            cur.execute(_EXTERNAL_SLICE_EVIDENCE_SQL, params)
            raw_evidence = cur.fetchall()
        else:
            first = calibration[0] if calibration else selected["sessions"][0]
            start, _ = _session_bounds(_session_day(first))
            _, end = _session_bounds(selected["sessions"][-1])
            params = (selected["instruments"], start,
                      end + timedelta(hours=1), cutoff)
            cur.execute(_LINEAGE_SQL, params + params)
            rows = cur.fetchall()
            raw_evidence = []
    if external:
        observed_evidence = {}
        expected_cell_keys = set(_external_replay_cell_keys(selected))
        for row in raw_evidence:
            session, instrument = str(row[0]), str(row[1]).strip()
            key = (session, instrument)
            if key not in expected_cell_keys or key in observed_evidence:
                raise RuntimeError(
                    "external feature ledger has duplicate or unexpected "
                    f"session/instrument evidence: {session}/{instrument}")
            identity = _external_source_content_identity(
                row[4], row[5], row[6],
                context=f"{session}/{instrument}")
            observed_evidence[key] = {
                "session": session,
                "instrument": instrument,
                "quote_rows": int(row[2] or 0),
                "trade_rows": int(row[3] or 0),
                **identity,
                "input_watermark": row[7].isoformat() if row[7] else None,
            }
        # The selected universe is a union across sessions.  A stock may first
        # appear on day two, leaving no daily sidecar on day one.  Materialize
        # that missing cell as an expected empty raw multiset so the direct
        # replay either proves zero rows or fails; do not let an omitted row and
        # an explicit zero row produce different immutable identities.
        evidence = []
        for session, instrument in _external_replay_cell_keys(selected):
            day = date.fromisoformat(session)
            row = observed_evidence.get((session, instrument))
            if row is None:
                row = _external_raw_replay_row(day, instrument, {})
                row["source_content_hash_contract"] = \
                    EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT
                row["input_hash"] = row["source_content_fingerprint"]
                row["input_watermark"] = None
            evidence.append(row)
        replay_manifest = _external_replay_manifest(evidence, selected)
        content_fingerprint = stable_fingerprint({
            "version": "external-daily-source-content-manifest-v3",
            "content_window_contract": EXTERNAL_CONTENT_WINDOW_CONTRACT,
            "rows": evidence,
        })
        content_metadata = {
            "content_hash_contract": (
                "external-daily-source-content-manifest-v3"),
            "source_content_hash_contract": (
                EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT),
            "source_content_window_contract": (
                EXTERNAL_CONTENT_WINDOW_CONTRACT),
            "content_fingerprint": content_fingerprint,
            "content_manifest_rows": len(evidence),
            "content_quote_rows": sum(row["quote_rows"] for row in evidence),
            "content_trade_rows": sum(row["trade_rows"] for row in evidence),
            "consumed_replay_content_contract": (
                EXTERNAL_RAW_REPLAY_CONTENT_VERSION),
            "consumed_replay_content_fingerprint": replay_manifest[
                "fingerprint"],
            "consumed_replay_content_manifest_rows": replay_manifest[
                "manifest_rows"],
        }
        return [{
            "source": row[0], "rows": int(row[1]),
            "min_event_time": row[2].isoformat() if row[2] else None,
            "max_event_time": row[3].isoformat() if row[3] else None,
            "max_source_watermark": row[4].isoformat() if row[4] else None,
            "max_available_at": row[5].isoformat() if row[5] else None,
            **content_metadata,
        } for row in rows]
    return [{
        "source": row[0], "rows": int(row[1]),
        "min_event_time": row[2].isoformat() if row[2] else None,
        "max_event_time": row[3].isoformat() if row[3] else None,
        "max_observed_at": row[4].isoformat() if row[4] else None,
        "max_available_at": row[5].isoformat() if row[5] else None,
        "content_hash_contract": "postgres-jsonb-multiset-v1",
        "content_fingerprint": stable_fingerprint({
            "source": str(row[0]), "rows": int(row[1]),
            "xor_seed_0": str(row[6]), "xor_seed_1": str(row[7]),
            "sum_seed_2": str(row[8]),
        }),
    } for row in rows]


def _input_hash_for_versions(
        hypothesis_id: str, config: dict, *, runner_version: str,
        evaluator_version: str, cost_model_version: str) -> str:
    # Wall-clock invocation time is audit metadata, not input identity.  The
    # selected sessions/instruments and source lineage below do change whenever
    # data inside the evaluated slice changes, so retries are idempotent without
    # hiding late-arriving observations.
    identity = {key: value for key, value in config.items()
                if key not in {"cutoff", "instrument_shard_size",
                               "legacy_instrument_count_ignored"}}
    payload = json.dumps({"hypothesis_id": hypothesis_id,
                          "runner_version": str(runner_version),
                          "evaluator_version": str(evaluator_version),
                          "cost_model_version": str(cost_model_version),
                          **identity},
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def _input_hash(hypothesis_id: str, config: dict) -> str:
    return _input_hash_for_versions(
        hypothesis_id, config, runner_version=RUNNER_VERSION,
        evaluator_version=_evaluator_version(config),
        cost_model_version=COST_MODEL_VERSION)


def _qa_reproduction_runtime_manifest(*, hypothesis_id: str,
                                      config: dict) -> dict:
    """Freeze the complete evaluator input and executable source identity.

    Candidate-lineage fingerprints intentionally describe a formula/model
    family and therefore do not bind every replay control (for example the
    sampling interval or entry threshold).  Independent QA needs a stronger
    identity: the exact JSON configuration plus the source set that interpreted
    it.  This artifact lives in the append-only report manifest and is the only
    configuration the reproduction worker may execute.
    """

    frozen_config = json.loads(json.dumps(
        config, sort_keys=True, separators=(",", ":"), default=str))
    source_manifest = _qa_reproduction_source_manifest()
    teacher = _teacher_runtime_identity(frozen_config)
    identity = {
        "version": QA_REPRODUCTION_RUNTIME_VERSION,
        "frozen_config": frozen_config,
        "frozen_config_fingerprint": stable_fingerprint(frozen_config),
        "experiment_input_hash": _input_hash(hypothesis_id, frozen_config),
        "code_version": RUNNER_VERSION,
        "forward_runner_version": FORWARD_RUNNER_VERSION,
        "forward_gate_version": FORWARD_GATE_VERSION,
        "raw_digest_version": FORWARD_RAW_DIGEST_VERSION,
        "evaluator_version": _evaluator_version(config),
        "cost_model_version": COST_MODEL_VERSION,
        "teacher_version": teacher["version"],
        "supervised_label_version": SUPERVISED_LABEL_VERSION,
        "multiple_testing_version": MULTIPLE_TESTING_VERSION,
        "stock_universe_version": STOCK_UNIVERSE_VERSION,
        "report_manifest_version": REPORT_MANIFEST_VERSION,
        "source_manifest": source_manifest,
    }
    return {
        **identity,
        "runtime_manifest_fingerprint": stable_fingerprint(identity),
    }


def prepare(hyp: dict, *, market_conn, meta_conn=None,
            cutoff: datetime | None = None) -> dict:
    """Freeze the causal slice before preregistration or trial allocation."""
    if meta_conn is None:
        raise RuntimeError(
            "intraday preparation requires reference-plane stock validation")
    edge = hyp.get("expected_edge") or {}
    config, spec = validate_current_explicit_v2_execution_edge(edge)
    _assert_resolved_data_contract(config)
    frozen_cutoff = cutoff or datetime.now(timezone.utc)
    selected = select_slice(market_conn, config, cutoff=frozen_cutoff)
    selected = _stock_only_slice(meta_conn, market_conn, selected)
    _assert_stock_selection_evidence(selected)
    timestamp_policy = (
        COMPLETED_SECOND_POLICY
        if selected.get("event_source") == EXTERNAL_EVENT_SOURCE else
        STRICT_TIMESTAMP_POLICY)
    config["timestamp_policy"] = timestamp_policy
    _assert_resolved_data_contract(config, selected=selected)
    if (selected.get("status") == "PASS"
            and timestamp_policy == COMPLETED_SECOND_POLICY
            and max(spec.horizons_seconds) >
            EXTERNAL_REPLAY_MAX_HORIZON_SECONDS):
        selected = {
            **selected,
            "status": "HORIZON_EXCEEDS_FROZEN_SOURCE_WINDOW",
            "statistical_readiness": "NEEDS_LONGER_VERIFIED_EVENT_WINDOW",
            "maximum_supported_horizon_seconds":
                EXTERNAL_REPLAY_MAX_HORIZON_SECONDS,
            "requested_maximum_horizon_seconds": max(spec.horizons_seconds),
            "horizon_blocker": (
                "completed-second source identity ends at 15:30 KST while "
                "decision sampling ends at 15:20 KST"),
        }
    if (selected.get("status") == "PASS" and
            timestamp_policy == COMPLETED_SECOND_POLICY and
            config["population_execution_model"] != "TAKER"):
        selected = {
            **selected,
            "status": "PASSIVE_NOT_IDENTIFIABLE_ON_COMPLETED_SECOND_SOURCE",
            "statistical_readiness": "NEEDS_SEQUENCED_ARRIVAL_DATA",
            "execution_eligibility": "TAKER_ONLY",
            "execution_blocker": (
                "intra-second quote/trade order and MBO queue are unavailable"),
        }
    return {"config": config, "spec": spec, "cutoff": frozen_cutoff,
            "selected": selected}


def record_data_feasibility(meta_conn, hypothesis_id: str,
                            prepared: dict) -> dict:
    """Persist a coverage probe without creating an experiment/trial row."""
    selected = prepared["selected"]
    _assert_stock_selection_evidence(selected)
    if selected.get("status") == "PASS":
        sessions = [_session_day(value) for value in selected["sessions"]]
        selected = {
            **selected,
            "record_stock_scope": assert_stock_instrument_ids(
                meta_conn, selected["reference_instrument_ids"],
                first_session=min(sessions), last_session=max(sessions)),
        }
    status = ("PASS" if selected.get("status") == "PASS" and
              selected.get("statistical_readiness") == "FULL"
              else "NEEDS_DATA")
    details = {
        "runner_version": RUNNER_VERSION,
        "research_lane": "INTRADAY_EVENT",
        "slice": {**selected,
                  "sessions": [str(day) for day in selected.get("sessions", [])]},
    }
    blob = json.dumps(details, sort_keys=True, separators=(",", ":"),
                      default=str)
    coverage_fingerprint = hashlib.sha256(blob.encode()).hexdigest()
    cutoff = prepared["cutoff"]
    with meta_conn.cursor() as cur:
        cur.execute("""
            insert into quant.data_feasibility_checks
              (hypothesis_id, research_lane, cutoff, coverage_fingerprint,
               status, details, first_checked_at, last_checked_at)
            values (%s,'INTRADAY_EVENT',%s,%s,%s,%s::jsonb,now(),now())
            on conflict (hypothesis_id, coverage_fingerprint) do update set
              cutoff=excluded.cutoff,
              status=excluded.status,
              details=excluded.details,
              last_checked_at=now()
            returning check_id::text
        """, (hypothesis_id, cutoff, coverage_fingerprint, status, blob))
        check_id = cur.fetchone()[0]
    meta_conn.commit()
    return {"check_id": check_id, "status": status,
            "coverage_fingerprint": coverage_fingerprint,
            "details": details}


def _json_object(value, field: str) -> dict:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"intraday dataset manifest has invalid {field}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(
            f"intraday dataset manifest lacks object {field}")
    return value


def _assert_dataset_manifest_projection(row, contract: dict) -> str:
    """Revalidate mutable catalog fields at experiment registration time."""

    if not row or len(row) != 6:
        raise RuntimeError(
            f"dataset manifest missing: {contract.get('dataset')}")
    dataset_id, source_versions, pit, quality, object_path, schema = row
    source_versions = _json_object(source_versions, "source_versions")
    pit = _json_object(pit, "point_in_time_policy")
    quality = _json_object(quality, "quality_summary")
    schema = _json_object(schema, "schema_definition")
    external = contract["event_source"] == EXTERNAL_EVENT_SOURCE
    expected_path = ("postgresql+fdw://ext_src/{quotes,ticks}" if external
                     else "timescaledb://market/{market_quotes,market_ticks}")
    expected_pit = ({
        "knowledge_clock": "event_time_only_no_receipt_clock",
        "feature_cutoff": "completed_source_second<=decision_time",
        "label_cutoff": "effective_entry_time+horizon",
        "instrument_isolation": True,
        "evidence_scope": "HISTORICAL_SEARCH_ONLY",
        "content_window": "[09:00:00,15:30:00) Asia/Seoul",
        "maximum_horizon_seconds": 600,
    } if external else {
        "knowledge_clock": "available_at=max(received_at,observed_at)",
        "feature_cutoff": (
            "event_time<=decision_time and available_at<=decision_time"),
        "label_cutoff": "entry_time+horizon",
        "instrument_isolation": True,
    })
    expected_quality = ({
        "status": (
            "HISTORICAL_COMPLETED_SECOND_REQUIRES_PER_EXPERIMENT_AUDIT"),
        "timestamp_resolution": "SECOND",
        "intra_second_order": "UNAVAILABLE",
        "execution": "TAKER_ONLY",
    } if external else {
        "status": "LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT",
        "missing_received_at": "reject",
    })
    required_schema = ({
        "market_quotes": {
            "physical_table": "ext_src.quotes",
            "required": {"ts", "symbol", "bid1", "ask1", "bid_vol1",
                         "ask_vol1", "bid10", "ask10", "bid_vol10",
                         "ask_vol10"},
        },
        "market_ticks": {
            "physical_table": "ext_src.ticks",
            "required": {"ts", "symbol", "price", "volume", "ofi_contrib"},
        },
    } if external else {
        "market_quotes": {
            "required": {"event_time", "received_at", "observed_at",
                         "instrument_id", "bid_prices", "bid_sizes",
                         "ask_prices", "ask_sizes", "source_event_id"},
        },
        "market_ticks": {
            "required": {"event_time", "received_at", "observed_at",
                         "instrument_id", "price", "quantity", "side",
                         "source_event_id"},
        },
    })
    mismatches = []
    if source_versions != contract["source_versions"]:
        mismatches.append("source_versions")
    if str(object_path or "") != expected_path:
        mismatches.append("object_path")
    for key, value in expected_pit.items():
        if pit.get(key) != value:
            mismatches.append(f"point_in_time_policy.{key}")
    for key, value in expected_quality.items():
        if quality.get(key) != value:
            mismatches.append(f"quality_summary.{key}")
    for table, expected in required_schema.items():
        actual = schema.get(table) or {}
        if not isinstance(actual, dict):
            mismatches.append(f"schema_definition.{table}")
            continue
        if expected.get("physical_table") is not None and actual.get(
                "physical_table") != expected["physical_table"]:
            mismatches.append(f"schema_definition.{table}.physical_table")
        if not expected["required"].issubset(set(actual.get("required") or [])):
            mismatches.append(f"schema_definition.{table}.required")
    if mismatches:
        raise RuntimeError(
            "registered intraday dataset/source/clock manifest drift: "
            + ",".join(sorted(set(mismatches))))
    return str(dataset_id)


def _register(meta_conn, hypothesis_id: str, config: dict) -> tuple[str, str, bool]:
    digest = _input_hash(hypothesis_id, config)
    contract = _assert_resolved_data_contract(config)
    dataset_name, separator, dataset_version = str(
        contract.get("dataset") or "").rpartition("/")
    if separator != "/" or not dataset_name or not dataset_version:
        raise RuntimeError("resolved intraday dataset identity is malformed")
    with meta_conn.cursor() as cur:
        cur.execute("""
            select dataset_id::text,
                   coalesce(source_versions, '{}'::jsonb),
                   coalesce(point_in_time_policy, '{}'::jsonb),
                   coalesce(quality_summary, '{}'::jsonb),
                   object_path,
                   coalesce(schema_definition, '{}'::jsonb)
              from quant.dataset_manifests
             where name=%s and version=%s
        """, (dataset_name, dataset_version))
        row = cur.fetchone()
        dataset_id = _assert_dataset_manifest_projection(row, contract)
        cur.execute("""
            insert into quant.experiments
              (hypothesis_id, dataset_id, code_version, config, seed,
               split_policy, cost_model_version, status, input_hash, trace_id,
               started_at)
            values (%s,%s,%s,%s::jsonb,0,%s::jsonb,%s,'RUNNING',%s,
                    gen_random_uuid(),now())
            on conflict (input_hash) do nothing
            returning experiment_id::text
        """, (hypothesis_id, dataset_id, RUNNER_VERSION,
              json.dumps(config, default=str),
              json.dumps({"unit": "KRX_SESSION", "purge": "horizon+latency",
                          "selection": config["slice"].get("selection_rule",
                                                            config["slice"]["status"])}),
              COST_MODEL_VERSION, digest))
        inserted = cur.fetchone()
        if inserted:
            experiment_id, duplicate = inserted[0], False
        else:
            cur.execute(
                "select experiment_id::text, status from quant.experiments "
                "where input_hash=%s for update", (digest,))
            experiment_id, status = cur.fetchone()
            if status == "FAILED":
                # Exactly one retrying worker can reclaim a failed immutable
                # input.  Existing metrics are deterministic upserts.
                cur.execute(
                    "update quant.experiments set status='RUNNING', "
                    "started_at=now(), ended_at=null where experiment_id=%s "
                    "and status='FAILED' returning experiment_id", (experiment_id,))
                duplicate = cur.fetchone() is None
            elif status == "COMPLETED":
                duplicate = True
            else:
                raise RuntimeError(
                    f"intraday experiment input already {status}: {experiment_id}")
    meta_conn.commit()
    return experiment_id, dataset_id, duplicate


def _reference_id_map(selected: dict) -> dict[str, str]:
    keys = [str(value) for value in selected.get("instruments") or []]
    ids = [str(value) for value in
           selected.get("reference_instrument_ids") or []]
    if len(keys) != len(ids) or not keys:
        raise RuntimeError(
            "stock-only slice lacks an exact reference instrument identity map")
    mapping = dict(zip(keys, ids))
    if len(mapping) != len(keys):
        raise RuntimeError("stock-only slice contains duplicate replay keys")
    return mapping


def _economic_family_id(plan: dict) -> str:
    """Mechanism family identity; numeric thresholds/AST shape are excluded."""
    family = {key: plan.get(key) for key in (
        "event", "context", "qualities", "direction", "output", "execution")}
    digest = stable_fingerprint({
        "version": "intraday-economic-family-v1",
        "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "semantic_family": family,
    })
    return f"intraday-economic-family-v1:{digest[:32]}"


def _candidate_specs(config: dict, row: dict) -> tuple[dict, dict, dict]:
    expression = row["intraday_signal_expr"]
    horizon = int(row["horizon_seconds"])
    execution = str(row["execution"]).upper()
    timestamp_policy = config.get(
        "timestamp_policy", STRICT_TIMESTAMP_POLICY)
    completed_second = timestamp_policy == COMPLETED_SECOND_POLICY
    teacher = _teacher_runtime_identity(config)
    feature_spec = {
        "version": "intraday-causal-feature-spec-v3",
        "ast_fields": sorted(fields_of(expression)),
        "ast_clock_windows_seconds": sorted(clocks_of(expression)),
        "teacher_version": teacher["version"],
        "teacher_feature_spec_hash": teacher["feature_spec_hash"],
        "teacher_features": list(teacher["features"]),
        "timestamp_policy": timestamp_policy,
        "clock_aggregation_version": (
            "completed-second-state-median-taker-envelope-v1"
            if completed_second else None),
        "state_feature_aggregation": (
            "PER_RAW_STATE_SCALAR_THEN_WITHIN_SECOND_MEDIAN"
            if completed_second else "LATEST_UNAMBIGUOUS_VISIBLE_STATE"),
        "ordered_quote_flow_policy": (
            "UNAVAILABLE_WITHOUT_SEQUENCE"
            if completed_second else "FAIL_CLOSED_MISSING_ON_AMBIGUITY"),
    }
    if (_feature_window_contract(config) ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT):
        feature_spec = {
            "version": "intraday-causal-feature-spec-v4",
            "feature_window_contract_version":
                EXPLICIT_FEATURE_WINDOW_CONTRACT,
            "ast_fields": sorted(fields_of(expression)),
            "ast_field_window_bindings": [
                {"field": field, "seconds": seconds}
                for field, seconds in field_window_bindings_of(expression)
            ],
            "ast_primitive_windows_seconds": sorted(
                primitive_windows_of(expression)),
            "ast_temporal_windows_seconds": sorted(
                temporal_windows_of(expression)),
            "feature_cube": teacher["feature_cube_spec"],
            "feature_cube_spec_hash": teacher["feature_cube_spec_hash"],
            "sample_interval_seconds": config["sample_interval_seconds"],
            "teacher_base_feature_lookback_seconds":
                config["feature_lookback_seconds"],
            "teacher_version": teacher["version"],
            "teacher_feature_spec_hash": teacher["feature_spec_hash"],
            "teacher_features": list(teacher["features"]),
            "timestamp_policy": timestamp_policy,
            "clock_aggregation_version": (
                "completed-second-state-median-taker-envelope-v1"
                if completed_second else None),
            "state_feature_aggregation": (
                "PER_RAW_STATE_SCALAR_THEN_WITHIN_SECOND_MEDIAN"
                if completed_second else "LATEST_UNAMBIGUOUS_VISIBLE_STATE"),
            "ordered_quote_flow_policy": (
                "UNAVAILABLE_WITHOUT_SEQUENCE"
                if completed_second else "FAIL_CLOSED_MISSING_ON_AMBIGUITY"),
        }
    label_spec = {
        "version": SUPERVISED_LABEL_VERSION,
        "horizon_seconds": horizon,
        "execution": execution,
        "position_mode": config["position_mode"],
        "order_latency_ms": config["order_latency_ms"],
        "effective_latency_policy": (
            "COARSE_CLOCK_CEIL_TO_SECOND"
            if completed_second else "EXACT_REQUESTED_LATENCY"),
        "taker_price_contract": (
            "CONDITIONAL_ONE_SHARE_MAX_ASK_MIN_BID_ENVELOPE"
            if completed_second else "LATEST_VISIBLE_QUOTE"),
        "execution_capacity_supported": not completed_second,
        "fee_bps_per_side": config["fee_bps_per_side"],
        "maker_fee_bps_per_side": config["maker_fee_bps_per_side"],
        "passive_nonfill_target": "ZERO_NET_PER_OPPORTUNITY",
        "cost_model_version": COST_MODEL_VERSION,
    }
    model_spec = {
        "version": "intraday-symbolic-plus-frozen-teacher-v1",
        "coefficient_policy": row["coefficient_policy"],
        "entry_policy": row["entry_policy"],
        "minimum_predicted_edge_bps": config["minimum_predicted_edge_bps"],
        "teacher_version": teacher["version"],
        "oos_fit_forbidden": True,
        "teacher_promotion_authority": False,
    }
    if (_feature_window_contract(config) ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT):
        model_spec.update({
            "feature_window_contract_version":
                EXPLICIT_FEATURE_WINDOW_CONTRACT,
            "teacher_feature_spec_hash": teacher["feature_spec_hash"],
            "feature_cube_spec_hash": teacher["feature_cube_spec_hash"],
        })
    return feature_spec, label_spec, model_spec


def _assert_same_hypothesis_retry_identity(existing, *, config: dict,
                                           primary_row: dict,
                                           feature_spec: dict,
                                           label_spec: dict,
                                           model_spec: dict) -> None:
    """Fail closed if an idempotent hypothesis retry changed frozen identity."""
    expected = {
        "candidate AST": stable_fingerprint(
            primary_row["intraday_signal_expr"]),
        "semantic plan": stable_fingerprint(primary_row["semantic_plan"]),
        "baseline AST": (stable_fingerprint(
            primary_row["source_baseline_expr"])
            if primary_row.get("source_baseline_expr") is not None else None),
        "feature spec": stable_fingerprint(feature_spec),
        "label spec": stable_fingerprint(label_spec),
        "model spec": stable_fingerprint(model_spec),
        "economic family": _economic_family_id(
            primary_row["semantic_plan"]),
        "evaluator version": _evaluator_version(config),
        "cost model version": COST_MODEL_VERSION,
    }
    actual = {
        "candidate AST": existing.candidate_ast_fingerprint,
        "semantic plan": existing.semantic_plan_fingerprint,
        "baseline AST": existing.baseline_ast_fingerprint,
        "feature spec": existing.feature_spec_fingerprint,
        "label spec": existing.label_spec_fingerprint,
        "model spec": existing.model_spec_fingerprint,
        "economic family": existing.economic_family_id,
        "evaluator version": existing.evaluator_version,
        "cost model version": existing.cost_model_version,
    }
    changed = [name for name in expected if expected[name] != actual[name]]
    if changed:
        raise RuntimeError(
            "same-hypothesis intraday retry changed immutable identity: "
            + ", ".join(changed))


def _candidate_source_contract(row: dict, *, config: dict,
                               feature_spec: dict,
                               label_spec: dict, model_spec: dict) -> dict:
    """Return every component that defines one durable evaluation identity."""
    return {
        "candidate_ast": row["intraday_signal_expr"],
        "semantic_plan": row["semantic_plan"],
        "baseline_ast": row.get("source_baseline_expr"),
        "feature_spec": feature_spec,
        "label_spec": label_spec,
        "model_spec": model_spec,
        "evaluator_version": _evaluator_version(config),
        "cost_model_version": COST_MODEL_VERSION,
    }


def _find_same_hypothesis_ast_lineage(meta_conn, *, hypothesis_id: str,
                                      candidate_ast: dict):
    """Return the hypothesis' existing exact-AST node before global ancestry.

    A global ``latest AST`` lookup is not an idempotency lookup: another
    hypothesis may have registered the same expression after this hypothesis.
    Querying the owning hypothesis first prevents that interleaving from either
    breaking an unchanged retry or laundering a changed immutable identity into
    a second node.
    """
    ast_fp = stable_fingerprint(candidate_ast)
    with meta_conn.cursor() as cur:
        cur.execute("""
            select candidate_lineage_id::text
              from quant.intraday_candidate_lineages
             where hypothesis_id=%s::uuid
               and candidate_ast_fingerprint=%s
             order by created_at, candidate_lineage_id
             limit 2
        """, (str(hypothesis_id), ast_fp))
        rows = cur.fetchall()
    if len(rows) > 1:
        raise RuntimeError(
            "same-hypothesis intraday AST has multiple immutable lineage nodes")
    if not rows:
        return None
    return load_candidate_lineage(meta_conn, str(rows[0][0]))


def _register_trial_lineages(meta_conn, *, hypothesis_id: str,
                             config: dict) -> tuple[object, dict[str, object]]:
    """Register every viewed equation before the first candidate sees data."""
    primary_row = {
        "intraday_signal_expr": config["intraday_signal_expr"],
        "semantic_plan": config["semantic_plan"],
        "horizon_seconds": config["horizon_seconds"],
        "execution": config["execution"],
        "entry_policy": config["entry_policy"],
        "coefficient_policy": config["coefficient_policy"],
        "source_baseline_expr": config.get("source_baseline_expr"),
        "parent_ast_fingerprint": config.get("parent_ast_fingerprint"),
        "parent_candidate_identity_fingerprint": config.get(
            "parent_candidate_identity_fingerprint"),
    }
    feature_spec, label_spec, model_spec = _candidate_specs(config, primary_row)
    pending = list(config.get("screening_population") or [])
    lineages: dict[str, object] = {}
    population_by_fp = {
        str(row.get("ast_fingerprint") or ""): row for row in pending}

    def register_population_parent(parent_fp: str,
                                   trail: frozenset[str] = frozenset()):
        """Register a declared parent chain before its child, fail closed."""
        if parent_fp in lineages:
            return lineages[parent_fp]
        if parent_fp in trail:
            raise RuntimeError("cyclic explicit evolutionary parent chain")
        row = population_by_fp.get(parent_fp)
        if row is None:
            raise RuntimeError(
                "declared evolutionary parent is absent from frozen cohort")
        row_parent_fp = str(row.get("parent_ast_fingerprint") or "")
        declared_identity = str(row.get(
            "parent_candidate_identity_fingerprint") or "").strip()
        parent = None
        parent_reason = "EXACT_CANDIDATE_IDENTITY_REUSE"
        if row_parent_fp:
            parent = register_population_parent(
                row_parent_fp, trail | frozenset({parent_fp}))
            parent_reason = "EXPLICIT_POPULATION_PARENT"
            if (declared_identity and
                    parent.candidate_identity_fingerprint !=
                    declared_identity):
                raise RuntimeError(
                    "declared evolutionary parent AST and identity disagree")
        elif declared_identity:
            parent = find_latest_candidate_lineage(
                meta_conn, candidate_identity=declared_identity)
            if parent is None:
                raise RuntimeError(
                    "declared evolutionary parent identity is not durable")
            parent_reason = "EXPLICIT_DURABLE_PARENT_IDENTITY"

        row_feature, row_label, row_model = _candidate_specs(config, row)
        if parent is None:
            parent = find_latest_candidate_lineage(
                meta_conn, source_contract=_candidate_source_contract(
                    row, config=config, feature_spec=row_feature,
                    label_spec=row_label,
                    model_spec=row_model))
        lineage = register_candidate_lineage(
            meta_conn, hypothesis_id=hypothesis_id,
            candidate_ast=row["intraday_signal_expr"],
            semantic_plan=row["semantic_plan"],
            baseline_ast=row.get("source_baseline_expr"),
            feature_spec=row_feature, label_spec=row_label,
            model_spec=row_model,
            economic_family_id=_economic_family_id(row["semantic_plan"]),
            evaluator_version=_evaluator_version(config),
            cost_model_version=COST_MODEL_VERSION,
            created_by="svc_quant/intraday-experiment-runner",
            parent=parent,
            metadata={
                "candidate_role": row.get("candidate_role") or
                                  "LINEAGE_PARENT",
                "declared_parent_ast_fingerprint": row_parent_fp or None,
                "parent_resolution": parent_reason if parent else None,
                "pre_registered_as_explicit_parent": True,
                "screening_only": True,
                "runner_version": RUNNER_VERSION,
            },
        )
        lineages[parent_fp] = lineage
        if row in pending:
            pending.remove(row)
        return lineage

    retry_primary = _find_same_hypothesis_ast_lineage(
        meta_conn, hypothesis_id=hypothesis_id,
        candidate_ast=primary_row["intraday_signal_expr"])
    if retry_primary is not None:
        _assert_same_hypothesis_retry_identity(
            retry_primary, config=config, primary_row=primary_row,
            feature_spec=feature_spec, label_spec=label_spec,
            model_spec=model_spec)
    inherited_parent = None
    inheritance_reason = "EXACT_CANDIDATE_IDENTITY_REUSE"
    primary_parent_fp = str(primary_row.get("parent_ast_fingerprint") or "")
    if retry_primary is not None and primary_parent_fp:
        parent_row = population_by_fp.get(primary_parent_fp)
        if (parent_row is None or retry_primary.parent_lineage_id is None):
            raise RuntimeError(
                "retried evolved primary lacks its immutable explicit parent")
        durable_parent = load_candidate_lineage(
            meta_conn, retry_primary.parent_lineage_id)
        if durable_parent.candidate_ast_fingerprint != stable_fingerprint(
                parent_row["intraday_signal_expr"]):
            raise RuntimeError(
                "retried evolved primary points to a different parent AST")
        lineages[primary_parent_fp] = durable_parent
        pending.remove(parent_row)
    if retry_primary is None:
        declared_parent_identity = str(primary_row.get(
            "parent_candidate_identity_fingerprint") or "").strip()
        if primary_parent_fp:
            inherited_parent = register_population_parent(primary_parent_fp)
            if (declared_parent_identity and
                    inherited_parent.candidate_identity_fingerprint !=
                    declared_parent_identity):
                raise RuntimeError(
                    "primary parent AST and candidate identity disagree")
            inheritance_reason = "EXPLICIT_POPULATION_PARENT"
        elif declared_parent_identity:
            inherited_parent = find_latest_candidate_lineage(
                meta_conn, candidate_identity=declared_parent_identity)
            if inherited_parent is None:
                raise RuntimeError(
                    "declared evolutionary parent identity is not durable")
            inheritance_reason = "EXPLICIT_EVOLUTION_PARENT_IDENTITY"
        else:
            inherited_parent = find_latest_candidate_lineage(
                meta_conn, source_contract=_candidate_source_contract(
                    primary_row, config=config, feature_spec=feature_spec,
                    label_spec=label_spec, model_spec=model_spec))
        if (inheritance_reason == "EXACT_CANDIDATE_IDENTITY_REUSE"
                and inherited_parent is not None and
                inherited_parent.hypothesis_id == str(hypothesis_id)):
            # A concurrent insert between the dedicated lookup and this global
            # fallback is still a same-hypothesis retry, never a new parent.
            _assert_same_hypothesis_retry_identity(
                inherited_parent, config=config, primary_row=primary_row,
                feature_spec=feature_spec, label_spec=label_spec,
                model_spec=model_spec)
            retry_primary = inherited_parent
            inherited_parent = None
    primary = retry_primary or register_candidate_lineage(
        meta_conn,
        hypothesis_id=hypothesis_id,
        candidate_ast=primary_row["intraday_signal_expr"],
        semantic_plan=primary_row["semantic_plan"],
        baseline_ast=primary_row.get("source_baseline_expr"),
        feature_spec=feature_spec,
        label_spec=label_spec,
        model_spec=model_spec,
        economic_family_id=_economic_family_id(primary_row["semantic_plan"]),
        evaluator_version=_evaluator_version(config),
        cost_model_version=COST_MODEL_VERSION,
        created_by="svc_quant/intraday-experiment-runner",
        parent=inherited_parent,
        metadata={
            "candidate_role": "PREREGISTERED_PRIMARY",
            "runner_version": RUNNER_VERSION,
            "cross_hypothesis_parent_reason": (
                inheritance_reason if inherited_parent else None),
            "source_baseline_is_identity_not_ancestry": bool(
                primary_row.get("source_baseline_expr")),
        },
    )
    lineages[fingerprint(primary_row["intraday_signal_expr"])] = primary
    while pending:
        progressed = False
        for row in list(pending):
            parent_fp = str(row.get("parent_ast_fingerprint") or "")
            if parent_fp and parent_fp not in lineages:
                continue
            declared_parent_identity = str(row.get(
                "parent_candidate_identity_fingerprint") or "").strip()
            if parent_fp:
                # A parent explicitly included in this frozen population is
                # stronger provenance than any cross-hypothesis lookup.
                parent = lineages[parent_fp]
                if (declared_parent_identity and
                        parent.candidate_identity_fingerprint !=
                        declared_parent_identity):
                    raise RuntimeError(
                        "declared evolutionary parent AST and identity disagree")
                parent_reason = "EXPLICIT_POPULATION_PARENT"
            elif declared_parent_identity:
                parent = find_latest_candidate_lineage(
                    meta_conn, candidate_identity=declared_parent_identity)
                if parent is None:
                    raise RuntimeError(
                        "declared screening parent identity is not durable")
                parent_reason = "EXPLICIT_DURABLE_PARENT_IDENTITY"
            else:
                parent = primary
                parent_reason = "SHARED_REPLAY_PRIMARY_ROOT"
            feature_spec, label_spec, model_spec = _candidate_specs(config, row)
            lineage = register_candidate_lineage(
                meta_conn,
                hypothesis_id=hypothesis_id,
                candidate_ast=row["intraday_signal_expr"],
                semantic_plan=row["semantic_plan"],
                baseline_ast=row.get("source_baseline_expr"),
                feature_spec=feature_spec,
                label_spec=label_spec,
                model_spec=model_spec,
                economic_family_id=_economic_family_id(row["semantic_plan"]),
                evaluator_version=_evaluator_version(config),
                cost_model_version=COST_MODEL_VERSION,
                created_by="svc_quant/intraday-experiment-runner",
                parent=parent,
                metadata={
                    "candidate_role": row.get("candidate_role") or
                                      "LINKED_SCREENING_CANDIDATE",
                    "declared_parent_ast_fingerprint": parent_fp or None,
                    "parent_resolution": parent_reason,
                    "screening_only": True,
                    "runner_version": RUNNER_VERSION,
                },
            )
            lineages[str(row["ast_fingerprint"])] = lineage
            pending.remove(row)
            progressed = True
        if not progressed:
            unresolved = sorted({
                str(row.get("parent_ast_fingerprint") or "")
                for row in pending
                if str(row.get("parent_ast_fingerprint") or "")
            })
            raise RuntimeError(
                "screening population has missing or cyclic explicit parent: "
                + ", ".join(unresolved))
    return primary, lineages


def _stable_dataset_cutoff(source_lineage: list[dict],
                           fallback: datetime) -> str:
    values = [_aware_cutoff(fallback)]
    for row in source_lineage:
        for key in ("max_source_watermark", "max_available_at", "max_observed_at"):
            if row.get(key):
                try:
                    values.append(_aware_cutoff(datetime.fromisoformat(
                        str(row[key]))))
                except ValueError:
                    pass
    return max(values).isoformat()


def _aware_cutoff(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError("dataset cutoff must be timezone aware")
    return value.astimezone(timezone.utc)


def _allocate_trial_schedule(meta_conn, *, primary, experiment_id: str,
                             dataset_id: str, config: dict,
                             spec: IntradayLaneSpec, selected: dict,
                             source_lineage: list[dict]) -> dict:
    """Freeze the nested calibration/6/20/60 schedule before raw replay."""
    identity = _reference_id_map(selected)
    calibration_sessions = [_session_day(value) for value in
                            selected.get("calibration_sessions") or []]
    evaluation_sessions = [_session_day(value) for value in selected["sessions"]]
    discovery_sessions = _nested_session_prefix(
        evaluation_sessions, config["fast_screen_sessions"])
    validation_sessions = _nested_session_prefix(
        evaluation_sessions,
        min(config["intermediate_screen_sessions"], len(evaluation_sessions)))
    discovery_keys, discovery_panel = _profiled_panel(
        selected, config["fast_screen_instruments"])
    validation_keys, validation_panel = _profiled_panel(
        selected, config["intermediate_screen_instruments"])
    full_keys = list(selected["instruments"])
    watermark = {"source_lineage": source_lineage,
                 "event_source": selected.get("event_source")}
    # The replay cutoff is content-derived and retry-stable, but it must also
    # cover every requested label/purge timestamp.  Using the worker wall clock
    # here would make an otherwise identical retry conflict with its lockbox;
    # using only the last observed row can be earlier than a quiet session end.
    _, final_session_end = _session_bounds(max(evaluation_sessions))
    replay_floor = final_session_end + effective_purge_gap(
        spec, config["timestamp_policy"])
    dataset_cutoff = _stable_dataset_cutoff(source_lineage, replay_floor)

    def ids(keys):
        try:
            return [identity[key] for key in keys]
        except KeyError as exc:
            raise RuntimeError(f"rung key lacks STOCK identity: {exc}") from exc

    calibration = allocate_experiment_rung(
        meta_conn, candidate=primary, experiment_id=experiment_id,
        dataset_id=dataset_id, rung=CALIBRATION,
        session_dates=calibration_sessions, instrument_ids=ids(full_keys),
        selection_policy_version="strict-pre-oos-calibration-v1",
        dataset_cutoff=dataset_cutoff, source_watermark=watermark,
        allocation_reason="freeze teacher/structure calibration before any replay",
        allocated_by="svc_quant/intraday-experiment-runner")
    discovery = allocate_experiment_rung(
        meta_conn, candidate=primary, experiment_id=experiment_id,
        dataset_id=dataset_id, rung=DISCOVERY_6,
        session_dates=discovery_sessions, instrument_ids=ids(full_keys),
        selection_policy_version="nested-stock-bracket-v2",
        dataset_cutoff=dataset_cutoff, source_watermark=watermark,
        allocation_reason="outcome-blind discovery resource bracket",
        allocated_by="svc_quant/intraday-experiment-runner",
        predecessor=calibration)
    validation = None
    if len(evaluation_sessions) >= config["intermediate_screen_sessions"]:
        validation = allocate_experiment_rung(
            meta_conn, candidate=primary, experiment_id=experiment_id,
            dataset_id=dataset_id, rung=VALIDATION_20,
            session_dates=validation_sessions, instrument_ids=ids(full_keys),
            selection_policy_version="nested-stock-bracket-v2",
            dataset_cutoff=dataset_cutoff, source_watermark=watermark,
            allocation_reason="outcome-blind successive-halving validation bracket",
            allocated_by="svc_quant/intraday-experiment-runner",
            predecessor=discovery)
    full = None
    if len(evaluation_sessions) == 60:
        if validation is None:
            raise RuntimeError("FULL_60 requires a frozen VALIDATION_20 rung")
        full = allocate_experiment_rung(
            meta_conn, candidate=primary, experiment_id=experiment_id,
            dataset_id=dataset_id, rung=FULL_60,
            session_dates=evaluation_sessions, instrument_ids=ids(full_keys),
            selection_policy_version="all-stock-full-replay-v1",
            dataset_cutoff=dataset_cutoff, source_watermark=watermark,
            allocation_reason="all-stock historical support replay",
            allocated_by="svc_quant/intraday-experiment-runner",
            predecessor=validation)
    return {
        # Exposure is date-wide for the complete frozen STOCK universe.  The
        # smaller evaluation_keys are only a compute bracket and can never make
        # an unrecorded instrument/date look fresh at a later rung.
        "calibration": {"rung": calibration, "keys": full_keys,
                        "evaluation_keys": discovery_keys,
                        "panel": discovery_panel},
        "discovery": {"rung": discovery, "keys": full_keys,
                      "evaluation_keys": discovery_keys,
                      "panel": discovery_panel},
        "validation": ({"rung": validation, "keys": full_keys,
                        "evaluation_keys": validation_keys,
                        "panel": validation_panel} if validation else None),
        "full": ({"rung": full, "keys": full_keys,
                  "evaluation_keys": full_keys,
                  "panel": {"mode": "ALL_STOCKS"}} if full else None),
        "dataset_cutoff": dataset_cutoff,
        "lineage_count": 1 + len(config.get("screening_population") or []),
        "ledger_version": TRIAL_LEDGER_VERSION,
    }


def _session_exposure_evidence(market_conn, *, day: date, keys: list[str],
                               cutoff: datetime, event_source: str) -> dict:
    """Read only immutable daily provenance, never candidate raw events."""
    if event_source == EXTERNAL_EVENT_SOURCE:
        with market_conn.cursor() as cur:
            cur.execute(_EXTERNAL_SESSION_EVIDENCE_SQL, (day, keys))
            raw = cur.fetchall()
        rows = []
        for row in raw:
            instrument = str(row[0]).strip()
            identity = _external_source_content_identity(
                row[3], row[4], row[5],
                context=f"{day.isoformat()}/{instrument}")
            rows.append({
                "instrument": instrument,
                "quotes": int(row[1] or 0),
                "trades": int(row[2] or 0),
                **identity,
                "input_watermark": row[6].isoformat() if row[6] else None,
            })
        return {
            "content_fingerprint": stable_fingerprint(rows),
            "quote_rows": sum(row["quotes"] for row in rows),
            "trade_rows": sum(row["trades"] for row in rows),
            "source_watermark": {
                "event_source": event_source,
                "daily_input_watermarks": sorted({
                    row["input_watermark"] for row in rows
                    if row["input_watermark"]}),
                "daily_manifest_rows": len(rows),
                "source_content_hash_contract": (
                    EXTERNAL_SOURCE_CONTENT_HASH_CONTRACT),
            },
        }
    start, end = _session_bounds(day)
    params = (keys, start, end, cutoff)
    with market_conn.cursor() as cur:
        cur.execute(_LOCAL_SESSION_EVIDENCE_SQL, params + params)
        raw = cur.fetchall()
    rows = [{
        "source": str(row[0]),
        "rows": int(row[1] or 0),
        "max_available_at": row[2].isoformat() if row[2] else None,
        # Typed full-record hashing avoids the 6x JSON serialization penalty.
        # XOR plus an independently seeded sum preserves row multiplicity.
        "payload_multiset_digest": [str(row[3]), str(row[4])],
        "payload_digest_version": LOCAL_SOURCE_CONTENT_HASH_CONTRACT,
    } for row in raw]
    by_source = {row["source"]: row["rows"] for row in rows}
    return {
        "content_fingerprint": stable_fingerprint(rows),
        "quote_rows": int(by_source.get("market.market_quotes", 0)),
        "trade_rows": int(by_source.get("market.market_ticks", 0)),
        "source_watermark": {"event_source": event_source,
                             "tables": rows},
    }


def _record_rung_exposures(meta_conn, market_conn, *, schedule_row: dict,
                           selected: dict, cutoff: datetime,
                           event_source: str, knowledge_cutoff: str) -> dict:
    """Append the whole frozen rung before its first raw evaluator access."""
    rung = schedule_row["rung"]
    keys = list(schedule_row["keys"])
    identity = _reference_id_map(selected)
    instrument_ids = [identity[key] for key in keys]
    evidence = []
    for day in rung.planned_session_dates:
        access = record_session_access(
            meta_conn, rung=rung, session_date=day,
            instrument_ids=instrument_ids,
            knowledge_cutoff=knowledge_cutoff,
            source_watermark={
                "event_source": event_source,
                "phase": "PRE_RAW_ACCESS",
                "rung_plan_fingerprint": rung.rung_plan_fingerprint,
            },
            accessed_by="svc_quant/intraday-experiment-runner",
            access_purpose=(CALIBRATION if rung.rung == CALIBRATION
                            else ADAPTIVE_SEARCH),
            knowledge_clock_mode=EVENT_TIME_HISTORICAL_ONLY,
        )
        # Only a committed marker authorizes the following market query.
        daily = _session_exposure_evidence(
            market_conn, day=day, keys=keys, cutoff=cutoff,
            event_source=event_source)
        exposure = record_session_exposure(
            meta_conn, access=access, rung=rung, session_date=day,
            instrument_ids=instrument_ids,
            session_content_fingerprint=daily["content_fingerprint"],
            quote_row_count=daily["quote_rows"],
            trade_row_count=daily["trade_rows"],
            knowledge_cutoff=knowledge_cutoff,
            source_watermark=daily["source_watermark"],
            exposed_by="svc_quant/intraday-experiment-runner",
            exposure_purpose=(CALIBRATION if rung.rung == CALIBRATION
                              else ADAPTIVE_SEARCH),
            knowledge_clock_mode=EVENT_TIME_HISTORICAL_ONLY,
        )
        evidence.append({
            "session": day.isoformat(), "inserted": exposure.inserted,
            "access_inserted": access.inserted,
            "access_fingerprint": access.access_fingerprint,
            "evidence_fingerprint": exposure.exposure_evidence_fingerprint,
            "session_content_fingerprint": daily["content_fingerprint"],
            "quote_rows": daily["quote_rows"],
            "trade_rows": daily["trade_rows"],
            # This is the source-side content contract observed for the whole
            # frozen universe on this session.  Keep it in the append-only
            # report so an adaptive result never has to reconstruct or infer
            # provenance from a later database state.
            "source_watermark": daily["source_watermark"],
        })
    return {
        "rung": rung.rung,
        "experiment_rung_id": rung.experiment_rung_id,
        "candidate_lineage_id": rung.candidate.candidate_lineage_id,
        "root_lineage_id": rung.candidate.root_lineage_id,
        "dataset_id": rung.dataset_id,
        "dataset_cutoff": str(knowledge_cutoff),
        "session_count": len(evidence),
        "instrument_count": len(keys),
        # Repeat the immutable rung identities in the report/metric projection.
        # Recomputing only an ad-hoc list hash here would let a metric claim
        # FULL_60 without proving which frozen plan it came from.
        "session_set_fingerprint": rung.session_set_fingerprint,
        "instrument_ids_fingerprint": rung.instrument_set_fingerprint,
        "rung_plan_fingerprint": rung.rung_plan_fingerprint,
        "sessions": evidence,
        "append_before_raw_replay": True,
    }


def _canonical_report_value(value):
    """Return the JSON form used by the durable report manifest."""

    def encode(item):
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        raise TypeError(f"not JSON serializable: {type(item).__name__}")

    return json.loads(json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
        default=encode))


def _required_aware_iso(value, field: str) -> str:
    """Normalize one persisted timestamp without inventing a timezone."""
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{field} must be timezone aware")
    return parsed.astimezone(timezone.utc).isoformat()


def _candidate_search_contracts(config: dict) -> list[dict]:
    """Canonical formula/evaluator contracts for the exact rung population."""
    primary = {
        "intraday_signal_expr": config["intraday_signal_expr"],
        "semantic_plan": config["semantic_plan"],
        "horizon_seconds": config["horizon_seconds"],
        "execution": config["execution"],
        "entry_policy": config["entry_policy"],
        "coefficient_policy": config["coefficient_policy"],
    }
    rows = [("PRIMARY", primary)] + sorted((
        (str(row["ast_fingerprint"]), row)
        for row in config.get("screening_population") or []),
        key=lambda item: item[0])
    contracts = []
    for candidate, row in rows:
        expression = parse_expr(row["intraday_signal_expr"])
        feature_spec, label_spec, model_spec = _candidate_specs(config, row)
        contracts.append({
            "candidate": candidate,
            "ast_fingerprint": fingerprint(expression),
            "semantic_plan_fingerprint": stable_fingerprint(
                row["semantic_plan"]),
            "horizon_seconds": int(row["horizon_seconds"]),
            "clock_domains": sorted(effective_clock_domains_of(expression)),
            "clock_windows_seconds": sorted(clocks_of(expression)),
            "execution": str(row["execution"]).upper(),
            "entry_policy": str(row["entry_policy"]).upper(),
            "coefficient_policy": str(row["coefficient_policy"]).upper(),
            "feature_contract": feature_spec,
            "label_contract": label_spec,
            "model_contract": model_spec,
        })
    return _canonical_report_value(contracts)


def _adaptive_rung_search_exposure(
        *, config: dict, spec: IntradayLaneSpec, selected: dict,
        schedule_row: dict, exposure_manifest: dict, screen: dict,
        dataset_id: str, dataset_cutoff, event_source: str,
        source_lineage: list[dict]) -> dict:
    """Build an ID-independent, fail-closed adaptive-rung exposure identity.

    The ledger's rung/access/evidence fingerprints deliberately bind UUIDs and
    are therefore unsuitable for comparing the same scientific exposure across
    retries or experiments.  This identity verifies those durable rows and then
    hashes only the dataset, content, exact panel and evaluator contracts.  It
    contains no outcome and grants no promotion authority.
    """
    rung = schedule_row.get("rung")
    if rung is None or str(getattr(rung, "rung", "")) not in {
            DISCOVERY_6, VALIDATION_20}:
        raise RuntimeError("search exposure requires an adaptive discovery rung")
    rung_name = str(rung.rung)
    if (str(screen.get("rung") or "") != rung_name
            or str(exposure_manifest.get("rung") or "") != rung_name):
        raise RuntimeError("adaptive screen, schedule and exposure rung differ")
    if (str(getattr(rung, "dataset_id", "")) != str(dataset_id)
            or not str(dataset_id)):
        raise RuntimeError("adaptive exposure dataset differs from frozen rung")
    cutoff = _required_aware_iso(dataset_cutoff, "dataset_cutoff")
    if (str(exposure_manifest.get("dataset_id") or "") != str(dataset_id)
            or _required_aware_iso(
                exposure_manifest.get("dataset_cutoff"),
                "exposure dataset_cutoff") != cutoff):
        raise RuntimeError(
            "adaptive exposure dataset/cutoff differs from frozen schedule")

    planned_sessions = [value.isoformat() if isinstance(value, date)
                        else str(value)
                        for value in rung.planned_session_dates]
    exposure_rows = list(exposure_manifest.get("sessions") or [])
    exposed_sessions = [str(row.get("session") or "")
                        for row in exposure_rows]
    screen_sessions = [str(value) for value in screen.get("sessions") or []]
    evaluated_sessions = [str(value) for value in
                          screen.get("evaluated_sessions") or []]
    if (not planned_sessions or len(set(planned_sessions)) != len(
            planned_sessions)):
        raise RuntimeError("adaptive rung lacks unique planned sessions")
    if exposed_sessions != planned_sessions or screen_sessions != planned_sessions:
        raise RuntimeError(
            "adaptive exposure sessions differ from the frozen rung plan")
    expected_session_fp = stable_fingerprint(planned_sessions)
    if (str(rung.session_set_fingerprint) != expected_session_fp
            or str(exposure_manifest.get("session_set_fingerprint") or "")
            != expected_session_fp
            or int(exposure_manifest.get("session_count") or -1)
            != len(planned_sessions)
            or int(screen.get("session_count") or -1) != len(planned_sessions)
            or isinstance(screen.get("evaluated_session_count"), bool)
            or not isinstance(screen.get("evaluated_session_count"), int)
            or screen.get("evaluated_session_count")
            != len(evaluated_sessions)):
        raise RuntimeError("adaptive session counts/fingerprints do not reconcile")
    evaluation_status = str(screen.get("evaluation_status") or "")
    if evaluation_status == "EVALUATED":
        if evaluated_sessions != planned_sessions:
            raise RuntimeError("evaluated adaptive rung omitted a planned session")
    elif evaluation_status == "SKIPPED_COST_INFEASIBLE":
        if evaluated_sessions:
            raise RuntimeError("skipped adaptive rung claims evaluated sessions")
    else:
        raise RuntimeError("adaptive rung lacks an explicit evaluation status")

    full_keys = [str(value) for value in schedule_row.get("keys") or []]
    panel_keys = [str(value) for value in
                  schedule_row.get("evaluation_keys") or []]
    selected_keys = [str(value) for value in selected.get("instruments") or []]
    if (not full_keys or full_keys != selected_keys
            or not panel_keys or len(set(panel_keys)) != len(panel_keys)
            or not set(panel_keys).issubset(full_keys)):
        raise RuntimeError("adaptive rung lacks an exact nested panel membership")
    reference_identity_fp = str(
        selected.get("reference_identity_fingerprint") or "")
    if (len(reference_identity_fp) != 64
            or any(char not in "0123456789abcdef"
                   for char in reference_identity_fp)):
        raise RuntimeError(
            "adaptive rung lacks exact governed reference identity evidence")
    identity_map = _reference_id_map(selected)
    panel_reference_ids = [identity_map[key] for key in panel_keys]
    full_reference_ids = sorted(identity_map[key] for key in full_keys)
    expected_full_fp = stable_fingerprint(full_reference_ids)
    if (tuple(full_reference_ids) != tuple(rung.planned_instrument_ids)
            or int(rung.planned_instrument_count) != len(full_keys)
            or str(rung.instrument_set_fingerprint) != expected_full_fp
            or int(exposure_manifest.get("instrument_count") or -1)
            != len(full_keys)
            or str(exposure_manifest.get("instrument_ids_fingerprint") or "")
            != expected_full_fp):
        raise RuntimeError(
            "adaptive full-universe counts/fingerprints do not reconcile")
    expected_legacy_panel_fp = hashlib.sha256(
        "|".join(panel_keys).encode()).hexdigest()
    if (int(screen.get("instrument_count") or -1) != len(panel_keys)
            or str(screen.get("instrument_fingerprint") or "")
            != expected_legacy_panel_fp):
        raise RuntimeError("adaptive panel counts/fingerprints do not reconcile")

    panel_manifest = _canonical_report_value(
        schedule_row.get("panel") or {})
    if (_canonical_report_value(screen.get("panel_manifest") or {})
            != panel_manifest
            or panel_manifest.get("promotion_authority") is not False):
        raise RuntimeError("adaptive panel manifest is missing or changed")
    manifest_members = [str(value) for value in (
        list(panel_manifest.get("information_rich") or [])
        + list(panel_manifest.get("representative_guard") or []))]
    if (manifest_members and (len(set(manifest_members)) != len(panel_keys)
                              or set(manifest_members) != set(panel_keys))):
        raise RuntimeError("panel manifest membership differs from replay panel")

    content_rows = []
    for expected_session, row in zip(planned_sessions, exposure_rows):
        content_fp = str(row.get("session_content_fingerprint") or "")
        watermark = row.get("source_watermark")
        quote_rows = row.get("quote_rows")
        trade_rows = row.get("trade_rows")
        if (str(row.get("session") or "") != expected_session
                or len(content_fp) != 64
                or any(char not in "0123456789abcdef" for char in content_fp)
                or not isinstance(watermark, dict) or not watermark
                or isinstance(quote_rows, bool) or not isinstance(quote_rows, int)
                or quote_rows < 0
                or isinstance(trade_rows, bool) or not isinstance(trade_rows, int)
                or trade_rows < 0):
            raise RuntimeError(
                "adaptive rung lacks exact per-session content evidence")
        content_rows.append({
            "session": expected_session,
            "session_content_fingerprint": content_fp,
            "quote_rows": quote_rows,
            "trade_rows": trade_rows,
            "source_watermark": watermark,
        })

    if not isinstance(source_lineage, list) or not source_lineage:
        raise RuntimeError("adaptive rung lacks frozen source lineage")
    canonical_lineage = _canonical_report_value(source_lineage)
    candidate_contracts = _candidate_search_contracts(config)
    data_contract = _assert_resolved_data_contract(config)
    dataset_name, _, dataset_version = data_contract["dataset"].rpartition("/")
    panel_is_full_universe = (
        len(panel_keys) == len(full_keys) and set(panel_keys) == set(full_keys))
    identity = {
        "version": ADAPTIVE_SEARCH_EXPOSURE_VERSION,
        "fingerprint_contract": SEARCH_EXPOSURE_FINGERPRINT_CONTRACT,
        "identifier_exclusions": list(
            SEARCH_EXPOSURE_IDENTIFIER_EXCLUSIONS),
        "evidence_purpose": ADAPTIVE_SEARCH,
        "adaptive_search_only": True,
        "promotion_authority": False,
        "dataset": {
            "name": dataset_name,
            "version": dataset_version,
            "dataset_id": str(dataset_id),
            "dataset_cutoff": cutoff,
            "asset_scope": STOCK_ASSET_SCOPE,
            "stock_universe_contract_version": STOCK_UNIVERSE_VERSION,
            "reference_identity_fingerprint": reference_identity_fp,
        },
        "rung": rung_name,
        "evaluation": {
            "status": evaluation_status,
            "measurement_scope": (
                "ADAPTIVE_RUNG_MEASURED" if evaluation_status == "EVALUATED"
                else "CALIBRATION_ONLY_RESOURCE_STOP"),
            "planned_sessions": planned_sessions,
            "planned_session_count": len(planned_sessions),
            "evaluated_sessions": evaluated_sessions,
            "evaluated_session_count": len(evaluated_sessions),
            "session_set_fingerprint": expected_session_fp,
            "panel_replay_keys": panel_keys,
            "panel_reference_instrument_ids": panel_reference_ids,
            "panel_instrument_count": len(panel_keys),
            "panel_order_fingerprint": stable_fingerprint(panel_keys),
            "panel_reference_set_fingerprint": stable_fingerprint(
                sorted(panel_reference_ids)),
            "panel_manifest": panel_manifest,
            "full_universe_instrument_count": len(full_keys),
            "full_universe_reference_set_fingerprint": expected_full_fp,
        },
        "content_evidence": {
            "scope": "FULL_FROZEN_STOCK_UNIVERSE_PER_SESSION",
            "per_session": content_rows,
            "panel_only_content_fingerprints_available":
                panel_is_full_universe,
            "conservative_full_universe_content_limitation": (
                None if panel_is_full_universe else
                "Session content hashes cover the full frozen STOCK universe, "
                "not only the evaluated panel. They conservatively invalidate "
                "the exposure when any non-panel source content changes and "
                "cannot prove panel-only byte equivalence."),
        },
        "source_contract": {
            "event_source": str(event_source),
            "source_lineage": canonical_lineage,
            "source_lineage_fingerprint": stable_fingerprint(
                canonical_lineage),
            "knowledge_clock_mode": EVENT_TIME_HISTORICAL_ONLY,
            "timestamp_policy": str(config["timestamp_policy"]),
        },
        "lane_contract": _canonical_report_value(manifest(
            spec, source=event_source,
            timestamp_policy=config["timestamp_policy"])),
        "execution_contract": {
            "population_execution_model": config[
                "population_execution_model"],
            "position_mode": config["position_mode"],
            "order_latency_ms": config["order_latency_ms"],
            "max_quote_age_seconds": config["max_quote_age_seconds"],
            "minimum_predicted_edge_bps": config[
                "minimum_predicted_edge_bps"],
        },
        "cost_contract": {
            "cost_model_version": COST_MODEL_VERSION,
            "fee_bps_per_side": config["fee_bps_per_side"],
            "maker_fee_bps_per_side": config[
                "maker_fee_bps_per_side"],
        },
        "evaluator_contract": {
            "runner_version": RUNNER_VERSION,
            "evaluator_version": _evaluator_version(config),
            "fast_screen_version": FAST_SCREEN_VERSION,
            "candidate_contracts": candidate_contracts,
            "candidate_set_fingerprint": stable_fingerprint(
                candidate_contracts),
        },
        "cross_checks": {
            "ledger_session_set_fingerprint_verified": True,
            "ledger_full_universe_fingerprint_verified": True,
            "screen_panel_fingerprint_verified": True,
            "screen_panel_manifest_verified": True,
            "per_session_content_evidence_verified": True,
        },
    }
    canonical = _canonical_report_value(identity)
    sealed = {
        **canonical,
        "search_exposure_fingerprint": search_exposure_fingerprint(canonical),
    }
    assert_strict_exposure(sealed)
    return sealed


def _as_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _mark_experiment_failed(meta_conn, experiment_id: str) -> None:
    """Best-effort terminal state after first clearing an aborted transaction."""
    meta_conn.rollback()
    with meta_conn.cursor() as cur:
        cur.execute(
            "update quant.experiments set status='FAILED', ended_at=now() "
            "where experiment_id=%s and status='RUNNING'", (experiment_id,))
    meta_conn.commit()


def _guard_experiment_step(meta_conn, experiment_id: str, function, /,
                           *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except Exception:
        _mark_experiment_failed(meta_conn, experiment_id)
        raise


def _candidate_accumulators(config: dict, spec: IntradayLaneSpec, *, trials: int
                            ) -> dict[str, CandidateAccumulator]:
    """Build independent statistics engines for one shared replay cohort."""
    # Later rungs may carry fewer survivors, but every candidate exposed on an
    # earlier rung remains a trial.  Never let successive halving erase search
    # multiplicity from DSR.
    # ``trials`` already includes the primary ledger reservation.  Sidecars are
    # additional formulas inside that reservation, not a second copy of the
    # primary.  Count the larger of the append-only family pressure and the
    # complete synchronous formula population replayed by this experiment.
    current_formula_trials = 1 + len(config["screening_population"])
    primary_trials = max(
        1, int(trials), 1 + int(config.get("primary_attempts_before") or 0))
    historical_screens = int(
        config.get("historical_exact_screening_exposures") or 0)
    current_screen_exposure = max(
        len(config["screening_population"]),
        int(config.get("screening_trial_exposure") or 0))
    config["selection_adjusted_trials"] = (
        primary_trials + historical_screens + current_screen_exposure)
    config["selection_pressure_breakdown"] = {
        "primary_trials_including_current": primary_trials,
        "historical_exact_screening_exposures": historical_screens,
        "current_screening_exposures": current_screen_exposure,
        "current_synchronous_formula_vectors": current_formula_trials,
    }

    def build(row: dict) -> CandidateAccumulator:
        return CandidateAccumulator(
            expr=row["intraday_signal_expr"], spec=spec,
            horizon_seconds=row["horizon_seconds"], execution=row["execution"],
            position_mode=config["position_mode"], threshold=config["threshold"],
            entry_policy=row["entry_policy"],
            coefficient_policy=row["coefficient_policy"],
            minimum_predicted_edge_bps=config["minimum_predicted_edge_bps"],
            trials=current_formula_trials, family_pbo=None,
            semantic_plan=row["semantic_plan"],
            feature_window_contract_version=
                _feature_window_contract(config))

    primary = {
        "intraday_signal_expr": config["intraday_signal_expr"],
        "horizon_seconds": config["horizon_seconds"],
        "execution": config["execution"],
        "entry_policy": config["entry_policy"],
        "coefficient_policy": config["coefficient_policy"],
        "semantic_plan": config["semantic_plan"],
    }
    out = {"PRIMARY": build(primary)}
    for row in config["screening_population"]:
        out[row["ast_fingerprint"]] = build(row)
    return out


def _pareto_ranks(rows: list[dict]) -> dict[str, int]:
    """Non-dominated ranks over net/gross/coverage/novelty and complexity."""
    remaining = {row["key"]: row for row in rows}
    ranks: dict[str, int] = {}
    rank = 1

    def vector(row):
        summary = row["report"].get("summary") or {}
        missing = float("-inf")
        return (
            summary.get("mean_net_bps_per_opportunity")
            if summary.get("mean_net_bps_per_opportunity") is not None else missing,
            summary.get("mean_mid_markout_bps")
            if summary.get("mean_mid_markout_bps") is not None else missing,
            summary.get("instrument_coverage")
            if summary.get("instrument_coverage") is not None else missing,
            row["novelty"],
            -row["complexity"],
        )

    while remaining:
        frontier = []
        for key, row in remaining.items():
            values = vector(row)
            dominated = False
            for other_key, other in remaining.items():
                if other_key == key:
                    continue
                rival = vector(other)
                if all(left >= right for left, right in zip(rival, values)) and \
                        any(left > right for left, right in zip(rival, values)):
                    dominated = True
                    break
            if not dominated:
                frontier.append(key)
        for key in frontier:
            ranks[key] = rank
            remaining.pop(key)
        rank += 1
    return ranks


def _complexity_bucket(nodes: int) -> str:
    if nodes <= 5:
        return "NODES_1_5"
    if nodes <= 10:
        return "NODES_6_10"
    if nodes <= 20:
        return "NODES_11_20"
    return "NODES_21_PLUS"


def _residual_qd_archive(rows: list[dict]) -> tuple[dict[str, dict], list[dict]]:
    """Keep one screening elite per worst-residual/complexity behavior cell.

    Residuals are measured on the shared OOS replay, so this archive is only a
    search diagnostic.  It cannot alter the preregistered primary decision or
    grant promotion authority.
    """
    annotations: dict[str, dict] = {}
    cells: dict[str, list[dict]] = {}
    for row in rows:
        behavior = row["report"].get("residual_behavior") or {}
        worst = behavior.get("worst_time_bucket")
        robust_gain = behavior.get(
            "median_time_bucket_mae_improvement_vs_null_bps")
        eligible = (behavior.get("status") == "PASS" and worst
                    and isinstance(robust_gain, (int, float))
                    and not isinstance(robust_gain, bool))
        if not eligible:
            annotations[row["key"]] = {
                "status": "NOT_ELIGIBLE", "cell": None, "elite": False,
                "reason": behavior.get("status") or "MISSING_RESIDUAL_BEHAVIOR",
            }
            continue
        cell = f"{worst}/{_complexity_bucket(row['complexity'])}"
        summary = row["report"].get("summary") or {}
        net = summary.get("mean_net_bps_per_opportunity")
        coverage = summary.get("instrument_coverage")
        item = {
            "key": row["key"], "cell": cell,
            "worst_time_bucket": worst,
            "complexity_bucket": _complexity_bucket(row["complexity"]),
            "median_time_bucket_mae_bps": behavior.get(
                "median_time_bucket_mae_bps"),
            "median_time_bucket_mae_improvement_vs_null_bps": float(
                robust_gain),
            "mean_net_bps_per_opportunity": (
                float(net) if isinstance(net, (int, float))
                and not isinstance(net, bool) else None),
            "instrument_coverage": (
                float(coverage) if isinstance(coverage, (int, float))
                and not isinstance(coverage, bool) else None),
            "complexity_nodes": row["complexity"],
        }
        cells.setdefault(cell, []).append(item)

    archive = []
    for cell, group in sorted(cells.items()):
        def quality(item: dict) -> tuple:
            return (
                item["median_time_bucket_mae_improvement_vs_null_bps"],
                item["mean_net_bps_per_opportunity"]
                if item["mean_net_bps_per_opportunity"] is not None
                else float("-inf"),
                item["instrument_coverage"]
                if item["instrument_coverage"] is not None else float("-inf"),
                -item["complexity_nodes"],
                item["key"],
            )

        winner = max(group, key=quality)
        archive.append({**winner, "competitors": len(group)})
        for item in group:
            annotations[item["key"]] = {
                "status": "ELIGIBLE",
                "cell": cell,
                "elite": item["key"] == winner["key"],
                "competitors": len(group),
                "median_time_bucket_mae_bps": item[
                    "median_time_bucket_mae_bps"],
                "median_time_bucket_mae_improvement_vs_null_bps": item[
                    "median_time_bucket_mae_improvement_vs_null_bps"],
                "adaptive_search_memory_only": True,
                "independent_confirmation": False,
                "forward_new_sessions_required": True,
                "promotion_authority": False,
            }
    return annotations, archive


def _annotate_population(config: dict, reports: dict[str, dict]) -> dict:
    """Label screen evidence without granting it promotion authority."""
    primary = reports["PRIMARY"]
    primary_summary = primary.get("summary") or {}
    primary_expr = config["intraday_signal_expr"]
    metadata = {row["ast_fingerprint"]: row
                for row in config["screening_population"]}
    ranking_rows = [{
        "key": "PRIMARY", "report": primary, "novelty": 0.0,
        "complexity": count_nodes(primary_expr), "expression": primary_expr,
    }]
    for key, report in reports.items():
        if key == "PRIMARY":
            continue
        expression = metadata[key]["intraday_signal_expr"]
        ranking_rows.append({
            "key": key, "report": report,
            "novelty": 1.0 - structural_similarity(primary_expr, expression),
            "complexity": count_nodes(expression), "expression": expression,
        })
    for row in ranking_rows:
        rivals = [other for other in ranking_rows if other is not row]
        population_novelty = (min(
            1.0 - structural_similarity(
                row["expression"], other["expression"])
            for other in rivals) if rivals else 1.0)
        row["report"]["search_structural_novelty"] = population_novelty
        row["report"]["complexity_nodes"] = row["complexity"]
    ranks = _pareto_ranks(ranking_rows)
    residual_qd, residual_archive = _residual_qd_archive(ranking_rows)
    primary["residual_qd"] = residual_qd["PRIMARY"]
    screening_reports = []
    for row in ranking_rows[1:]:
        key, report = row["key"], row["report"]
        source = metadata[key]
        gate_decision = report.get("decision")
        empirical_influence = None
        if source.get("candidate_role") == "STRUCTURAL_ABLATION":
            ablation_summary = report.get("summary") or {}

            def difference(metric: str):
                left, right = primary_summary.get(metric), ablation_summary.get(metric)
                if (not isinstance(left, (int, float)) or isinstance(left, bool)
                        or not isinstance(right, (int, float))
                        or isinstance(right, bool)):
                    return None
                return float(left) - float(right)

            net_increment = difference("mean_net_bps_per_opportunity")
            empirical_influence = {
                "comparison": "PRIMARY_MINUS_STRUCTURAL_ABLATION",
                "ablation_operator": source.get("ablation_operator"),
                "ablation_path": source.get("ablation_path"),
                "ablation_of_ast_fingerprint": source.get(
                    "ablation_of_ast_fingerprint"),
                "net_increment_bps": net_increment,
                "gross_increment_bps": difference("mean_mid_markout_bps"),
                "implementation_drag_increment_bps": difference(
                    "mean_implementation_drag_bps"),
                "coverage_increment": difference("instrument_coverage"),
                "interpretation": (
                    "POSITIVE_POINT_ESTIMATE" if net_increment is not None
                    and net_increment > 0 else
                    "NON_POSITIVE_POINT_ESTIMATE" if net_increment is not None
                    else "NOT_MEASURED"),
                "evidence_warning": (
                    "same-replay screening contrast; descriptive, not causal or "
                    "promotion evidence"),
            }
        report.update({
            "screening_only": True,
            "evidence_tier": "SCREENING_ONLY",
            "screening_gate_decision": gate_decision,
            "decision": "SCREENING_ONLY",
            "candidate_role": source.get("candidate_role"),
            "source_lead_ids": list(source.get("source_lead_ids") or []),
            "title": source.get("title"),
            "evolution_role": source.get("evolution_role"),
            "parent_ast_fingerprint": source.get("parent_ast_fingerprint"),
            "parent_of_ast_fingerprint": source.get(
                "parent_of_ast_fingerprint"),
            "ablation_operator": source.get("ablation_operator"),
            "ablation_path": source.get("ablation_path"),
            "ablation_of_ast_fingerprint": source.get(
                "ablation_of_ast_fingerprint"),
            "ablation_version": source.get("ablation_version"),
            "empirical_influence": empirical_influence,
            "novelty_vs_primary": row["novelty"],
            "complexity_nodes": row["complexity"],
            "pareto_rank": ranks[key],
            "pareto_front": ranks[key] == 1,
            "residual_qd": residual_qd[key],
            "not_a_promotion": (
                "SCREENING_ONLY evidence may nominate an independent "
                "confirmatory primary experiment; it cannot promote alpha"),
        })
        screening_reports.append(report)
    primary["screening_population"] = screening_reports
    primary["population_evaluation"] = {
        "shared_raw_replay": True,
        "candidate_count": 1 + len(screening_reports),
        "selection_adjusted_trials": max(
            int(primary["summary"].get("trials") or 1),
            int(config.get("selection_adjusted_trials") or 0)),
        "selection_pressure_breakdown": dict(
            config.get("selection_pressure_breakdown") or {}),
        "current_synchronous_formula_vectors": 1 + len(screening_reports),
        "selection_rule": (
            "cost-net/coverage/novelty/complexity Pareto screen plus "
            "same-replay structural-ablation influence and residual-behavior "
            "MAP-Elites archive"),
        "residual_archive_version": "krx-domain-residual-qd-v1",
        "residual_archive": residual_archive,
        "residual_archive_cells": len(residual_archive),
        "residual_archive_boundary": "OOS_DIAGNOSTIC_SCREENING_ONLY",
        "residual_archive_independent_confirmation": False,
        "residual_archive_forward_new_sessions_required": True,
        "promotion_authority": "PRIMARY_ONLY",
    }
    primary["summary"].update({
        "screening_candidates": len(screening_reports),
        "screening_pareto_survivors": sum(
            bool(row["pareto_front"]) for row in screening_reports),
        "screening_positive_net": sum(
            ((row.get("summary") or {}).get("mean_net_bps_per_opportunity")
             or 0.0) > 0 for row in screening_reports),
        "screening_residual_qd_elites": sum(
            bool((row.get("residual_qd") or {}).get("elite"))
            for row in screening_reports),
    })
    return primary


def _population_multiple_testing(report: dict) -> dict:
    """Attach synchronous-family SPA/RC and calibrated-DSR diagnostics.

    The benchmark is cash (zero cost-net session return).  This historical
    diagnostic never promotes a candidate and never claims to cover older
    family trials whose session vectors are absent from the immutable ledger.
    """
    candidates = [("PRIMARY", report)] + [
        (str(row.get("ast_fingerprint") or ""), row)
        for row in report.get("screening_population") or []]
    relative: dict[str, list[float]] = {}
    paired: dict[str, dict] = {}
    strategy_reports: list[tuple[str, dict]] = []
    for key, row in candidates:
        ast_common = ((row.get("control_comparison") or {}).get(
            "ast_common_capital_session_returns_bps") or
            row.get("session_returns_bps") or {})
        strategy_reports.append((f"AST:{key}", {
            **row, "session_returns_bps": ast_common}))
        for label in ("supervised_control", "hybrid_control"):
            control = row.get(label) or {}
            strategy = control.get("strategy") or {}
            common = control.get("paired_common_capital_session_returns_bps")
            if common:
                strategy_reports.append((f"{label.upper()}:{key}", {
                    **strategy, "session_returns_bps": common}))

    def ordered_session_identity(values: dict) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(
            (type(session).__name__, str(session)) for session in values))

    primary_spa_returns = (strategy_reports[0][1].get("session_returns_bps")
                           or {})
    reference_session_identity = (
        ordered_session_identity(primary_spa_returns)
        if isinstance(primary_spa_returns, dict) and primary_spa_returns
        else ())
    synchronous_strategy_vectors = bool(reference_session_identity)
    synchronous_vector_failures: list[str] = []
    seen_outcomes: set[str] = set()
    for key, row in strategy_reports:
        returns = row.get("session_returns_bps") or {}
        if not isinstance(returns, dict) or not returns:
            paired[key] = {"valid": False, "reason": "missing session returns"}
            synchronous_strategy_vectors = False
            synchronous_vector_failures.append(key)
            continue
        if ordered_session_identity(returns) != reference_session_identity:
            paired[key] = {
                "valid": False,
                "reason": "strategy must cover the exact primary session keys",
            }
            synchronous_strategy_vectors = False
            synchronous_vector_failures.append(key)
            continue
        outcome_fp = stable_fingerprint(returns)
        if outcome_fp in seen_outcomes:
            paired[key] = {"valid": False,
                           "reason": "duplicate strategy outcome vector"}
            continue
        seen_outcomes.add(outcome_fp)
        zero = {session: 0.0 for session in returns}
        result = paired_session_deltas(
            returns, zero, minimum_effect=0.0, min_sessions=20)
        paired[key] = {name: value for name, value in result.items()
                       if name != "paired_deltas"}
        if result.get("valid"):
            relative[key] = list(result["paired_deltas"])
        else:
            synchronous_strategy_vectors = False
            synchronous_vector_failures.append(key)
    spa = (spa_reality_check(relative, n_boot=10_000,
                             restart_probability=0.25, seed=20260817,
                             min_sessions=20)
           if relative and synchronous_strategy_vectors else {
               "valid": False,
               "reason": "complete synchronous family session vectors unavailable",
               "non_synchronous_candidates": sorted(set(
                   synchronous_vector_failures)),
           })
    primary_returns = report.get("session_returns_bps") or {}
    primary_formula_session_identity = (
        ordered_session_identity(primary_returns)
        if isinstance(primary_returns, dict) and primary_returns else ())
    formula_sharpes: list[float] = []
    complete_formula_vectors = bool(primary_formula_session_identity)
    formula_vector_fingerprints: list[str] = []
    formula_shape_fingerprints: list[str] = []
    for _key, row in candidates:
        returns = row.get("session_returns_bps") or {}
        sharpe_value = (row.get("summary") or {}).get("sharpe")
        valid_sharpe = (
            isinstance(sharpe_value, (int, float))
            and not isinstance(sharpe_value, bool)
            and math.isfinite(float(sharpe_value)))
        same_sessions = (
            isinstance(returns, dict)
            and ordered_session_identity(returns) ==
                primary_formula_session_identity
            and all(isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in returns.values()))
        if not valid_sharpe or not same_sessions:
            complete_formula_vectors = False
            continue
        # Structurally distinct preregistered formulas remain distinct trials
        # even when they happen to produce an identical outcome vector.  The
        # duplicate fingerprint remains a diagnostic, never a trial-count
        # discount.
        formula_sharpes.append(float(sharpe_value))
        formula_vector_fingerprints.append(stable_fingerprint(returns))
        formula_shape_fingerprints.append(str(
            row.get("ast_shape_fingerprint")
            or row.get("ast_fingerprint") or _key))

    calibrated_dsr = None
    expected_trials = int((report.get("summary") or {}).get("trials") or 0)
    declared_selection_trials = max(
        expected_trials,
        int((report.get("population_evaluation") or {}).get(
            "selection_adjusted_trials") or 0))
    complete_formula_vectors = (
        complete_formula_vectors
        and len(formula_sharpes) == expected_trials
        and len(candidates) == expected_trials)
    dispersion_sample_count = len(formula_sharpes)
    structurally_distinct_formulas = len(set(formula_shape_fingerprints))
    trial_sharpe_std = None
    if dispersion_sample_count >= MIN_DSR_DISPERSION_FORMULAS:
        anchor = formula_sharpes[0]
        deviations = [value - anchor for value in formula_sharpes]
        centred = math.fsum(deviations) / dispersion_sample_count
        trial_sharpe_std = math.sqrt(math.fsum(
            (value - centred) ** 2 for value in deviations) /
            (dispersion_sample_count - 1))
    dsr_calibration_ready = (
        complete_formula_vectors
        and dispersion_sample_count >= MIN_DSR_DISPERSION_FORMULAS
        and structurally_distinct_formulas >= MIN_DSR_DISPERSION_FORMULAS
        and isinstance(trial_sharpe_std, (int, float))
        and math.isfinite(float(trial_sharpe_std))
        and float(trial_sharpe_std) >= 0.0)
    if dsr_calibration_ready:
        returns = [float(value) / 10_000.0 for _session, value in
                   sorted(primary_returns.items())]
        observed_sigma = float(trial_sharpe_std)
        sigma_used = max(
            observed_sigma, DSR_TRIAL_SHARPE_STD_FLOOR)
        needs_extrapolation = declared_selection_trials > expected_trials
        floor_is_binding = sigma_used > observed_sigma
        if not needs_extrapolation and not floor_is_binding:
            calibrated_dsr = deflated_sharpe(
                returns, trials=expected_trials,
                trial_sharpes=formula_sharpes,
                effective_trials=float(expected_trials), periods=252)
        else:
            # The current complete cohort estimates Sharpe dispersion.  Older
            # screening vectors are unavailable, so never fabricate them:
            # conservatively apply that observed dispersion to the full
            # append-only selection count.
            calibrated_dsr = deflated_sharpe(
                returns, trials=declared_selection_trials,
                trial_sharpe_std=sigma_used,
                effective_trials=float(declared_selection_trials), periods=252)
            calibrated_dsr.update({
                "calibration_mode": (
                    "CONSERVATIVE_COHORT_FLOOR_EXTRAPOLATION"
                    if needs_extrapolation else
                    "CONSERVATIVE_COHORT_FLOOR_CALIBRATION"),
                "dispersion_estimator": "CURRENT_SYNCHRONOUS_COHORT_SAMPLE_STD",
                "observed_trial_sharpe_std": observed_sigma,
                "trial_sharpe_std_floor": DSR_TRIAL_SHARPE_STD_FLOOR,
                "trial_sharpe_std_used": sigma_used,
            })
    return {
        "version": MULTIPLE_TESTING_VERSION,
        "benchmark": "CASH_ZERO_COST_NET_SESSION_RETURN",
        "paired_cost_net": paired,
        "spa_reality_check": spa,
        "observed_population_dsr": calibrated_dsr,
        "observed_unique_strategy_outcomes": len(seen_outcomes),
        "observed_formula_trials": len(formula_sharpes),
        "structurally_distinct_formula_trials": structurally_distinct_formulas,
        "observed_unique_formula_outcomes": len(set(
            formula_vector_fingerprints)),
        "selection_adjusted_trials_declared": declared_selection_trials,
        "dispersion_sample_count": dispersion_sample_count,
        "dispersion_extrapolated_to_trials": declared_selection_trials,
        "dispersion_extrapolation": (
            declared_selection_trials > dispersion_sample_count),
        "dsr_calibration_ready": dsr_calibration_ready,
        "minimum_dispersion_formulas": MIN_DSR_DISPERSION_FORMULAS,
        "observed_trial_sharpe_std": trial_sharpe_std,
        "trial_sharpe_std_floor": (
            DSR_TRIAL_SHARPE_STD_FLOOR
            if (declared_selection_trials > dispersion_sample_count
                or (isinstance(trial_sharpe_std, (int, float))
                    and float(trial_sharpe_std) <
                    DSR_TRIAL_SHARPE_STD_FLOOR)) else None),
        "trial_sharpe_std_used": (
            calibrated_dsr.get("trial_sharpe_std")
            if isinstance(calibrated_dsr, dict) else None),
        "historical_vectors_fabricated": False,
        "raw_count_as_effective_upper_bound": True,
        "effective_trial_count_policy":
            "RAW_APPEND_ONLY_SELECTION_COUNT_UPPER_BOUND",
        "historical_trial_vectors_missing": max(
            0, declared_selection_trials - dispersion_sample_count),
        "complete_synchronous_formula_vectors_available":
            complete_formula_vectors,
        "complete_historical_trial_vectors_available": (
            complete_formula_vectors
            and declared_selection_trials == dispersion_sample_count),
        "e_bh": {
            "status": "NOT_RUN",
            "reason": "no preregistered always-valid e-value process yet",
            "fabricated_e_values": False,
        },
        "historical_diagnostic_only": False,
        "historical_nomination_gate_only": True,
        "independent_confirmation": False,
        "promotion_authority": "FORWARD_NOMINATION_ONLY",
    }


def _apply_population_dsr_gate(report: dict, multiple_testing: dict) -> dict:
    """Use a complete synchronous formula population to calibrate primary DSR.

    The standalone evaluator deliberately starts fail-closed with the legacy
    unit-dispersion DSR.  Only this runner can prove that every declared formula
    was evaluated over the exact same session vector.  A calibrated historical
    DSR may clear the historical nomination gate; the independent forward
    lockbox remains mandatory before any QA/promotion decision.
    """
    if multiple_testing.get(
            "complete_synchronous_formula_vectors_available") is not True:
        return report
    calibrated = multiple_testing.get("observed_population_dsr") or {}
    summary = report.get("summary") or {}
    if int(summary.get("sessions") or 0) < int(DEFAULT_CRITERIA["min_sessions"]):
        return report
    expected = int(summary.get("trials") or 0)
    selection_trials = int(multiple_testing.get(
        "selection_adjusted_trials_declared") or 0)
    dispersion_count = int(multiple_testing.get(
        "dispersion_sample_count") or 0)
    if (calibrated.get("calibration_mode") not in
            {"observed_trial_sharpes", "observed_trial_sharpe_std",
             "CONSERVATIVE_COHORT_FLOOR_EXTRAPOLATION",
             "CONSERVATIVE_COHORT_FLOOR_CALIBRATION"}
            or int(calibrated.get("trials") or 0) != selection_trials
            or int(multiple_testing.get("observed_formula_trials") or 0)
            != expected
            or dispersion_count != expected
            or dispersion_count < MIN_DSR_DISPERSION_FORMULAS
            or int(multiple_testing.get(
                "structurally_distinct_formula_trials") or 0)
            < MIN_DSR_DISPERSION_FORMULAS
            or selection_trials < expected):
        return report
    required = (
        "deflated_sharpe", "sharpe", "expected_max_sharpe",
        "trial_sharpe_std", "effective_trials")
    if any(not isinstance(calibrated.get(key), (int, float))
           or isinstance(calibrated.get(key), bool)
           or not math.isfinite(float(calibrated[key])) for key in required):
        return report

    summary.update({
        "deflated_sharpe": calibrated["deflated_sharpe"],
        "sharpe": calibrated["sharpe"],
        "dsr_calibration_mode": calibrated["calibration_mode"],
        "dsr_expected_max_sharpe": calibrated["expected_max_sharpe"],
        "dsr_trial_sharpe_std": calibrated["trial_sharpe_std"],
        "dsr_effective_trials": calibrated["effective_trials"],
        "dsr_dispersion_sample_count": dispersion_count,
        "dsr_dispersion_extrapolated": selection_trials > dispersion_count,
        "dsr_dispersion_mode": calibrated["calibration_mode"],
        "dsr_observed_trial_sharpe_std": multiple_testing.get(
            "observed_trial_sharpe_std"),
        "dsr_trial_sharpe_std_floor": multiple_testing.get(
            "trial_sharpe_std_floor"),
        "dsr_historical_vectors_fabricated": False,
        "dsr_raw_count_as_effective_upper_bound": True,
        "selection_adjusted_trials": selection_trials,
        "expected_max_sharpe": calibrated["expected_max_sharpe"],
        "trial_sharpe_std": calibrated["trial_sharpe_std"],
        "effective_trials": calibrated["effective_trials"],
    })
    failed = [code for code in report.get("failed_criteria") or []
              if code not in {"DSR_TRIAL_DISPERSION_UNMEASURED", "OVERFIT_DSR"}]
    if float(calibrated["deflated_sharpe"]) < float(
            DEFAULT_CRITERIA["min_deflated_sharpe"]):
        failed.append("OVERFIT_DSR")
    report["failed_criteria"] = list(dict.fromkeys(failed))
    if report.get("decision") != "NO_EVIDENCE":
        report["decision"] = "SUBMIT_TO_QA" if not failed else "HOLD"
    return report


def _rung_candidate_evidence(
        report: dict, *, observed_at=None,
        search_exposure_fingerprint: str | None = None,
        evidence_scope: str | None = None,
        measurement_scope: str | None = None) -> list[dict]:
    """Compact immutable search outcomes retained after candidates are halved."""
    observed = (_required_aware_iso(observed_at, "observed_at")
                if observed_at is not None else None)
    if (search_exposure_fingerprint is not None
            and (len(search_exposure_fingerprint) != 64
                 or any(char not in "0123456789abcdef"
                        for char in search_exposure_fingerprint))):
        raise RuntimeError("candidate evidence has invalid search exposure hash")
    rows = [("PRIMARY", report)] + [
        (str(row.get("ast_fingerprint") or ""), row)
        for row in report.get("screening_population") or []]
    return [{
        "candidate": key,
        "summary": {name: (row.get("summary") or {}).get(name)
                    for name in ("sessions", "opportunities", "sharpe",
                                 "deflated_sharpe",
                                 "mean_net_bps_per_opportunity",
                                 "session_net_ci_low_bps",
                                 "session_net_ci_high_bps")},
        "session_returns_bps": row.get("session_returns_bps") or {},
        "decision": row.get("decision"),
        "failed_criteria": list(row.get("failed_criteria") or []),
        "adaptive_failure_memory": row.get("adaptive_failure_memory") or {},
        "search_objectives": _search_objective_payload(row),
        "observed_at": observed,
        "search_exposure_fingerprint": search_exposure_fingerprint,
        "evidence_scope": evidence_scope,
        "measurement_scope": measurement_scope,
        "adaptive_search_only": True,
        "promotion_authority": False,
    } for key, row in rows]


def _completed_adaptive_rung_evidence(
        report: dict, *, screen: dict, config: dict, spec: IntradayLaneSpec,
        selected: dict, schedule_row: dict, exposure_manifest: dict,
        dataset_id: str, dataset_cutoff, event_source: str,
        source_lineage: list[dict], completed_at) -> dict:
    """Bind measured outcomes to one exact, non-promoting search exposure."""
    completion = _required_aware_iso(completed_at, "completed_at")
    search_exposure = _adaptive_rung_search_exposure(
        config=config, spec=spec, selected=selected,
        schedule_row=schedule_row, exposure_manifest=exposure_manifest,
        screen=screen, dataset_id=dataset_id, dataset_cutoff=dataset_cutoff,
        event_source=event_source, source_lineage=source_lineage)
    rung_name = str(screen.get("rung") or "")
    evidence_scope = {
        DISCOVERY_6: "F1",
        VALIDATION_20: "F2",
    }.get(rung_name)
    if evidence_scope is None:
        raise RuntimeError("completed adaptive evidence has an unknown rung")
    exposure_fp = search_exposure["search_exposure_fingerprint"]
    measurement_scope = search_exposure["evaluation"]["measurement_scope"]
    return {
        **screen,
        "candidate_count": 1 + len(config.get("screening_population") or []),
        "candidate_evidence": _rung_candidate_evidence(
            report, observed_at=completion,
            search_exposure_fingerprint=exposure_fp,
            evidence_scope=evidence_scope,
            measurement_scope=measurement_scope),
        "multiple_testing": report.get("multiple_testing"),
        "search_exposure": search_exposure,
        "search_exposure_fingerprint": exposure_fp,
        "completed_at": completion,
        "completion_clock": "UTC_WALL_CLOCK_AFTER_EVALUATOR_RETURN",
        "adaptive_search_only": True,
        "promotion_authority": False,
    }


def _search_objective_payload(report: dict) -> dict:
    """Return a versioned, no-imputation quality vector for adaptive search."""
    summary = report.get("summary") or {}
    raw = {
        "cost_net_bps": summary.get("mean_net_bps_per_opportunity"),
        "oos_sharpe": summary.get("sharpe"),
        "coverage_ratio": summary.get("instrument_coverage"),
        "robustness_score": summary.get("positive_fold_ratio"),
        "novelty_score": report.get("search_structural_novelty"),
        "complexity_nodes": report.get("complexity_nodes"),
    }
    missing = []
    values = {}
    for name, value in raw.items():
        if name == "complexity_nodes":
            valid = (isinstance(value, int) and not isinstance(value, bool)
                     and value > 0)
            if valid:
                values[name] = int(value)
        else:
            valid = (isinstance(value, (int, float))
                     and not isinstance(value, bool)
                     and math.isfinite(float(value)))
            if valid and name in {
                    "coverage_ratio", "robustness_score", "novelty_score"}:
                valid = 0.0 <= float(value) <= 1.0
            if valid:
                values[name] = float(value)
        if not valid:
            missing.append(name)
    sessions = summary.get("sessions")
    opportunities = summary.get("opportunities")
    for name, value in (("sessions", sessions),
                        ("opportunities", opportunities)):
        if (not isinstance(value, (int, float)) or isinstance(value, bool)
                or not math.isfinite(float(value)) or float(value) <= 0):
            missing.append(name)
    complete = not missing
    return {
        "version": SEARCH_OBJECTIVES_VERSION,
        "complete": complete,
        "values": values if complete else {
            key: values[key] for key in sorted(values)},
        "missing": sorted(set(missing)),
        "sessions": (int(sessions) if isinstance(sessions, (int, float))
                     and not isinstance(sessions, bool)
                     and math.isfinite(float(sessions))
                     and float(sessions).is_integer() else None),
        "opportunities": (
            int(opportunities) if isinstance(opportunities, (int, float))
            and not isinstance(opportunities, bool)
            and math.isfinite(float(opportunities))
            and float(opportunities).is_integer() else None),
        "imputation": "NONE",
        "adaptive_search_only": True,
        "promotion_authority": False,
    }


def _next_rung_config(config: dict, report: dict, gate: dict, *,
                      candidate_budget: int) -> tuple[dict, dict]:
    """Deterministically retain linked candidates for the next search rung."""
    source = {row["ast_fingerprint"]: row
              for row in config.get("screening_population") or []}
    survivors = set(str(value) for value in gate.get("survivors") or [])
    ranked = [row for row in report.get("screening_population") or []
              if row.get("ast_fingerprint") in survivors]

    def finite(value, default):
        return (float(value) if isinstance(value, (int, float))
                and not isinstance(value, bool) and math.isfinite(float(value))
                else default)

    ranked.sort(key=lambda row: (
        int(row.get("pareto_rank") or 1_000_000),
        -int(bool((row.get("residual_qd") or {}).get("elite"))),
        -finite((row.get("summary") or {}).get(
            "mean_net_bps_per_opportunity"), float("-inf")),
        int(row.get("complexity_nodes") or 1_000_000),
        str(row.get("ast_fingerprint") or ""),
    ))
    input_count = 1 + len(source)
    eta = int(config.get("successive_halving_eta") or 3)
    eta_budget = max(1, math.ceil(input_count / eta))
    applied_budget = min(int(candidate_budget), eta_budget)
    linked_budget = max(0, applied_budget - 1)
    chosen_keys = [str(row["ast_fingerprint"])
                   for row in ranked[:linked_budget]]
    next_config = {
        **config,
        "screening_population": [source[key] for key in chosen_keys],
        # Keep the original exposure count; eliminated candidates still count.
        "screening_trial_exposure": max(
            int(config.get("screening_trial_exposure") or 0), len(source)),
    }
    manifest = {
        "version": "intraday-successive-halving-v1",
        "eta": eta,
        "eta_derived_budget_including_primary": eta_budget,
        "candidate_budget_cap_including_primary": int(candidate_budget),
        "candidate_budget_including_primary": applied_budget,
        "input_candidates_including_primary": input_count,
        "selected_candidates_including_primary": 1 + len(chosen_keys),
        "selected_linked_ast_fingerprints": chosen_keys,
        "eliminated_linked_ast_fingerprints": sorted(set(source) - set(chosen_keys)),
        "selection_inputs": [
            "futility survivor", "pareto rank", "residual QD elite",
            "cost-net point estimate", "complexity"],
        "futility_and_resource_allocation_only": True,
        "promotion_authority": False,
    }
    return next_config, manifest


def _compact_text(value, *, limit: int = 96) -> str | None:
    if value is None:
        return None
    return str(value)[:limit]


def _compact_score_calibration_dimensions(
        calibration: dict, *, screening_candidate: str | None = None) -> dict:
    beta = calibration.get("beta_bps_per_score_unit")
    if (not isinstance(beta, (int, float)) or isinstance(beta, bool)
            or not math.isfinite(float(beta))):
        beta = None
    dimensions = {
        "calibration": True,
        "score_calibration_fingerprint": stable_fingerprint(calibration),
        "status": _compact_text(calibration.get("status")),
        "coefficient_policy": _compact_text(
            calibration.get("coefficient_policy")),
        "beta_bps_per_score_unit": (
            float(beta) if beta is not None else None),
    }
    if screening_candidate is not None:
        dimensions["screening_candidate"] = str(screening_candidate)
    return dimensions


def _compact_residual_dimensions(
        residual: dict, residual_qd: dict, *,
        screening_candidate: str | None = None) -> dict:
    median_mae = residual.get("median_time_bucket_mae_bps")
    if (not isinstance(median_mae, (int, float))
            or isinstance(median_mae, bool)
            or not math.isfinite(float(median_mae))):
        median_mae = None
    dimensions = {
        "residual_artifact_fingerprint": stable_fingerprint({
            "residual_behavior": residual,
            "residual_qd": residual_qd,
        }),
        "version": _compact_text(residual.get("version")),
        "status": _compact_text(residual.get("status")),
        "worst_time_bucket": _compact_text(
            residual.get("worst_time_bucket")),
        "median_time_bucket_mae_bps": (
            float(median_mae) if median_mae is not None else None),
        "residual_qd_cell": _compact_text(residual_qd.get("cell")),
        "residual_qd_elite": bool(residual_qd.get("elite")),
    }
    if screening_candidate is not None:
        dimensions["screening_candidate"] = str(screening_candidate)
    return dimensions


def _indexed_dimensions_json(dimensions: dict) -> str:
    encoded = json.dumps(
        dimensions, sort_keys=True, separators=(",", ":"), default=str)
    size = len(encoded.encode("utf-8"))
    if size > MAX_INDEXED_DIMENSIONS_JSON_BYTES:
        raise RuntimeError(
            "experiment metric dimensions exceed the compact B-tree budget: "
            f"{size}>{MAX_INDEXED_DIMENSIONS_JSON_BYTES} bytes")
    return encoded


def _intraday_evaluation_identity(report: dict) -> dict:
    """Compact exact window/content identity for synchronous PBO rows."""
    exposures = [row for row in
                 ((report.get("trial_lockbox") or {}).get("exposures") or [])
                 if row.get("rung") != CALIBRATION]
    full = next((row for row in exposures if row.get("rung") == FULL_60), None)
    selected = full or (exposures[-1] if exposures else {})
    sessions = list(selected.get("sessions") or [])
    boundary_rows = [{
        "session": row.get("session"),
    } for row in sessions]
    content_rows = [{
        "session": row.get("session"),
        "session_content_fingerprint": row.get("session_content_fingerprint"),
    } for row in sessions]
    primary_folds = [{
        "fold": fold.get("fold"),
        "window": f"INTRADAY_FOLD_{fold.get('fold')}",
        "start_session": fold.get("start_session"),
        "end_session": fold.get("end_session"),
        "sessions": fold.get("sessions"),
    } for fold in (report.get("folds") or [])]
    scope = str(selected.get("rung") or "UNALLOCATED")
    session_values = [row.get("session") for row in sessions]
    session_set_fingerprint = selected.get("session_set_fingerprint")
    instrument_count = int(selected.get("instrument_count") or 0)
    instrument_ids_fingerprint = selected.get(
        "instrument_ids_fingerprint")
    rung_plan_fingerprint = selected.get("rung_plan_fingerprint")
    experiment_rung_id = selected.get("experiment_rung_id")
    fold_ranges = []
    if session_values and primary_folds and all(
            all(value is not None and value != "" for value in row.values())
            for row in primary_folds):
        try:
            fold_ranges = [(
                session_values.index(row["start_session"]),
                session_values.index(row["end_session"]),
                int(row["sessions"]),
            ) for row in primary_folds]
        except (ValueError, TypeError, OverflowError):
            fold_ranges = []
    folds_complete = bool(
        fold_ranges
        and len(fold_ranges) == 4
        and [row["fold"] for row in primary_folds] ==
            list(range(1, len(primary_folds) + 1))
        and len({row["window"] for row in primary_folds}) ==
            len(primary_folds)
        and fold_ranges[0][0] == 0
        and fold_ranges[-1][1] == len(session_values) - 1
        and all(declared == 15 for _start, _end, declared in fold_ranges)
        and all(start <= end and declared == end - start + 1
                for start, end, declared in fold_ranges)
        and all(right[0] == left[1] + 1
                for left, right in zip(fold_ranges, fold_ranges[1:])))
    primary_fold_set_fingerprint = stable_fingerprint(primary_folds)
    complete = bool(
        scope == FULL_60
        and len(sessions) == 60
        and len(set(session_values)) == 60
        and session_values == sorted(session_values)
        and stable_fingerprint(session_values) == session_set_fingerprint
        and int((report.get("summary") or {}).get("sessions") or 0) == 60
        and instrument_count > 0
        and bool(instrument_ids_fingerprint)
        and bool(rung_plan_fingerprint)
        and bool(experiment_rung_id)
        and folds_complete
        and all(row["session"] and row["session_content_fingerprint"]
                for row in content_rows))
    boundary_fingerprint = stable_fingerprint(boundary_rows)
    content_fingerprint = stable_fingerprint(content_rows)
    identity = {
        "evaluation_scope": scope,
        "evaluation_identity_complete": complete,
        "session_boundary_fingerprint": boundary_fingerprint,
        "session_set_fingerprint": session_set_fingerprint,
        "source_content_fingerprint": content_fingerprint,
        "instrument_count": instrument_count,
        "instrument_ids_fingerprint": instrument_ids_fingerprint,
        "experiment_rung_id": experiment_rung_id,
        "rung_plan_fingerprint": rung_plan_fingerprint,
        "primary_fold_count": len(primary_folds),
        "primary_fold_set_fingerprint": primary_fold_set_fingerprint,
        "cost_model_version": COST_MODEL_VERSION,
    }
    identity["evaluation_fingerprint"] = stable_fingerprint(identity)
    return identity


def _load_authoritative_forward_revision(meta_conn, experiment_id: str
                                         ) -> dict | None:
    """Return the latest append-only forward revision, when one exists."""
    with meta_conn.cursor() as cur:
        cur.execute("""
            select report
              from quant.intraday_forward_report_revisions
             where experiment_id=%s::uuid
             order by revision_number desc
             limit 1
        """, (experiment_id,))
        row = cur.fetchone()
    return _as_json(row[0]) if row is not None else None


def _load_completed_report(meta_conn, experiment_id: str) -> dict:
    """Rehydrate enough immutable evidence for an idempotent orchestrator retry."""
    authoritative = _load_authoritative_forward_revision(
        meta_conn, experiment_id)
    if authoritative is not None:
        return authoritative
    with meta_conn.cursor() as cur:
        cur.execute("select config from quant.experiments where experiment_id=%s",
                    (experiment_id,))
        config = _as_json(cur.fetchone()[0])
        cur.execute("""
            select metric, value, dimensions
              from quant.experiment_metrics
             where experiment_id=%s and split='WALK_FORWARD'
             order by metric, dimensions::text
        """, (experiment_id,))
        rows = cur.fetchall()
        cur.execute("""
            select report
              from quant.intraday_report_manifests
             where experiment_id=%s
        """, (experiment_id,))
        manifest_row = cur.fetchone()
    summary, folds = {}, {}
    screening_summaries: dict[str, dict] = {}
    screening_folds: dict[str, dict[int, dict]] = {}
    screening_meta: dict[str, dict] = {}
    final_dimensions = None
    pre_dimensions = None
    calibration_dimensions = None
    residual_dimensions = None
    primary_residual_qd = None
    governance_dimensions = None
    for metric, value, raw_dimensions in rows:
        dimensions = _as_json(raw_dimensions)
        screening_key = dimensions.get("screening_candidate")
        if dimensions.get("summary") is True and not screening_key:
            summary[metric] = float(value)
        elif dimensions.get("summary") is True and screening_key:
            screening_summaries.setdefault(str(screening_key), {})[metric] = \
                float(value)
        if (metric == "fold_mean_net_bps" and "fold" in dimensions
                and not screening_key):
            fold = int(dimensions["fold"])
            folds.setdefault(fold, {"fold": fold,
                                    "start_session": dimensions.get("start_session"),
                                    "end_session": dimensions.get("end_session")})
            folds[fold]["mean_net_bps"] = float(value)
        elif (metric == "fold_mean_net_bps" and "fold" in dimensions
              and screening_key):
            fold = int(dimensions["fold"])
            target = screening_folds.setdefault(str(screening_key), {})
            target.setdefault(fold, {
                "fold": fold,
                "start_session": dimensions.get("start_session"),
                "end_session": dimensions.get("end_session"),
            })["mean_net_bps"] = float(value)
        if metric == "intraday_screening_result" and screening_key:
            screening_meta.setdefault(str(screening_key), {}).update(dimensions)
        if metric == "intraday_score_calibration" and not screening_key:
            calibration_dimensions = dimensions
        elif metric == "intraday_score_calibration" and screening_key:
            screening_meta.setdefault(str(screening_key), {})[
                "score_calibration"] = dimensions
        if metric == "intraday_residual_behavior":
            behavior = {key: value for key, value in dimensions.items()
                        if key not in {"screening_candidate", "residual_qd"}}
            if screening_key:
                target = screening_meta.setdefault(str(screening_key), {})
                target["residual_behavior"] = behavior
                target["residual_qd"] = dimensions.get("residual_qd")
            else:
                residual_dimensions = behavior
                primary_residual_qd = dimensions.get("residual_qd")
        if metric == "intraday_pre_pbo_gate_pass":
            pre_dimensions = dimensions
        elif metric == "intraday_gate_pass":
            final_dimensions = dimensions
        elif metric == "intraday_governance_manifest":
            governance_dimensions = dimensions
    governance = (_as_json(manifest_row[0]) if manifest_row else
                  governance_dimensions or {})
    calibration_dimensions = (
        governance.get("score_calibration") or calibration_dimensions)
    residual_dimensions = (
        governance.get("residual_behavior") or residual_dimensions)
    primary_residual_qd = (
        governance.get("residual_qd") or primary_residual_qd)
    screening_artifacts = governance.get("screening_candidates") or {}
    if not isinstance(screening_artifacts, dict):
        screening_artifacts = {}
    gate = final_dimensions or pre_dimensions or {}
    if not final_dimensions:
        gate = governance.get("pre_pbo_gate") or gate
    expression = config.get("intraday_signal_expr") or {"const": 0, "unit": "RATIO"}
    parsed = parse_expr(expression)
    from intraday_alpha_ast import fields_of, fingerprint, shape_fingerprint

    screening_reports = []
    for candidate in config.get("screening_population") or []:
        key = str(candidate.get("ast_fingerprint") or "")
        # Successive halving deliberately omits eliminated candidates from
        # FULL_60 metric rows. Their earlier evidence remains in the immutable
        # discovery manifest, so do not fabricate empty full-run reports here.
        if (key not in screening_summaries and key not in screening_folds
                and key not in screening_meta and key not in screening_artifacts):
            continue
        durable_meta = screening_artifacts.get(key) or {}
        if not isinstance(durable_meta, dict):
            durable_meta = {}
        # The append-only manifest is authoritative. Compact metric dimensions
        # remain a backward-compatible index/query projection only.
        meta = {**screening_meta.get(key, {}), **durable_meta}
        screening_reports.append({
            "evaluator_version": _evaluator_version(config),
            "ast_fingerprint": key,
            "summary": screening_summaries.get(key, {}),
            "lane_manifest": {
                "coefficient_policy": candidate.get("coefficient_policy"),
                "score_calibration": meta.get("score_calibration"),
            },
            "folds": [screening_folds.get(key, {})[fold]
                      for fold in sorted(screening_folds.get(key, {}))],
            "decision": "SCREENING_ONLY",
            "screening_only": True,
            "evidence_tier": "SCREENING_ONLY",
            "screening_gate_decision": meta.get("screening_gate_decision"),
            "failed_criteria": list(meta.get("failed_criteria") or []),
            "candidate_role": candidate.get("candidate_role"),
            "source_lead_ids": list(candidate.get("source_lead_ids") or []),
            "ablation_operator": candidate.get("ablation_operator"),
            "ablation_path": candidate.get("ablation_path"),
            "ablation_of_ast_fingerprint": candidate.get(
                "ablation_of_ast_fingerprint"),
            "ablation_version": candidate.get("ablation_version"),
            "empirical_influence": meta.get("empirical_influence"),
            "pareto_rank": meta.get("pareto_rank"),
            "pareto_front": meta.get("pareto_front"),
            "residual_behavior": meta.get("residual_behavior") or {},
            "residual_qd": meta.get("residual_qd") or {},
            "novelty_vs_primary": meta.get("novelty_vs_primary"),
            "complexity_nodes": meta.get("complexity_nodes"),
            "search_structural_novelty": meta.get(
                "search_structural_novelty"),
            "search_objectives": meta.get("search_objectives") or {},
            "adaptive_failure_memory": meta.get(
                "adaptive_failure_memory") or {},
            "idempotent_replay": True,
        })
    report = {
        "evaluator_version": _evaluator_version(config),
        "ast_fingerprint": fingerprint(parsed),
        "ast_shape_fingerprint": shape_fingerprint(parsed),
        "fields": sorted(fields_of(parsed)),
        "lane_manifest": config.get("lane_manifest") or {},
        "causality": {"rehydrated": True},
        "folds": [folds[key] for key in sorted(folds)],
        "session_returns_bps": {},
        "summary": summary,
        "score_calibration": calibration_dimensions,
        "residual_behavior": residual_dimensions or {},
        "residual_qd": primary_residual_qd or {},
        "search_structural_novelty": governance.get(
            "primary_search_structural_novelty"),
        "complexity_nodes": governance.get("primary_complexity_nodes"),
        "search_objectives": governance.get("primary_search_objectives") or {},
        "adaptive_failure_memory": governance.get(
            "primary_adaptive_failure_memory") or {},
        "screening_population": screening_reports,
        "supervised_control": governance.get("supervised_control") or {},
        "hybrid_control": governance.get("hybrid_control") or {},
        "multiple_testing": governance.get("multiple_testing") or {},
        "trial_lockbox": governance.get("trial_lockbox") or {},
        "discovery_rungs": governance.get("discovery_rungs") or [],
        "evidence_tier": governance.get("evidence_tier"),
        "forward_lockbox": governance.get("forward_lockbox") or {},
        "reproduction_runtime": governance.get("reproduction_runtime") or {},
        "population_evaluation": {
            "shared_raw_replay": True,
            "candidate_count": 1 + len(screening_reports),
            "residual_archive_version": "krx-domain-residual-qd-v1",
            "residual_archive_boundary": "OOS_DIAGNOSTIC_SCREENING_ONLY",
            "promotion_authority": "PRIMARY_ONLY",
        },
        "failed_criteria": list(gate.get("failed_criteria") or []),
        "decision": gate.get("decision") or "HOLD",
        "not_a_promotion": (
            "SUBMIT_TO_QA is a review request; Risk, QA, and CEO retain promotion authority"),
        "slice": config.get("slice") or {},
        "source_quality": [],
        "idempotent_replay": True,
    }
    confirmation = _load_forward_confirmation_status(
        meta_conn, experiment_id)
    return _overlay_forward_confirmation(report, confirmation)


def _store_report(meta_conn, experiment_id: str, report: dict) -> None:
    summary = report.get("summary") or {}
    evaluation_identity = _intraday_evaluation_identity(report)
    rows = [(key, value, {"summary": True}) for key, value in summary.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)]
    calibration = report.get("score_calibration") or {}
    rows.append(("intraday_score_calibration",
                 1 if calibration.get("status") in {
                     "PASS", "NOT_REQUIRED_FIXED_EQUATION"} else 0,
                 _compact_score_calibration_dimensions(calibration)))
    residual = report.get("residual_behavior") or {}
    primary_residual_qd = report.get("residual_qd") or {}
    residual_value = residual.get("median_time_bucket_mae_bps")
    rows.append(("intraday_residual_behavior",
                 float(residual_value) if isinstance(
                     residual_value, (int, float))
                 and not isinstance(residual_value, bool) else 0.0,
                 _compact_residual_dimensions(
                     residual, primary_residual_qd)))
    for fold in report.get("folds") or []:
        if isinstance(fold.get("mean_net_bps"), (int, float)):
            rows.append(("fold_mean_net_bps", fold["mean_net_bps"],
                         {"fold": fold["fold"], "start_session": fold["start_session"],
                          "end_session": fold["end_session"]}))
            rows.append(("total_return", fold["mean_net_bps"] / 10_000.0,
                         {**evaluation_identity,
                          "window": f"INTRADAY_FOLD_{fold['fold']}",
                           "start_session": fold["start_session"],
                           "end_session": fold["end_session"]}))
    screening_artifacts: dict[str, dict] = {}
    for candidate in report.get("screening_population") or []:
        key = candidate["ast_fingerprint"]
        candidate_summary = candidate.get("summary") or {}
        for metric, value in candidate_summary.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rows.append((metric, value, {
                    "summary": True, "screening_candidate": key}))
        for fold in candidate.get("folds") or []:
            if isinstance(fold.get("mean_net_bps"), (int, float)):
                rows.append(("fold_mean_net_bps", fold["mean_net_bps"], {
                    "screening_candidate": key,
                    "fold": fold["fold"],
                    "start_session": fold["start_session"],
                    "end_session": fold["end_session"],
                }))
                rows.append(("total_return", fold["mean_net_bps"] / 10_000.0, {
                    **evaluation_identity,
                    "screening_candidate": key,
                    "window": f"INTRADAY_FOLD_{fold['fold']}",
                    "start_session": fold["start_session"],
                    "end_session": fold["end_session"],
                }))
        screening_result = {
            "screening_gate_decision": candidate.get(
                "screening_gate_decision"),
            "failed_criteria": candidate.get("failed_criteria") or [],
            "pareto_rank": candidate.get("pareto_rank"),
            "pareto_front": bool(candidate.get("pareto_front")),
            "novelty_vs_primary": candidate.get("novelty_vs_primary"),
            "complexity_nodes": candidate.get("complexity_nodes"),
            "source_lead_ids": candidate.get("source_lead_ids") or [],
            "candidate_role": candidate.get("candidate_role"),
            "empirical_influence": candidate.get("empirical_influence"),
            "search_structural_novelty": candidate.get(
                "search_structural_novelty"),
            "search_objectives": _search_objective_payload(candidate),
            "adaptive_failure_memory": candidate.get(
                "adaptive_failure_memory") or {},
        }
        rows.append(("intraday_screening_result",
                     1 if candidate.get("pareto_front") else 0, {
                         "screening_candidate": key,
                         "screening_only": True,
                         "screening_result_fingerprint": stable_fingerprint(
                             screening_result),
                         "screening_gate_decision": _compact_text(
                             screening_result["screening_gate_decision"]),
                         "pareto_rank": screening_result["pareto_rank"],
                         "pareto_front": screening_result["pareto_front"],
                         "novelty_vs_primary": screening_result[
                             "novelty_vs_primary"],
                         "complexity_nodes": screening_result[
                             "complexity_nodes"],
                     }))
        candidate_calibration = ((candidate.get("lane_manifest") or {}).get(
            "score_calibration") or {})
        rows.append(("intraday_score_calibration",
                     1 if candidate_calibration.get("status") in {
                         "PASS", "NOT_REQUIRED_FIXED_EQUATION"} else 0,
                     _compact_score_calibration_dimensions(
                         candidate_calibration, screening_candidate=key)))
        candidate_residual = candidate.get("residual_behavior") or {}
        candidate_residual_qd = candidate.get("residual_qd") or {}
        candidate_residual_value = candidate_residual.get(
            "median_time_bucket_mae_bps")
        screening_artifacts[str(key)] = {
            **screening_result,
            "score_calibration": candidate_calibration,
            "residual_behavior": candidate_residual,
            "residual_qd": candidate_residual_qd,
        }
        rows.append(("intraday_residual_behavior",
                     float(candidate_residual_value) if isinstance(
                         candidate_residual_value, (int, float))
                     and not isinstance(candidate_residual_value, bool) else 0.0,
                     _compact_residual_dimensions(
                         candidate_residual, candidate_residual_qd,
                         screening_candidate=key)))
    pre_pbo_gate = {
        "decision": report.get("decision"),
        "failed_criteria": report.get("failed_criteria") or [],
    }
    rows.append(("intraday_pre_pbo_gate_pass",
                 1 if report.get("decision") == "SUBMIT_TO_QA" else 0,
                 {"decision": _compact_text(report.get("decision")),
                  "failed_criteria_count": len(
                      pre_pbo_gate["failed_criteria"]),
                  "gate_fingerprint": stable_fingerprint(pre_pbo_gate)}))
    governance = {
        "evidence_tier": report.get("evidence_tier"),
        "score_calibration": calibration,
        "residual_behavior": residual,
        "residual_qd": primary_residual_qd,
        "primary_search_structural_novelty": report.get(
            "search_structural_novelty"),
        "primary_complexity_nodes": report.get("complexity_nodes"),
        "primary_search_objectives": _search_objective_payload(report),
        "primary_adaptive_failure_memory": report.get(
            "adaptive_failure_memory") or {},
        "screening_candidates": screening_artifacts,
        "pre_pbo_gate": pre_pbo_gate,
        "supervised_control": report.get("supervised_control") or {},
        "hybrid_control": report.get("hybrid_control") or {},
        "multiple_testing": report.get("multiple_testing") or {},
        "trial_lockbox": report.get("trial_lockbox") or {},
        "evaluation_identity": evaluation_identity,
        "primary_folds": [{
            "fold": fold.get("fold"),
            "window": f"INTRADAY_FOLD_{fold.get('fold')}",
            "start_session": fold.get("start_session"),
            "end_session": fold.get("end_session"),
            "sessions": fold.get("sessions"),
        } for fold in (report.get("folds") or [])],
        "discovery_rungs": report.get("discovery_rungs") or [],
        "forward_lockbox": report.get("forward_lockbox") or {},
        "reproduction_runtime": report.get("reproduction_runtime") or {},
        "product_filter": (report.get("slice") or {}).get("product_filter"),
        "product_filter_version": (report.get("slice") or {}).get(
            "product_filter_version"),
    }
    governance_fingerprint = stable_fingerprint(governance)
    rows.append(("intraday_governance_manifest", 1.0, {
        "manifest": True,
        "manifest_version": REPORT_MANIFEST_VERSION,
        "report_fingerprint": governance_fingerprint,
    }))
    try:
        with meta_conn.cursor() as cur:
            cur.execute("""
                insert into quant.intraday_report_manifests
                  (experiment_id, report_fingerprint, manifest_version,
                   report, created_by)
                values (%s,%s,%s,%s::jsonb,
                        'svc_quant/intraday-experiment-runner')
                on conflict (experiment_id) do nothing
                returning report_fingerprint
            """, (experiment_id, governance_fingerprint,
                  REPORT_MANIFEST_VERSION,
                  json.dumps(governance, default=str)))
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute("""
                    select report_fingerprint
                      from quant.intraday_report_manifests
                     where experiment_id=%s
                """, (experiment_id,))
                existing = cur.fetchone()
                if existing is None or str(existing[0]) != governance_fingerprint:
                    raise RuntimeError(
                        "immutable intraday report manifest conflicts with retry")
            for metric, value, dimensions in rows:
                cur.execute("""
                    insert into quant.experiment_metrics
                      (experiment_id, split, metric, value, dimensions, cost_model_version)
                    values (%s,'WALK_FORWARD',%s,%s,%s::jsonb,%s)
                    on conflict (experiment_id, split, metric, dimensions)
                    do update set value=excluded.value
                """, (experiment_id, metric, value,
                      _indexed_dimensions_json(dimensions),
                      COST_MODEL_VERSION))
            cur.execute("update quant.experiments set status='COMPLETED', ended_at=now() where experiment_id=%s",
                        (experiment_id,))
        meta_conn.commit()
    except Exception:
        meta_conn.rollback()
        raise


def persist_final_gate(meta_conn, experiment_id: str, report: dict) -> None:
    """Persist the post-ledger PBO decision exactly once as the release evidence."""
    dimensions = {"decision": report.get("decision"),
                  "failed_criteria": report.get("failed_criteria") or []}
    pbo = (report.get("summary") or {}).get("pbo")
    with meta_conn.cursor() as cur:
        cur.execute("""
            insert into quant.experiment_metrics
              (experiment_id, split, metric, value, dimensions, cost_model_version)
            values (%s,'WALK_FORWARD','intraday_gate_pass',%s,%s::jsonb,%s)
            on conflict (experiment_id, split, metric, dimensions)
            do update set value=excluded.value
        """, (experiment_id,
              1 if report.get("decision") == "SUBMIT_TO_QA" else 0,
              json.dumps(dimensions), COST_MODEL_VERSION))
        if pbo is not None:
            cur.execute("""
                insert into quant.experiment_metrics
                  (experiment_id, split, metric, value, dimensions, cost_model_version)
                values (%s,'WALK_FORWARD','pbo',%s,'{"summary":true}'::jsonb,%s)
                on conflict (experiment_id, split, metric, dimensions)
                do update set value=excluded.value
            """, (experiment_id, pbo, COST_MODEL_VERSION))
    meta_conn.commit()


def _is_forward_nominee(final_gate: dict | None) -> bool:
    """Only a FULL_60 candidate whose sole open gate is forward may enter."""
    gate = _as_json(final_gate)
    failures = {str(value) for value in gate.get("failed_criteria") or []}
    return (str(gate.get("decision") or "").upper() == "HOLD"
            and failures == {"INDEPENDENT_FORWARD_CONFIRMATION_PENDING"})


def _load_forward_confirmation_status(meta_conn, experiment_id: str
                                      ) -> dict | None:
    with meta_conn.cursor() as cur:
        cur.execute("""
            select confirmation.forward_confirmation_id::text,
                   confirmation.experiment_rung_id::text,
                   confirmation.candidate_lineage_id::text,
                   confirmation.decision,
                   confirmation.confirmation_evidence_fingerprint,
                   confirmation.gate_statistics,
                   confirmation.gate_failures,
                   confirmation.decision_reason,
                   confirmation.forward_start_session_date,
                   confirmation.forward_end_session_date,
                   confirmation.forward_session_count,
                   confirmation.confirmed_at
              from quant.intraday_forward_confirmations confirmation
              join quant.intraday_experiment_rungs rung
                on rung.experiment_rung_id = confirmation.experiment_rung_id
             where rung.experiment_id = %s::uuid
             limit 1
        """, (experiment_id,))
        row = cur.fetchone()
    if hasattr(meta_conn, "rollback"):
        meta_conn.rollback()
    if row is None or len(row) < 12:
        return None
    return {
        "forward_confirmation_id": str(row[0]),
        "experiment_rung_id": str(row[1]),
        "candidate_lineage_id": str(row[2]),
        "decision": str(row[3]),
        "evidence_fingerprint": str(row[4]),
        "gate_statistics": _as_json(row[5]),
        "gate_failures": list(_as_json(row[6]) or []),
        "decision_reason": str(row[7]),
        "start_session": row[8].isoformat(),
        "end_session": row[9].isoformat(),
        "session_count": int(row[10]),
        "confirmed_at": row[11].isoformat(),
    }


def _overlay_forward_confirmation(report: dict, confirmation: dict | None
                                  ) -> dict:
    if not confirmation:
        return report
    out = dict(report)
    decision = str(confirmation["decision"]).upper()
    failures = [str(value) for value in confirmation.get("gate_failures") or []]
    out["experiment_status"] = "COMPLETED_WITH_FORWARD_CONFIRMATION"
    out["evidence_tier"] = "INDEPENDENT_FORWARD_CONFIRMATION"
    out["forward_confirmation"] = dict(confirmation)
    gate_statistics = confirmation.get("gate_statistics") or {}
    asset_contract = {
        "asset_class": "EQUITY",
        "instrument_type": "STOCK",
        "asset_scope": "KRX_ACTIVE_STOCK_ONLY",
        "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "instrument_count": gate_statistics.get("stock_universe_count"),
        "instrument_set_fingerprint": gate_statistics.get(
            "stock_universe_fingerprint"),
        "promotion_authority": False,
    }
    out["asset_contract"] = asset_contract
    out["forward_lockbox"] = {
        **(report.get("forward_lockbox") or {}),
        "status": "CONFIRMED",
        "forward_rung_allocated": True,
        "independent_confirmation": True,
        "confirmation_decision": decision,
        "forward_confirmation_id": confirmation["forward_confirmation_id"],
        "session_count": confirmation["session_count"],
        "historical_sessions_search_exposed": True,
        "legacy_61_sessions_eligible": False,
        "promotion_authority": False,
    }
    historical_failures = [
        str(value) for value in report.get("failed_criteria") or []
        if value != "INDEPENDENT_FORWARD_CONFIRMATION_PENDING"]
    out["failed_criteria"] = list(dict.fromkeys(
        historical_failures + failures))
    out.pop("forward_nomination", None)
    if decision == "PASS" and not out["failed_criteria"]:
        out["decision"] = "SUBMIT_TO_QA"
        out["qa_handoff"] = {
            "status": "REQUESTED",
            "requested_by": FORWARD_GATE_VERSION,
            "forward_confirmation_id": confirmation[
                "forward_confirmation_id"],
            "next_owner": "QA_REPRODUCTION",
            "automatic_promotion": False,
            "promotion_authority": False,
            "asset_contract": asset_contract,
        }
    else:
        # A scientific FAIL is terminal rejection evidence, while a data-
        # inconclusive result remains a hold.  Keep the report, outcome
        # revision, and hypothesis state on one decision mapping.
        out["decision"] = "REJECT" if decision == "FAIL" else "HOLD"
        out["qa_handoff"] = {
            "status": "NOT_REQUESTED",
            "next_owner": "RESEARCH_FEEDBACK",
            "automatic_promotion": False,
            "promotion_authority": False,
            "asset_contract": asset_contract,
        }
    return out


def _forward_outcome_decision(decision: str) -> tuple[str, str, list[str]]:
    verdict = str(decision or "").upper()
    if verdict == "PASS":
        return "SUBMIT_TO_QA", "SUPPORTED", []
    if verdict == "FAIL":
        return "REJECT", "REJECTED", ["FORWARD_CONFIRMATION_FAILED"]
    if verdict == "INCONCLUSIVE":
        return "GATE_HOLD", "INCONCLUSIVE", ["FORWARD_DATA_INCONCLUSIVE"]
    raise ValueError(f"unknown forward decision: {decision}")


def _assert_governed_forward_stock_evidence(
        meta_conn, experiment_id: str) -> None:
    """Re-attest the common stock-evidence contract at a forward boundary.

    Candidate selection and enqueue use the same SQL predicate, but those are
    earlier snapshots.  Reference identity, report completeness, or immutable
    evidence can be missing when a delayed forward confirmation is published.
    A PASS must therefore prove the exact experiment again immediately before
    it can become SUBMIT_TO_QA/SUPPORTED or create a QA handoff.
    """
    with meta_conn.cursor() as cur:
        cur.execute(
            _SQL_ASSERT_GOVERNED_FORWARD_STOCK_EVIDENCE,
            (str(experiment_id),),
        )
        row = cur.fetchone()
    if row is None or len(row) != 1 or row[0] is not True:
        raise RuntimeError(
            "forward PASS requires complete governed KRX ACTIVE STOCK-only "
            "FULL_60 evidence; QA submission and support are blocked")


def _existing_forward_publication(meta_conn, experiment_id: str
                                  ) -> dict | None:
    """Load a fully joined publication so a committed retry is a pure read."""
    with meta_conn.cursor() as cur:
        cur.execute("""
            select report.report_revision_id::text,
                   report.forward_confirmation_id::text,
                   report.report_fingerprint,
                   report.outcome_revision_id::text,
                   report.hypothesis_status,
                   report.decision,
                   outcome.outcome_revision_fingerprint,
                   report.lifecycle_request,
                   handoff.qa_handoff_id::text,
                   hypothesis.status
              from quant.intraday_forward_report_revisions report
              join research.experiment_outcome_revisions outcome
                on outcome.outcome_revision_id = report.outcome_revision_id
              join quant.experiments experiment
                on experiment.experiment_id = report.experiment_id
              join quant.hypotheses hypothesis
                on hypothesis.hypothesis_id = experiment.hypothesis_id
              left join quant.intraday_forward_qa_handoffs handoff
                on handoff.forward_confirmation_id =
                   report.forward_confirmation_id
             where report.experiment_id=%s::uuid
             order by report.revision_number desc
             limit 1
        """, (experiment_id,))
        row = cur.fetchone()
    if hasattr(meta_conn, "rollback"):
        meta_conn.rollback()
    if row is None:
        return None
    actual_status = str(row[9])
    lifecycle_state_ok = (
        actual_status in {str(row[4]), "ARCHIVED"}
        or (str(row[5]) == "PASS" and actual_status in {
            "INCONCLUSIVE", "SUPPORTED", "REJECTED"}))
    if (str(row[5]) == "PASS" and row[8] is None) or not lifecycle_state_ok:
        raise RuntimeError(
            "forward publication is incomplete: QA handoff or hypothesis "
            "lifecycle state is missing")
    return {
        "experiment_id": experiment_id,
        "report_revision_id": str(row[0]),
        "forward_confirmation_id": str(row[1]),
        "report_fingerprint": str(row[2]),
        "outcome_revision_id": str(row[3]),
        "hypothesis_status": actual_status,
        "requested_hypothesis_status": str(row[4]),
        "decision": str(row[5]),
        "outcome_revision_fingerprint": str(row[6]),
        "qa_handoff_requested": str(row[5]) == "PASS",
        "lifecycle": _as_json(row[7]),
        "idempotent_retry": True,
    }


def _publish_forward_finalization(meta_conn, experiment_id: str) -> dict:
    """Atomically append report/outcome revisions and an optional QA request.

    The legacy ``research.experiment_outcomes`` table intentionally remains one
    row per experiment.  Forward evidence is a revision of that row, exposed by
    ``research.v_current_experiment_outcomes`` without making all-row historical
    consumers interpret two contradictory terminal decisions.
    """
    existing = _existing_forward_publication(meta_conn, experiment_id)
    if existing is not None:
        if str(existing.get("decision") or "").upper() == "PASS":
            _assert_governed_forward_stock_evidence(
                meta_conn, experiment_id)
        return existing
    confirmation = _load_forward_confirmation_status(meta_conn, experiment_id)
    if confirmation is None:
        raise RuntimeError("forward publication requires a durable confirmation")
    if str(confirmation.get("decision") or "").upper() == "PASS":
        _assert_governed_forward_stock_evidence(meta_conn, experiment_id)
    report = _load_completed_report(meta_conn, experiment_id)
    if str(confirmation.get("decision") or "").upper() == "PASS":
        runtime_artifact = _forward_runtime_artifact_attestation(report)
        if not runtime_artifact["reproduction_route_available"]:
            raise RuntimeError(
                "PASS forward confirmation cannot be published because its "
                "frozen runtime artifact is unavailable")
    with meta_conn.cursor() as cur:
        cur.execute("""
            select e.hypothesis_id::text,
                   coalesce(e.trial_family_id, ''),
                   coalesce(e.trial_number, 1),
                   coalesce(h.proposal_id::text, ''), h.title, h.status,
                   manifest.report_fingerprint, base.outcome_id
              from quant.experiments e
              join quant.hypotheses h using (hypothesis_id)
              join quant.intraday_report_manifests manifest
                using (experiment_id)
              left join lateral (
                select outcome_id
                  from research.experiment_outcomes outcome
                 where outcome.experiment_id = e.experiment_id::text
                 order by outcome.decided_at desc, outcome.created_at desc,
                          outcome.outcome_id desc
                 limit 1
              ) base on true
             where e.experiment_id=%s::uuid
        """, (experiment_id,))
        metadata = cur.fetchone()
    if metadata is None:
        raise RuntimeError("forward publication lacks experiment metadata")
    (hypothesis_id, trial_family_id, trial_number, proposal_id, title,
     current_status, base_fingerprint, base_outcome_id) = metadata
    if not base_outcome_id:
        raise RuntimeError(
            "forward publication requires the historical base outcome")
    outcome_decision, hypothesis_status, lesson_codes = \
        _forward_outcome_decision(confirmation["decision"])
    # One confirmation creates exactly one deterministic outcome revision.  A
    # UUID FK gives report publication a verifiable identity, not a loose text
    # label that could silently collide on retry.
    outcome_revision_id = confirmation["forward_confirmation_id"]

    if confirmation["decision"] == "PASS":
        from strategy_lifecycle import evaluate_promotion

        lifecycle_result = evaluate_promotion(
            str(title or hypothesis_id)[:40], current_state="RESEARCH",
            gate_decision="SUBMIT_TO_QA",
            research_lane="INTRADAY_EVENT")
        lifecycle = {
            **lifecycle_result.as_dict(),
            "request_created": bool(lifecycle_result.approved_by_quant),
            "approved_by_quant": bool(lifecycle_result.approved_by_quant),
        }
    else:
        lifecycle = {
            "requested_by": "quant-backtest-department",
            "request_created": False,
            "gate_decision": confirmation["decision"],
            "next_owner": "RESEARCH_FEEDBACK",
            "not_a_promotion": True,
        }

    failures = [str(value) for value in
                confirmation.get("gate_failures") or []]
    outcome_failures = failures or lesson_codes
    outcome_notes = (
        "independent forward confirmation: "
        f"{confirmation.get('decision_reason') or confirmation['decision']}")
    oos_summary = {
        "evidence_tier": "INDEPENDENT_FORWARD_CONFIRMATION",
        "forward_confirmation_id": confirmation["forward_confirmation_id"],
        "confirmation_evidence_fingerprint": confirmation[
            "evidence_fingerprint"],
        "base_report_fingerprint": str(base_fingerprint),
        "decision": confirmation["decision"],
        "gate_statistics": confirmation.get("gate_statistics") or {},
        "qa_handoff": report.get("qa_handoff") or {},
        "lifecycle": lifecycle,
        "asset_contract": {
            "asset_class": "EQUITY",
            "instrument_type": "STOCK",
            "asset_scope": "KRX_ACTIVE_STOCK_ONLY",
            "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
            "instrument_count": (confirmation.get("gate_statistics") or {}).get(
                "stock_universe_count"),
            "instrument_set_fingerprint": (
                confirmation.get("gate_statistics") or {}).get(
                    "stock_universe_fingerprint"),
            "promotion_authority": False,
        },
    }
    outcome_payload = {
        "version": FORWARD_REPORT_REVISION_VERSION,
        "outcome_revision_id": outcome_revision_id,
        "base_outcome_id": str(base_outcome_id),
        "experiment_id": experiment_id,
        "hypothesis_id": str(hypothesis_id),
        "trial_family_id": str(trial_family_id),
        "trial_number": int(trial_number),
        "decision": outcome_decision,
        "decided_at": confirmation["confirmed_at"],
        "proposal_id": str(proposal_id),
        "failed_criteria": outcome_failures,
        "oos_summary": oos_summary,
        "lesson_codes": lesson_codes,
        "notes": outcome_notes,
    }
    outcome_fingerprint = stable_fingerprint(outcome_payload)
    revised_report = {
        **report,
        "asset_contract": oos_summary["asset_contract"],
        "authoritative_revision": {
            "version": FORWARD_REPORT_REVISION_VERSION,
            "revision_number": 1,
            "base_report_fingerprint": str(base_fingerprint),
            "forward_confirmation_id": confirmation[
                "forward_confirmation_id"],
            "outcome_revision_id": outcome_revision_id,
            "outcome_revision_fingerprint": outcome_fingerprint,
            "base_outcome_id": str(base_outcome_id),
            "hypothesis_status": hypothesis_status,
            "append_only": True,
        },
        "lifecycle": lifecycle,
    }
    report_fingerprint = stable_fingerprint(revised_report)
    try:
        with meta_conn.cursor() as cur:
            cur.execute("""
                insert into research.experiment_outcome_revisions (
                  outcome_revision_id, base_outcome_id, experiment_id,
                  forward_confirmation_id, revision_number, decision,
                  decided_at, failed_criteria, oos_summary, lesson_codes,
                  notes, outcome_revision_fingerprint, revised_by
                ) values (
                  %s::uuid, %s, %s::uuid, %s::uuid, 1, %s,
                  %s::timestamptz, %s::text[], %s::jsonb, %s::text[], %s,
                  %s, 'svc_quant/intraday-forward-finalizer'
                )
                on conflict (forward_confirmation_id) do nothing
                returning outcome_revision_id::text,
                          outcome_revision_fingerprint, base_outcome_id,
                          decision
            """, (
                outcome_revision_id, str(base_outcome_id), experiment_id,
                confirmation["forward_confirmation_id"], outcome_decision,
                confirmation["confirmed_at"], outcome_failures,
                json.dumps(oos_summary, default=str), lesson_codes,
                outcome_notes,
                outcome_fingerprint,
            ))
            outcome_row = cur.fetchone()
            if outcome_row is None:
                cur.execute("""
                    select outcome_revision_id::text,
                           outcome_revision_fingerprint, base_outcome_id,
                           decision
                      from research.experiment_outcome_revisions
                     where forward_confirmation_id=%s::uuid
                """, (confirmation["forward_confirmation_id"],))
                outcome_row = cur.fetchone()
            expected_outcome = (
                outcome_revision_id, outcome_fingerprint,
                str(base_outcome_id), outcome_decision)
            if (outcome_row is None
                    or tuple(str(value) for value in outcome_row)
                    != expected_outcome):
                raise RuntimeError(
                    "immutable forward outcome revision conflicts with retry")

            cur.execute("""
                insert into quant.intraday_forward_report_revisions (
                  experiment_id, forward_confirmation_id, revision_number,
                  base_report_fingerprint, report_fingerprint, decision,
                  outcome_revision_id, hypothesis_status, report,
                  lifecycle_request, published_by
                ) values (
                  %s::uuid, %s::uuid, 1, %s, %s, %s, %s::uuid, %s,
                  %s::jsonb, %s::jsonb,
                  'svc_quant/intraday-forward-finalizer'
                )
                on conflict (forward_confirmation_id) do nothing
                returning report_revision_id::text, report_fingerprint,
                          outcome_revision_id::text
            """, (
                experiment_id, confirmation["forward_confirmation_id"],
                str(base_fingerprint), report_fingerprint,
                confirmation["decision"], outcome_revision_id,
                hypothesis_status, json.dumps(revised_report, default=str),
                json.dumps(lifecycle, default=str),
            ))
            report_row = cur.fetchone()
            if report_row is None:
                cur.execute("""
                    select report_revision_id::text, report_fingerprint,
                           outcome_revision_id::text
                      from quant.intraday_forward_report_revisions
                     where forward_confirmation_id=%s::uuid
                """, (confirmation["forward_confirmation_id"],))
                report_row = cur.fetchone()
            if (report_row is None or str(report_row[1]) != report_fingerprint
                    or str(report_row[2]) != outcome_revision_id):
                raise RuntimeError(
                    "immutable forward report revision conflicts with retry")
            report_revision_id = str(report_row[0])

            if confirmation["decision"] == "PASS":
                handoff = {
                    **(revised_report.get("qa_handoff") or {}),
                    "report_revision_id": report_revision_id,
                    "report_fingerprint": report_fingerprint,
                    "outcome_revision_id": outcome_revision_id,
                    "outcome_revision_fingerprint": outcome_fingerprint,
                    "lifecycle": lifecycle,
                }
                cur.execute("""
                    insert into quant.intraday_forward_qa_handoffs (
                      forward_confirmation_id, report_revision_id,
                      experiment_id, request_payload, requested_by
                    ) values (
                      %s::uuid, %s::uuid, %s::uuid, %s::jsonb,
                      'svc_quant/intraday-forward-finalizer'
                    )
                    on conflict (forward_confirmation_id) do nothing
                """, (
                    confirmation["forward_confirmation_id"],
                    report_revision_id, experiment_id,
                    json.dumps(handoff, default=str),
                ))
                cur.execute("""
                    select report_revision_id::text, experiment_id::text,
                           request_payload
                      from quant.intraday_forward_qa_handoffs
                     where forward_confirmation_id=%s::uuid
                """, (confirmation["forward_confirmation_id"],))
                handoff_row = cur.fetchone()
                if (handoff_row is None
                        or str(handoff_row[0]) != report_revision_id
                        or str(handoff_row[1]) != experiment_id
                        or stable_fingerprint(_as_json(handoff_row[2]))
                        != stable_fingerprint(handoff)):
                    raise RuntimeError(
                        "immutable forward QA handoff conflicts with retry")

            # A forward PASS is only a request for independent QA.  Keep the
            # actual hypothesis INCONCLUSIVE until the immutable reproduction
            # verdict arrives; the report column retains the legacy projected
            # status required by the append-only confirmation mapping.
            if confirmation["decision"] == "PASS":
                # Publication is only a request for independent reproduction.
                # Demote even a legacy optimistic SUPPORTED state in the same
                # transaction; ARCHIVED remains monotonic.  Migration 010 also
                # enforces this boundary for older publisher images.
                cur.execute("""
                    update quant.hypotheses
                       set status=%s, status_changed_at=now()
                     where hypothesis_id=%s::uuid
                       and status <> 'ARCHIVED'
                """, ("INCONCLUSIVE", str(hypothesis_id)))
            elif hypothesis_status != "INCONCLUSIVE":
                cur.execute("""
                    update quant.hypotheses
                       set status=%s, status_changed_at=now()
                     where hypothesis_id=%s::uuid
                       and status='INCONCLUSIVE'
                """, (hypothesis_status, str(hypothesis_id)))
            cur.execute("select status from quant.hypotheses "
                        "where hypothesis_id=%s::uuid", (str(hypothesis_id),))
            final_status_row = cur.fetchone()
            final_status = str(final_status_row[0]) if final_status_row else ""
            allowed_statuses = {hypothesis_status, "ARCHIVED"}
            if confirmation["decision"] == "PASS":
                allowed_statuses = {"INCONCLUSIVE", "ARCHIVED"}
            if final_status not in allowed_statuses:
                raise RuntimeError(
                    "forward lifecycle finalization found incompatible "
                    f"hypothesis status: {current_status}->{final_status}")
        meta_conn.commit()
    except Exception:
        meta_conn.rollback()
        raise
    return {
        "experiment_id": experiment_id,
        "forward_confirmation_id": confirmation["forward_confirmation_id"],
        "report_revision_id": report_revision_id,
        "report_fingerprint": report_fingerprint,
        "outcome_revision_id": outcome_revision_id,
        "outcome_revision_fingerprint": outcome_fingerprint,
        "hypothesis_status": final_status,
        "requested_hypothesis_status": hypothesis_status,
        "decision": confirmation["decision"],
        "qa_handoff_requested": confirmation["decision"] == "PASS",
        "lifecycle": lifecycle,
    }


def _repair_forward_publications(meta_conn, *, limit: int = 100,
                                 now: datetime | None = None) -> dict:
    """Finish confirmations committed before report/outcome publication."""
    repair_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with meta_conn.cursor() as cur:
        cur.execute("""
            select rung.experiment_id::text
              from quant.intraday_forward_confirmations confirmation
              join quant.intraday_experiment_rungs rung
                on rung.experiment_rung_id = confirmation.experiment_rung_id
              join quant.experiments experiment
                on experiment.experiment_id = rung.experiment_id
              join quant.hypotheses hypothesis
                on hypothesis.hypothesis_id = experiment.hypothesis_id
              join quant.intraday_forward_work_items work
                on work.experiment_id = rung.experiment_id
              left join quant.intraday_forward_report_revisions revision
                on revision.forward_confirmation_id =
                   confirmation.forward_confirmation_id
              left join research.experiment_outcome_revisions outcome
                on outcome.forward_confirmation_id =
                   confirmation.forward_confirmation_id
              left join quant.intraday_forward_qa_handoffs handoff
                on handoff.forward_confirmation_id =
                   confirmation.forward_confirmation_id
             where work.status not in ('FAILED', 'LEASED')
               and (work.next_attempt_at is null
                    or work.next_attempt_at <= %s::timestamptz)
               and (
                 revision.report_revision_id is null
                 or outcome.outcome_revision_id is null
                 or (confirmation.decision = 'PASS'
                     and handoff.qa_handoff_id is null)
                 or (
                   hypothesis.status <> 'ARCHIVED'
                   and (
                     (confirmation.decision = 'PASS'
                      and hypothesis.status not in
                          ('INCONCLUSIVE', 'SUPPORTED', 'REJECTED'))
                     or
                     (confirmation.decision <> 'PASS'
                      and hypothesis.status <> case confirmation.decision
                           when 'FAIL' then 'REJECTED'
                           else 'INCONCLUSIVE' end)
                   )
                 )
               )
             order by coalesce(work.next_attempt_at, confirmation.confirmed_at),
                      confirmation.forward_confirmation_id
             limit %s
        """, (repair_now, max(1, int(limit))))
        experiment_ids = [str(row[0]) for row in cur.fetchall()]
    meta_conn.rollback()
    finalized, failed = [], []
    for repair_experiment_id in experiment_ids:
        try:
            finalized.append(_publish_forward_finalization(
                meta_conn, repair_experiment_id))
        except Exception as exc:
            try:
                meta_conn.rollback()
            except Exception:
                pass
            error = f"{type(exc).__name__}: {exc}"[:400]
            _record_forward_publication_repair_failure(
                meta_conn, experiment_id=repair_experiment_id,
                error=error, now=repair_now)
            failed.append({
                "experiment_id": repair_experiment_id,
                "error": error,
            })
    return {"finalized": finalized, "failed": failed}


def _record_forward_publication_repair_failure(
        meta_conn, *, experiment_id: str, error: str,
        now: datetime) -> None:
    """Spend the same bounded error budget for post-confirmation repair."""
    instant = now.astimezone(timezone.utc)
    with meta_conn.cursor() as cur:
        cur.execute("""
            update quant.intraday_forward_work_items work
               set error_count = least(work.max_error_count,
                                       work.error_count + 1),
                   status = case
                     when work.error_count + 1 >= work.max_error_count
                       then 'FAILED'
                     else 'RETRY'
                   end,
                   next_attempt_at = case
                     when work.error_count + 1 >= work.max_error_count then null
                     else %s::timestamptz + least(
                       interval '1 minute' * %s
                         * power(2, least(work.error_count, 16)),
                       interval '1 hour' * %s)
                   end,
                   leased_at=null, lease_expires_at=null, leased_by=null,
                   lease_token=null, last_error=%s,
                   updated_at=%s::timestamptz
             where work.experiment_id=%s::uuid
               and work.status not in ('FAILED', 'LEASED')
        """, (instant, FORWARD_ERROR_BACKOFF_MINUTES,
              FORWARD_ERROR_BACKOFF_MAX_HOURS, str(error)[:400], instant,
              experiment_id))
    meta_conn.commit()


def _enqueue_forward_candidates(meta_conn) -> int:
    with meta_conn.cursor() as cur:
        cur.execute(_FORWARD_ENQUEUE_SQL)
        inserted = int(cur.rowcount or 0)
    meta_conn.commit()
    return inserted


def _lease_forward_work_items(meta_conn, *, limit: int, worker: str,
                              now: datetime) -> list[dict]:
    if int(limit) < 1:
        raise ValueError("forward candidate limit must be positive")
    if not str(worker or "").strip():
        raise ValueError("forward lease requires a worker identity")
    if now.tzinfo is None:
        raise ValueError("forward lease timestamp must be timezone aware")
    lease_now = now.astimezone(timezone.utc)
    _expire_stale_forward_leases(meta_conn, now=lease_now)
    with meta_conn.cursor() as cur:
        cur.execute(_FORWARD_LEASE_SQL, (
            lease_now, int(limit), lease_now, lease_now,
            FORWARD_WORK_LEASE_MINUTES, str(worker), lease_now,
        ))
        claims = [{
            "experiment_id": str(row[0]),
            "lease_token": str(row[1]),
            "attempt_count": int(row[2]),
            "error_count": int(row[3]),
            "max_error_count": int(row[4]),
        } for row in cur.fetchall()]
    meta_conn.commit()
    return claims


def _expire_stale_forward_leases(meta_conn, *, now: datetime) -> int:
    """Charge a crashed worker exactly once before making its row retryable."""
    instant = now.astimezone(timezone.utc)
    with meta_conn.cursor() as cur:
        cur.execute("""
            update quant.intraday_forward_work_items work
               set error_count = least(work.max_error_count,
                                       work.error_count + 1),
                   status = case
                     when work.error_count + 1 >= work.max_error_count
                       then 'FAILED'
                     else 'RETRY'
                   end,
                   next_attempt_at = case
                     when work.error_count + 1 >= work.max_error_count then null
                     else %s::timestamptz + least(
                       interval '1 minute' * %s
                         * power(2, least(work.error_count, 16)),
                       interval '1 hour' * %s)
                   end,
                   leased_at=null, lease_expires_at=null, leased_by=null,
                   lease_token=null,
                   last_error='lease expired before worker completion',
                   updated_at=%s::timestamptz
             where work.status='LEASED'
               and work.lease_expires_at <= %s::timestamptz
        """, (instant, FORWARD_ERROR_BACKOFF_MINUTES,
              FORWARD_ERROR_BACKOFF_MAX_HOURS, instant, instant))
        expired = int(cur.rowcount or 0)
    meta_conn.commit()
    return expired


def _finish_forward_work_item(meta_conn, *, claim: dict, result: dict,
                              worker: str, now: datetime) -> bool:
    """Release one claim with owner/token fencing and a durable next attempt."""
    result_status = str(result.get("status") or "ERROR").upper()
    if result_status == "CONFIRMED":
        status, next_attempt, error_count = "CONFIRMED", None, int(
            claim.get("error_count") or 0)
        last_error = None
    elif result_status == "WAITING_FOR_NEW_LOCAL_SESSIONS":
        status = "WAITING"
        next_attempt = now.astimezone(timezone.utc) + timedelta(
            hours=FORWARD_WAIT_RETRY_HOURS)
        error_count = int(claim.get("error_count") or 0)
        last_error = None
    elif result_status == "NOT_NOMINATED":
        status, next_attempt, error_count = "NOT_NOMINATED", None, int(
            claim.get("error_count") or 0)
        last_error = None
    elif result_status == "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE":
        # A deployment removed the only executable copy of the frozen
        # evaluator. Retrying the same current image cannot repair that fact,
        # so consume the bounded operational budget immediately while leaving
        # the scientific hypothesis at its pre-forward state.
        error_count = int(claim.get("max_error_count") or
                          FORWARD_MAX_ERROR_COUNT)
        status, next_attempt = "FAILED", None
        last_error = "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE"
    else:
        error_count = int(claim.get("error_count") or 0) + 1
        max_error_count = int(claim.get("max_error_count") or
                              FORWARD_MAX_ERROR_COUNT)
        if error_count >= max_error_count:
            status, next_attempt = "FAILED", None
        else:
            status = "RETRY"
            delay_minutes = min(
                FORWARD_ERROR_BACKOFF_MINUTES *
                (2 ** min(error_count - 1, 16)),
                FORWARD_ERROR_BACKOFF_MAX_HOURS * 60,
            )
            next_attempt = now.astimezone(timezone.utc) + timedelta(
                minutes=delay_minutes)
        last_error = str(result.get("error") or result_status)[:400]
    last_result = json.dumps(result, sort_keys=True, default=str)[:4_000]
    try:
        with meta_conn.cursor() as cur:
            cur.execute("""
                update quant.intraday_forward_work_items
                   set status=%s, next_attempt_at=%s::timestamptz,
                       error_count=%s, leased_at=null,
                       lease_expires_at=null, leased_by=null,
                       lease_token=null, last_result=%s, last_error=%s,
                       updated_at=%s::timestamptz
                 where experiment_id=%s::uuid
                   and status='LEASED'
                   and leased_by=%s
                   and lease_token=%s::uuid
            """, (
                status, next_attempt, error_count, last_result, last_error,
                now.astimezone(timezone.utc), claim["experiment_id"],
                str(worker), claim["lease_token"],
            ))
            finished = cur.rowcount == 1
        meta_conn.commit()
    except Exception:
        meta_conn.rollback()
        raise
    return finished


def _heartbeat_forward_work_item(meta_conn, *, claim: dict, worker: str,
                                 now: datetime) -> bool:
    """Extend an expensive replay only while its exact lease is still owned."""
    try:
        with meta_conn.cursor() as cur:
            cur.execute("""
                update quant.intraday_forward_work_items
                   set lease_expires_at=%s::timestamptz
                         + interval '1 minute' * %s,
                       updated_at=%s::timestamptz
                 where experiment_id=%s::uuid
                   and status='LEASED'
                   and leased_by=%s
                   and lease_token=%s::uuid
                   and lease_expires_at > %s::timestamptz
            """, (
                now.astimezone(timezone.utc), FORWARD_WORK_LEASE_MINUTES,
                now.astimezone(timezone.utc), claim["experiment_id"],
                str(worker), claim["lease_token"], now.astimezone(timezone.utc),
            ))
            owned = cur.rowcount == 1
        meta_conn.commit()
    except Exception:
        meta_conn.rollback()
        raise
    return owned


def _reconcile_forward_work_items(meta_conn) -> int:
    """Close the crash seam only after every semantic publication exists."""
    with meta_conn.cursor() as cur:
        cur.execute("""
            update quant.intraday_forward_work_items work
               set status='CONFIRMED', next_attempt_at=null,
                   leased_at=null, lease_expires_at=null, leased_by=null,
                   lease_token=null, last_error=null,
                   last_result='{"status":"CONFIRMED_BY_RECONCILIATION"}',
                   updated_at=now()
             where work.status <> 'CONFIRMED'
               and exists (
                 select 1
                   from quant.intraday_experiment_rungs rung
                   join quant.intraday_forward_confirmations confirmation
                     on confirmation.experiment_rung_id =
                        rung.experiment_rung_id
                   join quant.intraday_forward_report_revisions revision
                     on revision.forward_confirmation_id =
                        confirmation.forward_confirmation_id
                   join research.experiment_outcome_revisions outcome
                     on outcome.forward_confirmation_id =
                        confirmation.forward_confirmation_id
                   join quant.experiments experiment
                     on experiment.experiment_id = rung.experiment_id
                   join quant.hypotheses hypothesis
                     on hypothesis.hypothesis_id = experiment.hypothesis_id
                  where rung.experiment_id = work.experiment_id
                    and outcome.outcome_revision_id =
                        revision.outcome_revision_id
                    and outcome.decision = case confirmation.decision
                      when 'PASS' then 'SUBMIT_TO_QA'
                      when 'FAIL' then 'REJECT'
                      when 'INCONCLUSIVE' then 'GATE_HOLD'
                      else null
                    end
                    and (
                      hypothesis.status = 'ARCHIVED'
                      or (confirmation.decision = 'PASS'
                          and hypothesis.status in
                              ('INCONCLUSIVE', 'SUPPORTED', 'REJECTED'))
                      or (confirmation.decision = 'FAIL'
                          and hypothesis.status = 'REJECTED')
                      or (confirmation.decision = 'INCONCLUSIVE'
                          and hypothesis.status = 'INCONCLUSIVE')
                    )
                    and (
                      confirmation.decision <> 'PASS'
                      or exists (
                        select 1
                          from quant.intraday_forward_qa_handoffs handoff
                         where handoff.forward_confirmation_id =
                               confirmation.forward_confirmation_id
                           and handoff.report_revision_id =
                               revision.report_revision_id
                      )
                    )
               )
        """)
        reconciled = int(cur.rowcount or 0)
    meta_conn.commit()
    return reconciled


def _forward_candidate_rows(meta_conn, *, experiment_ids: list[str]
                            ) -> list[dict]:
    if not experiment_ids:
        return []
    with meta_conn.cursor() as cur:
        cur.execute(_FORWARD_CANDIDATES_BY_ID_SQL, (experiment_ids,))
        rows = cur.fetchall()
    meta_conn.rollback()  # close the read snapshot before a long market replay
    decoded = [{
        "experiment_id": str(row[0]),
        "dataset_id": str(row[1]),
        "config": _as_json(row[2]),
        "governance_report": _as_json(row[3]),
        "frozen_at": row[4],
        "candidate_lineage_id": str(row[5]),
        "search_cutoff": _session_day(row[6]),
        "final_gate": _as_json(row[7]),
        "score_calibration": _as_json(row[8]),
        "forward_rung_id": str(row[9]) if row[9] is not None else None,
        "forward_dataset_cutoff": row[10],
    } for row in rows]
    order = {experiment_id: index for index, experiment_id in
             enumerate(experiment_ids)}
    return sorted(decoded, key=lambda row: order.get(
        row["experiment_id"], len(order)))


def _forward_stock_universe(meta_conn, full_rung, *,
                            session_dates=None) -> list[str]:
    """Revalidate exact UUIDs for the supplied replay dates; fail shut."""
    instruments = list(full_rung.planned_instrument_ids)
    if not instruments:
        raise RuntimeError("FULL_60 rung has no immutable instrument UUID array")
    with meta_conn.cursor() as cur:
        cur.execute("""
            select instrument_id::text, instrument_type, asset_class, market,
                   status, listed_from, listed_to, is_spac
              from quant.current_krx_stock_instrument_identity
             where instrument_id = any(%s::uuid[])
        """, (instruments,))
        rows = {str(row[0]): {
            "instrument_type": str(row[1]).upper(),
            "asset_class": str(row[2]).upper(),
            "market": str(row[3]).upper(),
            "status": str(row[4]).upper(),
            "listed_from": row[5],
            "listed_to": row[6],
            "is_spac": bool(row[7]),
        } for row in cur.fetchall()}
    meta_conn.rollback()
    missing = sorted(set(instruments) - set(rows))
    sessions = tuple(_session_day(value) for value in (
        full_rung.planned_session_dates
        if session_dates is None else session_dates))
    if not sessions:
        raise RuntimeError("FULL_60 rung has no immutable session array")
    first_session, last_session = min(sessions), max(sessions)
    invalid = sorted(key for key, value in rows.items() if not (
        value["instrument_type"] == "STOCK"
        and value["asset_class"] == "EQUITY"
        and value["market"] == "KRX"
        and value["status"] == "ACTIVE"
        and not value["is_spac"]
        and (value["listed_from"] is None
             or value["listed_from"] <= first_session)
        and (value["listed_to"] is None
             or value["listed_to"] >= last_session)
    ))
    if missing or invalid:
        raise RuntimeError(
            "forward universe failed STOCK/SPAC/listing identity validation: "
            f"missing={len(missing)} invalid={len(invalid)}")
    if tuple(sorted(instruments)) != full_rung.planned_instrument_ids:
        raise RuntimeError("FULL_60 instrument UUID array is not canonical")
    return instruments


def _forward_sessions(meta_conn, *, search_cutoff: date, frozen_at: datetime,
                      knowledge_cutoff: datetime,
                      required_sessions: int) -> dict:
    """Freeze the first N completed future KRX sessions without market reads."""
    required = max(20, int(required_sessions))
    if frozen_at.tzinfo is None or knowledge_cutoff.tzinfo is None:
        raise ValueError("forward timestamps must be timezone aware")
    # Never admit the day on which the candidate was frozen.  Filtering rows
    # by available_at alone would remove the pre-freeze morning and leave a
    # misleading partial afternoon session in the grouped result.
    first_day = max(search_cutoff + timedelta(days=1),
                    frozen_at.astimezone(KST).date() + timedelta(days=1))
    current_kst_day = knowledge_cutoff.astimezone(KST).date()
    with meta_conn.cursor() as cur:
        cur.execute(
            _FORWARD_CALENDAR_SESSIONS_SQL,
            (knowledge_cutoff, first_day, first_day, first_day,
             current_kst_day, knowledge_cutoff, required),
        )
        rows = cur.fetchall()
    meta_conn.rollback()
    sessions = [_session_day(row[0]) for row in rows]
    if sessions != sorted(set(sessions)):
        raise RuntimeError("forward session discovery was not sorted and unique")
    manifests = [{
        "session": session.isoformat(),
        "opens_at": row[1].isoformat() if row[1] else None,
        "closes_at": row[2].isoformat() if row[2] else None,
        "calendar_version_id": str(row[3]),
        "calendar_version": int(row[4]),
        "calendar_content_hash": str(row[5]),
        "calendar_known_at": row[6].isoformat() if row[6] else None,
    } for session, row in zip(sessions, rows)]
    calendar = ({
        "calendar_version_id": str(rows[0][3]),
        "calendar_version": int(rows[0][4]),
        "calendar_content_hash": str(rows[0][5]),
        "calendar_known_at": rows[0][6].isoformat() if rows[0][6] else None,
    } if rows else None)
    return {
        "status": "READY" if len(sessions) == required else "WAITING",
        "required_sessions": required,
        "available_sessions": len(sessions),
        "sessions": sessions,
        "session_manifest": manifests,
        "calendar": calendar,
        "session_set_fingerprint": stable_fingerprint(
            {"calendar": calendar,
             "sessions": [session.isoformat() for session in sessions]}),
        "selection_rule": (
            "FIRST_N_COMPLETED_VERSIONED_KRX_REGULAR_SESSIONS_AFTER_FREEZE;"
            "NO_QUOTE_TRADE_OR_RETURN_DATE_FILTER;NO_SKIPPING"),
        "candidate_frozen_at": frozen_at.isoformat(),
        "knowledge_cutoff": knowledge_cutoff.isoformat(),
        "current_kst_day_excluded": True,
        "raw_market_read_before_exposure": False,
        "historical_event_time_replay_eligible": False,
    }


def _deterministic_forward_dataset_cutoff(cohort: dict,
                                          spec: IntradayLaneSpec, *,
                                          timestamp_policy: str =
                                          STRICT_TIMESTAMP_POLICY) -> datetime:
    """Return a concurrency-stable arrival cutoff for one ready cohort.

    Midnight after the last fixed KRX date is well beyond the regular close
    while excluding later backfills.  A calendar publication timestamp can be
    later, so it is part of the same deterministic maximum.
    """
    sessions = [_session_day(value) for value in cohort.get("sessions") or []]
    if cohort.get("status") != "READY" or not sessions:
        raise ValueError("a ready forward cohort is required to freeze cutoff")
    if sessions != sorted(set(sessions)):
        raise ValueError("forward cohort sessions must be sorted and unique")
    after_last = datetime.combine(
        sessions[-1] + timedelta(days=1), time.min, KST
    ).astimezone(timezone.utc) + effective_purge_gap(
        spec, timestamp_policy)
    calendar = cohort.get("calendar") or {}
    raw_known = calendar.get("calendar_known_at")
    if not raw_known:
        raise ValueError("forward cohort lacks its calendar knowledge timestamp")
    known_at = datetime.fromisoformat(str(raw_known).replace("Z", "+00:00"))
    if known_at.tzinfo is None:
        raise ValueError("calendar knowledge timestamp must be timezone aware")
    return max(after_last, known_at.astimezone(timezone.utc))


def _forward_spec(config: dict) -> IntradayLaneSpec:
    return IntradayLaneSpec(
        sample_interval_seconds=int(config["sample_interval_seconds"]),
        feature_lookback_seconds=int(config["feature_lookback_seconds"]),
        horizons_seconds=(int(config["horizon_seconds"]),),
        order_latency_ms=int(config["order_latency_ms"]),
        max_quote_age_seconds=float(config["max_quote_age_seconds"]),
        fee_bps_per_side=float(config["fee_bps_per_side"]),
        maker_fee_bps_per_side=float(config["maker_fee_bps_per_side"]),
    )


def _validate_frozen_candidate(config: dict, candidate) -> None:
    primary = {
        "intraday_signal_expr": config["intraday_signal_expr"],
        "semantic_plan": config["semantic_plan"],
        "horizon_seconds": config["horizon_seconds"],
        "execution": config["execution"],
        "entry_policy": config["entry_policy"],
        "coefficient_policy": config["coefficient_policy"],
        "source_baseline_expr": config.get("source_baseline_expr"),
    }
    feature_spec, label_spec, model_spec = _candidate_specs(config, primary)
    checks = {
        "AST": (stable_fingerprint(config["intraday_signal_expr"]),
                candidate.candidate_ast_fingerprint),
        "semantic plan": (stable_fingerprint(config["semantic_plan"]),
                          candidate.semantic_plan_fingerprint),
        "features": (stable_fingerprint(feature_spec),
                     candidate.feature_spec_fingerprint),
        "labels": (stable_fingerprint(label_spec),
                   candidate.label_spec_fingerprint),
        "model": (stable_fingerprint(model_spec),
                  candidate.model_spec_fingerprint),
    }
    mismatches = [name for name, pair in checks.items() if pair[0] != pair[1]]
    if candidate.evaluator_version != _evaluator_version(config):
        mismatches.append("evaluator version")
    if candidate.cost_model_version != COST_MODEL_VERSION:
        mismatches.append("cost model version")
    if mismatches:
        raise RuntimeError(
            "frozen forward candidate no longer matches runtime: "
            + ", ".join(mismatches))


def _record_forward_accesses(meta_conn, *, rung, instrument_ids: list[str],
                             cutoff: datetime,
                             spec: IntradayLaneSpec,
                             timestamp_policy: str =
                             STRICT_TIMESTAMP_POLICY) -> dict[str, object]:
    """Consume the entire fixed cohort durably before raw replay begins."""
    accesses = {}
    for day in rung.planned_session_dates:
        access = record_session_access(
            meta_conn, rung=rung, session_date=day,
            instrument_ids=instrument_ids, knowledge_cutoff=cutoff,
            source_watermark={
                "event_source": LOCAL_EVENT_SOURCE,
                "knowledge_clock": ARRIVAL_TIME_CAUSAL,
                "phase": "PRE_RAW_ACCESS",
                "purge_seconds": effective_purge_gap(
                    spec, timestamp_policy).total_seconds(),
                "rung_plan_fingerprint": rung.rung_plan_fingerprint,
            },
            accessed_by="svc_quant/intraday-forward-confirmation",
            access_purpose=FORWARD_CONFIRMATION,
            knowledge_clock_mode=ARRIVAL_TIME_CAUSAL,
        )
        accesses[day.isoformat()] = access
    return accesses


def _digest_record(state: dict, record: dict) -> None:
    encoded = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False).encode("utf-8")
    state["hasher"].update(len(encoded).to_bytes(8, "big"))
    state["hasher"].update(encoded)


def _new_forward_raw_digest(day: date) -> dict:
    state = {
        "hasher": hashlib.sha256(),
        "quote_rows": 0,
        "trade_rows": 0,
        "max_available_at": None,
    }
    _digest_record(state, {
        "content_digest_version": FORWARD_RAW_DIGEST_VERSION,
        "session": day.isoformat(),
    })
    return state


def _append_forward_raw_events(state: dict, *, instrument_id: str,
                               quotes, trades) -> None:
    """Hash exactly the eligible event objects handed to the evaluator."""
    _digest_record(state, {"kind": "INSTRUMENT", "id": str(instrument_id)})
    for event in quotes:
        _digest_record(state, {
            "kind": "QUOTE",
            "instrument_id": str(event.instrument_id),
            "event_time": event.event_time.isoformat(),
            "received_at": event.received_at.isoformat(),
            "observed_at": event.observed_at.isoformat(),
            "source_event_id": str(event.source_event_id),
            "bid_prices": [str(value) for value in event.bid_prices],
            "bid_sizes": [str(value) for value in event.bid_sizes],
            "ask_prices": [str(value) for value in event.ask_prices],
            "ask_sizes": [str(value) for value in event.ask_sizes],
        })
        state["quote_rows"] += 1
        available = event.available_at
        if (state["max_available_at"] is None
                or available > state["max_available_at"]):
            state["max_available_at"] = available
    for event in trades:
        _digest_record(state, {
            "kind": "TRADE",
            "instrument_id": str(event.instrument_id),
            "event_time": event.event_time.isoformat(),
            "received_at": event.received_at.isoformat(),
            "observed_at": event.observed_at.isoformat(),
            "source_event_id": str(event.source_event_id),
            "price": str(event.price),
            "quantity": str(event.quantity),
            "side": int(event.side),
        })
        state["trade_rows"] += 1
        available = event.available_at
        if (state["max_available_at"] is None
                or available > state["max_available_at"]):
            state["max_available_at"] = available


def _finish_forward_raw_digest(state: dict, *, purge_gap: timedelta) -> dict:
    return {
        "content_fingerprint": state["hasher"].hexdigest(),
        "content_digest_version": FORWARD_RAW_DIGEST_VERSION,
        "quote_rows": int(state["quote_rows"]),
        "trade_rows": int(state["trade_rows"]),
        "max_available_at": (
            state["max_available_at"].isoformat()
            if state["max_available_at"] is not None else None),
        "purge_seconds": purge_gap.total_seconds(),
    }


def _record_forward_exposures(meta_conn, *, accesses: dict[str, object],
                              replay_evidence: dict[str, dict], rung,
                              instrument_ids: list[str], cutoff: datetime,
                              spec: IntradayLaneSpec,
                              timestamp_policy: str =
                              STRICT_TIMESTAMP_POLICY) -> list[dict]:
    """Append post-read evidence from the exact event stream evaluated."""
    expected = [day.isoformat() for day in rung.planned_session_dates]
    if sorted(accesses) != sorted(expected):
        raise RuntimeError("forward access marker set is incomplete")
    if sorted(replay_evidence) != sorted(expected):
        raise RuntimeError("forward raw replay evidence set is incomplete")
    evidence = []
    for day in rung.planned_session_dates:
        key = day.isoformat()
        access = accesses[key]
        daily = replay_evidence[key]
        if daily.get("content_digest_version") != FORWARD_RAW_DIGEST_VERSION:
            raise RuntimeError("forward raw replay digest version is invalid")
        exposure = record_session_exposure(
            meta_conn, access=access, rung=rung, session_date=day,
            instrument_ids=instrument_ids,
            session_content_fingerprint=daily["content_fingerprint"],
            quote_row_count=daily["quote_rows"],
            trade_row_count=daily["trade_rows"],
            knowledge_cutoff=cutoff,
            source_watermark={
                "event_source": LOCAL_EVENT_SOURCE,
                "knowledge_clock": ARRIVAL_TIME_CAUSAL,
                "content_digest_version": FORWARD_RAW_DIGEST_VERSION,
                "max_available_at": daily.get("max_available_at"),
                "purge_seconds": effective_purge_gap(
                    spec, timestamp_policy).total_seconds(),
                "rung_plan_fingerprint": rung.rung_plan_fingerprint,
            },
            exposed_by="svc_quant/intraday-forward-confirmation",
            exposure_purpose=FORWARD_CONFIRMATION,
            knowledge_clock_mode=ARRIVAL_TIME_CAUSAL,
        )
        evidence.append({
            "session": day.isoformat(), "inserted": exposure.inserted,
            "access_inserted": access.inserted,
            "access_fingerprint": access.access_fingerprint,
            "evidence_fingerprint": exposure.exposure_evidence_fingerprint,
            "quote_rows": daily["quote_rows"],
            "trade_rows": daily["trade_rows"],
        })
    return evidence


def _forward_accumulator(config: dict, spec: IntradayLaneSpec, *,
                         score_calibration: dict,
                         governance_report: dict) -> CandidateAccumulator:
    accumulator = CandidateAccumulator(
        expr=config["intraday_signal_expr"], spec=spec,
        horizon_seconds=int(config["horizon_seconds"]),
        execution=config["execution"], position_mode=config["position_mode"],
        threshold=float(config["threshold"]),
        entry_policy=config["entry_policy"],
        coefficient_policy=config["coefficient_policy"],
        minimum_predicted_edge_bps=float(
            config["minimum_predicted_edge_bps"]),
        trials=1, family_pbo=None, semantic_plan=config["semantic_plan"],
        feature_window_contract_version=_feature_window_contract(config),
    )
    teacher = ((governance_report.get("supervised_control") or {}).get(
        "calibration"))
    accumulator.restore_frozen_calibration(
        score_calibration, teacher if isinstance(teacher, dict) else None)
    return accumulator


def _evaluate_forward_replay(
        market_conn, *, config: dict, spec: IntradayLaneSpec,
        rung, instrument_ids: list[str], cutoff: datetime,
        score_calibration: dict, governance_report: dict,
        lease_guard: Callable[[bool], None] | None = None) -> dict:
    accumulator = _forward_accumulator(
        config, spec, score_calibration=score_calibration,
        governance_report=governance_report)
    accumulator.schedule_sessions(
        session.isoformat() for session in rung.planned_session_dates)
    timestamp_policy = config.get(
        "timestamp_policy", STRICT_TIMESTAMP_POLICY)
    purge_gap = effective_purge_gap(spec, timestamp_policy)
    shard_size = int(config.get("instrument_shard_size") or 8)
    shards = [instrument_ids[index:index + shard_size]
              for index in range(0, len(instrument_ids), shard_size)]
    digest_states = {
        day.isoformat(): _new_forward_raw_digest(day)
        for day in rung.planned_session_dates
    }
    replay = []
    for shard_number, shard in enumerate(shards, 1):
        sample_count = 0
        for day in rung.planned_session_dates:
            if lease_guard is not None:
                lease_guard(False)
            start, end = _session_bounds(day)
            events = load_instrument_events_batch(
                market_conn, instrument_ids=shard, start=start,
                end=end + purge_gap, as_known_at=cutoff,
                source=LOCAL_EVENT_SOURCE)
            for instrument in shard:
                quotes, trades = events[instrument]
                _append_forward_raw_events(
                    digest_states[day.isoformat()], instrument_id=instrument,
                    quotes=quotes, trades=trades)
                samples = _build_runtime_samples(
                    config, quotes, trades, spec, start=start, end=end)
                sample_count += len(samples)
                # Empty slices still count in requested coverage and cannot be
                # omitted from the exact FULL_60 stock universe.
                accumulator.add(instrument, samples)
        replay.append({
            "shard": shard_number,
            "instrument_count": len(shard),
            "sample_count": sample_count,
            "instrument_fingerprint": stable_fingerprint(shard),
        })
    report = accumulator.finish()
    session_evidence = {
        day.isoformat(): _finish_forward_raw_digest(
            digest_states[day.isoformat()], purge_gap=purge_gap)
        for day in rung.planned_session_dates
    }
    report["lane_manifest"].update(manifest(
        spec, source=LOCAL_EVENT_SOURCE,
        timestamp_policy=timestamp_policy))
    report["forward_replay"] = {
        "version": FORWARD_RUNNER_VERSION,
        "session_count": len(rung.planned_session_dates),
        "instrument_count": len(instrument_ids),
        "shards": replay,
        "session_evidence": session_evidence,
        "content_digest_version": FORWARD_RAW_DIGEST_VERSION,
        "event_source": LOCAL_EVENT_SOURCE,
        "knowledge_clock_mode": ARRIVAL_TIME_CAUSAL,
        "score_or_model_refit": False,
        "historical_61_session_reuse": False,
    }
    return report


def _forward_test_index(meta_conn, experiment_rung_id: str) -> int:
    """Load the globally unique index assigned atomically by PostgreSQL."""
    with meta_conn.cursor() as cur:
        cur.execute("""
            select forward_test_index
              from quant.intraday_experiment_rungs
             where experiment_rung_id = %s::uuid and rung = 'FORWARD'
        """, (experiment_rung_id,))
        row = cur.fetchone()
    meta_conn.rollback()
    if row is None or int(row[0] or 0) < 1:
        raise RuntimeError("forward test index could not be established")
    return int(row[0])


def _stationary_forward_interval(values: list[float], *, alpha: float) -> dict:
    if len(values) < 2:
        return {"low": None, "high": None}
    if not 0.0 < float(alpha) < 1.0:
        raise ValueError("forward alpha must be in (0, 1)")
    means = []
    for path in stationary_bootstrap_indices(
            len(values), n_boot=10_000, restart_probability=0.25,
            seed=20260817):
        means.append(math.fsum(values[index] for index in path) / len(path))
    means.sort()
    return {
        "low": means[int((alpha / 2.0) * len(means))],
        "high": means[min(int((1.0 - alpha / 2.0) * len(means)),
                          len(means) - 1)],
        "method": "STATIONARY_BOOTSTRAP",
        "two_sided_alpha": alpha,
        "restart_probability": 0.25,
        "draws": len(means),
        "seed": 20260817,
    }


def _forward_gate(report: dict, *, rung, exposure_evidence: list[dict],
                  minimum_opportunities: int = 100,
                  forward_test_index: int = 1) -> dict:
    expected = [session.isoformat() for session in rung.planned_session_dates]
    returns = report.get("session_returns_bps") or {}
    actual = sorted(str(value) for value in returns)
    values = [float(returns[session]) for session in expected
              if session in returns]
    test_index = int(forward_test_index)
    if test_index < 1:
        raise ValueError("forward_test_index must be positive")
    # Sum_i 0.10/(i(i+1)) = 0.10.  Because each interval is two-sided and the
    # release claim uses only its lower tail, the total one-sided false-positive
    # budget is at most 0.05 across an unbounded sequence of candidates.
    two_sided_alpha = 0.10 / (test_index * (test_index + 1))
    interval = _stationary_forward_interval(values, alpha=two_sided_alpha)
    summary = report.get("summary") or {}
    data_failures = []
    economic_failures = []
    if actual != expected:
        data_failures.append("FORWARD_SESSION_VECTOR_NOT_EXACT")
    if len(expected) < 20:
        data_failures.append("INSUFFICIENT_FORWARD_SESSIONS")
    if any(int(row.get("quote_rows") or 0) <= 0 for row in exposure_evidence):
        data_failures.append("FORWARD_SESSION_WITHOUT_QUOTES")
    if any(int(row.get("trade_rows") or 0) <= 0 for row in exposure_evidence):
        # The date remains in the zero-return vector.  It is not skipped.
        data_failures.append("FORWARD_SESSION_WITHOUT_TRADES")
    if int(summary.get("opportunities") or 0) < int(minimum_opportunities):
        data_failures.append("FORWARD_OPPORTUNITIES_BELOW_MINIMUM")
    if float(summary.get("instrument_coverage") or 0.0) < 0.80:
        data_failures.append("FORWARD_STOCK_COVERAGE_BELOW_MINIMUM")
    if any(row.get("status") == "FAIL" for row in report.get("causality") or []):
        data_failures.append("FORWARD_CAUSALITY_NOT_PASS")
    if (summary.get("mean_net_bps_per_opportunity") is None or
            float(summary["mean_net_bps_per_opportunity"]) <= 0.0):
        economic_failures.append("FORWARD_COST_NET_EDGE_NOT_POSITIVE")
    if interval.get("low") is None or float(interval["low"]) <= 0.0:
        economic_failures.append("FORWARD_STATIONARY_CI_CROSSES_ZERO")
    positive_ratio = (sum(value > 0.0 for value in values) / len(values)
                      if values else None)
    if positive_ratio is None or positive_ratio < 0.60:
        economic_failures.append("FORWARD_POSITIVE_SESSION_RATIO_LOW")
    failures = list(dict.fromkeys(data_failures + economic_failures))
    decision = ("INCONCLUSIVE" if data_failures else
                "FAIL" if economic_failures else "PASS")
    return {
        "version": FORWARD_GATE_VERSION,
        "decision": decision,
        "gate_failures": failures,
        "statistics": {
            "planned_sessions": len(expected),
            "observed_sessions": len(actual),
            "session_ids": expected,
            "session_returns_bps": {key: returns[key] for key in expected
                                    if key in returns},
            "session_mean_net_bps": (
                math.fsum(values) / len(values) if values else None),
            "stationary_session_mean_ci_bps": interval,
            "positive_session_ratio": positive_ratio,
            "opportunities": summary.get("opportunities"),
            "instrument_coverage": summary.get("instrument_coverage"),
            "mean_net_bps_per_opportunity": summary.get(
                "mean_net_bps_per_opportunity"),
            "mean_implementation_drag_bps": summary.get(
                "mean_implementation_drag_bps"),
            "minimum_opportunities": int(minimum_opportunities),
            "minimum_instrument_coverage": 0.80,
            "minimum_positive_session_ratio": 0.60,
            "minimum_effect_bps": 0.0,
            "fixed_horizon": True,
            "optional_stopping": False,
            "single_frozen_candidate": True,
            "forward_test_index": test_index,
            "online_multiple_testing": (
                "SUMMABLE_CHRONOLOGICAL_ALPHA_SPENDING_FWER_0_05_ONE_SIDED"),
            "two_sided_alpha_spent": two_sided_alpha,
            "one_sided_familywise_alpha_budget": 0.05,
            "cost_net": True,
            "score_or_model_refit": False,
        },
    }


def _forward_gate_with_runtime_artifact(
        gate: dict, governance_report: dict) -> tuple[dict, dict]:
    """Remove scientific authority when the frozen evaluator cannot run."""

    runtime_artifact = _forward_runtime_artifact_attestation(
        governance_report or {})
    if not runtime_artifact["reproduction_route_available"]:
        return ({
            **gate,
            "decision": "INCONCLUSIVE",
            "gate_failures": ["FORWARD_RUNTIME_ARTIFACT_UNAVAILABLE"],
            "statistics": {
                **(gate.get("statistics") or {}),
                "runtime_artifact": runtime_artifact,
                "performance_evidence_authority": False,
            },
        }, runtime_artifact)
    return ({
        **gate,
        "statistics": {
            **(gate.get("statistics") or {}),
            "runtime_artifact": runtime_artifact,
            "performance_evidence_authority": True,
        },
    }, runtime_artifact)


QA_REPRODUCTION_INPUT_VERSION = \
    "intraday-forward-qa-reproduction-input-v1"
QA_REPRODUCTION_VERSION = "intraday-forward-qa-reproduction-v1"


def _validated_qa_reproduction_config(*, experiment: dict,
                                      request_contract: dict,
                                      governance_report: dict) -> dict:
    """Validate the frozen contract; executable-route checks run separately."""

    runtime = governance_report.get("reproduction_runtime") or {}
    if (not isinstance(runtime, dict)
            or runtime.get("version") != QA_REPRODUCTION_RUNTIME_VERSION):
        raise RuntimeError("QA reproduction lacks a frozen runtime manifest")
    frozen_config = runtime.get("frozen_config") or {}
    source_manifest = runtime.get("source_manifest") or {}
    if not isinstance(frozen_config, dict) or not frozen_config:
        raise RuntimeError("QA reproduction frozen config is invalid")
    if (not isinstance(source_manifest, dict)
            or source_manifest.get("version") !=
            QA_REPRODUCTION_SOURCE_VERSION):
        raise RuntimeError("QA reproduction source manifest is invalid")

    runtime_identity = {
        key: value for key, value in runtime.items()
        if key != "runtime_manifest_fingerprint"
    }
    source_identity = {
        key: value for key, value in source_manifest.items()
        if key != "source_fingerprint"
    }
    checks = {
        "frozen config fingerprint": (
            stable_fingerprint(frozen_config),
            runtime.get("frozen_config_fingerprint")),
        "runtime manifest fingerprint": (
            stable_fingerprint(runtime_identity),
            runtime.get("runtime_manifest_fingerprint")),
        "source manifest fingerprint": (
            stable_fingerprint(source_identity),
            source_manifest.get("source_fingerprint")),
        "experiment input hash": (
            str(experiment.get("input_hash") or ""),
            runtime.get("experiment_input_hash")),
        "experiment code version": (
            str(experiment.get("code_version") or ""),
            runtime.get("code_version")),
        "experiment cost model": (
            str(experiment.get("cost_model_version") or ""),
            runtime.get("cost_model_version")),
        "hypothesis input hash": (
            _input_hash_for_versions(
                str(request_contract.get("hypothesis_id") or ""),
                frozen_config,
                runner_version=str(runtime.get("code_version") or ""),
                evaluator_version=str(
                    runtime.get("evaluator_version") or ""),
                cost_model_version=str(
                    runtime.get("cost_model_version") or "")),
            runtime.get("experiment_input_hash")),
    }
    mismatches = sorted(name for name, pair in checks.items()
                        if pair[0] != pair[1])
    if mismatches:
        raise RuntimeError(
            "QA reproduction runtime differs from immutable evidence: "
            + ", ".join(mismatches))
    return frozen_config


def preflight_qa_reproduction_runtime(reproduction_input: dict) -> dict:
    """Validate immutable runtime identity without opening the market store."""

    bundle = dict(reproduction_input or {})
    if bundle.get("contract_version") != QA_REPRODUCTION_INPUT_VERSION:
        raise RuntimeError("unknown QA reproduction input contract")
    request = bundle.get("request") or {}
    experiment = bundle.get("experiment") or {}
    revision = bundle.get("report_revision") or {}
    if not all(isinstance(value, dict) and value for value in (
            request, experiment, revision)):
        raise RuntimeError(
            "QA reproduction runtime preflight bundle is incomplete")
    governance_report = revision.get("report") or {}
    if (not isinstance(governance_report, dict)
            or stable_fingerprint(governance_report) !=
            str(revision.get("report_fingerprint") or "")):
        raise RuntimeError("QA reproduction report revision hash is inconsistent")
    _validated_qa_reproduction_config(
        experiment=experiment,
        request_contract=request.get("reproduction_contract") or {},
        governance_report=governance_report)
    return _forward_runtime_artifact_attestation(governance_report)


def _qa_reproduction_date(value: object, *, field: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"QA reproduction {field} is not an ISO date") \
            from exc


def _qa_reproduction_timestamp(value: object, *, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"QA reproduction {field} is not an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"QA reproduction {field} must be timezone aware")
    return parsed.astimezone(timezone.utc)


def reproduce_forward_confirmation(
        market_conn, reproduction_input: dict, *,
        lease_guard: Callable[[bool], None] | None = None) -> dict:
    """Independently recompute one immutable stock-only forward PASS.

    The metadata database owns leasing and returns one joined, validated input
    bundle.  This function is deliberately write-free: it reads only the raw
    market connection, restores the frozen AST/calibration, recomputes every
    session digest and gate statistic, and returns an audit verdict.  A mismatch
    is a completed QA ``FAIL`` result, not an infrastructure retry.
    """

    bundle = dict(reproduction_input or {})
    if bundle.get("contract_version") != QA_REPRODUCTION_INPUT_VERSION:
        raise RuntimeError("unknown QA reproduction input contract")
    required_objects = (
        "work_item", "request", "experiment", "candidate", "forward_rung",
        "report_revision", "confirmation")
    if any(not isinstance(bundle.get(key), dict) or not bundle[key]
           for key in required_objects):
        raise RuntimeError("QA reproduction input bundle is incomplete")

    request = bundle["request"]
    request_contract = request.get("reproduction_contract") or {}
    experiment = bundle["experiment"]
    candidate_payload = bundle["candidate"]
    rung_payload = bundle["forward_rung"]
    revision = bundle["report_revision"]
    confirmation = bundle["confirmation"]
    governance_report = revision.get("report") or {}
    if (not isinstance(governance_report, dict)
            or stable_fingerprint(governance_report) !=
            str(revision.get("report_fingerprint") or "")):
        raise RuntimeError("QA reproduction report revision hash is inconsistent")
    config = _validated_qa_reproduction_config(
        experiment=experiment, request_contract=request_contract,
        governance_report=governance_report)
    if (request_contract.get("requested_action") !=
            "INDEPENDENT_QA_REPRODUCTION"
            or request_contract.get("promotion_authority") is not False
            or request_contract.get("asset_class") != "EQUITY"
            or request_contract.get("instrument_type") != "STOCK"
            or request_contract.get("asset_scope") != STOCK_ASSET_SCOPE
            or request_contract.get("product_filter") !=
            "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY"
            or config.get("asset_scope") !=
            "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY"):
        raise RuntimeError("QA reproduction is not an exact stock-only contract")

    sessions = tuple(_qa_reproduction_date(value, field="planned session")
                     for value in rung_payload.get(
                         "planned_session_dates") or [])
    instruments = tuple(str(value) for value in
                        rung_payload.get("planned_instrument_ids") or [])
    if (len(sessions) < 20 or sessions != tuple(sorted(set(sessions)))
            or not instruments
            or instruments != tuple(sorted(set(instruments)))):
        raise RuntimeError(
            "QA reproduction forward session/instrument arrays are not exact")
    session_fp = stable_fingerprint(
        [session.isoformat() for session in sessions])
    instrument_fp = stable_fingerprint(list(instruments))
    if (int(rung_payload.get("planned_session_count") or 0) != len(sessions)
            or int(rung_payload.get("planned_instrument_count") or 0) !=
            len(instruments)
            or str(rung_payload.get("session_set_fingerprint") or "") !=
            session_fp
            or str(rung_payload.get("instrument_set_fingerprint") or "") !=
            instrument_fp
            or int(rung_payload.get("forward_test_index") or 0) < 1):
        raise RuntimeError("QA reproduction frozen rung identity is inconsistent")

    identity_checks = {
        "experiment_id": str(experiment.get("experiment_id") or ""),
        "hypothesis_id": str(experiment.get("hypothesis_id") or ""),
        "forward_confirmation_id": str(
            confirmation.get("forward_confirmation_id") or ""),
        "report_revision_id": str(revision.get("report_revision_id") or ""),
        "instrument_count": len(instruments),
        "instrument_set_fingerprint": instrument_fp,
        "session_count": len(sessions),
        "session_set_fingerprint": session_fp,
        "rung_plan_fingerprint": str(
            rung_payload.get("rung_plan_fingerprint") or ""),
    }
    mismatched_contract = sorted(
        key for key, expected in identity_checks.items()
        if request_contract.get(key) != expected)
    if mismatched_contract:
        raise RuntimeError(
            "QA reproduction request differs from frozen evidence: "
            + ", ".join(mismatched_contract))

    candidate = SimpleNamespace(**candidate_payload)
    if (str(candidate_payload.get("candidate_lineage_id") or "") !=
            str(rung_payload.get("candidate_lineage_id") or "")
            or str(candidate_payload.get("root_lineage_id") or "") !=
            str(rung_payload.get("root_lineage_id") or "")):
        raise RuntimeError("QA reproduction rung belongs to another candidate")
    rung = SimpleNamespace(
        experiment_rung_id=str(rung_payload.get("experiment_rung_id") or ""),
        planned_session_dates=sessions,
        planned_instrument_ids=instruments,
        planned_session_count=len(sessions),
        planned_instrument_count=len(instruments),
        session_set_fingerprint=session_fp,
        instrument_set_fingerprint=instrument_fp,
        rung_plan_fingerprint=str(
            rung_payload.get("rung_plan_fingerprint") or ""),
        forward_test_index=int(rung_payload["forward_test_index"]),
    )
    if not rung.experiment_rung_id or len(rung.rung_plan_fingerprint) != 64:
        raise RuntimeError("QA reproduction rung lacks immutable identity")

    score_calibration = governance_report.get("score_calibration") or {}
    if not score_calibration:
        raise RuntimeError("QA reproduction lacks frozen score calibration")
    cutoff = _qa_reproduction_timestamp(
        rung_payload.get("dataset_cutoff"), field="dataset cutoff")

    expected_exposures = bundle.get("session_exposures") or []
    if not isinstance(expected_exposures, list):
        raise RuntimeError("QA reproduction session exposures must be an array")
    expected_by_session = {}
    for row in expected_exposures:
        if not isinstance(row, dict):
            raise RuntimeError("QA reproduction exposure is not an object")
        session = _qa_reproduction_date(
            row.get("session_date"), field="exposure session").isoformat()
        if session in expected_by_session:
            raise RuntimeError("QA reproduction contains duplicate exposure")
        if (str(row.get("instrument_set_fingerprint") or "") != instrument_fp
                or int(row.get("instrument_count") or 0) != len(instruments)):
            raise RuntimeError(
                "QA reproduction exposure has another stock universe")
        expected_by_session[session] = {
            "content_fingerprint": str(
                row.get("session_content_fingerprint") or ""),
            "quote_rows": int(row.get("quote_row_count") or 0),
            "trade_rows": int(row.get("trade_row_count") or 0),
        }
    session_ids = [session.isoformat() for session in sessions]
    if sorted(expected_by_session) != session_ids:
        raise RuntimeError("QA reproduction exposure set is incomplete")

    runtime_artifact = _forward_runtime_artifact_attestation(
        governance_report)
    if not runtime_artifact["reproduction_route_available"]:
        result = {
            "version": QA_REPRODUCTION_VERSION,
            "verdict": "INCONCLUSIVE",
            "checks": {"runtime_artifact_available": False},
            "failed_checks": ["runtime_artifact_available"],
            "reason_code": "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE",
            "runtime_artifact": runtime_artifact,
            "work_item_id": str(
                bundle["work_item"].get("work_item_id") or ""),
            "reproduction_request_id": str(
                bundle["work_item"].get("reproduction_request_id") or ""),
            "experiment_id": identity_checks["experiment_id"],
            "forward_confirmation_id": identity_checks[
                "forward_confirmation_id"],
            "report_revision_id": identity_checks["report_revision_id"],
            "report_fingerprint": str(
                revision.get("report_fingerprint") or ""),
            "candidate_identity_fingerprint": str(
                candidate_payload.get(
                    "candidate_identity_fingerprint") or ""),
            "rung_plan_fingerprint": rung.rung_plan_fingerprint,
            "instrument_set_fingerprint": instrument_fp,
            "session_set_fingerprint": session_fp,
            "expected_raw_session_evidence_fingerprint":
                stable_fingerprint(expected_by_session),
            "observed_raw_session_evidence_fingerprint": None,
            "original_gate_core_fingerprint": None,
            "reproduced_gate_core_fingerprint": None,
            "reproduced_gate": None,
            "score_or_model_refit": False,
            "stock_only": True,
            "historical_61_session_reuse": False,
            "independent_process": True,
            "promotion_authority": False,
        }
        result["result_fingerprint"] = stable_fingerprint(result)
        return result

    # Interpret the frozen formula/config only after proving that this process
    # is the exact runtime that created it.  Older schemas may be unreadable by
    # a new deployment; that is an honest INCONCLUSIVE artifact condition, not
    # a malformed-candidate retry or a scientific failure.
    _validate_frozen_candidate(config, candidate)
    spec = _forward_spec(config)
    if lease_guard is not None:
        lease_guard(True)
    reproduced_report = _evaluate_forward_replay(
        market_conn, config=config, spec=spec, rung=rung,
        instrument_ids=list(instruments), cutoff=cutoff,
        score_calibration=score_calibration,
        governance_report=governance_report,
        lease_guard=lease_guard)
    observed_by_session = ((reproduced_report.get("forward_replay") or {}).get(
        "session_evidence") or {})
    observed_compact = {
        str(session): {
            "content_fingerprint": str(
                (row or {}).get("content_fingerprint") or ""),
            "quote_rows": int((row or {}).get("quote_rows") or 0),
            "trade_rows": int((row or {}).get("trade_rows") or 0),
        } for session, row in observed_by_session.items()
    }
    exposure_rows = [{
        "session": session,
        "quote_rows": observed_compact.get(session, {}).get("quote_rows", 0),
        "trade_rows": observed_compact.get(session, {}).get("trade_rows", 0),
    } for session in session_ids]
    reproduced_gate = _forward_gate(
        reproduced_report, rung=rung, exposure_evidence=exposure_rows,
        minimum_opportunities=max(100, int(config.get(
            "fast_screen_min_opportunities") or 100)),
        forward_test_index=rung.forward_test_index)

    original_statistics = confirmation.get("gate_statistics") or {}
    reproducible_statistic_keys = tuple(reproduced_gate["statistics"])
    original_core = {key: original_statistics.get(key)
                     for key in reproducible_statistic_keys}
    reproduced_core = {key: reproduced_gate["statistics"].get(key)
                       for key in reproducible_statistic_keys}
    checks = {
        "raw_session_evidence_exact": observed_compact == expected_by_session,
        "confirmation_was_pass": confirmation.get("decision") == "PASS",
        "gate_version_exact": confirmation.get("gate_version") ==
            FORWARD_GATE_VERSION,
        "gate_decision_exact": reproduced_gate["decision"] ==
            confirmation.get("decision"),
        "gate_failures_exact": list(reproduced_gate["gate_failures"]) ==
            list(confirmation.get("gate_failures") or []),
        "gate_statistics_exact": stable_fingerprint(reproduced_core) ==
            stable_fingerprint(original_core),
        "confirmation_evidence_exact": str(
            request_contract.get("confirmation_evidence_fingerprint") or
            "") == str(confirmation.get(
                "confirmation_evidence_fingerprint") or ""),
    }
    failures = sorted(key for key, passed in checks.items() if not passed)
    result = {
        "version": QA_REPRODUCTION_VERSION,
        "verdict": "PASS" if not failures else "FAIL",
        "checks": checks,
        "failed_checks": failures,
        "work_item_id": str(bundle["work_item"].get("work_item_id") or ""),
        "reproduction_request_id": str(
            bundle["work_item"].get("reproduction_request_id") or ""),
        "experiment_id": identity_checks["experiment_id"],
        "forward_confirmation_id": identity_checks[
            "forward_confirmation_id"],
        "report_revision_id": identity_checks["report_revision_id"],
        "report_fingerprint": str(revision.get("report_fingerprint") or ""),
        "candidate_identity_fingerprint": str(
            candidate_payload.get("candidate_identity_fingerprint") or ""),
        "rung_plan_fingerprint": rung.rung_plan_fingerprint,
        "instrument_set_fingerprint": instrument_fp,
        "session_set_fingerprint": session_fp,
        "expected_raw_session_evidence_fingerprint": stable_fingerprint(
            expected_by_session),
        "observed_raw_session_evidence_fingerprint": stable_fingerprint(
            observed_compact),
        "original_gate_core_fingerprint": stable_fingerprint(original_core),
        "reproduced_gate_core_fingerprint": stable_fingerprint(
            reproduced_core),
        "reproduced_gate": reproduced_gate,
        "score_or_model_refit": False,
        "stock_only": True,
        "historical_61_session_reuse": False,
        "independent_process": True,
        "promotion_authority": False,
    }
    result["result_fingerprint"] = stable_fingerprint(result)
    return result


def _persist_forward_metric(meta_conn, *, experiment_id: str,
                            confirmation, gate: dict) -> None:
    dimensions = _forward_metric_dimensions(confirmation, gate)
    with meta_conn.cursor() as cur:
        cur.execute("""
            insert into quant.experiment_metrics
              (experiment_id, split, metric, value, dimensions,
               cost_model_version)
            values (%s,'WALK_FORWARD','intraday_forward_gate_pass',%s,
                    %s::jsonb,%s)
            on conflict (experiment_id, split, metric, dimensions)
            do update set value=excluded.value
        """, (experiment_id, 1 if gate["decision"] == "PASS" else 0,
              json.dumps(dimensions), COST_MODEL_VERSION))
    meta_conn.commit()


def _forward_metric_dimensions(confirmation, gate: dict) -> dict:
    return {
        "forward_confirmation_id": confirmation.forward_confirmation_id,
        "experiment_rung_id": confirmation.experiment_rung_id,
        "candidate_lineage_id": confirmation.candidate_lineage_id,
        "decision": gate["decision"],
        "gate_version": FORWARD_GATE_VERSION,
        "evidence_fingerprint": confirmation.evidence_fingerprint,
        "evidence_tier": "INDEPENDENT_FORWARD_CONFIRMATION",
        "failed_criteria": gate["gate_failures"],
        "historical_61_session_reuse": False,
        "qa_submission_requested": gate["decision"] == "PASS",
        "promotion_authority": False,
        "next_owner": ("QA_REPRODUCTION" if gate["decision"] == "PASS"
                       else "RESEARCH_FEEDBACK"),
    }


def _repair_forward_metrics(meta_conn) -> int:
    """Repair the only cross-transaction seam after confirmation commits."""
    with meta_conn.cursor() as cur:
        cur.execute("""
            select rung.experiment_id::text,
                   confirmation.forward_confirmation_id::text,
                   confirmation.experiment_rung_id::text,
                   confirmation.candidate_lineage_id::text,
                   confirmation.decision,
                   confirmation.confirmation_evidence_fingerprint,
                   confirmation.gate_failures
              from quant.intraday_forward_confirmations confirmation
              join quant.intraday_experiment_rungs rung
                on rung.experiment_rung_id = confirmation.experiment_rung_id
             where not exists (
               select 1
                 from quant.experiment_metrics metric
                where metric.experiment_id = rung.experiment_id
                  and metric.split = 'WALK_FORWARD'
                  and metric.metric = 'intraday_forward_gate_pass'
                  and metric.dimensions->>'forward_confirmation_id' =
                      confirmation.forward_confirmation_id::text
             )
             order by confirmation.confirmed_at
        """)
        rows = cur.fetchall()
        for row in rows:
            confirmation = type("ForwardMetricRepair", (), {
                "forward_confirmation_id": str(row[1]),
                "experiment_rung_id": str(row[2]),
                "candidate_lineage_id": str(row[3]),
                "evidence_fingerprint": str(row[5]),
            })()
            gate = {
                "decision": str(row[4]),
                "gate_failures": list(_as_json(row[6]) or []),
            }
            dimensions = _forward_metric_dimensions(confirmation, gate)
            cur.execute("""
                insert into quant.experiment_metrics
                  (experiment_id, split, metric, value, dimensions,
                   cost_model_version)
                values (%s,'WALK_FORWARD','intraday_forward_gate_pass',%s,
                        %s::jsonb,%s)
                on conflict (experiment_id, split, metric, dimensions)
                do update set value=excluded.value
            """, (str(row[0]), 1 if row[4] == "PASS" else 0,
                  json.dumps(dimensions), COST_MODEL_VERSION))
    meta_conn.commit()
    return len(rows)


def run_forward_confirmation(meta_conn, market_conn, candidate_row: dict,
                             *, now: datetime | None = None,
                             lease_guard: Callable[[bool], None] | None = None
                             ) -> dict:
    """Advance one nominated FULL_60 candidate through its future lockbox."""
    experiment_id = str(candidate_row["experiment_id"])
    if lease_guard is not None:
        lease_guard(True)
    if not _is_forward_nominee(candidate_row.get("final_gate")):
        return {"experiment_id": experiment_id, "status": "NOT_NOMINATED"}
    config = dict(candidate_row["config"])
    if config.get("asset_scope") != "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY":
        raise RuntimeError("forward confirmation requires STOCK-only asset scope")
    runtime_artifact = _forward_runtime_artifact_attestation(
        candidate_row.get("governance_report") or {})
    if not runtime_artifact["reproduction_route_available"]:
        return {
            "experiment_id": experiment_id,
            "status": "FROZEN_RUNTIME_ARTIFACT_UNAVAILABLE",
            "decision": "INCONCLUSIVE",
            "failed_criteria": ["FORWARD_RUNTIME_ARTIFACT_UNAVAILABLE"],
            "runtime_artifact": runtime_artifact,
            "historical_61_session_reuse": False,
        }
    candidate = load_candidate_lineage(
        meta_conn, candidate_row["candidate_lineage_id"])
    _validate_frozen_candidate(config, candidate)
    full_rung = load_experiment_rung(
        meta_conn, experiment_id=experiment_id, rung=FULL_60,
        candidate=candidate)
    instrument_ids = _forward_stock_universe(meta_conn, full_rung)
    spec = _forward_spec(config)
    required = max(20, int(config.get(
        "forward_confirmation_min_new_sessions") or 20))
    frozen_at = candidate_row["frozen_at"]
    if frozen_at.tzinfo is None:
        frozen_at = frozen_at.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("forward knowledge cutoff must be timezone aware")

    if candidate_row.get("forward_rung_id"):
        forward_rung = load_experiment_rung(
            meta_conn, experiment_id=experiment_id, rung=FORWARD,
            candidate=candidate)
        cutoff = candidate_row.get("forward_dataset_cutoff")
        if cutoff is None:
            raise RuntimeError("allocated forward rung lacks its dataset cutoff")
        cohort = _forward_sessions(
            meta_conn,
            search_cutoff=candidate_row["search_cutoff"], frozen_at=frozen_at,
            knowledge_cutoff=cutoff, required_sessions=required)
        if tuple(cohort["sessions"]) != forward_rung.planned_session_dates:
            raise RuntimeError(
                "allocated forward cohort is not the first chronological "
                "eligible session set")
        instrument_ids = _forward_stock_universe(
            meta_conn, full_rung,
            session_dates=forward_rung.planned_session_dates)
    else:
        availability_cutoff = current.astimezone(timezone.utc)
        cohort = _forward_sessions(
            meta_conn,
            search_cutoff=candidate_row["search_cutoff"], frozen_at=frozen_at,
            knowledge_cutoff=availability_cutoff, required_sessions=required)
        if cohort["status"] != "READY":
            return {
                "experiment_id": experiment_id,
                "candidate_lineage_id": candidate.candidate_lineage_id,
                "status": "WAITING_FOR_NEW_LOCAL_SESSIONS",
                "available_sessions": cohort["available_sessions"],
                "required_sessions": cohort["required_sessions"],
                "historical_61_session_reuse": False,
            }
        # Do not persist wall-clock ``now``: two workers selecting the same
        # nominee must construct an identical plan.  Re-read the calendar at
        # the deterministic raw-data cutoff and freeze that exact manifest.
        cutoff = _deterministic_forward_dataset_cutoff(
            cohort, spec, timestamp_policy=config.get(
                "timestamp_policy", STRICT_TIMESTAMP_POLICY))
        frozen_cohort = _forward_sessions(
            meta_conn,
            search_cutoff=candidate_row["search_cutoff"], frozen_at=frozen_at,
            knowledge_cutoff=cutoff, required_sessions=required)
        if (frozen_cohort["status"] != "READY"
                or frozen_cohort["sessions"] != cohort["sessions"]):
            raise RuntimeError(
                "forward calendar cohort changed at deterministic cutoff")
        cohort = frozen_cohort
        instrument_ids = _forward_stock_universe(
            meta_conn, full_rung, session_dates=cohort["sessions"])
        forward_rung = allocate_experiment_rung(
            meta_conn, candidate=candidate, experiment_id=experiment_id,
            dataset_id=candidate_row["dataset_id"], rung=FORWARD,
            session_dates=cohort["sessions"], instrument_ids=instrument_ids,
            selection_policy_version=(
                "first-n-versioned-krx-calendar-stock-sessions-v2"),
            dataset_cutoff=cutoff,
            source_watermark={
                "version": FORWARD_RUNNER_VERSION,
                "cohort": {key: value for key, value in cohort.items()
                           if key != "sessions"},
                "candidate_frozen_at": frozen_at.isoformat(),
                "event_source": LOCAL_EVENT_SOURCE,
            },
            allocation_reason=(
                "fixed-horizon independent confirmation over every first "
                "eligible future local STOCK session without skipping"),
            allocated_by="svc_quant/intraday-forward-confirmation",
            predecessor=full_rung,
            lockbox_cutoff_session_date=candidate_row["search_cutoff"],
        )

    if lease_guard is not None:
        lease_guard(True)
    forward_accesses = _record_forward_accesses(
        meta_conn, rung=forward_rung, instrument_ids=instrument_ids,
        cutoff=cutoff, spec=spec,
        timestamp_policy=config.get(
            "timestamp_policy", STRICT_TIMESTAMP_POLICY))
    forward_report = _evaluate_forward_replay(
        market_conn, config=config, spec=spec, rung=forward_rung,
        instrument_ids=instrument_ids, cutoff=cutoff,
        score_calibration=candidate_row["score_calibration"],
        governance_report=candidate_row["governance_report"],
        lease_guard=lease_guard)
    if lease_guard is not None:
        lease_guard(True)
    exposure_evidence = _record_forward_exposures(
        meta_conn, accesses=forward_accesses,
        replay_evidence=(forward_report.get("forward_replay") or {}).get(
            "session_evidence") or {},
        rung=forward_rung, instrument_ids=instrument_ids, cutoff=cutoff,
        spec=spec, timestamp_policy=config.get(
            "timestamp_policy", STRICT_TIMESTAMP_POLICY))
    test_index = _forward_test_index(
        meta_conn, forward_rung.experiment_rung_id)
    gate = _forward_gate(
        forward_report, rung=forward_rung,
        exposure_evidence=exposure_evidence,
        minimum_opportunities=max(100, int(config.get(
            "fast_screen_min_opportunities") or 100)),
        forward_test_index=test_index)
    # The forward replay ran only after every raw access/exposure was durably
    # recorded.  If its deployment differs from the frozen FULL_60 evaluator,
    # preserve that ledger but discard performance authority fail-closed.
    gate, runtime_artifact = _forward_gate_with_runtime_artifact(
        gate, candidate_row.get("governance_report") or {})
    gate_statistics = {
        **gate["statistics"],
        "candidate_identity_fingerprint": (
            candidate.candidate_identity_fingerprint),
        "candidate_ast_fingerprint": candidate.candidate_ast_fingerprint,
        "forward_rung_plan_fingerprint": forward_rung.rung_plan_fingerprint,
        "score_calibration_fingerprint": stable_fingerprint(
            candidate_row["score_calibration"]),
        "exposure_evidence_fingerprints": [
            row["evidence_fingerprint"] for row in exposure_evidence],
        "stock_universe_fingerprint": stable_fingerprint(instrument_ids),
        "stock_universe_count": len(instrument_ids),
        "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        "event_source": LOCAL_EVENT_SOURCE,
        "knowledge_clock_mode": ARRIVAL_TIME_CAUSAL,
        "candidate_frozen_at": frozen_at.isoformat(),
        "knowledge_cutoff": cutoff.isoformat(),
        "historical_61_session_reuse": False,
    }
    if lease_guard is not None:
        lease_guard(True)
    confirmation = record_forward_confirmation(
        meta_conn, rung=forward_rung, decision=gate["decision"],
        gate_version=FORWARD_GATE_VERSION, gate_statistics=gate_statistics,
        gate_failures=gate["gate_failures"],
        decision_reason=(
            "frozen runtime artifact is unavailable after deployment; "
            "forward performance was held as inconclusive"
            if not runtime_artifact["reproduction_route_available"] else
            "fixed future-session gate passed without refit"
            if gate["decision"] == "PASS" else
            "fixed future-session gate did not establish cost-net alpha"),
        confirmed_by="svc_quant/intraday-forward-confirmation",
    )
    _persist_forward_metric(
        meta_conn, experiment_id=experiment_id,
        confirmation=confirmation, gate=gate)
    if lease_guard is not None:
        lease_guard(True)
    publication = _publish_forward_finalization(meta_conn, experiment_id)
    return {
        "experiment_id": experiment_id,
        "candidate_lineage_id": candidate.candidate_lineage_id,
        "forward_confirmation_id": confirmation.forward_confirmation_id,
        "status": "CONFIRMED",
        "decision": gate["decision"],
        "failed_criteria": gate["gate_failures"],
        "sessions": len(forward_rung.planned_session_dates),
        "stocks": len(instrument_ids),
        "historical_61_session_reuse": False,
        "publication": publication,
    }


def run_forward_confirmations(meta_conn, market_conn, *, limit: int = 4,
                              now: datetime | None = None,
                              worker: str = "manual-forward-sweep") -> dict:
    """Lease a fair due batch; one waiting/error row cannot block later work."""
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ValueError("forward sweep timestamp must be timezone aware")
    repaired_metrics = _repair_forward_metrics(meta_conn)
    repaired_publications = _repair_forward_publications(
        meta_conn, limit=max(100, int(limit) * 4))
    reconciled = _reconcile_forward_work_items(meta_conn)
    enqueued = _enqueue_forward_candidates(meta_conn)
    claims = _lease_forward_work_items(
        meta_conn, limit=limit, worker=worker, now=current)
    experiment_ids = [claim["experiment_id"] for claim in claims]
    rows = _forward_candidate_rows(
        meta_conn, experiment_ids=experiment_ids)
    rows_by_id = {row["experiment_id"]: row for row in rows}
    results = []
    for claim in claims:
        heartbeat_at = (current if now is not None
                        else datetime.now(timezone.utc))
        last_heartbeat_at = heartbeat_at
        try:
            lease_owned = _heartbeat_forward_work_item(
                meta_conn, claim=claim, worker=worker, now=heartbeat_at)
        except Exception as heartbeat_exc:
            result = {
                "experiment_id": claim["experiment_id"],
                "status": "ERROR",
                "error": (f"forward lease heartbeat failed: "
                          f"{type(heartbeat_exc).__name__}: {heartbeat_exc}")[
                              :400],
            }
            try:
                lease_released = _finish_forward_work_item(
                    meta_conn, claim=claim, result=result,
                    worker=worker, now=heartbeat_at)
            except Exception:
                lease_released = False
            results.append({**result, "lease_released": lease_released})
            continue
        if not lease_owned:
            results.append({
                "experiment_id": claim["experiment_id"],
                "status": "LEASE_LOST",
                "lease_released": False,
            })
            continue

        def lease_guard(force: bool = False) -> None:
            nonlocal last_heartbeat_at
            instant = (current if now is not None
                       else datetime.now(timezone.utc))
            if (not force
                    and instant - last_heartbeat_at < timedelta(minutes=30)):
                return
            if not _heartbeat_forward_work_item(
                    meta_conn, claim=claim, worker=worker, now=instant):
                raise RuntimeError(
                    "forward work lease was lost during raw replay")
            last_heartbeat_at = instant
        row = rows_by_id.get(claim["experiment_id"])
        if row is None:
            result = {
                "experiment_id": claim["experiment_id"],
                "status": "NOT_NOMINATED",
                "reason": "candidate ceased to satisfy the frozen nominee gate",
            }
            try:
                lease_released = _finish_forward_work_item(
                    meta_conn, claim=claim, result=result,
                    worker=worker, now=current)
            except Exception as finish_exc:
                lease_released = False
                result["work_item_release_error"] = (
                    f"{type(finish_exc).__name__}: {finish_exc}"[:400])
            results.append({**result, "lease_released": lease_released})
            continue
        try:
            result = run_forward_confirmation(
                meta_conn, market_conn, row, now=current,
                lease_guard=lease_guard)
        except Exception as exc:  # fail closed and leave append-only facts intact
            try:
                meta_conn.rollback()
            except Exception:
                pass
            result = {
                "experiment_id": row.get("experiment_id"),
                "status": "ERROR",
                "error": f"{type(exc).__name__}: {exc}"[:400],
            }
        try:
            finish_now = (current if now is not None
                          else datetime.now(timezone.utc))
            lease_released = _finish_forward_work_item(
                meta_conn, claim=claim, result=result,
                worker=worker, now=finish_now)
        except Exception as finish_exc:
            lease_released = False
            result = {
                **result,
                "work_item_release_error": (
                    f"{type(finish_exc).__name__}: {finish_exc}"[:400]),
            }
        results.append({**result, "lease_released": lease_released})
    return {
        "version": FORWARD_RUNNER_VERSION,
        "checked": len(claims),
        "enqueued": enqueued,
        "leased": len(claims),
        "reconciled_work_items": reconciled,
        "repaired_forward_metrics": repaired_metrics,
        "repaired_forward_publications": repaired_publications,
        "results": results,
        "historical_61_session_reuse": False,
    }


def _run_fast_screen(config: dict, spec: IntradayLaneSpec, selected: dict, *,
                     market_conn, cutoff: datetime, event_source: str,
                     trials: int, source_lineage,
                     rung: str = DISCOVERY_6,
                     planned_sessions=None, planned_instruments=None,
                     planned_panel_manifest=None) -> tuple[dict, dict]:
    """Replay a small deterministic panel before an all-universe raw run."""
    purge_gap = effective_purge_gap(spec, config["timestamp_policy"])
    sessions = ([_session_day(value) for value in planned_sessions]
                if planned_sessions is not None else _nested_session_prefix(
                    selected["sessions"], config["fast_screen_sessions"]))
    if planned_instruments is None:
        instruments, panel_manifest = _profiled_panel(
            selected, config["fast_screen_instruments"])
    else:
        instruments = [str(value) for value in planned_instruments]
        panel_manifest = dict(planned_panel_manifest or {})
    # A liquid symbol can contain roughly 600k event rows per session. Keep an
    # external shard to one instrument so Python object materialization cannot
    # multiply that footprint before build_samples releases the raw events.
    shard_size = (1 if event_source == EXTERNAL_EVENT_SOURCE else
                  min(config["instrument_shard_size"], 8))
    shards = [instruments[index:index + shard_size]
              for index in range(0, len(instruments), shard_size)]
    explicit_cube = (_feature_window_contract(config) ==
                     EXPLICIT_FEATURE_WINDOW_CONTRACT)
    cube_spec = _feature_cube_spec(config) if explicit_cube else None
    cache = SampleCache(cache_identity(
        spec=spec, event_source=event_source,
        execution_model=config["population_execution_model"],
        source_lineage=source_lineage,
        timestamp_policy=config["timestamp_policy"],
        feature_cube_spec=cube_spec))
    cache_stats = {"hits": 0, "misses": 0, "writes": 0,
                   "write_disabled": 0}
    if explicit_cube:
        cache_stats["feature_cube_cache"] = "ENABLED_VERSIONED_BATCH_V2"
    try:
        cache_stats["prune"] = prune_sample_cache()
    except OSError as exc:
        cache_stats["prune"] = {
            "status": "UNAVAILABLE", "error": type(exc).__name__}

    def samples_for(day, shard) -> dict[str, list]:
        session = _session_day(day)
        start, end = _session_bounds(session)
        load_end = end + purge_gap
        if event_source == EXTERNAL_EVENT_SOURCE:
            load_end = min(load_end, _external_content_end(session))
        if event_source != EXTERNAL_EVENT_SOURCE:
            events = load_instrument_events_batch(
                market_conn, instrument_ids=shard, start=start,
                end=load_end, as_known_at=cutoff,
                source=event_source)
            return {instrument: _build_runtime_samples(
                config, *events[instrument], spec, start=start, end=end)
                for instrument in shard}

        # External shards are deliberately one symbol wide. This also makes a
        # cache file the exact bounded unit rebuilt after a miss or corruption.
        instrument = shard[0]
        cached = (
            cache.load_batch(session, instrument, expected_spec=cube_spec)
            if explicit_cube else cache.load(session, instrument))
        if cached is not None:
            cache_stats["hits"] += 1
            return {instrument: cached}
        cache_stats["misses"] += 1
        events = load_instrument_events_batch(
            market_conn, instrument_ids=shard, start=start,
            end=load_end, as_known_at=cutoff,
            source=event_source)
        samples = _build_runtime_samples(
            config, *events[instrument], spec, start=start, end=end)
        stored = (cache.store_batch(session, instrument, samples)
                  if explicit_cube else
                  cache.store(session, instrument, samples))
        if stored:
            cache_stats["writes"] += 1
        else:
            cache_stats["write_disabled"] += 1
        return {instrument: samples}

    population = CandidatePopulationAccumulator(_candidate_accumulators(
        config, spec, trials=trials))
    calibration_shards = []
    if population.requires_calibration:
        for shard_number, shard in enumerate(shards, 1):
            sample_count = 0
            for raw_day in selected.get("calibration_sessions") or []:
                by_instrument = samples_for(raw_day, shard)
                for instrument in shard:
                    samples = by_instrument[instrument]
                    sample_count += len(samples)
                    population.calibrate(instrument, samples)
            calibration_shards.append({
                "shard": shard_number, "instrument_count": len(shard),
                "sample_count": sample_count,
            })
    calibration = population.freeze_calibration()
    calibration_only_fail_fast = _all_symbolic_candidates_cost_infeasible(
        calibration)

    screen_shards = []
    if not calibration_only_fail_fast:
        for shard_number, shard in enumerate(shards, 1):
            sample_count = 0
            for raw_day in sessions:
                by_instrument = samples_for(raw_day, shard)
                for instrument in shard:
                    samples = by_instrument[instrument]
                    sample_count += len(samples)
                    population.add(instrument, samples)
            screen_shards.append({
                "shard": shard_number, "instrument_count": len(shard),
                "sample_count": sample_count,
                "instrument_fingerprint": hashlib.sha256(
                    "|".join(shard).encode()).hexdigest()[:16],
            })

    finished_population = population.finish()
    if calibration_only_fail_fast:
        _annotate_calibration_only_failures(finished_population, calibration)
    report = _annotate_population(config, finished_population)
    report["multiple_testing"] = _population_multiple_testing(report)
    _apply_population_dsr_gate(report, report["multiple_testing"])
    gate = _fast_screen_gate(report, config)
    measured_calibration_observations = max(
        (int((row or {}).get("observations") or 0)
         for row in calibration.values()), default=0)
    measured_calibration_instruments = max(
        (int((row or {}).get("instruments") or 0)
         for row in calibration.values()), default=0)
    report["fast_discovery_screen"] = {
        **gate,
        "evaluation_status": (
            "SKIPPED_COST_INFEASIBLE" if calibration_only_fail_fast
            else "EVALUATED"),
        "evaluation_skip_reason": (
            "ALL_SYMBOLIC_CANDIDATES_INFEASIBLE"
            if calibration_only_fail_fast else None),
        "calibration_sample_count": sum(
            int(row.get("sample_count") or 0) for row in calibration_shards),
        "calibration_instrument_count": measured_calibration_instruments,
        "calibration_observations": measured_calibration_observations,
        "calibration_is_measured_search_memory": bool(
            calibration_only_fail_fast
            and measured_calibration_observations > 0
            and measured_calibration_instruments > 0),
        "raw_data_absence_inferred": (
            False if calibration_only_fail_fast
            and measured_calibration_observations > 0 else None),
        "rung": str(rung),
        "sessions": [str(day) for day in sessions],
        "evaluated_sessions": (
            [] if calibration_only_fail_fast else
            [str(day) for day in sessions]),
        "session_count": len(sessions),
        "evaluated_session_count": (
            0 if calibration_only_fail_fast else len(sessions)),
        "instrument_count": len(instruments),
        "instrument_fingerprint": hashlib.sha256(
            "|".join(instruments).encode()).hexdigest(),
        "selection": (
            "calibration-profiled information-rich bracket plus deterministic "
            "representative activity guard"),
        "panel_manifest": panel_manifest,
        "full_universe_instrument_count": len(selected["instruments"]),
        "full_evaluation_session_count": len(selected["sessions"]),
        "sample_cache": {**cache_stats, "identity": cache.identity,
                         "scope": "DISCOVERY_PANEL_ONLY",
                         "evidence_authority": "NONE",
                         "promotion_authority": False},
        "cost_feasibility_preflight": {
            "status": ("ALL_SYMBOLIC_CANDIDATES_INFEASIBLE"
                       if calibration_only_fail_fast else "CONTINUE"),
            "calibration_only_fail_fast": calibration_only_fail_fast,
            "candidate_statuses": {
                key: str((value or {}).get("status") or "")
                for key, value in sorted(calibration.items())
            },
            "rule": (
                "maximum calibrated predicted markout must exceed the "
                "minimum observed executable entry hurdle"),
            "promotion_authority": False,
        },
    }
    report["score_calibration"] = calibration.get("PRIMARY")
    report["calibration_population"] = calibration
    report["calibration_shards"] = calibration_shards
    report["universe_shards"] = screen_shards
    report["lane_manifest"].update(manifest(
        spec, source=event_source,
        timestamp_policy=config["timestamp_policy"]))
    return report, gate


def run(hyp: dict, hypothesis_id: str, *, meta_conn, market_conn) -> dict:
    prepared = hyp.get("_intraday_preflight") or prepare(
        hyp, market_conn=market_conn, meta_conn=meta_conn)
    config = prepared["config"]
    spec = prepared["spec"]
    cutoff = prepared["cutoff"]
    selected = prepared["selected"]
    if (selected.get("status") == "PASS"
            and selected.get("statistical_readiness") != "FULL"):
        raise RuntimeError(
            "intraday governed replay requires one calibration plus exactly "
            "60 evaluation sessions; short history remains diagnostic-only "
            "and must not allocate a DISCOVERY_6 trial")
    if selected.get("status") == "PASS":
        # A cached/preflight marker is not authority. Revalidate reference
        # identity on every run and retry before any rung is allocated.
        prior_filter_audit = {
            key: selected.get(key) for key in (
                "product_filter", "product_filter_version",
                "pre_product_filter_instruments",
                "post_product_filter_instruments",
                "product_filter_excluded",
                "reference_identity_fingerprint",
                "symbol_identity_as_of",
            ) if key in selected
        }
        selected = _stock_only_slice(meta_conn, market_conn, selected)
        _assert_stock_selection_evidence(selected)
        selected["initial_product_filter_audit"] = prior_filter_audit
        selected["reference_identity_revalidated"] = True
    if selected.get("status") != "PASS":
        raise RuntimeError(
            "intraday run called with non-executable feasibility slice: "
            f"{selected.get('status')}")
    _assert_resolved_data_contract(config, selected=selected)
    lineage = _lineage(market_conn, selected, cutoff)
    event_source = selected.get("event_source", LOCAL_EVENT_SOURCE)
    effective_shard_size = (1 if event_source == EXTERNAL_EVENT_SOURCE else
                            config["instrument_shard_size"])
    purge_gap = effective_purge_gap(spec, config["timestamp_policy"])
    persisted = {**config,
                 "effective_instrument_shard_size": effective_shard_size,
                 "cutoff": cutoff.isoformat(),
                 "slice": {**selected,
                           "sessions": [str(day) for day in selected["sessions"]]},
                 "source_lineage": lineage,
                 "lane_manifest": manifest(
                     spec, source=event_source,
                     timestamp_policy=config["timestamp_policy"])}
    experiment_id, dataset_id, duplicate = _register(
        meta_conn, hypothesis_id, persisted)
    if duplicate:
        report = _load_completed_report(meta_conn, experiment_id)
        return {
            "experiment_id": experiment_id, "duplicate": True,
            "fragility": "INSUFFICIENT" if report.get("evidence_tier") ==
                         "SEARCH_EXPOSED_HISTORICAL_SUPPORT" else
                         "ROBUST" if report["decision"] == "SUBMIT_TO_QA" else
                         "INSUFFICIENT" if report["decision"] == "NO_EVIDENCE" else
                         "FRAGILE",
            "backtest_metrics": {
                "turnover_total": report["summary"].get("opportunities", 0),
                "total_return": report["summary"].get(
                    "mean_net_bps_per_opportunity")},
            "intraday_report": report, "research_lane": "INTRADAY_EVENT"}

    try:
        primary_lineage, candidate_lineages = _register_trial_lineages(
            meta_conn, hypothesis_id=hypothesis_id, config=config)
        trial_schedule = _allocate_trial_schedule(
            meta_conn, primary=primary_lineage, experiment_id=experiment_id,
            dataset_id=dataset_id, config=config, spec=spec, selected=selected,
            source_lineage=lineage)
        # Every raw/quality query below must use exactly the cutoff frozen in
        # the rung lockbox.  The preflight wall clock is selection metadata and
        # must never silently widen a retry's observable source snapshot.
        cutoff = _aware_cutoff(datetime.fromisoformat(
            str(trial_schedule["dataset_cutoff"])))
        trial_lockbox = {
            "version": TRIAL_LEDGER_VERSION,
            "status": "HISTORICAL_SEARCH_SCHEDULE_FROZEN",
            "primary_candidate_lineage_id": primary_lineage.candidate_lineage_id,
            "root_lineage_id": primary_lineage.root_lineage_id,
            "registered_candidate_lineages": {
                key: value.candidate_lineage_id
                for key, value in sorted(candidate_lineages.items())},
            "dataset_id": dataset_id,
            "dataset_cutoff": trial_schedule["dataset_cutoff"],
            "rungs": {
                key: (value["rung"].experiment_rung_id if value else None)
                for key, value in trial_schedule.items()
                if key in {"calibration", "discovery", "validation", "full"}},
            "exposures": [],
            "append_before_raw_replay": True,
            "existing_61_sessions_declared_unused": False,
        }
    except Exception:
        _mark_experiment_failed(meta_conn, experiment_id)
        raise

    discovery_rungs: list[dict] = []
    full_config = config
    screen_report = None

    frozen_lineage_fingerprint = stable_fingerprint(lineage)

    def assert_source_snapshot_unchanged() -> None:
        observed = _lineage(market_conn, selected, cutoff)
        if stable_fingerprint(observed) != frozen_lineage_fingerprint:
            raise RuntimeError(
                "intraday source content changed after lockbox allocation; "
                "discarding replay so a new content-addressed trial can run")

    def stop_after_screen(report: dict, *, failure_code: str,
                          screen_key: str) -> dict:
        failed = list(report.get("failed_criteria") or [])
        for code in (failure_code, "FULL_UNIVERSE_CONFIRMATION_NOT_RUN"):
            if code not in failed:
                failed.append(code)
        screen = report[screen_key]
        report.update({
            "decision": "NO_EVIDENCE",
            "evidence_tier": "ADAPTIVE_DISCOVERY_ONLY",
            "failed_criteria": failed,
            "discovery_rungs": discovery_rungs,
            "trial_lockbox": trial_lockbox,
            "not_a_promotion": (
                "A deterministic discovery rung rejected further resource "
                "allocation. Every viewed session remains search-exposed; "
                "linked survivors are nominations only."),
            "source_quality": {
                "status": "NOT_RUN_DISCOVERY_SCREEN",
                "reason": "quality scan is reserved for full replay",
            },
            "slice": {
                **persisted["slice"],
                "evaluated_sessions": screen.get(
                    "evaluated_sessions", screen["sessions"]),
                "evaluated_instrument_count": screen["instrument_count"],
                "evidence_tier": "ADAPTIVE_DISCOVERY_ONLY",
            },
        })
        assert_source_snapshot_unchanged()
        _guard_experiment_step(
            meta_conn, experiment_id, _store_report,
            meta_conn, experiment_id, report)
        return {
            "experiment_id": experiment_id,
            "fragility": "INSUFFICIENT",
            "backtest_metrics": {
                "turnover_total": report["summary"].get("opportunities", 0),
                "total_return": report["summary"].get(
                    "mean_net_bps_per_opportunity"),
            },
            "intraday_report": report,
            "research_lane": "INTRADAY_EVENT",
        }

    if (event_source == EXTERNAL_EVENT_SOURCE
            and config["fast_screen_enabled"]):
        rung_exposure_manifests = {}
        for schedule_key in ("calibration", "discovery"):
            exposure_manifest = _guard_experiment_step(
                meta_conn, experiment_id, _record_rung_exposures,
                meta_conn, market_conn,
                schedule_row=trial_schedule[schedule_key], selected=selected,
                cutoff=cutoff, event_source=event_source,
                knowledge_cutoff=trial_schedule["dataset_cutoff"])
            trial_lockbox["exposures"].append(exposure_manifest)
            rung_exposure_manifests[schedule_key] = exposure_manifest
        discovery_plan = trial_schedule["discovery"]
        screen_report, screen_gate = _guard_experiment_step(
            meta_conn, experiment_id, _run_fast_screen,
            config, spec, selected, market_conn=market_conn, cutoff=cutoff,
            event_source=event_source, trials=int(hyp.get("_trials") or 1),
            source_lineage=lineage, rung=DISCOVERY_6,
            planned_sessions=discovery_plan["rung"].planned_session_dates,
            planned_instruments=discovery_plan["evaluation_keys"],
            planned_panel_manifest=discovery_plan["panel"])
        discovery_rungs.append(_completed_adaptive_rung_evidence(
            screen_report, screen=screen_report["fast_discovery_screen"],
            config=config, spec=spec, selected=selected,
            schedule_row=discovery_plan,
            exposure_manifest=rung_exposure_manifests["discovery"],
            dataset_id=dataset_id,
            dataset_cutoff=trial_schedule["dataset_cutoff"],
            event_source=event_source, source_lineage=lineage,
            completed_at=datetime.now(timezone.utc)))
        if not screen_gate["primary_pass"]:
            return stop_after_screen(
                screen_report, failure_code="FAST_DISCOVERY_SCREEN_REJECTED",
                screen_key="fast_discovery_screen")

        intermediate_config, halving = _next_rung_config(
            config, screen_report, screen_gate,
            candidate_budget=config["intermediate_candidate_budget"])
        discovery_rungs[-1]["next_rung_selection"] = halving
        if config["intermediate_screen_enabled"]:
            validation_plan = trial_schedule.get("validation")
            if validation_plan is None:
                raise RuntimeError(
                    "intermediate replay lacks a frozen VALIDATION_20 rung")
            validation_exposure_manifest = _guard_experiment_step(
                meta_conn, experiment_id, _record_rung_exposures,
                meta_conn, market_conn, schedule_row=validation_plan,
                selected=selected, cutoff=cutoff, event_source=event_source,
                knowledge_cutoff=trial_schedule["dataset_cutoff"])
            trial_lockbox["exposures"].append(validation_exposure_manifest)
            intermediate_run_config = {
                **intermediate_config,
                "fast_screen_sessions": config[
                    "intermediate_screen_sessions"],
                "fast_screen_instruments": config[
                    "intermediate_screen_instruments"],
            }
            intermediate_report, intermediate_gate = _guard_experiment_step(
                meta_conn, experiment_id, _run_fast_screen,
                intermediate_run_config, spec, selected,
                market_conn=market_conn, cutoff=cutoff,
                event_source=event_source,
                trials=int(hyp.get("_trials") or 1),
                source_lineage=lineage, rung=VALIDATION_20,
                planned_sessions=validation_plan["rung"].planned_session_dates,
                planned_instruments=validation_plan["evaluation_keys"],
                planned_panel_manifest=validation_plan["panel"])
            intermediate_screen = intermediate_report.pop(
                "fast_discovery_screen")
            intermediate_report["intermediate_discovery_screen"] = \
                intermediate_screen
            discovery_rungs.append(_completed_adaptive_rung_evidence(
                intermediate_report, screen=intermediate_screen,
                config=intermediate_run_config, spec=spec, selected=selected,
                schedule_row=validation_plan,
                exposure_manifest=validation_exposure_manifest,
                dataset_id=dataset_id,
                dataset_cutoff=trial_schedule["dataset_cutoff"],
                event_source=event_source, source_lineage=lineage,
                completed_at=datetime.now(timezone.utc)))
            if not intermediate_gate["primary_pass"]:
                return stop_after_screen(
                    intermediate_report,
                    failure_code="INTERMEDIATE_DISCOVERY_SCREEN_REJECTED",
                    screen_key="intermediate_discovery_screen")
            full_config, final_halving = _next_rung_config(
                intermediate_config, intermediate_report, intermediate_gate,
                candidate_budget=1)
            discovery_rungs[-1]["next_rung_selection"] = final_halving
            screen_report = intermediate_report
        else:
            full_config = intermediate_config

        # The early rungs control whether the expensive all-stock replay is
        # allocated.  Once allocated, FULL_60 contains every preregistered
        # formula counted by the synchronous population so DSR/PBO have a
        # complete trial matrix.  Sidecars remain screening-only and can never
        # replace the primary candidate.
        full_config = {
            **full_config,
            "screening_population": list(config["screening_population"]),
            "screening_trial_exposure": max(
                len(config["screening_population"]),
                int(full_config.get("screening_trial_exposure") or 0)),
        }

    if trial_schedule.get("full") is None:
        # A 6/20-session bracket is useful search feedback but cannot be
        # relabelled as the all-stock FULL_60 evidence rung.
        if screen_report is not None:
            screen_key = ("intermediate_discovery_screen"
                          if "intermediate_discovery_screen" in screen_report
                          else "fast_discovery_screen")
            return stop_after_screen(
                screen_report,
                failure_code="HISTORICAL_SESSION_COUNT_BELOW_FULL_RUNG",
                screen_key=screen_key)
        raise RuntimeError(
            "exactly 60 frozen evaluation sessions are required for a full "
            "all-stock intraday replay")

    recorded_rungs = {row["rung"] for row in trial_lockbox["exposures"]}
    if CALIBRATION not in recorded_rungs:
        trial_lockbox["exposures"].append(_guard_experiment_step(
            meta_conn, experiment_id, _record_rung_exposures,
            meta_conn, market_conn,
            schedule_row=trial_schedule["calibration"], selected=selected,
            cutoff=cutoff, event_source=event_source,
            knowledge_cutoff=trial_schedule["dataset_cutoff"]))
    trial_lockbox["exposures"].append(_guard_experiment_step(
        meta_conn, experiment_id, _record_rung_exposures,
        meta_conn, market_conn, schedule_row=trial_schedule["full"],
        selected=selected, cutoff=cutoff, event_source=event_source,
        knowledge_cutoff=trial_schedule["dataset_cutoff"]))

    population = CandidatePopulationAccumulator(_candidate_accumulators(
        full_config, spec, trials=int(hyp.get("_trials") or 1)))
    shard_size = effective_shard_size
    shards = [selected["instruments"][index:index + shard_size]
              for index in range(0, len(selected["instruments"]), shard_size)]
    quality_counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "NO_DATA": 0}
    quality_totals = {
        "total_quotes": 0, "eligible_quotes": 0,
        "quotes_without_received_at": 0, "nonpositive_quotes": 0,
        "crossed_quotes": 0,
    }
    quality_examples = []
    calibration_shard_reports = []
    shard_reports = []
    raw_replay_rows: list[dict] = []
    expected_raw_replay_fingerprint = None
    expected_raw_replay_rows = None
    if event_source == EXTERNAL_EVENT_SOURCE:
        fingerprints = {row.get("consumed_replay_content_fingerprint")
                        for row in lineage}
        manifest_counts = {row.get(
            "consumed_replay_content_manifest_rows") for row in lineage}
        if (len(fingerprints) != 1 or None in fingerprints
                or len(manifest_counts) != 1 or None in manifest_counts):
            raise RuntimeError(
                "external lineage lacks one frozen consumed replay manifest")
        expected_raw_replay_fingerprint = next(iter(fingerprints))
        expected_raw_replay_rows = int(next(iter(manifest_counts)))
    try:
        # Fit only the one-parameter score scale on sessions strictly preceding
        # the OOS slice.  All universe shards contribute before the coefficient
        # is frozen, so shard order cannot turn into a hidden model choice.
        if (population.requires_calibration
                or event_source == EXTERNAL_EVENT_SOURCE):
            for shard_number, instruments in enumerate(shards, 1):
                shard_samples = 0
                for raw_day in selected.get("calibration_sessions") or []:
                    day = _session_day(raw_day)
                    start, end = _session_bounds(day)
                    sample_load_end = min(
                        end + purge_gap, _external_content_end(day)) \
                        if event_source == EXTERNAL_EVENT_SOURCE else \
                        end + purge_gap
                    replay_load_end = _replay_load_end(
                        day, event_source, sample_load_end)
                    events = load_instrument_events_batch(
                        market_conn, instrument_ids=instruments, start=start,
                        end=replay_load_end, as_known_at=cutoff,
                        source=event_source,
                        raw_content_evidence=(raw_content := {})
                        if event_source == EXTERNAL_EVENT_SOURCE else None,
                        content_end=(_external_content_end(day)
                                     if event_source == EXTERNAL_EVENT_SOURCE
                                     else None))
                    if event_source == EXTERNAL_EVENT_SOURCE:
                        raw_replay_rows.extend(
                            _external_raw_replay_row(
                                day, instrument, raw_content[instrument])
                            for instrument in instruments)
                    if population.requires_calibration:
                        for instrument in instruments:
                            quotes, trades = events[instrument]
                            samples = _build_runtime_samples(
                                config, quotes, trades, spec,
                                start=start, end=end)
                            shard_samples += len(samples)
                            population.calibrate(instrument, samples)
                calibration_shard_reports.append({
                    "shard": shard_number,
                    "instrument_count": len(instruments),
                    "sample_count": shard_samples,
                })
        calibration_reports = population.freeze_calibration()

        for shard_number, instruments in enumerate(shards, 1):
            shard_samples = 0
            for day in selected["sessions"]:
                start, end = _session_bounds(day)
                sample_load_end = end + purge_gap
                if event_source == EXTERNAL_EVENT_SOURCE:
                    sample_load_end = min(
                        sample_load_end, _external_content_end(day))
                replay_load_end = _replay_load_end(
                    day, event_source, sample_load_end)
                quality = source_quality_batch(
                    market_conn, instrument_ids=instruments, start=start,
                    end=sample_load_end, as_known_at=cutoff,
                    source=event_source)
                events = load_instrument_events_batch(
                    market_conn, instrument_ids=instruments, start=start,
                    end=replay_load_end, as_known_at=cutoff,
                    source=event_source,
                    raw_content_evidence=(raw_content := {})
                    if event_source == EXTERNAL_EVENT_SOURCE else None,
                    content_end=(_external_content_end(day)
                                 if event_source == EXTERNAL_EVENT_SOURCE
                                 else None))
                if event_source == EXTERNAL_EVENT_SOURCE:
                    raw_replay_rows.extend(
                        _external_raw_replay_row(day, instrument,
                                                 raw_content[instrument])
                        for instrument in instruments)
                for instrument in instruments:
                    q = quality[instrument]
                    quality_counts[q["status"]] += 1
                    for key in quality_totals:
                        quality_totals[key] += q[key]
                    if q["status"] != "PASS" and len(quality_examples) < 50:
                        quality_examples.append({
                            "instrument_id": instrument, "session": str(day), **q})
                    quotes, trades = events[instrument]
                    samples = _build_runtime_samples(
                        config, quotes, trades, spec, start=start, end=end)
                    shard_samples += len(samples)
                    population.add(instrument, samples)
            shard_reports.append({
                "shard": shard_number,
                "instrument_count": len(instruments),
                "sample_count": shard_samples,
                "instrument_fingerprint": hashlib.sha256(
                    "|".join(instruments).encode()).hexdigest()[:16],
            })
        raw_replay_verification = None
        if event_source == EXTERNAL_EVENT_SOURCE:
            observed_manifest = _external_replay_manifest(
                raw_replay_rows, selected)
            observed_raw_replay_fingerprint = observed_manifest["fingerprint"]
            raw_replay_verification = {
                "contract": EXTERNAL_RAW_REPLAY_CONTENT_VERSION,
                "expected_fingerprint": expected_raw_replay_fingerprint,
                "observed_fingerprint": observed_raw_replay_fingerprint,
                "expected_manifest_rows": expected_raw_replay_rows,
                "observed_manifest_rows": observed_manifest["manifest_rows"],
                "status": "PASS" if (
                    observed_raw_replay_fingerprint ==
                    expected_raw_replay_fingerprint
                    and observed_manifest["manifest_rows"] ==
                    expected_raw_replay_rows)
                else "FAIL",
            }
            if raw_replay_verification["status"] != "PASS":
                raise RuntimeError(
                    "actual external raw replay differs from the frozen "
                    "per-session STOCK source manifest")

        report = _annotate_population(full_config, population.finish())
        report["multiple_testing"] = _population_multiple_testing(report)
        _apply_population_dsr_gate(report, report["multiple_testing"])
        report["source_quality"] = {
            "counts_by_status": quality_counts,
            "totals": quality_totals,
            "non_pass_examples": quality_examples,
            "raw_replay_content_verification": raw_replay_verification,
        }
        report["universe_shards"] = shard_reports
        report["score_calibration"] = calibration_reports.get("PRIMARY")
        report["calibration_population"] = calibration_reports
        report["calibration_shards"] = calibration_shard_reports
        report["summary"]["universe_shards"] = len(shards)
        report["slice"] = persisted["slice"]
        report["lane_manifest"].update(manifest(
            spec, source=event_source,
            timestamp_policy=config["timestamp_policy"]))
        report["discovery_rungs"] = discovery_rungs
        report["trial_lockbox"] = trial_lockbox
        report["evidence_tier"] = "SEARCH_EXPOSED_HISTORICAL_SUPPORT"
        report["reproduction_runtime"] = _qa_reproduction_runtime_manifest(
            hypothesis_id=hypothesis_id, config=persisted)
        report["forward_lockbox"] = {
            "status": "AWAITING_NEW_SESSIONS",
            "candidate_frozen": True,
            "candidate_lineage_id": primary_lineage.candidate_lineage_id,
            "root_lineage_id": primary_lineage.root_lineage_id,
            "frozen_identity": {
                "candidate": primary_lineage.candidate_identity_fingerprint,
                "ast": primary_lineage.candidate_ast_fingerprint,
                "semantic_plan": primary_lineage.semantic_plan_fingerprint,
                "features": primary_lineage.feature_spec_fingerprint,
                "labels": primary_lineage.label_spec_fingerprint,
                "model": primary_lineage.model_spec_fingerprint,
                "evaluator_version": primary_lineage.evaluator_version,
                "cost_model_version": primary_lineage.cost_model_version,
                "full_rung_plan": trial_schedule["full"][
                    "rung"].rung_plan_fingerprint,
            },
            "historical_sessions_search_exposed": True,
            "last_exposed_session": str(selected["sessions"][-1]),
            "minimum_new_sessions": config[
                "forward_confirmation_min_new_sessions"],
            "eligible_session_rule": (
                "append every eligible STOCK session in chronological order; "
                "session_date must be later than last_exposed_session, source "
                "watermark later than the frozen experiment watermark, and "
                "knowledge clock must be ARRIVAL_TIME_CAUSAL; no skipping"),
            "fixed_protocol": {
                "minimum_sessions": config[
                    "forward_confirmation_min_new_sessions"],
                "primary_effect": "COST_NET_SESSION_RETURN_BPS_VS_ZERO",
                "minimum_effect_bps": 0.0,
                "optional_stopping": False,
                "multiple_testing": "PREDECLARED_E_PROCESS_OR_FIXED_HORIZON",
                "legacy_61_sessions_eligible": False,
            },
            "forward_rung_allocated": False,
            "independent_confirmation": False,
            "promotion_authority": False,
        }
        if (event_source == EXTERNAL_EVENT_SOURCE
                and config["fast_screen_enabled"]):
            report["fast_discovery_screen"] = {
                **discovery_rungs[0],
                "primary_pass": True,
                "full_universe_confirmation_run": True,
            }
            if len(discovery_rungs) > 1:
                report["intermediate_discovery_screen"] = {
                    **discovery_rungs[1],
                    "primary_pass": True,
                    "full_universe_confirmation_run": True,
                }
        assert_source_snapshot_unchanged()
        _store_report(meta_conn, experiment_id, report)
    except Exception:
        # The scientific/report transaction may be aborted (for example an
        # index or JSON constraint failure).  Clear it before recording the
        # terminal experiment state; otherwise PostgreSQL rejects the FAILED
        # update too and leaves a permanently RUNNING row.
        meta_conn.rollback()
        with meta_conn.cursor() as cur:
            cur.execute("update quant.experiments set status='FAILED', ended_at=now() where experiment_id=%s",
                        (experiment_id,))
        meta_conn.commit()
        raise
    return {"experiment_id": experiment_id,
            "fragility": "INSUFFICIENT" if report.get("evidence_tier") ==
                         "SEARCH_EXPOSED_HISTORICAL_SUPPORT" else
                         "ROBUST" if report["decision"] == "SUBMIT_TO_QA" else
                         "INSUFFICIENT" if report["decision"] == "NO_EVIDENCE" else "FRAGILE",
            "backtest_metrics": {"turnover_total": report["summary"]["opportunities"],
                                 "total_return": report["summary"].get(
                                     "mean_net_bps_per_opportunity")},
            "intraday_report": report, "research_lane": "INTRADAY_EVENT"}
