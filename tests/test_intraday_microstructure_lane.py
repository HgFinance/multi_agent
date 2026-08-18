from __future__ import annotations

from datetime import datetime, timedelta, timezone
import random
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
sys.path.insert(0, str(PIPELINE))

from intraday_microstructure import (  # noqa: E402
    COMPLETED_SECOND_POLICY,
    EXTERNAL_EVENT_SOURCE,
    IntradayLaneSpec,
    QuoteEvent,
    TradeEvent,
    audit_causality,
    build_samples,
    manifest,
    score_signal,
    STRICT_TIMESTAMP_POLICY,
    HorizonLabel,
    IntradaySample,
    walk_forward_linear_score,
    _QUOTE_SQL,
    _QUOTE_BATCH_SQL,
    _SOURCE_QUALITY_SQL,
    _SOURCE_QUALITY_BATCH_SQL,
    _TRADE_SQL,
    _TRADE_BATCH_SQL,
    _EXTERNAL_QUOTE_SQL,
    _EXTERNAL_SOURCE_QUALITY_SQL,
    _EXTERNAL_TRADE_SQL,
    load_instrument_events_batch,
)
from intraday_sample_cache import (SampleCache, identity as cache_identity)  # noqa: E402


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
    assert manifest(spec)["purge_gap_seconds"] == pytest.approx(30.25)


def test_external_replay_contract_is_explicit_and_schema_safe() -> None:
    # Sparse deeper L10 levels are normalized before QuoteEvent float parsing,
    # while the best quote remains subject to the executable-market filter.
    assert "coalesce(bid10,0)" in _EXTERNAL_QUOTE_SQL
    assert "coalesce(ask_vol10,0)" in _EXTERNAL_QUOTE_SQL
    assert "symbol = %s" in _EXTERNAL_QUOTE_SQL
    assert "ts >= %s and ts < %s and ts <= %s" in _EXTERNAL_QUOTE_SQL
    assert "hash_record_extended(quotes, 0)" in _EXTERNAL_QUOTE_SQL
    assert "case when ofi_contrib > 0 then 1" in _EXTERNAL_TRADE_SQL
    assert "when ofi_contrib < 0 then -1" in _EXTERNAL_TRADE_SQL
    assert "symbol = %s" in _EXTERNAL_TRADE_SQL
    assert "ts >= %s and ts < %s and ts <= %s" in _EXTERNAL_TRADE_SQL
    assert "hash_record_extended(ticks, 0)" in _EXTERNAL_TRADE_SQL
    assert "count(*) as quotes_without_received_at" in \
        _EXTERNAL_SOURCE_QUALITY_SQL

    replay = manifest(IntradayLaneSpec(), source=EXTERNAL_EVENT_SOURCE)
    assert replay["event_source"] == EXTERNAL_EVENT_SOURCE
    assert replay["arrival_clock_pit"] is False
    assert replay["historical_replay_only"] is True
    assert replay["source_granularity"] == "RAW_QUOTE_TRADE_EVENTS"
    assert replay["daily_aggregate_replay_allowed"] is False
    assert "receipt clock unavailable" in replay["clock"]
    assert "forward receipt-clock confirmation required" in \
        replay["evidence_limit"]


def _coarse_quote(second: int, bid: float, ask: float, bid_size: float,
                  ask_size: float, source: str) -> QuoteEvent:
    event = BASE + timedelta(seconds=second)
    return QuoteEvent(
        event_time=event, received_at=event, observed_at=event,
        instrument_id=IID,
        bid_prices=(bid, bid - 1), bid_sizes=(bid_size, bid_size),
        ask_prices=(ask, ask + 1), ask_sizes=(ask_size, ask_size),
        source_event_id=source)


