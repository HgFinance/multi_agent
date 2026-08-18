"""Dependence-aware, multiple-testing diagnostics for intraday candidates.

The functions in this module are deliberately pure: they do not query the
trial ledger or decide a release.  Callers must pass the complete synchronous
candidate family and cost-net session outcomes.  Historical SPA/Reality Check
results are retrospective diagnostics, not independent confirmation; a
candidate exposed to these sessions still needs a frozen future lockbox.

Primary methods:
  Hansen (2005), Test for Superior Predictive Ability
  Wang and Ramdas (2022), False discovery rate control with e-values
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence

from overfit_stats import stationary_bootstrap_indices


MODULE_VERSION = "intraday-multiple-testing-v1"
DEFAULT_BOOTSTRAPS = 10_000
# Engineering default matching Hansen's empirical illustration, not a
# universal paper threshold.  Production reports must sensitivity-check q.
DEFAULT_RESTART_PROBABILITY = 0.25
DEFAULT_MIN_SESSIONS = 20
_VARIANCE_EPSILON = 1e-18


def paired_session_deltas(
        candidate_cost_net: Mapping[str, float],
        benchmark_cost_net: Mapping[str, float], *,
        minimum_effect: float = 0.0,
        min_sessions: int = 2) -> dict:
    """Align cost-net session outcomes and return paired excess performance.

    The returned value for session ``t`` is
    ``candidate[t] - benchmark[t] - minimum_effect``.  Missing sessions are
    not silently intersected because that would let a strategy choose its
    evaluation sample through trading/data availability.
    """
    candidate_keys = set(candidate_cost_net)
    benchmark_keys = set(benchmark_cost_net)
    if candidate_keys != benchmark_keys:
        return {
            "valid": False,
            "paired_deltas": [],
            "reason": "candidate and benchmark must cover identical sessions",
            "candidate_only": sorted(map(str, candidate_keys - benchmark_keys)),
            "benchmark_only": sorted(map(str, benchmark_keys - candidate_keys)),
        }
    try:
        threshold = float(minimum_effect)
    except (TypeError, ValueError, OverflowError):
        threshold = math.nan
    if not math.isfinite(threshold):
        return {"valid": False, "paired_deltas": [],
                "reason": "minimum_effect must be finite"}
    if min_sessions < 1 or len(candidate_keys) < min_sessions:
        return {
            "valid": False,
            "paired_deltas": [],
            "reason": f"need at least {min_sessions} paired sessions",
            "n_sessions": len(candidate_keys),
        }

    sessions = sorted(candidate_keys, key=str)
    deltas = []
    try:
        for session in sessions:
            candidate = float(candidate_cost_net[session])
            benchmark = float(benchmark_cost_net[session])
            delta = candidate - benchmark - threshold
            if not (math.isfinite(candidate) and math.isfinite(benchmark) and
                    math.isfinite(delta)):
                raise ValueError("non-finite session value")
            deltas.append(delta)
    except (TypeError, ValueError, OverflowError) as exc:
        return {"valid": False, "paired_deltas": [],
                "reason": f"invalid paired session value: {exc}"}

    return {
        "valid": True,
        "sessions": [str(session) for session in sessions],
        "paired_deltas": deltas,
        "n_sessions": len(deltas),
        "minimum_effect": threshold,
        "mean_paired_delta": math.fsum(deltas) / len(deltas),
        "values_are_cost_net": True,
    }


def _stationary_long_run_variance(values: Sequence[float],
                                  restart_probability: float) -> float:
    """Hansen's stationary-bootstrap population long-run variance."""
    n = len(values)
    mean = math.fsum(values) / n
    centred = [value - mean for value in values]
    gamma_zero = math.fsum(value * value for value in centred) / n
    variance = gamma_zero
    continuation = 1.0 - restart_probability
    for lag in range(1, n):
        covariance = math.fsum(
            centred[index] * centred[index + lag]
            for index in range(n - lag)) / n
        kernel = (((n - lag) / n) * continuation ** lag +
                  (lag / n) * continuation ** (n - lag))
        variance += 2.0 * kernel * covariance
    # Finite precision can put an analytically non-negative estimate a few
    # ulps below zero.  Materially negative estimates fail in the caller.
    if -_VARIANCE_EPSILON <= variance < 0.0:
        return 0.0
    return variance


def _mc_pvalue(exceedances: int, draws: int) -> float:
    """Conservative finite-Monte-Carlo p-value (never spuriously zero)."""
    return (exceedances + 1.0) / (draws + 1.0)


