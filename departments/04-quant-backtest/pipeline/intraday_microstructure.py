#!/usr/bin/env python3
"""Causal intraday microstructure feature and execution-label lane.

The daily alpha factory intentionally compresses one trading session into one
row.  That is useful for daily portfolio selection but destroys the clock on
which order-book signals live.  This module is a separate lane with three hard
rules:

1. a feature may use only events whose ``available_at`` is no later than the
   decision time;
2. an order enters only after the preregistered latency; and
3. a candidate is scored on an executable round trip, not only a future
   mid-price classification label.

The implementation is deliberately deterministic and dependency-free.  It is
small enough to audit, while the database loader materialises only one bounded
instrument shard/session at a time so it never loads the full tick store.
"""

from __future__ import annotations

from array import array
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from statistics import fmean, median, pstdev
from typing import Callable, Iterable, Sequence


UTC = timezone.utc
KST = timezone(timedelta(hours=9))
LANE_VERSION = "krx-intraday-causal-v6"
LOCAL_EVENT_SOURCE = "LOCAL_RECEIPT_CLOCK"
EXTERNAL_EVENT_SOURCE = "EXTERNAL_FDW_EVENT_TIME"
EVENT_SOURCES = frozenset({LOCAL_EVENT_SOURCE, EXTERNAL_EVENT_SOURCE})
RAW_EVENT_GRANULARITY = "RAW_QUOTE_TRADE_EVENTS"
STRICT_TIMESTAMP_POLICY = "STRICT_RECEIPT_ORDER_V1"
COMPLETED_SECOND_POLICY = "COMPLETED_SECOND_MEDIAN_ENVELOPE_V1"
TIMESTAMP_POLICIES = frozenset({
    STRICT_TIMESTAMP_POLICY,
    COMPLETED_SECOND_POLICY,
})
NUMERIC_FEATURES = frozenset({
    "quote_age_ms", "spread_bps", "queue_imbalance_l1",
    "queue_imbalance_l10", "microprice_offset_bps", "trade_flow_imbalance",
    "quote_event_ofi", "normalized_quote_ofi", "bid_depth_l1",
    "ask_depth_l1", "book_depth_l1", "book_depth_l10", "trade_count",
    "quote_count", "trade_intensity", "realized_volatility_bps", "entry_bid",
    "entry_ask", "entry_mid", "multi_level_quote_ofi_l10",
    "normalized_multi_level_quote_ofi_l10", "depth_imbalance_slope",
    "quote_ofi_depth_divergence", "quote_event_transition_count",
    "normalized_quote_ofi_per_event", "signed_trade_volume",
    "trade_volume", "trade_side_known_ratio",
    "quote_ofi_per_trade_volume",
})


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


def _positive(value: float, name: str) -> float:
    out = float(value)
    if not math.isfinite(out) or out <= 0:
        raise ValueError(f"{name} must be finite and positive")
    return out


@dataclass(frozen=True, slots=True)
class QuoteEvent:
    event_time: datetime
    received_at: datetime
    observed_at: datetime
    instrument_id: str
    bid_prices: tuple[float, ...]
    bid_sizes: tuple[float, ...]
    ask_prices: tuple[float, ...]
    ask_sizes: tuple[float, ...]
    source_event_id: str = ""
    source_row_count: int = 1

    def __post_init__(self) -> None:
        event = _aware(self.event_time, "event_time")
        received = _aware(self.received_at, "received_at")
        observed = _aware(self.observed_at, "observed_at")
        object.__setattr__(self, "event_time", event)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "observed_at", observed)
        if observed < received:
            raise ValueError("observed_at precedes received_at")
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        if (isinstance(self.source_row_count, bool) or
                int(self.source_row_count) != self.source_row_count or
                int(self.source_row_count) < 1):
            raise ValueError("source_row_count must be a positive integer")
        object.__setattr__(self, "source_row_count", int(self.source_row_count))
        sizes = (len(self.bid_prices), len(self.bid_sizes),
                 len(self.ask_prices), len(self.ask_sizes))
        if min(sizes) < 1 or sizes[0] != sizes[1] or sizes[2] != sizes[3]:
            raise ValueError("quote price/size ladders must be non-empty and aligned")
        if any(float(v) < 0 or not math.isfinite(float(v))
               for v in (*self.bid_sizes, *self.ask_sizes)):
            raise ValueError("quote sizes must be finite and non-negative")
        bid = _positive(self.bid_prices[0], "best bid")
        ask = _positive(self.ask_prices[0], "best ask")
        if ask < bid:
            raise ValueError("crossed quote is not eligible for the causal lane")

    @property
    def available_at(self) -> datetime:
        return max(self.received_at, self.observed_at)

    @property
    def best_bid(self) -> float:
        return float(self.bid_prices[0])

    @property
    def best_ask(self) -> float:
        return float(self.ask_prices[0])

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2.0


@dataclass(frozen=True, slots=True)
class TradeEvent:
    event_time: datetime
    received_at: datetime
    observed_at: datetime
    instrument_id: str
    price: float
    quantity: float
    side: int
    source_event_id: str = ""

    def __post_init__(self) -> None:
        event = _aware(self.event_time, "event_time")
        received = _aware(self.received_at, "received_at")
        observed = _aware(self.observed_at, "observed_at")
        object.__setattr__(self, "event_time", event)
        object.__setattr__(self, "received_at", received)
        object.__setattr__(self, "observed_at", observed)
        if observed < received:
            raise ValueError("observed_at precedes received_at")
        if not self.instrument_id:
            raise ValueError("instrument_id is required")
        _positive(self.price, "trade price")
        if float(self.quantity) < 0 or not math.isfinite(float(self.quantity)):
            raise ValueError("trade quantity must be finite and non-negative")
        if self.side not in (-1, 0, 1):
            raise ValueError("trade side must be -1, 0, or 1")

    @property
    def available_at(self) -> datetime:
        return max(self.received_at, self.observed_at)


@dataclass(frozen=True, slots=True)
class IntradayLaneSpec:
    sample_interval_seconds: int = 5
    feature_lookback_seconds: int = 30
    horizons_seconds: tuple[int, ...] = (5, 30, 300)
    order_latency_ms: int = 250
    max_quote_age_seconds: float = 5.0
    fee_bps_per_side: float = 0.0
    maker_fee_bps_per_side: float = 0.0

    def __post_init__(self) -> None:
        if self.sample_interval_seconds < 1:
            raise ValueError("sample_interval_seconds must be positive")
        if self.feature_lookback_seconds < 1:
            raise ValueError("feature_lookback_seconds must be positive")
        horizons = tuple(sorted(set(int(v) for v in self.horizons_seconds)))
        if not horizons or horizons[0] < 1:
            raise ValueError("horizons_seconds must contain positive values")
        object.__setattr__(self, "horizons_seconds", horizons)
        if self.order_latency_ms < 0:
            raise ValueError("order_latency_ms must be non-negative")
        if self.max_quote_age_seconds <= 0:
            raise ValueError("max_quote_age_seconds must be positive")
        if self.fee_bps_per_side < 0:
            raise ValueError("fee_bps_per_side must be non-negative")
        if self.maker_fee_bps_per_side < 0:
            raise ValueError("maker_fee_bps_per_side must be non-negative")

    @property
    def purge_gap(self) -> timedelta:
        """Minimum event-time purge/embargo between train and test samples."""
        # A passive order may rest for one horizon and, after a late fill, hold
        # for one full horizon.  Use the longest possible label span for every
        # lane so neither replay loading nor train/test purging truncates it.
        return timedelta(seconds=2 * max(self.horizons_seconds),
                         milliseconds=self.order_latency_ms)


@dataclass(frozen=True, slots=True)
class HorizonLabel:
    horizon_seconds: int
    exit_time: datetime
    future_mid: float
    long_mid_markout_bps: float
    short_mid_markout_bps: float
    long_taker_net_bps: float
    short_taker_net_bps: float
    long_passive_filled: bool
    short_passive_filled: bool
    long_passive_fill_time: datetime | None
    short_passive_fill_time: datetime | None
    long_passive_net_bps: float | None
    short_passive_net_bps: float | None
    # Optional v2 diagnostics are last/defaulted so v1 cache fixtures and
    # callers constructing labels by keyword remain readable.
    long_passive_exit_time: datetime | None = None
    short_passive_exit_time: datetime | None = None
    long_passive_fill_delay_ms: float | None = None
    short_passive_fill_delay_ms: float | None = None
    long_passive_adverse_selection_bps: float | None = None
    short_passive_adverse_selection_bps: float | None = None
    long_passive_nonfill_opportunity_cost_bps: float | None = None
    short_passive_nonfill_opportunity_cost_bps: float | None = None


@dataclass(frozen=True, slots=True)
class IntradaySample:
    instrument_id: str
    decision_time: datetime
    entry_time: datetime
    source_quote_event_time: datetime
    quote_age_ms: float
    spread_bps: float
    queue_imbalance_l1: float
    queue_imbalance_l10: float
    microprice_offset_bps: float
    trade_flow_imbalance: float | None
    quote_event_ofi: float | None
    normalized_quote_ofi: float | None
    bid_depth_l1: float
    ask_depth_l1: float
    book_depth_l1: float
    book_depth_l10: float
    trade_count: int
    quote_count: int
    trade_intensity: float
    realized_volatility_bps: float | None
    entry_bid_depth_l1: float
    entry_ask_depth_l1: float
    entry_bid: float
    entry_ask: float
    entry_mid: float
    labels: tuple[HorizonLabel, ...]
    # New fields stay at the end with defaults for constructor/cache
    # compatibility.  They are derived only from causally visible snapshots and
    # tape volume; none claims MBO order IDs or cancellation attribution.
    multi_level_quote_ofi_l10: float | None = None
    normalized_multi_level_quote_ofi_l10: float | None = None
    depth_imbalance_slope: float = 0.0
    quote_ofi_depth_divergence: float | None = None
    quote_event_transition_count: int = 0
    normalized_quote_ofi_per_event: float | None = None
    signed_trade_volume: float = 0.0
    trade_volume: float = 0.0
    trade_side_known_ratio: float | None = None
    quote_ofi_per_trade_volume: float | None = None
    # External second-clock replay can bound a one-share taker price but cannot
    # identify executable size or market impact.  Keep that limitation beside
    # the sample so downstream reports never turn snapshot size into capacity.
    execution_capacity_supported: bool = True
    execution_spread_bps: float | None = None

    def feature_dict(self) -> dict[str, float | int | None]:
        return {
            "spread_bps": self.spread_bps,
            "queue_imbalance_l1": self.queue_imbalance_l1,
            "queue_imbalance_l10": self.queue_imbalance_l10,
            "microprice_offset_bps": self.microprice_offset_bps,
            "trade_flow_imbalance": self.trade_flow_imbalance,
            "quote_event_ofi": self.quote_event_ofi,
            "normalized_quote_ofi": self.normalized_quote_ofi,
            "multi_level_quote_ofi_l10": self.multi_level_quote_ofi_l10,
            "normalized_multi_level_quote_ofi_l10": (
                self.normalized_multi_level_quote_ofi_l10),
            "depth_imbalance_slope": self.depth_imbalance_slope,
            "quote_ofi_depth_divergence": self.quote_ofi_depth_divergence,
            "quote_event_transition_count": self.quote_event_transition_count,
            "normalized_quote_ofi_per_event": (
                self.normalized_quote_ofi_per_event),
            "signed_trade_volume": self.signed_trade_volume,
            "trade_volume": self.trade_volume,
            "trade_side_known_ratio": self.trade_side_known_ratio,
            "quote_ofi_per_trade_volume": self.quote_ofi_per_trade_volume,
            "bid_depth_l1": self.bid_depth_l1,
            "ask_depth_l1": self.ask_depth_l1,
            "book_depth_l1": self.book_depth_l1,
            "book_depth_l10": self.book_depth_l10,
            "trade_count": self.trade_count,
            "quote_count": self.quote_count,
            "trade_intensity": self.trade_intensity,
            "realized_volatility_bps": self.realized_volatility_bps,
            "quote_age_ms": self.quote_age_ms,
        }


