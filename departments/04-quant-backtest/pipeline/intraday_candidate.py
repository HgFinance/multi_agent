"""End-to-end evaluator for an intraday AST candidate.

This module is intentionally pure: the runtime adapter may load Timescale rows and
persist the returned report, while the scientific decision remains reproducible in
unit tests.  Dependence between overlapping five-second labels is not treated as
independent evidence; returns are first aggregated by KRX session and all inference
uses those session-level observations.
"""

from __future__ import annotations

from collections import defaultdict
import copy
from datetime import timezone
import hashlib
import heapq
import math
from statistics import fmean, median, pstdev
from zoneinfo import ZoneInfo

from intraday_alpha_ast import evaluate as evaluate_ast
from intraday_alpha_ast import (
    EXPLICIT_FEATURE_WINDOW_CONTRACT,
    LEGACY_FEATURE_WINDOW_CONTRACT,
    fields_of,
    fingerprint,
    shape_fingerprint,
    unit_of,
    validate_feature_window_contract,
)
from intraday_microstructure import (IntradayLaneSpec, IntradaySample,
                                     IntradaySampleBatch, audit_causality)
from intraday_supervised import (
    CostAwareTeacher,
    _decision_index_fingerprint,
    executable_target,
)
from overfit_stats import (bootstrap_ci, deflated_sharpe,
                           stationary_bootstrap_indices)


EVALUATOR_VERSION = "intraday-candidate-evaluator-v11"
EXPLICIT_WINDOW_EVALUATOR_VERSION = "intraday-candidate-evaluator-v12"
KST = ZoneInfo("Asia/Seoul")
ENTRY_POLICIES = frozenset({
    "POSITIVE_SCORE",
    "PREDICTED_MARKOUT_CLEARS_COST",
})
COEFFICIENT_POLICIES = frozenset({
    "FIXED_FROM_SOURCE", "PREREGISTERED_NO_OOS_FIT", "STRUCTURE_ONLY",
})
CALIBRATION_VERSION = "origin-anchored-positive-shrinkage-v1"
CALIBRATION_SHRINKAGE_FRACTION = 0.10
MIN_CALIBRATION_OBSERVATIONS = 1_000
MIN_CALIBRATION_NONZERO_SCORES = 100
MIN_CALIBRATION_INSTRUMENTS = 2

DEFAULT_CRITERIA = {
    # DSR/bootstrap implementation requires 60 independent observations.  Since
    # overlapping ticks are collapsed to sessions, the release gate must require
    # the same 60 sessions instead of pretending hundreds of ticks are independent.
    "min_sessions": 60,
    "min_instruments": 2,
    "min_instrument_coverage": 0.80,
    "min_opportunities": 100,
    "min_mean_net_bps_per_opportunity": 0.0,
    "min_positive_session_ratio": 0.60,
    "min_deflated_sharpe": 0.95,
    "min_passive_fill_rate": 0.10,
    "max_pbo": 0.50,
}


def apply_family_pbo(report: dict, family_pbo: float | None,
                     criteria: dict | None = None) -> dict:
    """Attach the family-level overfit result and recompute the review decision.

    PBO is only available after the current experiment has been written to the
    common ledger.  This second, deterministic pass avoids either inventing PBO
    before persistence or running the expensive event replay twice.
    """
    rules = {**DEFAULT_CRITERIA, **(criteria or {})}
    out = {**report, "summary": dict(report.get("summary") or {})}
    failed = [item for item in (report.get("failed_criteria") or [])
              if item not in {"PBO_UNMEASURED", "OVERFIT_PBO",
                              "INDEPENDENT_FORWARD_CONFIRMATION_PENDING"}]
    out["summary"]["pbo"] = family_pbo
    if family_pbo is None:
        failed.append("PBO_UNMEASURED")
    elif not math.isfinite(float(family_pbo)) or float(family_pbo) > rules["max_pbo"]:
        failed.append("OVERFIT_PBO")
    forward_lockbox = report.get("forward_lockbox") or {}
    search_exposed = (
        report.get("evidence_tier") == "SEARCH_EXPOSED_HISTORICAL_SUPPORT"
        or (bool(forward_lockbox) and
            forward_lockbox.get("independent_confirmation") is not True)
    )
    if search_exposed:
        if "INDEPENDENT_FORWARD_CONFIRMATION_PENDING" not in failed:
            failed.append("INDEPENDENT_FORWARD_CONFIRMATION_PENDING")
    out["failed_criteria"] = failed
    had_evidence = report.get("decision") != "NO_EVIDENCE"
    out["decision"] = ("NO_EVIDENCE" if not had_evidence else
                       "SUBMIT_TO_QA" if not failed else "HOLD")
    if search_exposed and had_evidence:
        out["forward_nomination"] = {
            "decision": "NOMINATE_FORWARD",
            "status": "AWAITING_INDEPENDENT_CONFIRMATION",
            "independent_confirmation": False,
            "promotion_authority": False,
        }
    elif not search_exposed:
        out.pop("forward_nomination", None)
    return out


def _label(sample: IntradaySample, horizon_seconds: int):
    return next((label for label in sample.labels
                 if label.horizon_seconds == horizon_seconds), None)


def _prepare_sample_sequence(samples):
    """Preserve cube alignment while retaining legacy list sorting behavior."""
    rows = list(samples)
    feature_cube = getattr(samples, "feature_cube", None)
    if feature_cube is not None:
        if any(rows[index].decision_time > rows[index + 1].decision_time
               for index in range(len(rows) - 1)):
            raise ValueError("feature-cube sample batches must be chronological")
        if feature_cube.row_count != len(rows):
            raise ValueError("feature cube does not align with sample rows")
        return rows, feature_cube
    return sorted(rows, key=lambda row: row.decision_time), None


def _safe_timestamp(value) -> float | None:
    try:
        timestamp = float(value.timestamp())
    except (AttributeError, TypeError, ValueError, OverflowError, OSError):
        return None
    return timestamp if math.isfinite(timestamp) else None


def _capital_window(sample: IntradaySample, label, *, execution: str,
                    side: int) -> tuple[float, float, str]:
    """Return a conservative decision-to-release capital reservation.

    Taker capital is released at its executable exit.  A passive order reserves
    capital while it rests and, when filled, throughout a full post-fill holding
    horizon.  Old cache rows without the optional passive-exit field remain
    readable because fill time plus the fixed horizon is sufficient to derive
    the same clock.
    """
    start = _safe_timestamp(sample.decision_time)
    if start is None:
        raise ValueError("decision_time must have a finite timestamp")
    horizon = float(label.horizon_seconds)
    execution = str(execution).upper()
    if execution == "TAKER":
        end = _safe_timestamp(label.exit_time)
        if end is None or end <= start:
            entry = _safe_timestamp(sample.entry_time)
            end = max(start + horizon, (entry or start) + horizon)
        return start, end, "TAKER_DECISION_TO_EXECUTABLE_EXIT"

    prefix = "long" if side > 0 else "short"
    fill = _safe_timestamp(getattr(label, f"{prefix}_passive_fill_time", None))
    passive_net = getattr(label, f"{prefix}_passive_net_bps", None)
    filled = (bool(getattr(label, f"{prefix}_passive_filled", False))
              or fill is not None or passive_net is not None)
    passive_exit = _safe_timestamp(
        getattr(label, f"{prefix}_passive_exit_time", None))
    expiry = _safe_timestamp(label.exit_time)
    if filled:
        derived_exit = fill + horizon if fill is not None else None
        usable = [value for value in (passive_exit, derived_exit)
                  if value is not None and value > start]
        if usable:
            # audit_causality rejects disagreement for current labels.  max is
            # fail-conservative for legacy/cache rows rather than understating
            # reserved capital when one optional timestamp is malformed.
            return start, max(usable), "PASSIVE_DECISION_TO_FILL_PLUS_HORIZON"
        # A legacy row claiming a fill without its fill timestamp cannot support
        # an exact clock.  Reserve the maximum possible rest+hold span.
        return start, max(expiry or start, start + 2.0 * horizon), \
            "PASSIVE_FILLED_TIMESTAMP_MISSING_CONSERVATIVE_MAX"

    if expiry is None or expiry <= start:
        entry = _safe_timestamp(sample.entry_time)
        expiry = max(start + horizon, (entry or start) + horizon)
    return start, expiry, "PASSIVE_DECISION_TO_ORDER_EXPIRY"


def _predicted_entry_hurdle_bps(
        sample: IntradaySample, *, execution: str,
        fee_bps_per_side: float, maker_fee_bps_per_side: float,
        minimum_predicted_edge_bps: float) -> float:
    """Return the preregistered executable hurdle for one long entry."""

    hurdle = float(minimum_predicted_edge_bps)
    if str(execution).upper() == "TAKER":
        execution_spread = (
            sample.execution_spread_bps
            if sample.execution_spread_bps is not None else
            (float(sample.entry_ask) - float(sample.entry_bid)) /
            ((float(sample.entry_ask) + float(sample.entry_bid)) / 2.0) *
            10_000.0)
        return hurdle + float(execution_spread) + 2.0 * float(
            fee_bps_per_side)
    return hurdle + float(maker_fee_bps_per_side) + float(
        fee_bps_per_side)


