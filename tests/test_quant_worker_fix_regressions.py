from __future__ import annotations

from datetime import date, timedelta
import inspect
import json
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import backtest_runner
import config_binding
import feature_catalog
import dataset_refinery
import experiment_orchestrator
import factory_bridge
import orphan_finalizer
import signal_ic
import strategy_templates
import walk_forward


_BASE_CONFIG = {
    "strategy": "REV-5-SMOKE",
    "lookback_days": 5,
    "top_n": 20,
    "rebalance": "EVERY_5_TRADING_DAYS",
    "initial_capital": 100_000_000.0,
}


def _daily_row(instrument_id: str, session: date, close: float) -> dict:
    return {
        "instrument_id": instrument_id,
        "trade_date": session,
        "open": close,
        "high": close,
        "low": close,
        "close": close,
        "volume": 1_000,
        "notional": close * 1_000,
    }


def _governed_market(rows: list[dict]) -> backtest_runner.Market:
    governed = backtest_runner._GovernedStockRows()
    governed.extend(rows)
    instruments, bounds = backtest_runner._stock_row_evidence(governed)
    backtest_runner._seal_loaded_stock_rows(
        governed, "stock-universe-v1", instruments, bounds)
    return backtest_runner.Market.from_rows(governed)


def test_future_adjustment_gap_does_not_rewrite_past_pit_universe() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    future = date(2026, 1, 7)
    rows = backtest_runner._GovernedStockRows()
    rows.extend([
        _daily_row("FUTURE_SPLIT", first, 100.0),
        _daily_row("FUTURE_SPLIT", second, 101.0),
        _daily_row("FUTURE_SPLIT", future, 202.0),
        _daily_row("ORDINARY", first, 100.0),
        _daily_row("ORDINARY", second, 110.0),
        _daily_row("SAFE", first, 100.0),
        _daily_row("SAFE", second, 101.0),
    ])
    instruments, bounds = backtest_runner._stock_row_evidence(rows)
    backtest_runner._seal_loaded_stock_rows(
        rows, "stock-universe-v1", instruments, bounds)
    receipt = rows._stock_scope_receipt

    market = backtest_runner.Market.from_rows(rows)

    assert market.symbols == ["FUTURE_SPLIT", "ORDINARY", "SAFE"]
    assert strategy_templates.PITView(market, second).closes(
        "FUTURE_SPLIT", 10) == [100.0, 101.0]
    assert (future, "FUTURE_SPLIT") in market.closes
    assert market._stock_scope_receipt is receipt
    assert market._require_stock_scope() is receipt
    assert len(rows) == len(market.closes), "gap audit must not mutate the panel"

    before = market.adjustment_audit_manifest(end=second)
    complete = market.adjustment_audit_manifest()
    assert before["event_count"] == 0
    assert complete["event_count"] == 1
    assert complete["events"][0]["multiple"] == 2
    assert complete == market.adjustment_audit_manifest(), "audit must be deterministic"

    # A physical prefix carries the same stock receipt and no future gap.  It
    # is executable; the full interval fails only when the event date arrives.
    prefix_dates = [first, second]
    prefix = backtest_runner.Market(
        dates=prefix_dates,
        opens={k: v for k, v in market.opens.items() if k[0] <= second},
        closes={k: v for k, v in market.closes.items() if k[0] <= second},
        symbols=list(market.symbols),
        notionals={k: v for k, v in market.notionals.items() if k[0] <= second},
    )._inherit_stock_scope_from(market)
    assert prefix._stock_scope_receipt is receipt
    assert prefix.adjustment_audit_manifest()["event_count"] == 0
    assert backtest_runner.run_backtest(prefix, _BASE_CONFIG).equity

    with pytest.raises(backtest_runner.UnmeasuredAdjustmentGapError,
                       match="NOT_MEASURED.*FUTURE_SPLIT"):
        backtest_runner.run_backtest(market, _BASE_CONFIG)


def test_hot_path_is_single_pass_and_disorder_fails_closed_without_sorting() -> None:
    class CountingRows(list):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    disorder = CountingRows([
        _daily_row("A", second, 101.0),
        _daily_row("A", first, 100.0),
    ])
    with pytest.raises(backtest_runner.UnmeasuredAdjustmentOrderingError,
                       match="strictly chronological"):
        backtest_runner.Market.from_rows(disorder)
    assert disorder.iterations == 1

    ordered = CountingRows()
    for symbol_index in range(10):
        for offset in range(200):
            ordered.append(_daily_row(
                f"S{symbol_index:02d}", first + timedelta(days=offset),
                100.0 + symbol_index + offset / 100.0))
    market = backtest_runner.Market.from_rows(ordered)
    assert ordered.iterations == 1, "large-panel construction must not rescan rows"
    assert len(market.closes) == len(ordered)
    source = inspect.getsource(backtest_runner.Market.from_rows)
    assert "sorted(observations)" not in source
    assert "for (session, iid), close in closes.items()" not in source


