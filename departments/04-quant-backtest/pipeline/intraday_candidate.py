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
import math
from statistics import fmean, pstdev
from zoneinfo import ZoneInfo

from intraday_alpha_ast import evaluate as evaluate_ast
from intraday_alpha_ast import fields_of, fingerprint, shape_fingerprint
from intraday_microstructure import IntradayLaneSpec, IntradaySample, audit_causality
from overfit_stats import bootstrap_ci, deflated_sharpe


EVALUATOR_VERSION = "intraday-candidate-evaluator-v2"
KST = ZoneInfo("Asia/Seoul")

DEFAULT_CRITERIA = {
    # DSR/bootstrap implementation requires 60 independent observations.  Since
    # overlapping ticks are collapsed to sessions, the release gate must require
    # the same 60 sessions instead of pretending hundreds of ticks are independent.
    "min_sessions": 60,
    "min_instruments": 2,
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
                  execution: str, position_mode: str) -> list[dict]:
    execution = str(execution).upper()
    if execution not in {"TAKER", "PASSIVE_FIFO_LOWER_BOUND"}:
        raise ValueError(f"unsupported execution: {execution}")
    position_mode = str(position_mode).upper()
    if position_mode != "LONG_ONLY":
        raise ValueError("factory intraday evaluator currently supports LONG_ONLY only")
    rows = []
    for sample, raw in zip(samples, values):
        if raw is None or not math.isfinite(float(raw)):
            continue
        value = float(raw)
        # Point-in-time borrow availability/fees are not in the governed source
        # plane.  Negative scores therefore mean abstain, never a free short.
        side = 1 if value > threshold else 0
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


def _max_concurrency(observations: list[dict], horizon_seconds: int) -> int:
    events = []
    for row in observations:
        start = row["decision_time"]
        events.append((start, 1))
        events.append((start.timestamp() + horizon_seconds, -1))
    # Exits sort before entries at the same instant so adjacent opportunities do
    # not count as overlapping capital commitments.
    normalized = [(value.timestamp() if hasattr(value, "timestamp") else float(value), delta)
                  for value, delta in events]
    current = peak = 0
    for _when, delta in sorted(normalized, key=lambda item: (item[0], item[1])):
        current += delta
        peak = max(peak, current)
    return peak


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


