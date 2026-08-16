from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
sys.path.insert(0, str(PIPELINE))

from intraday_microstructure import (  # noqa: E402
    IntradayLaneSpec,
    QuoteEvent,
    TradeEvent,
    audit_causality,
    build_samples,
    manifest,
    score_signal,
    HorizonLabel,
    IntradaySample,
    walk_forward_linear_score,
    _QUOTE_SQL,
    _SOURCE_QUALITY_SQL,
)


UTC = timezone.utc
BASE = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)
IID = "00000000-0000-0000-0000-000000000001"


def quote(second: int, bid: float, ask: float, bid_size: float,
          ask_size: float, *, delay_ms: int = 100) -> QuoteEvent:
    event = BASE + timedelta(seconds=second)
    available = event + timedelta(milliseconds=delay_ms)
    return QuoteEvent(
        event_time=event,
        received_at=available,
        observed_at=available,
        instrument_id=IID,
        bid_prices=(bid, bid - 1),
        bid_sizes=(bid_size, bid_size),
        ask_prices=(ask, ask + 1),
        ask_sizes=(ask_size, ask_size),
        source_event_id=f"q-{second}",
    )


def trade(second: int, side: int, quantity: float = 10.0,
          *, delay_ms: int = 100) -> TradeEvent:
    event = BASE + timedelta(seconds=second)
    available = event + timedelta(milliseconds=delay_ms)
    return TradeEvent(
        event_time=event,
        received_at=available,
        observed_at=available,
        instrument_id=IID,
        price=101.0,
        quantity=quantity,
        side=side,
        source_event_id=f"t-{second}-{side}",
    )


def test_causal_lane_builds_features_and_executable_labels() -> None:
    quotes = [
        quote(0, 99, 101, 10, 10),
        quote(4, 100, 102, 30, 10),
        quote(10, 102, 104, 30, 10),
        quote(20, 104, 106, 20, 20),
    ]
    trades = [trade(2, 1, 20), trade(3, -1, 5)]
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        feature_lookback_seconds=10,
        horizons_seconds=(5, 15),
        order_latency_ms=250,
        max_quote_age_seconds=6,
        fee_bps_per_side=1,
    )

    samples = build_samples(
        quotes, trades, spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=11),
    )

    assert len(samples) == 2
    first = samples[0]
    assert first.decision_time == BASE + timedelta(seconds=5)
    assert first.source_quote_event_time == BASE + timedelta(seconds=4)
    assert first.queue_imbalance_l1 == pytest.approx(0.5)
    assert first.trade_flow_imbalance == pytest.approx(0.6)
    assert first.microprice_offset_bps > 0
    labels = {v.horizon_seconds: v for v in first.labels}
    assert labels[5].long_mid_markout_bps > 0
    assert labels[5].long_taker_net_bps < labels[5].long_mid_markout_bps
    assert audit_causality(samples, spec)["status"] == "PASS"
    assert manifest(spec)["execution_model"] == "TAKER_BOTH_SIDES"
    assert manifest(spec)["purge_gap_seconds"] == pytest.approx(15.25)


def test_late_event_never_enters_feature_window() -> None:
    # Exchange timestamp is old, but the quote was not observed until after the
    # decision.  Sorting only by event_time would leak its large imbalance.
    late = quote(1, 105, 107, 1000, 1, delay_ms=10_000)
    visible = quote(0, 99, 101, 10, 10)
    future = quote(8, 100, 102, 10, 10)
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        feature_lookback_seconds=10,
        horizons_seconds=(5,),
        order_latency_ms=0,
        max_quote_age_seconds=6,
    )
    samples = build_samples(
        [visible, late, future], [], spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6),
    )
    assert len(samples) == 1
    assert samples[0].queue_imbalance_l1 == 0
    assert samples[0].quote_count == 1


def test_future_clock_skew_event_falls_back_to_eligible_quote() -> None:
    visible = quote(0, 99, 101, 10, 10)
    # Received before its rounded exchange timestamp.  It is available by the
    # decision clock but still must not replace the older causally valid quote.
    skewed = quote(6, 105, 107, 1000, 1, delay_ms=-1500)
    future = quote(9, 100, 102, 10, 10)
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        horizons_seconds=(5,),
        order_latency_ms=0,
        max_quote_age_seconds=6,
    )
    samples = build_samples(
        [visible, skewed, future], [], spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6),
    )
    assert len(samples) == 1
    assert samples[0].entry_mid == 100


