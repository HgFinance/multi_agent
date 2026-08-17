from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
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
import formula_discovery
import intraday_alpha_ast as intraday_grammar
import intraday_candidate as candidate_module
import intraday_experience
import intraday_ablation
import lead_intake
from factory_contracts import MethodologyLeadV1
from intraday_alpha_ast import (IntradayExprError, evaluate, fields_of, parse,
                                shape_fingerprint, unit_of)
from intraday_candidate import (_CapacityReservoir, CandidateAccumulator,
                                CandidatePopulationAccumulator,
                                evaluate_candidate)
from factory_bridge import (_SQL_FAMILY_OR_EXACT_TRIALS, _normalized_formula,
                            count_family_trials, expected_edge_for, gate0)
from factory_bridge import lessons_from
from intraday_experiment_runner import (StaleIntradayCohortError,
                                        FAST_SCREEN_VERSION,
                                        _annotate_population, _input_hash,
                                        _fast_screen_gate, _stratified,
                                        _load_completed_report, config_from_edge,
                                        record_data_feasibility, select_slice)
from intraday_microstructure import (HorizonLabel, IntradayLaneSpec,
                                      IntradaySample, EXTERNAL_EVENT_SOURCE)
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


def test_candidate_requires_session_level_evidence_and_can_submit_only_to_qa() -> None:
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
    assert report["decision"] == "SUBMIT_TO_QA"
    assert report["summary"]["sessions"] == 70
    assert report["summary"]["opportunities"] == 280
    assert report["summary"]["mean_implementation_drag_bps"] == pytest.approx(1.0)
    assert report["failed_criteria"] == []
    assert "not_a_promotion" in report

    short = evaluate_candidate(
        {"A": by_instrument["A"][:2]}, expr=expr, spec=spec,
        horizon_seconds=5, execution="TAKER", trials=1, family_pbo=None)
    assert short["decision"] == "HOLD"
    assert "SESSIONS_BELOW_MINIMUM" in short["failed_criteria"]
    assert "PBO_UNMEASURED" in short["failed_criteria"]


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
    assert config["instrument_shard_size"] == 32
    assert spec.purge_gap == timedelta(milliseconds=5250)

    proposal["suggested_params"]["fee_bps_per_side"] = 0
    gate = gate0(proposal)
    assert not gate.ok and "INTRADAY_CONTRACT_INVALID" in gate.codes

    proposal = _intraday_proposal()
    proposal["suggested_params"]["position_mode"] = "LONG_SHORT"
    gate = gate0(proposal)
    assert not gate.ok and "INTRADAY_CONTRACT_INVALID" in gate.codes


def test_screening_population_unifies_clocks_horizons_and_execution_labels() -> None:
    proposal = _intraday_proposal()
    edge, _ = expected_edge_for(proposal)
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
        "semantic_plan": passive_plan,
        "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
    }]
    edge["screening_cohort_version"] = "intraday-screening-cohort-v3"

    config, spec = config_from_edge(edge)
    assert spec.horizons_seconds == (5, 30)
    assert spec.feature_lookback_seconds == 90
    assert config["population_execution_model"] == "PASSIVE_FIFO_LOWER_BOUND"
    assert config["screening_trial_exposure"] == 1
    assert config["screening_population"][0]["screening_only"] is True


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
                       match="intraday-screening-cohort-v3"):
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


def test_screening_exposure_counts_as_an_exact_formula_trial() -> None:
    class Cursor:
        def __init__(self, conn): self.conn = conn
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params): self.conn.executed = (sql, params)
        def fetchone(self): return (4,)

    class Conn:
        def __init__(self): self.executed = None
        def cursor(self): return Cursor(self)

    conn = Conn()
    expr = {"op": "field", "field": "microprice_offset_bps"}
    assert count_family_trials(conn, "family", expr) == 4
    sql, params = conn.executed
    assert sql == _SQL_FAMILY_OR_EXACT_TRIALS
    assert "jsonb_array_elements" in sql
    assert "screening_population" in sql
    assert len(params) == 4 and params[1] == params[2] == params[3]


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
        def fetchone(self): return (config,)
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
        },
        "screening_population": [{
            "ast_fingerprint": "linked-positive",
            "summary": {
                "opportunities": 1_000,
                "mean_net_bps_per_opportunity": 1.0,
                "session_mean_net_bps": 2.0,
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
    }}
    assert _fast_screen_gate(positive, config)["primary_pass"] is True


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
         "oos_summary": {"mean_net_bps_per_opportunity": 0.4}},
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
         "oos_summary": {"mean_net_bps_per_opportunity": 0.2, "sessions": 30}},
        {"intraday_signal_expr": child,
         "semantic_plan": _evolution_plan(context="WIDE_SPREAD"),
         "decision": "SUBMIT_TO_QA", "lesson_codes": [],
         "oos_summary": {"mean_net_bps_per_opportunity": 0.5, "sessions": 30}},
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
    expr = {"op": "field", "field": "microprice_offset_bps"}
    plan = {
        "event": "MICROPRICE_DISLOCATION", "context": ["ALL"],
        "qualities": ["LEVEL"], "direction": "REVERT",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 5,
    }
    screened = [{
        "intraday_signal_expr": expr, "semantic_plan": plan,
        "decision": "SCREENING_ONLY", "evidence_tier": "SCREENING_ONLY",
        "lesson_codes": [],
        "oos_summary": {"mean_mid_markout_bps": 25.0,
                        "mean_net_bps_per_opportunity": 2.0,
                        "sessions": 60},
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
    assert "SCREEN_SURVIVOR still needs" in intraday_experience.render(memory)


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
