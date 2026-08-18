"""Shared producer/consumer contract for adaptive-search exposure identities.

The quant runner seals the identity and the research breeder verifies it.  Both
must use this module so a locally plausible but incompatible digest cannot make
all measured F1/F2 memory disappear at the hand-off boundary.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime
import re
from typing import Any, Mapping


ADAPTIVE_SEARCH_EXPOSURE_VERSION = "intraday-adaptive-search-exposure-v1"
FINGERPRINT_CONTRACT = "canonical-json-sha256-v1"
IDENTIFIER_EXCLUSIONS = (
    "experiment_id", "experiment_rung_id", "rung_plan_fingerprint",
    "candidate_lineage_id", "root_lineage_id", "session_access_id",
    "session_exposure_id", "completion_timestamp",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOP_LEVEL_SECTIONS = (
    "dataset", "evaluation", "content_evidence", "source_contract",
    "lane_contract", "execution_contract", "cost_contract",
    "evaluator_contract", "cross_checks",
)
_CROSS_CHECKS = (
    "ledger_session_set_fingerprint_verified",
    "ledger_full_universe_fingerprint_verified",
    "screen_panel_fingerprint_verified",
    "screen_panel_manifest_verified",
    "per_session_content_evidence_verified",
)


def _stable_fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":")).encode("utf-8")).hexdigest()


def exposure_fingerprint(exposure: Mapping[str, Any]) -> str:
    """Hash the full stable identity, excluding only its self digest."""

    identity = {key: value for key, value in exposure.items()
                if key != "search_exposure_fingerprint"}
    return _stable_fingerprint(identity)


def has_exact_declaration(exposure: Mapping[str, Any]) -> bool:
    """Return whether a persisted identity declares the active hash contract."""

    return (
        exposure.get("version") == ADAPTIVE_SEARCH_EXPOSURE_VERSION
        and exposure.get("fingerprint_contract") == FINGERPRINT_CONTRACT
        and exposure.get("identifier_exclusions") == list(IDENTIFIER_EXCLUSIONS)
    )


def _aware_iso(value: Any) -> bool:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() is not None


def _nonnegative_integer(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def _finite_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def strict_validation_error(exposure: Mapping[str, Any]) -> str | None:
    """Validate the complete scientific identity, not merely its self-hash."""

    if not has_exact_declaration(exposure):
        return "DECLARATION_MISMATCH"
    if (exposure.get("evidence_purpose") != "ADAPTIVE_SEARCH"
            or exposure.get("adaptive_search_only") is not True
            or exposure.get("promotion_authority") is not False
            or exposure.get("rung") not in {"DISCOVERY_6", "VALIDATION_20"}):
        return "PURPOSE_OR_RUNG_MISMATCH"
    if any(not isinstance(exposure.get(name), Mapping)
           or not exposure.get(name) for name in _TOP_LEVEL_SECTIONS):
        return "MISSING_SCIENTIFIC_SECTION"

    dataset = exposure["dataset"]
    if (any(not str(dataset.get(name) or "").strip() for name in (
            "name", "version", "dataset_id", "asset_scope",
            "stock_universe_contract_version"))
            or not _aware_iso(dataset.get("dataset_cutoff"))
            or not _SHA256.fullmatch(str(
                dataset.get("reference_identity_fingerprint") or ""))):
        return "INVALID_DATASET_IDENTITY"

    evaluation = exposure["evaluation"]
    status = evaluation.get("status")
    scope = evaluation.get("measurement_scope")
    expected_scope = {
        "EVALUATED": "ADAPTIVE_RUNG_MEASURED",
        "SKIPPED_COST_INFEASIBLE": "CALIBRATION_ONLY_RESOURCE_STOP",
    }.get(status)
    planned = evaluation.get("planned_sessions")
    evaluated = evaluation.get("evaluated_sessions")
    panel_keys = evaluation.get("panel_replay_keys")
    panel_ids = evaluation.get("panel_reference_instrument_ids")
    panel_manifest = evaluation.get("panel_manifest")
    if (scope != expected_scope or not isinstance(planned, list) or not planned
            or len(planned) != len(set(planned))
            or evaluation.get("planned_session_count") != len(planned)
            or not isinstance(evaluated, list)
            or evaluation.get("evaluated_session_count") != len(evaluated)
            or (status == "EVALUATED" and evaluated != planned)
            or (status == "SKIPPED_COST_INFEASIBLE" and evaluated)
            or evaluation.get("session_set_fingerprint") !=
            _stable_fingerprint(planned)
            or not isinstance(panel_keys, list) or not panel_keys
            or len(panel_keys) != len(set(panel_keys))
            or not isinstance(panel_ids, list) or len(panel_ids) != len(panel_keys)
            or len(panel_ids) != len(set(panel_ids))
            or evaluation.get("panel_instrument_count") != len(panel_keys)
            or evaluation.get("panel_order_fingerprint") !=
            _stable_fingerprint(panel_keys)
            or evaluation.get("panel_reference_set_fingerprint") !=
            _stable_fingerprint(sorted(panel_ids))
            or not _nonnegative_integer(
                evaluation.get("full_universe_instrument_count"))
            or evaluation.get("full_universe_instrument_count") < len(panel_keys)
            or not _SHA256.fullmatch(str(evaluation.get(
                "full_universe_reference_set_fingerprint") or ""))
            or not isinstance(panel_manifest, Mapping)
            or panel_manifest.get("promotion_authority") is not False):
        return "INVALID_EVALUATION_IDENTITY"

    content = exposure["content_evidence"]
    content_rows = content.get("per_session")
    if (content.get("scope") != "FULL_FROZEN_STOCK_UNIVERSE_PER_SESSION"
            or not isinstance(content_rows, list)
            or len(content_rows) != len(planned)):
        return "INVALID_CONTENT_EVIDENCE"
    for session, row in zip(planned, content_rows):
        if (not isinstance(row, Mapping) or row.get("session") != session
                or not _SHA256.fullmatch(str(
                    row.get("session_content_fingerprint") or ""))
                or not _nonnegative_integer(row.get("quote_rows"))
                or not _nonnegative_integer(row.get("trade_rows"))
                or not isinstance(row.get("source_watermark"), Mapping)
                or not row.get("source_watermark")):
            return "INVALID_CONTENT_EVIDENCE"

    source = exposure["source_contract"]
    lineage = source.get("source_lineage")
    if (not str(source.get("event_source") or "").strip()
            or not isinstance(lineage, list) or not lineage
            or source.get("source_lineage_fingerprint") !=
            _stable_fingerprint(lineage)
            or source.get("knowledge_clock_mode") !=
            "EVENT_TIME_HISTORICAL_ONLY"
            or not str(source.get("timestamp_policy") or "").strip()):
        return "INVALID_SOURCE_CONTRACT"

    execution = exposure["execution_contract"]
    if (not str(execution.get("population_execution_model") or "").strip()
            or not str(execution.get("position_mode") or "").strip()
            or not _nonnegative_integer(execution.get("order_latency_ms"))
            or not _finite_number(execution.get("max_quote_age_seconds"))
            or not _finite_number(execution.get("minimum_predicted_edge_bps"))):
        return "INVALID_EXECUTION_CONTRACT"
    cost = exposure["cost_contract"]
    if (not str(cost.get("cost_model_version") or "").strip()
            or not _finite_number(cost.get("fee_bps_per_side"))
            or not _finite_number(cost.get("maker_fee_bps_per_side"))):
        return "INVALID_COST_CONTRACT"
    evaluator = exposure["evaluator_contract"]
    contracts = evaluator.get("candidate_contracts")
    if (any(not str(evaluator.get(name) or "").strip() for name in (
            "runner_version", "evaluator_version", "fast_screen_version"))
            or not isinstance(contracts, list) or not contracts
            or evaluator.get("candidate_set_fingerprint") !=
            _stable_fingerprint(contracts)):
        return "INVALID_EVALUATOR_CONTRACT"
    if any(exposure["cross_checks"].get(name) is not True
           for name in _CROSS_CHECKS):
        return "UNVERIFIED_CROSS_CHECK"
    claimed = str(exposure.get("search_exposure_fingerprint") or "")
    if claimed and (not _SHA256.fullmatch(claimed)
                    or exposure_fingerprint(exposure) != claimed):
        return "FINGERPRINT_MISMATCH"
    return None


def assert_strict_exposure(exposure: Mapping[str, Any]) -> None:
    error = strict_validation_error(exposure)
    if error:
        raise ValueError(f"invalid adaptive search exposure: {error}")