def _observations(samples: list[IntradaySample], values: list[float | None], *,
                  horizon_seconds: int, threshold: float,
                  execution: str, position_mode: str,
                  entry_policy: str = "POSITIVE_SCORE",
                  fee_bps_per_side: float = 0.0,
                  maker_fee_bps_per_side: float = 0.0,
                  minimum_predicted_edge_bps: float = 0.0) -> list[dict]:
    execution = str(execution).upper()
    if execution not in {"TAKER", "PASSIVE_FIFO_LOWER_BOUND"}:
        raise ValueError(f"unsupported execution: {execution}")
    position_mode = str(position_mode).upper()
    if position_mode != "LONG_ONLY":
        raise ValueError("factory intraday evaluator currently supports LONG_ONLY only")
    entry_policy = str(entry_policy).upper()
    if entry_policy not in ENTRY_POLICIES:
        raise ValueError(f"unsupported entry_policy: {entry_policy}")
    rows = []
    for sample, raw in zip(samples, values):
        if raw is None or not math.isfinite(float(raw)):
            continue
        value = float(raw)
        hurdle = float(threshold)
        if entry_policy == "PREDICTED_MARKOUT_CLEARS_COST":
            # The AST predicts future mid-markout in BPS.  A trade is admissible
            # only if that predicted move clears the executable round trip and
            # the preregistered safety margin.  This prevents a directionally
            # useful pressure score from becoming a high-turnover loss machine.
            hurdle = _predicted_entry_hurdle_bps(
                sample, execution=execution,
                fee_bps_per_side=fee_bps_per_side,
                maker_fee_bps_per_side=maker_fee_bps_per_side,
                minimum_predicted_edge_bps=minimum_predicted_edge_bps)
        # Point-in-time borrow availability/fees are not in the governed source
        # plane.  Negative scores therefore mean abstain, never a free short.
        side = 1 if value > hurdle else 0
        label = _label(sample, horizon_seconds)
        if side == 0 or label is None:
            continue
        if side > 0:
            mid = label.long_mid_markout_bps
            net = (label.long_taker_net_bps if execution == "TAKER"
                   else label.long_passive_net_bps)
        else:
            mid = label.short_mid_markout_bps
            net = (label.short_taker_net_bps if execution == "TAKER"
                   else label.short_passive_net_bps)
        capital_start, capital_end, capital_clock = _capital_window(
            sample, label, execution=execution, side=side)
        rows.append({
            "instrument_id": sample.instrument_id,
            "decision_time": sample.decision_time,
            "session": sample.decision_time.astimezone(KST).date().isoformat(),
            "side": side,
            "score": value,
            "entry_hurdle_bps": hurdle,
            "mid_markout_bps": float(mid),
            "filled": net is not None,
            # No fill means no position and zero P&L per opportunity.  Reporting
            # only per-fill returns rewards strategies that almost never execute.
            "net_bps_per_opportunity": 0.0 if net is None else float(net),
            "net_bps_per_fill": None if net is None else float(net),
            "capacity_shares_l1": (
                (float(sample.entry_ask_depth_l1) if side > 0
                 else float(sample.entry_bid_depth_l1))
                if sample.execution_capacity_supported else None),
            "capital_start_timestamp": capital_start,
            "capital_end_timestamp": capital_end,
            "capital_clock": capital_clock,
        })
    return rows


def _time_context_allows(sample: IntradaySample, contexts) -> bool:
    selected = {str(value).upper() for value in (contexts or ())}
    local = sample.decision_time.astimezone(KST).time()
    if "OPEN" in selected and not (local.hour == 9 and local.minute < 30):
        return False
    if "MIDDAY" in selected and not ((local.hour, local.minute) >= (11, 30) and
                                      (local.hour, local.minute) < (13, 30)):
        return False
    if "CLOSE" in selected and not ((local.hour, local.minute) >= (14, 50) and
                                     (local.hour, local.minute) < (15, 20)):
        return False
    return True


def _time_behavior_bucket(sample: IntradaySample) -> str:
    """Stable KRX time cluster used as a residual behavior descriptor."""
    local = sample.decision_time.astimezone(KST).time()
    if local.hour == 9 and local.minute < 30:
        return "OPEN"
    if (local.hour, local.minute) >= (11, 30) and \
            (local.hour, local.minute) < (13, 30):
        return "MIDDAY"
    if (local.hour, local.minute) >= (14, 50) and \
            (local.hour, local.minute) < (15, 20):
        return "CLOSE"
    return "CONTINUOUS"


def _residual_cell(stats: list[float]) -> dict:
    (count, signed_sum, absolute_sum, squared_sum,
     null_absolute_sum, null_squared_sum) = stats
    mae = absolute_sum / count
    null_mae = null_absolute_sum / count
    rmse = math.sqrt(squared_sum / count)
    null_rmse = math.sqrt(null_squared_sum / count)
    return {
        "observations": int(count),
        "mean_error_bps": signed_sum / count,
        "mean_absolute_error_bps": mae,
        "rmse_bps": rmse,
        "null_mean_absolute_error_bps": null_mae,
        "null_rmse_bps": null_rmse,
        "mae_improvement_vs_null_bps": null_mae - mae,
        "rmse_improvement_vs_null_bps": null_rmse - rmse,
    }


def _bootstrap_mean(values: list[float], *, n_boot: int = 1000,
                    seed: int = 20260816) -> tuple[float | None, float | None]:
    if len(values) < 2:
        return None, None
    state = seed
    means = []
    for _ in range(n_boot):
        sample = []
        for _index in values:
            state = (1103515245 * state + 12345) % (1 << 31)
            sample.append(values[state % len(values)])
        means.append(fmean(sample))
    means.sort()
    return means[int(0.025 * len(means))], means[int(0.975 * len(means))]


def _stationary_mean(values: list[float], *, n_boot: int = 1000,
                     seed: int = 20260816,
                     restart_probability: float = 0.25) -> dict:
    """Dependence-aware percentile interval for a paired session mean."""
    if len(values) < 2:
        return {
            "ci_low_bps": None,
            "ci_high_bps": None,
            "bootstrap_method": "stationary",
            "restart_probability": restart_probability,
            "reason": "need at least two common scheduled sessions",
        }
    means = []
    for indices in stationary_bootstrap_indices(
            len(values), n_boot=n_boot,
            restart_probability=restart_probability, seed=seed):
        means.append(fmean(values[index] for index in indices))
    means.sort()
    return {
        "ci_low_bps": means[int(0.025 * len(means))],
        "ci_high_bps": means[min(int(0.975 * len(means)), len(means) - 1)],
        "bootstrap_method": "stationary",
        "restart_probability": restart_probability,
        "expected_block_length_sessions": 1.0 / restart_probability,
        "n_boot": len(means),
        "seed": seed,
    }


def _capital_peaks(
        deltas_by_session: dict[str, dict[float, int]]) -> dict[str, int]:
    peaks: dict[str, int] = {}
    for session, deltas in deltas_by_session.items():
        current = peak = 0
        for when in sorted(deltas):
            current += deltas[when]
            peak = max(peak, current)
        peaks[session] = peak
    return peaks


def _folds(session_returns: dict[str, float], n_splits: int = 4) -> list[dict]:
    ordered = sorted(session_returns)
    if not ordered:
        return []
    size = max(1, math.ceil(len(ordered) / max(1, n_splits)))
    out = []
    for number, lo in enumerate(range(0, len(ordered), size), 1):
        dates = ordered[lo:lo + size]
        values = [session_returns[date] for date in dates]
        out.append({
            "fold": number,
            "start_session": dates[0],
            "end_session": dates[-1],
            "sessions": len(dates),
            "mean_net_bps": fmean(values),
            "positive": fmean(values) > 0,
        })
    return out


