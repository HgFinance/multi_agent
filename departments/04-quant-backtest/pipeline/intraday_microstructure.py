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

from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import math
from statistics import fmean, pstdev
from typing import Callable, Iterable, Sequence


UTC = timezone.utc
LANE_VERSION = "krx-intraday-causal-v1"
LOCAL_EVENT_SOURCE = "LOCAL_RECEIPT_CLOCK"
EXTERNAL_EVENT_SOURCE = "EXTERNAL_FDW_EVENT_TIME"
EVENT_SOURCES = frozenset({LOCAL_EVENT_SOURCE, EXTERNAL_EVENT_SOURCE})
NUMERIC_FEATURES = frozenset({
    "quote_age_ms", "spread_bps", "queue_imbalance_l1",
    "queue_imbalance_l10", "microprice_offset_bps", "trade_flow_imbalance",
    "quote_event_ofi", "normalized_quote_ofi", "bid_depth_l1",
    "ask_depth_l1", "book_depth_l1", "book_depth_l10", "trade_count",
    "quote_count", "trade_intensity", "realized_volatility_bps", "entry_bid",
    "entry_ask", "entry_mid",
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
        return timedelta(seconds=max(self.horizons_seconds),
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

    def feature_dict(self) -> dict[str, float | int | None]:
        return {
            "spread_bps": self.spread_bps,
            "queue_imbalance_l1": self.queue_imbalance_l1,
            "queue_imbalance_l10": self.queue_imbalance_l10,
            "microprice_offset_bps": self.microprice_offset_bps,
            "trade_flow_imbalance": self.trade_flow_imbalance,
            "quote_event_ofi": self.quote_event_ofi,
            "normalized_quote_ofi": self.normalized_quote_ofi,
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
    pb, cb = previous.best_bid, current.best_bid
    pa, ca = previous.best_ask, current.best_ask
    pbs, cbs = float(previous.bid_sizes[0]), float(current.bid_sizes[0])
    pas, cas = float(previous.ask_sizes[0]), float(current.ask_sizes[0])
    return ((cbs if cb >= pb else 0.0) -
            (pbs if cb <= pb else 0.0) -
            (cas if ca <= pa else 0.0) +
            (pas if ca >= pa else 0.0))


def _quote_ofi(events: Sequence[QuoteEvent]) -> float | None:
    """Cont-Kukanov-Stoikov L1 order-flow imbalance over quote updates."""
    if len(events) < 2:
        return None
    return sum(_quote_ofi_step(previous, current)
               for previous, current in zip(events, events[1:]))


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


def _last_known(events: Sequence, available_times: Sequence[datetime],
                at: datetime):
    index = bisect_right(available_times, at) - 1
    # Some feeds round exchange timestamps and can make a newly received event
    # appear slightly newer than the decision clock.  Walk backwards to the
    # latest *eligible* event; returning None immediately would incorrectly
    # discard an older quote that really was known.
    while index >= 0:
        event = events[index]
        if event.event_time <= at:
            return event
        index -= 1
    return None


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
                  execution_model: str | None = None) -> list[IntradaySample]:
    """Build point-in-time features and multi-horizon executable labels."""
    if not quotes:
        return []
    quote_instruments = {q.instrument_id for q in quotes}
    trade_instruments = {t.instrument_id for t in trades}
    if len(quote_instruments) != 1 or (trade_instruments - quote_instruments):
        raise ValueError("one instrument per build_samples call is required")

    qs = sorted(quotes, key=lambda q: (q.available_at, q.event_time,
                                       q.source_event_id))
    ts = sorted(trades, key=lambda t: (t.available_at, t.event_time,
                                       t.source_event_id))
    qa = [q.available_at for q in qs]
    ta = [t.available_at for t in ts]
    # Prefix sufficient statistics remove repeated lookback-window list creation.
    # Rows whose exchange clock is later than their availability clock are rare;
    # those windows fall back to the exact filtered calculation below.
    quote_ofi_prefix = [0.0]
    quote_variance_prefix = [0.0]
    for index, quote in enumerate(qs):
        if index == 0:
            ofi_step = variance_step = 0.0
        else:
            previous = qs[index - 1]
            ofi_step = _quote_ofi_step(previous, quote)
            change = math.log(quote.mid / previous.mid) * 10_000.0
            variance_step = change * change
        quote_ofi_prefix.append(quote_ofi_prefix[-1] + ofi_step)
        quote_variance_prefix.append(
            quote_variance_prefix[-1] + variance_step)
    quote_clock_exceptions = [
        index for index, quote in enumerate(qs)
        if quote.event_time > quote.available_at]
    trade_signed_prefix = [0.0]
    trade_volume_prefix = [0.0]
    for trade in ts:
        trade_signed_prefix.append(
            trade_signed_prefix[-1] + float(trade.side) * float(trade.quantity))
        trade_volume_prefix.append(
            trade_volume_prefix[-1] + (
                float(trade.quantity) if trade.side != 0 else 0.0))
    trade_clock_exceptions = [
        index for index, trade in enumerate(ts)
        if trade.event_time > trade.available_at]
    need_passive = (execution_model is None or
                    str(execution_model).upper().startswith("PASSIVE"))
    latency = timedelta(milliseconds=spec.order_latency_ms)
    lookback = timedelta(seconds=spec.feature_lookback_seconds)
    samples: list[IntradaySample] = []

    for decision in _fixed_grid(start, end, spec.sample_interval_seconds):
        entry_time = decision + latency
        decision_quote = _last_known(qs, qa, decision)
        entry_quote = _last_known(qs, qa, entry_time)
        if decision_quote is None or entry_quote is None:
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
            quote_count = len(visible_quotes)
            quote_ofi = _quote_ofi(visible_quotes)
            quote_mids = [quote.mid for quote in visible_quotes]
            quote_returns = [math.log(right / left) * 10_000.0
                             for left, right in zip(quote_mids, quote_mids[1:])]
            realized_volatility = (
                math.sqrt(sum(value * value for value in quote_returns))
                if quote_returns else None)
        else:
            quote_count = qhi - qlo
            quote_ofi = (quote_ofi_prefix[qhi] - quote_ofi_prefix[qlo + 1]
                         if quote_count >= 2 else None)
            variance = (quote_variance_prefix[qhi] -
                        quote_variance_prefix[qlo + 1]
                        if quote_count >= 2 else None)
            realized_volatility = (
                math.sqrt(max(0.0, variance)) if variance is not None else None)

        tx_lo = bisect_left(trade_clock_exceptions, tlo)
        tx_hi = bisect_left(trade_clock_exceptions, thi)
        hidden_trades = [index for index in trade_clock_exceptions[tx_lo:tx_hi]
                         if ts[index].event_time > decision]
        signed = trade_signed_prefix[thi] - trade_signed_prefix[tlo]
        volume = trade_volume_prefix[thi] - trade_volume_prefix[tlo]
        for index in hidden_trades:
            trade = ts[index]
            signed -= float(trade.side) * float(trade.quantity)
            if trade.side != 0:
                volume -= float(trade.quantity)
        trade_count = thi - tlo - len(hidden_trades)
        trade_flow = signed / volume if volume > 0 else None
        bid_l1 = float(decision_quote.bid_sizes[0])
        ask_l1 = float(decision_quote.ask_sizes[0])
        depth_l1 = bid_l1 + ask_l1
        depth_l10 = (sum(float(v) for v in decision_quote.bid_sizes[:10]) +
                     sum(float(v) for v in decision_quote.ask_sizes[:10]))
        normalized_ofi = (quote_ofi / depth_l1
                          if quote_ofi is not None and depth_l1 > 0 else None)
        feature_mid = decision_quote.mid
        entry_mid = entry_quote.mid
        spread_bps = ((decision_quote.best_ask - decision_quote.best_bid) /
                      feature_mid * 10_000.0)
        microprice_offset = (
            _microprice(decision_quote) / feature_mid - 1.0) * 10_000.0

        labels: list[HorizonLabel] = []
        for horizon in spec.horizons_seconds:
            exit_time = entry_time + timedelta(seconds=horizon)
            exit_quote = _last_known(qs, qa, exit_time)
            if exit_quote is None:
                continue
            exit_age = (exit_time - exit_quote.available_at).total_seconds()
            if exit_age < 0 or exit_age > spec.max_quote_age_seconds:
                continue
            future_mid = exit_quote.mid
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
            long_passive_net = (
                (exit_quote.best_bid / entry_quote.best_bid - 1.0) * 10_000.0 -
                passive_fees if long_fill is not None else None)
            short_passive_net = (
                (entry_quote.best_ask / exit_quote.best_ask - 1.0) * 10_000.0 -
                passive_fees if short_fill is not None else None)
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
            queue_imbalance_l1=_imbalance(decision_quote.bid_sizes,
                                          decision_quote.ask_sizes, 1),
            queue_imbalance_l10=_imbalance(decision_quote.bid_sizes,
                                           decision_quote.ask_sizes, 10),
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
 where instrument_id::text = any(%s)
   and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
   and bid_prices[1] > 0 and ask_prices[1] > 0
   and ask_prices[1] >= bid_prices[1]
 order by instrument_id::text, event_time, source_event_id
"""

_TRADE_BATCH_SQL = """
select event_time, received_at, observed_at, instrument_id::text,
       price, quantity, side, source_event_id
  from market.market_ticks
 where instrument_id::text = any(%s)
   and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
 order by instrument_id::text, event_time, source_event_id
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
 where instrument_id::text = any(%s)
   and event_time >= %s and event_time < %s
   and greatest(received_at, observed_at) <= %s
 group by instrument_id
 order by instrument_id::text
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
              bid1,':',ask1,':',bid_vol1,':',ask_vol1)
  from ext_src.quotes
 where symbol = %s
   and ts >= %s and ts < %s and ts <= %s
   and bid1 > 0 and ask1 > 0 and ask1 >= bid1
 order by ts, 9
"""

_EXTERNAL_TRADE_SQL = """
select ts, ts, ts, btrim(symbol), price, volume,
       case when ofi_contrib > 0 then 1
            when ofi_contrib < 0 then -1 else 0 end,
       concat('extt:',btrim(symbol),':',extract(epoch from ts)::bigint,':',
              price,':',volume,':',ofi_contrib)
  from ext_src.ticks
 where symbol = %s
   and ts >= %s and ts < %s and ts <= %s
   and price > 0 and volume > 0
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
        raise ValueError(f"unsupported intraday event source: {value!r}")
    return source


def _source_sql(source: str, local: str, external: str) -> str:
    return external if _event_source(source) == EXTERNAL_EVENT_SOURCE else local


def _quote_event(row) -> QuoteEvent:
    return QuoteEvent(
        event_time=row[0], received_at=row[1], observed_at=row[2],
        instrument_id=row[3],
        bid_prices=tuple(float(v) for v in row[4]),
        bid_sizes=tuple(float(v) for v in row[5]),
        ask_prices=tuple(float(v) for v in row[6]),
        ask_sizes=tuple(float(v) for v in row[7]),
        source_event_id=row[8],
    )


def _trade_event(row) -> TradeEvent:
    return TradeEvent(
        event_time=row[0], received_at=row[1], observed_at=row[2],
        instrument_id=row[3], price=float(row[4]), quantity=float(row[5]),
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


def load_instrument_events(conn, *, instrument_id: str, start: datetime,
                           end: datetime, as_known_at: datetime,
                           source: str = LOCAL_EVENT_SOURCE,
                           ) -> tuple[list[QuoteEvent], list[TradeEvent]]:
    """Read one bounded instrument slice from an explicit source contract."""
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    if cutoff < end:
        raise ValueError("as_known_at must cover the requested event-time interval")
    params = (instrument_id, start, end, cutoff)
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(source, _QUOTE_SQL, _EXTERNAL_QUOTE_SQL),
                       params)
        quotes = [_quote_event(row) for row in _fetch_chunks(cursor)]
        cursor.execute(_source_sql(source, _TRADE_SQL, _EXTERNAL_TRADE_SQL),
                       params)
        trades = [_trade_event(row) for row in _fetch_chunks(cursor)]
    return quotes, trades


def load_instrument_events_batch(conn, *, instrument_ids, start: datetime,
                                 end: datetime, as_known_at: datetime,
                                 source: str = LOCAL_EVENT_SOURCE,
                                 ) -> dict[str, tuple[list[QuoteEvent],
                                                       list[TradeEvent]]]:
    """Read a bounded shard in two SQL scans and group rows by instrument."""
    ids = tuple(dict.fromkeys(
        str(value) for value in instrument_ids
        if value is not None and str(value)))
    if not ids:
        return {}
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    if cutoff < end:
        raise ValueError("as_known_at must cover the requested event-time interval")
    grouped = {instrument_id: ([], []) for instrument_id in ids}
    params = (list(ids), start, end, cutoff)
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(
            source, _QUOTE_BATCH_SQL, _EXTERNAL_QUOTE_BATCH_SQL), params)
        for row in _fetch_chunks(cursor):
            grouped[row[3]][0].append(_quote_event(row))
        cursor.execute(_source_sql(
            source, _TRADE_BATCH_SQL, _EXTERNAL_TRADE_BATCH_SQL), params)
        for row in _fetch_chunks(cursor):
            grouped[row[3]][1].append(_trade_event(row))
    return grouped


def source_quality(conn, *, instrument_id: str, start: datetime, end: datetime,
                   as_known_at: datetime,
                   source: str = LOCAL_EVENT_SOURCE) -> dict:
    """Count source rows rejected before object construction."""
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(
                       source, _SOURCE_QUALITY_SQL,
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
    ids = tuple(dict.fromkeys(
        str(value) for value in instrument_ids
        if value is not None and str(value)))
    if not ids:
        return {}
    start = _aware(start, "start")
    end = _aware(end, "end")
    cutoff = _aware(as_known_at, "as_known_at")
    with conn.cursor() as cursor:
        cursor.execute(_source_sql(
                       source, _SOURCE_QUALITY_BATCH_SQL,
                       _EXTERNAL_SOURCE_QUALITY_BATCH_SQL),
                       (list(ids), start, end, cutoff))
        rows = cursor.fetchall()
    out = {instrument_id: _quality_record(0, 0, 0, 0, 0)
           for instrument_id in ids}
    for row in rows:
        out[row[0]] = _quality_record(*row[1:])
    return out


def manifest(spec: IntradayLaneSpec,
             *, source: str = LOCAL_EVENT_SOURCE) -> dict:
    """Serializable contract persisted with every intraday experiment."""
    event_source = _event_source(source)
    external = event_source == EXTERNAL_EVENT_SOURCE
    payload = asdict(spec)
    payload.update({
        "lane_version": LANE_VERSION,
        "event_source": event_source,
        "clock": ("EVENT_TIME_ONLY(ts); separate receipt clock unavailable"
                  if external else
                  "AVAILABLE_AT=max(received_at,observed_at)"),
        "arrival_clock_pit": not external,
        "historical_replay_only": external,
        "evidence_limit": (
            "external source cannot audit network arrival latency or "
            "out-of-order receipt; forward receipt-clock confirmation required"
            if external else None),
        "feature_cutoff": (
            "event_time<=decision_time (event-time historical replay)"
            if external else
            "event_time<=decision_time and available_at<=decision_time"),
        "entry_rule": "latest visible quote at decision_time+order_latency",
        "label_rule": "latest visible quote at entry_time+horizon",
        "execution_model": "TAKER_BOTH_SIDES",
        "passive_execution_model": "FIFO_NO_CANCELLATION_CREDIT_LOWER_BOUND",
        "passive_exact_queue_supported": False,
        "passive_exact_queue_blocker": "snapshot L10 has no order IDs/MBO queue",
        "purge_gap_seconds": spec.purge_gap.total_seconds(),
    })
    return payload
