"""End-to-end evaluator for an intraday AST candidate.

This module is intentionally pure: the runtime adapter may load Timescale rows and
persist the returned report, while the scientific decision remains reproducible in
unit tests.  Dependence between overlapping five-second labels is not treated as
independent evidence; returns are first aggregated by KRX session and all inference
uses those session-level observations.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import timezone
import hashlib
import heapq
import math
from statistics import fmean, median, pstdev
from zoneinfo import ZoneInfo

from intraday_alpha_ast import evaluate as evaluate_ast
from intraday_alpha_ast import (fields_of, fingerprint, shape_fingerprint,
                                unit_of)
from intraday_microstructure import IntradayLaneSpec, IntradaySample, audit_causality
from overfit_stats import bootstrap_ci, deflated_sharpe


EVALUATOR_VERSION = "intraday-candidate-evaluator-v7"
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
              if item not in {"PBO_UNMEASURED", "OVERFIT_PBO"}]
    out["summary"]["pbo"] = family_pbo
    if family_pbo is None:
        failed.append("PBO_UNMEASURED")
    elif not math.isfinite(float(family_pbo)) or float(family_pbo) > rules["max_pbo"]:
        failed.append("OVERFIT_PBO")
    out["failed_criteria"] = failed
    had_evidence = report.get("decision") != "NO_EVIDENCE"
    out["decision"] = ("NO_EVIDENCE" if not had_evidence else
                       "SUBMIT_TO_QA" if not failed else "HOLD")
    return out


def _label(sample: IntradaySample, horizon_seconds: int):
    return next((label for label in sample.labels
                 if label.horizon_seconds == horizon_seconds), None)


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
            hurdle = float(minimum_predicted_edge_bps)
            if execution == "TAKER":
                hurdle += float(sample.spread_bps) + 2.0 * float(fee_bps_per_side)
            else:
                hurdle += (float(maker_fee_bps_per_side)
                           + float(fee_bps_per_side))
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
            "capacity_shares_l1": (float(sample.entry_ask_depth_l1) if side > 0
                                    else float(sample.entry_bid_depth_l1)),
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
                 criteria: dict | None = None):
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
        self.expr = expr
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
        self.capacity = _CapacityReservoir()
        self.calibration_observations = 0
        self.calibration_nonzero_scores = 0
        self.calibration_instruments: set[str] = set()
        self.calibration_sessions: set[str] = set()
        self.calibration_sum_score_sq = 0.0
        self.calibration_sum_score_markout = 0.0
        self.calibration_beta = (
            None if coefficient_policy == "STRUCTURE_ONLY" else 1.0)
        self.calibration_status = (
            "PENDING" if coefficient_policy == "STRUCTURE_ONLY"
            else "NOT_REQUIRED_FIXED_EQUATION")
        self.residual_prediction_unit = (
            "BPS" if coefficient_policy == "STRUCTURE_ONLY"
            or unit_of(expr) == "BPS" else None)
        self.time_residual_stats: dict[str, list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
        self.session_residual_stats: dict[str, list[float]] = defaultdict(
            lambda: [0.0, 0.0, 0.0, 0.0, 0.0, 0.0])

    def calibrate(self, instrument_id: str,
                  samples: list[IntradaySample]) -> None:
        """Accumulate a pooled score->markout scale without retaining raw rows.

        Calibration is origin anchored so an AST state gate that emits zero
        remains an abstention after mapping.  Only a positive coefficient is
        admissible: a negative fitted relation falsifies the proposed direction
        instead of silently turning it into a different strategy.
        """
        if self.coefficient_policy != "STRUCTURE_ONLY":
            return
        if self.calibration_status != "PENDING":
            raise ValueError("score calibration is already frozen")
        instrument_id = str(instrument_id)
        ordered = sorted(samples, key=lambda row: row.decision_time)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match calibration sample")
        audit_causality(ordered, self.spec)
        values = evaluate_ast(ordered, self.expr)
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
        if contributed:
            self.calibration_instruments.add(instrument_id)

    def freeze_calibration(self) -> dict:
        """Lock one scale coefficient before any evaluation sample is consumed."""
        if self.coefficient_policy != "STRUCTURE_ONLY":
            return self._calibration_report()
        if self.calibration_status != "PENDING":
            return self._calibration_report()
        sufficient = (
            self.calibration_observations >= MIN_CALIBRATION_OBSERVATIONS
            and self.calibration_nonzero_scores >= MIN_CALIBRATION_NONZERO_SCORES
            and len(self.calibration_instruments) >= MIN_CALIBRATION_INSTRUMENTS
            and self.calibration_sum_score_sq > 0.0
        )
        if not sufficient:
            self.calibration_beta = 0.0
            self.calibration_status = "INSUFFICIENT_CALIBRATION"
            return self._calibration_report()
        denominator = self.calibration_sum_score_sq * (
            1.0 + CALIBRATION_SHRINKAGE_FRACTION)
        raw_beta = self.calibration_sum_score_markout / denominator
        self.calibration_beta = max(0.0, raw_beta) if math.isfinite(raw_beta) else 0.0
        self.calibration_status = (
            "PASS" if self.calibration_beta > 0.0
            else "NON_POSITIVE_DIRECTIONAL_RELATION")
        return self._calibration_report()

    def _calibration_report(self) -> dict:
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
        }

    def add(self, instrument_id: str, samples: list[IntradaySample]) -> None:
        """Consume one instrument/session slice and immediately release its rows."""
        instrument_id = str(instrument_id)
        ordered = sorted(samples, key=lambda row: row.decision_time)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match sample instrument")
        audit = audit_causality(ordered, self.spec)
        self._add_prepared(instrument_id, ordered, audit)

    def _add_prepared(self, instrument_id: str,
                      ordered: list[IntradaySample], audit: dict) -> None:
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
        values = evaluate_ast(ordered, self.expr)
        if self.coefficient_policy == "STRUCTURE_ONLY":
            beta = float(self.calibration_beta or 0.0)
            values = [None if value is None else float(value) * beta
                      for value in values]
        if self.semantic_plan:
            paired = [(sample, value) for sample, value in zip(ordered, values)
                      if _time_context_allows(
                          sample, self.semantic_plan.get("context") or ())]
            eligible_samples = [item[0] for item in paired]
            eligible_values = [item[1] for item in paired]
        else:
            eligible_samples, eligible_values = ordered, values
        self._add_residuals(eligible_samples, eligible_values)
        observations = _observations(
            eligible_samples, eligible_values,
            horizon_seconds=self.horizon_seconds, threshold=self.threshold,
            execution=self.execution, position_mode=self.position_mode,
            entry_policy=self.entry_policy,
            fee_bps_per_side=self.spec.fee_bps_per_side,
            maker_fee_bps_per_side=self.spec.maker_fee_bps_per_side,
            minimum_predicted_edge_bps=self.minimum_predicted_edge_bps)
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
            self.capacity_sum += capacity
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
                start = row["decision_time"].timestamp()
                deltas[start] += 1
                deltas[start + self.horizon_seconds] -= 1

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
            "promotion_authority": False,
        }

    def finish(self) -> dict:
        residual_behavior = self._residual_behavior()
        session_peak_capital = {}
        for session, deltas in self.session_capital_deltas.items():
            current = peak = 0
            for when in sorted(deltas):
                current += deltas[when]
                peak = max(peak, current)
            session_peak_capital[session] = peak
        session_returns = {
            session: self.session_net_sum[session] / max(1, peak)
            for session, peak in sorted(session_peak_capital.items())
        }
        session_values = list(session_returns.values())
        folds = _folds(session_returns)
        positive_ratio = (sum(row["positive"] for row in folds) / len(folds)
                          if folds else None)
        mean_ci = _bootstrap_mean(session_values)
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
            "session_net_ci_low_bps": mean_ci[0],
            "session_net_ci_high_bps": mean_ci[1],
            "positive_fold_ratio": positive_ratio,
            "deflated_sharpe": dsr.get("deflated_sharpe"),
            "sharpe": dsr.get("sharpe"),
            "bootstrap_sharpe_ci_low": sharpe_ci.get("bootstrap_ci_low"),
            "bootstrap_sharpe_ci_high": sharpe_ci.get("bootstrap_ci_high"),
            "pbo": self.family_pbo,
            "trials": self.trials,
            "mean_capacity_shares_l1": (
                self.capacity_sum / self.opportunities
                if self.opportunities else None),
            "p10_capacity_shares_l1": self.capacity.quantile(0.10),
            "capacity_quantile_sample_size": len(self.capacity.values),
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
        return {
            "evaluator_version": EVALUATOR_VERSION,
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
                "minimum_predicted_edge_bps": self.minimum_predicted_edge_bps,
                "purge_gap_seconds": self.spec.purge_gap.total_seconds(),
                "semantic_context": list(self.semantic_plan.get("context") or []),
                "portfolio_capital_model": (
                    "EXACT_PORTFOLIO_EVENT_SWEEP_FROM_STREAMED_DELTAS"),
                "capacity_quantile_method": (
                    "ORDER_INDEPENDENT_SHA256_BOTTOM_K_10000"),
            },
            "causality": self.causality,
            "folds": folds,
            "session_returns_bps": session_returns,
            "residual_behavior": residual_behavior,
            "summary": summary,
            "failed_criteria": failed,
            "decision": ("NO_EVIDENCE" if not evidence else
                         "SUBMIT_TO_QA" if not failed else "HOLD"),
            "not_a_promotion": (
                "SUBMIT_TO_QA is a review request; Risk, QA, and CEO retain promotion authority"),
        }


class CandidatePopulationAccumulator:
    """Evaluate several preregistered ASTs from one causal sample replay.

    Every candidate retains independent observations, capital normalization,
    costs and statistical gates.  Only the expensive raw-event-to-sample pass,
    ordering and causality audit are shared.
    """

    def __init__(self, candidates: dict[str, CandidateAccumulator]):
        if not candidates:
            raise ValueError("candidate population must not be empty")
        self.candidates = dict(candidates)
        specs = {candidate.spec for candidate in self.candidates.values()}
        if len(specs) != 1:
            raise ValueError("candidate population must share one lane specification")
        self.spec = next(iter(specs))

    @property
    def requires_calibration(self) -> bool:
        return any(candidate.coefficient_policy == "STRUCTURE_ONLY"
                   for candidate in self.candidates.values())

    def add(self, instrument_id: str, samples: list[IntradaySample]) -> None:
        instrument_id = str(instrument_id)
        ordered = sorted(samples, key=lambda row: row.decision_time)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match sample instrument")
        audit = audit_causality(ordered, self.spec)
        for candidate in self.candidates.values():
            candidate._add_prepared(instrument_id, ordered, audit)

    def calibrate(self, instrument_id: str,
                  samples: list[IntradaySample]) -> None:
        for candidate in self.candidates.values():
            candidate.calibrate(instrument_id, samples)

    def freeze_calibration(self) -> dict[str, dict]:
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
                              criteria: dict | None = None) -> dict:
    accumulator = CandidateAccumulator(
        expr=expr, spec=spec, horizon_seconds=horizon_seconds,
        execution=execution, position_mode=position_mode, threshold=threshold,
        entry_policy=entry_policy,
        coefficient_policy=coefficient_policy,
        minimum_predicted_edge_bps=minimum_predicted_edge_bps,
        trials=trials, family_pbo=family_pbo, semantic_plan=semantic_plan,
        criteria=criteria)
    for instrument_id, samples in instrument_samples:
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
                       criteria: dict | None = None) -> dict:
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
        criteria=criteria)