def _imbalance(bid_sizes: Sequence[float], ask_sizes: Sequence[float],
               levels: int) -> float:
    bid = sum(float(v) for v in bid_sizes[:levels])
    ask = sum(float(v) for v in ask_sizes[:levels])
    total = bid + ask
    return (bid - ask) / total if total > 0 else 0.0


def _microprice(quote: QuoteEvent) -> float:
    bid_size = float(quote.bid_sizes[0])
    ask_size = float(quote.ask_sizes[0])
    total = bid_size + ask_size
    if total <= 0:
        return quote.mid
    return ((quote.best_ask * bid_size) +
            (quote.best_bid * ask_size)) / total


def _quote_ofi_step(previous: QuoteEvent, current: QuoteEvent) -> float:
    step = _quote_ofi_step_levels(previous, current, levels=1)[0]
    # L1 is validated by QuoteEvent.__post_init__, so this is unreachable for
    # eligible objects and keeps the historical helper's numeric return type.
    return 0.0 if step is None else step


def _ladder_level(prices: Sequence[float], sizes: Sequence[float], index: int,
                  *, bid: bool) -> tuple[float, float] | None:
    """Return one valid snapshot level without manufacturing sparse depth."""
    if index >= len(prices) or index >= len(sizes):
        return None
    price, size = float(prices[index]), float(sizes[index])
    if (not math.isfinite(price) or price <= 0 or
            not math.isfinite(size) or size < 0):
        return None
    if index:
        previous = float(prices[index - 1])
        if (not math.isfinite(previous) or previous <= 0 or
                (bid and price > previous) or
                (not bid and price < previous)):
            return None
    return price, size


def _quote_ofi_step_levels(previous: QuoteEvent, current: QuoteEvent, *,
                           levels: int = 10) -> tuple[float | None, ...]:
    """Cont-style snapshot-transition OFI at each visible book level.

    This is multi-level *snapshot* OFI.  Without MBO/order identifiers it does
    not label an observed size change as an add, cancellation, or execution.
    """
    out: list[float | None] = []
    for index in range(levels):
        pb = _ladder_level(previous.bid_prices, previous.bid_sizes, index,
                           bid=True)
        cb = _ladder_level(current.bid_prices, current.bid_sizes, index,
                           bid=True)
        pa = _ladder_level(previous.ask_prices, previous.ask_sizes, index,
                           bid=False)
        ca = _ladder_level(current.ask_prices, current.ask_sizes, index,
                           bid=False)
        if any(value is None for value in (pb, cb, pa, ca)):
            out.append(None)
            continue
        previous_bid, previous_bid_size = pb
        current_bid, current_bid_size = cb
        previous_ask, previous_ask_size = pa
        current_ask, current_ask_size = ca
        out.append(
            (current_bid_size if current_bid >= previous_bid else 0.0) -
            (previous_bid_size if current_bid <= previous_bid else 0.0) -
            (current_ask_size if current_ask <= previous_ask else 0.0) +
            (previous_ask_size if current_ask >= previous_ask else 0.0))
    return tuple(out)


def _ordered_quote_stats(events: Sequence[QuoteEvent], *, levels: int = 10
                         ) -> tuple[tuple[float | None, ...], int, bool]:
    """Aggregate MLOFI while failing closed on unsequenced tied snapshots."""
    if len(events) < 2:
        return (tuple(None for _ in range(levels)), 0, False)
    keys = [(event.available_at, event.event_time) for event in events]
    ambiguous = len(set(keys)) != len(keys)
    if ambiguous:
        return (tuple(None for _ in range(levels)), 0, True)
    totals = [0.0] * levels
    counts = [0] * levels
    for previous, current in zip(events, events[1:]):
        for index, step in enumerate(_quote_ofi_step_levels(
                previous, current, levels=levels)):
            if step is not None:
                totals[index] += step
                counts[index] += 1
    expected = len(events) - 1
    return (tuple(totals[index] if counts[index] == expected else None
                  for index in range(levels)), counts[0], False)


def _quote_ofi(events: Sequence[QuoteEvent]) -> float | None:
    """Cont-Kukanov-Stoikov L1 order-flow imbalance over quote updates."""
    return _ordered_quote_stats(events, levels=1)[0][0]


def _passive_fill(events: Sequence[TradeEvent], *, side: int, limit_price: float,
                  queue_ahead: float) -> datetime | None:
    """Conservative FIFO fill: printed opposing volume must clear queue ahead.

    Snapshot data cannot identify cancellations or exact order IDs.  We give no
    cancellation credit, so this is a lower-bound replay rather than a claim of
    exchange-exact queue position.
    """
    remaining = max(0.0, float(queue_ahead))
    for trade_event in events:
        crosses = ((side > 0 and trade_event.side < 0 and
                    float(trade_event.price) <= limit_price) or
                   (side < 0 and trade_event.side > 0 and
                    float(trade_event.price) >= limit_price))
        if not crosses:
            continue
        remaining -= float(trade_event.quantity)
        if remaining <= 0:
            return trade_event.available_at
    return None


def _quote_state(event: QuoteEvent) -> tuple:
    """Snapshot identity used by the fail-closed same-clock policy."""
    return (tuple(event.bid_prices), tuple(event.bid_sizes),
            tuple(event.ask_prices), tuple(event.ask_sizes))


@dataclass(frozen=True, slots=True)
class _CompletedSecondSummary:
    """Order-invariant feature summary for one closed source-clock second."""

    interval_start: datetime
    available_at: datetime
    source_row_count: int
    distinct_state_count: int
    mid: float
    spread_bps: float
    queue_imbalance_l1: float
    queue_imbalance_l10: float
    microprice_offset_bps: float
    bid_depth_l1: float
    ask_depth_l1: float
    book_depth_l1: float
    book_depth_l10: float


def _second_start(value: datetime) -> datetime:
    return _aware(value, "event_time").replace(microsecond=0)


def _completed_second_quotes(
        quotes: Sequence[QuoteEvent],
        ) -> tuple[list[QuoteEvent], list[QuoteEvent],
                   dict[tuple[datetime, datetime], _CompletedSecondSummary]]:
    """Reduce unsequenced rows to closed-second features and price bounds.

    A source timestamp ``s`` represents an unordered multiset observed within
    ``[s, s+1s)``.  Nothing from that set is visible until ``s+1s``.  Feature
    scalars are calculated on every valid source state *before* taking their
    median; no synthetic ladder is presented as an exchange quote.  The second
    returned stream is an internal one-share taker price envelope: buy/cover at
    max ask and sell/short at min bid.  Its sizes are zero because this source
    cannot identify executable capacity or impact.
    """
    grouped: dict[datetime, list[QuoteEvent]] = {}
    for quote in quotes:
        grouped.setdefault(_second_start(quote.event_time), []).append(quote)

    carriers: list[QuoteEvent] = []
    envelopes: list[QuoteEvent] = []
    summaries: dict[tuple[datetime, datetime], _CompletedSecondSummary] = {}
    for interval_start in sorted(grouped):
        rows = grouped[interval_start]
        available = max(
            interval_start + timedelta(seconds=1),
            max(row.available_at for row in rows))
        instrument_ids = {row.instrument_id for row in rows}
        if len(instrument_ids) != 1:
            raise ValueError("completed-second quote bucket mixes instruments")
        instrument_id = next(iter(instrument_ids))

        mids = [row.mid for row in rows]
        spreads = [
            (row.best_ask - row.best_bid) / row.mid * 10_000.0
            for row in rows
        ]
        imbalance_l1 = [
            _imbalance(row.bid_sizes, row.ask_sizes, 1) for row in rows
        ]
        imbalance_l10 = [
            _imbalance(row.bid_sizes, row.ask_sizes, 10) for row in rows
        ]
        microprice_offsets = [
            (_microprice(row) / row.mid - 1.0) * 10_000.0
            for row in rows
        ]
        bid_l1 = [float(row.bid_sizes[0]) for row in rows]
        ask_l1 = [float(row.ask_sizes[0]) for row in rows]
        depth_l1 = [bid + ask for bid, ask in zip(bid_l1, ask_l1)]
        depth_l10 = [
            sum(float(value) for value in row.bid_sizes[:10]) +
            sum(float(value) for value in row.ask_sizes[:10])
            for row in rows
        ]
        summary = _CompletedSecondSummary(
            interval_start=interval_start,
            available_at=available,
            source_row_count=sum(row.source_row_count for row in rows),
            distinct_state_count=len({_quote_state(row) for row in rows}),
            mid=float(median(mids)),
            spread_bps=float(median(spreads)),
            queue_imbalance_l1=float(median(imbalance_l1)),
            queue_imbalance_l10=float(median(imbalance_l10)),
            microprice_offset_bps=float(median(microprice_offsets)),
            bid_depth_l1=float(median(bid_l1)),
            ask_depth_l1=float(median(ask_l1)),
            book_depth_l1=float(median(depth_l1)),
            book_depth_l10=float(median(depth_l10)),
        )
        summaries[(interval_start, available)] = summary

        # A deterministic actual state is only a temporal carrier for lookup;
        # all feature and mid-label scalars come from ``summary`` above.
        carrier_row = min(rows, key=_quote_state)
        carriers.append(QuoteEvent(
            event_time=interval_start,
            received_at=available,
            observed_at=available,
            instrument_id=instrument_id,
            bid_prices=carrier_row.bid_prices,
            bid_sizes=carrier_row.bid_sizes,
            ask_prices=carrier_row.ask_prices,
            ask_sizes=carrier_row.ask_sizes,
            source_event_id=f"completed-second-feature:{interval_start.isoformat()}",
            source_row_count=summary.source_row_count,
        ))

        levels = min(min(len(row.bid_prices), len(row.ask_prices))
                     for row in rows)
        bid_prices = tuple(min(float(row.bid_prices[level]) for row in rows)
                           for level in range(levels))
        ask_prices = tuple(max(float(row.ask_prices[level]) for row in rows)
                           for level in range(levels))
        envelopes.append(QuoteEvent(
            event_time=interval_start,
            received_at=available,
            observed_at=available,
            instrument_id=instrument_id,
            bid_prices=bid_prices,
            bid_sizes=tuple(0.0 for _ in range(levels)),
            ask_prices=ask_prices,
            ask_sizes=tuple(0.0 for _ in range(levels)),
            source_event_id=f"completed-second-envelope:{interval_start.isoformat()}",
            source_row_count=summary.source_row_count,
        ))
    return carriers, envelopes, summaries


