#!/usr/bin/env python3
"""Sprint J1: 거래 Calendar 수집 - LS 일별 시세 관측 기반.

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(거래 Calendar), 4.2, 8.2
      docs/06-integrations/ls-openapi/03-stock/ (t8410 API전용주식차트(일주월년))
      supabase/migrations/20260729000100_foundation_reference.sql
        (reference.market_calendar_versions, reference.market_sessions)

▶ 왜 관측 역산인가 (중요 - 이 파일의 전제다)
  거래 Calendar 를 직접 주는 공식 API 가 **없다**. 확인 결과:
    - KRX Open API 31개 서비스: 지수·주식·증권상품·채권·파생·일반상품·ESG 의 일별
      시세와 기본정보뿐이다. Calendar/휴장일 서비스가 목록에 없다.
    - LS Open API: `nday`(조회영업일수)는 요청 파라미터일 뿐 Calendar Source 가 아니다.

  그래서 **일별 시세가 존재하는 날 = 거래일**로 역산한다. "거래가 있었다"는 관측이
  휴장일 목록보다 강한 증거이므로 과거 Calendar 로는 신뢰할 수 있다.

▶ 이 방법의 한계. 숨기지 않고 metadata 와 문서에 남긴다.
  1. **미래 거래일을 알 수 없다.** 관측은 과거만 준다. 만기·정산일 계산이 필요해지면
     별도 Source 가 필요하고 그때까지 fail-closed 로 막아야 한다.
  2. 단일 종목으로 역산하면 그 종목의 거래정지일이 거짓 비거래일이 된다.
     그래서 유동성 높은 **여러 기준 종목의 합집합**을 쓴다.
  3. 개장·폐장 시각은 t8410 의 s_time/e_time 을 그대로 쓴다. 조회 시점의 값이라
     과거 특정일에 단축 거래가 있었다면 반영되지 않는다 - metadata 에 근거를 남긴다.

자체 점검(호출 없음): python departments/01-research/collectors/calendar_collector.py
실제 수집:            python departments/01-research/collectors/calendar_collector.py --collect
                      (옵션: --from 20260101 --to 20260730)
"""
from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_registry import SourceRegistry

COLLECTOR_VERSION = "research-calendar-collector-v1"

MARKET_KRX = "KRX"
KST = timezone(timedelta(hours=9))

# t8410 InBlock. gubun 2 = 일봉. qrycnt 는 Number 라 반드시 int 로 보낸다
# (문자열이면 IGW40011 data type 오류 - ls_client.call_tr 주석 참고).
T8410_PATH = "/stock/chart"
T8410_MAX_ROWS = 500          # 비압축 최대. 압축은 2000
T8410_RATE_LIMIT = 1.0        # 문서 "초당 호출 제한: 1"

# 역산 기준 종목. 유동성 상위이면서 서로 다른 섹터를 골라 동시 거래정지 위험을 줄인다.
# 한 종목이 거래정지된 날을 비거래일로 오판하지 않으려고 합집합을 쓴다.
REFERENCE_SYMBOLS = ("005930", "000660", "005380")  # 삼성전자, SK하이닉스, 현대차

SESSION_REGULAR = "REGULAR"


class CalendarUnavailable(RuntimeError):
    """Calendar 를 만들 수 없다. 추정으로 채우지 않는다."""


@dataclass(frozen=True)
class SessionRow:
    """reference.market_sessions 한 행."""

    trade_date: date
    session_type: str
    is_trading_day: bool
    opens_at: datetime | None
    closes_at: datetime | None
    metadata: dict


@dataclass(frozen=True)
class CalendarDraft:
    """적재 전 Calendar. content_hash 로 같은 내용의 중복 Version 을 막는다."""

    market: str
    effective_from: date
    effective_to: date
    sessions: tuple[SessionRow, ...]
    content_hash: str
    observed_symbols: tuple[str, ...]
    session_open: str
    session_close: str

    @property
    def trading_days(self) -> int:
        return sum(1 for s in self.sessions if s.is_trading_day)

    def summary(self) -> str:
        return (
            f"{self.market} {self.effective_from}~{self.effective_to} "
            f"전체 {len(self.sessions)}일 중 거래일 {self.trading_days}일 "
            f"(장 {self.session_open}~{self.session_close}, "
            f"기준종목 {len(self.observed_symbols)}개, hash {self.content_hash[:12]})"
        )


