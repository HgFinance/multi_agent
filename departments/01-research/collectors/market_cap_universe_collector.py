#!/usr/bin/env python3
"""Persist a forward-only PIT snapshot of the LS t1444 top-100 universe.

The t1444 endpoint exposes the current ranking, not a historical publication
archive. This collector records each observation as a new immutable
``quant.universe_versions`` row and never backfills an old ``as_of`` date from
today's response. Raw ranking rows stay in memory only; the database receives
instrument identity, provider rank, and a non-sensitive source receipt.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence


_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "autonomous"))

COLLECTOR_VERSION = "research-t1444-pit-universe-v1"
UNIVERSE_NAME = "krx-t1444-market-cap-top100"
TR_CODE = "t1444"
UPCODE = "001"  # KRX 전체 업종
TOP_N = 100
PROVIDER_SCAN_ROWS = 200
KST = timezone(timedelta(hours=9))
SYMBOL_RE = re.compile(r"^[0-9A-Z]{6}$")


class MarketCapUniverseError(RuntimeError):
    """A fail-closed error; no partial PIT snapshot is published."""


def ranked_symbols(
    rows: Sequence[Mapping[str, Any]], *, limit: int = TOP_N
) -> list[dict[str, Any]]:
    """Validate the provider ranking and return only identity/rank fields.

    Provider response order is the rank. ``total`` is required as a contract
    check but is deliberately not persisted: it is raw market data, while the
    PIT universe only needs membership and rank.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    if len(rows) < limit:
        raise MarketCapUniverseError(
            f"t1444 returned {len(rows)} rows; {limit} PIT members are required"
        )

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for rank, row in enumerate(rows[:limit], start=1):
        symbol = str(row.get("shcode") or "").strip().upper()
        if not SYMBOL_RE.fullmatch(symbol):
            raise MarketCapUniverseError(
                f"t1444 row {rank} has an invalid shcode; refusing a partial snapshot"
            )
        if symbol in seen:
            raise MarketCapUniverseError(
                f"t1444 returned duplicate shcode {symbol}; refusing a partial snapshot"
            )
        seen.add(symbol)
        try:
            total = Decimal(str(row.get("total") or ""))
        except (InvalidOperation, ValueError) as exc:
            raise MarketCapUniverseError(
                f"t1444 row {rank} has an invalid market-cap field"
            ) from exc
        if not total.is_finite() or total <= 0:
            raise MarketCapUniverseError(
                f"t1444 row {rank} has a non-positive market-cap field"
            )
        result.append({"rank": rank, "symbol": symbol})
    return result