@pytest.mark.parametrize(
    ("previous", "current", "expected"),
    [
        (100.0, 200.0, True),
        (100.0, 300.0, True),
        (100.0, 199.6, True),
        (100.0, 199.9, True),
        (100.0, 199.59, False),
        (100.0, 200.39, True),
        (100.0, 200.41, False),
        (100.0, 405.0, False),
        (100.0, 1_001.9, True),
        (100.0, 1_002.1, False),
    ],
)
def test_adjustment_detector_has_one_canonical_02pct_boundary(
    previous: float, current: float, expected: bool,
) -> None:
    assert strategy_templates.looks_like_unadjusted_adjustment_gap(
        previous, current) is expected
    assert backtest_runner._looks_like_unadjusted_integer_split(
        previous, current) is expected
    assert (strategy_templates._adjustment_break([previous, current]) == 1) is expected
    assert (backtest_runner._looks_like_unadjusted_integer_split
            is strategy_templates.looks_like_unadjusted_adjustment_gap)
    assert dataset_refinery.SPLIT_MIN_RATIO == \
        strategy_templates.ADJUSTMENT_MIN_MULTIPLE
    assert dataset_refinery.SPLIT_TOLERANCE == \
        strategy_templates.ADJUSTMENT_RELATIVE_TOLERANCE


def _proposal(edge_type: str) -> dict:
    return {
        "proposal_id": f"prop_{edge_type}",
        "edge_type": edge_type,
        "universe_key": "krx_all",
        "label": "forward_return",
        "baseline": "equal_weight_buy_and_hold",
        "economic_rationale": "forced flow may move prices temporarily",
        "counterparty": "liquidity demander",
        "competing_explanation": "market beta exposure",
        "competing_explanation_codes": ["BETA_EXPOSURE"],
        "skeptic_sign": "independent-worker",
        "lead_ids": ["lead-a"],
        "falsification_tests": ["reject when cost-net return is non-positive"],
        "data_requirements": {"tables": ["market_bars"],
                              "min_history_days": 750},
        "suggested_params": {"lookback_days": 20},
        "trial_budget": 5,
        "prior_check": {},
        "source_reported_effect": {},
        "research_packet_ids": [],
        "claim_ids": [],
    }


@pytest.mark.parametrize("raw_edge", ["order_flow", "liquidity_premium"])
def test_event_time_raw_labels_fail_closed_in_binding_and_gate0(raw_edge: str) -> None:
    """Near-name daily templates must never silently stand in for raw data."""

    assert raw_edge in strategy_templates.NOT_IMPLEMENTED
    assert strategy_templates.template_for_edge(raw_edge) is None

    binding = config_binding.bind(
        {"expected_edge": {"type": raw_edge, "horizon_days": 20}},
        _BASE_CONFIG,
    )
    assert not binding.ok
    assert raw_edge in binding.rejected[0]
    assert "미구현" in binding.rejected[0]

    gate = factory_bridge.gate0(_proposal(raw_edge))
    assert not gate.ok
    assert "UNMAPPED_VOCAB" in gate.codes
    assert any(raw_edge in reason and "미구현" in reason
               for reason in gate.reasons)


@pytest.mark.parametrize(
    "daily_edge", ["order_flow_imbalance", "illiquidity_premium"])
def test_explicit_daily_microstructure_edges_bind_and_pass_gate0(
    daily_edge: str,
) -> None:
    assert strategy_templates.template_for_edge(daily_edge) is not None
    binding = config_binding.bind(
        {"expected_edge": {"type": daily_edge, "horizon_days": 20}},
        _BASE_CONFIG,
    )
    assert binding.ok, binding.rejected
    gate = factory_bridge.gate0(_proposal(daily_edge))
    assert gate.ok, gate.as_dict()


def test_adjustment_audit_is_persisted_for_success_and_failure() -> None:
    source = inspect.getsource(backtest_runner.register_and_run)
    assert source.count('"adjustment_gap_audit"') >= 2
    assert '"measurement_status"' in source
    assert '"NOT_MEASURED"' in source