def test_stale_quote_is_not_treated_as_free_liquidity() -> None:
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        horizons_seconds=(5,),
        order_latency_ms=0,
        max_quote_age_seconds=1,
    )
    samples = build_samples(
        [quote(0, 99, 101, 10, 10), quote(20, 100, 102, 10, 10)],
        [], spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6),
    )
    assert samples == []
    assert audit_causality(samples, spec)["status"] == "NO_EVIDENCE"


def test_score_signal_rejects_gross_prediction_that_cannot_cross_spread() -> None:
    quotes = [
        quote(0, 99, 101, 30, 10),
        quote(5, 100, 102, 30, 10),
        quote(10, 101, 103, 30, 10),
    ]
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        horizons_seconds=(5,),
        order_latency_ms=0,
        max_quote_age_seconds=6,
    )
    samples = build_samples(
        quotes, [], spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6),
    )
    result = score_signal(
        samples,
        lambda sample: sample.queue_imbalance_l1,
        horizon_seconds=5,
    )
    assert result["mean_mid_markout_bps"] > 0
    assert result["mean_taker_net_bps"] < 0
    assert result["decision"] == "REJECT"


def test_invalid_clocks_and_crossed_quotes_fail_closed() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        quote_event = QuoteEvent(
            event_time=datetime(2026, 8, 14),
            received_at=BASE,
            observed_at=BASE,
            instrument_id=IID,
            bid_prices=(99,), bid_sizes=(1,),
            ask_prices=(101,), ask_sizes=(1,),
        )
        assert quote_event
    with pytest.raises(ValueError, match="crossed"):
        quote(0, 102, 101, 1, 1)
    assert "ask_prices[1] >= bid_prices[1]" in _QUOTE_SQL
    assert "crossed_quotes" in _SOURCE_QUALITY_SQL


def test_walk_forward_purges_labels_and_abstains_below_spread() -> None:
    samples = []
    for index in range(120):
        decision = BASE + timedelta(seconds=index)
        feature = 1.0 if index % 2 == 0 else -1.0
        # The gross move is predictable but only 1bp, while the round-trip
        # executable result loses 1bp after the spread.
        label = HorizonLabel(
            horizon_seconds=5,
            exit_time=decision + timedelta(seconds=5),
            future_mid=100.01,
            long_mid_markout_bps=feature,
            short_mid_markout_bps=-feature,
            long_taker_net_bps=feature - 2.0,
            short_taker_net_bps=-feature - 2.0,
            long_passive_filled=True,
            short_passive_filled=True,
            long_passive_fill_time=decision + timedelta(seconds=1),
            short_passive_fill_time=decision + timedelta(seconds=1),
            long_passive_net_bps=feature,
            short_passive_net_bps=-feature,
        )
        samples.append(IntradaySample(
            instrument_id=IID,
            decision_time=decision,
            entry_time=decision,
            source_quote_event_time=decision,
            quote_age_ms=0,
            spread_bps=2.0,
            queue_imbalance_l1=feature,
            queue_imbalance_l10=feature,
            microprice_offset_bps=feature,
            trade_flow_imbalance=feature,
            quote_event_ofi=feature,
            trade_count=10,
            quote_count=10,
            entry_bid=99.99,
            entry_ask=100.01,
            entry_mid=100.0,
            labels=(label,),
        ))
    spec = IntradayLaneSpec(horizons_seconds=(5,), order_latency_ms=0)
    result = walk_forward_linear_score(
        samples,
        feature_names=("queue_imbalance_l1",),
        horizon_seconds=5,
        spec=spec,
        n_splits=3,
    )
    assert result["eligible_oos_samples"] > 0
    assert result["trades"] == 0
    assert result["coverage"] == 0
    assert result["decision"] == "REJECT"
    assert result["folds"][0]["train"] == 44  # 48 pre-test rows minus 4 overlaps


def test_passive_fifo_requires_printed_volume_to_clear_queue() -> None:
    quotes = [
        quote(0, 99, 101, 20, 20),
        quote(9, 100, 102, 20, 20),
    ]
    # Only 5 shares sell at our bid: the 20-share queue ahead is not cleared.
    trades = [trade(6, -1, 5)]
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        horizons_seconds=(5,),
        order_latency_ms=0,
        max_quote_age_seconds=6,
    )
    samples = build_samples(
        quotes, trades, spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6),
    )
    passive = score_signal(
        samples, lambda _: 1.0, horizon_seconds=5,
        execution="PASSIVE_FIFO_LOWER_BOUND",
    )
    assert passive["opportunities"] == 1
    assert passive["trades"] == 0
    assert passive["fill_rate"] == 0
    assert manifest(spec)["passive_exact_queue_supported"] is False