def spa_reality_check(
        relative_performance: Mapping[str, Sequence[float]], *,
        n_boot: int = DEFAULT_BOOTSTRAPS,
        restart_probability: float = DEFAULT_RESTART_PROBABILITY,
        seed: int = 20260817,
        min_sessions: int = DEFAULT_MIN_SESSIONS) -> dict:
    """Run Hansen SPA and White-style Reality Check on a full trial family.

    ``relative_performance[candidate][t]`` is the paired, cost-net session
    performance relative to one frozen benchmark; positive is better.  Every
    candidate must use the same ordered sessions.  The same stationary-
    bootstrap path is applied to every candidate, preserving both serial and
    cross-candidate dependence.

    Returned ``spa_pvalue_consistent`` uses Hansen's sample-dependent null.
    Lower/upper p-values expose sensitivity to null recentering.  The Reality
    Check uses the non-studentized maximum and least-favourable null and is an
    audit diagnostic because irrelevant noisy alternatives can reduce power.
    """
    if not relative_performance:
        return {"valid": False, "reason": "candidate family is empty"}
    if n_boot < 1:
        return {"valid": False, "reason": "n_boot must be positive"}
    try:
        q = float(restart_probability)
    except (TypeError, ValueError, OverflowError):
        q = math.nan
    if not math.isfinite(q) or not 0.0 < q <= 1.0:
        return {"valid": False,
                "reason": "restart_probability must be in (0, 1]"}

    candidate_ids = sorted(relative_performance)
    columns: dict[str, list[float]] = {}
    lengths = set()
    try:
        for candidate_id in candidate_ids:
            column = [float(value)
                      for value in relative_performance[candidate_id]]
            if any(not math.isfinite(value) for value in column):
                raise ValueError(f"non-finite value for {candidate_id}")
            columns[candidate_id] = column
            lengths.add(len(column))
    except (TypeError, ValueError, OverflowError) as exc:
        return {"valid": False, "reason": str(exc)}
    if len(lengths) != 1:
        return {"valid": False,
                "reason": "all candidates must cover the same sessions"}
    n_sessions = next(iter(lengths))
    if n_sessions < max(3, min_sessions):
        return {"valid": False,
                "reason": f"need at least {max(3, min_sessions)} sessions",
                "n_sessions": n_sessions}

    root_n = math.sqrt(n_sessions)
    means: dict[str, float] = {}
    omegas: dict[str, float] = {}
    t_statistics: dict[str, float] = {}
    ignored_zero_variance = []
    for candidate_id in candidate_ids:
        values = columns[candidate_id]
        mean = math.fsum(values) / n_sessions
        long_run_variance = _stationary_long_run_variance(values, q)
        if not math.isfinite(long_run_variance) or long_run_variance < 0.0:
            return {"valid": False,
                    "reason": f"invalid long-run variance for {candidate_id}"}
        means[candidate_id] = mean
        if long_run_variance <= _VARIANCE_EPSILON:
            if mean > 0.0:
                return {
                    "valid": False,
                    "reason": ("positive candidate has degenerate session "
                               f"variance: {candidate_id}"),
                }
            ignored_zero_variance.append(candidate_id)
            omegas[candidate_id] = 0.0
            t_statistics[candidate_id] = (-math.inf if mean < 0.0 else 0.0)
            continue
        omega = math.sqrt(long_run_variance)
        omegas[candidate_id] = omega
        t_statistics[candidate_id] = root_n * mean / omega

    observed_spa = max(0.0, max(t_statistics.values()))
    observed_rc = max(0.0, root_n * max(means.values()))
    consistent_boundary = math.sqrt(2.0 * math.log(math.log(n_sessions)))

    subtract_lower = {}
    subtract_consistent = {}
    subtract_upper = {}
    for candidate_id in candidate_ids:
        mean = means[candidate_id]
        statistic = t_statistics[candidate_id]
        subtract_lower[candidate_id] = max(0.0, mean)
        subtract_consistent[candidate_id] = (
            mean if statistic > -consistent_boundary else 0.0)
        subtract_upper[candidate_id] = mean

    exceed_lower = exceed_consistent = exceed_upper = exceed_rc = 0
    try:
        paths = stationary_bootstrap_indices(
            n_sessions, n_boot=n_boot, restart_probability=q, seed=seed)
        for path in paths:
            boot_means = {
                candidate_id: math.fsum(
                    columns[candidate_id][index] for index in path) / n_sessions
                for candidate_id in candidate_ids
            }

            def _spa_bootstrap_statistic(subtractions: Mapping[str, float]) -> float:
                values = [
                    root_n * (boot_means[candidate_id] -
                              subtractions[candidate_id]) /
                    omegas[candidate_id]
                    for candidate_id in candidate_ids
                    if omegas[candidate_id] > 0.0
                ]
                return max(0.0, max(values, default=0.0))

            if _spa_bootstrap_statistic(subtract_lower) >= observed_spa:
                exceed_lower += 1
            if _spa_bootstrap_statistic(subtract_consistent) >= observed_spa:
                exceed_consistent += 1
            if _spa_bootstrap_statistic(subtract_upper) >= observed_spa:
                exceed_upper += 1
            rc_bootstrap = max(
                0.0,
                root_n * max(boot_means[candidate_id] - means[candidate_id]
                             for candidate_id in candidate_ids),
            )
            if rc_bootstrap >= observed_rc:
                exceed_rc += 1
    except (TypeError, ValueError, OverflowError) as exc:
        return {"valid": False, "reason": str(exc)}

    p_lower = _mc_pvalue(exceed_lower, n_boot)
    p_consistent = _mc_pvalue(exceed_consistent, n_boot)
    p_upper = _mc_pvalue(exceed_upper, n_boot)
    p_rc = _mc_pvalue(exceed_rc, n_boot)
    best_candidate = (max(candidate_ids, key=lambda key: t_statistics[key])
                      if observed_spa > 0.0 else None)
    diagnostics = {}
    for candidate_id in candidate_ids:
        statistic = t_statistics[candidate_id]
        diagnostics[candidate_id] = {
            "mean_relative_performance": means[candidate_id],
            "long_run_std": omegas[candidate_id],
            "studentized_statistic": (statistic
                                      if math.isfinite(statistic) else None),
        }
    return {
        "valid": True,
        "spa_statistic": observed_spa,
        "spa_pvalue_lower": p_lower,
        "spa_pvalue_consistent": p_consistent,
        "spa_pvalue_upper": p_upper,
        "reality_check_statistic": observed_rc,
        "reality_check_pvalue": p_rc,
        "best_candidate": best_candidate,
        "candidate_diagnostics": diagnostics,
        "ignored_zero_variance_candidates": ignored_zero_variance,
        "n_candidates": len(candidate_ids),
        "n_sessions": n_sessions,
        "n_boot": n_boot,
        "restart_probability": q,
        "expected_block_length": 1.0 / q,
        "seed": seed,
        "common_bootstrap_indices": True,
        "monte_carlo_pvalue_floor": 1.0 / (n_boot + 1.0),
        "historical_diagnostic_only": True,
        "independent_confirmation": False,
    }