def _completed_second_trades(trades: Sequence[TradeEvent]) -> list[TradeEvent]:
    """Delay unordered tape rows until their source-clock second has closed."""
    grouped: dict[tuple[str, datetime], list[TradeEvent]] = {}
    for trade in trades:
        key = (trade.instrument_id, _second_start(trade.event_time))
        grouped.setdefault(key, []).append(trade)
    out = []
    for (_, interval_start), rows in sorted(grouped.items()):
        available = max(interval_start + timedelta(seconds=1),
                        max(row.available_at for row in rows))
        for trade in rows:
            out.append(TradeEvent(
                event_time=interval_start,
                received_at=available,
                observed_at=available,
                instrument_id=trade.instrument_id,
                price=trade.price,
                quantity=trade.quantity,
                side=trade.side,
                source_event_id=trade.source_event_id,
            ))
    return out


def _timestamp_policy(value: str | None) -> str:
    policy = str(value or STRICT_TIMESTAMP_POLICY).upper()
    if policy not in TIMESTAMP_POLICIES:
        raise ValueError(f"unsupported timestamp policy: {value!r}")
    return policy


def _ceil_second(value: datetime) -> datetime:
    value = _aware(value, "entry_time")
    if value.microsecond == 0:
        return value
    return value.replace(microsecond=0) + timedelta(seconds=1)


def effective_purge_gap(
        spec: IntradayLaneSpec,
        timestamp_policy: str = STRICT_TIMESTAMP_POLICY) -> timedelta:
    """Return the longest feature-to-label span under the clock contract."""
    policy = _timestamp_policy(timestamp_policy)
    if policy == STRICT_TIMESTAMP_POLICY:
        return spec.purge_gap
    latency_ms = int(math.ceil(spec.order_latency_ms / 1000.0) * 1000)
    return timedelta(
        seconds=max(spec.horizons_seconds) + latency_ms / 1000.0)


def _build_last_known_resolver(
        events: Sequence[QuoteEvent]) -> array | None:
    """Precompute the latest causally resolvable quote at every row.

    Quotes are sorted by ``(available_at, event_time, source_event_id)`` before
    this helper is called.  For the normal clock contract
    ``event_time <= available_at``, every lookup is therefore a prefix query.
    The array stores the last row in the newest same-clock group whose states
    all agree.  An ambiguous group inherits the preceding resolvable row,
    exactly matching :func:`_last_known_naive` without rescanning history.

    A source-clock exception (exchange time later than availability) turns the
    query into a two-clock search rather than a simple prefix.  Those rows are
    rare and retain the exact naive path instead of admitting an approximation.
    """
    if any(event.event_time > event.available_at for event in events):
        return None
    latest = array("q")
    last_resolvable = -1
    group_start = 0
    while group_start < len(events):
        first = events[group_start]
        key = (first.available_at, first.event_time)
        group_end = group_start + 1
        while (group_end < len(events) and
               (events[group_end].available_at,
                events[group_end].event_time) == key):
            group_end += 1
        state = _quote_state(first)
        if all(_quote_state(events[index]) == state
               for index in range(group_start + 1, group_end)):
            # The naive lookup returns the lexicographically last source row
            # when every tied row describes the same observable book state.
            last_resolvable = group_end - 1
        latest.extend([last_resolvable] * (group_end - group_start))
        group_start = group_end
    return latest


def _last_known_naive(events: Sequence,
                      available_times: Sequence[datetime], at: datetime):
    """Reference two-clock lookup retained for exceptions and regression."""
    index = bisect_right(available_times, at) - 1
    # Some feeds round exchange timestamps and can make a newly received event
    # appear slightly newer than the decision clock.  Walk backwards to the
    # latest *eligible* event; returning None immediately would incorrectly
    # discard an older quote that really was known.
    while index >= 0:
        event = events[index]
        if event.event_time <= at:
            # Source IDs are not exchange sequence numbers.  If multiple
            # different snapshots share both the availability and exchange
            # timestamp (notably the legacy second-clock feed), choosing the
            # lexicographically last ID would invent an intra-second order.
            key = (event.available_at, event.event_time)
            left = index
            while left and (events[left - 1].available_at,
                            events[left - 1].event_time) == key:
                left -= 1
            right = index + 1
            while right < len(events) and (events[right].available_at,
                                           events[right].event_time) == key:
                right += 1
            states = {_quote_state(item) for item in events[left:right]}
            if len(states) == 1:
                return event
            index = left - 1
            continue
        index -= 1
    return None


def _last_known(events: Sequence, available_times: Sequence[datetime],
                at: datetime, resolver: Sequence[int] | None = None):
    """Return the latest unambiguous causal quote.

    ``resolver`` is an exact acceleration artifact, never a different tie
    policy.  Unsupported two-clock exception sets deliberately use the retained
    reference implementation.
    """
    if resolver is None:
        return _last_known_naive(events, available_times, at)
    if len(resolver) != len(events):
        raise ValueError("last-known resolver does not match quote rows")
    index = bisect_right(available_times, at) - 1
    if index < 0:
        return None
    resolved = int(resolver[index])
    return events[resolved] if resolved >= 0 else None


def _fixed_grid(start: datetime, end: datetime, step_seconds: int) -> Iterable[datetime]:
    start = _aware(start, "start")
    end = _aware(end, "end")
    if end <= start:
        return
    epoch = int(start.timestamp())
    first = epoch + ((step_seconds - epoch % step_seconds) % step_seconds)
    current = datetime.fromtimestamp(first, tz=UTC)
    while current < end:
        yield current
        current += timedelta(seconds=step_seconds)


