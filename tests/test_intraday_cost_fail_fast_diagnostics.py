from __future__ import annotations

import sys
import inspect
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (PIPELINE, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_experiment_runner as runner  # noqa: E402


def test_cost_fail_fast_is_measured_memory_not_raw_data_absence():
    reports = {
        "PRIMARY": {"failed_criteria": ["NO_EXECUTABLE_OBSERVATIONS"]},
        "side": {"failed_criteria": ["NO_EXECUTABLE_OBSERVATIONS"]},
    }
    calibration = {
        "PRIMARY": {
            "status": "NO_COST_FEASIBLE_ENTRY",
            "coefficient_policy": "STRUCTURE_ONLY",
            "cost_feasible_entry_possible": False,
            "observations": 262_042,
            "minimum_observed_entry_hurdle_bps": 23.0,
            "maximum_calibrated_predicted_markout_bps": 3.9757,
        },
        "side": {
            "status": "NON_POSITIVE_DIRECTIONAL_RELATION",
            "coefficient_policy": "STRUCTURE_ONLY",
            "cost_feasible_entry_possible": False,
            "observations": 259_773,
            "minimum_observed_entry_hurdle_bps": 23.0,
            "maximum_calibrated_predicted_markout_bps": 0.0,
        },
    }

    assert runner._all_symbolic_candidates_cost_infeasible(calibration) is True
    runner._annotate_calibration_only_failures(reports, calibration)

    primary = reports["PRIMARY"]["adaptive_failure_memory"]
    side = reports["side"]["adaptive_failure_memory"]
    assert primary["classification"] == "CALIBRATION_COST_INFEASIBLE"
    assert primary["observations"] == 262_042
    assert primary["minimum_observed_entry_hurdle_bps"] == 23.0
    assert primary["maximum_calibrated_predicted_markout_bps"] == 3.9757
    assert primary["raw_data_absence_inferred"] is False
    assert primary["promotion_authority"] is False
    assert side["classification"] == "CALIBRATION_DIRECTION_NON_POSITIVE"
    assert "NO_EXECUTABLE_OBSERVATIONS" in reports["PRIMARY"]["failed_criteria"]
    assert "CALIBRATION_COST_INFEASIBLE" in reports["PRIMARY"]["failed_criteria"]


def test_mixed_cost_feasibility_does_not_skip_shared_replay():
    calibration = {
        "PRIMARY": {
            "status": "NO_COST_FEASIBLE_ENTRY",
            "coefficient_policy": "STRUCTURE_ONLY",
            "cost_feasible_entry_possible": False,
        },
        "side": {
            "status": "PASS",
            "coefficient_policy": "STRUCTURE_ONLY",
            "cost_feasible_entry_possible": True,
        },
    }
    assert runner._all_symbolic_candidates_cost_infeasible(calibration) is False


def test_empty_or_insufficient_calibration_is_not_an_economic_failure():
    for observations in (0, 25):
        calibration = {"PRIMARY": {
            "status": "INSUFFICIENT_CALIBRATION",
            "coefficient_policy": "STRUCTURE_ONLY",
            "cost_feasible_entry_possible": False,
            "observations": observations,
        }}
        assert runner._all_symbolic_candidates_cost_infeasible(
            calibration) is False


def test_search_objectives_keep_measured_zero_but_never_impute_missing():
    measured = runner._search_objective_payload({
        "summary": {
            "sessions": 6,
            "opportunities": 100,
            "mean_net_bps_per_opportunity": 0.0,
            "sharpe": 0.0,
            "instrument_coverage": 0.0,
            "positive_fold_ratio": 0.0,
        },
        "search_structural_novelty": 0.0,
        "complexity_nodes": 3,
    })
    missing = runner._search_objective_payload({
        "summary": {"sessions": 6, "opportunities": 100},
        "complexity_nodes": 3,
    })

    assert measured["complete"] is True
    assert all(value == 0.0 for key, value in measured["values"].items()
               if key != "complexity_nodes")
    assert missing["complete"] is False
    assert "cost_net_bps" in missing["missing"]
    assert missing["imputation"] == "NONE"


def test_external_empty_cell_has_same_explicit_zero_identity():
    first = runner._external_raw_replay_row(
        runner.date(2026, 8, 14), "005930", {})
    second = runner._external_raw_replay_row(
        runner.date(2026, 8, 14), "005930", {
            "quotes": {"row_count": 0}, "ticks": {"row_count": 0}})
    assert first == second
    assert first["quote_rows"] == 0
    assert len(first["source_content_fingerprint"]) == 64


def test_full_replay_hashes_both_calibration_and_evaluation_raw_reads():
    source = inspect.getsource(runner.run)
    assert source.count("raw_content_evidence=") >= 2
    assert source.count("raw_replay_rows.extend") >= 2
    assert "consumed_replay_content_fingerprint" in source
    assert runner._external_content_end(date(2026, 8, 14)).isoformat() == \
        "2026-08-14T06:30:00+00:00"