def test_signal_ic_drops_forward_labels_that_cross_adjustment_gaps() -> None:
    first = date(2026, 1, 5)
    signal_day = date(2026, 1, 6)
    forward_day = date(2026, 1, 7)
    rows = []
    for symbol, p0, p1, p2 in (
        ("A", 100.0, 110.0, 330.0),  # 3x unit change in the label
        ("B", 100.0, 105.0, 210.0),  # 2x unit change in the label
        ("C", 100.0, 100.0, 100.0),
    ):
        rows.extend([
            _daily_row(symbol, first, p0),
            _daily_row(symbol, signal_day, p1),
            _daily_row(symbol, forward_day, p2),
        ])
    market = backtest_runner.Market.from_rows(rows)
    measured = signal_ic.ic_series(
        market,
        {"strategy": "MOM", "lookback_days": 1},
        horizon=1,
        sample_dates=[signal_day],
        min_names=3,
    )
    assert measured == [], "split-generated labels must not manufacture IC"


def test_feature_catalog_drops_any_label_path_crossing_adjustment_gap() -> None:
    first = date(2026, 1, 5)
    middle = date(2026, 1, 6)
    last = date(2026, 1, 7)

    class Cursor:
        def execute(self, *_args, **_kwargs):
            return None

        def fetchall(self):
            return [
                ("SPLIT", first, 100.0),
                ("SPLIT", middle, 200.0),
                # Endpoint ratio is no longer an integer; the intermediate
                # transition must still invalidate the complete label path.
                ("SPLIT", last, 210.0),
                ("SAFE", first, 100.0),
                ("SAFE", middle, 101.0),
                ("SAFE", last, 102.0),
            ]

    measured = feature_catalog._forward_returns(
        Cursor(), [first], 2, ["SAFE", "SPLIT"])
    assert "SPLIT" not in measured[first]
    assert measured[first]["SAFE"] == pytest.approx(0.02)


def test_derived_market_must_match_source_values_order_and_contiguous_scope() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    third = date(2026, 1, 7)
    source = _governed_market([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 101.0),
        _daily_row("A", third, 102.0),
    ])

    changed = backtest_runner.Market(
        dates=[first, second],
        opens={key: value for key, value in source.opens.items()
               if key[0] <= second},
        closes={(first, "A"): 100.0, (second, "A"): 202.0},
        symbols=["A"],
        notionals={key: value for key, value in source.notionals.items()
                   if key[0] <= second},
    )
    with pytest.raises(RuntimeError, match="changed source closes"):
        changed._inherit_stock_scope_from(source)

    reordered = backtest_runner.Market(
        dates=[second, first],
        opens={key: value for key, value in source.opens.items()
               if key[0] <= second},
        closes={key: value for key, value in source.closes.items()
                if key[0] <= second},
        symbols=["A"],
        notionals={key: value for key, value in source.notionals.items()
                   if key[0] <= second},
    )
    with pytest.raises(RuntimeError, match="preserve source date/symbol order"):
        reordered._inherit_stock_scope_from(source)

    non_contiguous = backtest_runner.Market(
        dates=[first, third],
        opens={key: value for key, value in source.opens.items()
               if key[0] in {first, third}},
        closes={key: value for key, value in source.closes.items()
                if key[0] in {first, third}},
        symbols=["A"],
        notionals={key: value for key, value in source.notionals.items()
                   if key[0] in {first, third}},
    )
    with pytest.raises(RuntimeError, match="contiguous source date window"):
        non_contiguous._inherit_stock_scope_from(source)


def test_post_audit_market_mutation_fails_before_simulation() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    source = _governed_market([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 101.0),
    ])
    source.closes[(second, "A")] = 202.0
    with pytest.raises(RuntimeError, match="identity changed after"):
        backtest_runner.run_backtest(source, _BASE_CONFIG)


def test_adjustment_audit_fingerprint_binds_scope_status_and_canonical_events() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    rows_a_first = [
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 200.0),
        _daily_row("B", first, 100.0),
        _daily_row("B", second, 300.0),
    ]
    rows_b_first = rows_a_first[2:] + rows_a_first[:2]
    first_market = backtest_runner.Market.from_rows(rows_a_first)
    second_market = backtest_runner.Market.from_rows(rows_b_first)
    first_audit = first_market.adjustment_audit_manifest()
    second_audit = second_market.adjustment_audit_manifest()
    assert first_audit["events"] == second_audit["events"]
    assert first_audit["event_fingerprint"] == second_audit["event_fingerprint"]
    assert first_audit["audit_fingerprint"] == second_audit["audit_fingerprint"]

    before = first_market.adjustment_audit_manifest(end=first)
    starts_at_event = first_market.adjustment_audit_manifest(
        start=second, end=second)
    assert before["event_fingerprint"] == starts_at_event["event_fingerprint"]
    assert before["audit_fingerprint"] != starts_at_event["audit_fingerprint"]
    assert before["event_fingerprint"] != first_audit["event_fingerprint"]
    assert before["audit_fingerprint"] != first_audit["audit_fingerprint"]
    assert before["scope"]["row_count"] == 2
    assert before["scope"]["data_identity_contract"] == \
        backtest_runner.MARKET_DATA_FINGERPRINT_CONTRACT

    unverified = backtest_runner.Market(
        dates=list(first_market.dates),
        opens=dict(first_market.opens),
        closes=dict(first_market.closes),
        symbols=list(first_market.symbols),
        notionals=dict(first_market.notionals),
    )
    unverified._adjustment_gaps = first_market._adjustment_gaps
    unverified._adjustment_gap_sessions = first_market._adjustment_gap_sessions
    with pytest.raises(RuntimeError, match="lacks a verified"):
        unverified.adjustment_audit_manifest()