def test_completed_second_replay_is_permutation_invariant_and_causal() -> None:
    quotes = [
        _coarse_quote(0, 99, 100, 10, 30, "q0-a"),
        _coarse_quote(0, 101, 102, 30, 10, "q0-b"),
        _coarse_quote(1, 100, 103, 20, 20, "q1-a"),
        _coarse_quote(1, 99, 104, 40, 5, "q1-b"),
        _coarse_quote(2, 102, 105, 10, 10, "q2-a"),
        _coarse_quote(2, 101, 106, 10, 10, "q2-b"),
    ]
    trades = [trade(0, 1, 7, delay_ms=0),
              trade(0, -1, 3, delay_ms=0)]
    spec = IntradayLaneSpec(
        sample_interval_seconds=1, feature_lookback_seconds=2,
        horizons_seconds=(1,), order_latency_ms=250,
        max_quote_age_seconds=3)

    def replay(qs, ts):
        return build_samples(
            qs, ts, spec, start=BASE + timedelta(seconds=1),
            end=BASE + timedelta(seconds=2), execution_model="TAKER",
            timestamp_policy=COMPLETED_SECOND_POLICY)

    expected = replay(quotes, trades)
    assert len(expected) == 1
    sample = expected[0]
    # Bucket [0,1) is first visible at t=1.  Bucket [1,2) may set the
    # conservatively quantized entry, but cannot leak into the t=1 feature.
    assert sample.source_quote_event_time == BASE
    assert sample.decision_time == BASE + timedelta(seconds=1)
    assert sample.entry_time == BASE + timedelta(seconds=2)
    assert sample.queue_imbalance_l1 == pytest.approx(0.0)
    assert sample.quote_count == 2
    assert sample.trade_count == 2
    assert sample.quote_event_ofi is None
    assert sample.multi_level_quote_ofi_l10 is None
    assert sample.quote_event_transition_count == 0
    assert sample.execution_capacity_supported is False

    rng = random.Random(20260818)
    for _ in range(100):
        shuffled_quotes = list(quotes)
        shuffled_trades = list(trades)
        rng.shuffle(shuffled_quotes)
        rng.shuffle(shuffled_trades)
        assert replay(shuffled_quotes, shuffled_trades) == expected


def test_completed_second_taker_uses_worst_side_price_envelope() -> None:
    # min(ask)=100 < max(bid)=101 across states. Combining those optimistic
    # sides would create a crossed synthetic quote. The valid envelope is the
    # opposite pair: sell=min(bid), buy=max(ask).
    quotes = [
        _coarse_quote(0, 99, 100, 10, 10, "feature-a"),
        _coarse_quote(0, 101, 102, 10, 10, "feature-b"),
        _coarse_quote(1, 100, 101, 10, 10, "entry-a"),
        _coarse_quote(1, 98, 105, 10, 10, "entry-b"),
        _coarse_quote(2, 104, 105, 10, 10, "exit-a"),
        _coarse_quote(2, 102, 108, 10, 10, "exit-b"),
    ]
    spec = IntradayLaneSpec(
        sample_interval_seconds=1, feature_lookback_seconds=2,
        horizons_seconds=(1,), order_latency_ms=250,
        max_quote_age_seconds=3, fee_bps_per_side=1)
    sample = build_samples(
        quotes, [], spec, start=BASE + timedelta(seconds=1),
        end=BASE + timedelta(seconds=2), execution_model="TAKER",
        timestamp_policy=COMPLETED_SECOND_POLICY)[0]
    label = sample.labels[0]
    assert sample.entry_bid == 98
    assert sample.entry_ask == 105
    assert sample.entry_bid <= sample.entry_ask
    assert label.long_taker_net_bps == pytest.approx(
        (102 / 105 - 1) * 10_000 - 2)
    # No actual observed entry ask is worse than the bound, and no actual exit
    # bid is worse than the bound, so this result cannot improve any raw-state
    # boundary pairing.
    raw_pair_returns = [
        (exit_bid / entry_ask - 1) * 10_000 - 2
        for entry_ask in (101, 105) for exit_bid in (104, 102)
    ]
    assert label.long_taker_net_bps <= min(raw_pair_returns)
    assert sample.execution_spread_bps == pytest.approx(
        (105 - 98) / ((105 + 98) / 2) * 10_000)


