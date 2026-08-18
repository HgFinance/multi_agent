from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
FACTORY = ROOT / "departments" / "01-research" / "factory"
for path in (PIPELINE, CONTRACTS, FACTORY):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import alpha_semantics as semantics
import data_resolution as data_resolution_module
import formula_discovery
import intraday_alpha_ast as intraday_grammar
import intraday_candidate as candidate_module
import intraday_supervised as supervised_module
import intraday_experience
import intraday_ablation
import lead_intake
from factory_contracts import MethodologyLeadV1
from intraday_alpha_ast import (IntradayExprError, evaluate, fields_of, parse,
                                shape_fingerprint, unit_of)
from intraday_candidate import (_CapacityReservoir, CandidateAccumulator,
                                 CandidatePopulationAccumulator,
                                 _paired_increment, apply_family_pbo,
                                 evaluate_candidate)
from factory_bridge import (_SQL_EXACT_SCREENING_EXPOSURES,
                            _SQL_FAMILY_OR_EXACT_TRIALS, _normalized_formula,
                            count_family_trials, count_screening_exposures,
                            expected_edge_for, gate0, to_hypothesis_row)
from factory_bridge import lessons_from
from intraday_experiment_runner import (StaleIntradayCohortError,
                                         FAST_SCREEN_VERSION,
                                         _annotate_population,
                                         _all_symbolic_candidates_cost_infeasible,
                                         _apply_population_dsr_gate,
                                         _assert_dataset_manifest_projection,
                                         _candidate_accumulators,
                                         _external_content_end,
                                         _external_replay_manifest,
                                         _input_hash,
                                         _population_multiple_testing,
                                         _fast_screen_gate, _stratified,
                                        _lineage, _load_completed_report,
                                        _replay_load_end, _session_bounds,
                                        _stable_dataset_cutoff, config_from_edge,
                                        record_data_feasibility,
                                        run as run_intraday, select_slice)
from intraday_microstructure import (COMPLETED_SECOND_POLICY, HorizonLabel,
                                      IntradayLaneSpec, IntradaySample,
                                      EXTERNAL_EVENT_SOURCE,
                                      effective_purge_gap)
from trial_family import family_id, hypothesis_view
from strategy_lifecycle import evaluate_promotion


def _sample(instrument: str, at: datetime, *, signal: float,
            net: float) -> IntradaySample:
    label = HorizonLabel(
        horizon_seconds=5,
        exit_time=at + timedelta(seconds=5),
        future_mid=100.01,
        long_mid_markout_bps=net + 1.0,
        short_mid_markout_bps=-(net + 1.0),
        long_taker_net_bps=net,
        short_taker_net_bps=-net - 2.0,
        long_passive_filled=True,
        short_passive_filled=True,
        long_passive_fill_time=at + timedelta(seconds=1),
        short_passive_fill_time=at + timedelta(seconds=1),
        long_passive_net_bps=net + 1.0,
        short_passive_net_bps=-net - 1.0,
    )
    return IntradaySample(
        instrument_id=instrument, decision_time=at, entry_time=at,
        source_quote_event_time=at, quote_age_ms=0.0, spread_bps=2.0,
        queue_imbalance_l1=signal, queue_imbalance_l10=signal * .8,
        microprice_offset_bps=signal, trade_flow_imbalance=signal,
        quote_event_ofi=signal * 100.0, normalized_quote_ofi=signal,
        bid_depth_l1=100.0, ask_depth_l1=100.0, book_depth_l1=200.0,
        book_depth_l10=2000.0, trade_count=10, quote_count=20,
        trade_intensity=2.0, realized_volatility_bps=1.0,
        entry_bid_depth_l1=100.0, entry_ask_depth_l1=100.0,
        entry_bid=99.99, entry_ask=100.01, entry_mid=100.0,
        labels=(label,),
    )


def test_semantic_plan_is_typed_and_excludes_tuning_from_family() -> None:
    plan = {
        "event": "ORDER_FLOW", "context": ["TIGHT_SPREAD"],
        "qualities": ["PERSISTENCE", "STATE_CONDITIONAL"],
        "direction": "FOLLOW", "output": "TAKER_NET_PNL",
        "execution": "TAKER", "horizon_seconds": 5,
    }
    later = {**plan, "horizon_seconds": 30}
    assert semantics.lane_of(plan) == "INTRADAY_EVENT"
    assert semantics.fingerprint(plan) == semantics.fingerprint(later)
    assert semantics.check_observables(
        plan, {"trade_flow_imbalance", "spread_bps"},
        operators={"rolling_mean", "where"},
        conditional_fields={"spread_bps"})["ok"]
    assert not semantics.check_observables(plan, {"microprice_offset_bps"})["ok"]

    with pytest.raises(ValueError, match="no executable intraday observable"):
        semantics.validate({**plan, "event": "CROSS_ASSET_FLOW"})


def test_intraday_ast_enforces_units_and_explicit_clocks() -> None:
    expr = {
        "op": "sub",
        "args": [
            {"op": "field", "field": "trade_flow_imbalance"},
            {"op": "rolling_mean", "seconds": 30,
             "arg": {"op": "field", "field": "trade_flow_imbalance"}},
        ],
    }
    assert unit_of(expr) == "RATIO"
    assert fields_of(expr) == {"trade_flow_imbalance"}
    assert shape_fingerprint(expr) == shape_fingerprint({
        **expr, "args": [expr["args"][0], {**expr["args"][1], "seconds": 60}]})

    with pytest.raises(IntradayExprError, match="incompatible units"):
        parse({"op": "add", "args": [
            {"op": "field", "field": "spread_bps"},
            {"op": "field", "field": "book_depth_l1"},
        ]})


def test_structural_ablations_are_deterministic_simpler_bps_controls() -> None:
    expr = _intraday_proposal()["suggested_params"]["intraday_signal_expr"]
    controls = intraday_ablation.generate(expr)
    assert controls == intraday_ablation.generate(expr)
    assert controls[0]["ablation_operator"] == "REMOVE_STATE_GATE_KEEP_THEN"
    assert any(row["ablation_operator"] == "REMOVE_RATIO_MODULATOR_LEFT"
               for row in controls)
    assert all(intraday_grammar.unit_of(row["intraday_signal_expr"]) == "BPS"
               for row in controls)
    assert all(intraday_grammar.count_nodes(row["intraday_signal_expr"])
               < intraday_grammar.count_nodes(expr) for row in controls)

    ratio_expr = {"op": "sub", "args": [
        {"op": "field", "field": "queue_imbalance_l1"},
        {"op": "field", "field": "queue_imbalance_l10"},
    ]}
    ratio_controls = intraday_ablation.generate(ratio_expr)
    assert ratio_controls
    assert all(intraday_grammar.unit_of(row["intraday_signal_expr"]) == "RATIO"
               for row in ratio_controls)


def test_temporal_ast_never_reads_future_samples() -> None:
    base = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)
    rows = [_sample("A", base + timedelta(seconds=i * 10), signal=float(i), net=1)
            for i in range(4)]
    expr = {"op": "delta", "seconds": 20,
            "arg": {"op": "field", "field": "trade_flow_imbalance"}}
    before = evaluate(rows, expr)
    rows[-1] = _sample("A", rows[-1].decision_time, signal=999.0, net=1)
    after = evaluate(rows, expr)
    assert before[:3] == after[:3]
    assert before[-1] != after[-1]


def test_candidate_requires_measured_trial_dispersion_before_qa_submission() -> None:
    base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    by_instrument = {"A": [], "B": []}
    for day in range(70):
        for instrument in by_instrument:
            for offset in range(2):
                # Always-positive but non-constant session P&L gives a defined DSR.
                net = 0.6 + (day % 5) * 0.1 + offset * 0.05
                by_instrument[instrument].append(_sample(
                    instrument, base + timedelta(days=day, seconds=offset * 10),
                    signal=1.0, net=net))
    expr = {"op": "field", "field": "trade_flow_imbalance"}
    spec = IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0)
    report = evaluate_candidate(
        by_instrument, expr=expr, spec=spec, horizon_seconds=5,
        execution="TAKER", trials=4, family_pbo=0.2)
    assert report["decision"] == "HOLD"
    assert report["summary"]["sessions"] == 70
    assert report["summary"]["opportunities"] == 280
    assert report["summary"]["mean_implementation_drag_bps"] == pytest.approx(1.0)
    assert report["failed_criteria"] == [
        "DSR_TRIAL_DISPERSION_UNMEASURED"]
    assert report["summary"]["dsr_calibration_mode"] == \
        "legacy_unit_trial_sharpe_std"
    assert report["summary"]["dsr_trial_sharpe_std"] == 1.0
    assert report["summary"]["dsr_effective_trials"] == 4.0
    assert report["summary"]["dsr_expected_max_sharpe"] is not None
    assert report["summary"]["trial_sharpe_std"] == 1.0
    assert report["summary"]["effective_trials"] == 4.0
    assert report["summary"]["expected_max_sharpe"] is not None
    assert "not_a_promotion" in report

    short = evaluate_candidate(
        {"A": by_instrument["A"][:2]}, expr=expr, spec=spec,
        horizon_seconds=5, execution="TAKER", trials=1, family_pbo=None)
    assert short["decision"] == "HOLD"
    assert "SESSIONS_BELOW_MINIMUM" in short["failed_criteria"]
    assert "PBO_UNMEASURED" in short["failed_criteria"]


def test_primary_session_mean_gate_uses_stationary_bootstrap_contract() -> None:
    base = datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc)
    samples = {
        instrument: [
            _sample(
                instrument,
                base + timedelta(days=day),
                signal=1.0,
                net=0.5 + (day % 3) * 0.25,
            )
            for day in range(8)
        ]
        for instrument in ("A", "B")
    }
    report = evaluate_candidate(
        samples,
        expr={"op": "field", "field": "trade_flow_imbalance"},
        spec=IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0),
        horizon_seconds=5,
        execution="TAKER",
        family_pbo=0.2,
        criteria={
            "min_sessions": 1,
            "min_instruments": 1,
            "min_opportunities": 1,
            "min_deflated_sharpe": -100,
            "min_positive_session_ratio": 0,
        },
    )

    summary = report["summary"]
    assert summary["session_net_ci_method"] == "stationary"
    assert summary["session_net_ci_restart_probability"] == 0.25
    assert summary["session_net_ci_expected_block_length_sessions"] == 4.0
    assert summary["session_net_ci_n_boot"] == 1_000
    assert summary["session_net_ci_seed"] == 20260816
    assert summary["session_net_ci_low_bps"] is not None
    assert summary["session_net_ci_high_bps"] is not None


def test_structure_only_score_is_scaled_on_prior_sessions_then_frozen() -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    spec = IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0,
                            fee_bps_per_side=11.5)
    accumulator = CandidateAccumulator(
        expr={"op": "field", "field": "normalized_quote_ofi"},
        spec=spec, horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        coefficient_policy="STRUCTURE_ONLY", family_pbo=0.2,
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1})
    for instrument in ("A", "B"):
        calibration = [
            _sample(instrument, base + timedelta(seconds=index),
                    signal=1.0, net=39.0)
            for index in range(500)
        ]
        accumulator.calibrate(instrument, calibration)
    frozen = accumulator.freeze_calibration()
    assert frozen["status"] == "PASS"
    assert frozen["observations"] == 1_000
    assert frozen["instruments"] == 2
    assert frozen["beta_bps_per_score_unit"] == pytest.approx(40.0 / 1.1)

    accumulator.add("A", [_sample(
        "A", base + timedelta(days=1), signal=1.0, net=5.0)])
    report = accumulator.finish()
    assert report["summary"]["opportunities"] == 1
    calibration_report = report["lane_manifest"]["score_calibration"]
    assert calibration_report["status"] == "PASS"
    assert calibration_report["oos_fit_forbidden"] is True


def test_calibration_proves_bounded_formula_cannot_clear_stock_costs() -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    accumulator = CandidateAccumulator(
        expr={"op": "field", "field": "normalized_quote_ofi"},
        spec=IntradayLaneSpec(
            horizons_seconds=(5,), order_latency_ms=0,
            fee_bps_per_side=11.5),
        horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        coefficient_policy="STRUCTURE_ONLY")
    for instrument in ("A", "B"):
        accumulator.calibrate(instrument, [
            _sample(instrument, base + timedelta(seconds=index),
                    signal=1.0, net=1.0)
            for index in range(500)
        ])

    frozen = accumulator.freeze_calibration()

    assert frozen["status"] == "NO_COST_FEASIBLE_ENTRY"
    assert frozen["maximum_positive_raw_score"] == 1.0
    assert frozen["maximum_calibrated_predicted_markout_bps"] < 2.0
    assert frozen["minimum_observed_entry_hurdle_bps"] == pytest.approx(25.0)
    assert frozen["cost_feasible_entry_possible"] is False
    assert frozen["cost_feasibility_proof"] == \
        "MAX_CALIBRATED_PREDICTION_NOT_ABOVE_MINIMUM_HURDLE"
    assert _all_symbolic_candidates_cost_infeasible({
        "PRIMARY": frozen,
        "LINKED": {
            "status": "NON_POSITIVE_DIRECTIONAL_RELATION",
            "coefficient_policy": "STRUCTURE_ONLY",
            "cost_feasible_entry_possible": False,
            "observations": 1_000,
        },
    }) is True
    assert _all_symbolic_candidates_cost_infeasible({
        "PRIMARY": frozen,
        "LINKED": {
            "status": "PASS",
            "coefficient_policy": "STRUCTURE_ONLY",
            "cost_feasible_entry_possible": True,
        },
    }) is False
    assert _all_symbolic_candidates_cost_infeasible({
        "PRIMARY": frozen,
        "FIXED": {
            "status": "NOT_REQUIRED_FIXED_EQUATION",
            "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
            "cost_feasible_entry_possible": False,
        },
    }) is False


def test_forward_restore_reuses_structure_beta_and_schedules_empty_stock_days(
        ) -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    spec = IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0,
                            fee_bps_per_side=11.5)
    source = CandidateAccumulator(
        expr={"op": "field", "field": "normalized_quote_ofi"},
        spec=spec, horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        coefficient_policy="STRUCTURE_ONLY")
    for instrument in ("A", "B"):
        source.calibrate(instrument, [
            _sample(instrument, base + timedelta(seconds=index),
                    signal=1.0, net=39.0)
            for index in range(500)
        ])
    artifact = source.freeze_calibration()

    forward = CandidateAccumulator(
        expr={"op": "field", "field": "normalized_quote_ofi"},
        spec=spec, horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        coefficient_policy="STRUCTURE_ONLY")
    restored = forward.restore_frozen_calibration(
        artifact, artifact["supervised_control"])
    assert restored["restored_without_refit"] is True
    assert restored["beta_bps_per_score_unit"] == artifact[
        "beta_bps_per_score_unit"]
    assert restored["supervised_control"]["model_fingerprint"] == artifact[
        "supervised_control"]["model_fingerprint"]
    with pytest.raises(ValueError, match="already frozen"):
        forward.calibrate("A", [_sample("A", base, signal=1.0, net=1.0)])

    sessions = [(base + timedelta(days=index)).date().isoformat()
                for index in range(20)]
    forward.schedule_sessions(sessions)
    # Both exact-stock members remain requested even with no raw samples.
    forward.add("A", [])
    forward.add("B", [])
    report = forward.finish()
    assert report["summary"]["sessions"] == 20
    assert report["summary"]["instruments_requested"] == 2
    assert report["summary"]["instruments_with_samples"] == 0
    assert set(report["session_returns_bps"]) == set(sessions)


def test_structure_only_negative_relation_is_not_silently_flipped() -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    accumulator = CandidateAccumulator(
        expr={"op": "field", "field": "normalized_quote_ofi"},
        spec=IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0),
        horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        coefficient_policy="STRUCTURE_ONLY")
    for instrument in ("A", "B"):
        accumulator.calibrate(instrument, [
            _sample(instrument, base + timedelta(seconds=index),
                    signal=1.0, net=-41.0)
            for index in range(500)
        ])
    frozen = accumulator.freeze_calibration()
    assert frozen["status"] == "NON_POSITIVE_DIRECTIONAL_RELATION"
    assert frozen["beta_bps_per_score_unit"] == 0.0