def test_adjustment_audit_is_bounded_but_fingerprints_omitted_events() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)

    def _many(last_multiple: float) -> dict:
        rows = []
        for index in range(101):
            symbol = f"S{index:03d}"
            rows.extend([
                _daily_row(symbol, first, 100.0),
                _daily_row(symbol, second,
                           last_multiple if index == 100 else 200.0),
            ])
        return backtest_runner.Market.from_rows(
            rows).adjustment_audit_manifest()

    twice = _many(200.0)
    changed_omitted_event = _many(300.0)
    assert twice["event_count"] == 101
    assert len(twice["events"]) == 100
    assert twice["events_truncated"] is True
    assert twice["event_fingerprint"] != \
        changed_omitted_event["event_fingerprint"]
    assert twice["audit_fingerprint"] != \
        changed_omitted_event["audit_fingerprint"]


def test_global_future_gap_prefix_deletion_is_deprecated() -> None:
    rows = [
        _daily_row("A", date(2026, 1, 5), 100.0),
        _daily_row("A", date(2026, 1, 6), 200.0),
    ]
    with pytest.raises(NotImplementedError, match="PIT-unsafe"):
        dataset_refinery.cut_at_unadjusted_gap(rows)
    assert len(rows) == 2, "deprecated cleaner must not mutate its input"


def test_mutated_source_cannot_launder_a_fresh_child_audit() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    source = _governed_market([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 101.0),
    ])
    source.closes[(second, "A")] = 202.0
    child = backtest_runner.Market(
        dates=list(source.dates),
        opens=dict(source.opens),
        closes=dict(source.closes),
        symbols=list(source.symbols),
        notionals=dict(source.notionals),
    )
    with pytest.raises(RuntimeError, match="identity changed after"):
        child._inherit_stock_scope_from(source)


def test_derived_market_must_preserve_mapping_iteration_order() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    source = _governed_market([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 101.0),
    ])
    child = backtest_runner.Market(
        dates=list(source.dates),
        opens=dict(source.opens),
        closes=dict(reversed(list(source.closes.items()))),
        symbols=list(source.symbols),
        notionals=dict(source.notionals),
    )
    with pytest.raises(RuntimeError, match="keys, values, or order"):
        child._inherit_stock_scope_from(source)


@pytest.mark.parametrize("cleared_field", [
    "_adjustment_gaps",
    "_adjustment_gap_sessions",
    "_adjustment_session_index_fingerprint",
    "_adjustment_audit_seal",
])
def test_metadata_clearing_cannot_bypass_adjustment_audit(
    cleared_field: str,
) -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    market = _governed_market([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 200.0),
    ])
    empty = frozenset() if cleared_field == "_adjustment_gap_sessions" \
        else (() if cleared_field != "_adjustment_audit_seal" else "")
    setattr(market, cleared_field, empty)
    with pytest.raises(RuntimeError, match="NOT_MEASURED"):
        market.adjustment_audit_manifest()
    with pytest.raises(RuntimeError, match="NOT_MEASURED"):
        backtest_runner.run_backtest(market, _BASE_CONFIG)


def test_audit_seal_binds_detector_version(monkeypatch: pytest.MonkeyPatch) -> None:
    market = backtest_runner.Market.from_rows([
        _daily_row("A", date(2026, 1, 5), 100.0),
        _daily_row("A", date(2026, 1, 6), 101.0),
    ])
    monkeypatch.setattr(
        backtest_runner, "ADJUSTMENT_GAP_DETECTOR_VERSION", "tampered-v999")
    with pytest.raises(RuntimeError, match="evidence seal"):
        market.adjustment_audit_manifest()


