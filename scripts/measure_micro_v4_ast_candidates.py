#!/usr/bin/env python3
"""Measure a small preregistered set of executable v4 AST combinations.

This is a diagnostic, not a strategy promotion path.  It uses non-overlapping
forward-return windows and prints every attempted expression so failed ideas stay in
memory.  Transaction costs and walk-forward gates still belong to the experiment
worker.
"""

from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments/04-quant-backtest/pipeline"
COLLECTORS = ROOT / "departments/01-research/collectors"
sys.path[:0] = [str(PIPELINE), str(COLLECTORS)]

from feature_catalog import _dates, _forward_returns, _pit_of, rank_ic, summarize
from stock_universe import assert_stock_instrument_ids


FIELDS = (
    "order_flow_imbalance", "size_weighted_ofi", "depth_imbalance_l1",
    "depth_imbalance_l10", "spread_bps",
)
FSV = "ms-daily-v4"


def _begin_read_only_transaction(connection) -> None:
    """Make one diagnostic transaction read-only without polluting its pool."""
    with connection.cursor() as cursor:
        # Supavisor transaction pooling reuses server sessions.  Setting the
        # session default to read-only can therefore break a later writer that
        # receives the same backend.  This guard vanishes with the rollback.
        cursor.execute("SET TRANSACTION READ ONLY")


def _rollback_and_close(connection) -> None:
    try:
        connection.rollback()
    finally:
        connection.close()


def source(field: str) -> dict:
    return {"op": "ts_last", "field": field, "n": 1}


def ranked(field: str) -> dict:
    return {"op": "rank", "arg": source(field)}


def binary(op: str, a: dict, b: dict) -> dict:
    return {"op": op, "args": [a, b]}


CANDIDATES = {
    "large_flow_confirmation": binary(
        "add", ranked("size_weighted_ofi"), ranked("order_flow_imbalance")),
    "large_vs_all_divergence": {
        "op": "abs", "arg": binary(
            "sub", ranked("size_weighted_ofi"), ranked("order_flow_imbalance"))},
    "deep_book_confirmation": binary(
        "add", ranked("size_weighted_ofi"), ranked("depth_imbalance_l10")),
    "surface_vs_deep_divergence": {
        "op": "abs", "arg": binary(
            "sub", ranked("depth_imbalance_l1"), ranked("depth_imbalance_l10"))},
    "liquid_large_pressure": binary(
        "sub", ranked("size_weighted_ofi"), ranked("spread_bps")),
}


def percentile_ranks(values: dict[str, float]) -> dict[str, float]:
    """Deterministic [0,1] cross-sectional ranks with identical values tied."""
    unique = sorted(set(values.values()))
    if len(unique) < 2:
        return {key: 0.5 for key in values}
    rank = {value: i / (len(unique) - 1) for i, value in enumerate(unique)}
    return {key: rank[value] for key, value in values.items()}


def evaluate(expr: dict, ranked_fields: dict[str, dict[str, float]]) -> dict[str, float]:
    op = expr.get("op")
    if op == "rank":
        return ranked_fields[expr["arg"]["field"]]
    if op == "abs":
        return {key: abs(value) for key, value in evaluate(expr["arg"], ranked_fields).items()}
    left, right = (evaluate(arg, ranked_fields) for arg in expr["args"])
    common = left.keys() & right.keys()
    if op == "add":
        return {key: left[key] + right[key] for key in common}
    if op == "sub":
        return {key: left[key] - right[key] for key in common}
    raise ValueError(f"unsupported diagnostic op {op!r}")