def test_completed_second_passive_is_not_identifiable() -> None:
    quotes = [
        _coarse_quote(0, 99, 101, 10, 10, "a"),
        _coarse_quote(1, 100, 102, 10, 10, "b"),
        _coarse_quote(2, 101, 103, 10, 10, "c"),
    ]
    spec = IntradayLaneSpec(
        sample_interval_seconds=1, horizons_seconds=(1,),
        order_latency_ms=0, max_quote_age_seconds=3)
    with pytest.raises(ValueError, match="supports TAKER only"):
        build_samples(
            quotes, [], spec, start=BASE + timedelta(seconds=1),
            end=BASE + timedelta(seconds=2),
            execution_model="PASSIVE_FIFO_LOWER_BOUND",
            timestamp_policy=COMPLETED_SECOND_POLICY)

    replay = manifest(
        spec, source=EXTERNAL_EVENT_SOURCE,
        timestamp_policy=COMPLETED_SECOND_POLICY)
    assert replay["passive_execution_model"] == "UNSUPPORTED"
    assert replay["execution_capacity_supported"] is False
    assert replay["effective_order_latency"] == \
        "ceil(decision_time+requested_latency) to the next whole-second boundary"


def test_cache_identity_binds_timestamp_and_execution_contract() -> None:
    kwargs = {
        "spec": IntradayLaneSpec(horizons_seconds=(5,)),
        "event_source": EXTERNAL_EVENT_SOURCE,
        "execution_model": "TAKER",
        "source_lineage": [{"rows": 10}],
    }
    coarse = cache_identity(
        **kwargs, timestamp_policy=COMPLETED_SECOND_POLICY)
    strict = cache_identity(
        **kwargs, timestamp_policy=STRICT_TIMESTAMP_POLICY)
    assert coarse != strict


def test_discovery_cache_identity_is_spec_and_lineage_bound() -> None:
    base = cache_identity(
        spec=IntradayLaneSpec(horizons_seconds=(5, 30)),
        event_source=EXTERNAL_EVENT_SOURCE, execution_model="TAKER",
        source_lineage=[{"source": "ext_src.quotes", "rows": 10}])
    assert base == cache_identity(
        spec=IntradayLaneSpec(horizons_seconds=(5, 30)),
        event_source=EXTERNAL_EVENT_SOURCE, execution_model="TAKER",
        source_lineage=[{"source": "ext_src.quotes", "rows": 10}])
    assert base != cache_identity(
        spec=IntradayLaneSpec(horizons_seconds=(5, 60)),
        event_source=EXTERNAL_EVENT_SOURCE, execution_model="TAKER",
        source_lineage=[{"source": "ext_src.quotes", "rows": 10}])
    assert base != cache_identity(
        spec=IntradayLaneSpec(horizons_seconds=(5, 30)),
        event_source=EXTERNAL_EVENT_SOURCE, execution_model="TAKER",
        source_lineage=[{"source": "ext_src.quotes", "rows": 11}])
    with pytest.raises(ValueError, match="raw quote/trade event sources only"):
        cache_identity(
            spec=IntradayLaneSpec(), event_source="microstructure_features",
            execution_model="TAKER", source_lineage=[])