def evaluate_candidate(samples_by_instrument: dict[str, list[IntradaySample]], *,
                       expr: dict, spec: IntradayLaneSpec,
                       horizon_seconds: int, execution: str,
                       position_mode: str = "LONG_ONLY",
                       threshold: float = 0.0, trials: int = 1,
                       family_pbo: float | None = None,
                       semantic_plan: dict | None = None,
                       criteria: dict | None = None) -> dict:
    """Evaluate one preregistered expression without tuning it on test results."""
    if horizon_seconds not in spec.horizons_seconds:
        raise ValueError("horizon_seconds is absent from the lane specification")
    if threshold < 0 or not math.isfinite(float(threshold)):
        raise ValueError("threshold must be finite and non-negative")
    rules = {**DEFAULT_CRITERIA, **(criteria or {})}
    observations: list[dict] = []
    causality = []
    for instrument_id in sorted(samples_by_instrument):
        samples = sorted(samples_by_instrument[instrument_id],
                         key=lambda row: row.decision_time)
        if any(row.instrument_id != instrument_id for row in samples):
            raise ValueError("samples_by_instrument key does not match sample instrument")
        causality.append({"instrument_id": instrument_id,
                          **audit_causality(samples, spec)})
        values = evaluate_ast(samples, expr)
        if semantic_plan:
            paired = [(sample, value) for sample, value in zip(samples, values)
                      if _time_context_allows(
                          sample, semantic_plan.get("context") or ())]
            eligible_samples = [item[0] for item in paired]
            eligible_values = [item[1] for item in paired]
        else:
            eligible_samples, eligible_values = samples, values
        observations.extend(_observations(
            eligible_samples, eligible_values, horizon_seconds=horizon_seconds,
            threshold=threshold, execution=execution,
            position_mode=position_mode))

    sessions: dict[str, list[float]] = defaultdict(list)
    for row in observations:
        sessions[row["session"]].append(row["net_bps_per_opportunity"])
    # Treat every opportunity as a horizon-long equal-capital position.  Session
    # return is cumulative net P&L divided by peak concurrent commitments, not
    # an average trade that would erase economically meaningful frequency.
    by_session_observations: dict[str, list[dict]] = defaultdict(list)
    for row in observations:
        by_session_observations[row["session"]].append(row)
    session_returns = {
        session: sum(values) / max(
            1, _max_concurrency(by_session_observations[session], horizon_seconds))
        for session, values in sorted(sessions.items())
    }
    session_values = list(session_returns.values())
    filled = [row["net_bps_per_fill"] for row in observations
              if row["net_bps_per_fill"] is not None]
    capacities = sorted(row["capacity_shares_l1"] for row in observations)
    mid = [row["mid_markout_bps"] for row in observations]
    implementation_drag = [
        row["mid_markout_bps"] - row["net_bps_per_opportunity"]
        for row in observations]
    folds = _folds(session_returns)
    positive_ratio = (sum(row["positive"] for row in folds) / len(folds)
                      if folds else None)
    mean_ci = _bootstrap_mean(session_values)
    dsr = deflated_sharpe(
        [value / 10_000.0 for value in session_values], trials=max(1, trials),
        periods=252)
    sharpe_ci = bootstrap_ci(
        [value / 10_000.0 for value in session_values], periods=252)

    summary = {
        "sessions": len(session_values),
        "instruments": len({row["instrument_id"] for row in observations}),
        "opportunities": len(observations),
        "fills": len(filled),
        "fill_rate": len(filled) / len(observations) if observations else None,
        "mean_mid_markout_bps": fmean(mid) if mid else None,
        "mean_implementation_drag_bps": (
            fmean(implementation_drag) if implementation_drag else None),
        "mean_net_bps_per_fill": fmean(filled) if filled else None,
        "mean_net_bps_per_opportunity": (
            fmean(row["net_bps_per_opportunity"] for row in observations)
            if observations else None),
        "session_mean_net_bps": fmean(session_values) if session_values else None,
        "session_net_ci_low_bps": mean_ci[0],
        "session_net_ci_high_bps": mean_ci[1],
        "positive_fold_ratio": positive_ratio,
        "deflated_sharpe": dsr.get("deflated_sharpe"),
        "sharpe": dsr.get("sharpe"),
        "bootstrap_sharpe_ci_low": sharpe_ci.get("bootstrap_ci_low"),
        "bootstrap_sharpe_ci_high": sharpe_ci.get("bootstrap_ci_high"),
        "pbo": family_pbo,
        "trials": trials,
        "mean_capacity_shares_l1": fmean(capacities) if capacities else None,
        "p10_capacity_shares_l1": (
            capacities[max(0, int(0.10 * len(capacities)) - 1)] if capacities else None),
        "max_concurrent_opportunities": _max_concurrency(
            observations, horizon_seconds),
    }
    failed = []
    if not observations:
        failed.append("NO_EXECUTABLE_OBSERVATIONS")
    if any(row["status"] != "PASS" for row in causality):
        failed.append("CAUSALITY_NOT_PASS")
    for metric, rule in (("sessions", "min_sessions"),
                         ("instruments", "min_instruments"),
                         ("opportunities", "min_opportunities")):
        if summary[metric] < rules[rule]:
            failed.append(f"{metric.upper()}_BELOW_MINIMUM")
    if (summary["mean_net_bps_per_opportunity"] is None or
            summary["mean_net_bps_per_opportunity"] <=
            rules["min_mean_net_bps_per_opportunity"]):
        failed.append("NET_EDGE_NOT_POSITIVE")
    if (summary["session_net_ci_low_bps"] is None or
            summary["session_net_ci_low_bps"] <= 0):
        failed.append("SESSION_BOOTSTRAP_CI_CROSSES_ZERO")
    if (positive_ratio is None or
            positive_ratio < rules["min_positive_session_ratio"]):
        failed.append("WALK_FORWARD_FOLDS_FRAGILE")
    if (summary["deflated_sharpe"] is None or
            summary["deflated_sharpe"] < rules["min_deflated_sharpe"]):
        failed.append("OVERFIT_DSR")
    if execution.upper() == "PASSIVE_FIFO_LOWER_BOUND" and (
            summary["fill_rate"] is None or
            summary["fill_rate"] < rules["min_passive_fill_rate"]):
        failed.append("PASSIVE_FILL_RATE_TOO_LOW")
    if family_pbo is None:
        failed.append("PBO_UNMEASURED")
    elif family_pbo > rules["max_pbo"]:
        failed.append("OVERFIT_PBO")

    evidence = bool(observations and session_values)
    return {
        "evaluator_version": EVALUATOR_VERSION,
        "ast_fingerprint": fingerprint(expr),
        "ast_shape_fingerprint": shape_fingerprint(expr),
        "fields": sorted(fields_of(expr)),
        "lane_manifest": {
            "horizon_seconds": horizon_seconds,
            "execution": execution.upper(),
            "position_mode": position_mode.upper(),
            "threshold": threshold,
            "purge_gap_seconds": spec.purge_gap.total_seconds(),
            "semantic_context": list((semantic_plan or {}).get("context") or []),
        },
        "causality": causality,
        "folds": folds,
        "session_returns_bps": session_returns,
        "summary": summary,
        "failed_criteria": failed,
        # A quant evaluator can only submit evidence to QA.  It never promotes.
        "decision": ("NO_EVIDENCE" if not evidence else
                     "SUBMIT_TO_QA" if not failed else "HOLD"),
        "not_a_promotion": (
            "SUBMIT_TO_QA is a review request; Risk, QA, and CEO retain promotion authority"),
    }