def test_audit_seal_binds_lossless_gap_evidence() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    market = backtest_runner.Market.from_rows([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 200.0),
    ])
    original = market._adjustment_gaps[0]
    market._adjustment_gaps = (backtest_runner.AdjustmentGap(
        instrument_id=original.instrument_id,
        previous_session=original.previous_session,
        session=original.session,
        previous_close=50.0,
        close=100.0,
        multiple=original.multiple,
    ),)
    with pytest.raises(RuntimeError, match="evidence seal"):
        market.adjustment_audit_manifest()


def test_rescan_is_streaming_and_retains_only_symbol_state_plus_gaps() -> None:
    class CountingDict(dict):
        item_scans = 0

        def items(self):
            self.item_scans += 1
            return super().items()

    first = date(2026, 1, 5)
    rows = [
        _daily_row(f"S{symbol:03d}", first + timedelta(days=offset),
                   100.0 + symbol + offset / 1000.0)
        for symbol in range(40)
        for offset in range(250)
    ]
    market = backtest_runner.Market.from_rows(rows)
    counted = CountingDict(market.closes)
    market.closes = counted
    market._rescan_adjustment_audit()
    assert counted.item_scans == 1
    assert not market._adjustment_gaps
    source = inspect.getsource(
        backtest_runner.Market._rescan_adjustment_audit)
    assert "observations" not in source
    assert "date_order" not in source
    assert "last_close" in source


def test_range_audit_ignores_instruments_not_yet_observed_in_pit_scope() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    future = date(2026, 1, 7)

    def _market(future_symbol: str) -> backtest_runner.Market:
        return backtest_runner.Market.from_rows([
            _daily_row("A", first, 100.0),
            _daily_row("A", second, 101.0),
            _daily_row(future_symbol, future, 50.0),
        ])

    left = _market("FUTURE_LEFT").adjustment_audit_manifest(end=second)
    right = _market("FUTURE_RIGHT").adjustment_audit_manifest(end=second)
    assert left["scope"]["instrument_count"] == 1
    assert left["scope"]["instrument_ids"] == ["A"]
    assert left["scope"]["instrument_fingerprint"] == \
        right["scope"]["instrument_fingerprint"]
    assert left["scope"]["data_identity_fingerprint"] == \
        right["scope"]["data_identity_fingerprint"]
    assert left["audit_fingerprint"] == right["audit_fingerprint"]


def test_same_session_gap_error_is_deterministic_across_row_interleavings() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    a_rows = [_daily_row("A", first, 100.0),
              _daily_row("A", second, 200.0)]
    b_rows = [_daily_row("B", first, 100.0),
              _daily_row("B", second, 300.0)]
    for rows in (a_rows + b_rows, b_rows + a_rows):
        market = backtest_runner.Market.from_rows(rows)
        with pytest.raises(backtest_runner.UnmeasuredAdjustmentGapError) as caught:
            market._raise_if_unmeasured_adjustment_gap(second)
        assert caught.value.gap.instrument_id == "A"


def test_walk_forward_marks_only_gap_window_and_continues_later_window() -> None:
    first = date(2026, 1, 5)
    sessions = [first + timedelta(days=offset) for offset in range(12)]
    closes = [100.0, 101.0, 102.0, 103.0, 104.0, 208.0,
              209.0, 210.0, 211.0, 212.0, 213.0, 214.0]
    market = _governed_market([
        _daily_row("A", session, close)
        for session, close in zip(sessions, closes)
    ])
    affected = walk_forward.WFWindow(
        label="affected", warmup_start=sessions[0],
        test_start=sessions[3], test_end=sessions[6],
        n_warmup_days=3, n_test_days=4, partial=False)
    safe = walk_forward.WFWindow(
        label="safe", warmup_start=sessions[6],
        test_start=sessions[8], test_end=sessions[11],
        n_warmup_days=2, n_test_days=4, partial=False)

    first_result = walk_forward.run_window(
        walk_forward.slice_market(market, affected), affected, _BASE_CONFIG)
    second_result = walk_forward.run_window(
        walk_forward.slice_market(market, safe), safe, _BASE_CONFIG)
    assert first_result["measurement_status"] == "NOT_MEASURED"
    assert first_result["measurement_not_measured"] == 1.0
    assert "total_return" not in first_result
    assert second_result.get("measurement_status") != "NOT_MEASURED"
    assert isinstance(second_result["total_return"], float)
    stats, _flags, _verdict = walk_forward.fragility_summary(
        [("affected", first_result), ("safe", second_result)],
        min_test_days=1)
    assert stats["n_not_measured"] == 1
    assert stats["n_windows"] == 1