def build_samples(quotes: Sequence[QuoteEvent], trades: Sequence[TradeEvent],
                  spec: IntradayLaneSpec, *, start: datetime,
                  end: datetime,
                  execution_model: str | None = None,
                  timestamp_policy: str = STRICT_TIMESTAMP_POLICY,
                  ) -> list[IntradaySample]:
    """Build point-in-time features and multi-horizon executable labels."""
    if not quotes:
        return []
    policy = _timestamp_policy(timestamp_policy)
    coarse_second = policy == COMPLETED_SECOND_POLICY
    if coarse_second and str(execution_model or "").upper() != "TAKER":
        raise ValueError(
            "completed-second external replay supports TAKER only; passive "
            "queue and fill order are not identifiable")
    quote_instruments = {q.instrument_id for q in quotes}
    trade_instruments = {t.instrument_id for t in trades}
    if len(quote_instruments) != 1 or (trade_instruments - quote_instruments):
        raise ValueError("one instrument per build_samples call is required")

    if coarse_second:
        feature_quotes, execution_quotes, coarse_summaries = \
            _completed_second_quotes(quotes)
        normalized_trades = _completed_second_trades(trades)
    else:
        feature_quotes = list(quotes)
        execution_quotes = feature_quotes
        coarse_summaries = {}
        normalized_trades = list(trades)

    qs = sorted(feature_quotes, key=lambda q: (q.available_at, q.event_time,
                                               q.source_event_id))
    eqs = sorted(execution_quotes,
                 key=lambda q: (q.available_at, q.event_time,
                                q.source_event_id))
    ts = sorted(normalized_trades,
                key=lambda t: (t.available_at, t.event_time,
                               t.source_event_id))
    qa = [q.available_at for q in qs]
    eqa = [q.available_at for q in eqs]
    ta = [t.available_at for t in ts]
    quote_resolver = _build_last_known_resolver(qs)
    execution_quote_resolver = (
        quote_resolver if eqs is qs else _build_last_known_resolver(eqs))
    # Prefix sufficient statistics remove repeated lookback-window list creation.
    # Rows whose exchange clock is later than their availability clock are rare;
    # those windows fall back to the exact filtered calculation below.
    # Exact timestamp keys are contiguous under the causal sort.  A compact
    # byte flag avoids a second datetime-key dictionary for a 600k-row liquid
    # session, while still identifying every unsequenced tied snapshot.
    ambiguous_quote = bytearray(len(qs))
    group_start = 0
    while group_start < len(qs):
        group_end = group_start + 1
        first = qs[group_start]
        while (group_end < len(qs) and
               qs[group_end].available_at == first.available_at and
               qs[group_end].event_time == first.event_time):
            group_end += 1
        if group_end - group_start > 1:
            ambiguous_quote[group_start:group_end] = \
                b"\x01" * (group_end - group_start)
        group_start = group_end
    # ``array`` keeps the ten level prefixes bounded (~72 MiB for 600k rows)
    # instead of allocating millions of boxed Python float/int objects.
    mlofi_prefix = [array("d", [0.0]) for _ in range(10)]
    mlofi_count_prefix = [array("I", [0]) for _ in range(10)]
    quote_ambiguity_prefix = array("I", [0])
    quote_variance_prefix = array("d", [0.0])
    quote_count_prefix = array("Q", [0])
    for index, quote in enumerate(qs):
        if (coarse_second or index == 0 or ambiguous_quote[index] or
                ambiguous_quote[index - 1]):
            ofi_steps = (None,) * 10
            if coarse_second and index > 0:
                current_mid = coarse_summaries[
                    (quote.event_time, quote.available_at)].mid
                previous = qs[index - 1]
                previous_mid = coarse_summaries[
                    (previous.event_time, previous.available_at)].mid
                change = math.log(current_mid / previous_mid) * 10_000.0
                variance_step = change * change
            else:
                variance_step = 0.0
        else:
            previous = qs[index - 1]
            ofi_steps = _quote_ofi_step_levels(previous, quote, levels=10)
            change = math.log(quote.mid / previous.mid) * 10_000.0
            variance_step = change * change
        for level, step in enumerate(ofi_steps):
            mlofi_prefix[level].append(
                mlofi_prefix[level][-1] + (0.0 if step is None else step))
            mlofi_count_prefix[level].append(
                mlofi_count_prefix[level][-1] + int(step is not None))
        quote_ambiguity_prefix.append(
            quote_ambiguity_prefix[-1] +
            int(ambiguous_quote[index]))
        quote_variance_prefix.append(
            quote_variance_prefix[-1] + variance_step)
        quote_count_prefix.append(
            quote_count_prefix[-1] + int(quote.source_row_count))
    quote_clock_exceptions = [
        index for index, quote in enumerate(qs)
        if quote.event_time > quote.available_at]
    trade_signed_prefix = array("d", [0.0])
    trade_known_volume_prefix = array("d", [0.0])
    trade_total_volume_prefix = array("d", [0.0])
    for trade in ts:
        trade_signed_prefix.append(
            trade_signed_prefix[-1] + float(trade.side) * float(trade.quantity))
        trade_known_volume_prefix.append(
            trade_known_volume_prefix[-1] + (
                float(trade.quantity) if trade.side != 0 else 0.0))
        trade_total_volume_prefix.append(
            trade_total_volume_prefix[-1] + float(trade.quantity))
    trade_clock_exceptions = [
        index for index, trade in enumerate(ts)
        if trade.event_time > trade.available_at]
    need_passive = (execution_model is None or
                    str(execution_model).upper().startswith("PASSIVE"))
    latency = timedelta(milliseconds=spec.order_latency_ms)
    lookback = timedelta(seconds=spec.feature_lookback_seconds)
    samples: list[IntradaySample] = []

    for decision in _fixed_grid(start, end, spec.sample_interval_seconds):
        requested_entry_time = decision + latency
        entry_time = (_ceil_second(requested_entry_time)
                      if coarse_second else requested_entry_time)
        decision_quote = _last_known(
            qs, qa, decision, resolver=quote_resolver)
        if decision_quote is None:
            continue
        entry_quote = _last_known(
            eqs, eqa, entry_time, resolver=execution_quote_resolver)
        entry_feature_quote = _last_known(
            qs, qa, entry_time, resolver=quote_resolver)
        if entry_quote is None or entry_feature_quote is None:
            continue
        feature_age = (decision - decision_quote.available_at).total_seconds()
        entry_age = (entry_time - entry_quote.available_at).total_seconds()
        if (feature_age < 0 or feature_age > spec.max_quote_age_seconds or
                entry_age < 0 or entry_age > spec.max_quote_age_seconds):
            continue

        window_start = decision - lookback
        qlo = bisect_right(qa, window_start)
        qhi = bisect_right(qa, decision)
        tlo = bisect_right(ta, window_start)
        thi = bisect_right(ta, decision)

        qx_lo = bisect_left(quote_clock_exceptions, qlo)
        qx_hi = bisect_left(quote_clock_exceptions, qhi)
        hidden_quotes = [index for index in quote_clock_exceptions[qx_lo:qx_hi]
                         if qs[index].event_time > decision]
        if hidden_quotes:
            visible_quotes = [q for q in qs[qlo:qhi]
                              if q.event_time <= decision]
            quote_count = sum(q.source_row_count for q in visible_quotes)
            mlofi_levels, quote_transitions, ambiguous_quotes = \
                _ordered_quote_stats(visible_quotes, levels=10)
            if ambiguous_quotes:
                realized_volatility = None
            else:
                quote_mids = [quote.mid for quote in visible_quotes]
                quote_returns = [math.log(right / left) * 10_000.0
                                 for left, right in
                                 zip(quote_mids, quote_mids[1:])]
                realized_volatility = (
                    math.sqrt(sum(value * value for value in quote_returns))
                    if quote_returns else None)
        else:
            state_count = qhi - qlo
            quote_count = quote_count_prefix[qhi] - quote_count_prefix[qlo]
            ambiguous_quotes = (
                quote_ambiguity_prefix[qhi] - quote_ambiguity_prefix[qlo] > 0)
            if coarse_second:
                # A completed bucket is an unordered state multiset.  Between-
                # bucket median-mid volatility is valid, but Cont OFI/MLOFI and
                # event transition counts require an event sequence we do not
                # possess.
                mlofi_levels = (None,) * 10
                quote_transitions = 0
                variance = (quote_variance_prefix[qhi] -
                            quote_variance_prefix[qlo + 1]
                            if state_count >= 2 else 0.0)
                realized_volatility = (
                    math.sqrt(max(0.0, variance))
                    if state_count >= 2 else None)
            elif state_count < 2 or ambiguous_quotes:
                mlofi_levels = (None,) * 10
                quote_transitions = 0
                realized_volatility = None
            else:
                quote_transitions = (
                    mlofi_count_prefix[0][qhi] -
                    mlofi_count_prefix[0][qlo + 1])
                level_values: list[float | None] = []
                for level in range(10):
                    count = (mlofi_count_prefix[level][qhi] -
                             mlofi_count_prefix[level][qlo + 1])
                    value = (mlofi_prefix[level][qhi] -
                             mlofi_prefix[level][qlo + 1])
                    level_values.append(
                        value if count == quote_transitions else None)
                mlofi_levels = tuple(level_values)
                variance = (quote_variance_prefix[qhi] -
                            quote_variance_prefix[qlo + 1])
                realized_volatility = math.sqrt(max(0.0, variance))
        quote_ofi = mlofi_levels[0]
        multi_level_ofi = (
            sum(value for value in mlofi_levels if value is not None)
            if quote_ofi is not None else None)

        tx_lo = bisect_left(trade_clock_exceptions, tlo)
        tx_hi = bisect_left(trade_clock_exceptions, thi)
        hidden_trades = [index for index in trade_clock_exceptions[tx_lo:tx_hi]
                         if ts[index].event_time > decision]
        signed = trade_signed_prefix[thi] - trade_signed_prefix[tlo]
        known_volume = (trade_known_volume_prefix[thi] -
                        trade_known_volume_prefix[tlo])
        total_volume = (trade_total_volume_prefix[thi] -
                        trade_total_volume_prefix[tlo])
        for index in hidden_trades:
            trade = ts[index]
            signed -= float(trade.side) * float(trade.quantity)
            if trade.side != 0:
                known_volume -= float(trade.quantity)
            total_volume -= float(trade.quantity)
        trade_count = thi - tlo - len(hidden_trades)
        trade_flow = signed / known_volume if known_volume > 0 else None
        decision_summary = (coarse_summaries[
                                (decision_quote.event_time,
                                 decision_quote.available_at)]
                            if coarse_second else None)
        entry_summary = (coarse_summaries[
                            (entry_feature_quote.event_time,
                             entry_feature_quote.available_at)]
                         if coarse_second else None)
        if decision_summary is not None:
            bid_l1 = decision_summary.bid_depth_l1
            ask_l1 = decision_summary.ask_depth_l1
            depth_l1 = decision_summary.book_depth_l1
            depth_l10 = decision_summary.book_depth_l10
        else:
            bid_l1 = float(decision_quote.bid_sizes[0])
            ask_l1 = float(decision_quote.ask_sizes[0])
            depth_l1 = bid_l1 + ask_l1
            depth_l10 = (
                sum(float(v) for v in decision_quote.bid_sizes[:10]) +
                sum(float(v) for v in decision_quote.ask_sizes[:10]))
        normalized_ofi = (quote_ofi / depth_l1
                          if quote_ofi is not None and depth_l1 > 0 else None)
        multi_level_ofi_depth = sum(
            float(decision_quote.bid_sizes[level]) +
            float(decision_quote.ask_sizes[level])
            for level, value in enumerate(mlofi_levels)
            if value is not None and
            level < len(decision_quote.bid_sizes) and
            level < len(decision_quote.ask_sizes))
        normalized_multi_level_ofi = (
            multi_level_ofi / multi_level_ofi_depth
            if multi_level_ofi is not None and multi_level_ofi_depth > 0
            else None)
        queue_imbalance_l1 = (
            decision_summary.queue_imbalance_l1
            if decision_summary is not None else
            _imbalance(decision_quote.bid_sizes, decision_quote.ask_sizes, 1))
        queue_imbalance_l10 = (
            decision_summary.queue_imbalance_l10
            if decision_summary is not None else
            _imbalance(decision_quote.bid_sizes, decision_quote.ask_sizes, 10))
        depth_imbalance_slope = queue_imbalance_l1 - queue_imbalance_l10
        ofi_depth_divergence = (
            normalized_ofi - normalized_multi_level_ofi
            if normalized_ofi is not None and
            normalized_multi_level_ofi is not None else None)
        feature_mid = (decision_summary.mid if decision_summary is not None
                       else decision_quote.mid)
        entry_mid = (entry_summary.mid if entry_summary is not None
                     else entry_feature_quote.mid)
        spread_bps = (
            decision_summary.spread_bps
            if decision_summary is not None else
            ((decision_quote.best_ask - decision_quote.best_bid) /
             feature_mid * 10_000.0))
        microprice_offset = (
            decision_summary.microprice_offset_bps
            if decision_summary is not None else
            (_microprice(decision_quote) / feature_mid - 1.0) * 10_000.0)

        labels: list[HorizonLabel] = []
        for horizon in spec.horizons_seconds:
            exit_time = entry_time + timedelta(seconds=horizon)
            exit_quote = _last_known(
                eqs, eqa, exit_time, resolver=execution_quote_resolver)
            exit_feature_quote = _last_known(
                qs, qa, exit_time, resolver=quote_resolver)
            if exit_quote is None or exit_feature_quote is None:
                continue
            exit_age = (exit_time - exit_quote.available_at).total_seconds()
            if exit_age < 0 or exit_age > spec.max_quote_age_seconds:
                continue
            exit_summary = (
                coarse_summaries[(exit_feature_quote.event_time,
                                  exit_feature_quote.available_at)]
                if coarse_second else None)
            future_mid = (exit_summary.mid if exit_summary is not None
                          else exit_feature_quote.mid)
            long_mid = (future_mid / entry_mid - 1.0) * 10_000.0
            short_mid = (entry_mid / future_mid - 1.0) * 10_000.0
            fees = 2.0 * spec.fee_bps_per_side
            # Conservative taker round trip: cross at entry and again at exit.
            long_net = (exit_quote.best_bid / entry_quote.best_ask - 1.0) * 10_000.0 - fees
            short_net = (entry_quote.best_bid / exit_quote.best_ask - 1.0) * 10_000.0 - fees
            long_fill = short_fill = None
            if need_passive:
                future_lo = bisect_right(ta, entry_time)
                future_hi = bisect_right(ta, exit_time)
                future_trades = [t for t in ts[future_lo:future_hi]
                                 if entry_time < t.event_time <= exit_time]
                long_fill = _passive_fill(
                    future_trades, side=1, limit_price=entry_quote.best_bid,
                    queue_ahead=float(entry_quote.bid_sizes[0]))
                short_fill = _passive_fill(
                    future_trades, side=-1, limit_price=entry_quote.best_ask,
                    queue_ahead=float(entry_quote.ask_sizes[0]))
            passive_fees = (spec.maker_fee_bps_per_side +
                            spec.fee_bps_per_side)
            # ``horizon`` is the holding period, not a common deadline that
            # silently shortens exposure for a late passive fill.  Orders may
            # rest for at most one horizon; a filled order exits exactly one
            # horizon after its observed fill timestamp.
            long_passive_exit_time = (
                long_fill + timedelta(seconds=horizon)
                if long_fill is not None else None)
            short_passive_exit_time = (
                short_fill + timedelta(seconds=horizon)
                if short_fill is not None else None)

            def fresh_quote(at: datetime | None) -> QuoteEvent | None:
                if at is None:
                    return None
                candidate = _last_known(
                    eqs, eqa, at, resolver=execution_quote_resolver)
                if candidate is None:
                    return None
                age = (at - candidate.available_at).total_seconds()
                return (candidate if 0 <= age <= spec.max_quote_age_seconds
                        else None)

            long_passive_exit_quote = fresh_quote(long_passive_exit_time)
            short_passive_exit_quote = fresh_quote(short_passive_exit_time)
            long_fill_quote = fresh_quote(long_fill)
            short_fill_quote = fresh_quote(short_fill)
            long_passive_net = (
                (long_passive_exit_quote.best_bid /
                 entry_quote.best_bid - 1.0) * 10_000.0 - passive_fees
                if long_passive_exit_quote is not None else None)
            short_passive_net = (
                (entry_quote.best_ask /
                 short_passive_exit_quote.best_ask - 1.0) * 10_000.0 -
                passive_fees
                if short_passive_exit_quote is not None else None)
            long_adverse_selection = (
                -(long_passive_exit_quote.mid / long_fill_quote.mid - 1.0) *
                10_000.0
                if long_passive_exit_quote is not None and
                long_fill_quote is not None else None)
            short_adverse_selection = (
                -(short_fill_quote.mid / short_passive_exit_quote.mid - 1.0) *
                10_000.0
                if short_passive_exit_quote is not None and
                short_fill_quote is not None else None)
            # Positive means a conservative unfilled order missed a favorable
            # executable move; negative means non-fill avoided a loss.
            long_nonfill_opportunity = (
                (exit_quote.best_bid / entry_quote.best_bid - 1.0) *
                10_000.0 - passive_fees
                if need_passive and long_fill is None else None)
            short_nonfill_opportunity = (
                (entry_quote.best_ask / exit_quote.best_ask - 1.0) *
                10_000.0 - passive_fees
                if need_passive and short_fill is None else None)
            labels.append(HorizonLabel(
                horizon_seconds=horizon,
                exit_time=exit_time,
                future_mid=future_mid,
                long_mid_markout_bps=long_mid,
                short_mid_markout_bps=short_mid,
                long_taker_net_bps=long_net,
                short_taker_net_bps=short_net,
                long_passive_filled=long_fill is not None,
                short_passive_filled=short_fill is not None,
                long_passive_fill_time=long_fill,
                short_passive_fill_time=short_fill,
                long_passive_net_bps=long_passive_net,
                short_passive_net_bps=short_passive_net,
                long_passive_exit_time=long_passive_exit_time,
                short_passive_exit_time=short_passive_exit_time,
                long_passive_fill_delay_ms=(
                    (long_fill - entry_time).total_seconds() * 1000.0
                    if long_fill is not None else None),
                short_passive_fill_delay_ms=(
                    (short_fill - entry_time).total_seconds() * 1000.0
                    if short_fill is not None else None),
                long_passive_adverse_selection_bps=long_adverse_selection,
                short_passive_adverse_selection_bps=short_adverse_selection,
                long_passive_nonfill_opportunity_cost_bps=(
                    long_nonfill_opportunity),
                short_passive_nonfill_opportunity_cost_bps=(
                    short_nonfill_opportunity),
            ))
        if not labels:
            continue
        samples.append(IntradaySample(
            instrument_id=decision_quote.instrument_id,
            decision_time=decision,
            entry_time=entry_time,
            source_quote_event_time=decision_quote.event_time,
            quote_age_ms=feature_age * 1000.0,
            spread_bps=spread_bps,
            queue_imbalance_l1=queue_imbalance_l1,
            queue_imbalance_l10=queue_imbalance_l10,
            microprice_offset_bps=microprice_offset,
            trade_flow_imbalance=trade_flow,
            quote_event_ofi=quote_ofi,
            normalized_quote_ofi=normalized_ofi,
            bid_depth_l1=bid_l1,
            ask_depth_l1=ask_l1,
            book_depth_l1=depth_l1,
            book_depth_l10=depth_l10,
            trade_count=trade_count,
            quote_count=quote_count,
            trade_intensity=(trade_count /
                             max(1.0, spec.feature_lookback_seconds)),
            realized_volatility_bps=realized_volatility,
            entry_bid_depth_l1=float(entry_quote.bid_sizes[0]),
            entry_ask_depth_l1=float(entry_quote.ask_sizes[0]),
            entry_bid=entry_quote.best_bid,
            entry_ask=entry_quote.best_ask,
            entry_mid=entry_mid,
            labels=tuple(labels),
            multi_level_quote_ofi_l10=multi_level_ofi,
            normalized_multi_level_quote_ofi_l10=(
                normalized_multi_level_ofi),
            depth_imbalance_slope=depth_imbalance_slope,
            quote_ofi_depth_divergence=ofi_depth_divergence,
            quote_event_transition_count=quote_transitions,
            normalized_quote_ofi_per_event=(
                normalized_ofi / quote_transitions
                if normalized_ofi is not None and quote_transitions > 0
                else None),
            signed_trade_volume=signed,
            trade_volume=total_volume,
            trade_side_known_ratio=(known_volume / total_volume
                                    if total_volume > 0 else None),
            quote_ofi_per_trade_volume=(
                quote_ofi / total_volume
                if quote_ofi is not None and total_volume > 0 else None),
            execution_capacity_supported=not coarse_second,
            # Keep the executable entry spread separate from the decision-time
            # feature spread.  A quote learned during order latency may widen
            # the price to cross even though it was unavailable to the signal.
            execution_spread_bps=(
                (entry_quote.best_ask - entry_quote.best_bid) /
                ((entry_quote.best_ask + entry_quote.best_bid) / 2.0) *
                10_000.0),
        ))
    return samples


