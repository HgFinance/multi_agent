#!/usr/bin/env python3
"""market-api - 시세 읽기 전용 조회면 (FastAPI).

담당: 재일 (리서치/퀀트)
근거: TEAM_JAEIL 가이드 F03 완료 기준 "트레이딩·리스크는 DB 없이 Snapshot API 를
      조회한다"(미착수였음) + DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN 6.2
      (market-api: Snapshot, Bar, Breadth, DQ Read API / TimescaleDB Read-only).

경계 (research-api 와 같은 원칙):
1. **읽기 전용** - 쓰기 Endpoint 없음, TimescaleDB 세션 read-only 강제,
   자체 점검이 GET 외 메서드를 거부한다.
2. **다른 본부는 이 API 만 본다** - TimescaleDB Credential 은 리서치·퀀트만
   갖는다는 경계를 이 조회면이 대신 지킨다.
3. 종목 식별은 심볼로 받는다 - instrument_id(uuid) 매핑은 기동 시 reference 에서
   한 번 읽어 캐시한다(상장 변경은 재시작 주기로 충분).

실행: compose market-api (uvicorn market_api:app --port 8036)
점검: python api/market_api.py          # DB 없이
      python api/market_api.py --probe  # 실 DB 관통
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))

from fastapi import FastAPI, HTTPException, Query  # noqa: E402

from source_registry import load_project_env  # noqa: E402

API_VERSION = "research-market-api-v1"
KST = timezone(timedelta(hours=9))

app = FastAPI(title="Market Read API", version="0.1.0")

_ts = None          # TimescaleDB 연결 (read-only)
_sym2iid: dict = {}  # symbol -> instrument_id (기동 시 1회)
_iid2sym: dict = {}


def get_ts():
    global _ts
    import psycopg2

    if _ts is not None and not _ts.closed:
        return _ts
    _ts = psycopg2.connect(load_project_env()["TIMESCALE_DATABASE_URL"], connect_timeout=10)
    with _ts.cursor() as cur:
        cur.execute("set default_transaction_read_only = on")
    _ts.commit()
    return _ts


def symbol_map() -> dict:
    global _sym2iid, _iid2sym
    if _sym2iid:
        return _sym2iid
    import psycopg2

    s = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=15)
    try:
        with s.cursor() as cur:
            cur.execute("""
                select sy.symbol, i.instrument_id::text
                from reference.instruments i
                join reference.instrument_symbols sy using (instrument_id)
                where i.market = 'KRX' and sy.is_primary
            """)
            _sym2iid = {sym: iid for sym, iid in cur.fetchall()}
            _iid2sym = {v: k for k, v in _sym2iid.items()}
    finally:
        s.close()
    return _sym2iid


def _iid_or_404(symbol: str) -> str:
    iid = symbol_map().get(symbol)
    if iid is None:
        raise HTTPException(404, f"모르는 심볼이다: {symbol}")
    return iid


def _query(sql: str, params: tuple):
    conn = get_ts()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
        conn.rollback()
        return [dict(zip(cols, r)) for r in rows]
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise


@app.get("/health")
def health() -> dict:
    rows = _query("""
        select 'ticks' as domain, count(*) as rows,
               max(event_time) as last_event
        from market.market_ticks where event_time > now() - interval '2 days'
        union all
        select 'quotes', count(*), max(event_time)
        from market.market_quotes where event_time > now() - interval '2 days'
        union all
        select 'bars_1d', count(*), max(bucket_time)
        from market.market_bars where interval_code = '1D'
        union all
        select 'bars_1m', count(*), max(bucket_time)
        from market.market_bars where interval_code = '1M'
        union all
        select 'breadth', count(*), max(event_time) from market.market_breadth
    """, ())
    return {"version": API_VERSION, "read_only": True, "domains": rows}


@app.get("/snapshot/{symbol}")
def snapshot(symbol: str) -> dict:
    """마지막 체결 + 마지막 호가. 세션 밖에서는 마감 스냅샷이 나온다."""
    iid = _iid_or_404(symbol)
    tick = _query("""
        select event_time, price, quantity, cumulative_volume
        from market.market_ticks where instrument_id = %s
        order by event_time desc limit 1
    """, (iid,))
    quote = _query("""
        select event_time, best_bid, best_ask, mid_price,
               total_bid_size, total_ask_size
        from market.market_quotes where instrument_id = %s
        order by event_time desc limit 1
    """, (iid,))
    if not tick and not quote:
        raise HTTPException(404, f"{symbol} 의 시세가 아직 없다")
    return {"symbol": symbol,
            "last_trade": tick[0] if tick else None,
            "last_quote": quote[0] if quote else None}


@app.get("/bars/{symbol}")
def bars(
    symbol: str,
    interval: str = Query("1D", pattern="^(1D|1M|5M|15M|1H)$"),
    limit: int = Query(120, gt=0, le=2000),
    source: Optional[str] = Query(None, description="ls_chart | derived 등. 없으면 전체"),
    to: Optional[datetime] = None,
):
    """봉 조회 - 백필(ls_chart)과 자체 파생이 한 테이블에서 나온다(source 로 구분)."""
    iid = _iid_or_404(symbol)
    cond, params = "", [iid, interval]
    if source:
        cond += " and source = %s"
        params.append(source)
    if to is not None:
        if to.tzinfo is None:
            raise HTTPException(422, "to 는 timezone 이 있어야 한다")
        cond += " and bucket_time <= %s"
        params.append(to)
    params.append(limit)
    return _query(f"""
        select bucket_time, open, high, low, close, volume, source, is_final
        from market.market_bars
        where instrument_id = %s and interval_code = %s{cond}
        order by bucket_time desc limit %s
    """, tuple(params))


@app.get("/breadth")
def breadth(market: str = Query("KOSPI"), limit: int = Query(20, gt=0, le=500)):
    return _query("""
        select event_time, market, advancers, decliners, unchanged,
               up_volume, down_volume, total_value
        from market.market_breadth where market = %s
        order by event_time desc limit %s
    """, (market, limit))


@app.get("/dq/summary")
def dq_summary() -> dict:
    """수집 건강 요약 - 오늘 심볼 커버리지와 최근 유입."""
    rows = _query("""
        select count(distinct instrument_id) as symbols_today,
               count(*) filter (where event_time > now() - interval '10 minutes') as ticks_10m,
               max(event_time) as last_tick
        from market.market_ticks
        where event_time::date = (now() at time zone 'Asia/Seoul')::date
    """, ())
    return {"today": rows[0]}


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없이
# ---------------------------------------------------------------------------

def _check_readonly_surface():
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        assert not (methods - {"GET", "HEAD", "OPTIONS"}), \
            f"읽기 전용 API 에 쓰기 메서드: {route.path}"
    print("  읽기 전용 표면           OK")


def _check_bar_params():
    from fastapi.exceptions import HTTPException as HE

    try:
        bars.__wrapped__ if hasattr(bars, "__wrapped__") else None
    except Exception:
        pass
    # naive to 거부 (PIT 9시간 오차 방지 - research-api 와 같은 규칙)
    global _sym2iid
    _sym2iid = {"005930": "00000000-0000-0000-0000-000000000000"}
    try:
        bars("005930", interval="1D", limit=10, source=None,
             to=datetime(2026, 7, 31, 9, 0))
        raise AssertionError("naive to 가 통과했다")
    except HE:
        pass
    finally:
        _sym2iid = {}
    print("  파라미터 가드            OK")


def _probe():
    h = health()
    print("  /health:")
    for d in h["domains"]:
        print(f"    {d['domain']:8} {d['rows']:>10,}  last={d['last_event']}")
    s = snapshot("005930")
    lt = s["last_trade"]
    print(f"  /snapshot/005930: {lt['price']}원 @ {lt['event_time']}")
    b = bars("005930", interval="1D", limit=5, source="ls_chart", to=None)
    print(f"  /bars/005930 1D(ls_chart) 최근 5: {[str(x['bucket_time'])[:10] for x in b]}")
    bm = bars("005930", interval="1M", limit=3, source=None, to=None)
    print(f"  /bars 1M 최근 3: {[(str(x['bucket_time'])[11:16], x['source']) for x in bm]}")
    d = dq_summary()
    print(f"  /dq/summary: {d['today']}")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--probe" in sys.argv:
        print(f"{API_VERSION} 실 DB 관통")
        raise SystemExit(_probe())

    print(f"{API_VERSION} 자체 점검 (DB 없이)")
    _check_readonly_surface()
    _check_bar_params()
    print("market-api 2개 영역 통과. 관통은 --probe")