def test_ordering_failure_has_terminal_not_measured_disposition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    second = date(2026, 1, 6)
    first = date(2026, 1, 5)
    rows = [_daily_row("A", second, 101.0),
            _daily_row("A", first, 100.0)]
    error = backtest_runner.UnmeasuredAdjustmentOrderingError(
        "NOT_MEASURED: non-chronological A")
    audit = backtest_runner._ordering_failure_audit_manifest(rows, error)
    assert audit["status"] == "REJECTED_NON_CHRONOLOGICAL_INPUT"
    assert audit["measurement_status"] == "NOT_MEASURED"
    assert audit["scope"]["start_session"] == first.isoformat()
    assert not backtest_runner.zombie_experiment(
        "FAILED", True, 1, 0, 999.0)
    source = inspect.getsource(backtest_runner.register_and_run)
    ordering_branch = source.index("_ordering_failure_audit_manifest")
    cancelled_branch = source.index("status='CANCELLED'", ordering_branch)
    assert ordering_branch < cancelled_branch
    assert "status='FAILED'" in source[ordering_branch:cancelled_branch]

    governed_rows = backtest_runner._GovernedStockRows()
    governed_rows.extend(rows)
    instruments, bounds = backtest_runner._stock_row_evidence(governed_rows)
    backtest_runner._seal_loaded_stock_rows(
        governed_rows, "stock-universe-v1", instruments, bounds)
    queries: list[tuple[str, tuple | None]] = []

    class Cursor:
        result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, params=None):
            normalized = " ".join(str(statement).split())
            queries.append((normalized, params))
            self.result = (("11111111-1111-1111-1111-111111111111",)
                           if "returning experiment_id" in normalized.lower()
                           else None)

        def fetchone(self):
            return self.result

    class Connection:
        closed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            queries.append(("COMMIT", None))

        def rollback(self):
            queries.append(("ROLLBACK", None))

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setitem(sys.modules, "source_registry", types.SimpleNamespace(
        load_project_env=lambda: {"DATABASE_URL": "postgresql://unused"}))
    monkeypatch.setitem(sys.modules, "db_writer", types.SimpleNamespace(
        connect=lambda *_args, **_kwargs: connection))
    monkeypatch.setattr(
        backtest_runner, "load_dataset",
        lambda *_args: ("daily-id", "stock-universe-v1", "daily-hash",
                        governed_rows))
    monkeypatch.setattr(backtest_runner, "seal_micro_lineage",
                        lambda *_args: None)
    monkeypatch.setattr(backtest_runner, "code_version", lambda: "code-v-test")

    with pytest.raises(backtest_runner.UnmeasuredAdjustmentOrderingError):
        backtest_runner.register_and_run(
            "daily", "v1", hypothesis_id="hypothesis-id",
            config=dict(_BASE_CONFIG))

    failed_updates = [statement for statement, _params in queries
                      if "status='FAILED'" in statement]
    cancelled_updates = [statement for statement, _params in queries
                         if "status='CANCELLED'" in statement]
    run_inserts = [(statement, params) for statement, params in queries
                   if statement.lower().startswith(
                       "insert into quant.backtest_runs")]
    assert failed_updates
    assert not cancelled_updates
    assert len(run_inserts) == 1
    summary = json.loads(run_inserts[0][1][8])
    assert summary["measurement_status"] == "NOT_MEASURED"
    assert (summary["adjustment_gap_audit"]["status"]
            == "REJECTED_NON_CHRONOLOGICAL_INPUT")
    assert connection.closed


def test_not_measured_window_cannot_become_robust_or_supported() -> None:
    measured = {
        "total_return": 0.10,
        "sharpe_rf0": 1.0,
        "max_drawdown": -0.05,
        "test_days": 50,
    }
    unavailable = {
        "usable": False,
        "measurement_status": "NOT_MEASURED",
        "measurement_not_measured": 1.0,
        "measurement_reason": "unadjusted corporate-action gap crossed",
        "test_days": 50,
    }

    stats, flags, verdict = walk_forward.fragility_summary([
        ("unavailable", unavailable), ("safe", measured)])

    assert stats["n_windows"] == 1
    assert stats["n_not_measured"] == 1
    assert stats["mean_window_return"] == pytest.approx(0.10)
    assert flags == ["WINDOWS_NOT_MEASURED"]
    assert verdict == "INSUFFICIENT"