def _parse_hhmmss(raw: str) -> time:
    text = (raw or "").strip()
    if len(text) != 6 or not text.isdigit():
        raise CalendarUnavailable(f"장 시간 형식이 HHMMSS 가 아니다: {raw!r}")
    return time(int(text[0:2]), int(text[2:4]), int(text[4:6]))


def _parse_yyyymmdd(raw: str) -> date:
    text = (raw or "").strip()
    if len(text) != 8 or not text.isdigit():
        raise CalendarUnavailable(f"날짜 형식이 YYYYMMDD 가 아니다: {raw!r}")
    return date(int(text[0:4]), int(text[4:6]), int(text[6:8]))


def build_draft(
    *,
    market: str,
    start: date,
    end: date,
    observed: dict[str, set[date]],
    session_open: str,
    session_close: str,
) -> CalendarDraft:
    """관측된 거래일 집합으로 Calendar 를 만든다.

    observed 는 {종목코드: {거래일}} 이다. 합집합을 거래일로 본다 - 한 종목이 거래정지된
    날을 비거래일로 오판하지 않기 위한 것이다.

    비거래일도 is_trading_day=false 로 넣는다. "행이 없음"과 "비거래일"을 구분해야
    조회 쪽에서 Calendar 미수집 구간을 알 수 있다.
    """
    if start > end:
        raise ValueError("start 가 end 보다 늦다")
    if not observed:
        raise CalendarUnavailable("관측된 거래일이 없다. 추정으로 Calendar 를 만들지 않는다")

    union: set[date] = set()
    for days in observed.values():
        union |= days
    if not union:
        raise CalendarUnavailable("모든 기준 종목에서 거래일이 관측되지 않았다")

    opens = _parse_hhmmss(session_open)
    closes = _parse_hhmmss(session_close)
    if closes <= opens:
        raise CalendarUnavailable(f"폐장({session_close})이 개장({session_open})보다 이르다")

    # 종목별 관측 차이를 남긴다. 한 종목만 빠진 날은 그 종목의 거래정지 가능성이다.
    per_symbol_missing = {
        sym: sorted(d.isoformat() for d in (union - days)) for sym, days in observed.items()
    }

    sessions: list[SessionRow] = []
    cur = start
    while cur <= end:
        trading = cur in union
        meta: dict = {
            "method": "derived_from_daily_bars",
            "source": "ls:t8410",
            "reference_symbols": list(observed),
        }
        if trading:
            absent = [s for s, days in observed.items() if cur not in days]
            if absent:
                # 거래일인데 일부 종목에 바가 없다 = 그 종목의 거래정지 가능성
                meta["absent_symbols"] = absent
        sessions.append(
            SessionRow(
                trade_date=cur,
                session_type=SESSION_REGULAR,
                is_trading_day=trading,
                opens_at=datetime.combine(cur, opens, tzinfo=KST) if trading else None,
                closes_at=datetime.combine(cur, closes, tzinfo=KST) if trading else None,
                metadata=meta,
            )
        )
        cur += timedelta(days=1)

    # 같은 내용이면 같은 hash. Version 중복 적재를 DB unique(market, content_hash)가 막는다.
    material = "|".join(
        [
            market,
            session_open,
            session_close,
            ",".join(sorted(d.isoformat() for d in union)),
        ]
    )
    content_hash = hashlib.sha256(material.encode()).hexdigest()

    return CalendarDraft(
        market=market,
        effective_from=start,
        effective_to=end,
        sessions=tuple(sessions),
        content_hash=content_hash,
        observed_symbols=tuple(sorted(observed)),
        session_open=session_open,
        session_close=session_close,
    )


def fetch_trading_days(
    client, symbol: str, start: date, end: date
) -> tuple[set[date], str, str]:
    """t8410 으로 일봉을 받아 거래일 집합과 장 개장·폐장 시각을 돌려준다."""
    d = client.call_tr(
        path=T8410_PATH,
        tr_cd="t8410",
        in_block={
            "t8410InBlock": {
                "shcode": symbol,
                "gubun": "2",             # 일봉
                "qrycnt": T8410_MAX_ROWS,  # Number - int 로 보내야 한다
                "sdate": start.strftime("%Y%m%d"),
                "edate": end.strftime("%Y%m%d"),
                "cts_date": "",
                "comp_yn": "N",
                "sujung": "Y",
            }
        },
        rate_limit_per_sec=T8410_RATE_LIMIT,
    )
    head = d.get("t8410OutBlock") or {}
    bars = d.get("t8410OutBlock1")
    if bars is None:
        raise CalendarUnavailable(f"{symbol}: t8410OutBlock1 이 없다 (keys={sorted(d)})")

    days = {_parse_yyyymmdd(b["date"]) for b in bars if b.get("date")}
    return days, str(head.get("s_time", "")).strip(), str(head.get("e_time", "")).strip()