def e_bh(e_values: Mapping[str, float], *, alpha: float = 0.05,
         validity: Mapping[str, bool] | None = None) -> dict:
    """Apply the e-Benjamini-Hochberg step-up rule.

    Numeric non-negativity alone cannot establish that a statistic is an
    e-value.  Therefore a validity certificate is required for every member of
    the predeclared family.  Missing/false certificates or non-finite values
    fail the entire decision closed rather than shrinking the tested family.
    """
    if not 0.0 < alpha < 1.0:
        return {"valid": False, "rejected": [],
                "reason": "alpha must be in (0, 1)"}
    if not e_values:
        return {"valid": False, "rejected": [],
                "reason": "e-value family is empty"}
    keys = set(e_values)
    if validity is None or set(validity) != keys or not all(validity.values()):
        return {
            "valid": False,
            "rejected": [],
            "reason": "every family member needs an affirmative e-value validity certificate",
            "family_size": len(keys),
        }
    try:
        numeric = {key: float(value) for key, value in e_values.items()}
    except (TypeError, ValueError, OverflowError):
        numeric = {}
    if (set(numeric) != keys or
            any(not math.isfinite(value) or value < 0.0
                for value in numeric.values())):
        return {"valid": False, "rejected": [],
                "reason": "e-values must be finite and non-negative",
                "family_size": len(keys)}

    ordered = sorted(numeric.items(), key=lambda item: (-item[1], item[0]))
    family_size = len(ordered)
    selected_k = 0
    for rank, (_hypothesis, value) in enumerate(ordered, start=1):
        if value >= family_size / (alpha * rank):
            selected_k = rank
    if selected_k == 0:
        cutoff = None
        rejected = []
    else:
        cutoff = family_size / (alpha * selected_k)
        rejected = [hypothesis for hypothesis, value in ordered
                    if value >= cutoff]
    return {
        "valid": True,
        "method": "e-BH",
        "alpha": alpha,
        "family_size": family_size,
        "selected_k": selected_k,
        "cutoff": cutoff,
        "rejected": rejected,
        "ordered_e_values": ordered,
        "arbitrary_dependence_control": True,
    }