def test_orphan_finalizer_reconstructs_not_measured_windows_fail_closed() -> None:
    rows = [
        ("missing", "measurement_not_measured", 1.0, {
            "measurement_status": "NOT_MEASURED",
            "measurement_reason": "unadjusted corporate-action gap crossed",
        }),
        ("safe", "total_return", 0.10, {}),
        ("safe", "sharpe_rf0", 1.0, {}),
        ("safe", "max_drawdown", -0.05, {}),
        # Legacy three-column rows remain accepted.
        ("safe", "test_days", 50.0),
    ]
    windows, dropped = orphan_finalizer.windows_from_rows(rows)

    assert dropped == []
    missing = dict(windows)["missing"]
    assert missing["measurement_status"] == "NOT_MEASURED"
    assert "corporate-action gap" in missing["measurement_reason"]
    stats, flags, verdict = walk_forward.fragility_summary(
        windows, min_test_days=1)
    assert stats["n_not_measured"] == 1
    assert flags == ["WINDOWS_NOT_MEASURED"]
    assert verdict == "INSUFFICIENT"


def test_legacy_three_column_not_measured_evidence_is_not_dropped() -> None:
    rows = [
        ("missing", "measurement_not_measured", 1.0),
        ("safe", "total_return", 0.10),
        ("safe", "sharpe_rf0", 1.0),
        ("safe", "max_drawdown", -0.05),
        ("safe", "test_days", 50.0),
    ]
    windows, dropped = orphan_finalizer.windows_from_rows(rows)

    assert dropped == []
    assert dict(windows)["missing"]["measurement_status"] == "NOT_MEASURED"
    stats, flags, verdict = walk_forward.fragility_summary(
        windows, min_test_days=1)
    assert stats["n_not_measured"] == 1
    assert flags == ["WINDOWS_NOT_MEASURED"]
    assert verdict == "INSUFFICIENT"


def test_partial_orphan_metrics_can_never_reconstruct_supported() -> None:
    rows = [
        ("safe", "total_return", 0.10),
        ("safe", "sharpe_rf0", 1.0),
        ("safe", "max_drawdown", -0.05),
        ("partial", "total_return", 0.50),
    ]

    judged = orphan_finalizer.judge_from_stored(
        exp_id="experiment-id", hypothesis_id="hypothesis-id",
        hyp_status="TESTING", trial_family_id="family-id", trial_number=1,
        window_rows=rows, summary_rows=[], gate_rows=[])

    assert judged["verdict"] == "INSUFFICIENT"
    assert judged["new_status"] == "INCONCLUSIVE"
    assert judged["status_to_set"] == "INCONCLUSIVE"
    assert judged["decision"] != "SUBMIT_TO_QA"
    assert "1" in judged["note"], judged["note"]


def test_measured_fragility_is_not_masked_by_not_measured_window() -> None:
    measured_failure = {
        "total_return": -0.10,
        "sharpe_rf0": -1.0,
        "max_drawdown": -0.30,
        "test_days": 50,
    }
    unavailable = {
        "usable": False,
        "measurement_status": "NOT_MEASURED",
        "measurement_not_measured": 1.0,
        "test_days": 50,
    }

    stats, flags, verdict = walk_forward.fragility_summary([
        ("unavailable", unavailable), ("measured-failure", measured_failure)])

    assert stats["n_not_measured"] == 1
    assert "SIGN_INCONSISTENT" in flags
    assert verdict == "FRAGILE"


def test_legacy_unusable_window_also_blocks_robust_promotion() -> None:
    measured = {
        "total_return": 0.10,
        "sharpe_rf0": 1.0,
        "max_drawdown": -0.05,
        "test_days": 50,
    }
    legacy_unusable = {"usable": False, "reason": "no evaluation interval"}

    stats, flags, verdict = walk_forward.fragility_summary([
        ("legacy-unusable", legacy_unusable), ("safe", measured)])

    assert stats["n_not_measured"] == 1
    assert flags == ["WINDOWS_NOT_MEASURED"]
    assert verdict == "INSUFFICIENT"


def test_required_micro_without_panel_overlap_is_sealed_not_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    market = _governed_market([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 101.0),
    ])
    micro_rows = [{
        "instrument_id": "A",
        "trade_date": date(2030, 1, 1),
        "spread_bps": 10.0,
    }]
    monkeypatch.setattr(
        backtest_runner, "required_micro_dataset",
        lambda _config: ("micro-required", "v1"))
    monkeypatch.setattr(
        backtest_runner, "load_dataset",
        lambda _conn, _name, _version: (
            "dataset-id", "universe-id", "content-hash", micro_rows))

    with pytest.raises(backtest_runner.UnmeasuredMicroCoverageError,
                       match="NOT_MEASURED.*no usable overlap"):
        backtest_runner.attach_micro_if_needed(market, {}, object())

    assert market.micro == {}
    audit = market.adjustment_audit_manifest()
    assert audit["status"] == backtest_runner._ADJUSTMENT_AUDIT_VERIFIED
    assert backtest_runner.run_backtest(market, _BASE_CONFIG).equity