def audit_causality(samples: Sequence[IntradaySample],
                    spec: IntradayLaneSpec) -> dict:
    findings: list[str] = []
    for sample in samples:
        if sample.source_quote_event_time > sample.decision_time:
            findings.append(f"future feature quote at {sample.decision_time.isoformat()}")
        if sample.entry_time < sample.decision_time:
            findings.append(f"negative latency at {sample.decision_time.isoformat()}")
        if sample.quote_age_ms > spec.max_quote_age_seconds * 1000.0:
            findings.append(f"stale quote at {sample.decision_time.isoformat()}")
        for label in sample.labels:
            if label.exit_time <= sample.entry_time:
                findings.append(f"non-forward label at {sample.decision_time.isoformat()}")
            for side in ("long", "short"):
                fill = getattr(label, f"{side}_passive_fill_time")
                passive_exit = getattr(label, f"{side}_passive_exit_time")
                if fill is None and passive_exit is not None:
                    findings.append(
                        f"passive exit without fill at {sample.decision_time.isoformat()}")
                # A missing optional v2 target can come from a legacy cached
                # label.  When present, however, it must encode the full fixed
                # post-fill holding horizon exactly.
                if fill is not None and passive_exit is not None:
                    expected = fill + timedelta(seconds=label.horizon_seconds)
                    if passive_exit != expected:
                        findings.append(
                            f"passive holding horizon mismatch at "
                            f"{sample.decision_time.isoformat()}")
    return {
        "lane_version": LANE_VERSION,
        "status": ("NO_EVIDENCE" if not samples else
                   "PASS" if not findings else "FAIL"),
        "sample_count": len(samples),
        "findings": findings,
        "purge_gap_seconds": spec.purge_gap.total_seconds(),
    }