def test_population_calibration_requirement_clears_after_ast_and_teacher_freeze(
        ) -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    candidate = CandidateAccumulator(
        expr={"op": "field", "field": "normalized_quote_ofi"},
        spec=IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0),
        horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        coefficient_policy="STRUCTURE_ONLY")
    population = CandidatePopulationAccumulator({"PRIMARY": candidate})
    assert population.requires_calibration is True
    for instrument in ("A", "B"):
        population.calibrate(instrument, [
            _sample(instrument, base + timedelta(seconds=index),
                    signal=1.0, net=39.0)
            for index in range(500)
        ])
    frozen = population.freeze_calibration()["PRIMARY"]
    assert frozen["status"] == "PASS"
    assert frozen["supervised_control"]["status"] == "PASS"
    assert population.requires_calibration is False


def test_no_trade_scheduled_session_is_zero_and_failed_teacher_pairs_are_invalid(
        ) -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    accumulator = CandidateAccumulator(
        expr={"op": "field", "field": "microprice_offset_bps"},
        spec=IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0),
        horizon_seconds=5, execution="TAKER", family_pbo=0.2,
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1, "min_deflated_sharpe": -100,
                  "min_positive_session_ratio": 0})
    frozen = accumulator.freeze_calibration()
    assert frozen["supervised_control"]["status"] == \
        "INSUFFICIENT_CALIBRATION"
    accumulator.add("A", [
        _sample("A", base, signal=1.0, net=2.0),
        _sample("A", base + timedelta(days=1), signal=-1.0, net=99.0),
    ])
    report = accumulator.finish()
    sessions = sorted(report["session_returns_bps"])
    assert sessions == ["2026-08-12", "2026-08-13"]
    assert report["session_returns_bps"]["2026-08-13"] == 0.0
    assert report["summary"]["sessions"] == 2
    assert report["summary"]["session_mean_net_bps"] == pytest.approx(1.0)
    assert report["supervised_control"]["strategy"]["summary"]["sessions"] == 2
    assert report["hybrid_control"]["strategy"]["summary"]["sessions"] == 2
    assert report["supervised_control"]["increment_vs_ast"]["status"] == \
        "INVALID"
    assert report["hybrid_control"]["increment_vs_ast"]["status"] == \
        "INVALID"
    assert report["hybrid_control"]["increment_vs_ast"][
        "promotion_authority"] is False


def test_passive_capital_is_reserved_until_fill_plus_horizon() -> None:
    base = datetime(2026, 8, 12, 0, 0, tzinfo=timezone.utc)
    first = _sample("A", base, signal=1.0, net=2.0)
    first_label = replace(
        first.labels[0],
        long_passive_fill_time=base + timedelta(seconds=4),
        long_passive_exit_time=base + timedelta(seconds=9))
    first = replace(first, labels=(first_label,))
    second = _sample(
        "A", base + timedelta(seconds=6), signal=1.0, net=2.0)
    second_label = replace(
        second.labels[0],
        long_passive_fill_time=base + timedelta(seconds=7),
        long_passive_exit_time=base + timedelta(seconds=12))
    second = replace(second, labels=(second_label,))

    accumulator = CandidateAccumulator(
        expr={"op": "field", "field": "microprice_offset_bps"},
        spec=IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0),
        horizon_seconds=5, execution="PASSIVE_FIFO_LOWER_BOUND",
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1})
    accumulator.freeze_calibration()
    accumulator.add("A", [first, second])
    report = accumulator.finish()

    # The first order is still holding at t=6 (fill t=4, exit t=9), so the
    # second decision overlaps it.  decision+horizon would incorrectly say 1.
    assert report["summary"]["max_concurrent_opportunities"] == 2
    assert "PASSIVE_EXPIRY" in report["lane_manifest"][
        "portfolio_capital_model"]


def test_paired_comparison_uses_common_capital_and_stationary_sessions() -> None:
    paired = _paired_increment(
        {"s1": 4.0, "s2": 0.0, "s3": 2.0},
        {"s1": 0.0, "s2": 2.0, "s3": 0.0},
        sessions=["s1", "s2", "s3"],
        common_denominators={"s1": 2, "s2": 2, "s3": 4})
    assert paired["status"] == "PASS"
    assert paired["sessions"] == 3
    assert paired["mean_delta_bps"] == pytest.approx(0.5)
    assert paired["bootstrap_method"] == "stationary"
    assert paired["common_denominator"].startswith("MAX_CONCURRENT")
    assert paired["promotion_authority"] is False


def test_search_exposed_history_cannot_be_submitted_before_forward_confirmation(
        ) -> None:
    historical = {
        "decision": "SUBMIT_TO_QA",
        "summary": {},
        "failed_criteria": [],
        "evidence_tier": "SEARCH_EXPOSED_HISTORICAL_SUPPORT",
        "forward_lockbox": {"independent_confirmation": False},
    }
    gated = apply_family_pbo(historical, 0.2)
    assert gated["decision"] == "HOLD"
    assert "INDEPENDENT_FORWARD_CONFIRMATION_PENDING" in gated[
        "failed_criteria"]
    assert gated["forward_nomination"]["decision"] == "NOMINATE_FORWARD"
    assert gated["forward_nomination"]["promotion_authority"] is False
    assert apply_family_pbo(gated, 0.2)["failed_criteria"].count(
        "INDEPENDENT_FORWARD_CONFIRMATION_PENDING") == 1

    independently_confirmed = {
        **gated,
        "evidence_tier": "INDEPENDENT_FORWARD_CONFIRMATION",
        "forward_lockbox": {"independent_confirmation": True},
    }
    confirmed = apply_family_pbo(independently_confirmed, 0.2)
    assert confirmed["decision"] == "SUBMIT_TO_QA"
    assert "INDEPENDENT_FORWARD_CONFIRMATION_PENDING" not in confirmed[
        "failed_criteria"]
    assert "forward_nomination" not in confirmed


def test_semantic_time_context_filters_observations_not_ast_history() -> None:
    # 00:00 UTC is 09:00 KST (OPEN); 03:00 UTC is noon (MIDDAY).
    base = datetime(2026, 8, 14, 0, 0, tzinfo=timezone.utc)
    rows = [_sample("A", base, signal=1.0, net=1.0),
            _sample("A", base + timedelta(hours=3), signal=1.0, net=-5.0)]
    report = evaluate_candidate(
        {"A": rows}, expr={"op": "field", "field": "trade_flow_imbalance"},
        spec=IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0),
        horizon_seconds=5, execution="TAKER", family_pbo=0.2,
        semantic_plan={"context": ["OPEN"]},
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1, "min_deflated_sharpe": -100,
                  "min_positive_session_ratio": 0})
    assert report["summary"]["opportunities"] == 1
    assert report["summary"]["mean_net_bps_per_opportunity"] == 1.0


def _intraday_proposal() -> dict:
    plan = {
        "event": "ORDER_FLOW", "context": ["TIGHT_SPREAD"],
        "qualities": ["PERSISTENCE", "STATE_CONDITIONAL"],
        "direction": "FOLLOW", "output": "TAKER_NET_PNL",
        "execution": "TAKER", "horizon_seconds": 5,
    }
    flow = {"op": "sub", "args": [
        {"op": "field", "field": "trade_flow_imbalance"},
        {"op": "rolling_mean", "seconds": 30,
         "arg": {"op": "field", "field": "trade_flow_imbalance"}},
    ]}
    predicted_markout = {"op": "mul", "args": [
        flow, {"op": "field", "field": "realized_volatility_bps"}]}
    expr = {"op": "where",
            "condition": {"op": "lt", "args": [
                {"op": "field", "field": "spread_bps"},
                {"const": 5, "unit": "BPS"}]},
            "then": predicted_markout,
            "else": {"const": 0, "unit": "BPS"}}
    return {
        "edge_type": "order_flow_imbalance", "universe_key": "krx_all",
        "research_lane": "INTRADAY_EVENT", "semantic_plan": plan,
        "economic_rationale": "persistent aggressive flow moves the next quote",
        "counterparty": "urgent liquidity takers", "competing_explanation": "spread",
        "falsification_tests": ["net markout <= 0"], "trial_budget": 5,
        "data_requirements": {"tables": ["market_quotes", "market_ticks"],
                              "min_history_days": 60},
        "suggested_params": {
            "intraday_signal_expr": expr, "horizon_seconds": 5,
            "sample_interval_seconds": 5, "feature_lookback_seconds": 30,
            "order_latency_ms": 250, "execution": "TAKER",
            "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
            "minimum_predicted_edge_bps": 1.0,
            "evaluation_days": 60, "instrument_shard_size": 32,
        },
    }


def test_gate0_routes_intraday_contract_without_daily_binding() -> None:
    proposal = _intraday_proposal()
    gate = gate0(proposal)
    assert gate.ok, gate.as_dict()
    edge, dropped = expected_edge_for(proposal)
    assert not dropped
    assert edge["research_lane"] == "INTRADAY_EVENT"
    assert edge["semantic_fingerprint"] == semantics.fingerprint(
        proposal["semantic_plan"])
    config, spec = config_from_edge(edge)
    assert config["horizon_seconds"] == 5
    assert config["fee_bps_per_side"] == 11.5
    assert config["position_mode"] == "LONG_ONLY"
    assert config["universe_mode"] == "ALL_CAUSALLY_COLLECTED"
    assert config["data_source"] == "AUTO"
    assert config["fast_screen_enabled"] is True
    assert config["fast_screen_sessions"] == 6
    assert config["fast_screen_instruments"] == 16
    assert config["fast_screen_hard_net_floor_enabled"] is False
    assert config["instrument_shard_size"] == 32
    assert spec.purge_gap == timedelta(milliseconds=10250)

    explicit_screen = _intraday_proposal()
    explicit_screen["suggested_params"]["fast_screen_min_net_bps"] = 1.5
    explicit_gate = gate0(explicit_screen)
    assert explicit_gate.ok, explicit_gate.as_dict()
    explicit_edge, dropped = expected_edge_for(explicit_screen)
    assert "fast_screen_min_net_bps" not in dropped
    explicit_config, _ = config_from_edge(explicit_edge)
    assert explicit_config["fast_screen_hard_net_floor_enabled"] is True
    assert explicit_config["fast_screen_min_net_bps"] == pytest.approx(1.5)

    proposal["suggested_params"]["fee_bps_per_side"] = 0
    gate = gate0(proposal)
    assert not gate.ok and "INTRADAY_CONTRACT_INVALID" in gate.codes


def test_runner_revalidates_resolver_dataset_source_and_clock_contract() -> None:
    edge, _ = expected_edge_for(_intraday_proposal())
    contract = data_resolution_module._copy_authority_contract(
        data_resolution_module.EXTERNAL_INTRADAY_DATASET)
    edge["data_source"] = EXTERNAL_EVENT_SOURCE
    edge["resolved_data_contract"] = contract

    config, _spec = config_from_edge(edge)
    assert config["resolved_data_contract"] == contract

    drifted = {**edge, "resolved_data_contract": {
        **contract, "timestamp_policy": "STRICT_RECEIPT_ORDER_V1"}}
    with pytest.raises(RuntimeError, match="contract mismatch"):
        config_from_edge(drifted)

    wrong_dataset = {**edge, "resolved_data_contract": {
        **contract, "dataset": data_resolution_module.LIVE_INTRADAY_DATASET}}
    with pytest.raises(RuntimeError, match="contract mismatch"):
        config_from_edge(wrong_dataset)


def test_registration_revalidates_external_manifest_catalog_projection() -> None:
    contract = data_resolution_module._copy_authority_contract(
        data_resolution_module.EXTERNAL_INTRADAY_DATASET)
    pit = {
        "knowledge_clock": "event_time_only_no_receipt_clock",
        "feature_cutoff": "completed_source_second<=decision_time",
        "label_cutoff": "effective_entry_time+horizon",
        "instrument_isolation": True,
        "evidence_scope": "HISTORICAL_SEARCH_ONLY",
        "content_window": "[09:00:00,15:30:00) Asia/Seoul",
        "maximum_horizon_seconds": 600,
    }
    quality = {
        "status": "HISTORICAL_COMPLETED_SECOND_REQUIRES_PER_EXPERIMENT_AUDIT",
        "timestamp_resolution": "SECOND",
        "intra_second_order": "UNAVAILABLE",
        "execution": "TAKER_ONLY",
    }
    schema = {
        "market_quotes": {
            "physical_table": "ext_src.quotes",
            "required": ["ts", "symbol", "bid1", "ask1", "bid_vol1",
                         "ask_vol1", "bid10", "ask10", "bid_vol10",
                         "ask_vol10"],
        },
        "market_ticks": {
            "physical_table": "ext_src.ticks",
            "required": ["ts", "symbol", "price", "volume", "ofi_contrib"],
        },
    }
    row = (
        "dataset-id", contract["source_versions"], pit, quality,
        "postgresql+fdw://ext_src/{quotes,ticks}", schema)

    assert _assert_dataset_manifest_projection(row, contract) == "dataset-id"
    with pytest.raises(RuntimeError, match="manifest drift"):
        _assert_dataset_manifest_projection(
            (*row[:2], {**pit, "content_window": "[09:00,15:25)"},
             *row[3:]), contract)

    proposal = _intraday_proposal()
    proposal["suggested_params"]["position_mode"] = "LONG_SHORT"
    gate = gate0(proposal)
    assert not gate.ok and "INTRADAY_CONTRACT_INVALID" in gate.codes


def test_screening_population_unifies_clocks_horizons_and_execution_labels() -> None:
    proposal = _intraday_proposal()
    primary_baseline = {"op": "field", "field": "spread_bps"}
    proposal["suggested_params"][
        "source_baseline_expr"] = primary_baseline
    edge, dropped = expected_edge_for(proposal)
    assert "source_baseline_expr" not in dropped
    passive_plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "REVERT",
        "output": "PASSIVE_FILL_ADJUSTED_PNL",
        "execution": "PASSIVE_FIFO_LOWER_BOUND", "horizon_seconds": 30,
    }
    sidecar_expr = {"op": "rolling_mean", "seconds": 90,
                    "arg": {"op": "field",
                            "field": "microprice_offset_bps"}}
    edge["screening_population"] = [{
        "candidate_role": "LINKED_CANDIDATE",
        "source_lead_ids": ["lead-passive"],
        "ast_fingerprint": candidate_module.fingerprint(sidecar_expr),
        "intraday_signal_expr": sidecar_expr,
        "source_baseline_expr": sidecar_expr,
        "semantic_plan": passive_plan,
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
    }]
    edge["screening_cohort_version"] = "intraday-screening-cohort-v4"

    config, spec = config_from_edge(edge)
    assert spec.horizons_seconds == (5, 30)
    assert spec.feature_lookback_seconds == 90
    assert config["population_execution_model"] == "PASSIVE_FIFO_LOWER_BOUND"
    assert config["screening_trial_exposure"] == 1
    assert config["screening_population"][0]["screening_only"] is True
    assert config["source_baseline_expr"] == primary_baseline
    assert config["screening_population"][0][
        "source_baseline_expr"] == sidecar_expr


def test_runner_accepts_structure_only_score_but_not_unscaled_fixed_score() -> None:
    edge, _ = expected_edge_for(_intraday_proposal())
    edge["intraday_signal_expr"] = {
        "op": "rolling_mean", "seconds": 30,
        "arg": {"op": "field", "field": "normalized_quote_ofi"},
    }
    edge["coefficient_policy"] = "STRUCTURE_ONLY"
    config, _ = config_from_edge(edge)
    assert config["coefficient_policy"] == "STRUCTURE_ONLY"
    assert intraday_grammar.unit_of(config["intraday_signal_expr"]) == "RATIO"

    edge["coefficient_policy"] = "PREREGISTERED_NO_OOS_FIT"
    with pytest.raises(ValueError, match="must predict BPS"):
        config_from_edge(edge)


