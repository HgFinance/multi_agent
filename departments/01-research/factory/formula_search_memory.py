"""Pure adapter from normalized historical rows to formula search memory.

This boundary does not infer fitness, fill missing values, run the fidelity
scheduler, or grant promotion authority.  A row enters the quality-diversity
archive only when the evaluator persisted a complete, versioned six-objective
vector.  Calibration-only cost/direction failures remain separate failure
memory and are intentionally not converted into synthetic OOS observations.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable, Mapping


_HERE = Path(__file__).resolve().parent
_CONTRACTS = _HERE.parent / "contracts"
for _path in (_HERE, _CONTRACTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from formula_search_archive import (  # noqa: E402
    EVIDENCE,
    F1,
    F2,
    ExposureLedger,
    FormulaEvaluation,
    FormulaSearchArchive,
    Niche,
    ObjectiveVector,
)
import intraday_ast_contract as grammar  # noqa: E402


MODULE_VERSION = "formula-search-memory-v3"
SEARCH_OBJECTIVES_VERSION = "intraday-search-objectives-v1"
HISTORY_SCOPES = {"F1": F1, "F2": F2}

CALIBRATION_COST_FAILURES = frozenset({
    "NO_COST_FEASIBLE_ENTRY",
    "CALIBRATION_COST_INFEASIBLE",
})
CALIBRATION_DIRECTION_FAILURES = frozenset({
    "NON_POSITIVE_DIRECTIONAL_RELATION",
    "CALIBRATION_DIRECTION_NON_POSITIVE",
})
CALIBRATION_FAILURES = (
    CALIBRATION_COST_FAILURES | CALIBRATION_DIRECTION_FAILURES)

_OBJECTIVE_FIELDS = (
    "cost_net_bps",
    "oos_sharpe",
    "coverage_ratio",
    "robustness_score",
    "novelty_score",
    "complexity_nodes",
)
_BASE_REQUIRED_FIELDS = (
    "expression",
    "candidate_identity_fingerprint",
    "candidate_ast_fingerprint",
    "semantic_plan_fingerprint",
    "root_lineage_id",
    "source_lead_ids",
    "evidence_scope",
    "explicit_survivor",
    "exposure_fingerprint",
    "economic_family_id",
    "evaluator_version",
    "cost_model_version",
    "measurement_scope",
    "horizon_seconds",
    "clock_domains",
    "sessions",
    "opportunities",
    "observed_at",
)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class HistoryRowError(ValueError):
    """Typed audit rejection; ``code`` is stable, message is diagnostic."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


@dataclass(frozen=True)
class _Identity:
    row_index: int
    expression: dict[str, Any]
    candidate_identity_fingerprint: str
    candidate_ast_fingerprint: str
    semantic_plan_fingerprint: str
    root_lineage_id: str
    source_lead_ids: tuple[str, ...]
    evidence_scope: str
    fidelity: str
    explicit_survivor: bool
    exposure_fingerprint: str
    economic_family_id: str
    evaluator_version: str
    cost_model_version: str
    measurement_scope: str
    horizon_seconds: int
    clock_domains: tuple[str, ...]
    sessions: int
    opportunities: int
    observed_at: str
    failure_codes: tuple[str, ...]


@dataclass(frozen=True)
class _Prepared:
    row_index: int
    observed_at: str
    evaluation: FormulaEvaluation


def _failure_codes(row: Mapping[str, Any]) -> tuple[str, ...]:
    values: list[Any] = []
    for name in ("failure_codes", "lesson_codes", "failed_criteria"):
        raw = row.get(name) or ()
        if isinstance(raw, str):
            raw = raw.replace("|", ",").split(",")
        if isinstance(raw, (list, tuple, set, frozenset)):
            values.extend(raw)
    calibration_status = row.get("calibration_status")
    if calibration_status not in (None, ""):
        values.append(calibration_status)
    return tuple(sorted({
        str(value).strip().upper() for value in values if str(value).strip()
    }))