def test_failure_audit_fallback_cannot_mask_terminal_persistence() -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    market = _governed_market([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 101.0),
    ])
    market.closes[(second, "A")] = 202.0
    trigger = RuntimeError("simulation rejected mutated panel")

    first_receipt = backtest_runner._failure_adjustment_audit_manifest(
        market, trigger)
    second_receipt = backtest_runner._failure_adjustment_audit_manifest(
        market, trigger)

    assert first_receipt == second_receipt
    assert first_receipt["status"] == "REJECTED_AUDIT_INVARIANT"
    assert first_receipt["measurement_status"] == "NOT_MEASURED"
    assert first_receipt["audit_fingerprint"]
    assert "identity changed" in first_receipt["audit_error"]


def test_no_overlap_preparation_is_persisted_terminal_not_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = date(2026, 1, 5)
    second = date(2026, 1, 6)
    base_rows = backtest_runner._GovernedStockRows()
    base_rows.extend([
        _daily_row("A", first, 100.0),
        _daily_row("A", second, 101.0),
    ])
    instruments, bounds = backtest_runner._stock_row_evidence(base_rows)
    backtest_runner._seal_loaded_stock_rows(
        base_rows, "stock-universe-v1", instruments, bounds)
    micro_rows = [{
        "instrument_id": "A",
        "trade_date": date(2030, 1, 1),
        "spread_bps": 10.0,
    }]

    queries: list[tuple[str, tuple | None]] = []

    class Cursor:
        result = None

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, statement, params=None):
            normalized = " ".join(str(statement).split())
            queries.append((normalized, params))
            self.result = (("11111111-1111-1111-1111-111111111111",)
                           if "returning experiment_id" in normalized.lower()
                           else None)

        def fetchone(self):
            return self.result

    class Connection:
        closed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            queries.append(("COMMIT", None))

        def rollback(self):
            queries.append(("ROLLBACK", None))

        def close(self):
            self.closed = True

    connection = Connection()
    monkeypatch.setitem(sys.modules, "source_registry", types.SimpleNamespace(
        load_project_env=lambda: {"DATABASE_URL": "postgresql://unused"}))
    monkeypatch.setitem(sys.modules, "db_writer", types.SimpleNamespace(
        connect=lambda *_args, **_kwargs: connection))
    dataset_results = iter([
        ("daily-id", "stock-universe-v1", "daily-hash", base_rows),
        ("micro-id", "stock-universe-v1", "micro-hash", micro_rows),
    ])
    monkeypatch.setattr(
        backtest_runner, "load_dataset", lambda *_args: next(dataset_results))
    monkeypatch.setattr(backtest_runner, "seal_micro_lineage",
                        lambda *_args: None)
    monkeypatch.setattr(backtest_runner, "code_version", lambda: "code-v-test")
    monkeypatch.setattr(
        backtest_runner, "required_micro_dataset",
        lambda _config: ("micro-required", "v1"))

    with pytest.raises(backtest_runner.UnmeasuredMicroCoverageError):
        backtest_runner.register_and_run(
            "daily", "v1", hypothesis_id="hypothesis-id",
            config=dict(_BASE_CONFIG))

    failed_updates = [statement for statement, _params in queries
                      if "status='FAILED'" in statement]
    cancelled_updates = [statement for statement, _params in queries
                         if "status='CANCELLED'" in statement]
    run_inserts = [(statement, params) for statement, params in queries
                   if statement.lower().startswith(
                       "insert into quant.backtest_runs")]
    assert failed_updates
    assert not cancelled_updates
    assert len(run_inserts) == 1
    summary = json.loads(run_inserts[0][1][8])
    assert summary["measurement_status"] == "NOT_MEASURED"
    assert summary["adjustment_gap_audit"]["audit_fingerprint"]
    assert connection.closed


def test_daily_chain_does_not_swallow_required_micro_reload_failure() -> None:
    source = inspect.getsource(experiment_orchestrator._default_chain)
    attach = source.index("attach_micro_if_needed(market, config, conn)")
    windows = source.index("_verified_frozen_daily_windows(", attach)

    # Price-only strategies return zero from attach_micro_if_needed.  If the
    # call raises, the frozen contract required microstructure and continuing
    # would evaluate a feature-empty walk-forward strategy.
    assert "except Exception" not in source[attach:windows]
