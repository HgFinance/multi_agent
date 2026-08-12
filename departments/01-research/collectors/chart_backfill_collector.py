#!/usr/bin/env python3
"""LS 차트 백필 - 과거 봉을 market_bars 로 소급 적재 (백테스트 공백 커버).

담당: 재일 (리서치/퀀트)
근거: 재일님 결정 2026-07-31 "DB에 따로 적재해서 조회하는 식으로" - WebSocket
      수집은 오늘부터라 과거가 없다. 백테스트·에이전트는 market_bars 만 조회한다.

▶ 구조
  t8410(일봉, sujung=Y 수정주가) / t8412(N분봉) -> market.market_bars
    source='ls_chart' 로 적재 - 우리 틱 파생(bars_1m, source 별도)과 구분되며
    PK (bucket_time, instrument_id, market, interval_code, source) 가 멱등을 만든다.

▶ PIT 주석 (계약)
  봉은 **확정 사실**이라 소급 적재가 PIT 를 해치지 않는다 - 체결 Stream·뉴스와
  다른 점이다. 단 observed_at 은 적재 시각 그대로 둔다("언제 알았나"는 사실대로).
  수정주가(sujung=Y)는 과거 봉이 액면분할 등으로 재계산된 값이라는 뜻이다 -
  백테스트 가격 기준으로는 이것이 맞고, 원시가가 필요하면 sujung=N 별도 적재다.

▶ 실측 규격 (2026-07-31)
  - 두 TR 모두 path /stock/chart, 페이지당 최대 500행(comp_yn=N), 초당 1회
  - 연속조회: OutBlock 의 cts_date(/cts_time) 를 InBlock 에 되돌려준다 (과거 방향)
  - 필드: date[, time], open/high/low/close, jdiff_vol(거래량), value(거래대금)
  - t8412 time=HHMMSS 는 봉 구간의 **끝**(153000 = 마감 동시호가 봉)

사용
  python collectors/chart_backfill_collector.py                 # 자체 점검
  python collectors/chart_backfill_collector.py --daily --from 20240101   # 바스켓 일봉
  python collectors/chart_backfill_collector.py --minute --days 30 --top 50
  옵션: --symbols 005930,000660  (바스켓 대신 명시)
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from source_registry import load_project_env

COLLECTOR_VERSION = "research-chart-backfill-v1"
KST = timezone(timedelta(hours=9))
CHART_PATH = "/stock/chart"
RATE = 1.0            # 문서 "초당 1"
PAGE_MAX = 500
SOURCE = "ls_chart"
SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Bar:
    bucket_time: datetime
    interval_code: str    # '1D' | '1M'
    open: int
    high: int
    low: int
    close: int
    volume: int
    value: int


def parse_daily(row: dict) -> Bar:
    d = datetime.strptime(str(row["date"]), "%Y%m%d").replace(tzinfo=KST)
    return _bar(d, "1D", row)


def parse_minute(row: dict, *, ncnt: int = 1) -> Bar:
    # time 은 봉의 끝이다. bucket_time 은 관례상 시작으로 둔다 - 09:01:00 끝의
    # 1분봉 bucket 은 09:00:00. (bars_1m 연속집계의 time_bucket 과 같은 기준)
    end = datetime.strptime(  # noqa: DTZ007 - exchange-local timestamp receives KST below
        str(row["date"]) + f"{int(row['time']):06d}", "%Y%m%d%H%M%S"
    )
    start = end.replace(tzinfo=KST) - timedelta(minutes=ncnt)
    return _bar(start, "1M" if ncnt == 1 else f"{ncnt}M", row)


def _bar(bucket: datetime, code: str, row: dict) -> Bar:
    o, h, l, c = (int(row[k]) for k in ("open", "high", "low", "close"))
    if not (h >= max(o, c, l) and l <= min(o, c, h)):
        raise ValueError(f"OHLC 정합 위반: {row}")
    if min(o, h, l, c) <= 0:
        raise ValueError(f"0 이하 가격: {row}")
    return Bar(bucket, code, o, h, l, c, int(row.get("jdiff_vol") or 0),
               int(row.get("value") or 0))


def fetch_bars(client, symbol: str, *, daily: bool, sdate: str, edate: str,
               ncnt: int = 1, max_pages: int = 200) -> list[Bar]:
    """연속조회로 봉을 모은다. 과거 방향 페이징이며 실패는 예외로 올린다."""
    tr = "t8410" if daily else "t8412"
    cts_date, cts_time, tr_cont, key = "", "", "N", ""
    out: list[Bar] = []
    seen: set = set()
    for _page in range(max_pages):
        blk = {"shcode": symbol, "qrycnt": PAGE_MAX, "sdate": sdate, "edate": edate,
               "cts_date": cts_date, "comp_yn": "N"}
        if daily:
            blk |= {"gubun": "2", "sujung": "Y"}
        else:
            blk |= {"ncnt": ncnt, "nday": "0", "cts_time": cts_time}
        resp, hdrs = client.call_tr(
            path=CHART_PATH, tr_cd=tr, in_block={f"{tr}InBlock": blk},
            rate_limit_per_sec=RATE, tr_cont=tr_cont, tr_cont_key=key,
            return_headers=True,
        )
        rows = resp.get(f"{tr}OutBlock1") or []
        added = 0
        for r in rows:
            b = parse_daily(r) if daily else parse_minute(r, ncnt=ncnt)
            if b.bucket_time in seen:
                continue
            seen.add(b.bucket_time)
            out.append(b)
            added += 1
        ob = resp.get(f"{tr}OutBlock") or {}
        cts_date = str(ob.get("cts_date") or "").strip()
        cts_time = str(ob.get("cts_time") or "").strip()
        more = str(hdrs.get("tr_cont", "")).upper() == "Y" and (cts_date or cts_time)
        if not rows or added == 0 or not more:
            break
        tr_cont, key = "Y", str(hdrs.get("tr_cont_key", ""))
    return out


MARKET_CLOSE_KST = dtime(15, 30)


def is_bar_final(bucket_time, *, now=None) -> bool:
    """이 봉이 **확정됐는가.** 장중 조회에서 오늘 봉은 아직 아니다.

    ▶ 왜 필요한가 (실측 2026-08-04)
      LS 는 장중에도 그날 일봉을 준다 - 시작가·현재까지의 고저·현재가로 채운
      **미완성 봉**이다. 그런데 예전 코드는 is_final 을 모든 행에 True 로
      하드코딩했다. 필드는 있는데 상수 거짓말이 들어간 셈이라, 읽는 쪽에서
      "오늘 봉이 있다 = 확정됐다" 로 읽힐 수밖에 없었다.

      이건 계산 못 한 것을 0 으로 채우는 것과 같은 종류의 사고다 - 없는
      확실성을 만들어낸다.

    판정은 결정론이다: KST 기준 오늘 이전이면 확정, 오늘이면 마감(15:30)
    이후에만 확정. 미래 날짜는 확정이 아니다.
    """
    n = now or datetime.now(KST)
    d = bucket_time.astimezone(KST).date()
    if d < n.date():
        return True
    if d > n.date():
        return False              # 미래 봉 - 있을 수 없지만 확정으로 치지 않는다
    return n.time() >= MARKET_CLOSE_KST


def write_bars(conn, iid, bars: list[Bar], *, source_version: str,
               now=None) -> tuple[int, int]:
    """market_bars 멱등 적재. (신규, 중복).

    ▶ **do nothing 은 확정 봉에만 맞다.** 장중에 넣은 미확정 봉은 마감 뒤에
      확정치로 갱신돼야 하므로, 미확정으로 들어간 행은 나중 실행이 덮어쓴다.
      안 그러면 장중 현재가가 그날의 영구 종가로 굳는다.
    """
    from psycopg2.extras import execute_values

    ts = datetime.now(timezone.utc)
    kst_now = now or datetime.now(KST)
    rows = [
        (b.bucket_time, ts, iid, "KRX", b.interval_code,
         b.open, b.high, b.low, b.close, b.volume, 0, b.value,
         None, is_bar_final(b.bucket_time, now=kst_now),
         SOURCE, source_version, "PASS", SCHEMA_VERSION)
        for b in bars
    ]
    # ▶ count(*) 대신 RETURNING (2026-08-02 수정)
    #   예전에는 삽입 전후로 `select count(*) from market.market_bars
    #   where source=%s` 를 종목마다 두 번 했다. market_bars 는 400만 행
    #   하이퍼테이블이라 이 한 번이 전체 스캔이고, 종목 수만큼 반복하면
    #   백필 시간이 종목 수에 대해 2차식으로 늘어난다. 게다가 다른 세션이
    #   동시에 넣으면 그 증가분까지 '내가 넣은 것'으로 세어 수치가 틀린다.
    #   RETURNING 은 **이 문장이 실제로 삽입한 행**만 돌려준다.
    #
    #   fetch=True 가 필수다 - execute_values 는 page_size 단위로 문장을
    #   쪼개므로 그냥 fetchall() 하면 마지막 문장 것만 잡힌다
    #   (market_repository._insert 에 같은 함정의 실측 기록이 있다).
    with conn.cursor() as cur:
        returned = execute_values(cur, """
            insert into market.market_bars
              (bucket_time, observed_at, instrument_id, market, interval_code,
               open, high, low, close, volume, trade_count, notional,
               vwap, is_final, source, source_version, quality_status, schema_version)
            values %s
            on conflict (bucket_time, instrument_id, market, interval_code, source)
            -- ▶ **미확정 봉만 덮어쓴다.** 장중에 넣은 봉은 미완성이라 마감 뒤
            --   확정치로 갱신돼야 한다 - do nothing 으로 두면 장중 현재가가
            --   그날의 영구 종가로 굳는다. 확정된 행은 건드리지 않는다(봉은
            --   확정 사실이고, 확정 뒤 바뀌면 그건 정정이지 재수집이 아니다).
            do update set
              open = excluded.open, high = excluded.high, low = excluded.low,
              close = excluded.close, volume = excluded.volume,
              notional = excluded.notional, is_final = excluded.is_final,
              observed_at = excluded.observed_at
            where market.market_bars.is_final = false
            returning 1
        """, rows, page_size=1000, fetch=True)
        inserted = len(returned)
    conn.commit()
    return inserted, len(rows) - inserted


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def universe_symbols(limit: int = 0) -> tuple[str, ...]:
    """전체 KRX 상장 종목 코드. 기본 바스켓(뉴스 워치리스트 350종목)의 대체다.

    ▶ 왜 넓혀야 하나 (2026-08-10 실측)
      호가·체결은 2,600종목인데 일봉은 350종목이라 **유니버스가 어긋난다.**
      마이크로구조에서 찾은 것을 일봉으로 확인하려 해도 종목이 안 겹친다.
      횡단면 전략은 표본 종목 수가 곧 검정력이기도 하다.
    """
    import psycopg2

    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    cur = conn.cursor()
    # 상장 폐지된 종목도 남긴다 - 살아남은 것만 받으면 생존 편향이 들어간다.
    cur.execute("""
        select sy.symbol from reference.instruments i
        join reference.instrument_symbols sy using (instrument_id)
        where sy.is_primary and sy.symbol ~ '^[0-9]{6}$'
        group by sy.symbol order by sy.symbol
    """)
    out = tuple(r[0] for r in cur.fetchall())
    conn.close()
    return out[:limit] if limit else out


def _symbols_and_ids(symbols: tuple[str, ...]):
    import psycopg2
    from news_watch_service import parse_watchlist_file

    if not symbols:
        wl = Path(__file__).resolve().parent.parent / "config" / "news_watchlist.txt"
        symbols = parse_watchlist_file(wl.read_text(encoding="utf-8"))
    s = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
    with s.cursor() as cur:
        cur.execute("""
            select sy.symbol, i.instrument_id from reference.instruments i
            join reference.instrument_symbols sy using (instrument_id)
            where sy.symbol = any(%s) and sy.is_primary
        """, (list(symbols),))
        m = dict(cur.fetchall())
    s.close()
    missing = [x for x in symbols if x not in m]
    if missing:
        raise RuntimeError(f"Master 에 없는 종목: {missing[:5]}")
    return [(x, m[x]) for x in symbols]


def _collect(daily: bool, symbols, sdate: str, edate: str, ncnt: int, top: int | None) -> int:
    import psycopg2
    from ls_client import LsRestClient

    pairs = _symbols_and_ids(symbols)
    if top:
        pairs = pairs[:top]
    t = psycopg2.connect(load_project_env()["TIMESCALE_DATABASE_URL"], connect_timeout=10)
    client = LsRestClient()
    kind = "일봉" if daily else f"{ncnt}분봉"
    print(f"  {kind} {len(pairs)}종목, {sdate}~{edate} (초당 1회 - 예상 {len(pairs)}초+)")
    total_new = total_dup = 0
    oldest: datetime | None = None
    try:
        for i, (sym, iid) in enumerate(pairs, 1):
            bars = fetch_bars(client, sym, daily=daily, sdate=sdate, edate=edate, ncnt=ncnt)
            if not bars:
                print(f"    {sym}: 0행 - 확인 필요")
                continue
            new, dup = write_bars(t, iid, bars, source_version="t8410" if daily else "t8412")
            total_new += new
            total_dup += dup
            lo = min(b.bucket_time for b in bars)
            oldest = lo if oldest is None or lo < oldest else oldest
            if i % 25 == 0 or i == len(pairs):
                print(f"    [{i}/{len(pairs)}] 신규 {total_new:,} 중복 {total_dup:,} "
                      f"(최고 소급 {oldest:%Y-%m-%d})")
    finally:
        t.close()
    print(f"  완료: 신규 {total_new:,} / 중복(멱등) {total_dup:,} / 최고 소급 {oldest}")
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 호출·DB 없이
# ---------------------------------------------------------------------------

def _check_is_final_is_computed():
    """오늘 봉이 **장중에는 미확정**인가. 상수 True 였던 것을 고정한다.

    LS 는 장중에도 그날 일봉을 준다 - 시작가·현재까지 고저·현재가로 채운
    미완성 봉이다. 예전엔 is_final 을 모든 행에 True 로 하드코딩해서
    "오늘 봉이 있다 = 확정됐다" 로 읽힐 수밖에 없었다. 없는 확실성을
    만들어내는 것은 계산 못 한 값을 0 으로 채우는 것과 같은 사고다.
    """
    def bt(y, m, d):
        return datetime(y, m, d, 0, 0, tzinfo=KST)

    trading_day = datetime(2026, 8, 4, 11, 0, tzinfo=KST)     # 장중
    after_close = datetime(2026, 8, 4, 16, 0, tzinfo=KST)     # 마감 뒤

    # 어제 이전 봉은 언제 봐도 확정
    assert is_bar_final(bt(2026, 8, 3), now=trading_day) is True
    assert is_bar_final(bt(2026, 7, 31), now=after_close) is True
    # 오늘 봉은 마감 전 미확정, 마감 후 확정
    assert is_bar_final(bt(2026, 8, 4), now=trading_day) is False
    assert is_bar_final(bt(2026, 8, 4), now=after_close) is True
    # 마감 정각은 확정으로 본다(15:30 에 장이 닫힌다)
    assert is_bar_final(bt(2026, 8, 4),
                        now=datetime(2026, 8, 4, 15, 30, tzinfo=KST)) is True
    # 미래 봉은 확정이 아니다 - 있을 수 없지만 통과시키지 않는다
    assert is_bar_final(bt(2026, 8, 5), now=after_close) is False

    # UTC 로 들어와도 KST 로 환산해 판정한다 (bucket_time 은 tz-aware 다)
    utc_today = datetime(2026, 8, 3, 15, 0, tzinfo=timezone.utc)   # = 8/4 KST
    assert is_bar_final(utc_today, now=trading_day) is False
    assert is_bar_final(utc_today, now=after_close) is True


def _check_parse():
    d = parse_daily({"date": "20260730", "open": 100, "high": 110, "low": 90,
                     "close": 105, "jdiff_vol": 1000, "value": 5})
    assert d.interval_code == "1D" and d.bucket_time.tzinfo is KST
    m = parse_minute({"date": "20260731", "time": "090100", "open": 100, "high": 100,
                      "low": 99, "close": 99, "jdiff_vol": 10, "value": 1})
    assert m.bucket_time.timetz().replace(tzinfo=None) == dtime(9, 0), \
        "분봉 bucket 은 구간 시작이어야 한다"
    for bad in ({"high": 89}, {"low": 111}, {"open": 0}):
        row = {"date": "20260730", "open": 100, "high": 110, "low": 90,
               "close": 105, "jdiff_vol": 0, "value": 0} | bad
        try:
            parse_daily(row)
            raise AssertionError(f"{bad} 가 통과했다")
        except ValueError:
            pass
    print("  봉 파싱/정합             OK")


def _check_fetch_pagination():
    pages = [
        {"t8410OutBlock": {"cts_date": "20260701"},
         "t8410OutBlock1": [{"date": "20260730", "open": 1, "high": 2, "low": 1,
                             "close": 2, "jdiff_vol": 1, "value": 1}]},
        {"t8410OutBlock": {"cts_date": ""},
         "t8410OutBlock1": [{"date": "20260729", "open": 1, "high": 2, "low": 1,
                             "close": 2, "jdiff_vol": 1, "value": 1}]},
    ]
    calls = {"n": 0}

    class _C:
        def call_tr(self, **kw):
            i = calls["n"]
            calls["n"] += 1
            hdr = {"tr_cont": "Y" if i == 0 else "N", "tr_cont_key": "k"}
            return pages[min(i, 1)], hdr

    bars = fetch_bars(_C(), "005930", daily=True, sdate="20260101", edate="20260731")
    assert len(bars) == 2 and calls["n"] == 2
    # 같은 페이지 반복(진전 없음)이면 멈춘다
    calls["n"] = 0

    class _Loop:
        def call_tr(self, **kw):
            calls["n"] += 1
            return pages[0], {"tr_cont": "Y", "tr_cont_key": "k"}

    bars2 = fetch_bars(_Loop(), "005930", daily=True, sdate="20260101", edate="20260731")
    assert len(bars2) == 1 and calls["n"] == 2, "중복 페이지에서 무한 루프"
    print("  연속조회/무한루프 가드   OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    a = sys.argv
    def opt(n, d): return a[a.index(n) + 1] if n in a else d
    syms = tuple(s.strip() for s in opt("--symbols", "").split(",") if s.strip())
    if "--universe" in a and not syms:
        syms = universe_symbols(int(opt("--universe-limit", "0")))
        print(f"  전체 유니버스 {len(syms)}종목")

    if "--daily" in a:
        # ▶ --recent-days: 스케줄러용 상대 창. 매일 도는 증분 수집은 시작일을
        #   고정할 수 없고, 그렇다고 2024-01-01 부터 다시 받으면 매일 몇 년치를
        #   재요청한다. PK 가 멱등이라 겹쳐 받아도 안전하므로 짧은 창을 돌린다
        #   (연휴·장애로 며칠 빠져도 다음 실행이 메운다).
        recent = opt("--recent-days", "")
        if recent:
            start = (datetime.now(KST)
                     - timedelta(days=int(recent))).strftime("%Y%m%d")
            print(f"{COLLECTOR_VERSION} 일봉 증분 (최근 {recent}일)")
        else:
            start = opt("--from", "20240101")
            print(f"{COLLECTOR_VERSION} 일봉 백필")
        raise SystemExit(_collect(
            True, syms, start,
            opt("--to", datetime.now(KST).strftime("%Y%m%d")), 1,
            int(opt("--top", "0")) or None))
    if "--minute" in a:
        days = int(opt("--days", "30"))
        sd = (datetime.now(KST) - timedelta(days=days)).strftime("%Y%m%d")
        print(f"{COLLECTOR_VERSION} 분봉 백필 (최근 {days}일)")
        raise SystemExit(_collect(
            False, syms, sd, datetime.now(KST).strftime("%Y%m%d"),
            int(opt("--ncnt", "1")), int(opt("--top", "0")) or None))

    print(f"{COLLECTOR_VERSION} 자체 점검 (호출 없음)")
    _check_is_final_is_computed()
    print("  is_final 결정론 판정     OK")
    _check_parse()
    _check_fetch_pagination()
    print("차트 백필 3개 영역 통과. 실행은 --daily / --minute")
