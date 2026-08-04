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

from typing import Optional

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))

from fastapi import FastAPI, HTTPException, Query
from source_registry import load_project_env

API_VERSION = "research-market-api-v1"
KST = timezone(timedelta(hours=9))

app = FastAPI(title="Market Read API", version="0.1.0")

# ▶ Tool Gateway (2026-08-02 추가)
#   config.yaml 은 market.snapshot.read / market.bars.read / market.breadth.read /
#   market.dq.read / market.regime.read / market.microstructure.read 를 페르소나별로
#   선언해 놓았는데, **market-api 에는 게이트웨이가 아예 없어 어디서도 강제되지
#   않았다.** 익명 호출이 200 으로 나갔다(실측). 선언만 있고 강제가 없으면
#   "Hermes 에 적어놨으니 막힌다"는 오해가 그대로 남는다.
#
#   다만 research-api 와 달리 **기본은 관찰 모드**다. 이 API 는 퀀트본부와
#   팀원이 8036 으로 직접 조회하는 면이라, 강제부터 켜면 남의 작업이 멈춘다.
#   위반은 로그와 X-Tool-Gateway-Violation 헤더로 드러나므로, 그 기록을 보고
#   호출자를 다 배선한 뒤 TOOL_GATEWAY_ENFORCE_MARKET=true 로 올린다.
GATEWAY_STATUS = "NOT_INSTALLED"
try:
    from tool_gateway import ENFORCE_ENV_MARKET as _MARKET_ENV
    from tool_gateway import enforcing as _gateway_enforcing
    from tool_gateway import install as _install_gateway

    _install_gateway(app, env_var=_MARKET_ENV)
    GATEWAY_STATUS = "enforce" if _gateway_enforcing(_MARKET_ENV) else "observe"