def collect(client, *, start: date, end: date, symbols=REFERENCE_SYMBOLS) -> CalendarDraft:
    SourceRegistry().require("ls_openapi_rest")

    observed: dict[str, set[date]] = {}
    opens: set[str] = set()
    closes: set[str] = set()
    for sym in symbols:
        days, s_time, e_time = fetch_trading_days(client, sym, start, end)
        if not days:
            # 한 종목에서 하나도 안 나오면 그 종목만 문제다. 전체를 실패시키지 않고 제외한다.
            continue
        observed[sym] = days
        if s_time:
            opens.add(s_time)
        if e_time:
            closes.add(e_time)

    if not observed:
        raise CalendarUnavailable(
            f"기준 종목 {list(symbols)} 전부에서 거래일이 관측되지 않았다. "
            f"조회 구간({start}~{end})이나 종목 코드를 확인하세요"
        )
    if len(opens) != 1 or len(closes) != 1:
        # 종목마다 장 시간이 다르게 오면 추정하지 않고 멈춘다
        raise CalendarUnavailable(f"장 시간이 종목별로 다르다: opens={opens} closes={closes}")

    return build_draft(
        market=MARKET_KRX,
        start=start,
        end=end,
        observed=observed,
        session_open=opens.pop(),
        session_close=closes.pop(),
    )


# ---------------------------------------------------------------------------
# 자체 점검 - 외부 호출 없음
# ---------------------------------------------------------------------------

def _d(s: str) -> date:
    return _parse_yyyymmdd(s)


def _check_draft_basic():
    # 2026-07-06(월) ~ 07-12(일). 07-11,12 는 주말이라 관측되지 않는다.
    observed = {
        "005930": {_d("20260706"), _d("20260707"), _d("20260708"), _d("20260709"), _d("20260710")},
        "000660": {_d("20260706"), _d("20260707"), _d("20260708"), _d("20260709"), _d("20260710")},
    }
    dr = build_draft(
        market=MARKET_KRX, start=_d("20260706"), end=_d("20260712"),
        observed=observed, session_open="090000", session_close="153000",
    )
    assert len(dr.sessions) == 7, "구간 전체 일수가 들어와야 한다"
    assert dr.trading_days == 5
    # 비거래일도 행으로 남는다 - "없음"과 "비거래일"을 구분하기 위해서다
    weekend = [s for s in dr.sessions if not s.is_trading_day]
    assert len(weekend) == 2
    assert all(s.opens_at is None and s.closes_at is None for s in weekend)

    mon = next(s for s in dr.sessions if s.trade_date == _d("20260706"))
    assert mon.opens_at.tzinfo is not None and mon.opens_at.hour == 9
    assert mon.closes_at.hour == 15 and mon.closes_at.minute == 30
    assert mon.metadata["method"] == "derived_from_daily_bars"
    print("  Draft 생성                  OK")


def _check_union_and_halt_detection():
    """한 종목의 거래정지를 비거래일로 오판하지 않는다."""
    observed = {
        "005930": {_d("20260706"), _d("20260707"), _d("20260708")},
        "000660": {_d("20260706"), _d("20260708")},  # 07-07 거래정지 가정
    }
    dr = build_draft(
        market=MARKET_KRX, start=_d("20260706"), end=_d("20260708"),
        observed=observed, session_open="090000", session_close="153000",
    )
    assert dr.trading_days == 3, "합집합이므로 3일 전부 거래일이다"
    tue = next(s for s in dr.sessions if s.trade_date == _d("20260707"))
    assert tue.is_trading_day is True
    assert tue.metadata["absent_symbols"] == ["000660"], "빠진 종목을 근거로 남겨야 한다"
    print("  합집합/거래정지 구분        OK")