def test_stale_populated_screening_cohort_is_rejected_before_replay() -> None:
    edge, _ = expected_edge_for(_intraday_proposal())
    edge["screening_population"] = [{
        "title": "stale directionless sidecar",
        "semantic_plan": edge["semantic_plan"],
        "intraday_signal_expr": {
            "op": "field", "field": "realized_volatility_bps"},
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
    }]
    edge["screening_cohort_version"] = "intraday-screening-cohort-v1"
    with pytest.raises(StaleIntradayCohortError,
                       match="intraday-screening-cohort-v4"):
        config_from_edge(edge)


def test_intraday_family_is_semantic_not_numeric_tuning() -> None:
    proposal = _intraday_proposal()
    first, _ = expected_edge_for(proposal)
    later = _intraday_proposal()
    later["semantic_plan"] = {**later["semantic_plan"], "horizon_seconds": 30}
    later["suggested_params"] = {**later["suggested_params"],
                                 "horizon_seconds": 30}
    # Constants and clocks are tuning dimensions: changing them preserves the
    # equation structure and therefore the multiple-testing family.
    later_expr = later["suggested_params"]["intraday_signal_expr"]
    later_expr["condition"]["args"][1]["const"] = 7
    later_expr["then"]["args"][0]["args"][1]["seconds"] = 90
    second, _ = expected_edge_for(later)
    a = hypothesis_view(edge_type=first["type"], universe_key=first["universe_key"],
                        research_lane=first["research_lane"],
                        semantic_fingerprint=first["semantic_fingerprint"],
                        intraday_signal_expr=first["intraday_signal_expr"])
    b = hypothesis_view(edge_type=second["type"], universe_key=second["universe_key"],
                        research_lane=second["research_lane"],
                        semantic_fingerprint=second["semantic_fingerprint"],
                        intraday_signal_expr=second["intraday_signal_expr"])
    assert family_id(a) == family_id(b)
    assert _normalized_formula(second["intraday_signal_expr"]) is not None

    different = _intraday_proposal()
    different["suggested_params"]["intraday_signal_expr"] = {
        "op": "mul", "args": [
            {"op": "field", "field": "normalized_quote_ofi"},
            {"op": "field", "field": "trade_flow_imbalance"},
        ]}
    third, _ = expected_edge_for(different)
    c = hypothesis_view(
        edge_type=third["type"], universe_key=third["universe_key"],
        research_lane=third["research_lane"],
        semantic_fingerprint=third["semantic_fingerprint"],
        intraday_signal_expr=third["intraday_signal_expr"])
    assert family_id(a) != family_id(c)


def test_llm_formula_thesis_is_typed_and_visible_in_ast() -> None:
    expr = _intraday_proposal()["suggested_params"]["intraday_signal_expr"]
    plan = _intraday_proposal()["semantic_plan"]
    thesis = {
        "target": "TAKER_NET_PNL",
        "functional_form": "STATE_CONDITIONAL",
        "expected_sign": "POSITIVE",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {"spread_bps": "LIQUIDITY",
                  "trade_flow_imbalance": "PRESSURE",
                  "realized_volatility_bps": "VOLATILITY"},
        "identification": (
            "Persistent aggressive flow must predict positive net markout only "
            "when the spread state is tight."),
    }
    result = formula_discovery.assess(
        thesis, candidate=expr, semantic_plan=plan,
        grammar=lead_intake._intraday_ast())
    assert result["formula_contract_complete"]
    assert result["formula_discovery_version"] == "formula-discovery-v5"
    assert result["formula_math_profile"]["directional_pressure_fields"] == [
        "trade_flow_imbalance"]
    assert result["formula_math_profile"]["complexity_nodes"] > 1
    assert result["formula_math_profile"]["term_influence"] == {
        "realized_volatility_bps": ["VALUE"],
        "spread_bps": ["GATE"],
        "trade_flow_imbalance": ["VALUE"],
    }

    invalid = {**thesis, "functional_form": "CROSS_SCALE"}
    with pytest.raises(ValueError, match="two distinct clocks"):
        formula_discovery.assess(
            invalid, candidate=expr, semantic_plan=plan,
            grammar=lead_intake._intraday_ast())

    dimensionless = {"op": "field", "field": "trade_flow_imbalance"}
    dimensionless_thesis = {
        **thesis,
        "functional_form": "MONOTONE",
        "terms": {"trade_flow_imbalance": "PRESSURE"},
    }
    with pytest.raises(ValueError, match="must output BPS"):
        formula_discovery.assess(
            dimensionless_thesis, candidate=dimensionless,
            semantic_plan=plan, grammar=lead_intake._intraday_ast())

    structure = formula_discovery.assess(
        {**dimensionless_thesis, "coefficient_policy": "STRUCTURE_ONLY"},
        candidate=dimensionless, semantic_plan=plan,
        grammar=lead_intake._intraday_ast())
    assert structure["formula_math_profile"]["output_unit"] == "RATIO"
    assert structure["formula_math_profile"]["score_calibration"] == \
        "ORIGIN_ANCHORED_POSITIVE_SHRINKAGE_V1"


def test_formula_discovery_rejects_decorative_nonnegative_sign_term() -> None:
    expr = {"op": "mul", "args": [
        {"op": "field", "field": "spread_bps"},
        {"op": "div", "args": [
            {"op": "field", "field": "queue_imbalance_l1"},
            {"op": "sign", "arg": {"op": "field", "field": "trade_count"}},
        ]},
    ]}
    thesis = {
        "target": "TAKER_NET_PNL",
        "functional_form": "INTERACTION",
        "expected_sign": "POSITIVE",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {"spread_bps": "LIQUIDITY",
                  "queue_imbalance_l1": "PRESSURE",
                  "trade_count": "ACTIVITY"},
        "identification": (
            "Depth pressure must beat its flow-only and activity-only controls."),
    }
    with pytest.raises(ValueError, match="presence-only influence"):
        formula_discovery.assess(
            thesis, candidate=expr,
            semantic_plan={"output": "TAKER_NET_PNL", "execution": "TAKER"},
            grammar=lead_intake._intraday_ast())


def test_formula_discovery_rejects_exact_symbolic_degeneracy() -> None:
    base = {"op": "field", "field": "microprice_offset_bps"}
    expr = {"op": "where", "condition": {"op": "lt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"}]}, "then": base, "else": base}
    thesis = {
        "target": "TAKER_NET_PNL",
        "functional_form": "STATE_CONDITIONAL",
        "expected_sign": "STATE_DEPENDENT",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {"spread_bps": "LIQUIDITY",
                  "microprice_offset_bps": "PRESSURE"},
        "identification": "Spread state must change the conditional markout response.",
    }
    with pytest.raises(ValueError, match="identical then/else"):
        formula_discovery.assess(
            thesis, candidate=expr,
            semantic_plan={"output": "TAKER_NET_PNL", "execution": "TAKER"},
            grammar=lead_intake._intraday_ast())


def test_formula_discovery_rejects_magnitude_only_directional_markout() -> None:
    expr = {"op": "where", "condition": {"op": "gt", "args": [
        {"op": "rolling_zscore", "seconds": 300,
         "arg": {"op": "field", "field": "trade_count"}},
        {"const": 2, "unit": "RATIO"}]},
        "then": {"op": "rolling_mean", "seconds": 30,
                 "arg": {"op": "field",
                         "field": "realized_volatility_bps"}},
        "else": {"const": 0, "unit": "BPS"}}
    thesis = {
        "target": "TAKER_NET_PNL", "functional_form": "STATE_CONDITIONAL",
        "expected_sign": "POSITIVE",
        "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
        "terms": {"trade_count": "ACTIVITY",
                  "realized_volatility_bps": "VOLATILITY"},
        "identification": "High activity and volatility predict a positive markout.",
    }
    with pytest.raises(ValueError, match="no signed directional PRESSURE"):
        formula_discovery.assess(
            thesis, candidate=expr,
            semantic_plan={"output": "TAKER_NET_PNL", "execution": "TAKER"},
            grammar=lead_intake._intraday_ast())


def test_lead_intake_persists_formula_discovery_contract() -> None:
    proposal = _intraday_proposal()
    expr = proposal["suggested_params"]["intraday_signal_expr"]
    block = {
        "TITLE": "Typed equation hypothesis",
        "URL": "https://example.com/equation",
        "MECHANISM": "Persistent aggressive flow moves quotes in tight spreads.",
        "READINESS": "AST_READY",
        "OBSERVABLES": "spread_bps,trade_flow_imbalance,realized_volatility_bps",
        "CANDIDATE_SIGNAL_EXPR": expr,
        "RESEARCH_LANE": "INTRADAY_EVENT",
        "SEMANTIC_PLAN": proposal["semantic_plan"],
        "DERIVATION_MODE": "MECHANISM_MUTATION",
        "SOURCE_BASELINE_EXPR": {"op": "field", "field": "trade_flow_imbalance"},
        "DERIVATION_TRANSFORMS": "STATE_CONDITION",
        "NOVELTY_RATIONALE": "Adds an executable liquidity-state interaction.",
        "FORMULA_THESIS": {
            "target": "TAKER_NET_PNL",
            "functional_form": "STATE_CONDITIONAL",
            "expected_sign": "POSITIVE",
            "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
            "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
            "terms": {"spread_bps": "LIQUIDITY",
                      "trade_flow_imbalance": "PRESSURE",
                      "realized_volatility_bps": "VOLATILITY"},
            "identification": "Net markout must be positive only inside the tight-spread state.",
        },
    }
    metadata = lead_intake._readiness_metadata(block, block["MECHANISM"])
    assert metadata["formula_contract_complete"] is True
    assert metadata["formula_thesis"]["functional_form"] == "STATE_CONDITIONAL"
    assert metadata["formula_math_profile"]["output_unit"] == "BPS"

    without_thesis = {key: value for key, value in block.items()
                      if key != "FORMULA_THESIS"}
    with pytest.raises(ValueError, match="FORMULA_THESIS is required"):
        lead_intake._readiness_metadata(
            without_thesis, without_thesis["MECHANISM"])


def test_scout_parser_keeps_live_structured_formula_fields_isolated() -> None:
    text = """TITLE: Tight-spread order-flow pressure
PUBLISHED: 2026-04-04
ACCESSED: 2026-08-17
CLAIMED_EDGE: Signed quote pressure clears costs only in executable liquidity
MECHANISM: Signed quote flow consumes the thin side when spreads remain tight.
READINESS: AST_READY
RESEARCH_LANE: INTRADAY_EVENT
SEMANTIC_PLAN: {"event":"ORDER_FLOW","context":["TIGHT_SPREAD"],"qualities":["STATE_CONDITIONAL"],"direction":"FOLLOW","output":"TAKER_NET_PNL","execution":"TAKER","horizon_seconds":60}
OBSERVABLES: ["normalized_quote_ofi","spread_bps"]
CANDIDATE_SIGNAL_EXPR: {"op":"where","condition":{"op":"lt","args":[{"op":"field","field":"spread_bps"},{"const":5,"unit":"BPS"}]},"then":{"op":"mul","args":[{"op":"rolling_mean","arg":{"op":"field","field":"normalized_quote_ofi"},"seconds":60},{"op":"field","field":"spread_bps"}]},"else":{"const":0,"unit":"BPS"}}
TESTABLE_WITH: Compare the tight-spread state with ungated and wide-spread controls.
DERIVATION_MODE: MECHANISM_MUTATION
SOURCE_BASELINE_EXPR: {"op":"field","field":"normalized_quote_ofi"}
DERIVATION_TRANSFORMS: ["STATE_CONDITION","MECHANISM_INTERACTION"]
NOVELTY_RATIONALE: Makes executable liquidity an explicit state and scale.
FORMULA_THESIS: {"target":"TAKER_NET_PNL","functional_form":"STATE_CONDITIONAL","expected_sign":"STATE_DEPENDENT","coefficient_policy":"PREREGISTERED_NO_OOS_FIT","decision_rule":"PREDICTED_MARKOUT_CLEARS_COST","terms":{"normalized_quote_ofi":"PRESSURE","spread_bps":"LIQUIDITY"},"identification":"Positive net markout must survive state and scale ablations after locked costs."}
UNUSED_METADATA: must not be appended to the JSON thesis
LESSONS_ADDRESSED: COST_SENSITIVE|BASELINE_NOT_BEATEN|OVERFIT_PBO
URL: https://example.test/tight-spread
"""
    block = lead_intake.parse_blocks(text)[0]

    assert block["TITLE"] == "Tight-spread order-flow pressure"
    assert block["PUBLISHED"] == "2026-04-04"
    assert block["LESSONS_ADDRESSED"] == (
        "COST_SENSITIVE|BASELINE_NOT_BEATEN|OVERFIT_PBO")
    assert block["FORMULA_THESIS"].endswith("locked costs.\"}")

    lead = lead_intake.to_lead(
        block, lens="PRACTITIONER", source_type="BLOG",
        case_id="case-parser-regression", model_version="test-model",
        prompt_version="scout-parser-v5")
    contract = lead["ast_contract"]
    assert contract["observables"] == ["normalized_quote_ofi", "spread_bps"]
    assert contract["formula_contract_complete"] is True
    assert contract["lessons_addressed"].startswith("COST_SENSITIVE")
    assert lead["claimed_edge"].startswith("Signed quote pressure")
    assert lead["refs"][0]["source_published"] == "2026-04-04"
    assert lead["refs"][0]["declared_accessed"] == "2026-08-17"
    typed_lead = MethodologyLeadV1.model_validate(lead)
    assert typed_lead.refs[0].source_published == "2026-04-04"
    assert typed_lead.refs[0].declared_accessed == "2026-08-17"

    with pytest.raises(ValueError, match="source medium.*not the Scout lens"):
        lead_intake.to_lead(
            block, lens="PRACTITIONER", source_type="PRACTITIONER",
            case_id="case-source-type-regression", model_version="test-model",
            prompt_version="scout-parser-v5")


def test_intraday_gate_rejects_story_formula_mismatch() -> None:
    proposal = _intraday_proposal()
    proposal["suggested_params"] = {
        **proposal["suggested_params"],
        "intraday_signal_expr": {"op": "field", "field": "microprice_offset_bps"},
    }
    gate = gate0(proposal)
    assert not gate.ok
    assert "SEMANTIC_FORMULA_MISMATCH" in gate.codes

    proposal = _intraday_proposal()
    proposal["suggested_params"]["intraday_signal_expr"] = {
        "op": "add", "args": [
            {"op": "field", "field": "spread_bps"},
            {"op": "rolling_mean", "seconds": 30,
             "arg": {"op": "field", "field": "spread_bps"}}]}
    gate = gate0(proposal)
    assert not gate.ok
    assert any("must gate the signal" in reason for reason in gate.reasons)


def test_schema_migration_persists_lane_and_live_manifest() -> None:
    sql = (ROOT / "supabase" / "migrations" /
           "20260816150000_intraday_alpha_factory.sql").read_text(encoding="utf-8")
    assert "research_lane" in sql and "semantic_plan" in sql
    assert "krx-intraday-events" in sql
    assert "market_quotes" in sql and "market_ticks" in sql
    assert "coalesce(" in sql  # JSON NULL must not bypass the CHECK constraint.


def test_intraday_input_identity_ignores_wall_clock_but_tracks_lineage() -> None:
    base = {"cutoff": "2026-08-16T00:00:00+00:00",
            "slice": {"sessions": ["2026-08-14"], "instruments": ["A", "B"]},
            "source_lineage": [{"source": "market_quotes", "rows": 100}]}
    later_call = {**base, "cutoff": "2026-08-16T00:01:00+00:00"}
    late_data = {**later_call,
                 "source_lineage": [{"source": "market_quotes", "rows": 101}]}
    assert _input_hash("H1", base) == _input_hash("H1", later_call)
    assert _input_hash("H1", base) == _input_hash(
        "H1", {**base, "instrument_shard_size": 64,
               "legacy_instrument_count_ignored": 2})
    assert _input_hash("H1", base) != _input_hash("H1", late_data)