except Exception as _e:
    _msg = f"Tool Gateway 미설치: {type(_e).__name__}: {_e}"
    print(f"⚠ {_msg}", file=sys.stderr)
    if os.environ.get("TOOL_GATEWAY_ENFORCE_MARKET", "").lower() in ("1", "true", "yes"):
        raise RuntimeError(
            f"{_msg} - 강제 모드인데 게이트웨이가 없으면 전 경로가 무인증으로 "
            f"열린다. 기동을 막는다(fail-closed).") from _e

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
        except Exception:  # noqa: BLE001, S110 - intentional fallback boundary
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
def snapshot(symbol: str,
             as_of: Optional[str] = Query(
                 None, description="이 시각까지의 마지막 체결·호가(PIT, "
                                   "tz 포함 ISO8601). 없으면 최신")) -> dict:
    """마지막 체결 + 마지막 호가. 세션 밖에서는 마감 스냅샷이 나온다.

    ▶ **as_of 를 받는다.** market_ticks·market_quotes 는 event_time 으로 이력이
      원래 다 있었고 컷오프만 없어서 Replay 금지였다. 금지는 도구를 없애지만
      파라미터는 도구를 살린다 - 에이전트가 값을 하는지 증명하려면 과거로
      돌려볼 수 있어야 한다.
    """
    iid = _iid_or_404(symbol)
    tick = _query("""
        select event_time, price, quantity, cumulative_volume
        from market.market_ticks
        where instrument_id = %s
          and (%s::timestamptz is null or event_time <= %s::timestamptz)
        order by event_time desc limit 1
    """, (iid, as_of, as_of))
    quote = _query("""
        select event_time, best_bid, best_ask, mid_price,
               total_bid_size, total_ask_size
        from market.market_quotes
        where instrument_id = %s
          and (%s::timestamptz is null or event_time <= %s::timestamptz)
        order by event_time desc limit 1
    """, (iid, as_of, as_of))
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
    source: str | None = Query(None, description="ls_chart | derived 등. 없으면 전체"),
    to: datetime | None = None,
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
        -- notional 추가(2026-08-03): 적재는 되는데 SELECT 에 없어서
        -- evidence/liquidity.py 의 Amihud 비유동성이 **영구 None** 이었다.
        -- 레지스트리에는 ADOPTED 로 등재돼 있는데 값이 안 나오던 상태.
        select bucket_time, open, high, low, close, volume, notional,
               source, is_final
        from market.market_bars
        where instrument_id = %s and interval_code = %s{cond}
        order by bucket_time desc limit %s
    """, tuple(params))


@app.get("/breadth")
def breadth(market: str = Query("KOSPI"), limit: int = Query(20, gt=0, le=500),
            as_of: Optional[str] = Query(
                None, description="이 시각까지만 본다(PIT, tz 포함 ISO8601). "
                                  "없으면 최신")):
    """시장 폭. **as_of 를 받는다** - 이력은 event_time 으로 원래 다 있었고
    컷오프만 없어서 Replay 금지였다. 금지는 도구를 없애지만 파라미터는 도구를
    살린다 - 에이전트가 값을 하는지 증명하려면 과거로 돌려볼 수 있어야 한다."""
    return _query("""
        select event_time, market, advancers, decliners, unchanged,
               up_volume, down_volume, total_value
        from market.market_breadth
        where market = %s
          and (%s::timestamptz is null or event_time <= %s::timestamptz)
        order by event_time desc limit %s
    """, (market, as_of, as_of, limit))


@app.get("/dq/windows")
def dq_windows(hours: int = Query(24, gt=0, le=168),
               limit: int = Query(50, gt=0, le=500)):
    """RES-02 Market Data Steward 의 감사 결과 (스트림별 품질 판정).

    ▶ **감사는 돌고 있었는데 아무도 못 읽었다.** collectors/market_data_steward.py
      가 market.data_quality_windows 에 판정을 쓰는데 이를 노출하는 엔드포인트가
      없었다 - Agent 는 DB Credential 을 안 받으므로(통합 계획 6.2) API 가
      없으면 존재하지 않는 것과 같다. RES-02 는 P0 페르소나인데 리서치
      파이프라인 어디에도 배선돼 있지 않았고, 그래서 데이터 품질 게이트가
      Packet 생성 경로 앞에 서 있지 않았다.

    quality_status 는 PASS / WARN / FAIL. **행이 0건인 것은 PASS 가 아니다** -
    감사가 안 돌았다는 뜻이므로 호출부가 미확인으로 다뤄야 한다.
    """
    return _query("""
        select window_start, window_end, provider, stream_type,
               observed_count, duplicate_count, p95_latency_ms, max_latency_ms,
               quality_status, rule_version, metrics
        from market.data_quality_windows
        where window_end > now() - make_interval(hours => %s)
        order by window_end desc, stream_type
        limit %s
    """, (hours, limit))


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


@app.get("/regime/daily")
def regime_daily(days: int = Query(20, gt=0, le=120),
                 as_of: Optional[str] = Query(
                     None, pattern=r"^\d{4}-\d{2}-\d{2}$",
                     description="이 거래일까지만 본다(PIT). 없으면 최신")):
    """시장 레짐 집계 (RES-07 의 결정론 재료) - 일봉 단면 지표.

    등락 종목수·SMA20 상회 비율을 거래일별로 계산한다. SMA20 은 봉 20개가
    다 있는 종목만 분모에 넣는다(부족 종목을 하회로 세지 않는다 - coverage
    로 분모를 명시). 계산은 전부 이 SQL - LLM 은 이 결과를 서술만 한다.
    """
    return _query("""
        with b as (
          select instrument_id,
                 (bucket_time at time zone 'Asia/Seoul')::date as d,
                 close,
                 avg(close) over w20 as sma20,
                 count(*) over w20 as n20,
                 lag(close) over (partition by instrument_id order by bucket_time) as prev_close
          from market.market_bars
          where interval_code = '1D' and source = 'ls_chart'
            -- ▶ PIT 컷오프. 없으면 최신까지(LIVE 에서는 '지금'이 곧 컷오프라
            --   아무것도 막지 않는다). **이력은 원래 다 있었다** - 컷오프를
            --   안 받아서 Replay 금지 목록에 올라 있었을 뿐이다.
            and (%s::date is null
                 or (bucket_time at time zone 'Asia/Seoul')::date <= %s::date)
          window w20 as (partition by instrument_id order by bucket_time
                         rows between 19 preceding and current row)
        )
        select d as trade_date,
               count(*) filter (where prev_close is not null and close > prev_close) as advancers,
               count(*) filter (where prev_close is not null and close < prev_close) as decliners,
               count(*) filter (where prev_close is not null and close = prev_close) as unchanged,
               count(*) filter (where n20 = 20 and close > sma20) as above_sma20,
               count(*) filter (where n20 = 20) as sma20_coverage,
               count(*) as symbols
        from b
        group by d order by d desc limit %s
    """, (as_of, as_of, days))


@app.get("/microstructure/{symbol}")
def microstructure(symbol: str, trade_date: str | None = Query(
        None, pattern=r"^\d{4}-\d{2}-\d{2}$",
        description="KST 거래일. 없으면 데이터가 있는 최근일")):
    """미시구조 집계 (RES-03 의 결정론 재료) - 체결·호가 하루 요약.

    체결: VWAP·매수/매도 체결량(side 부호)·건수. 호가: 스프레드 bp 중앙값/
    p90·심도 불균형 평균. 데이터가 없는 날은 404 가 아니라 rows 0 으로
    드러난다(없음과 조회 실패를 구분).
    """
    iid = _iid_or_404(symbol)
    cond = "and (event_time at time zone 'Asia/Seoul')::date = %s::date" if trade_date else ""
    params_t = (iid, trade_date) if trade_date else (iid,)
    ticks = _query(f"""
        select (event_time at time zone 'Asia/Seoul')::date as trade_date,
               count(*) as trades,
               sum(quantity) as volume,
               sum(price * quantity) / nullif(sum(quantity), 0) as vwap,
               sum(quantity) filter (where side = 1) as buy_volume,
               sum(quantity) filter (where side = -1) as sell_volume,
               min(event_time) as first_trade, max(event_time) as last_trade
        from market.market_ticks
        where instrument_id = %s {cond}
        group by 1 order by 1 desc limit 1
    """, params_t)
    quotes = _query(f"""
        select (event_time at time zone 'Asia/Seoul')::date as trade_date,
               count(*) as quotes,
               percentile_cont(0.5) within group
                 (order by spread / nullif(mid_price, 0) * 10000) as spread_bp_p50,
               percentile_cont(0.9) within group
                 (order by spread / nullif(mid_price, 0) * 10000) as spread_bp_p90,
               avg(depth_imbalance) as depth_imbalance_avg
        from market.market_quotes
        where instrument_id = %s {cond}
          and mid_price is not null and spread is not null
        group by 1 order by 1 desc limit 1
    """, params_t)
    return {"symbol": symbol, "ticks": ticks[0] if ticks else None,
            "quotes": quotes[0] if quotes else None,
            "note": None if ticks or quotes else "해당 일자 데이터 없음"}


# ---------------------------------------------------------------------------
# 자체 점검 - DB 없이
# ---------------------------------------------------------------------------

def _check_readonly_surface():
    for route in app.routes:
        methods = getattr(route, "methods", set()) or set()
        assert not (methods - {"GET", "HEAD", "OPTIONS"}), \
            f"읽기 전용 API 에 쓰기 메서드: {route.path}"
    print("  읽기 전용 표면           OK")


def _check_gateway_covers_all_routes():
    """모든 엔드포인트가 scope 매핑을 갖는가.

    게이트웨이가 붙은 뒤로는 매핑 없는 경로가 **500(gateway_misconfigured)**
    이 된다 - 새 엔드포인트를 추가하면서 ENDPOINT_SCOPES 를 안 고치면 그
    경로가 통째로 죽는다. 배포 전에 여기서 잡는다.
    """
    assert GATEWAY_STATUS in ("enforce", "observe"), \
        f"market-api 에 게이트웨이가 안 붙었다({GATEWAY_STATUS})"
    from tool_gateway import GatewayConfigError, is_open_path, scope_for

    missing = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if not path or is_open_path(path):
            continue
        # /bars/{symbol} 처럼 경로 파라미터가 있으면 접두사로 판정된다
        probe = path.split("{", 1)[0].rstrip("/") or path
        try:
            scope_for(probe)
        except GatewayConfigError:
            missing.append(path)
    assert not missing, f"scope 매핑이 없는 엔드포인트(500 이 된다): {missing}"
    print(f"  게이트웨이 매핑({GATEWAY_STATUS})  OK")


def _check_bar_params():
    from fastapi.exceptions import HTTPException as HE

    try:
        bars.__wrapped__ if hasattr(bars, "__wrapped__") else None
    except Exception:  # noqa: BLE001, S110 - intentional fallback boundary
        pass
    # naive to 거부 (PIT 9시간 오차 방지 - research-api 와 같은 규칙)
    global _sym2iid
    _sym2iid = {"005930": "00000000-0000-0000-0000-000000000000"}
    try:
        bars("005930", interval="1D", limit=10, source=None,
            to=datetime(2026, 7, 31, 9, 0))  # noqa: DTZ001 - intentionally invalid input
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
    _check_gateway_covers_all_routes()
    _check_bar_params()
    print("market-api 3개 영역 통과. 관통은 --probe")