def _exact_integer(value: Any, name: str, *, positive: bool) -> int:
    if isinstance(value, bool):
        raise HistoryRowError("INVALID_INTEGER", f"{name} must be an integer")
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise HistoryRowError(
            "INVALID_INTEGER", f"{name} must be an integer") from exc
    if result != value or result < (1 if positive else 0):
        qualifier = "positive" if positive else "non-negative"
        raise HistoryRowError(
            "INVALID_INTEGER", f"{name} must be a {qualifier} integer")
    return result


def _timestamp(value: Any) -> str:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError as exc:
            raise HistoryRowError(
                "INVALID_OBSERVED_AT", "observed_at must be ISO-8601") from exc
    else:
        raise HistoryRowError(
            "INVALID_OBSERVED_AT", "observed_at must be ISO-8601")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoryRowError(
            "INVALID_OBSERVED_AT", "observed_at must include an offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _clock_domains(value: Any) -> tuple[str, ...]:
    raw = [value] if isinstance(value, str) else value
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise HistoryRowError(
            "INVALID_CLOCK_DOMAINS", "clock_domains must be a non-empty list")
    domains = tuple(sorted({
        str(item).strip().upper() for item in raw if str(item).strip()
    }))
    if not domains:
        raise HistoryRowError(
            "INVALID_CLOCK_DOMAINS", "clock_domains cannot be empty")
    return domains


def _sha256(value: Any, name: str) -> str:
    result = str(value or "").strip()
    if not _SHA256.fullmatch(result):
        raise HistoryRowError(
            "INVALID_DURABLE_CANDIDATE_IDENTITY",
            f"{name} must be lowercase SHA-256",
        )
    return result


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def _json_copy(value: Any) -> Any:
    return json.loads(json.dumps(
        value, allow_nan=False, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"),
    ))


def _source_lead_ids(value: Any) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise HistoryRowError(
            "INVALID_SOURCE_LEAD_IDS",
            "source_lead_ids must be a non-empty JSON array",
        )
    if any(not isinstance(item, str) for item in value):
        raise HistoryRowError(
            "INVALID_SOURCE_LEAD_IDS",
            "source_lead_ids must contain only string identifiers",
        )
    values = tuple(item.strip() for item in value)
    if not values or any(not item for item in values):
        raise HistoryRowError(
            "INVALID_SOURCE_LEAD_IDS",
            "source_lead_ids must contain only non-empty identifiers",
        )
    if len(values) != len(set(values)):
        raise HistoryRowError(
            "INVALID_SOURCE_LEAD_IDS",
            "source_lead_ids cannot contain duplicates",
        )
    return tuple(sorted(values))


def _identity(row: Mapping[str, Any], row_index: int) -> _Identity:
    missing = [name for name in _BASE_REQUIRED_FIELDS if name not in row]
    if missing:
        durable_missing = sorted(set(missing) & {
            "candidate_identity_fingerprint", "candidate_ast_fingerprint",
            "semantic_plan_fingerprint", "root_lineage_id", "source_lead_ids",
        })
        if durable_missing:
            raise HistoryRowError(
                "MISSING_DURABLE_CANDIDATE_IDENTITY",
                "history row is missing durable candidate identity: "
                + ", ".join(durable_missing),
            )
        raise HistoryRowError(
            "MISSING_REQUIRED_FIELD",
            "history row is missing: " + ", ".join(sorted(missing)),
        )
    expression = row.get("expression")
    if not isinstance(expression, dict):
        raise HistoryRowError("INVALID_EXPRESSION", "expression must be an AST object")
    try:
        expression = grammar.parse(expression)
    except (TypeError, ValueError, grammar.IntradayExprError) as exc:
        raise HistoryRowError("INVALID_EXPRESSION", str(exc)) from exc
    candidate_identity = _sha256(
        row.get("candidate_identity_fingerprint"),
        "candidate_identity_fingerprint",
    )
    candidate_ast = _sha256(
        row.get("candidate_ast_fingerprint"),
        "candidate_ast_fingerprint",
    )
    actual_candidate_ast = _fingerprint(expression)
    if candidate_ast != actual_candidate_ast:
        raise HistoryRowError(
            "CANDIDATE_AST_FINGERPRINT_MISMATCH",
            "candidate_ast_fingerprint does not match the executable AST",
        )
    semantic_plan = _sha256(
        row.get("semantic_plan_fingerprint"),
        "semantic_plan_fingerprint",
    )
    root_value = row.get("root_lineage_id")
    root_lineage_id = root_value.strip() if isinstance(root_value, str) else ""
    if not root_lineage_id:
        raise HistoryRowError(
            "INVALID_ROOT_LINEAGE_ID", "root_lineage_id is required")
    source_lead_ids = _source_lead_ids(row.get("source_lead_ids"))

    scope = str(row.get("evidence_scope") or "").strip().upper()
    if scope not in HISTORY_SCOPES:
        raise HistoryRowError(
            "UNSUPPORTED_EVIDENCE_SCOPE", "evidence_scope must be F1 or F2")
    survivor = row.get("explicit_survivor")
    if not isinstance(survivor, bool):
        raise HistoryRowError(
            "INVALID_SURVIVOR_FLAG", "explicit_survivor must be boolean")
    exposure = str(row.get("exposure_fingerprint") or "").strip()
    if not _SHA256.fullmatch(exposure):
        raise HistoryRowError(
            "INVALID_EXPOSURE_FINGERPRINT",
            "exposure_fingerprint must be lowercase SHA-256",
        )
    family = str(row.get("economic_family_id") or "").strip()
    if not family:
        raise HistoryRowError(
            "MISSING_ECONOMIC_FAMILY", "economic_family_id is required")
    evaluator = str(row.get("evaluator_version") or "").strip()
    cost_model = str(row.get("cost_model_version") or "").strip()
    if not evaluator or not cost_model:
        raise HistoryRowError(
            "MISSING_EVALUATION_CONTRACT",
            "evaluator_version and cost_model_version are required",
        )
    measurement_scope = str(row.get("measurement_scope") or "").strip()
    if measurement_scope not in {
            "ADAPTIVE_RUNG_MEASURED",
            "CALIBRATION_ONLY_RESOURCE_STOP"}:
        raise HistoryRowError(
            "INVALID_MEASUREMENT_SCOPE",
            "measurement_scope is not an adaptive rung contract",
        )
    horizon = _exact_integer(
        row.get("horizon_seconds"), "horizon_seconds", positive=True)
    domains = _clock_domains(row.get("clock_domains"))
    inferred = {str(value).strip().upper()
                for value in grammar.effective_clock_domains_of(expression)}
    if not inferred.issubset(domains):
        raise HistoryRowError(
            "CLOCK_DOMAIN_MISMATCH",
            "declared clock_domains omit AST clock domains: "
            + ", ".join(sorted(inferred - set(domains))),
        )
    sessions = _exact_integer(row.get("sessions"), "sessions", positive=False)
    opportunities = _exact_integer(
        row.get("opportunities"), "opportunities", positive=False)
    return _Identity(
        row_index=row_index,
        expression=expression,
        candidate_identity_fingerprint=candidate_identity,
        candidate_ast_fingerprint=candidate_ast,
        semantic_plan_fingerprint=semantic_plan,
        root_lineage_id=root_lineage_id,
        source_lead_ids=source_lead_ids,
        evidence_scope=scope,
        fidelity=HISTORY_SCOPES[scope],
        explicit_survivor=survivor,
        exposure_fingerprint=exposure,
        economic_family_id=family,
        evaluator_version=evaluator,
        cost_model_version=cost_model,
        measurement_scope=measurement_scope,
        horizon_seconds=horizon,
        clock_domains=domains,
        sessions=sessions,
        opportunities=opportunities,
        observed_at=_timestamp(row.get("observed_at")),
        failure_codes=_failure_codes(row),
    )


def _objectives(row: Mapping[str, Any], identity: _Identity) -> ObjectiveVector:
    raw = row.get("search_objectives")
    if not isinstance(raw, Mapping):
        raise HistoryRowError(
            "MISSING_SEARCH_OBJECTIVES",
            "search_objectives must be a versioned complete object",
        )
    if raw.get("version") != SEARCH_OBJECTIVES_VERSION:
        raise HistoryRowError(
            "UNSUPPORTED_SEARCH_OBJECTIVES_VERSION",
            f"search_objectives.version must be {SEARCH_OBJECTIVES_VERSION}",
        )
    if raw.get("complete") is not True:
        raise HistoryRowError(
            "INCOMPLETE_SEARCH_OBJECTIVES",
            "search_objectives.complete must be exactly true",
        )
    values = raw.get("values")
    if not isinstance(values, Mapping):
        raise HistoryRowError(
            "INCOMPLETE_SEARCH_OBJECTIVES",
            "search_objectives.values must contain six measured values",
        )
    missing = [name for name in _OBJECTIVE_FIELDS if name not in values]
    if missing:
        raise HistoryRowError(
            "INCOMPLETE_SEARCH_OBJECTIVES",
            "search objective fields are missing: " + ", ".join(sorted(missing)),
        )
    extra = sorted(set(values) - set(_OBJECTIVE_FIELDS))
    if extra:
        raise HistoryRowError(
            "INVALID_SEARCH_OBJECTIVES",
            "search_objectives.values has unknown fields: " + ", ".join(extra),
        )
    # Reject non-finite values before constructing ObjectiveVector.  In
    # particular, ``None`` and NaN never take a ``or 0`` path here.
    for name in _OBJECTIVE_FIELDS:
        value = values.get(name)
        if isinstance(value, bool):
            raise HistoryRowError(
                "INVALID_SEARCH_OBJECTIVES", f"{name} must be finite numeric")
        try:
            finite = float(value)
        except (TypeError, ValueError) as exc:
            raise HistoryRowError(
                "INVALID_SEARCH_OBJECTIVES",
                f"{name} must be finite numeric",
            ) from exc
        if not math.isfinite(finite):
            raise HistoryRowError(
                "NONFINITE_SEARCH_OBJECTIVE", f"{name} must be finite")
    try:
        result = ObjectiveVector(**{
            name: values.get(name) for name in _OBJECTIVE_FIELDS
        })
    except (TypeError, ValueError) as exc:
        raise HistoryRowError("INVALID_SEARCH_OBJECTIVES", str(exc)) from exc
    actual_complexity = grammar.count_nodes(identity.expression)
    if result.complexity_nodes != actual_complexity:
        raise HistoryRowError(
            "COMPLEXITY_MISMATCH",
            "complexity_nodes does not equal the executable AST node count",
        )
    if identity.sessions <= 0 or identity.opportunities <= 0:
        raise HistoryRowError(
            "NO_EXECUTABLE_OBSERVATIONS",
            "measured objectives require positive sessions and opportunities",
        )
    return result


def _prepare(
    row: Mapping[str, Any], row_index: int,
) -> tuple[_Prepared | None, str | None]:
    identity = _identity(row, row_index)
    if identity.measurement_scope == "CALIBRATION_ONLY_RESOURCE_STOP":
        return None, "CALIBRATION_RESOURCE_STOP"
    calibration_failures = set(identity.failure_codes) & CALIBRATION_FAILURES
    if calibration_failures:
        if calibration_failures & CALIBRATION_COST_FAILURES:
            return None, "CALIBRATION_COST_FAILURE"
        return None, "CALIBRATION_DIRECTION_FAILURE"
    objectives = _objectives(row, identity)
    niche = Niche.create(
        "::".join((
            identity.economic_family_id,
            f"EVALUATOR={identity.evaluator_version}",
            f"COST={identity.cost_model_version}",
        )),
        identity.horizon_seconds,
        identity.clock_domains,
    )
    evaluation = FormulaEvaluation(
        candidate_identity_fingerprint=(
            identity.candidate_identity_fingerprint),
        niche=niche,
        fidelity=identity.fidelity,
        outcome=EVIDENCE,
        objectives=objectives,
        candidate_payload={
            "expression": identity.expression,
            "candidate_identity_fingerprint": (
                identity.candidate_identity_fingerprint),
            "candidate_ast_fingerprint": identity.candidate_ast_fingerprint,
            "semantic_plan_fingerprint": identity.semantic_plan_fingerprint,
            "root_lineage_id": identity.root_lineage_id,
            "source_lead_ids": list(identity.source_lead_ids),
            "evidence_scope": identity.evidence_scope,
            "explicit_survivor": identity.explicit_survivor,
            "economic_family_id": identity.economic_family_id,
            "evaluator_version": identity.evaluator_version,
            "cost_model_version": identity.cost_model_version,
            "measurement_scope": identity.measurement_scope,
            "observed_at": identity.observed_at,
            "search_objectives_version": SEARCH_OBJECTIVES_VERSION,
            "adaptive_search_memory_only": True,
        },
        exposure_fingerprint=identity.exposure_fingerprint,
        sessions=identity.sessions,
        opportunities=identity.opportunities,
    )
    return _Prepared(row_index, identity.observed_at, evaluation), None


def build_formula_search_memory(
    history_rows: Iterable[Mapping[str, Any]],
    *,
    active_evaluator_version: str | None = None,
    active_cost_model_version: str | None = None,
) -> dict[str, Any]:
    """Build deterministic breeding memory from normalized historical rows.

    ``elite_candidates`` is the parent-selection view.  It contains a durable
    candidate identity iff that candidate is the final archive elite in its
    niche *and* its immutable history row says ``explicit_survivor=true``.
    ``elite_quality_scores`` is an identity-keyed compatibility projection.
    """
    archive = FormulaSearchArchive()
    ledger = ExposureLedger()
    counters: Counter[str] = Counter()
    rejection_counts: Counter[str] = Counter()
    rejected_rows: list[dict[str, Any]] = []
    prepared: list[_Prepared] = []

    rows = list(history_rows)
    if bool(active_evaluator_version) != bool(active_cost_model_version):
        raise ValueError(
            "active evaluator and cost model versions must be supplied together")
    counters["rows_seen"] = len(rows)
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            rejection_counts["ROW_NOT_OBJECT"] += 1
            rejected_rows.append({
                "row_index": index, "code": "ROW_NOT_OBJECT",
                "message": "history row must be an object",
            })
            continue
        if active_evaluator_version and (
                row.get("evaluator_version") != active_evaluator_version
                or row.get("cost_model_version") !=
                active_cost_model_version):
            counters["inactive_contract_skips"] += 1
            continue
        try:
            item, skip = _prepare(row, index)
        except HistoryRowError as exc:
            rejection_counts[exc.code] += 1
            rejected_rows.append({
                "row_index": index, "code": exc.code, "message": str(exc),
            })
            continue
        if skip is not None:
            counters["calibration_failure_skips"] += 1
            counters[f"{skip.lower()}_skips"] += 1
            continue
        assert item is not None
        prepared.append(item)
        counters[f"{item.evaluation.fidelity.lower()}_valid_rows"] += 1

    # Result identity is a final tie-breaker, making conflicting immutable rows
    # deterministic even when callers supply a different list order.
    prepared.sort(key=lambda item: (
        item.observed_at,
        item.evaluation.candidate_identity_fingerprint,
        item.evaluation.exposure_fingerprint,
        item.evaluation.result_identity,
    ))
    archive_actions: Counter[str] = Counter()
    for cycle, item in enumerate(prepared, 1):
        try:
            new_exposure = ledger.record(item.evaluation, cycle=cycle)
        except ValueError as exc:
            rejection_counts["CONFLICTING_EXPOSURE_RESULT"] += 1
            rejected_rows.append({
                "row_index": item.row_index,
                "code": "CONFLICTING_EXPOSURE_RESULT",
                "message": str(exc),
            })
            continue
        if not new_exposure:
            counters["duplicate_exposures"] += 1
        update = archive.observe(item.evaluation, cycle=cycle)
        archive_actions[update.action] += 1
        if update.action == "DUPLICATE_RESULT":
            counters["duplicate_results"] += 1
        else:
            counters["unique_results_accepted"] += 1

    elite_candidates: dict[str, dict[str, Any]] = {}
    non_survivor_elites = 0
    for entry in archive.entries:
        payload = entry.evaluation.candidate_payload
        if payload.get("explicit_survivor") is True:
            identity = entry.evaluation.candidate_identity_fingerprint
            elite_candidates[identity] = {
                "candidate_identity_fingerprint": identity,
                "quality_score": entry.quality_score,
                "ast_fingerprint": payload["candidate_ast_fingerprint"],
                "semantic_plan_fingerprint": payload[
                    "semantic_plan_fingerprint"],
                "root_lineage_id": payload["root_lineage_id"],
                "source_lead_ids": list(payload["source_lead_ids"]),
                "explicit_survivor": True,
                "expression": _json_copy(payload["expression"]),
                "economic_family_id": payload["economic_family_id"],
                "evaluator_version": payload["evaluator_version"],
                "cost_model_version": payload["cost_model_version"],
                "evidence_scope": payload["evidence_scope"],
                "measurement_scope": payload["measurement_scope"],
                "observed_at": payload["observed_at"],
                "exposure_fingerprint": (
                    entry.evaluation.exposure_fingerprint),
            }
        else:
            non_survivor_elites += 1
    elite_quality_scores = {
        identity: candidate["quality_score"]
        for identity, candidate in elite_candidates.items()
    }

    audit = {
        "rows_seen": counters["rows_seen"],
        "valid_rows": len(prepared),
        "unique_results_accepted": counters["unique_results_accepted"],
        "calibration_failure_skips": counters["calibration_failure_skips"],
        "calibration_resource_stop_skips": counters[
            "calibration_resource_stop_skips"],
        "inactive_contract_skips": counters["inactive_contract_skips"],
        "active_evaluator_version": active_evaluator_version,
        "active_cost_model_version": active_cost_model_version,
        "calibration_cost_failure_skips": counters[
            "calibration_cost_failure_skips"],
        "calibration_direction_failure_skips": counters[
            "calibration_direction_failure_skips"],
        "duplicate_exposures": counters["duplicate_exposures"],
        "duplicate_results": counters["duplicate_results"],
        "conflicting_exposure_results": rejection_counts[
            "CONFLICTING_EXPOSURE_RESULT"],
        "invalid_rows": sum(rejection_counts.values()),
        "archive_entries": len(archive.entries),
        "survivor_elites": len(elite_candidates),
        "non_survivor_elites": non_survivor_elites,
        "effective_exposure_trials": ledger.effective_trial_count,
        "rejection_counts": dict(sorted(rejection_counts.items())),
        "archive_actions": dict(sorted(archive_actions.items())),
        "scheduler_used": False,
        "promotion_authority_used": False,
        "missing_values_filled_with_zero": False,
    }
    rejected_rows.sort(key=lambda item: (item["row_index"], item["code"]))
    result = {
        "module_version": MODULE_VERSION,
        "search_objectives_version": SEARCH_OBJECTIVES_VERSION,
        "elite_candidates": dict(sorted(elite_candidates.items())),
        "elite_quality_scores": dict(sorted(elite_quality_scores.items())),
        "audit": audit,
        "rejected_rows": rejected_rows,
        "state_snapshot": {
            "archive": archive.to_payload(),
            "exposure_ledger": ledger.to_payload(),
        },
    }
    # This is also an invariant check against accidental NaN persistence.
    json.dumps(result, allow_nan=False, sort_keys=True, separators=(",", ":"))
    return result


build = build_formula_search_memory


__all__ = [
    "CALIBRATION_COST_FAILURES",
    "CALIBRATION_DIRECTION_FAILURES",
    "HISTORY_SCOPES",
    "HistoryRowError",
    "MODULE_VERSION",
    "SEARCH_OBJECTIVES_VERSION",
    "build",
    "build_formula_search_memory",
]