def score_signal(samples: Sequence[IntradaySample],
                 signal: Callable[[IntradaySample], float | None], *,
                 horizon_seconds: int, threshold: float = 0.0,
                 execution: str = "TAKER") -> dict:
    """Score a signed signal on mid markout and conservative taker P&L."""
    execution = str(execution).upper()
    if execution not in {"TAKER", "PASSIVE_FIFO_LOWER_BOUND"}:
        raise ValueError(f"unsupported execution mode: {execution}")
    mid_values: list[float] = []
    filled_mid_values: list[float] = []
    net_values: list[float] = []
    long_count = short_count = opportunities = 0
    for sample in samples:
        raw = signal(sample)
        if raw is None or not math.isfinite(float(raw)):
            continue
        value = float(raw)
        side = 1 if value > threshold else -1 if value < -threshold else 0
        if side == 0:
            continue
        label = next((v for v in sample.labels
                      if v.horizon_seconds == horizon_seconds), None)
        if label is None:
            continue
        opportunities += 1
        if side > 0:
            long_count += 1
            mid_values.append(label.long_mid_markout_bps)
            net = (label.long_taker_net_bps if execution == "TAKER"
                   else label.long_passive_net_bps)
        else:
            short_count += 1
            mid_values.append(label.short_mid_markout_bps)
            net = (label.short_taker_net_bps if execution == "TAKER"
                   else label.short_passive_net_bps)
        if net is not None:
            net_values.append(net)
            filled_mid_values.append(mid_values[-1])

    count = len(net_values)
    mean = fmean(net_values) if count else None
    deviation = pstdev(net_values) if count > 1 else None
    return {
        "lane_version": LANE_VERSION,
        "horizon_seconds": horizon_seconds,
        "execution": execution,
        "opportunities": opportunities,
        "trades": count,
        "long_trades": long_count,
        "short_trades": short_count,
        "mean_mid_markout_bps": fmean(filled_mid_values) if count else None,
        "mean_mid_markout_all_opportunities_bps": (
            fmean(mid_values) if opportunities else None),
        "mean_taker_net_bps": mean,
        "mean_net_bps_per_fill": mean,
        "mean_net_bps_per_opportunity": (
            sum(net_values) / opportunities if opportunities else None),
        "fill_rate": count / opportunities if opportunities else None,
        "taker_hit_rate": (sum(v > 0 for v in net_values) / count if count else None),
        "net_hit_rate": (sum(v > 0 for v in net_values) / count if count else None),
        "taker_information_ratio": (
            mean / deviation if mean is not None and deviation not in (None, 0.0)
            else None),
        "decision": "PROMISING" if mean is not None and mean > 0 else "REJECT",
    }