def measure(conn, *, meta_conn=None, stock_instrument_ids=None,
            horizon: int = 2, start: date | None = None) -> list[dict]:
    if meta_conn is None:
        raise RuntimeError(
            "micro-v4 diagnostic requires reference-plane stock validation")
    stock_ids = sorted({str(value) for value in
                        (stock_instrument_ids or ()) if value is not None})
    if not stock_ids:
        raise RuntimeError("micro-v4 diagnostic requires a stock UUID allowlist")
    stock_scope = assert_stock_instrument_ids(meta_conn, stock_ids)
    cur = conn.cursor()
    days = _dates(cur, horizon, FSV, stock_ids)
    if start is not None:
        days = [day for day in days if day >= start]
    if not days:
        return []
    label_read_through = max(days) + timedelta(days=horizon * 3)
    stock_scope = assert_stock_instrument_ids(
        meta_conn, stock_ids, first_session=min(days),
        last_session=label_read_through)
    future = _forward_returns(cur, days, horizon, stock_ids)
    pit = _pit_of(conn, cur, days)
    cur.execute(
        "select event_time::date,instrument_id," + ",".join(FIELDS) +
        " from market.microstructure_features where feature_set_version=%s "
        "and event_time::date=any(%s) "
        "and instrument_id=any(%s::uuid[])", (FSV, days, stock_ids))
    raw: dict = {}
    for day, instrument, *values in cur.fetchall():
        raw.setdefault(day, {})[str(instrument)] = dict(zip(FIELDS, values))

    ranked_by_day: dict = {}
    for day, instruments in raw.items():
        ranked_by_day[day] = {
            field: percentile_ranks({iid: float(row[field]) for iid, row in instruments.items()
                                     if row.get(field) is not None})
            for field in FIELDS
        }

    results = []
    for name, expr in CANDIDATES.items():
        scores = {day: evaluate(expr, ranked) for day, ranked in ranked_by_day.items()}
        series, names = rank_ic(scores, future)
        ic, t_stat, periods, avg_names = summarize(series, names)
        results.append({
            "name": name, "signal_expr": expr, "horizon": horizon,
            "ic": ic, "t_stat": t_stat, "periods": periods,
            "avg_names": avg_names, "pit": pit,
            "asset_scope": stock_scope["asset_scope"],
            "stock_universe_version": stock_scope["version"],
            "instrument_count": stock_scope["instrument_count"],
            "label_read_through": label_read_through.isoformat(),
            # AST 실행면은 큰 값을 산다. 역방향은 자동 합격이 아니라 별도 neg
            # 가설이어야 하므로 |t|가 아닌 사전 방향 t>=3만 화면 통과다.
            "screen_pass": bool(t_stat is not None and t_stat >= 3.0),
        })
    return results


def main(argv: list[str]) -> int:
    if "--self-check" in argv:
        ranks = percentile_ranks({"a": 1.0, "b": 2.0, "c": 2.0})
        assert ranks == {"a": 0.0, "b": 1.0, "c": 1.0}
        assert set(evaluate(CANDIDATES["liquid_large_pressure"], {
            "size_weighted_ofi": {"a": 0.8}, "spread_bps": {"a": 0.3},
        })) == {"a"}
        print("micro-v4 AST candidate diagnostic self-check OK")
        return 0

    import psycopg2
    from source_registry import load_project_env

    horizon = int(argv[argv.index("--horizon") + 1]) if "--horizon" in argv else 2
    start = (date.fromisoformat(argv[argv.index("--from") + 1])
             if "--from" in argv else None)
    env = load_project_env()
    conn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=30)
    meta_conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=30)
    try:
        _begin_read_only_transaction(conn)
        _begin_read_only_transaction(meta_conn)
        with meta_conn.cursor() as cursor:
            cursor.execute("""
                select instrument_id::text
                  from reference.instruments
                 where upper(instrument_type) = 'STOCK'
                   and upper(asset_class) = 'EQUITY'
                   and upper(market) = 'KRX'
                   and upper(status) = 'ACTIVE'
                 order by instrument_id
            """)
            stock_ids = [str(row[0]) for row in cursor.fetchall()]
        print(json.dumps(measure(
            conn, meta_conn=meta_conn, stock_instrument_ids=stock_ids,
            horizon=horizon, start=start), ensure_ascii=False,
            sort_keys=True, indent=2))
    finally:
        try:
            _rollback_and_close(meta_conn)
        finally:
            _rollback_and_close(conn)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