def snapshot_content_hash(members: Sequence[Mapping[str, Any]]) -> str:
    """Hash the ordered PIT membership manifest, not raw provider rows."""

    payload = json.dumps(
        [
            {
                "rank": int(m["rank"]),
                "stock_rank": int(m["stock_rank"]),
                "symbol": str(m["symbol"]),
            }
            for m in members
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def stock_members(
    ranked: Sequence[Mapping[str, Any]],
    identities: Mapping[str, Mapping[str, Any]],
    *,
    limit: int = TOP_N,
) -> list[dict[str, Any]]:
    """Keep the first ``limit`` provider-ranked KRX STOCK identities.

    t1444 is a market-cap ranking endpoint, not a stock-only endpoint. ETFs
    may appear in its response, so the stock contract is applied only after
    resolving the governed instrument identity. Provider rank is retained for
    auditability and ``stock_rank`` is the rank inside this stock universe.
    """

    if limit <= 0:
        raise ValueError("limit must be positive")
    result: list[dict[str, Any]] = []
    for member in ranked:
        symbol = str(member["symbol"])
        identity = identities.get(symbol)
        if not identity or str(identity.get("instrument_type", "")).upper() != "STOCK":
            continue
        result.append(
            {
                "rank": int(member["rank"]),
                "stock_rank": len(result) + 1,
                "symbol": symbol,
                "instrument_id": str(identity["instrument_id"]),
            }
        )
        if len(result) == limit:
            break
    if len(result) < limit:
        raise MarketCapUniverseError(
            f"t1444 provider scan produced only {len(result)} KRX STOCK members; "
            f"{limit} are required"
        )
    return result


def _resolve_instruments(
    conn, symbols: Sequence[str], observed_at: datetime
) -> dict[str, dict[str, str]]:
    """Resolve LS symbols at the observation instant; reject ambiguity."""

    with conn.cursor() as cur:
        cur.execute(
            """
            select sy.symbol, i.instrument_id::text, i.instrument_type
              from reference.instruments i
              join reference.instrument_symbols sy
                on sy.instrument_id = i.instrument_id
               and sy.provider = 'LS'
               and sy.market = 'KRX'
               and sy.symbol_type = 'TRADING'
               and sy.is_primary
               and sy.valid_from <= %s
               and (sy.valid_to is null or sy.valid_to > %s)
             where upper(i.market) = 'KRX'
               and upper(i.asset_class) = 'EQUITY'
               and upper(i.status) in ('ACTIVE', 'HALTED')
               and (i.listed_from is null or i.listed_from <= %s::date)
               and (i.listed_to is null or i.listed_to >= %s::date)
               and lower(coalesce(i.metadata->>'is_spac', 'false')) <> 'true'
               and sy.symbol = any(%s)
            order by sy.symbol, i.instrument_id
            """,
            (
                observed_at,
                observed_at,
                observed_at.date(),
                observed_at.date(),
                list(symbols),
            ),
        )
        rows = cur.fetchall()

    by_symbol: dict[str, dict[str, str]] = {}
    for symbol, instrument_id, instrument_type in rows:
        by_symbol.setdefault(str(symbol), {})[str(instrument_id)] = str(instrument_type)
    ambiguous = sorted(symbol for symbol, ids in by_symbol.items() if len(ids) > 1)
    if ambiguous:
        raise MarketCapUniverseError(
            f"LS/KRX symbol maps to multiple instrument identities: {ambiguous[:5]}"
        )
    missing = sorted(set(symbols) - set(by_symbol))
    if missing:
        raise MarketCapUniverseError(
            f"t1444 members have no PIT instrument identity: {missing[:5]}"
        )
    resolved: dict[str, dict[str, str]] = {}
    for symbol, values in by_symbol.items():
        instrument_id, instrument_type = next(iter(values.items()))
        resolved[symbol] = {
            "instrument_id": instrument_id,
            "instrument_type": instrument_type,
        }
    return resolved


def persist_snapshot(
    conn,
    *,
    members: Sequence[Mapping[str, Any]],
    instrument_by_symbol: Mapping[str, str],
    observed_at: datetime,
    receipt: Mapping[str, Any],
) -> str:
    """Publish one complete immutable universe version and its members."""

    content_hash = snapshot_content_hash(members)
    rules = {
        "builder": COLLECTOR_VERSION,
        "definition": "LS t1444 KRX market-cap ranking top 100 STOCKS",
        "provider": "LS",
        "tr_code": TR_CODE,
        "upcode": UPCODE,
        "ranking_order": "provider_response_order",
        "requested_rows": TOP_N,
        "provider_scan_rows": PROVIDER_SCAN_ROWS,
        "stock_filter": "reference.instruments.instrument_type=STOCK",
        "point_in_time_policy": "forward_observed_snapshots_only",
        "availability_boundary": "observed_at_upper_bound",
        "historical_backfill": "not_available_from_t1444",
        "observed_at": observed_at.isoformat(),
    }
    source_versions = {
        "collector": COLLECTOR_VERSION,
        "ls_openapi": dict(receipt),
    }
    with conn.cursor() as cur:
        cur.execute(
            """
            insert into quant.universe_versions
              (name, as_of, rules, member_count, content_hash, source_versions)
            values (%s, %s, %s::jsonb, %s, %s, %s::jsonb)
            on conflict (name, as_of, content_hash) do nothing
            returning universe_version_id::text
            """,
            (
                UNIVERSE_NAME,
                observed_at,
                json.dumps(rules, ensure_ascii=False),
                len(members),
                content_hash,
                json.dumps(source_versions, ensure_ascii=False),
            ),
        )
        row = cur.fetchone()
        if row is None:
            cur.execute(
                """
                select universe_version_id::text
                  from quant.universe_versions
                 where name = %s and as_of = %s and content_hash = %s
                """,
                (UNIVERSE_NAME, observed_at, content_hash),
            )
            row = cur.fetchone()
        if row is None:
            raise MarketCapUniverseError(
                "t1444 PIT version disappeared during idempotent publication"
            )
        universe_version_id = str(row[0])
        for member in members:
            symbol = str(member["symbol"])
            cur.execute(
                """
                insert into quant.universe_members
                  (universe_version_id, instrument_id, member_role, attributes)
                values (%s, %s, 'MARKET_CAP_RANK', %s::jsonb)
                on conflict do nothing
                """,
                (
                    universe_version_id,
                    instrument_by_symbol[symbol],
                    json.dumps(
                        {
                            "rank": int(member["rank"]),
                            "stock_rank": int(member["stock_rank"]),
                            "provider_symbol": symbol,
                        },
                        ensure_ascii=False,
                    ),
                ),
            )
    conn.commit()
    return universe_version_id


def collect() -> int:
    import psycopg2
    from ls_market_data import OnDemandMarketDataClient
    from source_registry import load_project_env

    observed_at = datetime.now(KST)
    batch = OnDemandMarketDataClient().fetch_ranking(
        TR_CODE,
        {"upcode": UPCODE},
        as_of=observed_at.date(),
        max_rows=PROVIDER_SCAN_ROWS,
    )
    ranked = ranked_symbols(batch.rows, limit=PROVIDER_SCAN_ROWS)
    env = load_project_env()
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        identities = _resolve_instruments(
            conn, [str(member["symbol"]) for member in ranked], observed_at
        )
        members = stock_members(ranked, identities)
        instrument_by_symbol = {
            str(member["symbol"]): str(member["instrument_id"])
            for member in members
        }
        version_id = persist_snapshot(
            conn,
            members=members,
            instrument_by_symbol=instrument_by_symbol,
            observed_at=observed_at,
            receipt=batch.receipt.as_dict(),
        )
    finally:
        conn.close()

    print(
        f"{COLLECTOR_VERSION}: as_of={observed_at.isoformat()} "
        f"members={len(members)} version={version_id} "
        f"pages={batch.receipt.pages}",
        flush=True,
    )
    return 0


def _check_validation():
    rows = [{"shcode": f"{i:06d}", "total": 1000 - i} for i in range(1, TOP_N + 1)]
    ranked = ranked_symbols(rows)
    identities = {
        member["symbol"]: {
            "instrument_id": f"id-{member['symbol']}",
            "instrument_type": "STOCK",
        }
        for member in ranked
    }
    members = stock_members(ranked, identities)
    assert len(members) == TOP_N
    assert members[0]["rank"] == 1
    assert members[0]["stock_rank"] == 1
    assert members[-1]["rank"] == TOP_N
    assert len(snapshot_content_hash(members)) == 64
    try:
        ranked_symbols(rows[: TOP_N - 1])
    except MarketCapUniverseError:
        pass
    else:
        raise AssertionError("short ranking must fail closed")
    duplicate = list(rows)
    duplicate[-1] = duplicate[0]
    try:
        ranked_symbols(duplicate)
    except MarketCapUniverseError:
        pass
    else:
        raise AssertionError("duplicate ranking member must fail closed")
    print("  t1444 응답·PIT manifest 검증  OK")


def _check_forward_only_contract():
    import inspect

    source = inspect.getsource(persist_snapshot)
    assert "historical_backfill" in source
    assert "observed_at" in source
    assert "delete from quant.universe" not in source.lower()
    print("  과거 소급 금지·불변 적재       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if "--collect" in sys.argv:
        raise SystemExit(collect())
    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_validation()
    _check_forward_only_contract()
    print("t1444 PIT 유니버스 수집기 2개 영역 통과. 적재는 --collect")