def test_external_content_correction_invalidates_identity_with_same_shape() -> None:
    at = datetime(2026, 8, 14, 6, 30, tzinfo=timezone.utc)
    aggregate = [
        ("ext_src.quotes", 100, at, at, at, None),
        ("ext_src.ticks", 50, at, at, at, None),
    ]

    class Cursor:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.current = []

        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, *_): self.current = next(self.responses)
        def fetchall(self): return self.current

    class Conn:
        def __init__(self, content_hash):
            self.responses = [aggregate, [
                (date(2026, 8, 14), "005930", 100, 50,
                 content_hash,
                 "pg-composite-row-xor0-sum1-sha256-v1",
                 content_hash, at),
            ]]

        def cursor(self): return Cursor(self.responses)

    selected = {
        "status": "PASS", "event_source": EXTERNAL_EVENT_SOURCE,
        "calibration_sessions": [], "sessions": [date(2026, 8, 14)],
        "instruments": ["005930"],
    }
    first = _lineage(Conn("a" * 64), selected, at)
    corrected = _lineage(Conn("b" * 64), selected, at)

    assert [row["rows"] for row in first] == [row["rows"] for row in corrected]
    assert first[0]["min_event_time"] == corrected[0]["min_event_time"]
    assert first[0]["content_fingerprint"] != corrected[0][
        "content_fingerprint"]
    assert _input_hash("H1", {"source_lineage": first}) != _input_hash(
        "H1", {"source_lineage": corrected})


def test_external_consumed_replay_identity_includes_calibration_and_empty_cells() -> None:
    at = datetime(2026, 8, 14, 6, 30, tzinfo=timezone.utc)
    aggregate = [
        ("ext_src.quotes", 100, at, at, at, None),
        ("ext_src.ticks", 50, at, at, at, None),
    ]

    class Cursor:
        def __init__(self):
            self.responses = iter([aggregate, [
                (date(2026, 8, 14), "005930", 100, 50, "a" * 64,
                 "pg-composite-row-xor0-sum1-sha256-v1", "a" * 64, at),
            ]])
            self.current = []

        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, *_): self.current = next(self.responses)
        def fetchall(self): return self.current

    class Conn:
        def cursor(self): return Cursor()

    selected = {
        "status": "PASS", "event_source": EXTERNAL_EVENT_SOURCE,
        "calibration_sessions": [date(2026, 8, 13)],
        "sessions": [date(2026, 8, 14)],
        "instruments": ["005930"],
    }
    lineage = _lineage(Conn(), selected, at)

    assert lineage[0]["consumed_replay_content_contract"] == \
        "external-raw-replay-content-v3"
    assert lineage[0]["consumed_replay_content_manifest_rows"] == 2
    assert len(lineage[0]["consumed_replay_content_fingerprint"]) == 64


@pytest.mark.parametrize("horizon_seconds", [5, 30, 60, 300, 599, 600])
def test_external_raw_identity_window_is_horizon_independent(
        horizon_seconds: int) -> None:
    day = date(2026, 8, 14)
    _start, decision_end = _session_bounds(day)
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        feature_lookback_seconds=30,
        horizons_seconds=(horizon_seconds,),
        order_latency_ms=250,
        max_quote_age_seconds=5.0,
        fee_bps_per_side=11.5,
        maker_fee_bps_per_side=11.5,
    )
    content_end = _external_content_end(day)
    sample_load_end = min(
        decision_end + effective_purge_gap(spec, COMPLETED_SECOND_POLICY),
        content_end)

    assert sample_load_end <= content_end
    assert _replay_load_end(
        day, EXTERNAL_EVENT_SOURCE, sample_load_end) == content_end
    assert content_end == datetime(
        2026, 8, 14, 6, 30, tzinfo=timezone.utc)


def test_external_raw_manifest_requires_calibration_and_evaluation_cell_set() -> None:
    selected = {
        "calibration_sessions": [date(2026, 8, 13)],
        "sessions": [date(2026, 8, 14)],
        "instruments": ["005930", "000660"],
    }
    rows = []
    for index, (session, instrument) in enumerate((
            ("2026-08-13", "005930"),
            ("2026-08-13", "000660"),
            ("2026-08-14", "005930"),
            ("2026-08-14", "000660")), 1):
        rows.append({
            "session": session, "instrument": instrument,
            "quote_rows": index, "trade_rows": index,
            "source_content_fingerprint": f"{index:064x}",
        })

    manifest = _external_replay_manifest(list(reversed(rows)), selected)
    assert manifest["manifest_rows"] == 4
    assert manifest["identity"]["content_window"] == {
        "timezone": "Asia/Seoul", "start": "09:00:00",
        "end_exclusive": "15:30:00", "interval": "HALF_OPEN",
    }
    with pytest.raises(RuntimeError, match="cell set differs"):
        _external_replay_manifest(rows[2:], selected)
    with pytest.raises(RuntimeError, match="cell set differs"):
        _external_replay_manifest([*rows, rows[0]], selected)


def test_runner_never_resolves_auto_from_table_existence() -> None:
    class NeverRead:
        def cursor(self):
            raise AssertionError("runner must reject AUTO before database reads")

    with pytest.raises(RuntimeError, match="resolver-only policy"):
        select_slice(
            NeverRead(), {"evaluation_days": 60, "data_source": "AUTO"},
            cutoff=datetime(2026, 8, 17, tzinfo=timezone.utc))


def test_dataset_cutoff_is_retry_stable_and_covers_replay_floor() -> None:
    floor = datetime(2026, 8, 14, 6, 31, tzinfo=timezone.utc)
    earlier = [{"max_source_watermark": "2026-08-14T06:30:00+00:00"}]
    later = [{"max_source_watermark": "2026-08-14T06:32:00+00:00"}]
    assert _stable_dataset_cutoff(earlier, floor) == floor.isoformat()
    assert _stable_dataset_cutoff(later, floor) == (
        floor + timedelta(minutes=1)).isoformat()


def test_primary_budget_and_formula_exposure_are_separate_ledgers() -> None:
    class Cursor:
        def __init__(self, conn): self.conn = conn
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params): self.conn.executed = (sql, params)
        def fetchone(self): return (self.conn.results.pop(0),)

    class Conn:
        def __init__(self):
            self.executed = None
            # Live-like ledger: one PRIMARY attempt, nineteen total exact-formula
            # appearances after adaptive screens.
            self.results = [1, 19]
        def cursor(self): return Cursor(self)

    conn = Conn()
    expr = {"op": "field", "field": "microprice_offset_bps"}
    assert count_family_trials(conn, "family", expr) == 1
    sql, params = conn.executed
    assert sql == _SQL_FAMILY_OR_EXACT_TRIALS
    assert "screening_population" not in sql
    assert "e.status in ('QUEUED', 'RUNNING', 'COMPLETED')" in sql
    assert "intraday_session_accesses" in sql
    assert "quant.backtest_runs" in sql
    assert "quant.experiment_metrics" in sql
    assert "research.experiment_outcomes" in sql
    assert len(params) == 3 and params[1] == params[2]

    assert count_screening_exposures(conn, expr) == 19
    exposure_sql, exposure_params = conn.executed
    assert exposure_sql == _SQL_EXACT_SCREENING_EXPOSURES
    assert "jsonb_array_elements" in exposure_sql
    assert "screening_population" in exposure_sql
    assert "intraday_session_accesses" in exposure_sql
    assert "quant.backtest_runs" in exposure_sql
    assert "quant.experiment_metrics" in exposure_sql
    assert "research.experiment_outcomes" in exposure_sql
    assert len(exposure_params) == 3
    assert exposure_params[0] == exposure_params[1] == exposure_params[2]


def test_historical_screening_exposure_reaches_dsr_without_spending_budget() -> None:
    proposal = _intraday_proposal()
    # An LLM cannot forge the system count through suggested params.
    proposal["suggested_params"]["primary_attempts_before"] = 999_999
    proposal["suggested_params"][
        "historical_exact_screening_exposures"] = 999_999
    gate = gate0(proposal, trials_used=1,
                 historical_screening_exposures=19)
    assert gate.ok, gate.as_dict()
    assert gate.trials_used == 1
    assert gate.trial_number == 2
    assert gate.historical_screening_exposures == 19
    assert gate.multiple_testing_exposures == 20
    assert "OVER_BUDGET" not in gate.codes

    row = to_hypothesis_row(proposal, gate)
    edge = row["expected_edge"]
    assert edge["primary_attempts_before"] == 1
    assert edge["historical_exact_screening_exposures"] == 19
    config, spec = config_from_edge(edge)
    assert config["primary_attempts_before"] == 1
    assert config["historical_exact_screening_exposures"] == 19
    accumulators = _candidate_accumulators(config, spec, trials=2)
    # Candidate vectors cover only the current synchronous cohort.  Historical
    # pressure is a separate DSR extrapolation input, never a fabricated vector.
    assert accumulators["PRIMARY"].trials == 1
    assert config["selection_adjusted_trials"] == 21


def test_selection_pressure_keeps_current_vectors_separate_from_history() -> None:
    edge, _ = expected_edge_for(_intraday_proposal())
    edge["primary_attempts_before"] = 1
    edge["historical_exact_screening_exposures"] = 16
    config, spec = config_from_edge(edge)
    primary = config["intraday_signal_expr"]
    config["screening_population"] = [{
        "ast_fingerprint": f"side-{index}",
        "intraday_signal_expr": primary,
        "semantic_plan": config["semantic_plan"],
        "horizon_seconds": config["horizon_seconds"],
        "execution": config["execution"],
        "entry_policy": config["entry_policy"],
        "coefficient_policy": config["coefficient_policy"],
    } for index in range(4)]
    config["screening_trial_exposure"] = 4

    accumulators = _candidate_accumulators(config, spec, trials=2)
    assert len(accumulators) == 5
    assert {candidate.trials for candidate in accumulators.values()} == {5}
    assert config["selection_adjusted_trials"] == 22
    assert config["selection_pressure_breakdown"] == {
        "primary_trials_including_current": 2,
        "historical_exact_screening_exposures": 16,
        "current_screening_exposures": 4,
        "current_synchronous_formula_vectors": 5,
    }


def test_idempotent_replay_keeps_screening_metrics_out_of_primary_summary() -> None:
    primary = {"op": "field", "field": "microprice_offset_bps"}
    side = {"op": "neg", "arg": primary}
    config = {
        "intraday_signal_expr": primary,
        "screening_population": [{
            "ast_fingerprint": "side-fp", "intraday_signal_expr": side,
            "candidate_role": "LINKED_CANDIDATE",
            "source_lead_ids": ["lead-side"],
        }],
    }
    rows = [
        ("mean_net_bps_per_opportunity", 1.0, {"summary": True}),
        ("mean_net_bps_per_opportunity", 99.0,
         {"summary": True, "screening_candidate": "side-fp"}),
        ("intraday_gate_pass", 0.0,
         {"decision": "HOLD", "failed_criteria": ["OVERFIT_DSR"]}),
        ("intraday_screening_result", 1.0, {
            "screening_candidate": "side-fp", "screening_only": True,
            "screening_gate_decision": "SUBMIT_TO_QA", "pareto_rank": 1,
            "pareto_front": True, "failed_criteria": [],
        }),
        ("intraday_residual_behavior", 1.5, {
            "status": "PASS", "worst_time_bucket": "OPEN",
            "median_time_bucket_mae_bps": 1.5,
            "promotion_authority": False,
            "residual_qd": {"cell": "OPEN/NODES_1_5", "elite": True},
        }),
        ("intraday_residual_behavior", 2.5, {
            "screening_candidate": "side-fp", "status": "PASS",
            "worst_time_bucket": "CLOSE",
            "median_time_bucket_mae_bps": 2.5,
            "promotion_authority": False,
            "residual_qd": {"cell": "CLOSE/NODES_1_5", "elite": True},
        }),
    ]

    class Cursor:
        def __init__(self): self.sql = ""
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, _params): self.sql = sql
        def fetchone(self):
            normalized = " ".join(str(self.sql).lower().split())
            if "select config from quant.experiments" in normalized:
                return (config,)
            return None
        def fetchall(self): return rows

    class Conn:
        def cursor(self): return Cursor()

    report = _load_completed_report(Conn(), "experiment")
    assert report["summary"]["mean_net_bps_per_opportunity"] == 1.0
    assert report["screening_population"][0]["summary"][
        "mean_net_bps_per_opportunity"] == 99.0
    assert report["decision"] == "HOLD"
    assert report["screening_population"][0]["decision"] == "SCREENING_ONLY"
    assert report["residual_behavior"]["worst_time_bucket"] == "OPEN"
    assert report["residual_qd"]["cell"] == "OPEN/NODES_1_5"
    assert report["screening_population"][0]["residual_behavior"][
        "worst_time_bucket"] == "CLOSE"
    assert report["screening_population"][0]["residual_qd"]["elite"] is True

    for relative in (
            "departments/04-quant-backtest/pipeline/experiment_orchestrator.py",
            "departments/04-quant-backtest/pipeline/experiment_card.py",
            "departments/04-quant-backtest/pipeline/orphan_finalizer.py"):
        assert "dimensions->>'screening_candidate' is null" in \
            (ROOT / relative).read_text(encoding="utf-8")


def test_session_discovery_is_partition_bounded_and_cutoff_reproducible() -> None:
    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self): return self
        def __exit__(self, *_): return False

        def execute(self, sql, params):
            self.conn.executed.append((sql, params))

        def fetchall(self):
            return [(date(2026, 8, 14),)]

    class Conn:
        def __init__(self): self.executed = []
        def cursor(self): return Cursor(self)

    conn = Conn()
    cutoff = datetime(2026, 8, 16, 3, 0, tzinfo=timezone.utc)
    selected = select_slice(
        conn, {"evaluation_days": 60}, cutoff=cutoff)
    sql, params = conn.executed[0]
    assert selected["status"] == "INSUFFICIENT_SESSIONS"
    assert selected["causal_sessions_available"] == 1
    assert selected["sessions"] == []  # the only day is reserved for calibration
    assert "event_time >= %s" in sql
    assert "greatest(received_at, observed_at) <= %s" in sql
    assert "now()" not in sql
    assert params == (cutoff - timedelta(days=180), cutoff, cutoff, 65)


def test_short_live_history_runs_one_calibration_and_two_oos_sessions() -> None:
    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self): return self
        def __exit__(self, *_): return False

        def execute(self, sql, params):
            self.conn.executed.append((sql, params))

        def fetchall(self):
            if len(self.conn.executed) == 1:
                return [(date(2026, 8, 14),), (date(2026, 8, 13),),
                        (date(2026, 8, 12),)]
            return [("instrument-a", 33_000), ("instrument-b", 29_000)]

    class Conn:
        def __init__(self): self.executed = []
        def cursor(self): return Cursor(self)

    conn = Conn()
    cutoff = datetime(2026, 8, 16, 3, 0, tzinfo=timezone.utc)
    selected = select_slice(
        conn, {"evaluation_days": 60}, cutoff=cutoff)

    assert selected["status"] == "PASS"
    assert selected["statistical_readiness"] == "SHORT_DIAGNOSTIC"
    assert selected["calibration_sessions"] == ["2026-08-12"]
    assert selected["sessions"] == [date(2026, 8, 13), date(2026, 8, 14)]
    assert selected["evaluation_session_count"] == 2
    assert selected["instruments"] == ["instrument-a", "instrument-b"]
    universe_sql, universe_params = conn.executed[1]
    assert "greatest(received_at, observed_at) <= %s" in universe_sql
    assert "limit" not in universe_sql.lower()
    assert "market.market_ticks" in universe_sql
    assert len(universe_params) == 6
    assert universe_params[2] == cutoff
    assert universe_params[5] == cutoff


def test_short_diagnostic_cannot_allocate_a_governed_discovery_rung() -> None:
    cutoff = datetime(2026, 8, 16, 3, 0, tzinfo=timezone.utc)
    prepared = {
        "config": {}, "spec": None, "cutoff": cutoff,
        "selected": {
            "status": "PASS", "statistical_readiness": "SHORT_DIAGNOSTIC",
            "product_filter": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
        },
    }
    with pytest.raises(RuntimeError, match="diagnostic-only"):
        run_intraday({"_intraday_preflight": prepared}, "H1",
                     meta_conn=object(), market_conn=object())