class _ReplayStats:
    """Small independent strategy ledger used by diagnostic controls."""

    def __init__(self, horizon_seconds: int):
        self.horizon_seconds = int(horizon_seconds)
        self.opportunities = 0
        self.fills = 0
        self.net_sum = 0.0
        self.fill_net_sum = 0.0
        self.session_net_sum: dict[str, float] = defaultdict(float)
        self.session_capital_deltas: dict[str, dict[float, int]] = defaultdict(
            lambda: defaultdict(int))

    def schedule(self, sessions) -> None:
        """Pre-register sessions so abstention/no-trade outcomes remain zero."""
        for raw in sessions:
            session = str(raw)
            self.session_net_sum.setdefault(session, 0.0)
            self.session_capital_deltas[session]

    def add(self, rows: list[dict]) -> None:
        for row in rows:
            self.opportunities += 1
            self.net_sum += float(row["net_bps_per_opportunity"])
            if row["net_bps_per_fill"] is not None:
                self.fills += 1
                self.fill_net_sum += float(row["net_bps_per_fill"])
            session = row["session"]
            self.session_net_sum[session] += float(
                row["net_bps_per_opportunity"])
            start = float(row["capital_start_timestamp"])
            end = float(row["capital_end_timestamp"])
            self.session_capital_deltas[session][start] += 1
            self.session_capital_deltas[session][end] -= 1

    def finish(self) -> dict:
        peaks = _capital_peaks(self.session_capital_deltas)
        returns = {
            session: self.session_net_sum[session] / max(1, peak)
            for session, peak in sorted(peaks.items())
        }
        values = list(returns.values())
        ci = _bootstrap_mean(values)
        return {
            "session_returns_bps": returns,
            "summary": {
                "sessions": len(values),
                "opportunities": self.opportunities,
                "fills": self.fills,
                "fill_rate": (self.fills / self.opportunities
                              if self.opportunities else None),
                "mean_net_bps_per_opportunity": (
                    self.net_sum / self.opportunities
                    if self.opportunities else None),
                "mean_net_bps_per_fill": (
                    self.fill_net_sum / self.fills if self.fills else None),
                "session_mean_net_bps": fmean(values) if values else None,
                "session_net_ci_low_bps": ci[0],
                "session_net_ci_high_bps": ci[1],
                "max_concurrent_opportunities": max(peaks.values(), default=0),
            },
        }


def _paired_increment(left: dict[str, float], right: dict[str, float], *,
                      sessions=None,
                      common_denominators: dict[str, float] | None = None,
                      valid: bool = True,
                      invalid_reason: str | None = None) -> dict:
    sessions = sorted(sessions if sessions is not None
                      else set(left) | set(right))
    if valid and not sessions:
        valid = False
        invalid_reason = "no common scheduled sessions"
    denominators = common_denominators or {session: 1.0 for session in sessions}
    basis = {
        session: max(1.0, float(denominators.get(session, 1.0)))
        for session in sessions
    }
    common_basis_fingerprint = hashlib.sha256(
        repr(sorted(basis.items())).encode()).hexdigest()
    shared = {
        "sessions": len(sessions),
        "scheduled_session_ids": sessions,
        "common_denominator": "MAX_CONCURRENT_RESERVED_CAPITAL_ACROSS_AST_TEACHER_HYBRID",
        "common_denominator_fingerprint": common_basis_fingerprint,
        "same_replay_paired": True,
        "selection_adjusted": False,
        "independent_confirmation": False,
        "promotion_authority": False,
    }
    if not valid:
        return {
            **shared,
            "status": "INVALID",
            "reason": invalid_reason or "paired comparison is invalid",
            "mean_delta_bps": None,
            "ci_low_bps": None,
            "ci_high_bps": None,
            "positive_session_ratio": None,
            "bootstrap_method": "stationary",
        }
    deltas = [
        (float(left.get(session, 0.0)) - float(right.get(session, 0.0))) /
        basis[session]
        for session in sessions
    ]
    ci = _stationary_mean(deltas)
    return {
        **shared,
        "status": "PASS",
        "mean_delta_bps": fmean(deltas) if deltas else None,
        "ci_low_bps": ci["ci_low_bps"],
        "ci_high_bps": ci["ci_high_bps"],
        "positive_session_ratio": (
            sum(value > 0.0 for value in deltas) / len(deltas)
            if deltas else None),
        "bootstrap_method": ci["bootstrap_method"],
        "restart_probability": ci["restart_probability"],
        "expected_block_length_sessions": ci.get(
            "expected_block_length_sessions"),
        "n_boot": ci.get("n_boot"),
        "seed": ci.get("seed"),
    }


class _CapacityReservoir:
    """Order-independent bottom-k sample for a diagnostic capacity quantile."""

    def __init__(self, limit: int = 10_000):
        self.limit = limit
        self.seen = 0
        self._heap: list[tuple[int, float]] = []

    @property
    def values(self) -> list[float]:
        return [row[1] for row in self._heap]

    def add(self, value: float, key: str) -> None:
        self.seen += 1
        rank = int.from_bytes(
            hashlib.sha256(key.encode("utf-8")).digest()[:8], "big")
        item = (-rank, float(value))
        if len(self._heap) < self.limit:
            heapq.heappush(self._heap, item)
            return
        if rank < -self._heap[0][0]:
            heapq.heapreplace(self._heap, item)

    def quantile(self, probability: float) -> float | None:
        values = self.values
        if not values:
            return None
        ordered = sorted(values)
        index = max(0, min(len(ordered) - 1,
                           int(probability * len(ordered)) - 1))
        return ordered[index]