def test_discovery_parquet_cache_round_trip(tmp_path) -> None:
    pytest.importorskip("pyarrow")
    spec = IntradayLaneSpec(
        sample_interval_seconds=5, feature_lookback_seconds=10,
        horizons_seconds=(5,), order_latency_ms=0, max_quote_age_seconds=6)
    samples = build_samples(
        [quote(0, 99, 101, 10, 10), quote(4, 100, 102, 20, 10),
         quote(10, 101, 103, 10, 10)],
        [trade(2, 1, 20)], spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6))
    cache = SampleCache("a" * 64, root=tmp_path)
    assert cache.load("2026-08-14", "005930") is None
    assert cache.store("2026-08-14", "005930", samples)
    assert cache.load("2026-08-14", "005930") == samples
    assert cache.store("2026-08-14", "000020", [])
    assert cache.load("2026-08-14", "000020") == []
    import pyarrow.parquet as pq
    metadata = pq.read_table(
        cache.path_for("2026-08-14", "000020")).schema.metadata
    assert metadata[b"intraday_source_granularity"] == \
        b"RAW_QUOTE_TRADE_EVENTS"
    assert metadata[b"evidence_authority"] == b"NONE"
    assert metadata[b"empty_semantics"] == \
        b"DERIVATION_PRODUCED_NO_SAMPLES_NOT_SOURCE_EMPTY"


def test_external_batch_loader_canonicalizes_exact_krx_symbol_keys() -> None:
    event = BASE
    quote_row = (
        event, event, event, "005930  ", [99.0], [10.0], [101.0], [11.0],
        "q1", 1, 2,
    )
    trade_row = (
        event, event, event, "005930  ", 100.0, 3.0, 1, "t1", 3, 4,
    )

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False

        def execute(self, sql, params):
            self.rows = [quote_row] if "ext_src.quotes" in sql else [trade_row]
            self.params = params

        def fetchall(self): return self.rows

    class Conn:
        def cursor(self): return Cursor()

    loaded = load_instrument_events_batch(
        Conn(), instrument_ids=[" 005930 "], start=event,
        end=event + timedelta(seconds=1),
        as_known_at=event + timedelta(seconds=1),
        source=EXTERNAL_EVENT_SOURCE,
    )
    assert list(loaded) == ["005930"]
    quotes, trades = loaded["005930"]
    assert [row.instrument_id for row in quotes] == ["005930"]
    assert [row.instrument_id for row in trades] == ["005930"]


def test_external_raw_evidence_hashes_late_session_rows_in_fixed_window() -> None:
    start = datetime(2026, 8, 14, 0, 0, tzinfo=UTC)
    content_end = datetime(2026, 8, 14, 6, 30, tzinfo=UTC)
    late = content_end - timedelta(seconds=1)
    quote_row = (
        late, late, late, "005930", [99.0], [10.0], [101.0], [11.0],
        "q-late", 11, 13,
    )
    trade_row = (
        late, late, late, "005930", 100.0, 3.0, 1, "t-late", 17, 19,
    )

    class Cursor:
        def __init__(self, conn): self.conn = conn
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params):
            self.conn.params.append(params)
            self.rows = [quote_row] if "ext_src.quotes" in sql else [trade_row]
        def fetchall(self): return self.rows

    class Conn:
        def __init__(self): self.params = []
        def cursor(self): return Cursor(self)

    conn = Conn()
    raw = {}
    loaded = load_instrument_events_batch(
        conn, instrument_ids=["005930"], start=start, end=content_end,
        as_known_at=content_end + timedelta(days=1),
        source=EXTERNAL_EVENT_SOURCE, raw_content_evidence=raw,
        content_end=content_end)

    assert len(loaded["005930"][0]) == 1
    assert len(loaded["005930"][1]) == 1
    assert raw["005930"]["quotes"] == {
        "row_count": 1, "xor_seed_0": 11, "sum_seed_1": 13}
    assert raw["005930"]["ticks"] == {
        "row_count": 1, "xor_seed_0": 17, "sum_seed_1": 19}
    assert all(params[2] == content_end for params in conn.params)

    class NeverConn:
        def cursor(self):
            raise AssertionError("invalid raw window must fail before SQL")

    with pytest.raises(ValueError, match="fixed half-open"):
        load_instrument_events_batch(
            NeverConn(), instrument_ids=["005930"], start=start,
            end=content_end - timedelta(minutes=5),
            as_known_at=content_end + timedelta(days=1),
            source=EXTERNAL_EVENT_SOURCE, raw_content_evidence={},
            content_end=content_end - timedelta(minutes=5))