def test_external_61_session_ledger_selects_one_calibration_and_60_oos() -> None:
    days = [date(2026, 5, 18) + timedelta(days=index)
            for index in range(61)]

    class Cursor:
        def __init__(self, conn):
            self.conn = conn

        def __enter__(self): return self
        def __exit__(self, *_): return False

        def execute(self, sql, params):
            self.conn.executed.append((sql, params))

        def fetchall(self):
            if len(self.conn.executed) == 1:
                return [(day,) for day in reversed(days)]
            return [("005930", 100_000), ("000660", 90_000)]

    class Conn:
        def __init__(self): self.executed = []
        def cursor(self): return Cursor(self)

    conn = Conn()
    selected = select_slice(
        conn, {"evaluation_days": 60, "data_source": EXTERNAL_EVENT_SOURCE},
        cutoff=datetime(2026, 8, 17, tzinfo=timezone.utc))

    assert selected["status"] == "PASS"
    assert selected["event_source"] == EXTERNAL_EVENT_SOURCE
    assert selected["arrival_clock_pit"] is False
    assert selected["historical_replay_only"] is True
    assert selected["calibration_session_count"] == 1
    assert selected["evaluation_session_count"] == 60
    assert selected["instruments"] == ["005930", "000660"]
    assert "market.microstructure_features" in conn.executed[0][0]
    assert "limit" not in conn.executed[1][0].lower()


def test_fast_discovery_screen_is_stratified_and_never_substitutes_sidecar() -> None:
    assert _stratified(list(range(10)), 4) == [0, 3, 6, 9]
    config = {
        "fast_screen_min_opportunities": 100,
        "fast_screen_min_net_bps": 0.0,
    }
    negative = {
        "summary": {
            "opportunities": 1_000,
            "mean_net_bps_per_opportunity": -1.0,
            "session_mean_net_bps": -2.0,
            "session_net_ci_high_bps": -0.2,
        },
        "screening_population": [{
            "ast_fingerprint": "linked-positive",
            "summary": {
                "opportunities": 1_000,
                "mean_net_bps_per_opportunity": 1.0,
                "session_mean_net_bps": 2.0,
                "session_net_ci_high_bps": 3.0,
            },
        }],
    }
    gate = _fast_screen_gate(negative, config)
    assert gate["version"] == FAST_SCREEN_VERSION
    assert gate["primary_pass"] is False
    assert gate["survivors"] == ["linked-positive"]
    assert gate["linked_survivor_count"] == 1
    assert gate["promotion_authority"] is False

    positive = {**negative, "summary": {
        "opportunities": 1_000,
        "mean_net_bps_per_opportunity": 0.01,
        "session_mean_net_bps": 0.02,
        "session_net_ci_high_bps": 0.5,
    }}
    assert _fast_screen_gate(positive, config)["primary_pass"] is True

    # Six sessions are too weak to kill an uncertain candidate solely because
    # its point estimate is negative.  The screen advances it for more evidence
    # only while the session-level upper interval remains positive.
    uncertain = {**negative, "summary": {
        "opportunities": 1_000,
        "mean_net_bps_per_opportunity": -0.10,
        "session_mean_net_bps": -0.20,
        "session_net_ci_high_bps": 0.40,
    }}
    uncertain_gate = _fast_screen_gate(uncertain, config)
    assert uncertain_gate["primary_pass"] is True
    assert uncertain_gate["criteria"][
        "positive_point_estimate_required_by_default"] is False
    assert uncertain_gate["promotion_authority"] is False

    # An explicitly preregistered point floor remains binding; only the default
    # path changes from a point-estimate filter to a futility filter.
    hard_floor = {**config, "fast_screen_hard_net_floor_enabled": True}
    assert _fast_screen_gate(uncertain, hard_floor)["primary_pass"] is False

    non_finite = {**positive, "summary": {
        **positive["summary"], "session_net_ci_high_bps": float("nan")}}
    assert _fast_screen_gate(non_finite, config)["primary_pass"] is False

    non_finite_count = {**positive, "summary": {
        **positive["summary"], "opportunities": float("nan")}}
    assert _fast_screen_gate(non_finite_count, config)["primary_pass"] is False


def test_streaming_shards_equal_single_pass_and_cover_requested_universe() -> None:
    spec = IntradayLaneSpec(
        sample_interval_seconds=5, feature_lookback_seconds=30,
        horizons_seconds=(5,), order_latency_ms=0,
        max_quote_age_seconds=5, fee_bps_per_side=1)
    expr = {"op": "field", "field": "trade_flow_imbalance"}
    samples = {}
    for instrument in ("A", "B"):
        samples[instrument] = [
            _sample(instrument,
                    datetime(2026, 1, 2, 0, index, tzinfo=timezone.utc),
                    signal=1.0, net=2.0)
            for index in range(4)]
    expected = evaluate_candidate(
        samples, expr=expr, spec=spec, horizon_seconds=5, execution="TAKER",
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1, "min_deflated_sharpe": -100,
                  "min_positive_session_ratio": 0})
    accumulator = CandidateAccumulator(
        expr=expr, spec=spec, horizon_seconds=5, execution="TAKER",
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1, "min_deflated_sharpe": -100,
                  "min_positive_session_ratio": 0})
    accumulator.add("A", samples["A"])
    accumulator.add("B", samples["B"])
    actual = accumulator.finish()

    for key in ("opportunities", "fills", "mean_mid_markout_bps",
                "mean_net_bps_per_opportunity", "session_mean_net_bps"):
        assert actual["summary"][key] == pytest.approx(expected["summary"][key])
    assert actual["summary"]["instruments_requested"] == 2
    assert actual["summary"]["instrument_coverage"] == 1.0
    assert actual["summary"]["max_concurrent_opportunities"] == 2
    assert actual["lane_manifest"]["portfolio_capital_model"].startswith(
        "EXACT_PORTFOLIO")


def test_population_sorts_and_audits_once_but_keeps_candidate_statistics(
        monkeypatch) -> None:
    spec = IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0,
                            fee_bps_per_side=1)
    primary_expr = {"op": "field", "field": "microprice_offset_bps"}
    side_expr = {"op": "neg", "arg": primary_expr}
    rules = {"min_sessions": 1, "min_instruments": 1,
             "min_opportunities": 1, "min_deflated_sharpe": -100,
             "min_positive_session_ratio": 0}
    candidates = {
        "PRIMARY": CandidateAccumulator(
            expr=primary_expr, spec=spec, horizon_seconds=5,
            execution="TAKER", family_pbo=0.2, trials=3, criteria=rules),
        "side": CandidateAccumulator(
            expr=side_expr, spec=spec, horizon_seconds=5,
            execution="TAKER", family_pbo=0.2, trials=3, criteria=rules),
    }
    calls = 0
    original = candidate_module.audit_causality

    def counted(samples, lane_spec):
        nonlocal calls
        calls += 1
        return original(samples, lane_spec)

    monkeypatch.setattr(candidate_module, "audit_causality", counted)
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    samples = [_sample("A", base + timedelta(seconds=offset * 10),
                       signal=1.0, net=2.0) for offset in range(3)]
    population = CandidatePopulationAccumulator(candidates)
    population.add("A", list(reversed(samples)))
    reports = population.finish()

    assert calls == 1
    assert reports["PRIMARY"]["summary"]["opportunities"] == 3
    residual = reports["PRIMARY"]["residual_behavior"]
    assert residual["status"] == "PASS"
    assert residual["observations"] == 3
    assert residual["worst_time_bucket"] == "OPEN"
    assert residual["mean_absolute_error_bps"] == pytest.approx(2.0)
    assert residual["null_mean_absolute_error_bps"] == pytest.approx(3.0)
    assert residual["mae_improvement_vs_null_bps"] == pytest.approx(1.0)
    assert residual[
        "median_time_bucket_mae_improvement_vs_null_bps"] == pytest.approx(1.0)
    assert residual["promotion_authority"] is False
    assert residual["independent_confirmation"] is False
    assert residual["forward_new_sessions_required"] is True
    assert reports["side"]["summary"]["opportunities"] == 0
    assert reports["side"]["decision"] == "NO_EVIDENCE"

    annotated = _annotate_population({
        "intraday_signal_expr": primary_expr,
        "screening_population": [{
            "ast_fingerprint": "side", "intraday_signal_expr": side_expr,
            "candidate_role": "LINKED_CANDIDATE",
            "source_lead_ids": ["lead-side"], "title": "inverse",
        }],
    }, reports)
    screened = annotated["screening_population"][0]
    assert screened["decision"] == "SCREENING_ONLY"
    assert screened["screening_gate_decision"] == "NO_EVIDENCE"
    assert screened["residual_qd"]["cell"] == "OPEN/NODES_1_5"
    assert screened["residual_qd"]["elite"] is False
    assert annotated["residual_qd"]["elite"] is True
    assert annotated["residual_qd"][
        "median_time_bucket_mae_improvement_vs_null_bps"] == pytest.approx(1.0)
    assert annotated["population_evaluation"]["residual_archive_cells"] == 1
    assert annotated["population_evaluation"][
        "residual_archive_boundary"] == "OOS_DIAGNOSTIC_SCREENING_ONLY"
    assert annotated["population_evaluation"][
        "residual_archive_independent_confirmation"] is False
    assert annotated["population_evaluation"][
        "residual_archive_forward_new_sessions_required"] is True
    assert annotated["population_evaluation"]["promotion_authority"] == \
        "PRIMARY_ONLY"


def test_complete_synchronous_formula_population_calibrates_primary_dsr() -> None:
    sessions = [f"2026-01-{index + 1:02d}" for index in range(30)] + [
        f"2026-02-{index + 1:02d}" for index in range(30)]

    def returns(offset: float) -> dict[str, float]:
        return {session: 1.0 + offset + (index % 7) * 0.1
                for index, session in enumerate(sessions)}

    primary_returns = returns(0.0)
    report = {
        "decision": "HOLD",
        "failed_criteria": ["OVERFIT_DSR", "DSR_TRIAL_DISPERSION_UNMEASURED"],
        "summary": {
            "sessions": 60,
            "trials": 4,
            "sharpe": 80.0,
            "deflated_sharpe": 0.0,
            "dsr_calibration_mode": "legacy_unit_trial_sharpe_std",
        },
        "session_returns_bps": primary_returns,
        "ast_shape_fingerprint": "shape-primary",
        "population_evaluation": {"selection_adjusted_trials": 22},
        "screening_population": [{
            "ast_fingerprint": "formula-a",
            "ast_shape_fingerprint": "shape-a",
            "summary": {"sharpe": 78.0},
            "session_returns_bps": returns(-0.05),
        }, {
            # A distinct preregistered formula is still a trial even if its
            # realized vector exactly matches another formula.
            "ast_fingerprint": "formula-b",
            "ast_shape_fingerprint": "shape-b",
            "summary": {"sharpe": 78.0},
            "session_returns_bps": returns(-0.05),
        }, {
            "ast_fingerprint": "formula-c",
            "ast_shape_fingerprint": "shape-c",
            "summary": {"sharpe": 79.0},
            "session_returns_bps": returns(-0.1),
        }],
    }
    multiple = _population_multiple_testing(report)

    assert multiple["observed_formula_trials"] == 4
    assert multiple["observed_unique_formula_outcomes"] == 3
    assert multiple["complete_synchronous_formula_vectors_available"] is True
    assert multiple["complete_historical_trial_vectors_available"] is False
    assert multiple["dispersion_sample_count"] == 4
    assert multiple["selection_adjusted_trials_declared"] == 22
    assert multiple["dispersion_extrapolation"] is True
    assert multiple["structurally_distinct_formula_trials"] == 4
    assert multiple["historical_vectors_fabricated"] is False
    assert multiple["raw_count_as_effective_upper_bound"] is True
    assert multiple["historical_trial_vectors_missing"] == 18
    calibrated = _apply_population_dsr_gate(report, multiple)
    assert calibrated["summary"]["dsr_calibration_mode"] == \
        "CONSERVATIVE_COHORT_FLOOR_EXTRAPOLATION"
    assert calibrated["summary"]["dsr_effective_trials"] == 22
    assert calibrated["summary"]["dsr_dispersion_sample_count"] == 4
    assert calibrated["summary"]["dsr_dispersion_extrapolated"] is True
    assert calibrated["summary"]["dsr_trial_sharpe_std"] >= 1.0
    assert "DSR_TRIAL_DISPERSION_UNMEASURED" not in calibrated["failed_criteria"]
    assert calibrated["decision"] == "SUBMIT_TO_QA"


def test_incomplete_formula_population_keeps_dsr_fail_closed() -> None:
    session_returns = {str(index): 1.0 + (index % 5) * 0.1
                       for index in range(60)}
    report = {
        "decision": "HOLD",
        "failed_criteria": ["OVERFIT_DSR", "DSR_TRIAL_DISPERSION_UNMEASURED"],
        "summary": {"sessions": 60, "trials": 2, "sharpe": 70.0},
        "session_returns_bps": session_returns,
        "screening_population": [],
    }
    multiple = _population_multiple_testing(report)

    assert multiple["complete_historical_trial_vectors_available"] is False
    assert _apply_population_dsr_gate(report, multiple)["failed_criteria"] == [
        "OVERFIT_DSR", "DSR_TRIAL_DISPERSION_UNMEASURED"]


def test_historical_dsr_zero_dispersion_uses_conservative_floor() -> None:
    sessions = [str(index) for index in range(60)]
    session_returns = {
        session: 1.0 + (index % 5) * 0.1
        for index, session in enumerate(sessions)
    }
    report = {
        "decision": "HOLD",
        "failed_criteria": ["OVERFIT_DSR", "DSR_TRIAL_DISPERSION_UNMEASURED"],
        "summary": {"sessions": 60, "trials": 4, "sharpe": 70.0},
        "session_returns_bps": session_returns,
        "ast_shape_fingerprint": "shape-primary",
        "population_evaluation": {"selection_adjusted_trials": 21},
        "screening_population": [{
            "ast_fingerprint": f"formula-{index}",
            "ast_shape_fingerprint": f"shape-{index}",
            "summary": {"sharpe": 70.0},
            "session_returns_bps": dict(session_returns),
        } for index in range(3)],
    }

    multiple = _population_multiple_testing(report)
    calibrated = multiple["observed_population_dsr"]

    assert multiple["dsr_calibration_ready"] is True
    assert multiple["observed_trial_sharpe_std"] == pytest.approx(0.0)
    assert multiple["trial_sharpe_std_floor"] == 1.0
    assert multiple["trial_sharpe_std_used"] == 1.0
    assert calibrated["calibration_mode"] == \
        "CONSERVATIVE_COHORT_FLOOR_EXTRAPOLATION"
    assert calibrated["trials"] == 21
    assert calibrated["effective_trials"] == 21
    assert calibrated["trial_sharpe_std"] == 1.0


def test_dsr_needs_four_current_structurally_distinct_formulas() -> None:
    sessions = [str(index) for index in range(60)]

    def returns(offset: float) -> dict[str, float]:
        return {session: offset + 1.0 + (index % 5) * 0.1
                for index, session in enumerate(sessions)}

    report = {
        "decision": "HOLD",
        "failed_criteria": ["OVERFIT_DSR", "DSR_TRIAL_DISPERSION_UNMEASURED"],
        "summary": {"sessions": 60, "trials": 3, "sharpe": 70.0},
        "session_returns_bps": returns(0.0),
        "ast_shape_fingerprint": "shape-primary",
        "population_evaluation": {"selection_adjusted_trials": 21},
        "screening_population": [{
            "ast_fingerprint": f"formula-{index}",
            "ast_shape_fingerprint": f"shape-{index}",
            "summary": {"sharpe": 69.0 - index},
            "session_returns_bps": returns(-0.1 * (index + 1)),
        } for index in range(2)],
    }

    multiple = _population_multiple_testing(report)

    assert multiple["complete_synchronous_formula_vectors_available"] is True
    assert multiple["dispersion_sample_count"] == 3
    assert multiple["minimum_dispersion_formulas"] == 4
    assert multiple["dsr_calibration_ready"] is False
    assert multiple["observed_population_dsr"] is None
    assert _apply_population_dsr_gate(report, multiple)["failed_criteria"] == [
        "OVERFIT_DSR", "DSR_TRIAL_DISPERSION_UNMEASURED"]