def _check_content_hash():
    base = {"005930": {_d("20260706"), _d("20260707")}}
    a = build_draft(market=MARKET_KRX, start=_d("20260706"), end=_d("20260707"),
                    observed=base, session_open="090000", session_close="153000")
    b = build_draft(market=MARKET_KRX, start=_d("20260706"), end=_d("20260707"),
                    observed={"000660": {_d("20260706"), _d("20260707")}},
                    session_open="090000", session_close="153000")
    # 거래일 집합이 같으면 기준 종목이 달라도 같은 Calendar 다
    assert a.content_hash == b.content_hash

    c = build_draft(market=MARKET_KRX, start=_d("20260706"), end=_d("20260707"),
                    observed={"005930": {_d("20260706")}},
                    session_open="090000", session_close="153000")
    assert a.content_hash != c.content_hash

    # 장 시간이 바뀌면 다른 Calendar 다 (단축 거래일 반영)
    e = build_draft(market=MARKET_KRX, start=_d("20260706"), end=_d("20260707"),
                    observed=base, session_open="100000", session_close="153000")
    assert a.content_hash != e.content_hash
    print("  content_hash                OK")


def _check_fail_closed():
    # 관측이 없으면 추정하지 않는다
    for bad in ({}, {"005930": set()}):
        try:
            build_draft(market=MARKET_KRX, start=_d("20260706"), end=_d("20260707"),
                        observed=bad, session_open="090000", session_close="153000")
            raise AssertionError("관측 없이 Calendar 가 만들어졌다")
        except CalendarUnavailable:
            pass

    # 장 시간이 깨졌으면 멈춘다
    for so, sc in (("09000", "153000"), ("abcdef", "153000"), ("153000", "090000")):
        try:
            build_draft(market=MARKET_KRX, start=_d("20260706"), end=_d("20260707"),
                        observed={"005930": {_d("20260706")}}, session_open=so, session_close=sc)
            raise AssertionError(f"잘못된 장 시간이 통과했다: {so}~{sc}")
        except CalendarUnavailable:
            pass

    try:
        _parse_yyyymmdd("2026-07-06")
        raise AssertionError("잘못된 날짜 형식이 통과했다")
    except CalendarUnavailable:
        pass
    print("  fail-closed                 OK")


def _check_future_limit():
    """미래 거래일은 알 수 없다. 관측 없는 구간은 전부 비거래일로 표시된다.

    이 동작을 계약으로 고정한다 - 미래 구간을 요청하면 '거래일 0일' 이라는 명백히
    이상한 결과가 나와야 한다. 조용히 주말/평일을 추정해 채우면 그게 사실이 된다.
    """
    dr = build_draft(
        market=MARKET_KRX, start=_d("20260706"), end=_d("20260731"),
        observed={"005930": {_d("20260706")}},
        session_open="090000", session_close="153000",
    )
    assert dr.trading_days == 1
    assert sum(1 for s in dr.sessions if not s.is_trading_day) == 25
    print("  미래 구간 한계 명시         OK")


def _collect_and_report(start: date, end: date) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    from ls_client import LsRestClient

    client = LsRestClient()
    print(f"  LS_ENV={client.environment}, 기준종목 {list(REFERENCE_SYMBOLS)}")
    draft = collect(client, start=start, end=end)
    print(f"  {draft.summary()}")

    nt = [s.trade_date.isoformat() for s in draft.sessions if not s.is_trading_day]
    # 주말이 아닌 비거래일 = 휴장일 후보
    holidays = [d for d in nt if date.fromisoformat(d).weekday() < 5]
    print(f"  비거래일 {len(nt)}일 중 평일(휴장일 후보) {len(holidays)}일")
    if holidays:
        print(f"    {holidays}")
    partial = [
        (s.trade_date.isoformat(), s.metadata["absent_symbols"])
        for s in draft.sessions
        if s.is_trading_day and "absent_symbols" in s.metadata
    ]
    if partial:
        print(f"  일부 종목 바 없음(거래정지 가능): {partial[:5]}")
    return 0


if __name__ == "__main__":
    if "--collect" in sys.argv:
        args = sys.argv
        def opt(name, default):
            return args[args.index(name) + 1] if name in args else default
        s = _parse_yyyymmdd(opt("--from", "20260101"))
        e = _parse_yyyymmdd(opt("--to", datetime.now(KST).strftime("%Y%m%d")))
        print(f"{COLLECTOR_VERSION} 실제 수집 {s} ~ {e}")
        raise SystemExit(_collect_and_report(s, e))

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_draft_basic()
    _check_union_and_halt_detection()
    _check_content_hash()
    _check_fail_closed()
    _check_future_limit()
    print("Calendar 5개 영역 통과. 실제 수집은 --collect")