def _solve_linear(system: list[list[float]], target: list[float]) -> list[float]:
    """Solve a small dense linear system with pivoted Gauss-Jordan elimination."""
    n = len(target)
    augmented = [list(map(float, system[row])) + [float(target[row])]
                 for row in range(n)]
    for column in range(n):
        pivot = max(range(column, n), key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError("singular regression system")
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        divisor = augmented[column][column]
        augmented[column] = [value / divisor for value in augmented[column]]
        for row in range(n):
            if row == column:
                continue
            factor = augmented[row][column]
            if factor:
                augmented[row] = [left - factor * right for left, right in
                                  zip(augmented[row], augmented[column])]
    return [augmented[row][-1] for row in range(n)]


def walk_forward_linear_score(
        samples: Sequence[IntradaySample], *, feature_names: Sequence[str],
        horizon_seconds: int, spec: IntradayLaneSpec, n_splits: int = 3,
        initial_train_fraction: float = 0.4, ridge: float = 1e-3,
        minimum_predicted_edge_bps: float = 0.0) -> dict:
    """Expanding purged walk-forward fit with an executable abstention gate.

    The model predicts future mid-price markout.  It trades only when the
    predicted absolute move clears the *current full spread*, round-trip fees,
    and an optional safety margin.  Thus a classifier that merely guesses the
    next direction cannot earn a false alpha verdict.
    """
    names = tuple(str(v) for v in feature_names)
    if not names or any(name not in NUMERIC_FEATURES for name in names):
        raise ValueError(f"unknown or empty feature_names: {names}")
    if n_splits < 1:
        raise ValueError("n_splits must be positive")
    if not 0.1 <= initial_train_fraction < 0.9:
        raise ValueError("initial_train_fraction must be in [0.1, 0.9)")
    if ridge < 0 or minimum_predicted_edge_bps < 0:
        raise ValueError("ridge and minimum_predicted_edge_bps must be non-negative")

    rows = []
    for sample in sorted(samples, key=lambda row: row.decision_time):
        label = next((value for value in sample.labels
                      if value.horizon_seconds == horizon_seconds), None)
        values = [getattr(sample, name) for name in names]
        if label is None or any(value is None or not math.isfinite(float(value))
                                for value in values):
            continue
        rows.append((sample, label, [float(value) for value in values]))
    n_rows = len(rows)
    initial = max(2, int(n_rows * initial_train_fraction))
    remaining = n_rows - initial
    test_size = max(1, remaining // n_splits) if remaining > 0 else 0
    predictions: list[tuple[IntradaySample, HorizonLabel, float, int]] = []
    fold_reports: list[dict] = []

    for fold in range(n_splits):
        test_lo = initial + fold * test_size
        test_hi = n_rows if fold == n_splits - 1 else min(n_rows, test_lo + test_size)
        if test_lo >= n_rows or test_lo >= test_hi:
            continue
        test_start = rows[test_lo][0].decision_time
        train = [row for row in rows[:test_lo]
                 if row[0].entry_time + spec.purge_gap <= test_start]
        test = rows[test_lo:test_hi]
        if len(train) < max(10, len(names) + 2):
            fold_reports.append({
                "fold": fold + 1, "status": "SKIP_INSUFFICIENT_PURGED_TRAIN",
                "train": len(train), "test": len(test),
            })
            continue

        x_train = [row[2] for row in train]
        y_train = [row[1].long_mid_markout_bps for row in train]
        mean_x = [fmean(row[column] for row in x_train)
                  for column in range(len(names))]
        scale_x = []
        for column, mean_value in enumerate(mean_x):
            variance = fmean((row[column] - mean_value) ** 2 for row in x_train)
            scale_x.append(math.sqrt(variance) if variance > 0 else 1.0)
        design = [[1.0] + [(value - mean_x[column]) / scale_x[column]
                            for column, value in enumerate(row)]
                  for row in x_train]
        width = len(names) + 1
        gram = [[sum(row[left] * row[right] for row in design)
                 for right in range(width)] for left in range(width)]
        for diagonal in range(1, width):
            gram[diagonal][diagonal] += ridge
        rhs = [sum(row[column] * target for row, target in zip(design, y_train))
               for column in range(width)]
        beta = _solve_linear(gram, rhs)
        for sample, label, values in test:
            z_test = [(value - mean_x[column]) / scale_x[column]
                      for column, value in enumerate(values)]
            predicted = beta[0] + sum(weight * value for weight, value
                                      in zip(beta[1:], z_test))
            threshold = (sample.spread_bps + 2.0 * spec.fee_bps_per_side +
                         minimum_predicted_edge_bps)
            side = 1 if predicted > threshold else -1 if predicted < -threshold else 0
            predictions.append((sample, label, predicted, side))
        fold_reports.append({
            "fold": fold + 1, "status": "PASS", "train": len(train),
            "test": len(test), "test_start": test_start.isoformat(),
        })

    mid_values: list[float] = []
    net_values: list[float] = []
    predicted_values: list[float] = []
    for _, label, predicted, side in predictions:
        if side == 0:
            continue
        predicted_values.append(predicted)
        if side > 0:
            mid_values.append(label.long_mid_markout_bps)
            net_values.append(label.long_taker_net_bps)
        else:
            mid_values.append(label.short_mid_markout_bps)
            net_values.append(label.short_taker_net_bps)
    count = len(net_values)
    mean_net = fmean(net_values) if count else None
    return {
        "lane_version": LANE_VERSION,
        "model": "PURGED_EXPANDING_RIDGE_WITH_SPREAD_ABSTENTION",
        "features": list(names),
        "horizon_seconds": horizon_seconds,
        "purge_gap_seconds": spec.purge_gap.total_seconds(),
        "eligible_oos_samples": len(predictions),
        "trades": count,
        "coverage": count / len(predictions) if predictions else 0.0,
        "mean_predicted_mid_bps": (
            fmean(abs(v) for v in predicted_values) if count else None),
        "mean_mid_markout_bps": fmean(mid_values) if count else None,
        "mean_taker_net_bps": mean_net,
        "taker_hit_rate": sum(v > 0 for v in net_values) / count if count else None,
        "decision": "PROMISING" if mean_net is not None and mean_net > 0 else "REJECT",
        "folds": fold_reports,
    }


_QUOTE_SQL = """
select event_time, received_at, observed_at, instrument_id::text,
       bid_prices, bid_sizes, ask_prices, ask_sizes, source_event_id
  from market.market_quotes
 where instrument_id = %s
   and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
   and bid_prices[1] > 0 and ask_prices[1] > 0
   and ask_prices[1] >= bid_prices[1]
 order by event_time, source_event_id
"""

_TRADE_SQL = """
select event_time, received_at, observed_at, instrument_id::text,
       price, quantity, side, source_event_id
  from market.market_ticks
 where instrument_id = %s
   and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
 order by event_time, source_event_id
"""

_QUOTE_BATCH_SQL = """
select event_time, received_at, observed_at, instrument_id::text,
       bid_prices, bid_sizes, ask_prices, ask_sizes, source_event_id
  from market.market_quotes
 where instrument_id = any(%s::uuid[])
   and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
   and bid_prices[1] > 0 and ask_prices[1] > 0
   and ask_prices[1] >= bid_prices[1]
 order by instrument_id, event_time, source_event_id
"""

_TRADE_BATCH_SQL = """
select event_time, received_at, observed_at, instrument_id::text,
       price, quantity, side, source_event_id
  from market.market_ticks
 where instrument_id = any(%s::uuid[])
   and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
 order by instrument_id, event_time, source_event_id
"""

_SOURCE_QUALITY_SQL = """
select count(*) as total_quotes,
       count(*) filter (where received_at is null) as quotes_without_received_at,
       count(*) filter (where bid_prices[1] <= 0 or ask_prices[1] <= 0) as nonpositive_quotes,
       count(*) filter (where ask_prices[1] < bid_prices[1]) as crossed_quotes,
       count(*) filter (where received_at is not null
                         and bid_prices[1] > 0 and ask_prices[1] > 0
                         and ask_prices[1] >= bid_prices[1]) as eligible_quotes
  from market.market_quotes
 where instrument_id = %s
   and event_time >= %s and event_time < %s
   and greatest(received_at, observed_at) <= %s
"""

_SOURCE_QUALITY_BATCH_SQL = """
select instrument_id::text,
       count(*) as total_quotes,
       count(*) filter (where received_at is null) as quotes_without_received_at,
       count(*) filter (where bid_prices[1] <= 0 or ask_prices[1] <= 0) as nonpositive_quotes,
       count(*) filter (where ask_prices[1] < bid_prices[1]) as crossed_quotes,
       count(*) filter (where received_at is not null
                         and bid_prices[1] > 0 and ask_prices[1] > 0
                         and ask_prices[1] >= bid_prices[1]) as eligible_quotes
  from market.market_quotes
 where instrument_id = any(%s::uuid[])
   and event_time >= %s and event_time < %s
   and greatest(received_at, observed_at) <= %s
 group by instrument_id
 order by instrument_id
"""

# Trading_bot kept the complete 61-session L10/tape history in its own
# TimescaleDB.  ``ext_src`` is a read-only postgres_fdw mapping already owned by
# the market database.  The legacy schema has only the exchange-second clock
# ``ts``; it does not preserve a separate receipt clock.  We therefore set the
# in-memory availability clock to ``ts`` and mark every such quote as
# ``quotes_without_received_at`` in the quality report.  This is an explicit
# event-time historical replay, never a claim of arrival-clock PIT evidence.
_EXTERNAL_QUOTE_SQL = """
select ts, ts, ts, btrim(symbol),
       array[coalesce(bid1,0),coalesce(bid2,0),coalesce(bid3,0),
             coalesce(bid4,0),coalesce(bid5,0),coalesce(bid6,0),
             coalesce(bid7,0),coalesce(bid8,0),coalesce(bid9,0),
             coalesce(bid10,0)],
       array[coalesce(bid_vol1,0),coalesce(bid_vol2,0),
             coalesce(bid_vol3,0),coalesce(bid_vol4,0),
             coalesce(bid_vol5,0),coalesce(bid_vol6,0),
             coalesce(bid_vol7,0),coalesce(bid_vol8,0),
             coalesce(bid_vol9,0),coalesce(bid_vol10,0)],
       array[coalesce(ask1,0),coalesce(ask2,0),coalesce(ask3,0),
             coalesce(ask4,0),coalesce(ask5,0),coalesce(ask6,0),
             coalesce(ask7,0),coalesce(ask8,0),coalesce(ask9,0),
             coalesce(ask10,0)],
       array[coalesce(ask_vol1,0),coalesce(ask_vol2,0),
             coalesce(ask_vol3,0),coalesce(ask_vol4,0),
             coalesce(ask_vol5,0),coalesce(ask_vol6,0),
             coalesce(ask_vol7,0),coalesce(ask_vol8,0),
             coalesce(ask_vol9,0),coalesce(ask_vol10,0)],
       concat('extq:',btrim(symbol),':',extract(epoch from ts)::bigint,':',
               bid1,':',ask1,':',bid_vol1,':',ask_vol1),
       hash_record_extended(quotes, 0),
       hash_record_extended(quotes, 1)::numeric
  from ext_src.quotes quotes
 where symbol = %s
    and ts >= %s and ts < %s and ts <= %s
 order by ts, 9
"""

_EXTERNAL_TRADE_SQL = """
select ts, ts, ts, btrim(symbol), price, volume,
       case when ofi_contrib > 0 then 1
            when ofi_contrib < 0 then -1 else 0 end,
       concat('extt:',btrim(symbol),':',extract(epoch from ts)::bigint,':',
               price,':',volume,':',ofi_contrib),
       hash_record_extended(ticks, 0),
       hash_record_extended(ticks, 1)::numeric
  from ext_src.ticks ticks
 where symbol = %s
    and ts >= %s and ts < %s and ts <= %s
    and price > 0
 order by ts, 8
"""

_EXTERNAL_QUOTE_BATCH_SQL = _EXTERNAL_QUOTE_SQL.replace(
    "symbol = %s", "symbol = any(%s)").replace(
        "order by ts, 9", "order by btrim(symbol), ts, 9")
_EXTERNAL_TRADE_BATCH_SQL = _EXTERNAL_TRADE_SQL.replace(
    "symbol = %s", "symbol = any(%s)").replace(
        "order by ts, 8", "order by btrim(symbol), ts, 8")

_EXTERNAL_SOURCE_QUALITY_SQL = """
select count(*) as total_quotes,
       count(*) as quotes_without_received_at,
       count(*) filter (where bid1 <= 0 or ask1 <= 0) as nonpositive_quotes,
       count(*) filter (where ask1 < bid1) as crossed_quotes,
       count(*) filter (where bid1 > 0 and ask1 > 0 and ask1 >= bid1)
         as eligible_quotes
  from ext_src.quotes
 where symbol = %s
   and ts >= %s and ts < %s and ts <= %s
"""

_EXTERNAL_SOURCE_QUALITY_BATCH_SQL = """
select btrim(symbol), count(*) as total_quotes,
       count(*) as quotes_without_received_at,
       count(*) filter (where bid1 <= 0 or ask1 <= 0) as nonpositive_quotes,
       count(*) filter (where ask1 < bid1) as crossed_quotes,
       count(*) filter (where bid1 > 0 and ask1 > 0 and ask1 >= bid1)
         as eligible_quotes
  from ext_src.quotes
 where symbol = any(%s)
   and ts >= %s and ts < %s and ts <= %s
 group by btrim(symbol)
 order by btrim(symbol)
"""


def _event_source(value: str | None) -> str:
    source = str(value or LOCAL_EVENT_SOURCE).upper()
    if source not in EVENT_SOURCES:
        raise ValueError(
            f"unsupported intraday event source: {value!r}; only raw "
            "quote/trade event contracts are replayable (daily aggregates "
            "are not event sources)")
    return source


def _instrument_key(value, source: str) -> str:
    """Return the exact replay key and reject ambiguous external identities."""
    event_source = _event_source(source)
    key = str(value).strip() if value is not None else ""
    if not key:
        raise ValueError("intraday replay instrument identifier is required")
    if (event_source == EXTERNAL_EVENT_SOURCE
            and (len(key) != 6 or not key.isascii() or not key.isdigit())):
        raise ValueError(
            "external replay requires an exact six-digit KRX trading symbol; "
            f"received {value!r}")
    return key


def _source_sql(source: str, local: str, external: str) -> str:
    return external if _event_source(source) == EXTERNAL_EVENT_SOURCE else local


def _quote_event(row, *, instrument_id: str | None = None) -> QuoteEvent:
    return QuoteEvent(
        event_time=row[0], received_at=row[1], observed_at=row[2],
        instrument_id=instrument_id or str(row[3]).strip(),
        bid_prices=tuple(float(v) for v in row[4]),
        bid_sizes=tuple(float(v) for v in row[5]),
        ask_prices=tuple(float(v) for v in row[6]),
        ask_sizes=tuple(float(v) for v in row[7]),
        source_event_id=row[8],
    )


def _trade_event(row, *, instrument_id: str | None = None) -> TradeEvent:
    return TradeEvent(
        event_time=row[0], received_at=row[1], observed_at=row[2],
        instrument_id=instrument_id or str(row[3]).strip(),
        price=float(row[4]), quantity=float(row[5]),
        side=int(row[6]), source_event_id=row[7],
    )


def _quality_record(total, missing_clock, nonpositive, crossed, eligible) -> dict:
    rejected = int(missing_clock) + int(nonpositive) + int(crossed)
    return {
        "total_quotes": int(total),
        "eligible_quotes": int(eligible),
        "quotes_without_received_at": int(missing_clock),
        "nonpositive_quotes": int(nonpositive),
        "crossed_quotes": int(crossed),
        "rejected_category_count_upper_bound": rejected,
        "status": ("NO_DATA" if int(total) == 0 else
                   "FAIL" if int(eligible) == 0 else
                   "WARN" if rejected else "PASS"),
    }


def _fetch_chunks(cursor, size: int = 10_000):
    """Bound temporary driver-row memory without requiring a named cursor."""
    fetchmany = getattr(cursor, "fetchmany", None)
    if fetchmany is None:
        yield from cursor.fetchall()
        return
    while True:
        rows = fetchmany(size)
        if not rows:
            return
        yield from rows


def _raw_content_add(raw_content_evidence: dict, source: str, row,
                     *, content_end: datetime | None) -> None:
    """Fold the exact external composite hashes consumed by the replay query."""
    if content_end is not None and row[0] >= content_end:
        return
    symbol = _instrument_key(row[3], EXTERNAL_EVENT_SOURCE)
    state = raw_content_evidence.setdefault(symbol, {
        "quotes": {"row_count": 0, "xor_seed_0": 0, "sum_seed_1": 0},
        "ticks": {"row_count": 0, "xor_seed_0": 0, "sum_seed_1": 0},
    })[source]
    h0, h1 = int(row[-2]), int(row[-1])
    state["row_count"] += 1
    unsigned = (int(state["xor_seed_0"]) & ((1 << 64) - 1)) ^ \
        (h0 & ((1 << 64) - 1))
    state["xor_seed_0"] = (
        unsigned if unsigned < (1 << 63) else unsigned - (1 << 64))
    state["sum_seed_1"] += h1


def load_instrument_events(conn, *, instrument_id: str, start: datetime,
                           end: datetime, as_known_at: datetime,
                           source: str = LOCAL_EVENT_SOURCE,
                           ) -> tuple[list[QuoteEvent], list[TradeEvent]]:
    """Read one bounded instrument slice from an explicit source contract."""
    event_source = _event_source(source)
    instrument_id = _instrument_key(instrument_id, event_source)
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    if cutoff < end:
        raise ValueError("as_known_at must cover the requested event-time interval")
    params = (instrument_id, start, end, cutoff)
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(event_source, _QUOTE_SQL,
                                   _EXTERNAL_QUOTE_SQL),
                       params)
        quotes = [_quote_event(
            row, instrument_id=_instrument_key(row[3], event_source))
            for row in _fetch_chunks(cursor)]
        cursor.execute(_source_sql(event_source, _TRADE_SQL,
                                   _EXTERNAL_TRADE_SQL),
                       params)
        trades = [_trade_event(
            row, instrument_id=_instrument_key(row[3], event_source))
            for row in _fetch_chunks(cursor)]
    unexpected = (
        {event.instrument_id for event in quotes}
        | {event.instrument_id for event in trades}
    ) - {instrument_id}
    if unexpected:
        raise ValueError(
            "intraday source returned events for unrequested instruments: "
            f"{sorted(unexpected)}")
    return quotes, trades


def load_instrument_events_batch(conn, *, instrument_ids, start: datetime,
                                 end: datetime, as_known_at: datetime,
                                 source: str = LOCAL_EVENT_SOURCE,
                                 raw_content_evidence: dict | None = None,
                                 content_end: datetime | None = None,
                                 ) -> dict[str, tuple[list[QuoteEvent],
                                                       list[TradeEvent]]]:
    """Read a bounded shard in two SQL scans and group rows by instrument."""
    event_source = _event_source(source)
    ids = tuple(dict.fromkeys(
        _instrument_key(value, event_source) for value in instrument_ids
        if value is not None and str(value).strip()))
    if not ids:
        return {}
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    if cutoff < end:
        raise ValueError("as_known_at must cover the requested event-time interval")
    grouped = {instrument_id: ([], []) for instrument_id in ids}
    if raw_content_evidence is not None:
        if event_source != EXTERNAL_EVENT_SOURCE:
            raise ValueError(
                "raw_content_evidence is supported only for external replay")
        if content_end is None:
            raise ValueError("content_end is required for external raw evidence")
        content_end = _aware(content_end, "content_end")
        local_day = start.astimezone(KST).date()
        expected_start = datetime.combine(
            local_day, datetime.min.time(), KST).replace(
                hour=9).astimezone(UTC)
        expected_end = datetime.combine(
            local_day, datetime.min.time(), KST).replace(
                hour=15, minute=30).astimezone(UTC)
        if start != expected_start or content_end != expected_end or end != expected_end:
            raise ValueError(
                "external raw evidence requires the fixed half-open "
                "[09:00,15:30) Asia/Seoul query window")
        for instrument_id in ids:
            raw_content_evidence.setdefault(instrument_id, {
                "quotes": {"row_count": 0, "xor_seed_0": 0,
                           "sum_seed_1": 0},
                "ticks": {"row_count": 0, "xor_seed_0": 0,
                          "sum_seed_1": 0},
            })
    params = (list(ids), start, end, cutoff)
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(
            event_source, _QUOTE_BATCH_SQL, _EXTERNAL_QUOTE_BATCH_SQL), params)
        for row in _fetch_chunks(cursor):
            instrument = _instrument_key(row[3], event_source)
            if instrument not in grouped:
                raise ValueError(
                    "intraday quote source returned an unrequested "
                    f"instrument: {instrument!r}")
            if raw_content_evidence is not None:
                _raw_content_add(raw_content_evidence, "quotes", row,
                                 content_end=content_end)
            if (float(row[4][0]) > 0 and float(row[6][0]) > 0
                    and float(row[6][0]) >= float(row[4][0])):
                grouped[instrument][0].append(
                    _quote_event(row, instrument_id=instrument))
        cursor.execute(_source_sql(
            event_source, _TRADE_BATCH_SQL, _EXTERNAL_TRADE_BATCH_SQL), params)
        for row in _fetch_chunks(cursor):
            instrument = _instrument_key(row[3], event_source)
            if instrument not in grouped:
                raise ValueError(
                    "intraday trade source returned an unrequested "
                    f"instrument: {instrument!r}")
            if raw_content_evidence is not None:
                _raw_content_add(raw_content_evidence, "ticks", row,
                                 content_end=content_end)
            if float(row[5]) > 0:
                grouped[instrument][1].append(
                    _trade_event(row, instrument_id=instrument))
    return grouped