def test_spa_rejects_equal_length_but_different_session_keys() -> None:
    sessions = [f"s-{index:02d}" for index in range(60)]

    def returns(offset: float) -> dict[str, float]:
        return {session: offset + 1.0 + (index % 5) * 0.1
                for index, session in enumerate(sessions)}

    mismatched = returns(-0.3)
    mismatched["replacement-session"] = mismatched.pop(sessions[-1])
    report = {
        "decision": "HOLD",
        "failed_criteria": ["OVERFIT_DSR", "DSR_TRIAL_DISPERSION_UNMEASURED"],
        "summary": {"sessions": 60, "trials": 4, "sharpe": 70.0},
        "session_returns_bps": returns(0.0),
        "ast_shape_fingerprint": "shape-primary",
        "screening_population": [{
            "ast_fingerprint": "formula-a",
            "ast_shape_fingerprint": "shape-a",
            "summary": {"sharpe": 69.0},
            "session_returns_bps": returns(-0.1),
        }, {
            "ast_fingerprint": "formula-b",
            "ast_shape_fingerprint": "shape-b",
            "summary": {"sharpe": 68.0},
            "session_returns_bps": returns(-0.2),
        }, {
            "ast_fingerprint": "formula-c",
            "ast_shape_fingerprint": "shape-c",
            "summary": {"sharpe": 67.0},
            "session_returns_bps": mismatched,
        }],
    }

    multiple = _population_multiple_testing(report)

    assert multiple["spa_reality_check"]["valid"] is False
    assert multiple["spa_reality_check"]["reason"] == \
        "complete synchronous family session vectors unavailable"
    assert multiple["spa_reality_check"]["non_synchronous_candidates"] == [
        "AST:formula-c"]
    assert multiple["complete_synchronous_formula_vectors_available"] is False


def test_dsr_rejects_python_equal_but_differently_typed_session_keys() -> None:
    """``True == 1`` must not make two different session identities equal."""

    primary = {index: 1.0 + (index % 5) * 0.1 for index in range(60)}
    typed_mismatch = dict(primary)
    value = typed_mismatch.pop(1)
    typed_mismatch[True] = value
    report = {
        "summary": {"sessions": 60, "trials": 4, "sharpe": 70.0},
        "session_returns_bps": primary,
        "ast_shape_fingerprint": "shape-primary",
        "screening_population": [{
            "ast_fingerprint": f"formula-{index}",
            "ast_shape_fingerprint": f"shape-{index}",
            "summary": {"sharpe": 69.0 - index},
            "session_returns_bps": typed_mismatch if index == 2 else {
                session: value - (index + 1) * 0.1
                for session, value in primary.items()
            },
        } for index in range(3)],
    }

    multiple = _population_multiple_testing(report)

    assert multiple["observed_formula_trials"] == 3
    assert multiple["complete_synchronous_formula_vectors_available"] is False
    assert multiple["dsr_calibration_ready"] is False


def test_population_shares_exact_teacher_contract_without_changing_reports(
        monkeypatch) -> None:
    """Eight symbolic candidates fit/predict two teachers, byte-identically."""
    monkeypatch.setattr(supervised_module, "MIN_OBSERVATIONS", 4)
    spec = IntradayLaneSpec(
        horizons_seconds=(5, 30), order_latency_ms=0,
        fee_bps_per_side=1)
    rules = {
        "min_sessions": 1, "min_instruments": 1,
        "min_opportunities": 1, "min_deflated_sharpe": -100,
        "min_positive_session_ratio": 0,
    }

    def candidates() -> dict[str, CandidateAccumulator]:
        fields = (
            "microprice_offset_bps", "normalized_quote_ofi",
            "trade_flow_imbalance", "queue_imbalance_l1",
        )
        out = {}
        for index in range(8):
            field = {"op": "field", "field": fields[index % 4]}
            expression = (field if index % 2 == 0
                          else {"op": "neg", "arg": field})
            context = (() if index % 4 < 2 else
                       ("OPEN",) if index % 4 == 2 else ("CLOSE",))
            out[f"C{index}"] = CandidateAccumulator(
                expr=expression, spec=spec,
                horizon_seconds=5 if index < 4 else 30,
                execution="TAKER", family_pbo=0.2, trials=8,
                semantic_plan={"context": context} if context else None,
                criteria=rules)
        return out

    def both_horizons(sample: IntradaySample) -> IntradaySample:
        five = sample.labels[0]
        thirty = replace(
            five, horizon_seconds=30,
            exit_time=sample.decision_time + timedelta(seconds=30))
        return replace(sample, labels=(five, thirty))

    base = datetime(2026, 5, 18, tzinfo=timezone.utc)
    calibration = {
        instrument: [
            both_horizons(_sample(
                instrument, base + timedelta(seconds=5 * index),
                signal=(-0.75 + index * 0.5), net=(-1.0 + index)))
            for index in range(3)
        ]
        for instrument in ("A", "B")
    }
    evaluation = {
        instrument: [
            both_horizons(_sample(
                instrument, base + timedelta(days=1),
                signal=0.8, net=2.0)),
            both_horizons(_sample(
                instrument, base + timedelta(days=1, hours=5, minutes=50),
                signal=-0.6, net=-1.0)),
        ]
        for instrument in ("A", "B")
    }

    independent = candidates()
    expected = {}
    for key, candidate in independent.items():
        for instrument, rows in calibration.items():
            candidate.calibrate(instrument, rows)
        candidate.freeze_calibration()
        for instrument, rows in evaluation.items():
            candidate.add(instrument, rows)
        expected[key] = candidate.finish()

    calibration_calls = []
    prediction_calls = []
    original_calibrate = supervised_module.CostAwareTeacher.calibrate
    original_predict = supervised_module.CostAwareTeacher.predict

    def counted_calibrate(self, instrument_id, samples):
        calibration_calls.append((self.horizon_seconds, self.execution,
                                  str(instrument_id)))
        return original_calibrate(self, instrument_id, samples)

    def counted_predict(self, samples):
        prediction_calls.append((self.horizon_seconds, self.execution))
        return original_predict(self, samples)

    monkeypatch.setattr(
        supervised_module.CostAwareTeacher, "calibrate", counted_calibrate)
    monkeypatch.setattr(
        supervised_module.CostAwareTeacher, "predict", counted_predict)

    population = CandidatePopulationAccumulator(candidates())
    for instrument, rows in calibration.items():
        population.calibrate(instrument, rows)
    frozen = population.freeze_calibration()
    for instrument, rows in evaluation.items():
        population.add(instrument, rows)
    actual = population.finish()

    # Two exact contracts per instrument/slice, rather than eight candidates.
    assert len(calibration_calls) == 2 * len(calibration)
    assert {row[:2] for row in calibration_calls} == {
        (5, "TAKER"), (30, "TAKER")}
    assert len(prediction_calls) == 2 * len(evaluation)
    assert set(prediction_calls) == {(5, "TAKER"), (30, "TAKER")}

    # Followers are restored through the normal fingerprint verifier.  Every
    # candidate retains its own contextual teacher/hybrid/AST diagnostics.
    assert len({frozen[f"C{index}"]["supervised_control"][
        "model_fingerprint"] for index in range(4)}) == 1
    assert len({frozen[f"C{index}"]["supervised_control"][
        "model_fingerprint"] for index in range(4, 8)}) == 1
    canonical = lambda value: json.dumps(
        value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert canonical(actual) == canonical(expected)


def test_structural_control_reports_primary_minus_ablation_influence() -> None:
    primary_expr = {"op": "rolling_mean", "seconds": 10,
                    "arg": {"op": "field",
                            "field": "microprice_offset_bps"}}
    control = intraday_ablation.generate(primary_expr)[0]
    reports = {
        "PRIMARY": {
            "decision": "HOLD",
            "summary": {"mean_net_bps_per_opportunity": 1.2,
                        "mean_mid_markout_bps": 3.0,
                        "mean_implementation_drag_bps": 1.8,
                        "instrument_coverage": .8, "trials": 4}},
        control["ast_fingerprint"]: {
            "decision": "HOLD",
            "summary": {"mean_net_bps_per_opportunity": .5,
                        "mean_mid_markout_bps": 2.0,
                        "mean_implementation_drag_bps": 1.5,
                        "instrument_coverage": .7}},
    }
    annotated = _annotate_population({
        "intraday_signal_expr": primary_expr,
        "screening_population": [{
            **control, "candidate_role": "STRUCTURAL_ABLATION",
            "source_lead_ids": ["lead-primary"], "title": "control",
        }],
    }, reports)
    influence = annotated["screening_population"][0]["empirical_influence"]
    assert influence["net_increment_bps"] == pytest.approx(.7)
    assert influence["gross_increment_bps"] == pytest.approx(1.0)
    assert influence["implementation_drag_increment_bps"] == pytest.approx(.3)
    assert influence["interpretation"] == "POSITIVE_POINT_ESTIMATE"
    assert "not causal" in influence["evidence_warning"]


def test_empirical_term_influence_reaches_next_generation_memory() -> None:
    primary = {"op": "rolling_mean", "seconds": 10,
               "arg": {"op": "field", "field": "microprice_offset_bps"}}
    control = intraday_ablation.generate(primary)[0]
    memory = intraday_experience.build([{
        "intraday_signal_expr": control["intraday_signal_expr"],
        "semantic_plan": {
            "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
            "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
            "output": "TAKER_NET_PNL", "execution": "TAKER",
            "horizon_seconds": 5,
        },
        "decision": "SCREENING_ONLY", "evidence_tier": "SCREENING_ONLY",
        "candidate_role": "STRUCTURAL_ABLATION",
        "ablation_operator": control["ablation_operator"],
        "ablation_path": control["ablation_path"],
        "ablation_of_ast_fingerprint": control[
            "ablation_of_ast_fingerprint"],
        "oos_summary": {"mean_net_bps_per_opportunity": .5},
        "empirical_influence": {
            "ablation_operator": control["ablation_operator"],
            "ablation_path": control["ablation_path"],
            "ablation_of_ast_fingerprint": control[
                "ablation_of_ast_fingerprint"],
            "net_increment_bps": .7, "gross_increment_bps": 1.0,
            "implementation_drag_increment_bps": .3,
            "interpretation": "POSITIVE_POINT_ESTIMATE",
        },
    }])
    assert memory.empirical_term_influence[0]["net_increment_bps"] == \
        pytest.approx(.7)
    rendered = intraday_experience.render(memory)
    assert "empirical term influence" in rendered
    assert "primary-minus-control net=0.7bps" in rendered


def test_score_calibration_result_reaches_next_generation_memory() -> None:
    memory = intraday_experience.build([{
        "intraday_signal_expr": {
            "op": "field", "field": "normalized_quote_ofi"},
        "semantic_plan": {
            "event": "ORDER_FLOW", "context": ["ALL"],
            "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
            "output": "TAKER_NET_PNL", "execution": "TAKER",
            "horizon_seconds": 5,
        },
        "decision": "GATE_HOLD",
        "oos_summary": {"mean_net_bps_per_opportunity": -2.0},
        "score_calibration": {
            "status": "NON_POSITIVE_DIRECTIONAL_RELATION",
            "beta_bps_per_score_unit": 0.0,
            "observations": 1200,
        },
    }])
    history = memory.history[0]
    assert history["score_calibration_status"] == \
        "NON_POSITIVE_DIRECTIONAL_RELATION"
    assert history["score_calibration_observations"] == 1200
    rendered = intraday_experience.render(memory)
    assert "calibration=NON_POSITIVE_DIRECTIONAL_RELATION" in rendered


def test_cost_hurdle_abstains_until_predicted_markout_clears_execution() -> None:
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    samples = [
        _sample("A", base, signal=3.9, net=2.0),
        _sample("A", base + timedelta(seconds=10), signal=4.1, net=2.0),
    ]
    spec = IntradayLaneSpec(
        sample_interval_seconds=5, feature_lookback_seconds=30,
        horizons_seconds=(5,), order_latency_ms=0,
        max_quote_age_seconds=5, fee_bps_per_side=1,
        maker_fee_bps_per_side=1)
    report = evaluate_candidate(
        {"A": samples},
        expr={"op": "field", "field": "microprice_offset_bps"},
        spec=spec, horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST",
        family_pbo=0.2,
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1, "min_deflated_sharpe": -100,
                  "min_positive_session_ratio": 0})
    # Current spread is 2bp and round-trip fees are 2bp, so only 4.1bp clears.
    assert report["summary"]["opportunities"] == 1
    assert report["lane_manifest"]["entry_policy"] == \
        "PREDICTED_MARKOUT_CLEARS_COST"

    # Legacy/in-memory samples may not carry the optional stored execution
    # spread.  Derive it from the latency-time entry quote, never from the
    # narrower decision-time feature spread.
    widened_entry = replace(
        _sample("A", base, signal=8.0, net=2.0),
        spread_bps=2.0, entry_bid=99.95, entry_ask=100.05,
        entry_mid=100.0, execution_spread_bps=None)
    widened_report = evaluate_candidate(
        {"A": [widened_entry]},
        expr={"op": "field", "field": "microprice_offset_bps"},
        spec=spec, horizon_seconds=5, execution="TAKER",
        entry_policy="PREDICTED_MARKOUT_CLEARS_COST", family_pbo=0.2,
        criteria={"min_sessions": 1, "min_instruments": 1,
                  "min_opportunities": 1, "min_deflated_sharpe": -100,
                  "min_positive_session_ratio": 0})
    assert widened_report["summary"]["opportunities"] == 0


def test_candidate_calibration_rejects_failed_causality_before_fitting() -> None:
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    invalid = replace(
        _sample("A", base, signal=1.0, net=2.0),
        source_quote_event_time=base + timedelta(seconds=1))
    candidate = CandidateAccumulator(
        expr={"op": "field", "field": "microprice_offset_bps"},
        spec=IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0),
        horizon_seconds=5, execution="TAKER")

    with pytest.raises(ValueError, match="calibration sample causality audit failed"):
        candidate.calibrate("A", [invalid])
    assert candidate.calibration_observations == 0
    assert candidate.teacher.report()["observations"] == 0


def test_population_calibration_rejects_failed_causality_before_fanout() -> None:
    base = datetime(2026, 1, 2, tzinfo=timezone.utc)
    invalid = replace(
        _sample("A", base, signal=1.0, net=2.0),
        source_quote_event_time=base + timedelta(seconds=1))
    spec = IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0)
    candidates = {
        key: CandidateAccumulator(
            expr={"op": "field", "field": field}, spec=spec,
            horizon_seconds=5, execution="TAKER")
        for key, field in {
            "PRIMARY": "microprice_offset_bps",
            "CONTROL": "queue_imbalance_l1",
        }.items()
    }
    population = CandidatePopulationAccumulator(candidates)

    with pytest.raises(ValueError, match="calibration sample causality audit failed"):
        population.calibrate("A", [invalid])
    assert all(candidate.calibration_observations == 0
               for candidate in candidates.values())
    assert all(candidate.teacher.report()["observations"] == 0
               for candidate in candidates.values())


def test_capacity_bottom_k_is_independent_of_shard_arrival_order() -> None:
    forward = _CapacityReservoir(limit=100)
    reverse = _CapacityReservoir(limit=100)
    rows = [(f"instrument-{index % 17}|{index}", float(index % 113))
            for index in range(2_000)]
    for key, value in rows:
        forward.add(value, key)
    for key, value in reversed(rows):
        reverse.add(value, key)
    assert sorted(forward.values) == sorted(reverse.values)
    assert forward.quantile(0.10) == reverse.quantile(0.10)


