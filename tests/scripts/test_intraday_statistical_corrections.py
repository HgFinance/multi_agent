import math
import sys
from pathlib import Path


PIPELINE = (Path(__file__).resolve().parents[2]
            / "departments" / "04-quant-backtest" / "pipeline")
sys.path.insert(0, str(PIPELINE))

import intraday_multiple_testing as multiple_testing  # noqa: E402
import overfit_stats  # noqa: E402
import pbo_cscv  # noqa: E402


def _returns(n=180):
    return [0.0012 + 0.009 * math.sin(index * 0.71) +
            0.002 * math.cos(index * 0.17)
            for index in range(n)]


def test_dsr_uses_observed_trial_dispersion_and_effective_trial_count():
    returns = _returns()
    weak_penalty = overfit_stats.deflated_sharpe(
        returns, trials=50, trial_sharpe_std=0.25, effective_trials=5)
    strong_penalty = overfit_stats.deflated_sharpe(
        returns, trials=50, trial_sharpe_std=1.25, effective_trials=50)

    assert weak_penalty["calibration_mode"] == "observed_trial_sharpe_std"
    assert strong_penalty["expected_max_sharpe"] > weak_penalty["expected_max_sharpe"]
    assert strong_penalty["deflated_sharpe"] < weak_penalty["deflated_sharpe"]


def test_dsr_accepts_complete_trial_sharpe_vector_and_preserves_legacy_api():
    observed = overfit_stats.deflated_sharpe(
        _returns(), trial_sharpes=[0.1, 0.4, 0.7, 1.0],
        effective_trials=2.5)
    legacy = overfit_stats.deflated_sharpe(_returns(), trials=4)

    assert observed["trials"] == 4
    assert observed["effective_trials"] == 2.5
    assert observed["trial_sharpe_std"] > 0.0
    assert observed["calibration_mode"] == "observed_trial_sharpes"
    assert legacy["calibration_mode"] == "legacy_unit_trial_sharpe_std"
    old = overfit_stats._legacy_deflated_sharpe(_returns(), trials=4)
    for key in ("deflated_sharpe", "sharpe", "expected_max_sharpe", "trials"):
        assert legacy[key] == old[key]


def test_dsr_fails_closed_on_partial_or_impossible_trial_calibration():
    partial = overfit_stats.deflated_sharpe(
        _returns(), trials=5, trial_sharpes=[0.1, 0.2])
    impossible = overfit_stats.deflated_sharpe(
        _returns(), trials=5, trial_sharpe_std=0.2, effective_trials=6)

    assert partial["deflated_sharpe"] is None
    assert impossible["deflated_sharpe"] is None


def test_dsr_fractional_effective_trials_have_monotone_nonnegative_null():
    counts = [1.0, 1.000001, 1.1, 1.25, 1.5, 1.75, 2.0, 2.5, 3.0]
    thresholds = [overfit_stats.expected_max_sharpe(
        trial_sharpe_std=0.8, effective_trials=count)
        for count in counts]

    assert thresholds[0] == 0.0
    assert all(value >= thresholds[0] for value in thresholds)
    assert all(right >= left
               for left, right in zip(thresholds, thresholds[1:]))
    # Integer points retain the published approximation exactly.
    gamma = 0.5772156649015329
    expected_two = 0.8 * (
        (1.0 - gamma) * overfit_stats._norm_ppf(0.5) +
        gamma * overfit_stats._norm_ppf(1.0 - 1.0 / (2.0 * math.e)))
    assert math.isclose(thresholds[counts.index(2.0)], expected_two,
                        rel_tol=0.0, abs_tol=1e-12)


def test_stationary_bootstrap_is_deterministic_and_continues_circular_blocks():
    first = list(overfit_stats.stationary_bootstrap_indices(
        7, n_boot=4, restart_probability=1e-12, seed=19))
    second = list(overfit_stats.stationary_bootstrap_indices(
        7, n_boot=4, restart_probability=1e-12, seed=19))

    assert first == second
    assert all(next_index == (index + 1) % 7
               for path in first
               for index, next_index in zip(path, path[1:]))


def test_bootstrap_ci_defaults_to_stationary_and_keeps_iid_audit_mode():
    stationary = overfit_stats.bootstrap_ci(
        _returns(120), n_boot=199, seed=31)
    repeated = overfit_stats.bootstrap_ci(
        _returns(120), n_boot=199, seed=31)
    iid = overfit_stats.bootstrap_ci(
        _returns(120), n_boot=199, seed=31, method="iid")

    assert stationary == repeated
    assert stationary["bootstrap_method"] == "stationary"
    assert stationary["expected_block_length"] == 4.0
    assert iid["bootstrap_method"] == "iid"
    assert iid["restart_probability"] == 1.0


