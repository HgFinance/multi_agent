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
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from intraday_alpha_ast import evaluate as evaluate_ast
from intraday_alpha_ast import fields_of, fingerprint, shape_fingerprint
from intraday_microstructure import IntradayLaneSpec, IntradaySample, audit_causality
from overfit_stats import bootstrap_ci, deflated_sharpe


EVALUATOR_VERSION = "intraday-candidate-evaluator-v5"
KST = ZoneInfo("Asia/Seoul")
ENTRY_POLICIES = frozenset({
    "POSITIVE_SCORE",
    "PREDICTED_MARKOUT_CLEARS_COST",
})

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
        self.expr = expr
        self.spec = spec
        self.horizon_seconds = horizon_seconds
        self.execution = execution
        self.position_mode = position_mode
        self.threshold = threshold
        self.entry_policy = entry_policy
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
        self.requested_instruments.add(instrument_id)
        self.causality.append({"instrument_id": instrument_id, **audit})
        if ordered:
            self.sampled_instruments.add(instrument_id)
        values = evaluate_ast(ordered, self.expr)
        if self.semantic_plan:
            paired = [(sample, value) for sample, value in zip(ordered, values)
                      if _time_context_allows(
                          sample, self.semantic_plan.get("context") or ())]
            eligible_samples = [item[0] for item in paired]
            eligible_values = [item[1] for item in paired]
        else:
            eligible_samples, eligible_values = ordered, values
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

    def finish(self) -> dict:
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

    def add(self, instrument_id: str, samples: list[IntradaySample]) -> None:
        instrument_id = str(instrument_id)
        ordered = sorted(samples, key=lambda row: row.decision_time)
        if any(row.instrument_id != instrument_id for row in ordered):
            raise ValueError("instrument key does not match sample instrument")
        audit = audit_causality(ordered, self.spec)
        for candidate in self.candidates.values():
            candidate._add_prepared(instrument_id, ordered, audit)

    def finish(self) -> dict[str, dict]:
        return {key: candidate.finish()
                for key, candidate in self.candidates.items()}


def evaluate_candidate_stream(instrument_samples, *, expr: dict,
                              spec: IntradayLaneSpec,
                              horizon_seconds: int, execution: str,
                              position_mode: str = "LONG_ONLY",
                              threshold: float = 0.0, trials: int = 1,
                              entry_policy: str = "POSITIVE_SCORE",
                              minimum_predicted_edge_bps: float = 0.0,
                              family_pbo: float | None = None,
                              semantic_plan: dict | None = None,
                              criteria: dict | None = None) -> dict:
    accumulator = CandidateAccumulator(
        expr=expr, spec=spec, horizon_seconds=horizon_seconds,
        execution=execution, position_mode=position_mode, threshold=threshold,
        entry_policy=entry_policy,
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
        minimum_predicted_edge_bps=minimum_predicted_edge_bps,
        trials=trials, family_pbo=family_pbo, semantic_plan=semantic_plan,
        criteria=criteria)