def test_data_feasibility_probe_is_persisted_outside_experiment_ledger() -> None:
    class Cursor:
        def __init__(self, conn): self.conn = conn
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params): self.conn.executed.append((sql, params))
        def fetchone(self): return ("check-1",)

    class Conn:
        def __init__(self): self.executed, self.commits = [], 0
        def cursor(self): return Cursor(self)
        def commit(self): self.commits += 1

    conn = Conn()
    result = record_data_feasibility(conn, "hyp-1", {
        "cutoff": datetime(2026, 8, 16, tzinfo=timezone.utc),
        "selected": {
            "status": "INSUFFICIENT_SESSIONS", "sessions": [],
            "instruments": [], "causal_sessions_available": 1,
        },
    })

    sql, params = conn.executed[0]
    assert result["status"] == "NEEDS_DATA"
    assert "quant.data_feasibility_checks" in sql
    assert "quant.experiments" not in sql
    assert len(params[2]) == 64
    assert conn.commits == 1


def test_feasibility_schema_and_autopilot_retry_do_not_spend_trials() -> None:
    migration = (ROOT / "supabase" / "migrations" /
                 "20260816180000_intraday_data_feasibility.sql").read_text(
                     encoding="utf-8")
    autopilot = (ROOT / "departments" / "01-research" / "factory" /
                 "factory_autopilot.py").read_text(encoding="utf-8")
    assert "quant.data_feasibility_checks" in migration
    assert "references quant.experiments" not in migration
    assert "must not count toward trial pressure, DSR, or PBO" in migration
    assert "f.status = 'NEEDS_DATA'" in autopilot
    assert "interval '1 hour'" in autopilot


def test_intraday_outcomes_become_creative_search_memory() -> None:
    proposal = _intraday_proposal()
    memory = intraday_experience.build([{
        "intraday_signal_expr": proposal["suggested_params"]["intraday_signal_expr"],
        "semantic_plan": proposal["semantic_plan"], "decision": "GATE_HOLD",
        "lesson_codes": ["COST_SENSITIVE"],
        "oos_summary": {"mean_net_bps_per_opportunity": -0.4,
                        "fill_rate": 0.7, "sessions": 60},
    }])
    text = intraday_experience.render(memory)
    assert memory.experiments == 1 and memory.semantic_families == 1
    assert "COST_SENSITIVE" in text
    assert "underexplored events" in text
    assert "implementation_drag=" in text
    assert "숫자 horizon만 바꾼 것은 새 아이디어가 아니다" in text