def _pbo_performance(n_variants=20, n_windows=12):
    return {
        f"v{variant:02d}": {
            f"w{window:02d}": (
                0.02 * math.sin((variant + 1) * (window + 2)) +
                0.001 * variant - 0.0003 * window)
            for window in range(n_windows)
        }
        for variant in range(n_variants)
    }


def test_cscv_large_universe_samples_uniform_complement_pairs_deterministically():
    splits_a, metadata_a = pbo_cscv._cscv_splits(
        12, max_splits=20, seed=7)
    splits_b, metadata_b = pbo_cscv._cscv_splits(
        12, max_splits=20, seed=7)
    splits_c, _ = pbo_cscv._cscv_splits(12, max_splits=20, seed=8)

    assert splits_a == splits_b
    assert metadata_a == metadata_b
    assert splits_a != splits_c
    assert metadata_a["sampling_mode"].startswith("uniform_complement_pairs")
    assert metadata_a["total_splits"] == math.comb(12, 6)
    assert len(splits_a) == 20
    universe = set(range(12))
    for left, right in zip(splits_a[::2], splits_a[1::2]):
        assert set(left).isdisjoint(right)
        assert set(left) | set(right) == universe


def test_cscv_exact_when_split_universe_fits_and_reports_adequacy():
    performance = _pbo_performance(n_variants=4, n_windows=4)
    result = pbo_cscv.compute(performance, max_splits=100, seed=11)

    assert result["sampling_mode"] == "exact"
    assert result["n_splits"] == math.comb(4, 2)
    assert result["total_splits"] == result["n_splits"]
    assert result["monte_carlo_se"] == 0.0
    assert result["adequacy_status"] == "diagnostic_only"
    assert result["sufficiency_warnings"]


def test_cscv_rejects_non_synchronous_matrix_instead_of_intersecting():
    performance = _pbo_performance(n_variants=4, n_windows=4)
    del performance["v03"]["w03"]
    result = pbo_cscv.compute(performance)

    assert result["probability_of_backtest_overfitting"] is None
    assert "synchronous" in result["reason"]


def test_cscv_validates_raw_matrix_before_any_identity_deduplication():
    performance = _pbo_performance(n_variants=5, n_windows=4)
    performance["v04"] = dict(performance["v00"])
    performance["v04"]["different_window"] = performance["v04"].pop("w03")
    identities = {variant: variant for variant in performance}
    identities["v04"] = identities["v00"]

    result = pbo_cscv.compute(
        performance, candidate_identities=identities)

    assert result["probability_of_backtest_overfitting"] is None
    assert result["duplicate_variants"] == []
    assert "synchronous" in result["reason"]


def test_cscv_never_deduplicates_distinct_candidates_by_realised_values():
    performance = _pbo_performance(n_variants=4, n_windows=4)
    performance["same_values_different_formula"] = dict(performance["v00"])

    retained = pbo_cscv.compute(performance)
    identities = {variant: variant for variant in performance}
    identities["same_values_different_formula"] = identities["v00"]
    proven_duplicate = pbo_cscv.compute(
        performance, candidate_identities=identities)

    assert retained["n_variants"] == 5
    assert "duplicate_variants" not in retained
    assert proven_duplicate["n_variants"] == 4
    assert proven_duplicate["duplicate_variants"] == [{
        "variant": "v00",
        "duplicate_of": "same_values_different_formula",
    }]