class CandidateAccumulator:
    """Memory-bounded sufficient statistics for a universe-wide candidate.

    One worker may feed this accumulator shard-by-shard and session-by-session.
    Shards are an execution detail: the final object is still one preregistered
    expression, one experiment, and one trial.  Capital is normalized by an exact
    portfolio-wide event sweep.  Only timestamp deltas and sufficient statistics
    survive each shard; raw samples and observations do not.
    """

    def __init__(self, *, expr: dict, spec: IntradayLaneSpec,
                 horizon_seconds: int, execution: str,
                 position_mode: str = "LONG_ONLY", threshold: float = 0.0,
                 entry_policy: str = "POSITIVE_SCORE",
                 coefficient_policy: str = "PREREGISTERED_NO_OOS_FIT",
                 minimum_predicted_edge_bps: float = 0.0,
                 trials: int = 1, family_pbo: float | None = None,
                 semantic_plan: dict | None = None,
                 criteria: dict | None = None,
                 feature_window_contract_version: str =
                 LEGACY_FEATURE_WINDOW_CONTRACT):
        if horizon_seconds not in spec.horizons_seconds:
            raise ValueError("horizon_seconds is absent from the lane specification")
        if threshold < 0 or not math.isfinite(float(threshold)):
            raise ValueError("threshold must be finite and non-negative")
        if (minimum_predicted_edge_bps < 0
                or not math.isfinite(float(minimum_predicted_edge_bps))):
            raise ValueError(
                "minimum_predicted_edge_bps must be finite and non-negative")
        entry_policy = str(entry_policy).upper()
        if entry_policy not in ENTRY_POLICIES:
            raise ValueError(f"unsupported entry_policy: {entry_policy}")
        coefficient_policy = str(coefficient_policy).upper()
        if coefficient_policy not in COEFFICIENT_POLICIES:
            raise ValueError(
                f"unsupported coefficient_policy: {coefficient_policy}")
        validate_feature_window_contract(
            expr, contract_version=feature_window_contract_version)
        self.expr = expr
        self.feature_window_contract_version = (
            feature_window_contract_version)
        self.evaluator_version = (
            EXPLICIT_WINDOW_EVALUATOR_VERSION
            if feature_window_contract_version ==
            EXPLICIT_FEATURE_WINDOW_CONTRACT else EVALUATOR_VERSION)
        self.spec = spec
        self.horizon_seconds = horizon_seconds
        self.execution = execution
        self.position_mode = position_mode
        self.threshold = threshold
        self.entry_policy = entry_policy
        self.coefficient_policy = coefficient_policy
        self.minimum_predicted_edge_bps = float(minimum_predicted_edge_bps)
        self.trials = trials
        self.family_pbo = family_pbo
        self.semantic_plan = semantic_plan or {}
        self.rules = {**DEFAULT_CRITERIA, **(criteria or {})}
        self.requested_instruments: set[str] = set()
        self.sampled_instruments: set[str] = set()
        self.opportunity_instruments: set[str] = set()
        self.causality: list[dict] = []
        self.session_net_sum: dict[str, float] = defaultdict(float)
        self.session_capital_deltas: dict[str, dict[float, int]] = defaultdict(
            lambda: defaultdict(int))
        self.opportunities = self.fills = 0
        self.mid_sum = self.net_sum = self.fill_net_sum = 0.0
        self.implementation_drag_sum = self.capacity_sum = 0.0
        self.capacity_observations = 0
        self.capacity = _CapacityReservoir()
        self.calibration_observations = 0
        self.calibration_nonzero_scores = 0
        self.calibration_instruments: set[str] = set()
        self.calibration_sessions: set[str] = set()
        self.calibration_sum_score_sq = 0.0
        self.calibration_sum_score_markout = 0.0
        self.calibration_max_positive_raw_score: float | None = None
        self.calibration_min_entry_hurdle_bps: float | None = None
        self.calibration_beta = (
            None if coefficient_policy == "STRUCTURE_ONLY" else 1.0)
        self.calibration_status = (
            "PENDING" if coefficient_policy == "STRUCTURE_ONLY"
            else "NOT_REQUIRED_FIXED_EQUATION")
        self._restored_calibration_report: dict | None = None
        self.residual_prediction_unit = (
            "BPS" if coefficient_policy == "STRUCTURE_ONLY"
            or unit_of(expr) == "BPS" else None)
        self.time_residual_stats: dict[str, list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.session_residual_stats: dict[str, list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.teacher = CostAwareTeacher(
            horizon_seconds=self.horizon_seconds, execution=self.execution,
            cost_inputs={
                "fee_bps_per_side": float(self.spec.fee_bps_per_side),
                "maker_fee_bps_per_side": float(
                    self.spec.maker_fee_bps_per_side),
                "passive_nonfill_net_bps_per_opportunity": 0.0,
            },
            feature_window_contract_version=
            self.feature_window_contract_version)
        self.teacher_stats = _ReplayStats(self.horizon_seconds)
        self.hybrid_stats = _ReplayStats(self.horizon_seconds)
        self.teacher_prediction_count = 0
        self.teacher_markout_squared_error = 0.0
        self.teacher_net_squared_error = 0.0
        self.teacher_brier_sum = 0.0

    def restore_frozen_calibration(
            self, score_calibration: dict,
            supervised_control: dict | None = None) -> dict:
        """Admit preregistered calibration state for a future-only replay.

        This is intentionally not a convenience fit path.  It accepts only a
        terminal artifact from the historical experiment and may be called only
        on a fresh accumulator before a forward sample is consumed.
        """
        if (self.requested_instruments or self.sampled_instruments
                or self.calibration_observations
                or self.calibration_sessions
                or self._restored_calibration_report is not None):
            raise ValueError("calibration restore requires a fresh accumulator")
        if not isinstance(score_calibration, dict):
            raise ValueError("score calibration report must be an object")
        if score_calibration.get("version") != CALIBRATION_VERSION:
            raise ValueError("score calibration version does not match runtime")
        if (str(score_calibration.get("coefficient_policy") or "").upper()
                != self.coefficient_policy):
            raise ValueError("score calibration policy does not match candidate")
        status = str(score_calibration.get("status") or "").upper()
        try:
            beta = float(score_calibration.get("beta_bps_per_score_unit"))
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("score calibration beta is invalid") from exc
        if not math.isfinite(beta) or beta < 0.0:
            raise ValueError("score calibration beta must be finite and non-negative")
        if self.coefficient_policy == "STRUCTURE_ONLY":
            if status != "PASS" or beta <= 0.0:
                raise ValueError(
                    "STRUCTURE_ONLY forward replay requires a usable frozen beta")
        elif status != "NOT_REQUIRED_FIXED_EQUATION" or beta != 1.0:
            raise ValueError("fixed equation calibration artifact is inconsistent")
        if score_calibration.get("oos_fit_forbidden") is not True:
            raise ValueError("frozen score calibration must forbid OOS fitting")
        self.calibration_beta = beta
        self.calibration_status = status
        self._restored_calibration_report = copy.deepcopy(score_calibration)
        if supervised_control is None:
            # A missing teacher is an explicit unusable diagnostic control; it
            # does not alter the symbolic candidate or gain promotion authority.
            self.teacher.freeze()
        else:
            self.teacher.restore(supervised_control)
        return {
            **self._calibration_report(),
            "supervised_control": self.teacher.report(),
            "restored_without_refit": True,
        }

    def schedule_sessions(self, session_dates) -> None:
        """Register every preregistered session, including a no-sample day."""
        for raw in session_dates:
            session = str(raw)
            self.session_net_sum.setdefault(session, 0.0)
            self.session_capital_deltas[session]
            self.teacher_stats.schedule({session})
            self.hybrid_stats.schedule({session})

    def calibrate(self, instrument_id: str,
                  samples: list[IntradaySample]) -> None:
        """Accumulate a pooled score->markout scale without retaining raw rows.

        Calibration is origin anchored so an AST state gate that emits zero
        remains an abstention after mapping.  Only a positive coefficient is
        admissible: a negative fitted relation falsifies the proposed direction
        instead of silently turning it into a different strategy.
        """
        instrument_id = str(instrument_id)
        ordered, feature_cube = _prepare_sample_sequence(samples)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match calibration sample")
        audit = audit_causality(ordered, self.spec)
        if audit["status"] == "FAIL":
            raise ValueError(
                "calibration sample causality audit failed: " +
                "; ".join(audit.get("findings") or []))
        if self.feature_window_contract_version == \
                EXPLICIT_FEATURE_WINDOW_CONTRACT:
            self.teacher.calibrate(
                instrument_id, ordered, feature_cube=feature_cube)
        else:
            self.teacher.calibrate(instrument_id, ordered)
        self._calibrate_score_prepared(
            instrument_id, ordered, feature_cube=feature_cube)

    def _calibrate_score_prepared(
            self, instrument_id: str,
            ordered: list[IntradaySample], *, feature_cube=None) -> None:
        """Accumulate only candidate-specific AST scale statistics.

        Population evaluation calls this after sorting, instrument validation,
        and causality auditing once and fitting the common supervised teacher
        once per exact training contract.  The score scale cannot be shared:
        it depends on this candidate's expression and semantic context.
        """
        if self.coefficient_policy != "STRUCTURE_ONLY":
            return
        if self.calibration_status != "PENDING":
            raise ValueError("score calibration is already frozen")
        values = evaluate_ast(
            ordered, self.expr, feature_cube=feature_cube,
            feature_window_contract=self.feature_window_contract_version)
        contributed = False
        for sample, raw in zip(ordered, values):
            if (self.semantic_plan and not _time_context_allows(
                    sample, self.semantic_plan.get("context") or ())):
                continue
            label = _label(sample, self.horizon_seconds)
            if label is None or raw is None:
                continue
            score = float(raw)
            markout = float(label.long_mid_markout_bps)
            if not math.isfinite(score) or not math.isfinite(markout):
                continue
            self.calibration_observations += 1
            self.calibration_sessions.add(
                sample.decision_time.astimezone(KST).date().isoformat())
            if score == 0.0:
                continue
            contributed = True
            self.calibration_nonzero_scores += 1
            self.calibration_sum_score_sq += score * score
            self.calibration_sum_score_markout += score * markout
            if (self.entry_policy == "PREDICTED_MARKOUT_CLEARS_COST"
                    and score > 0.0):
                self.calibration_max_positive_raw_score = max(
                    score, self.calibration_max_positive_raw_score
                    if self.calibration_max_positive_raw_score is not None
                    else score)
                hurdle = _predicted_entry_hurdle_bps(
                    sample, execution=self.execution,
                    fee_bps_per_side=self.spec.fee_bps_per_side,
                    maker_fee_bps_per_side=self.spec.maker_fee_bps_per_side,
                    minimum_predicted_edge_bps=
                    self.minimum_predicted_edge_bps)
                self.calibration_min_entry_hurdle_bps = min(
                    hurdle, self.calibration_min_entry_hurdle_bps
                    if self.calibration_min_entry_hurdle_bps is not None
                    else hurdle)
        if contributed:
            self.calibration_instruments.add(instrument_id)

    def freeze_calibration(self) -> dict:
        """Lock one scale coefficient before any evaluation sample is consumed."""
        teacher_report = self.teacher.freeze()
        teacher_sidecar = self.teacher.calibration_evidence()
        teacher_bundle = {
            "supervised_control": teacher_report,
            "supervised_control_calibration_evidence": teacher_sidecar,
        }
        if self.coefficient_policy != "STRUCTURE_ONLY":
            return {**self._calibration_report(), **teacher_bundle}
        if self.calibration_status != "PENDING":
            return {**self._calibration_report(), **teacher_bundle}
        sufficient = (
            self.calibration_observations >= MIN_CALIBRATION_OBSERVATIONS
            and self.calibration_nonzero_scores >= MIN_CALIBRATION_NONZERO_SCORES
            and len(self.calibration_instruments) >= MIN_CALIBRATION_INSTRUMENTS
            and self.calibration_sum_score_sq > 0.0
        )
        if not sufficient:
            self.calibration_beta = 0.0
            self.calibration_status = "INSUFFICIENT_CALIBRATION"
            return {**self._calibration_report(), **teacher_bundle}
        denominator = self.calibration_sum_score_sq * (
            1.0 + CALIBRATION_SHRINKAGE_FRACTION)
        raw_beta = self.calibration_sum_score_markout / denominator
        self.calibration_beta = max(0.0, raw_beta) if math.isfinite(raw_beta) else 0.0
        self.calibration_status = (
            "PASS" if self.calibration_beta > 0.0
            else "NON_POSITIVE_DIRECTIONAL_RELATION")
        if (self.calibration_status == "PASS"
                and self.entry_policy == "PREDICTED_MARKOUT_CLEARS_COST"):
            maximum_prediction = (
                float(self.calibration_beta) *
                float(self.calibration_max_positive_raw_score)
                if self.calibration_max_positive_raw_score is not None
                else None)
            if (maximum_prediction is None
                    or self.calibration_min_entry_hurdle_bps is None
                    or maximum_prediction <=
                    self.calibration_min_entry_hurdle_bps):
                self.calibration_status = "NO_COST_FEASIBLE_ENTRY"
        return {**self._calibration_report(), **teacher_bundle}

    def _calibration_report(self) -> dict:
        if self._restored_calibration_report is not None:
            return copy.deepcopy(self._restored_calibration_report)
        maximum_prediction = (
            float(self.calibration_beta) *
            float(self.calibration_max_positive_raw_score)
            if self.calibration_beta is not None
            and self.calibration_max_positive_raw_score is not None
            else None)
        cost_feasible = (
            maximum_prediction is not None
            and self.calibration_min_entry_hurdle_bps is not None
            and maximum_prediction > self.calibration_min_entry_hurdle_bps)
        return {
            "version": CALIBRATION_VERSION,
            "coefficient_policy": self.coefficient_policy,
            "status": self.calibration_status,
            "origin_anchored": True,
            "positive_coefficient_required": True,
            "shrinkage_fraction": CALIBRATION_SHRINKAGE_FRACTION,
            "beta_bps_per_score_unit": self.calibration_beta,
            "observations": self.calibration_observations,
            "nonzero_scores": self.calibration_nonzero_scores,
            "instruments": len(self.calibration_instruments),
            "sessions": len(self.calibration_sessions),
            "session_ids": sorted(self.calibration_sessions),
            "oos_fit_forbidden": True,
            "maximum_positive_raw_score":
                self.calibration_max_positive_raw_score,
            "maximum_calibrated_predicted_markout_bps": maximum_prediction,
            "minimum_observed_entry_hurdle_bps":
                self.calibration_min_entry_hurdle_bps,
            "cost_feasible_entry_possible": (
                cost_feasible
                if self.entry_policy == "PREDICTED_MARKOUT_CLEARS_COST"
                else None),
            "cost_feasibility_proof": (
                "MAX_CALIBRATED_PREDICTION_EXCEEDS_MINIMUM_HURDLE"
                if cost_feasible else
                "MAX_CALIBRATED_PREDICTION_NOT_ABOVE_MINIMUM_HURDLE"
                if self.entry_policy == "PREDICTED_MARKOUT_CLEARS_COST"
                else "NOT_APPLICABLE"),
        }

    def add(self, instrument_id: str, samples: list[IntradaySample]) -> None:
        """Consume one instrument/session slice and immediately release its rows."""
        instrument_id = str(instrument_id)
        ordered, feature_cube = _prepare_sample_sequence(samples)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match sample instrument")
        audit = audit_causality(ordered, self.spec)
        self._add_prepared(
            instrument_id, ordered, audit, feature_cube=feature_cube)

    def _add_prepared(
            self, instrument_id: str, ordered: list[IntradaySample], audit: dict,
            *, shared_teacher_predictions: list[dict | None] | None = None,
            shared_teacher_targets: list[tuple[float, float, float] | None]
            | None = None, feature_cube=None) -> None:
        """Consume an already sorted/audited slice shared by a population.

        This is deliberately private: callers that have not established the
        common lane specification must use :meth:`add`.  The population wrapper
        below performs the validation once, then fans the immutable samples out
        to independent sufficient-statistic accumulators.
        """
        if (self.coefficient_policy == "STRUCTURE_ONLY"
                and self.calibration_status == "PENDING"):
            raise ValueError(
                "STRUCTURE_ONLY score calibration must be frozen before OOS replay")
        self.requested_instruments.add(instrument_id)
        self.causality.append({"instrument_id": instrument_id, **audit})
        if ordered:
            self.sampled_instruments.add(instrument_id)
        values = evaluate_ast(
            ordered, self.expr, feature_cube=feature_cube,
            feature_window_contract=self.feature_window_contract_version)
        if self.coefficient_policy == "STRUCTURE_ONLY":
            beta = float(self.calibration_beta or 0.0)
            values = [None if value is None else float(value) * beta
                      for value in values]
        if self.semantic_plan:
            eligible_indices = [
                index for index, sample in enumerate(ordered)
                if _time_context_allows(
                    sample, self.semantic_plan.get("context") or ())
            ]
        else:
            eligible_indices = list(range(len(ordered)))
        eligible_samples = [ordered[index] for index in eligible_indices]
        eligible_values = [values[index] for index in eligible_indices]
        # Register every evaluation session with available samples before signal
        # selection.  A candidate that abstains for the whole day earns zero; it
        # may not silently drop that day and report only its active sessions.
        scheduled_sessions = {
            sample.decision_time.astimezone(KST).date().isoformat()
            for sample in ordered
        }
        for session in scheduled_sessions:
            self.session_net_sum.setdefault(session, 0.0)
            self.session_capital_deltas[session]
        self.teacher_stats.schedule(scheduled_sessions)
        self.hybrid_stats.schedule(scheduled_sessions)
        self._add_residuals(eligible_samples, eligible_values)
        observations = _observations(
            eligible_samples, eligible_values,
            horizon_seconds=self.horizon_seconds, threshold=self.threshold,
            execution=self.execution, position_mode=self.position_mode,
            entry_policy=self.entry_policy,
            fee_bps_per_side=self.spec.fee_bps_per_side,
            maker_fee_bps_per_side=self.spec.maker_fee_bps_per_side,
            minimum_predicted_edge_bps=self.minimum_predicted_edge_bps)
        # The frozen supervised model is a public-feature control, not another
        # promotion path.  It sees the exact same contextual slice and labels.
        # The hybrid requires both the AST decision and positive expected net;
        # its paired delta tells the next generation whether the symbolic term
        # adds anything beyond a generic frozen linear microstructure control.
        if shared_teacher_predictions is None:
            if self.feature_window_contract_version == \
                    EXPLICIT_FEATURE_WINDOW_CONTRACT:
                all_teacher_predictions = self.teacher.predict(
                    ordered, feature_cube=feature_cube)
                teacher_predictions = [
                    all_teacher_predictions[index]
                    for index in eligible_indices
                ]
            else:
                teacher_predictions = self.teacher.predict(eligible_samples)
        else:
            if len(shared_teacher_predictions) != len(ordered):
                raise ValueError(
                    "shared teacher predictions do not align with sample slice")
            teacher_predictions = [
                shared_teacher_predictions[index]
                for index in eligible_indices
            ]
        if (shared_teacher_targets is not None
                and len(shared_teacher_targets) != len(ordered)):
            raise ValueError(
                "shared teacher targets do not align with sample slice")
        teacher_values = [
            None if row is None else row["expected_net_bps"]
            for row in teacher_predictions]
        teacher_observations = _observations(
            eligible_samples, teacher_values,
            horizon_seconds=self.horizon_seconds,
            threshold=self.minimum_predicted_edge_bps,
            execution=self.execution, position_mode=self.position_mode,
            entry_policy="POSITIVE_SCORE",
            fee_bps_per_side=self.spec.fee_bps_per_side,
            maker_fee_bps_per_side=self.spec.maker_fee_bps_per_side,
            minimum_predicted_edge_bps=0.0)
        ast_entries = {row["decision_time"] for row in observations}
        hybrid_values = [
            value if sample.decision_time in ast_entries else None
            for sample, value in zip(eligible_samples, teacher_values)]
        hybrid_observations = _observations(
            eligible_samples, hybrid_values,
            horizon_seconds=self.horizon_seconds,
            threshold=self.minimum_predicted_edge_bps,
            execution=self.execution, position_mode=self.position_mode,
            entry_policy="POSITIVE_SCORE",
            fee_bps_per_side=self.spec.fee_bps_per_side,
            maker_fee_bps_per_side=self.spec.maker_fee_bps_per_side,
            minimum_predicted_edge_bps=0.0)
        self.teacher_stats.add(teacher_observations)
        self.hybrid_stats.add(hybrid_observations)
        for eligible_index, sample, prediction in zip(
                eligible_indices, eligible_samples, teacher_predictions):
            if prediction is None:
                continue
            target = (
                shared_teacher_targets[eligible_index]
                if shared_teacher_targets is not None
                else executable_target(
                    sample, horizon_seconds=self.horizon_seconds,
                    execution=self.execution)
            )
            if target is None:
                continue
            markout, net, positive = target
            self.teacher_prediction_count += 1
            self.teacher_markout_squared_error += (
                float(prediction["expected_markout_bps"]) - markout) ** 2
            self.teacher_net_squared_error += (
                float(prediction["expected_net_bps"]) - net) ** 2
            self.teacher_brier_sum += (
                float(prediction["positive_net_probability"]) - positive) ** 2
        if observations:
            self.opportunity_instruments.add(instrument_id)
        by_session: dict[str, list[dict]] = defaultdict(list)
        for row in observations:
            by_session[row["session"]].append(row)
            net = row["net_bps_per_opportunity"]
            mid = row["mid_markout_bps"]
            capacity = row["capacity_shares_l1"]
            self.opportunities += 1
            self.net_sum += net
            self.mid_sum += mid
            self.implementation_drag_sum += mid - net
            if capacity is not None:
                self.capacity_sum += capacity
                self.capacity_observations += 1
                self.capacity.add(
                    capacity,
                    f"{instrument_id}|{row['decision_time'].isoformat()}")
            if row["net_bps_per_fill"] is not None:
                self.fills += 1
                self.fill_net_sum += row["net_bps_per_fill"]
        for session, rows in by_session.items():
            self.session_net_sum[session] += sum(
                row["net_bps_per_opportunity"] for row in rows)
            deltas = self.session_capital_deltas[session]
            for row in rows:
                start = float(row["capital_start_timestamp"])
                end = float(row["capital_end_timestamp"])
                deltas[start] += 1
                deltas[end] -= 1

    def _add_residuals(self, samples: list[IntradaySample],
                       predictions: list[float | None]) -> None:
        """Retain bounded cluster statistics, never OOS rows or fitted choices."""
        if self.residual_prediction_unit != "BPS":
            return
        for sample, raw in zip(samples, predictions):
            label = _label(sample, self.horizon_seconds)
            if label is None or raw is None:
                continue
            prediction = float(raw)
            target = float(label.long_mid_markout_bps)
            if not math.isfinite(prediction) or not math.isfinite(target):
                continue
            residual = target - prediction
            session = sample.decision_time.astimezone(KST).date().isoformat()
            for stats in (self.time_residual_stats[_time_behavior_bucket(sample)],
                          self.session_residual_stats[session]):
                stats[0] += 1.0
                stats[1] += residual
                stats[2] += abs(residual)
                stats[3] += residual * residual
                stats[4] += abs(target)
                stats[5] += target * target

    def _residual_behavior(self) -> dict:
        status = "PASS"
        if self.residual_prediction_unit != "BPS":
            status = "NOT_APPLICABLE_NON_BPS"
        elif (self.coefficient_policy == "STRUCTURE_ONLY"
              and self.calibration_status != "PASS"):
            status = "UNUSABLE_CALIBRATION"
        elif not self.time_residual_stats:
            status = "NO_RESIDUAL_OBSERVATIONS"
        time_cells = {key: _residual_cell(value) for key, value in sorted(
            self.time_residual_stats.items()) if value[0]}
        session_cells = {key: _residual_cell(value) for key, value in sorted(
            self.session_residual_stats.items()) if value[0]}
        worst_time = (sorted(time_cells, key=lambda key: (
            -time_cells[key]["mean_absolute_error_bps"], key))[0]
                      if time_cells else None)
        worst_session = (sorted(session_cells, key=lambda key: (
            -session_cells[key]["mean_absolute_error_bps"], key))[0]
                         if session_cells else None)
        total = [sum(stats[index] for stats in self.time_residual_stats.values())
                 for index in range(6)]
        aggregate = _residual_cell(total) if total[0] else {}
        return {
            "version": "krx-domain-residual-qd-v1",
            "status": status,
            "target": "LONG_MIDPRICE_MARKOUT_BPS",
            "prediction_unit": self.residual_prediction_unit,
            "observations": int(total[0]),
            "mean_error_bps": aggregate.get("mean_error_bps"),
            "mean_absolute_error_bps": aggregate.get(
                "mean_absolute_error_bps"),
            "rmse_bps": aggregate.get("rmse_bps"),
            "null_mean_absolute_error_bps": aggregate.get(
                "null_mean_absolute_error_bps"),
            "mae_improvement_vs_null_bps": aggregate.get(
                "mae_improvement_vs_null_bps"),
            "worst_time_bucket": worst_time,
            "worst_time_bucket_mae_bps": (
                time_cells[worst_time]["mean_absolute_error_bps"]
                if worst_time else None),
            "median_time_bucket_mae_bps": (
                median(cell["mean_absolute_error_bps"]
                       for cell in time_cells.values()) if time_cells else None),
            "median_time_bucket_mae_improvement_vs_null_bps": (
                median(cell["mae_improvement_vs_null_bps"]
                       for cell in time_cells.values()) if time_cells else None),
            "worst_session": worst_session,
            "worst_session_mae_bps": (
                session_cells[worst_session]["mean_absolute_error_bps"]
                if worst_session else None),
            "time_buckets": time_cells,
            "session_cluster_count": len(session_cells),
            "selection_boundary": "OOS_DIAGNOSTIC_SCREENING_ONLY",
            "adaptive_search_memory_only": True,
            "independent_confirmation": False,
            "forward_new_sessions_required": True,
            "promotion_authority": False,
        }

    def finish(self) -> dict:
        residual_behavior = self._residual_behavior()
        session_peak_capital = _capital_peaks(self.session_capital_deltas)
        session_returns = {
            session: self.session_net_sum[session] / max(1, peak)
            for session, peak in sorted(session_peak_capital.items())
        }
        session_values = list(session_returns.values())
        folds = _folds(session_returns)
        positive_ratio = (sum(row["positive"] for row in folds) / len(folds)
                          if folds else None)
        # The promotion gate sees consecutive KRX sessions, whose returns may
        # remain serially dependent even after overlapping intraday labels have
        # been collapsed to one portfolio observation per day.  Reuse the same
        # stationary bootstrap contract as the paired teacher comparisons;
        # iid resampling here would make the primary alpha gate less
        # conservative than its diagnostic controls.
        mean_ci = _stationary_mean(session_values)
        dsr = deflated_sharpe(
            [value / 10_000.0 for value in session_values],
            trials=max(1, self.trials), periods=252)
        sharpe_ci = bootstrap_ci(
            [value / 10_000.0 for value in session_values], periods=252)
        requested = len(self.requested_instruments)
        sampled = len(self.sampled_instruments)
        coverage = sampled / requested if requested else 0.0
        summary = {
            "sessions": len(session_values),
            "instruments": len(self.opportunity_instruments),
            "instruments_requested": requested,
            "instruments_with_samples": sampled,
            "instrument_coverage": coverage,
            "opportunities": self.opportunities,
            "fills": self.fills,
            "fill_rate": (self.fills / self.opportunities
                          if self.opportunities else None),
            "mean_mid_markout_bps": (
                self.mid_sum / self.opportunities if self.opportunities else None),
            "mean_implementation_drag_bps": (
                self.implementation_drag_sum / self.opportunities
                if self.opportunities else None),
            "mean_net_bps_per_fill": (
                self.fill_net_sum / self.fills if self.fills else None),
            "mean_net_bps_per_opportunity": (
                self.net_sum / self.opportunities if self.opportunities else None),
            "session_mean_net_bps": fmean(session_values) if session_values else None,
            "session_net_ci_low_bps": mean_ci["ci_low_bps"],
            "session_net_ci_high_bps": mean_ci["ci_high_bps"],
            "session_net_ci_method": mean_ci["bootstrap_method"],
            "session_net_ci_restart_probability": mean_ci[
                "restart_probability"],
            "session_net_ci_expected_block_length_sessions": mean_ci.get(
                "expected_block_length_sessions"),
            "session_net_ci_n_boot": mean_ci.get("n_boot"),
            "session_net_ci_seed": mean_ci.get("seed"),
            "positive_fold_ratio": positive_ratio,
            "deflated_sharpe": dsr.get("deflated_sharpe"),
            "sharpe": dsr.get("sharpe"),
            "dsr_calibration_mode": dsr.get("calibration_mode"),
            "dsr_expected_max_sharpe": dsr.get("expected_max_sharpe"),
            "dsr_trial_sharpe_std": dsr.get("trial_sharpe_std"),
            "dsr_effective_trials": dsr.get("effective_trials"),
            # Canonical DSR evidence names are also kept without a prefix so a
            # release-gate consumer need not reverse-map implementation fields.
            "expected_max_sharpe": dsr.get("expected_max_sharpe"),
            "trial_sharpe_std": dsr.get("trial_sharpe_std"),
            "effective_trials": dsr.get("effective_trials"),
            "bootstrap_sharpe_ci_low": sharpe_ci.get("bootstrap_ci_low"),
            "bootstrap_sharpe_ci_high": sharpe_ci.get("bootstrap_ci_high"),
            "pbo": self.family_pbo,
            "trials": self.trials,
            "mean_capacity_shares_l1": (
                self.capacity_sum / self.capacity_observations
                if self.capacity_observations else None),
            "p10_capacity_shares_l1": self.capacity.quantile(0.10),
            "capacity_quantile_sample_size": len(self.capacity.values),
            "execution_capacity_supported": bool(
                self.capacity_observations),
            "max_concurrent_opportunities": max(
                session_peak_capital.values(), default=0),
            "residual_observations": residual_behavior["observations"],
            "mean_absolute_residual_bps": residual_behavior[
                "mean_absolute_error_bps"],
            "residual_rmse_bps": residual_behavior["rmse_bps"],
            "median_time_bucket_mae_bps": residual_behavior[
                "median_time_bucket_mae_bps"],
            "median_time_bucket_mae_improvement_vs_null_bps": (
                residual_behavior[
                    "median_time_bucket_mae_improvement_vs_null_bps"]),
        }
        failed = []
        if not self.opportunities:
            failed.append("NO_EXECUTABLE_OBSERVATIONS")
        if any(row["status"] == "FAIL" for row in self.causality):
            failed.append("CAUSALITY_NOT_PASS")
        for metric, rule in (("sessions", "min_sessions"),
                             ("instruments", "min_instruments"),
                             ("opportunities", "min_opportunities")):
            if summary[metric] < self.rules[rule]:
                failed.append(f"{metric.upper()}_BELOW_MINIMUM")
        if coverage < self.rules["min_instrument_coverage"]:
            failed.append("INSTRUMENT_COVERAGE_BELOW_MINIMUM")
        if (summary["mean_net_bps_per_opportunity"] is None or
                summary["mean_net_bps_per_opportunity"] <=
                self.rules["min_mean_net_bps_per_opportunity"]):
            failed.append("NET_EDGE_NOT_POSITIVE")
        if (summary["session_net_ci_low_bps"] is None or
                summary["session_net_ci_low_bps"] <= 0):
            failed.append("SESSION_BOOTSTRAP_CI_CROSSES_ZERO")
        if (positive_ratio is None or
                positive_ratio < self.rules["min_positive_session_ratio"]):
            failed.append("WALK_FORWARD_FOLDS_FRAGILE")
        if (summary["deflated_sharpe"] is None or
                summary["deflated_sharpe"] < self.rules["min_deflated_sharpe"]):
            failed.append("OVERFIT_DSR")
        if summary["dsr_calibration_mode"] == \
                "legacy_unit_trial_sharpe_std":
            # A raw number of searched formulas is not the cross-trial Sharpe
            # dispersion required by DSR.  The legacy unit-dispersion path is
            # retained for report compatibility, never as release evidence.
            failed.append("DSR_TRIAL_DISPERSION_UNMEASURED")
        if self.execution.upper() == "PASSIVE_FIFO_LOWER_BOUND" and (
                summary["fill_rate"] is None or
                summary["fill_rate"] < self.rules["min_passive_fill_rate"]):
            failed.append("PASSIVE_FILL_RATE_TOO_LOW")
        if self.family_pbo is None:
            failed.append("PBO_UNMEASURED")
        elif self.family_pbo > self.rules["max_pbo"]:
            failed.append("OVERFIT_PBO")
        if self.calibration_status not in {"PASS", "NOT_REQUIRED_FIXED_EQUATION"}:
            failed.append("SCORE_CALIBRATION_NOT_USABLE")

        evidence = bool(self.opportunities and session_values)
        teacher_strategy = self.teacher_stats.finish()
        hybrid_strategy = self.hybrid_stats.finish()
        scheduled_sessions = sorted(session_returns)
        teacher_peaks = _capital_peaks(
            self.teacher_stats.session_capital_deltas)
        hybrid_peaks = _capital_peaks(
            self.hybrid_stats.session_capital_deltas)
        common_denominators = {
            session: max(
                1,
                session_peak_capital.get(session, 0),
                teacher_peaks.get(session, 0),
                hybrid_peaks.get(session, 0),
            )
            for session in scheduled_sessions
        }
        common_returns = {
            "ast": {
                session: self.session_net_sum.get(session, 0.0) /
                common_denominators[session]
                for session in scheduled_sessions
            },
            "teacher": {
                session: self.teacher_stats.session_net_sum.get(session, 0.0) /
                common_denominators[session]
                for session in scheduled_sessions
            },
            "hybrid": {
                session: self.hybrid_stats.session_net_sum.get(session, 0.0) /
                common_denominators[session]
                for session in scheduled_sessions
            },
        }
        teacher_valid = self.teacher.status == "PASS"
        invalid_teacher_reason = (
            None if teacher_valid else
            f"teacher calibration status is {self.teacher.status}")
        n_predictions = self.teacher_prediction_count
        supervised_control = {
            "calibration": self.teacher.report(),
            "prediction": {
                "observations": n_predictions,
                "markout_rmse_bps": math.sqrt(
                    self.teacher_markout_squared_error / n_predictions)
                if n_predictions else None,
                "executable_net_rmse_bps": math.sqrt(
                    self.teacher_net_squared_error / n_predictions)
                if n_predictions else None,
                "positive_net_brier": (
                    self.teacher_brier_sum / n_predictions
                    if n_predictions else None),
            },
            "strategy": teacher_strategy,
            "paired_common_capital_session_returns_bps": common_returns[
                "teacher"],
            "increment_vs_ast": _paired_increment(
                self.teacher_stats.session_net_sum, self.session_net_sum,
                sessions=scheduled_sessions,
                common_denominators=common_denominators,
                valid=teacher_valid,
                invalid_reason=invalid_teacher_reason),
            "selection_boundary": "OOS_DIAGNOSTIC_CONTROL_ONLY",
            "independent_confirmation": False,
            "promotion_authority": False,
        }
        hybrid_control = {
            "definition": "FROZEN_TEACHER_EXPECTED_NET_AND_AST_ENTRY_GATE",
            "strategy": hybrid_strategy,
            "paired_common_capital_session_returns_bps": common_returns[
                "hybrid"],
            "increment_vs_ast": _paired_increment(
                self.hybrid_stats.session_net_sum, self.session_net_sum,
                sessions=scheduled_sessions,
                common_denominators=common_denominators,
                valid=teacher_valid,
                invalid_reason=invalid_teacher_reason),
            "increment_vs_teacher": _paired_increment(
                self.hybrid_stats.session_net_sum,
                self.teacher_stats.session_net_sum,
                sessions=scheduled_sessions,
                common_denominators=common_denominators,
                valid=teacher_valid,
                invalid_reason=invalid_teacher_reason),
            "selection_boundary": "OOS_DIAGNOSTIC_SCREENING_ONLY",
            "independent_confirmation": False,
            "forward_new_sessions_required": True,
            "promotion_authority": False,
        }
        return {
            "evaluator_version": self.evaluator_version,
            **({"feature_window_contract_version":
                self.feature_window_contract_version}
               if self.feature_window_contract_version ==
               EXPLICIT_FEATURE_WINDOW_CONTRACT else {}),
            "ast_fingerprint": fingerprint(self.expr),
            "ast_shape_fingerprint": shape_fingerprint(self.expr),
            "fields": sorted(fields_of(self.expr)),
            "lane_manifest": {
                "horizon_seconds": self.horizon_seconds,
                "execution": self.execution.upper(),
                "position_mode": self.position_mode.upper(),
                "threshold": self.threshold,
                "entry_policy": self.entry_policy,
                "coefficient_policy": self.coefficient_policy,
                "score_calibration": self._calibration_report(),
                "supervised_control": self.teacher.report(),
                "minimum_predicted_edge_bps": self.minimum_predicted_edge_bps,
                "purge_gap_seconds": self.spec.purge_gap.total_seconds(),
                "semantic_context": list(self.semantic_plan.get("context") or []),
                "portfolio_capital_model": (
                    "EXACT_PORTFOLIO_DECISION_TO_EXECUTABLE_EXIT_OR_"
                    "PASSIVE_EXPIRY_EVENT_SWEEP"),
                "capacity_quantile_method": (
                    "ORDER_INDEPENDENT_SHA256_BOTTOM_K_10000"),
            },
            "causality": self.causality,
            "folds": folds,
            "session_returns_bps": session_returns,
            "control_comparison": {
                "scheduled_session_ids": scheduled_sessions,
                "common_capital_denominator_by_session": common_denominators,
                "ast_common_capital_session_returns_bps": common_returns["ast"],
                "teacher_calibration_valid": teacher_valid,
                "paired_ci_method": "STATIONARY_BOOTSTRAP",
                "selection_boundary": "OOS_DIAGNOSTIC_CONTROL_ONLY",
                "promotion_authority": False,
            },
            "residual_behavior": residual_behavior,
            "supervised_control": supervised_control,
            "hybrid_control": hybrid_control,
            "summary": summary,
            "failed_criteria": failed,
            "decision": ("NO_EVIDENCE" if not evidence else
                         "SUBMIT_TO_QA" if not failed else "HOLD"),
            "not_a_promotion": (
                "SUBMIT_TO_QA is a review request; Risk, QA, and CEO retain promotion authority"),
        }


class CandidatePopulationAccumulator:
    """Evaluate several preregistered ASTs from one causal sample replay.

    Every candidate retains independent AST calibration, observations, capital
    normalization, costs, residuals, and statistical gates.  The raw replay,
    ordering/causality audit, and pure supervised-teacher fit/prediction pass
    are shared only across exact feature/label/model contracts.
    """

    def __init__(self, candidates: dict[str, CandidateAccumulator]):
        if not candidates:
            raise ValueError("candidate population must not be empty")
        self.candidates = dict(candidates)
        specs = {candidate.spec for candidate in self.candidates.values()}
        if len(specs) != 1:
            raise ValueError("candidate population must share one lane specification")
        self.spec = next(iter(specs))
        if len({id(candidate.teacher)
                for candidate in self.candidates.values()}) != len(self.candidates):
            raise ValueError(
                "population candidates must own independent teacher instances")

        # Fresh teachers with the same contract would consume the exact same
        # calibration stream.  Fit one representative, then restore its
        # fingerprint-verified artifact into fresh followers at freeze.  A
        # caller that supplied partially calibrated/restored teachers retains
        # the previous independent behavior via singleton groups.
        by_contract: dict[tuple, list[CandidateAccumulator]] = defaultdict(list)
        for candidate in self.candidates.values():
            by_contract[candidate.teacher.training_contract_key()].append(
                candidate)
        self._teacher_fit_groups: list[list[CandidateAccumulator]] = []
        for members in by_contract.values():
            if all(candidate.teacher.is_fresh() for candidate in members):
                self._teacher_fit_groups.append(members)
            else:
                self._teacher_fit_groups.extend([[candidate]
                                                 for candidate in members])
        self._teacher_artifacts: dict[int, dict] = {}

    @property
    def requires_calibration(self) -> bool:
        return any(candidate.teacher.status == "PENDING"
                   or candidate.calibration_status == "PENDING"
                   for candidate in self.candidates.values())

    def add(self, instrument_id: str, samples: list[IntradaySample], *,
            model_candidate=None, model_session: str | None = None) -> None:
        instrument_id = str(instrument_id)
        ordered, feature_cube = _prepare_sample_sequence(samples)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match sample instrument")
        audit = audit_causality(ordered, self.spec)
        predictions: dict[tuple, list[dict | None]] = {}
        targets: dict[tuple, list[tuple[float, float, float] | None]] = {}
        for candidate in self.candidates.values():
            training_key = candidate.teacher.training_contract_key()
            prediction_key = candidate.teacher.prediction_identity()
            if prediction_key not in predictions:
                if candidate.feature_window_contract_version == \
                        EXPLICIT_FEATURE_WINDOW_CONTRACT:
                    predictions[prediction_key] = candidate.teacher.predict(
                        ordered, feature_cube=feature_cube)
                else:
                    predictions[prediction_key] = candidate.teacher.predict(
                        ordered)
            if training_key not in targets:
                targets[training_key] = [
                    executable_target(
                        sample, horizon_seconds=candidate.horizon_seconds,
                        execution=candidate.execution)
                    for sample in ordered
                ]
            candidate._add_prepared(
                instrument_id, ordered, audit,
                shared_teacher_predictions=predictions[prediction_key],
                shared_teacher_targets=targets[training_key],
                feature_cube=feature_cube)
        if model_candidate is not None:
            # The MODEL_CANDIDATE lane owns its statistics, gates, lineage, and
            # authority.  It may reuse only the PRIMARY teacher's immutable
            # prediction/target pass; no AST score or AST entry decision crosses
            # this boundary.  MODEL_CANDIDATE independently recomputes the
            # frozen dot products and targets against this same aligned cube;
            # the shared values are assertions, never trusted evidence.
            primary = self.candidates.get("PRIMARY")
            if primary is None:
                raise ValueError(
                    "shared model replay requires a PRIMARY candidate")
            expected_model = (getattr(
                model_candidate, "teacher_report", {}) or {}).get(
                    "model_fingerprint")
            prediction_key = primary.teacher.prediction_identity()
            # The terminal prediction identity caches the verified frozen
            # fingerprint.  Calling report() here would rebuild three 244-wide
            # parameter payloads for every instrument/session slice.
            observed_model = prediction_key[-1]
            if expected_model != observed_model:
                raise ValueError(
                    "shared model replay teacher fingerprint changed")
            training_key = primary.teacher.training_contract_key()
            model_candidate.add_prepared(
                instrument_id, ordered, audit,
                predictions=predictions[prediction_key],
                targets=targets[training_key],
                evaluation_session=model_session,
                feature_cube=feature_cube,
                row_identity=(
                    str(feature_cube.decision_index_fingerprint)
                    if feature_cube is not None else
                    _decision_index_fingerprint(ordered)))

    def calibrate(self, instrument_id: str,
                  samples: list[IntradaySample]) -> None:
        instrument_id = str(instrument_id)
        ordered, feature_cube = _prepare_sample_sequence(samples)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match calibration sample")
        audit = audit_causality(ordered, self.spec)
        if audit["status"] == "FAIL":
            raise ValueError(
                "calibration sample causality audit failed: " +
                "; ".join(audit.get("findings") or []))
        for group_index, members in enumerate(self._teacher_fit_groups):
            if group_index in self._teacher_artifacts:
                raise ValueError("teacher is already frozen")
            if len(members) > 1 and any(
                    not candidate.teacher.is_fresh()
                    for candidate in members[1:]):
                raise ValueError(
                    "shared teacher follower changed before calibration freeze")
            representative = members[0]
            if representative.feature_window_contract_version == \
                    EXPLICIT_FEATURE_WINDOW_CONTRACT:
                representative.teacher.calibrate(
                    instrument_id, ordered, feature_cube=feature_cube)
            else:
                representative.teacher.calibrate(instrument_id, ordered)
        for candidate in self.candidates.values():
            candidate._calibrate_score_prepared(
                instrument_id, ordered, feature_cube=feature_cube)

    def freeze_calibration(self) -> dict[str, dict]:
        for group_index, members in enumerate(self._teacher_fit_groups):
            if group_index in self._teacher_artifacts:
                continue
            if len(members) > 1 and any(
                    not candidate.teacher.is_fresh()
                    for candidate in members[1:]):
                raise ValueError(
                    "shared teacher follower changed before calibration freeze")
            artifact = members[0].teacher.freeze()
            calibration_evidence = members[0].teacher.calibration_evidence()
            for candidate in members[1:]:
                candidate.teacher.restore(
                    artifact, calibration_evidence=calibration_evidence)
            self._teacher_artifacts[group_index] = copy.deepcopy(artifact)
        return {key: candidate.freeze_calibration()
                for key, candidate in self.candidates.items()}

    def finish(self) -> dict[str, dict]:
        return {key: candidate.finish()
                for key, candidate in self.candidates.items()}


def evaluate_candidate_stream(instrument_samples, *, expr: dict,
                              spec: IntradayLaneSpec,
                              horizon_seconds: int, execution: str,
                              position_mode: str = "LONG_ONLY",
                              threshold: float = 0.0, trials: int = 1,
                              entry_policy: str = "POSITIVE_SCORE",
                              coefficient_policy: str = "PREREGISTERED_NO_OOS_FIT",
                              minimum_predicted_edge_bps: float = 0.0,
                              family_pbo: float | None = None,
                              semantic_plan: dict | None = None,
                              criteria: dict | None = None,
                              feature_window_contract_version: str =
                              LEGACY_FEATURE_WINDOW_CONTRACT,
                              feature_cubes_by_instrument: dict | None = None
                              ) -> dict:
    accumulator = CandidateAccumulator(
        expr=expr, spec=spec, horizon_seconds=horizon_seconds,
        execution=execution, position_mode=position_mode, threshold=threshold,
        entry_policy=entry_policy,
        coefficient_policy=coefficient_policy,
        minimum_predicted_edge_bps=minimum_predicted_edge_bps,
        trials=trials, family_pbo=family_pbo, semantic_plan=semantic_plan,
        criteria=criteria,
        feature_window_contract_version=feature_window_contract_version)
    for instrument_id, samples in instrument_samples:
        cube = ((feature_cubes_by_instrument or {}).get(str(instrument_id))
                if feature_cubes_by_instrument is not None else None)
        if cube is not None:
            if feature_window_contract_version != \
                    EXPLICIT_FEATURE_WINDOW_CONTRACT:
                raise ValueError(
                    "feature cubes require the explicit-window contract")
            existing = getattr(samples, "feature_cube", None)
            if existing is not None and existing is not cube:
                raise ValueError(
                    "sample batch and separately supplied feature cube differ")
            if existing is None:
                samples = IntradaySampleBatch(tuple(samples), cube)
        accumulator.add(instrument_id, samples)
    return accumulator.finish()


def evaluate_candidate(samples_by_instrument: dict[str, list[IntradaySample]], *,
                       expr: dict, spec: IntradayLaneSpec,
                       horizon_seconds: int, execution: str,
                       position_mode: str = "LONG_ONLY",
                       threshold: float = 0.0, trials: int = 1,
                       entry_policy: str = "POSITIVE_SCORE",
                       coefficient_policy: str = "PREREGISTERED_NO_OOS_FIT",
                       minimum_predicted_edge_bps: float = 0.0,
                       family_pbo: float | None = None,
                       semantic_plan: dict | None = None,
                       criteria: dict | None = None,
                       feature_window_contract_version: str =
                       LEGACY_FEATURE_WINDOW_CONTRACT,
                       feature_cubes_by_instrument: dict | None = None) -> dict:
    """Evaluate one preregistered expression without tuning it on test results."""
    return evaluate_candidate_stream(
        ((instrument_id, samples_by_instrument[instrument_id])
         for instrument_id in sorted(samples_by_instrument)),
        expr=expr, spec=spec, horizon_seconds=horizon_seconds,
        execution=execution, position_mode=position_mode, threshold=threshold,
        entry_policy=entry_policy,
        coefficient_policy=coefficient_policy,
        minimum_predicted_edge_bps=minimum_predicted_edge_bps,
        trials=trials, family_pbo=family_pbo, semantic_plan=semantic_plan,
        criteria=criteria,
        feature_window_contract_version=feature_window_contract_version,
        feature_cubes_by_instrument=feature_cubes_by_instrument)