def source_quality(conn, *, instrument_id: str, start: datetime, end: datetime,
                   as_known_at: datetime,
                   source: str = LOCAL_EVENT_SOURCE) -> dict:
    """Count source rows rejected before object construction."""
    event_source = _event_source(source)
    instrument_id = _instrument_key(instrument_id, event_source)
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(
                       event_source, _SOURCE_QUALITY_SQL,
                       _EXTERNAL_SOURCE_QUALITY_SQL),
                       (instrument_id, start, end, cutoff))
        total, missing_clock, nonpositive, crossed, eligible = cursor.fetchone()
    # Categories can overlap.  Their sum is a diagnostic upper bound, while the
    # exact eligible count is reported by the loader.
    return _quality_record(total, missing_clock, nonpositive, crossed, eligible)


def source_quality_batch(conn, *, instrument_ids, start: datetime, end: datetime,
                         as_known_at: datetime,
                         source: str = LOCAL_EVENT_SOURCE) -> dict[str, dict]:
    """Return source diagnostics for a shard with one grouped query."""
    event_source = _event_source(source)
    ids = tuple(dict.fromkeys(
        _instrument_key(value, event_source) for value in instrument_ids
        if value is not None and str(value).strip()))
    if not ids:
        return {}
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(
                       event_source, _SOURCE_QUALITY_BATCH_SQL,
                       _EXTERNAL_SOURCE_QUALITY_BATCH_SQL),
                       (list(ids), start, end, cutoff))
        rows = cursor.fetchall()
    out = {instrument_id: _quality_record(0, 0, 0, 0, 0)
           for instrument_id in ids}
    for row in rows:
        instrument = _instrument_key(row[0], event_source)
        if instrument not in out:
            raise ValueError(
                "intraday quality source returned an unrequested "
                f"instrument: {instrument!r}")
        out[instrument] = _quality_record(*row[1:])
    return out


def manifest(spec: IntradayLaneSpec,
             *, source: str = LOCAL_EVENT_SOURCE,
             timestamp_policy: str | None = None) -> dict:
    """Serializable contract persisted with every intraday experiment."""
    event_source = _event_source(source)
    external = event_source == EXTERNAL_EVENT_SOURCE
    policy = _timestamp_policy(
        timestamp_policy or (COMPLETED_SECOND_POLICY if external
                             else STRICT_TIMESTAMP_POLICY))
    completed_second = policy == COMPLETED_SECOND_POLICY
    effective_latency_ceiling_ms = (
        int(math.ceil(spec.order_latency_ms / 1000.0) * 1000)
        if completed_second else spec.order_latency_ms)
    effective_purge_gap_seconds = effective_purge_gap(
        spec, policy).total_seconds()
    payload = asdict(spec)
    payload.update({
        "lane_version": LANE_VERSION,
        "event_source": event_source,
        "source_granularity": RAW_EVENT_GRANULARITY,
        "daily_aggregate_replay_allowed": False,
        "timestamp_policy": policy,
        "clock_aggregation_version": (
            "completed-second-state-median-taker-envelope-v1"
            if completed_second else None),
        "clock": (
            "SOURCE_SECOND_INTERVAL=[ts,ts+1s); available_at=ts+1s; "
            "separate receipt clock unavailable"
            if completed_second and external else
            "SOURCE_SECOND_INTERVAL=[event_time,event_time+1s); "
            "available_at=max(interval_end,all_row_receipt_clocks)"
            if completed_second else
            "AVAILABLE_AT=max(received_at,observed_at)"),
        "source_timestamp_resolution": (
            "SECOND; intra-second event sequence unavailable"
            if external else "SOURCE_DEFINED; receipt ordering used when distinct"),
        "same_timestamp_order": (
            "unordered completed-second multiset; scalar state features are "
            "median summaries; ordered quote OFI/MLOFI fail closed as unavailable"
            if completed_second else
            "different snapshots sharing available_at+event_time are "
            "ambiguous; ordered quote OFI/MLOFI fail closed"),
        "clock_summaries": {
            "wall_time": "seconds in causal decision windows",
            "quote_event": (
                "source snapshot-row count; not exchange-unique events or "
                "MBO messages" if completed_second else
                "count of visible snapshot rows and ordered transitions; "
                "not MBO messages"),
            "trade_volume": (
                "observed trade-message multiplicity and quantity; no "
                "exchange trade ID is available to prove unique prints; "
                "directional ratio separately reports known-side coverage"),
        },
        "multi_level_ofi": (
            "UNAVAILABLE_WITHOUT_INTRA_SECOND_SEQUENCE; no add/cancel attribution"
            if completed_second else
            "Cont-style per-level snapshot-transition OFI summed over visible "
            "L1..L10; no add/cancel attribution"),
        "coarse_feature_contract": (
            "calculate scalar spread/imbalance/microprice/depth per raw "
            "state, then take the within-second median"
            if completed_second else None),
        "coarse_taker_price_contract": (
            "conditional one-share price bound: buy_or_cover=max(ask1), "
            "sell_or_short=min(bid1); not an actual quote, fill, capacity, "
            "or market-impact claim" if completed_second else None),
        "arrival_clock_pit": not external,
        "historical_replay_only": external,
        "evidence_limit": (
            "external source cannot audit network arrival latency or "
            "out-of-order receipt; forward receipt-clock confirmation required"
            if external else None),
        "feature_cutoff": (
            "completed source second available_at<=decision_time "
            "(historical search only; no receipt clock)"
            if completed_second and external else
            "completed source second and every included row receipt "
            "available_at<=decision_time"
            if completed_second else
            "event_time<=decision_time and available_at<=decision_time"),
        "requested_order_latency_ms": spec.order_latency_ms,
        "effective_order_latency": (
            "ceil(decision_time+requested_latency) to the next whole-second "
            "boundary" if completed_second else "requested latency exactly"),
        "effective_order_latency_ceiling_ms": effective_latency_ceiling_ms,
        "entry_rule": (
            "completed-second conservative taker envelope at effective entry "
            "boundary" if completed_second else
            "latest visible quote at decision_time+order_latency"),
        "label_rule": (
            "median-mid diagnostic plus conservative taker envelope at "
            "effective_entry_time+horizon" if completed_second else
            "taker latest visible quote at entry_time+horizon"),
        "passive_label_rule": (
            "NOT_IDENTIFIABLE_WITH_COMPLETED_SECOND_SOURCE"
            if completed_second else
            "order timeout=entry_time+horizon; if filled, exit=fill_time+horizon"),
        "execution_model": "TAKER_BOTH_SIDES",
        "passive_execution_model": (
            "UNSUPPORTED" if completed_second else
            "FIFO_NO_CANCELLATION_CREDIT_LOWER_BOUND"),
        "passive_exact_queue_supported": False,
        "passive_exact_queue_blocker": (
            "second-clock snapshot/tape rows have neither intra-second order "
            "nor order IDs/MBO queue" if completed_second else
            "snapshot L10 has no order IDs/MBO queue"),
        "passive_diagnostics": (
            None if completed_second else
            "fill delay, signed adverse-selection cost, and nonfill "
            "opportunity cost; diagnostics do not upgrade the FIFO lower bound"),
        "execution_capacity_supported": not completed_second,
        "purge_gap_seconds": effective_purge_gap_seconds,
    })
    return payload
