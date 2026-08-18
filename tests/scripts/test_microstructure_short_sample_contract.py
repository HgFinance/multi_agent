from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
sys.path.insert(0, str(PIPELINE))

import backtest_runner  # noqa: E402
import config_binding  # noqa: E402
import experiment_worker  # noqa: E402
import factory_bridge  # noqa: E402
from alpha_ast import parse  # noqa: E402
from walk_forward import fragility_summary, make_windows  # noqa: E402


def test_two_day_rebalance_is_bound_and_executed() -> None:
    dates = [date(2026, 1, 1) + timedelta(days=i) for i in range(20)]
    assert config_binding._rebalance_for(2) == "EVERY_2_TRADING_DAYS"
    assert len(backtest_runner.rebalance_days(
        dates, {"rebalance": "EVERY_2_TRADING_DAYS"})) == 10


def test_micro_warmup_uses_formula_and_enabled_risk_window() -> None:
    config = {
        "strategy": "OFI-5-20",
        "signal_expr": parse({
            "op": "ts_mean", "field": "order_flow_imbalance", "n": 20}),
    }
    assert backtest_runner.required_warmup_days(config) == 20
    assert backtest_runner.required_warmup_days(dict(
        config, vol_target_annual=0.15, vol_lookback_days=60)) == 60


def test_gate_matches_short_walk_forward_ruler() -> None:
    proposal = factory_bridge._prop(
        suggested_params={"horizon_days": 2, "top_n": 20},
        data_requirements={
            "tables": ["market_bars", "microstructure_features"],
            "min_history_days": 3,
        },
    )
    assert factory_bridge.gate0(proposal, available_days=61).ok
    proposal["data_requirements"]["min_history_days"] = 61
    blocked = factory_bridge.gate0(proposal, available_days=61)
    assert "UNDERPOWERED_DESIGN" in blocked.codes

    dates = [date(2026, 5, 18) + timedelta(days=i) for i in range(61)]
    windows = make_windows(dates, warmup_days=3, embargo_days=2)
    assert len(windows) == 4
    metrics = [(w.label, {
        "total_return": 0.01,
        "sharpe_rf0": 0.2,
        "max_drawdown": -0.05,
        "test_days": w.n_test_days,
    }) for w in windows]
    summary, _, verdict = fragility_summary(metrics, min_test_days=10)
    assert summary["n_windows"] == 4
    assert verdict != "INSUFFICIENT"


def test_terminal_zombie_cleanup_cannot_touch_evidence() -> None:
    sql = " ".join(experiment_worker._SQL_CANCEL_TERMINAL_ZOMBIES.split())
    assert "e.status = 'RUNNING'" in sql
    assert "interval '30 minutes'" in sql
    assert "not exists (select 1 from quant.backtest_runs" in sql
    assert "not exists (select 1 from research.experiment_outcomes" in sql