def test_intraday_pbo_loader_requires_exact_full_window_content_and_cost():
    class Cursor:
        def __init__(self, connection):
            self.connection = connection

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params):
            self.connection.sql = sql
            self.connection.params = params

        def fetchall(self):
            return list(self.connection.rows)

    class Connection:
        def __init__(self, rows):
            self.rows = rows
            self.sql = ""
            self.params = None

        def cursor(self):
            return Cursor(self)

    def dimensions(window, *, candidate=None, scope="FULL_60",
                   evaluation="evaluation-a", boundary="boundary-a",
                   content="content-a", instrument="instruments-a",
                   cost="cost-v3", start=None):
        out = {
            "window": window,
            "evaluation_scope": scope,
            "evaluation_identity_complete": True,
            "evaluation_fingerprint": evaluation,
            "session_boundary_fingerprint": boundary,
            "source_content_fingerprint": content,
            "instrument_ids_fingerprint": instrument,
            "cost_model_version": cost,
            "start_session": start or f"start-{window}",
            "end_session": f"end-{window}",
        }
        if candidate:
            out["screening_candidate"] = candidate
        return out

    rows = []
    for experiment, candidate in (("reference", None),
                                  ("reference", "side-a"),
                                  ("peer", None)):
        for window in range(4):
            rows.append((experiment, dimensions(
                f"INTRADAY_FOLD_{window}", candidate=candidate),
                         0.01 * (window + 1), "cost-v3", True))
    for window in range(4):
        rows.append(("discovery", dimensions(
            f"INTRADAY_FOLD_{window}", scope="DISCOVERY_6",
            evaluation="evaluation-b", boundary="boundary-b",
            content="content-b"), 0.5, "cost-v3", True))
        rows.append(("old-cost", dimensions(
            f"INTRADAY_FOLD_{window}", cost="cost-v2"),
                     0.5, "cost-v2", True))
        rows.append(("wrong-boundary", dimensions(
            f"INTRADAY_FOLD_{window}", start="different-start"),
                     0.5, "cost-v3", True))
        rows.append(("wrong-universe", dimensions(
            f"INTRADAY_FOLD_{window}", instrument="instruments-b"),
                     0.5, "cost-v3", True))

    connection = Connection(rows)
    loaded = pbo_cscv.load_family_performance(
        connection, "family-a", reference_experiment_id="reference",
        evaluation_scope="FULL_60")

    assert set(loaded) == {
        "reference", "reference:SCREEN:side-a", "peer"}
    assert all(len(windows) == 4 for windows in loaded.values())
    assert "quant.current_krx_stock_instrument_identity" in connection.sql
    assert "instrument_type" in connection.sql
    assert "asset_class" in connection.sql
    assert "`" not in connection.sql
    assert "_GOVERNED_PBO_EVIDENCE" not in connection.sql

    # A caller-controlled asset label cannot substitute for the SQL evidence.
    # The boolean is computed by the common reference-backed predicate; false
    # and legacy rows lacking that column both fail closed.
    forged = []
    for row in rows:
        dim = dict(row[1], asset_scope="ALL_PRODUCTS")
        forged.append((row[0], dim, row[2], row[3], False))
    assert pbo_cscv.load_family_performance(
        Connection(forged), "family-a", reference_experiment_id="reference",
        evaluation_scope="FULL_60") == {}
    assert pbo_cscv.load_family_performance(
        Connection([row[:4] for row in rows]), "family-a",
        reference_experiment_id="reference",
        evaluation_scope="FULL_60") == {}


def test_pbo_uses_paper_rank_orientation_and_counts_median_tie_conservatively():
    good = overfit_stats.pbo([18, 19, 20], n_strategies=20)
    bad = overfit_stats.pbo([1, 2, 3], n_strategies=20)
    tied = overfit_stats.pbo([2.5], n_strategies=4)

    assert good["pbo"] == 0.0
    assert bad["pbo"] == 1.0
    assert tied["pbo"] == 1.0


def test_paired_session_delta_requires_identical_cost_net_sessions():
    paired = multiple_testing.paired_session_deltas(
        {"s1": 3.0, "s2": 1.0}, {"s1": 1.0, "s2": 2.0},
        minimum_effect=0.25)
    missing = multiple_testing.paired_session_deltas(
        {"s1": 3.0, "s2": 1.0}, {"s1": 1.0})

    assert paired["valid"] is True
    assert paired["paired_deltas"] == [1.75, -1.25]
    assert missing["valid"] is False
    assert missing["paired_deltas"] == []


def test_spa_detects_strong_candidate_with_common_stationary_resampling():
    sessions = 90
    family = {
        "noise_a": [0.0015 * math.sin(index * 0.91)
                    for index in range(sessions)],
        "noise_b": [0.0020 * math.cos(index * 0.47)
                    for index in range(sessions)],
        "strong": [0.0030 + 0.0010 * math.sin(index * 0.33)
                   for index in range(sessions)],
        "poor": [-0.0040 + 0.0080 * math.sin(index * 1.13)
                 for index in range(sessions)],
    }
    result = multiple_testing.spa_reality_check(
        family, n_boot=499, seed=23, min_sessions=20)
    permuted = multiple_testing.spa_reality_check(
        dict(reversed(list(family.items()))), n_boot=499, seed=23,
        min_sessions=20)

    assert result["valid"] is True
    assert result["best_candidate"] == "strong"
    assert result["spa_pvalue_consistent"] <= 0.05
    assert result["spa_pvalue_lower"] <= result["spa_pvalue_upper"]
    assert result["common_bootstrap_indices"] is True
    assert result["spa_pvalue_consistent"] == permuted["spa_pvalue_consistent"]


def test_spa_fails_closed_on_missing_candidate_sessions():
    result = multiple_testing.spa_reality_check(
        {"a": [0.1] * 30, "b": [0.1] * 29}, n_boot=19)
    assert result["valid"] is False
    assert "same sessions" in result["reason"]


def test_e_bh_requires_validity_certificates_and_applies_step_up_rule():
    values = {"h1": 100.0, "h2": 50.0, "h3": 0.2}
    closed = multiple_testing.e_bh(values, alpha=0.05)
    decided = multiple_testing.e_bh(
        values, alpha=0.05, validity={key: True for key in values})

    assert closed["valid"] is False
    assert closed["rejected"] == []
    assert decided["valid"] is True
    assert decided["selected_k"] == 2
    assert decided["cutoff"] == 30.0
    assert decided["rejected"] == ["h1", "h2"]
