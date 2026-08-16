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
import intraday_experience
import lead_intake
from intraday_alpha_ast import (IntradayExprError, evaluate, fields_of, parse,
                                shape_fingerprint, unit_of)
from intraday_candidate import evaluate_candidate
from factory_bridge import expected_edge_for, gate0
from factory_bridge import lessons_from
from intraday_experiment_runner import (_input_hash, config_from_edge,
                                        record_data_feasibility, select_slice)
from intraday_microstructure import (HorizonLabel, IntradayLaneSpec,
                                      IntradaySample)
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
    expr = {"op": "where",
            "condition": {"op": "lt", "args": [
                {"op": "field", "field": "spread_bps"},
                {"const": 5, "unit": "BPS"}]},
            "then": flow, "else": {"const": 0, "unit": "RATIO"}}
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
            "evaluation_days": 60, "instrument_count": 2,
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
    assert spec.purge_gap == timedelta(milliseconds=5250)

    proposal["suggested_params"]["fee_bps_per_side"] = 0
    gate = gate0(proposal)
    assert not gate.ok and "INTRADAY_CONTRACT_INVALID" in gate.codes

    proposal = _intraday_proposal()
    proposal["suggested_params"]["position_mode"] = "LONG_SHORT"
    gate = gate0(proposal)
    assert not gate.ok and "INTRADAY_CONTRACT_INVALID" in gate.codes


def test_intraday_family_is_semantic_not_numeric_tuning() -> None:
    proposal = _intraday_proposal()
    first, _ = expected_edge_for(proposal)
    later = _intraday_proposal()
    later["semantic_plan"] = {**later["semantic_plan"], "horizon_seconds": 30}
    later["suggested_params"] = {**later["suggested_params"],
                                 "horizon_seconds": 30}
    second, _ = expected_edge_for(later)
    a = hypothesis_view(edge_type=first["type"], universe_key=first["universe_key"],
                        research_lane=first["research_lane"],
                        semantic_fingerprint=first["semantic_fingerprint"])
    b = hypothesis_view(edge_type=second["type"], universe_key=second["universe_key"],
                        research_lane=second["research_lane"],
                        semantic_fingerprint=second["semantic_fingerprint"])
    assert family_id(a) == family_id(b)


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
    assert _input_hash("H1", base) != _input_hash("H1", late_data)


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
        conn, {"evaluation_days": 60, "instrument_count": 2}, cutoff=cutoff)
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
        conn, {"evaluation_days": 60, "instrument_count": 2}, cutoff=cutoff)

    assert selected["status"] == "PASS"
    assert selected["statistical_readiness"] == "SHORT_DIAGNOSTIC"
    assert selected["calibration_sessions"] == ["2026-08-12"]
    assert selected["sessions"] == [date(2026, 8, 13), date(2026, 8, 14)]
    assert selected["evaluation_session_count"] == 2
    assert selected["instruments"] == ["instrument-a", "instrument-b"]
    universe_sql, universe_params = conn.executed[1]
    assert "greatest(received_at, observed_at) <= %s" in universe_sql
    assert universe_params[2] == cutoff


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
    candidate = {"op": "rolling_mean", "seconds": 30, "arg": baseline}
    lead = lead_intake.to_lead({
        "TITLE": "Persistent event-time order flow", "URL": "https://example.com/paper",
        "MECHANISM": "urgent takers create persistent order flow and short markout",
        "TESTABLE_WITH": "trade_flow_imbalance rolling_mean 30 seconds",
        "READINESS": "AST_READY", "RESEARCH_LANE": "INTRADAY_EVENT",
        "SEMANTIC_PLAN": plan, "OBSERVABLES": ["trade_flow_imbalance"],
        "SOURCE_BASELINE_EXPR": baseline, "CANDIDATE_SIGNAL_EXPR": candidate,
        "DERIVATION_MODE": "MECHANISM_MUTATION",
        "DERIVATION_TRANSFORMS": ["CLOCK_CHANGE"],
        "NOVELTY_RATIONALE": "Test persistence duration on the local KRX event clock.",
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
        {"const": 5, "unit": "BPS"}]}, "then": parent,
        "else": {"const": 0, "unit": "RATIO"}}
    lead = lead_intake.to_lead({
        "TITLE": "Wide-spread persistent order flow",
        "URL": "https://example.com/evolution-paper",
        "MECHANISM": "Urgent takers persist when spread is wide and liquidity is costly.",
        "TESTABLE_WITH": "wide spread_bps gates rolling trade_flow_imbalance",
        "READINESS": "AST_READY", "RESEARCH_LANE": "INTRADAY_EVENT",
        "SEMANTIC_PLAN": _evolution_plan(context="WIDE_SPREAD"),
        "OBSERVABLES": ["spread_bps", "trade_flow_imbalance"],
        "SOURCE_BASELINE_EXPR": parent, "CANDIDATE_SIGNAL_EXPR": child,
        "DERIVATION_MODE": "MECHANISM_MUTATION",
        "DERIVATION_TRANSFORMS": ["STATE_CONDITION"],
        "NOVELTY_RATIONALE": "Condition the public persistence mechanism on liquidity cost.",
        "PARENT_SIGNAL_EXPR": parent,
        "EVOLUTION_OPERATORS": ["STATE_CONDITION"],
        "EXPECTED_INCREMENT": "The gate should improve net markout after spread cost.",
        "ABLATIONS": ["remove spread gate", "reverse spread gate"],
    }, lens="ACADEMIC", source_type="PAPER", case_id="evolution-case",
       model_version="test-model", prompt_version="evolution-v1")

    contract = lead["ast_contract"]
    assert contract["evolution_role"] == "CHILD"
    assert contract["evolution_operators"] == ["STATE_CONDITION"]
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
