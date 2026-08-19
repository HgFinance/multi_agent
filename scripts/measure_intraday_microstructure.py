#!/usr/bin/env python3
"""Measure causal intraday microstructure baselines on one bounded KRX slice.

This is a diagnostic, not a strategy release tool.  It deliberately reports
both mid-price markout and conservative taker round-trip P&L so a statistically
predictive feature cannot be mistaken for executable alpha.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
sys.path.insert(0, str(PIPELINE))

from intraday_microstructure import (  # noqa: E402
    IntradayLaneSpec,
    audit_causality,
    build_samples,
    load_instrument_events,
    manifest,
    score_signal,
    source_quality,
    walk_forward_linear_score,
)
from stock_universe import assert_stock_instrument_ids  # noqa: E402


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("timestamp needs an explicit timezone")
    return parsed.astimezone(timezone.utc)


def _connection():
    import psycopg2

    dsn = os.getenv("TIMESCALE_DATABASE_URL")
    if not dsn:
        try:
            collectors = ROOT / "departments" / "01-research" / "collectors"
            sys.path.insert(0, str(collectors))
            from source_registry import load_project_env
            dsn = load_project_env(ROOT).get("TIMESCALE_DATABASE_URL")
        except (FileNotFoundError, KeyError, ImportError):
            dsn = None
    if not dsn:
        raise RuntimeError("TIMESCALE_DATABASE_URL is not configured")
    return psycopg2.connect(dsn, connect_timeout=20)


def _reference_connection():
    """Open the reference plane used to prove product identity."""
    import psycopg2

    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        try:
            collectors = ROOT / "departments" / "01-research" / "collectors"
            sys.path.insert(0, str(collectors))
            from source_registry import load_project_env
            dsn = load_project_env(ROOT).get("DATABASE_URL")
        except (FileNotFoundError, KeyError, ImportError):
            dsn = None
    if not dsn:
        raise RuntimeError(
            "DATABASE_URL is required for reference-plane stock validation")
    return psycopg2.connect(dsn, connect_timeout=20)


def _begin_read_only_transaction(connection) -> None:
    """Scope the safety guard to this transaction, never the pooled session."""
    with connection.cursor() as cursor:
        # DATABASE_URL can be Supavisor transaction mode (6543).  A session-
        # level read-only default survives on the server backend and can poison
        # an unrelated writer after the backend returns to the pool.  SET
        # TRANSACTION disappears at rollback.
        cursor.execute("SET TRANSACTION READ ONLY")


def _rollback_and_close(connection) -> None:
    try:
        connection.rollback()
    finally:
        connection.close()


def _instrument_id(conn, symbol_or_id: str) -> str:
    if len(symbol_or_id) == 36 and symbol_or_id.count("-") == 4:
        return symbol_or_id
    with conn.cursor() as cursor:
        cursor.execute(
            "select instrument_id::text from market.symbol_map where symbol=%s",
            (symbol_or_id,),
        )
        row = cursor.fetchone()
    if row is None:
        raise ValueError(f"unknown symbol: {symbol_or_id}")
    return row[0]


def run(args: argparse.Namespace) -> dict:
    spec = IntradayLaneSpec(
        sample_interval_seconds=args.sample_seconds,
        feature_lookback_seconds=args.lookback_seconds,
        horizons_seconds=tuple(args.horizons),
        order_latency_ms=args.latency_ms,
        max_quote_age_seconds=args.max_quote_age_seconds,
        fee_bps_per_side=args.fee_bps,
        maker_fee_bps_per_side=args.maker_fee_bps,
    )
    start = args.start
    end = start + timedelta(minutes=args.minutes)
    kst = ZoneInfo("Asia/Seoul")
    local_start, local_end = start.astimezone(kst), end.astimezone(kst)
    continuous_start = local_start.replace(hour=9, minute=0, second=0,
                                           microsecond=0)
    continuous_end = local_start.replace(hour=15, minute=20, second=0,
                                         microsecond=0)
    if (not args.allow_auction and
            (local_start.date() != local_end.date() or
             local_start < continuous_start or local_end > continuous_end)):
        raise ValueError(
            "slice leaves KRX continuous trading (09:00-15:20 Asia/Seoul); "
            "use --allow-auction only for a separately preregistered auction study")
    read_start = start - timedelta(seconds=spec.feature_lookback_seconds + 5)
    read_end = end + timedelta(seconds=max(spec.horizons_seconds) + 5)
    cutoff = args.as_known_at or (read_end + timedelta(days=1))

    conn = _connection()
    meta_conn = None
    try:
        _begin_read_only_transaction(conn)
        instrument_id = _instrument_id(conn, args.symbol)
        # A UUID or market.symbol_map row is only a transport identity.  Prove
        # the product is an ACTIVE KRX EQUITY/STOCK over the requested session
        # before reading a single quote or computing a diagnostic score.
        meta_conn = _reference_connection()
        _begin_read_only_transaction(meta_conn)
        stock_scope = assert_stock_instrument_ids(
            meta_conn, [instrument_id],
            first_session=local_start.date(), last_session=local_end.date())
        quality = source_quality(
            conn,
            instrument_id=instrument_id,
            start=read_start,
            end=read_end,
            as_known_at=cutoff,
        )
        quotes, trades = load_instrument_events(
            conn,
            instrument_id=instrument_id,
            start=read_start,
            end=read_end,
            as_known_at=cutoff,
        )
    finally:
        try:
            if meta_conn is not None:
                _rollback_and_close(meta_conn)
        finally:
            _rollback_and_close(conn)

    samples = build_samples(quotes, trades, spec, start=start, end=end)
    signals = {
        "queue_imbalance_l1": lambda row: row.queue_imbalance_l1,
        "queue_imbalance_l10": lambda row: row.queue_imbalance_l10,
        "microprice_offset_bps": lambda row: row.microprice_offset_bps,
        "trade_flow_imbalance": lambda row: row.trade_flow_imbalance,
        "quote_event_ofi": lambda row: row.quote_event_ofi,
    }
    scores = {
        name: [score_signal(samples, fn, horizon_seconds=horizon,
                            threshold=args.threshold)
               for horizon in spec.horizons_seconds]
        for name, fn in signals.items()
    }
    passive_scores = {
        name: [score_signal(samples, fn, horizon_seconds=horizon,
                            threshold=args.threshold,
                            execution="PASSIVE_FIFO_LOWER_BOUND")
               for horizon in spec.horizons_seconds]
        for name, fn in signals.items()
    }
    joint_features = (
        "queue_imbalance_l1", "queue_imbalance_l10",
        "microprice_offset_bps", "trade_flow_imbalance", "quote_event_ofi",
        "spread_bps", "trade_count", "quote_count",
    )
    walk_forward = [walk_forward_linear_score(
        samples,
        feature_names=joint_features,
        horizon_seconds=horizon,
        spec=spec,
        n_splits=args.walk_forward_splits,
        minimum_predicted_edge_bps=args.minimum_edge_bps,
    ) for horizon in spec.horizons_seconds]
    return {
        "symbol": args.symbol,
        "instrument_id": instrument_id,
        "stock_scope": stock_scope,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "raw_quotes": len(quotes),
        "raw_trades": len(trades),
        "source_quality": quality,
        "manifest": manifest(spec),
        "causality": audit_causality(samples, spec),
        "scores": scores,
        "passive_fifo_lower_bound_scores": passive_scores,
        "walk_forward_joint_model": walk_forward,
    }


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser()
    result.add_argument("--symbol", default="005930")
    result.add_argument("--start", type=_timestamp, required=True,
                        help="ISO-8601 start with timezone")
    result.add_argument("--minutes", type=int, default=60)
    result.add_argument("--sample-seconds", type=int, default=5)
    result.add_argument("--lookback-seconds", type=int, default=30)
    result.add_argument("--horizons", type=int, nargs="+", default=[5, 30, 300])
    result.add_argument("--latency-ms", type=int, default=250)
    result.add_argument("--max-quote-age-seconds", type=float, default=5.0)
    result.add_argument("--fee-bps", type=float, default=0.0)
    result.add_argument("--maker-fee-bps", type=float, default=0.0)
    result.add_argument("--threshold", type=float, default=0.0)
    result.add_argument("--walk-forward-splits", type=int, default=3)
    result.add_argument("--minimum-edge-bps", type=float, default=0.0)
    result.add_argument("--as-known-at", type=_timestamp)
    result.add_argument("--allow-auction", action="store_true")
    return result


if __name__ == "__main__":
    print(json.dumps(run(parser().parse_args()), ensure_ascii=False,
                     sort_keys=True, indent=2))