def test_external_batch_loader_rejects_ambiguous_or_unrequested_symbols() -> None:
    class NeverConn:
        def cursor(self):
            raise AssertionError("invalid identity must fail before SQL")

    with pytest.raises(ValueError, match="exact six-digit KRX trading symbol"):
        load_instrument_events_batch(
            NeverConn(), instrument_ids=["A005930"], start=BASE,
            end=BASE + timedelta(seconds=1),
            as_known_at=BASE + timedelta(seconds=1),
            source=EXTERNAL_EVENT_SOURCE,
        )

    unexpected = (
        BASE, BASE, BASE, "000660", [99.0], [10.0], [101.0], [11.0],
        "q1", 1, 2,
    )

    class Cursor:
        def __enter__(self): return self
        def __exit__(self, *_): return False
        def execute(self, sql, params):
            self.rows = [unexpected] if "ext_src.quotes" in sql else []
        def fetchall(self): return self.rows

    class Conn:
        def cursor(self): return Cursor()

    with pytest.raises(ValueError, match="unrequested instrument"):
        load_instrument_events_batch(
            Conn(), instrument_ids=["005930"], start=BASE,
            end=BASE + timedelta(seconds=1),
            as_known_at=BASE + timedelta(seconds=1),
            source=EXTERNAL_EVENT_SOURCE,
        )


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


def test_latency_quote_sets_execution_but_never_signal_feature() -> None:
    decision_quote = quote(4, 99, 101, 10, 10)
    # This quote is learned 500ms after the decision but before the 1s-latent
    # order arrives.  It may set the wider fill spread/capacity, never the AST
    # feature spread.
    latency_quote = quote(5, 98, 102, 1000, 1, delay_ms=500)
    exit_quote = quote(10, 101, 103, 10, 10)
    spec = IntradayLaneSpec(
        sample_interval_seconds=5, feature_lookback_seconds=10,
        horizons_seconds=(5,), order_latency_ms=1000,
        max_quote_age_seconds=6)
    samples = build_samples(
        [decision_quote, latency_quote, exit_quote], [], spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6))
    assert len(samples) == 1
    sample = samples[0]
    assert sample.source_quote_event_time == decision_quote.event_time
    assert sample.queue_imbalance_l1 == 0.0
    assert sample.spread_bps == pytest.approx(
        (101 - 99) / ((101 + 99) / 2) * 10_000)
    assert sample.entry_bid == latency_quote.best_bid
    assert sample.entry_ask_depth_l1 == 1.0
    assert sample.execution_spread_bps == pytest.approx(
        (102 - 98) / ((102 + 98) / 2) * 10_000)
    assert sample.execution_spread_bps > sample.spread_bps


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
    for sql in (_QUOTE_SQL, _TRADE_SQL, _SOURCE_QUALITY_SQL):
        assert "greatest(received_at, observed_at) <= %s" in sql
    for sql in (_QUOTE_BATCH_SQL, _TRADE_BATCH_SQL,
                _SOURCE_QUALITY_BATCH_SQL):
        assert "instrument_id = any(%s::uuid[])" in sql
        assert "greatest(received_at, observed_at) <= %s" in sql


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
            normalized_quote_ofi=feature,
            bid_depth_l1=10.0,
            ask_depth_l1=10.0,
            book_depth_l1=20.0,
            book_depth_l10=200.0,
            trade_count=10,
            quote_count=10,
            trade_intensity=10.0,
            realized_volatility_bps=1.0,
            entry_bid_depth_l1=10.0,
            entry_ask_depth_l1=10.0,
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
    # Passive labels can extend through one-horizon wait plus one-horizon hold.
    assert result["folds"][0]["train"] == 39  # 48 rows minus 9 overlaps


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
