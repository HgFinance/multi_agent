from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (PIPELINE, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from intraday_ast_contract import (  # noqa: E402
    QUOTE_EVENT_CLOCK,
    TRADE_VOLUME_CLOCK,
    WALL_TIME_CLOCK,
    clock_domains_of,
    evaluate,
    unit_of,
)
from intraday_microstructure import (  # noqa: E402
    EXTERNAL_EVENT_SOURCE,
    HorizonLabel,
    IntradayLaneSpec,
    QuoteEvent,
    TradeEvent,
    _build_last_known_resolver,
    _last_known,
    _last_known_naive,
    audit_causality,
    build_samples,
    manifest,
)


UTC = timezone.utc
BASE = datetime(2026, 8, 14, tzinfo=UTC)
IID = "005930"


def quote(second: int, bid_sizes, ask_sizes, *, bid: float = 100.0,
          ask: float = 102.0, source: str | None = None) -> QuoteEvent:
    at = BASE + timedelta(seconds=second)
    return QuoteEvent(
        event_time=at,
        received_at=at,
        observed_at=at,
        instrument_id=IID,
        bid_prices=tuple(bid - level for level in range(10)),
        bid_sizes=tuple(float(value) for value in bid_sizes),
        ask_prices=tuple(ask + level for level in range(10)),
        ask_sizes=tuple(float(value) for value in ask_sizes),
        source_event_id=source or f"q-{second}",
    )


def clocked_quote(event_second: int, available_second: int, bid: float,
                  source: str) -> QuoteEvent:
    event = BASE + timedelta(seconds=event_second)
    available = BASE + timedelta(seconds=available_second)
    return QuoteEvent(
        event_time=event,
        received_at=available,
        observed_at=available,
        instrument_id=IID,
        bid_prices=(bid,),
        bid_sizes=(10.0,),
        ask_prices=(bid + 2.0,),
        ask_sizes=(10.0,),
        source_event_id=source,
    )


def trade(second: int, side: int, quantity: float, price: float) -> TradeEvent:
    at = BASE + timedelta(seconds=second)
    return TradeEvent(
        event_time=at,
        received_at=at,
        observed_at=at,
        instrument_id=IID,
        price=price,
        quantity=quantity,
        side=side,
        source_event_id=f"t-{second}-{side}",
    )


def one_sample(quotes, trades=(), *, passive: bool = False):
    spec = IntradayLaneSpec(
        sample_interval_seconds=5,
        feature_lookback_seconds=10,
        horizons_seconds=(5,),
        order_latency_ms=0,
        max_quote_age_seconds=10,
    )
    samples = build_samples(
        quotes,
        trades,
        spec,
        start=BASE + timedelta(seconds=5),
        end=BASE + timedelta(seconds=6),
        execution_model=("PASSIVE_FIFO_LOWER_BOUND" if passive else "TAKER"),
    )
    assert len(samples) == 1
    return samples[0], spec


def test_visible_l10_snapshot_transitions_and_clock_summaries_are_exact() -> None:
    initial_bid = [10, 20, *([10] * 8)]
    initial_ask = [15, 25, *([10] * 8)]
    current_bid = [15, 15, *([10] * 8)]
    current_ask = [10, 30, *([10] * 8)]
    sample, _ = one_sample(
        [quote(0, initial_bid, initial_ask),
         quote(4, current_bid, current_ask),
         quote(10, current_bid, current_ask, bid=101, ask=103)],
        [trade(2, 1, 30, 102), trade(3, 0, 10, 101)],
    )

    # L1 contributes +10 shares; L2 contributes -10; L3..L10 are unchanged.
    assert sample.quote_event_ofi == pytest.approx(10.0)
    assert sample.multi_level_quote_ofi_l10 == pytest.approx(0.0)
    assert sample.normalized_quote_ofi == pytest.approx(10.0 / 25.0)
    assert sample.normalized_multi_level_quote_ofi_l10 == 0.0
    assert sample.quote_ofi_depth_divergence == pytest.approx(0.4)
    assert sample.queue_imbalance_l1 == pytest.approx(0.2)
    assert sample.depth_imbalance_slope == pytest.approx(
        0.2 - ((110.0 - 120.0) / 230.0))
    assert sample.quote_event_transition_count == 1
    assert sample.normalized_quote_ofi_per_event == pytest.approx(0.4)
    assert sample.signed_trade_volume == 30.0
    assert sample.trade_volume == 40.0
    assert sample.trade_side_known_ratio == pytest.approx(0.75)
    assert sample.quote_ofi_per_trade_volume == pytest.approx(0.25)


def test_different_snapshots_on_same_source_clock_fail_ordered_ofi_closed() -> None:
    sizes = [10] * 10
    tied_a = quote(4, [20] * 10, sizes, source="same-second-a")
    tied_b = quote(4, [30] * 10, sizes, source="same-second-b")
    sample, spec = one_sample([
        quote(0, sizes, sizes), tied_a, tied_b,
        quote(10, sizes, sizes, bid=101, ask=103),
    ])

    assert sample.source_quote_event_time == BASE
    assert sample.quote_event_ofi is None
    assert sample.multi_level_quote_ofi_l10 is None
    assert sample.quote_event_transition_count == 0
    replay = manifest(spec, source=EXTERNAL_EVENT_SOURCE)
    assert replay["source_timestamp_resolution"].startswith("SECOND")
    assert "fail closed" in replay["same_timestamp_order"]
    assert "no add/cancel attribution" in replay["multi_level_ofi"]


@pytest.mark.parametrize("seed", range(5))
def test_last_known_prefix_matches_naive_for_random_ties(seed: int) -> None:
    rng = random.Random(20260818 + seed)
    quotes = []
    for group in range(80):
        available_second = group * 2
        event_second = available_second - rng.randint(0, min(2, group * 2))
        rows = rng.randint(1, 4)
        identical = rng.choice((True, False))
        base_bid = 100.0 + group / 100.0
        for row in range(rows):
            quotes.append(clocked_quote(
                event_second, available_second,
                base_bid if identical else base_bid + row / 1_000.0,
                f"q-{group:03d}-{row:02d}"))
    quotes.sort(key=lambda item: (
        item.available_at, item.event_time, item.source_event_id))
    available = [item.available_at for item in quotes]
    resolver = _build_last_known_resolver(quotes)
    assert resolver is not None

    query_rng = random.Random(90210 + seed)
    for _ in range(300):
        at = BASE + timedelta(seconds=query_rng.uniform(-1.0, 162.0))
        expected = _last_known_naive(quotes, available, at)
        actual = _last_known(quotes, available, at, resolver=resolver)
        assert actual is expected


def test_last_known_clock_exception_retains_exact_naive_path() -> None:
    quotes = [
        clocked_quote(0, 0, 100.0, "old"),
        # This rounded exchange clock is later than its receipt clock.
        clocked_quote(7, 2, 101.0, "future-clock"),
        clocked_quote(3, 3, 102.0, "tied-a"),
        clocked_quote(3, 3, 103.0, "tied-b"),
        clocked_quote(4, 4, 104.0, "new"),
    ]
    quotes.sort(key=lambda item: (
        item.available_at, item.event_time, item.source_event_id))
    available = [item.available_at for item in quotes]
    resolver = _build_last_known_resolver(quotes)
    assert resolver is None
    for tenth in range(-10, 101):
        at = BASE + timedelta(seconds=tenth / 10.0)
        assert _last_known(
            quotes, available, at, resolver=resolver) is _last_known_naive(
                quotes, available, at)


def test_all_ambiguous_groups_use_logarithmic_prefix_lookups() -> None:
    groups = 4_096
    quotes = []
    for second in range(groups):
        quotes.extend((
            clocked_quote(second, second, 100.0, f"q-{second:05d}-a"),
            clocked_quote(second, second, 101.0, f"q-{second:05d}-b"),
        ))
    resolver = _build_last_known_resolver(quotes)
    assert resolver is not None
    assert len(resolver) == len(quotes)
    assert set(resolver) == {-1}

    class CountingSequence:
        def __init__(self, rows):
            self.rows = rows
            self.reads = 0

        def __len__(self):
            return len(self.rows)

        def __getitem__(self, index):
            self.reads += 1
            return self.rows[index]

    event_view = CountingSequence(quotes)
    time_view = CountingSequence([item.available_at for item in quotes])
    query_count = 0
    for second in range(0, groups, 8):
        query_count += 1
        assert _last_known(
            event_view, time_view, BASE + timedelta(seconds=second),
            resolver=resolver) is None

    # bisect_right is logarithmic and an unresolved prefix never dereferences
    # historical quote rows.  This guards the former O(decisions * quotes)
    # regression without relying on wall-clock timing.
    assert time_view.reads <= query_count * (len(quotes).bit_length() + 1)
    assert event_view.reads == 0


def test_passive_fill_gets_full_post_fill_holding_horizon_and_diagnostics() -> None:
    sizes = [10] * 10
    sample, spec = one_sample(
        [quote(0, sizes, sizes),
         quote(4, sizes, sizes),
         quote(10, sizes, sizes, bid=101, ask=103),
         quote(11, sizes, sizes, bid=110, ask=112)],
        [trade(6, -1, 10, 100)],
        passive=True,
    )
    label: HorizonLabel = sample.labels[0]

    assert label.exit_time == BASE + timedelta(seconds=10)  # taker clock
    assert label.long_passive_fill_time == BASE + timedelta(seconds=6)
    assert label.long_passive_exit_time == BASE + timedelta(seconds=11)
    assert label.long_passive_fill_delay_ms == 1000.0
    assert label.long_passive_net_bps == pytest.approx(1000.0)
    assert label.long_passive_adverse_selection_bps < 0  # favorable post-fill
    assert label.short_passive_filled is False
    assert label.short_passive_nonfill_opportunity_cost_bps is not None
    assert audit_causality([sample], spec)["status"] == "PASS"
    assert spec.purge_gap == timedelta(seconds=10)
    assert "exit=fill_time+horizon" in manifest(spec)["passive_label_rule"]


def test_ast_exposes_unit_safe_event_and_volume_clock_domains() -> None:
    expression = {
        "op": "rolling_mean",
        "seconds": 30,
        "arg": {
            "op": "sub",
            "args": [
                {"op": "field", "field": "normalized_quote_ofi_per_event"},
                {"op": "field", "field": "quote_ofi_per_trade_volume"},
            ],
        },
    }
    assert unit_of(expression) == "RATIO"
    assert clock_domains_of(expression) == {
        WALL_TIME_CLOCK, QUOTE_EVENT_CLOCK, TRADE_VOLUME_CLOCK}
    assert unit_of({"op": "field", "field": "trade_volume"}) == "SHARES"

    sizes = [10] * 10
    sample, _ = one_sample(
        [quote(0, sizes, sizes), quote(4, [20] * 10, sizes),
         quote(10, sizes, sizes, bid=101, ask=103)],
        [trade(2, 1, 10, 102)],
    )
    assert evaluate([sample], {
        "op": "field", "field": "depth_imbalance_slope"}) == [
            sample.depth_imbalance_slope]