def test_repeated_losing_subtrees_become_soft_search_memory() -> None:
    shared = {"op": "mul", "args": [
        {"op": "rolling_mean", "seconds": 30,
         "arg": {"op": "field", "field": "trade_flow_imbalance"}},
        {"op": "field", "field": "realized_volatility_bps"},
    ]}
    gated = {
        "op": "where",
        "condition": {"op": "lt", "args": [
            {"op": "field", "field": "spread_bps"},
            {"const": 5, "unit": "BPS"}]},
        "then": shared, "else": {"const": 0, "unit": "BPS"},
    }
    base_plan = {
        "event": "ORDER_FLOW", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    gated_plan = {**base_plan, "context": ["TIGHT_SPREAD"],
                  "qualities": ["PERSISTENCE", "STATE_CONDITIONAL"]}
    memory = intraday_experience.build([
        {"intraday_signal_expr": shared, "semantic_plan": base_plan,
         "decision": "GATE_HOLD", "evidence_tier": "PRIMARY",
         "oos_summary": {"mean_net_bps_per_opportunity": -2.0}},
        {"intraday_signal_expr": gated, "semantic_plan": gated_plan,
         "decision": "GATE_HOLD", "evidence_tier": "PRIMARY",
         "oos_summary": {"mean_net_bps_per_opportunity": -1.0}},
    ])
    assert memory.frequent_losing_subtrees
    repeated = memory.frequent_losing_subtrees[0]
    assert repeated["losing_support"] == 2
    assert repeated["positive_support"] == 0
    rendered = intraday_experience.render(memory)
    assert "frequent losing subtrees" in rendered
    assert "soft search prior" in rendered


def test_reusable_term_bank_keeps_evidence_tiers_and_executable_ast() -> None:
    term = {"op": "rolling_mean", "seconds": 30,
            "arg": {"op": "field", "field": "trade_flow_imbalance"}}
    positive = {"op": "mul", "args": [
        term, {"op": "field", "field": "realized_volatility_bps"}]}
    negative = {"op": "add", "args": [
        term, {"op": "field", "field": "queue_imbalance_l1"}]}
    plan = {
        "event": "ORDER_FLOW", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    memory = intraday_experience.build([
        {"intraday_signal_expr": positive, "semantic_plan": plan,
         "decision": "SUBMIT_TO_QA", "evidence_tier": "PRIMARY",
         "oos_summary": {
             "mean_net_bps_per_opportunity": 0.4,
             "qa_reproduction": {"status": "PASS"},
         }},
        {"intraday_signal_expr": negative, "semantic_plan": plan,
         "decision": "GATE_HOLD", "evidence_tier": "PRIMARY",
         "oos_summary": {"mean_net_bps_per_opportunity": -0.8}},
    ])

    reused = next(row for row in memory.reusable_term_bank
                  if row["term_ast"] == term)
    assert reused["status"] == "PRIMARY_POSITIVE_ASSOCIATION"
    assert reused["formula_support"] == 2
    assert reused["primary_positive_support"] == 1
    assert reused["primary_negative_support"] == 1
    assert reused["unit"] == "RATIO"
    assert reused["search_action"] == "SET_LEVEL_REUSE_WITH_ABLATION"
    rendered = intraday_experience.render(memory)
    assert "typed reusable term bank" in rendered
    assert "no causal credit" in rendered


def test_qa_gated_outcomes_enter_memory_only_after_terminal_verdict() -> None:
    from intraday_ast_contract import fingerprint as intraday_fingerprint

    base_plan = {
        "event": "ORDER_FLOW", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }

    def row(field: str, decision: str, net: float, **extra) -> dict:
        return {
            "intraday_signal_expr": {
                "op": "rolling_mean", "seconds": 10,
                "arg": {"op": "field", "field": field},
            },
            "semantic_plan": extra.pop("semantic_plan", base_plan),
            "decision": decision,
            "oos_summary": {
                "mean_net_bps_per_opportunity": net,
                **extra.pop("summary", {}),
            },
            **extra,
        }

    pending = row(
        "queue_imbalance_l10", "BLOCKED", 9.0,
        summary={"qa_reproduction": {"status": "PENDING"}})
    inconclusive = row(
        "normalized_quote_ofi", "BLOCKED", -9.0,
        summary={"qa_reproduction": {"status": "INCONCLUSIVE"}})
    unverified_submission = row(
        "realized_volatility_bps", "SUBMIT_TO_QA", 8.0)
    legacy_boolean_pending = row(
        "microprice_offset_bps", "PROMOTED", 7.0,
        qa_verified=False)
    verified_pass = row(
        "microprice_offset_bps", "SUBMIT_TO_QA", 0.5,
        summary={"qa_reproduction": {"status": "PASS"}})
    verified_fail = row(
        "queue_imbalance_l1", "REJECT", 6.0,
        summary={"qa_reproduction": {"status": "FAIL"}},
        semantic_plan={**base_plan, "context": ["CLOSE"]})
    legacy_non_forward = row(
        "trade_flow_imbalance", "GATE_HOLD", -0.3,
        semantic_plan={**base_plan, "context": ["OPEN"]})

    memory = intraday_experience.build([
        pending, inconclusive, unverified_submission,
        legacy_boolean_pending, verified_pass, verified_fail,
        legacy_non_forward,
    ], [{
        "lead_id": "qa-failed-child",
        "intraday_signal_expr": verified_fail["intraday_signal_expr"],
        "semantic_plan": verified_fail["semantic_plan"],
        "parent_ast_fingerprint": intraday_fingerprint(
            verified_pass["intraday_signal_expr"]),
        "used": True,
    }])

    # The forward candidates awaiting authority disappear from every
    # prompt-facing evolutionary archive; legacy outcomes that never required
    # QA remain valid memory.
    assert memory.experiments == 3
    assert {item[0] for item in memory.positive_components} == {
        "field:microprice_offset_bps", "op:field", "op:rolling_mean"}
    negative = dict(memory.negative_components)
    assert negative["field:queue_imbalance_l1"] == 1
    assert negative["field:trade_flow_imbalance"] == 1
    assert "field:queue_imbalance_l10" not in negative
    assert "field:normalized_quote_ofi" not in negative
    assert {item["qa_memory_status"] for item in memory.history} == {
        "NOT_REQUIRED", "PASS", "FAIL"}
    failed = next(item for item in memory.history
                  if item["qa_memory_status"] == "FAIL")
    assert failed["best_net_bps"] is None
    assert {item["breeding_role"] for item in memory.breeding_parents} == {
        "NET_SURVIVOR", "FAILURE_INVERSION_PARENT"}
    tournament = memory.lineage_tournaments[0]
    assert tournament["parent_net_bps"] == pytest.approx(0.5)
    assert tournament["child_net_bps"] is None
    assert tournament["net_increment_bps"] is None
    assert tournament["status"] == "NO_COMPARABLE_NET_METRIC"

    assert intraday_experience._qa_memory_status({
        "decision": "PROMOTED", "qa_verified": True}) == "PASS"
    assert intraday_experience._qa_memory_status({
        "decision": "BLOCKED"}) == "WITHHELD"
    assert intraday_experience._qa_memory_status({
        "decision": "GATE_HOLD"}) == "NOT_REQUIRED"


def test_generation_arm_audit_is_balanced_but_never_promotes() -> None:
    plan = {
        "event": "ORDER_FLOW", "context": ["ALL"],
        "qualities": ["LEVEL"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    expressions = [
        {"op": "field", "field": "trade_flow_imbalance"},
        {"op": "field", "field": "normalized_quote_ofi"},
        {"op": "rolling_mean", "seconds": 30,
         "arg": {"op": "field", "field": "trade_flow_imbalance"}},
        {"op": "neg", "arg": {
            "op": "field", "field": "microprice_offset_bps"}},
    ]
    rows = [
        {"intraday_signal_expr": expr, "semantic_plan": plan,
         "decision": "GATE_HOLD",
         "oos_summary": {"mean_net_bps_per_opportunity": net}}
        for expr, net in zip(expressions, (0.2, -0.1, 0.4, -0.3))
    ]
    leads = [
        {"lead_id": f"lead-{index}", "intraday_signal_expr": expr,
         "semantic_plan": plan,
         "evolution_role": "SEED" if index < 2 else "CHILD"}
        for index, expr in enumerate(expressions)
    ]
    audit = intraday_experience.build(rows, leads).generation_arm_audit

    assert audit["status"] == "TOO_FEW_PER_ARM"
    assert audit["matched_net_sample_size"] == 2
    assert audit["arms"]["FRESH"]["net_measured_unique"] == 2
    assert audit["arms"]["LINEAGE"]["net_measured_unique"] == 2
    assert audit["promotion_authority"] is False
    rendered = intraday_experience.render(
        intraday_experience.build(rows, leads))
    assert "Keep the 6/6 LLM-call budget fixed" in rendered


def test_recurring_untested_terms_are_moved_to_saturation_queue() -> None:
    term = {"op": "rolling_mean", "seconds": 30,
            "arg": {"op": "field", "field": "trade_flow_imbalance"}}
    plan = {
        "event": "ORDER_FLOW", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    leads = []
    for index, other in enumerate(("queue_imbalance_l1",
                                   "normalized_quote_ofi")):
        leads.append({
            "lead_id": f"repeat-{index}", "semantic_plan": plan,
            "intraday_signal_expr": {"op": "add", "args": [
                term, {"op": "field", "field": other}]},
        })
    memory = intraday_experience.build([], leads)
    repeated = next(row for row in memory.reusable_term_bank
                    if row["term_ast"] == term)

    assert repeated["status"] == "RECURRING_UNTESTED"
    assert repeated["search_action"] == "EVALUATE_OR_STOP_REPROPOSING"
    rendered = intraday_experience.render(memory)
    assert "term saturation/avoidance queue" in rendered
    assert "do not treat as preferred material" in rendered


def test_residual_qd_elite_reaches_next_generation_memory_without_authority() -> None:
    plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["LEVEL"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    memory = intraday_experience.build([{
        "intraday_signal_expr": {
            "op": "field", "field": "microprice_offset_bps"},
        "semantic_plan": plan, "decision": "SCREENING_ONLY",
        "evidence_tier": "SCREENING_ONLY",
        "oos_summary": {"mean_net_bps_per_opportunity": -3.0},
        "residual_behavior": {
            "status": "PASS", "worst_time_bucket": "CLOSE",
            "median_time_bucket_mae_bps": 1.2, "rmse_bps": 1.8,
            "median_time_bucket_mae_improvement_vs_null_bps": 0.4,
            "promotion_authority": False,
        },
        "residual_qd": {
            "status": "ELIGIBLE", "cell": "CLOSE/NODES_1_5",
            "elite": True, "promotion_authority": False,
        },
    }])

    assert memory.residual_qd_elites[0]["cell"] == "CLOSE/NODES_1_5"
    assert memory.history[0]["worst_residual_time_bucket"] == "CLOSE"
    rendered = intraday_experience.render(memory)
    assert "residual-behavior QD elites" in rendered
    assert "mae_gain_vs_null=0.4bps" in rendered
    assert "OOS diagnostic only, no promotion" in rendered
    assert "forward sessions unseen by its lineage" in rendered


def test_intraday_feedback_separates_bad_signal_from_cost_flip() -> None:
    bad_signal = lessons_from(
        failed_criteria=["NET_EDGE_NOT_POSITIVE"],
        oos_summary={"mean_mid_markout_bps": -1.4,
                     "mean_net_bps_per_opportunity": -40.5})
    cost_flip = lessons_from(
        failed_criteria=["NET_EDGE_NOT_POSITIVE"],
        oos_summary={"mean_mid_markout_bps": 2.0,
                     "mean_net_bps_per_opportunity": -1.0})
    assert "BASELINE_NOT_BEATEN" in bad_signal
    assert "COST_SENSITIVE" not in bad_signal
    assert "COST_SENSITIVE" in cost_flip


def test_scout_can_submit_raw_event_time_literature_lead() -> None:
    plan = {"event": "ORDER_FLOW", "context": ["ALL"],
            "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
            "output": "TAKER_NET_PNL", "execution": "TAKER",
            "horizon_seconds": 5}
    baseline = {"op": "field", "field": "trade_flow_imbalance"}
    candidate = {"op": "mul", "args": [
        {"op": "rolling_mean", "seconds": 30, "arg": baseline},
        {"op": "field", "field": "realized_volatility_bps"}]}
    lead = lead_intake.to_lead({
        "TITLE": "Persistent event-time order flow", "URL": "https://example.com/paper",
        "MECHANISM": "urgent takers create persistent order flow and short markout",
        "TESTABLE_WITH": "trade_flow_imbalance rolling_mean 30 seconds",
        "READINESS": "AST_READY", "RESEARCH_LANE": "INTRADAY_EVENT",
        "SEMANTIC_PLAN": plan,
        "OBSERVABLES": ["trade_flow_imbalance", "realized_volatility_bps"],
        "SOURCE_BASELINE_EXPR": baseline, "CANDIDATE_SIGNAL_EXPR": candidate,
        "DERIVATION_MODE": "MECHANISM_MUTATION",
        "DERIVATION_TRANSFORMS": ["CLOCK_CHANGE"],
        "NOVELTY_RATIONALE": "Test persistence duration on the local KRX event clock.",
        "FORMULA_THESIS": {
            "target": "TAKER_NET_PNL", "functional_form": "MONOTONE",
            "expected_sign": "POSITIVE", "coefficient_policy": "STRUCTURE_ONLY",
            "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
            "terms": {"trade_flow_imbalance": "PRESSURE",
                      "realized_volatility_bps": "VOLATILITY"},
            "identification": "Persistent signed pressure must predict positive net markout.",
        },
    }, lens="ACADEMIC", source_type="PAPER", case_id="case-1",
       model_version="test-model", prompt_version="test-prompt")
    assert lead["ast_contract"]["research_lane"] == "INTRADAY_EVENT"
    assert lead["ast_contract"]["semantic_plan"]["event"] == "ORDER_FLOW"


def test_intraday_shadow_progression_requires_execution_calibration() -> None:
    missing = evaluate_promotion(
        "MICRO-1", current_state="SHADOW", gate_decision="SUBMIT_TO_QA",
        observed_days=25, research_lane="INTRADAY_EVENT")
    assert not missing.approved_by_quant
    assert any("execution evidence missing" in reason for reason in missing.reasons)

    calibrated = evaluate_promotion(
        "MICRO-1", current_state="SHADOW", gate_decision="SUBMIT_TO_QA",
        observed_days=25, research_lane="INTRADAY_EVENT",
        execution_evidence={"observed_events": 2000, "mean_live_net_bps": 0.3,
                            "latency_p95_ms": 180, "registered_latency_ms": 250,
                            "execution": "PASSIVE_FIFO_LOWER_BOUND",
                            "fill_calibration_mae": 0.08})
    assert calibrated.approved_by_quant and calibrated.to_state == "PAPER"


def _evolution_plan(*, context: str = "ALL", horizon: int = 5) -> dict:
    return {
        "event": "ORDER_FLOW", "context": [context],
        "qualities": (["PERSISTENCE"] if context == "ALL"
                      else ["PERSISTENCE", "STATE_CONDITIONAL"]),
        "direction": "FOLLOW", "output": "TAKER_NET_PNL",
        "execution": "TAKER", "horizon_seconds": horizon,
    }


def test_intraday_evolution_contract_requires_economic_child_and_ablations() -> None:
    parent = {"op": "rolling_mean", "arg": {
        "op": "field", "field": "trade_flow_imbalance"}, "seconds": 30}
    child = {"op": "where", "condition": {"op": "gt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"}]}, "then": {
            "op": "mul", "args": [
                parent, {"op": "field", "field": "realized_volatility_bps"}]},
        "else": {"const": 0, "unit": "BPS"}}
    lead = lead_intake.to_lead({
        "TITLE": "Wide-spread persistent order flow",
        "URL": "https://example.com/evolution-paper",
        "MECHANISM": "Urgent takers persist when spread is wide and liquidity is costly.",
        "TESTABLE_WITH": "wide spread_bps gates rolling trade_flow_imbalance",
        "READINESS": "AST_READY", "RESEARCH_LANE": "INTRADAY_EVENT",
        "SEMANTIC_PLAN": _evolution_plan(context="WIDE_SPREAD"),
        "OBSERVABLES": ["spread_bps", "trade_flow_imbalance",
                        "realized_volatility_bps"],
        "SOURCE_BASELINE_EXPR": parent, "CANDIDATE_SIGNAL_EXPR": child,
        "DERIVATION_MODE": "MECHANISM_MUTATION",
        "DERIVATION_TRANSFORMS": ["STATE_CONDITION"],
        "NOVELTY_RATIONALE": "Condition the public persistence mechanism on liquidity cost.",
        "PARENT_SIGNAL_EXPR": parent,
        "EVOLUTION_OPERATORS": ["STATE_CONDITION", "MECHANISM_INTERACTION"],
        "EXPECTED_INCREMENT": "The gate should improve net markout after spread cost.",
        "ABLATIONS": ["remove spread gate", "reverse spread gate"],
        "FORMULA_THESIS": {
            "target": "TAKER_NET_PNL", "functional_form": "STATE_CONDITIONAL",
            "expected_sign": "STATE_DEPENDENT",
            "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
            "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
            "terms": {"spread_bps": "LIQUIDITY",
                      "trade_flow_imbalance": "PRESSURE",
                      "realized_volatility_bps": "VOLATILITY"},
            "identification": "Order-flow pressure must add net markout only in wide spreads.",
        },
    }, lens="ACADEMIC", source_type="PAPER", case_id="evolution-case",
       model_version="test-model", prompt_version="evolution-v1")

    contract = lead["ast_contract"]
    assert contract["evolution_role"] == "CHILD"
    assert contract["evolution_operators"] == [
        "MECHANISM_INTERACTION", "STATE_CONDITION"]
    assert len(contract["parent_ast_fingerprint"]) == 16

    tuned = {**parent, "seconds": 60}
    with pytest.raises(ValueError, match="tunable parameters"):
        lead_intake.to_lead({
            "TITLE": "Clock-only child", "URL": "https://example.com/clock",
            "MECHANISM": "Persistent urgent order flow.",
            "TESTABLE_WITH": "trade_flow_imbalance rolling mean",
            "READINESS": "AST_READY", "RESEARCH_LANE": "INTRADAY_EVENT",
            "SEMANTIC_PLAN": _evolution_plan(),
            "OBSERVABLES": ["trade_flow_imbalance"],
            "SOURCE_BASELINE_EXPR": parent, "CANDIDATE_SIGNAL_EXPR": tuned,
            "DERIVATION_MODE": "MECHANISM_MUTATION",
            "DERIVATION_TRANSFORMS": ["CLOCK_CHANGE"],
            "NOVELTY_RATIONALE": "A slower clock.",
            "PARENT_SIGNAL_EXPR": parent, "EVOLUTION_OPERATORS": ["CLOCK_CHANGE"],
            "EXPECTED_INCREMENT": "Longer persistence.", "ABLATIONS": ["30 seconds"],
        }, lens="ACADEMIC", source_type="PAPER", case_id="evolution-case",
           model_version="test-model", prompt_version="evolution-v1")


def test_intraday_quality_diversity_archive_keeps_one_elite_per_niche() -> None:
    base = {"op": "rolling_mean", "arg": {
        "op": "field", "field": "trade_flow_imbalance"}, "seconds": 30}
    tested = [{"intraday_signal_expr": base, "semantic_plan": _evolution_plan(),
               "decision": "GATE_HOLD", "lesson_codes": ["UNDERPOWERED_DATA"],
               "oos_summary": {"sessions": 3}}]
    child_a = {"op": "where", "condition": {"op": "gt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"}]}, "then": base,
        "else": {"const": 0, "unit": "RATIO"}}
    child_b = {"op": "where", "condition": {"op": "gt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 10, "unit": "BPS"}]}, "then": base,
        "else": {"const": 0, "unit": "RATIO"}}
    common = {
        "semantic_plan": _evolution_plan(context="WIDE_SPREAD"), "used": False,
        "alpha_candidate_eligible": True, "candidate_vs_source_similarity": 0.5,
        "evolution_role": "CHILD", "evolution_operators": ["STATE_CONDITION"],
        "expected_increment": "cost-conditioned markout",
        "ablations": ["remove spread gate"],
    }
    memory = intraday_experience.build(tested, [
        {**common, "lead_id": "lead-a", "intraday_signal_expr": child_a},
        {**common, "lead_id": "lead-b", "intraday_signal_expr": child_b},
        {**common, "lead_id": "recycled", "intraday_signal_expr": base,
         "semantic_plan": _evolution_plan()},
    ])

    assert memory.candidate_population == 3
    assert len(memory.niche_elites) == 1
    assert memory.niche_elites[0]["niche_competitors"] == 2
    assert memory.niche_elites[0]["lineage_complete"] is True
    assert [row["lead_id"] for row in memory.recycled_candidates] == ["recycled"]
    rendered = intraday_experience.render(memory)
    assert "target 12 drafts" in rendered
    assert "DSR/PBO" in rendered


@pytest.mark.parametrize(
    ("target", "execution", "decision_rule"),
    [
        ("MIDPRICE_MARKOUT", "TAKER", "POSITIVE_SCORE"),
        ("PASSIVE_FILL_ADJUSTED_PNL", "PASSIVE_FIFO_LOWER_BOUND",
         "PREDICTED_MARKOUT_CLEARS_COST"),
    ],
)
def test_formula_thesis_uses_canonical_semantic_targets(
        target: str, execution: str, decision_rule: str) -> None:
    expr = {"op": "field", "field": "microprice_offset_bps"}
    result = formula_discovery.assess({
        "target": target,
        "functional_form": "MONOTONE",
        "expected_sign": "POSITIVE",
        "coefficient_policy": "STRUCTURE_ONLY",
        "decision_rule": decision_rule,
        "terms": {"microprice_offset_bps": "PRESSURE"},
        "identification": (
            "Microprice displacement must predict the signed future markout."),
    }, candidate=expr, semantic_plan={
        "output": target, "execution": execution,
    }, grammar=intraday_grammar)
    assert result["formula_contract_complete"] is True
    assert result["formula_thesis"]["target"] == target

    legacy = "PASSIVE_NET_PNL" if target.startswith("PASSIVE") \
        else "MID_MARKOUT_BPS"
    with pytest.raises(ValueError, match="is not controlled"):
        formula_discovery.assess({
            "target": legacy,
            "functional_form": "MONOTONE",
            "expected_sign": "POSITIVE",
            "coefficient_policy": "STRUCTURE_ONLY",
            "decision_rule": decision_rule,
            "terms": {"microprice_offset_bps": "PRESSURE"},
            "identification": (
                "Microprice displacement must predict the signed future markout."),
        }, candidate=expr, semantic_plan={
            "output": legacy, "execution": execution,
        }, grammar=intraday_grammar)


def test_parent_child_tournament_requires_net_increment_and_gate_survival() -> None:
    from intraday_ast_contract import fingerprint as intraday_fingerprint

    parent = {"op": "rolling_mean", "arg": {
        "op": "field", "field": "trade_flow_imbalance"}, "seconds": 30}
    child = {"op": "where", "condition": {"op": "gt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"}]}, "then": parent,
        "else": {"const": 0, "unit": "RATIO"}}
    rows = [
        {"intraday_signal_expr": parent, "semantic_plan": _evolution_plan(),
         "decision": "SUBMIT_TO_QA", "lesson_codes": [],
         "oos_summary": {"mean_net_bps_per_opportunity": 0.2, "sessions": 30,
                         "qa_reproduction": {"status": "PASS"}}},
        {"intraday_signal_expr": child,
         "semantic_plan": _evolution_plan(context="WIDE_SPREAD"),
         "decision": "SUBMIT_TO_QA", "lesson_codes": [],
         "oos_summary": {"mean_net_bps_per_opportunity": 0.5, "sessions": 30,
                         "qa_reproduction": {"status": "PASS"}}},
    ]
    memory = intraday_experience.build(rows, [{
        "lead_id": "child", "intraday_signal_expr": child,
        "semantic_plan": _evolution_plan(context="WIDE_SPREAD"), "used": True,
        "parent_ast_fingerprint": intraday_fingerprint(parent),
    }])

    assert memory.lineage_tournaments[0]["status"] == "CHILD_SURVIVES"
    assert memory.lineage_tournaments[0]["net_increment_bps"] == pytest.approx(0.3)
    assert len(memory.breeding_parents) == 2


def test_measured_negative_elites_become_controlled_stepping_stones() -> None:
    gross_but_costly = {
        "intraday_signal_expr": {"op": "field", "field": "microprice_offset_bps"},
        "semantic_plan": _evolution_plan(), "decision": "GATE_HOLD",
        "lesson_codes": ["COST_SENSITIVE"],
        "oos_summary": {"mean_mid_markout_bps": 2.0,
                        "mean_net_bps_per_opportunity": -20.0,
                        "sessions": 2},
    }
    wrong_direction = {
        "intraday_signal_expr": {"op": "neg", "arg": {
            "op": "field", "field": "microprice_offset_bps"}},
        "semantic_plan": _evolution_plan(context="OPEN"),
        "decision": "GATE_HOLD", "lesson_codes": ["BASELINE_NOT_BEATEN"],
        "oos_summary": {"mean_mid_markout_bps": -1.0,
                        "mean_net_bps_per_opportunity": -23.0,
                        "sessions": 2},
    }
    memory = intraday_experience.build([gross_but_costly, wrong_direction])
    roles = {row["breeding_role"]: row for row in memory.breeding_parents}
    assert roles["COST_STEPPING_STONE"]["allowed_child_operators"] == [
        "EXECUTION_AWARE", "STATE_CONDITION", "TARGET_CHANGE"]
    assert roles["FAILURE_INVERSION_PARENT"]["allowed_child_operators"] == [
        "FAILURE_MODE_INVERSION"]
    assert all(row["breeding_role"] != "NET_SURVIVOR"
               for row in memory.breeding_parents)


def test_positive_screening_evidence_breeds_but_still_allows_confirmation() -> None:
    expr = {"op": "where",
            "condition": {"op": "gt", "args": [
                {"op": "field", "field": "spread_bps"},
                {"const": 2, "unit": "BPS"}]},
            "then": {"op": "field", "field": "microprice_offset_bps"},
            "else": {"const": 0, "unit": "BPS"}}
    plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["LEVEL"], "direction": "REVERT",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    screened = [{
        "intraday_signal_expr": expr, "semantic_plan": plan,
        "decision": "SCREENING_ONLY", "evidence_tier": "SCREENING_ONLY",
        "candidate_role": "STRUCTURAL_ABLATION",
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
        "coefficient_policy": "STRUCTURE_ONLY",
        "lesson_codes": [],
        "oos_summary": {"mean_mid_markout_bps": 25.0,
                         "mean_net_bps_per_opportunity": 2.0,
                        "opportunities": 26, "sessions": 4},
    }]
    lead = {
        "lead_id": "confirm-me", "intraday_signal_expr": expr,
        "semantic_plan": plan, "used": False,
        "alpha_candidate_eligible": True,
        "formula_discovery_version": "formula-discovery-v5",
        "formula_contract_complete": True,
    }
    memory = intraday_experience.build(screened, [lead])

    assert not memory.recycled_candidates
    assert [row["lead_id"] for row in memory.niche_elites] == ["confirm-me"]
    assert memory.breeding_parents[0]["breeding_role"] == "SCREEN_SURVIVOR"
    rendered = intraday_experience.render(memory)
    assert "SCREEN_SURVIVOR still needs" in rendered
    encoded = next(line.split("=", 1)[1] for line in rendered.splitlines()
                   if "SCREEN_SURVIVOR_JSON=" in line)
    payload = json.loads(encoded)
    assert payload["intraday_signal_expr"] == expr
    assert payload["semantic_plan"] == plan
    assert payload["execution_contract"] == {
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
        "coefficient_policy": "STRUCTURE_ONLY"}
    assert payload["candidate_role"] == "STRUCTURAL_ABLATION"
    assert payload["evidence"]["opportunities"] == 26
    assert payload["evidence"]["sessions"] == 4
    assert "adaptive same-replay" in payload["evidence"]["caveat"]
    assert payload["authority"] == {
        "promotion_authority": False,
        "independent_primary_required": True,
        "forward_new_sessions_required": True,
    }
    assert len(encoded) <= intraday_experience.SCREEN_SURVIVOR_JSON_MAX_CHARS

    crowded = intraday_experience.build([
        {**screened[0], "semantic_plan": {**plan, "context": [context]}}
        for context in ("ALL", "OPEN", "CLOSE", "WIDE_SPREAD")])
    crowded_text = intraday_experience.render(crowded)
    assert crowded_text.count("SCREEN_SURVIVOR_JSON=") == \
        intraday_experience.SCREEN_SURVIVOR_RENDER_LIMIT
    assert "1 additional screening survivors omitted" in crowded_text


def test_previous_formula_contract_does_not_occupy_live_niche() -> None:
    expr = {"op": "field", "field": "microprice_offset_bps"}
    plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["LEVEL"], "direction": "REVERT",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    stale = {
        "lead_id": "old-validator-pass", "intraday_signal_expr": expr,
        "semantic_plan": plan, "used": False,
        "alpha_candidate_eligible": True,
        "formula_discovery_version": "formula-discovery-v3",
        "formula_contract_complete": True,
    }
    memory = intraday_experience.build([], [stale])
    assert memory.niche_elites == ()
