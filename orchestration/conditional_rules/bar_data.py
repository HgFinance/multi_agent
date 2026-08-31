"""조건주문 평가용 봉 조달 - LS 통합차트 직접 조회.

조건주문 평가는 ``market.market_bars`` 를 **읽지 않는다.** 그 테이블은 야간
백필(chart-minute-universe, 20:30 KST)이 채우므로 원리상 당일 장중 규칙을
만족시킬 수 없고, 실제로 2026-08-26 실측에서 1M 이 381행/1종목/이틀 지연이라
``/bars?interval=1M`` 이 언제나 ``[]`` 를 돌려줬다. 그 결과 조건규칙이 ACTIVE
인데도 평가 행조차 남지 않았다. market_bars 는 백테스트·PIT 재현용 아카이브로
그대로 두고(개발원칙 5), **실시간 평가만** 이 경로로 분리한다.

▶ 왜 t8452 인가
  (통합)주식챠트(N분). ``exchgubun='U'`` 로 KRX·NXT 를 합쳐 준다. 거래소 단독
  TR(t8412)로 받으면 NXT 가 통째로 빠진다 - subscription_plan.py 가 실시간
  틱에서 이미 겪고 고친 문제다(2026-08-11 실측: 하루 체결의 25%). 조건주문
  시세(t1102)도 이미 ``exchgubun='U'`` 를 쓰므로 봉만 KRX 단독이면 시세와
  봉이 다른 시장을 보게 된다.

▶ 필요한 만큼만 받는다
  LS 는 페이지당 최대 500행(비압축)이고 초당 1회다. 그래서 요청 봉 수를
  지표가 요구하는 만큼으로 계산해 그만큼만 페이징한다. SMA(20)·SMA(60)은
  당일 한 페이지로 끝나고, SMA(120)을 장 초반에 계산할 때만 전일 구간으로
  연속조회가 들어간다.

▶ 실패는 조용히 통과시키지 않는다
  스로틀·타임아웃은 재시도하고, 그래도 실패하면 예외로 올려 평가를 보류한다
  (개발원칙 9: 실패 시 거래 확대가 아니라 차단 방향). 부족한 봉을 0 이나
  기본값으로 채우지 않는다.
"""

from __future__ import annotations

import math
import os
import threading
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from .contracts import Timeframe
from .evaluator import Candle
from .indicators.broker.ls_readonly import (
    LSOpenAPIReadOnlyTransport,
    LSReadOnlyTransportError,
)


KST = timezone(timedelta(hours=9))

CHART_PATH = "/stock/chart"
TR_DAILY = "t8451"           # (통합)주식챠트(일주월년)
TR_MINUTE = "t8452"          # (통합)주식챠트(N분)
PAGE_MAX = 500               # 문서: 최대 500 (t8452 는 비압축만)
MIN_CALL_INTERVAL_SECONDS = 1.05   # 문서 "초당 1" + 여유
MAX_PAGES = 12               # 500*12 = 6000 봉. 그 이상 요구하면 계약 위반이다
MAX_ATTEMPTS = 3

# KRX 정규장 09:00~15:30 = 390분. 세션당 1분봉 상한이며 세션 수 환산에 쓴다.
SESSION_MINUTES = 390
MARKET_CLOSE_KST = dtime(15, 30)

# Timeframe -> 1분봉 몇 개가 한 봉인가.
_FRAME_MINUTES: dict[Timeframe, int] = {
    Timeframe.M1: 1,
    Timeframe.M3: 3,
    Timeframe.M5: 5,
    Timeframe.M10: 10,
    Timeframe.M15: 15,
    Timeframe.M30: 30,
    Timeframe.H1: 60,
}


class BarResolverError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class BarResolver(Protocol):
    def bars(
        self, symbol: str, timeframe: Timeframe, required: int
    ) -> list[Candle]: ...


def _decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BarResolverError(
            "CONDITIONAL_BAR_VALUE_INVALID",
            "LS chart row carries a non-numeric price or volume",
            retryable=False,
        ) from exc


def _is_no_trade(row: dict[str, Any]) -> bool:
    """무거래 행인가.

    LS 는 무거래 구간에 ``open=high=low=0, close=기준가, jdiff_vol=0`` 을 준다.
    이상한 값이 아니라 **봉이 없다는 뜻**이라 건너뛴다 - OHLC 정합 위반으로
    올리면 관리종목·거래정지가 섞인 순간 실행 전체가 죽는다.
    """
    try:
        return (
            int(float(row.get("open", 0))) == 0
            and int(float(row.get("high", 0))) == 0
            and int(float(row.get("low", 0))) == 0
        )
    except (TypeError, ValueError):
        return True


def _bucket_start(row: dict[str, Any], ncnt: int) -> datetime:
    """``time`` 은 봉 구간의 **끝**이다. bucket_time 은 관례상 시작으로 둔다."""

    end = datetime.strptime(
        f"{int(row['date']):08d}{int(row['time']):06d}", "%Y%m%d%H%M%S"
    ).replace(tzinfo=KST)
    return end - timedelta(minutes=ncnt)


def _elapsed_session_minutes(now: datetime) -> int:
    """오늘 정규장에서 지금까지 확정될 수 있는 1분봉 수."""

    open_at = now.replace(hour=9, minute=0, second=0, microsecond=0)
    close_at = now.replace(
        hour=MARKET_CLOSE_KST.hour, minute=MARKET_CLOSE_KST.minute,
        second=0, microsecond=0,
    )
    end = min(now, close_at)
    if end <= open_at:
        return 0
    return int((end - open_at).total_seconds() // 60)


def _start_date_for(needed: int, now: datetime) -> date:
    """필요한 봉 수를 채울 만큼만 과거로 넓힌다.

    ``qrycnt`` 는 응답 크기를 제한하지 않는다(2026-08-26 실측: qrycnt=10 에도
    365행이 왔다). 응답 범위를 정하는 것은 sdate~edate 이고, ``stime`` 은
    문서상 미사용이라 하루보다 잘게 자를 수 없다. 그래서 조회 단위는 **세션
    하루**이며, 오늘치로 충분하면 전일을 건드리지 않는다 - SMA(20)·SMA(60)은
    장 초반을 빼면 오늘 한 페이지로 끝난다.
    """

    remaining = needed - _elapsed_session_minutes(now)
    if remaining <= 0:
        return now.date()
    extra_sessions = math.ceil(remaining / SESSION_MINUTES)
    # 주말·공휴일 때문에 달력일은 세션보다 길다. 넉넉히 잡되 상한을 둔다.
    return now.date() - timedelta(days=min(extra_sessions * 2 + 2, 30))


def _start_date_for_daily(needed: int, now: datetime) -> date:
    """Return a conservative calendar window for ``needed`` final daily bars."""

    # Weekends/holidays mean ``needed * 2`` is occasionally too small.  The
    # capped three-day multiplier keeps one PAPER request bounded while giving
    # SMA/RSI enough prior sessions without pretending a missing bar exists.
    return now.date() - timedelta(days=min(max(needed * 3 + 10, 30), 3650))


def timeframe_close_at(bucket_time: datetime, timeframe: Timeframe) -> datetime:
    """Return the canonical close timestamp for a final domestic-stock bar."""

    if bucket_time.tzinfo is None:
        raise BarResolverError(
            "CONDITIONAL_BAR_TIME_INVALID",
            "candle bucket_time must include timezone",
            retryable=False,
        )
    local = bucket_time.astimezone(KST)
    if timeframe is Timeframe.D1:
        return local.replace(
            hour=MARKET_CLOSE_KST.hour,
            minute=MARKET_CLOSE_KST.minute,
            second=0,
            microsecond=0,
        ).astimezone(timezone.utc)
    step = _FRAME_MINUTES.get(timeframe)
    if step is None:
        raise BarResolverError(
            "CONDITIONAL_BAR_TIMEFRAME_UNSUPPORTED",
            f"{timeframe.value} is not served by the LS chart resolver",
            retryable=False,
        )
    return (local + timedelta(minutes=step)).astimezone(timezone.utc)


class LSChartBarResolver:
    """t8452 를 직접 읽어 확정 1분봉을 만들고, 필요하면 상위 프레임으로 묶는다.

    상위 프레임(5M/15M/1H)은 LS 의 native ncnt 를 쓰지 않고 **확정 1분봉을
    묶어서** 만든다. CONDITIONAL_TRADING_RULE_ENGINE.md 가 "higher intraday
    frames have one canonical implementation: aggregate final 1M candles" 로
    정렬 규칙을 하나로 못박고 있어서다. 조달처만 바꾸고 정렬·부분봉 정책은
    기존 계약을 그대로 유지한다.
    """

    def __init__(self, transport: LSOpenAPIReadOnlyTransport) -> None:
        self._transport = transport
        self._rate_lock = threading.Lock()
        self._last_call = 0.0
        self._cache_lock = threading.Lock()
        # (symbol, timeframe) -> (bucket_key, candles)
        self._cache: dict[tuple[str, str], tuple[datetime, list[Candle]]] = {}

    @classmethod
    def from_env(cls) -> "LSChartBarResolver":
        # 조건주문 워커는 PAPER 전용이다. 공유 LS 클라이언트가 LIVE 도 지원하지만
        # 여기서는 거부한다 - 시세 리졸버와 같은 계약이다.
        if os.getenv("LS_ENV", "").strip().upper() != "PAPER":
            raise BarResolverError(
                "CONDITIONAL_BAR_PAPER_ENV_REQUIRED",
                "conditional bar resolver requires LS_ENV=PAPER",
                retryable=False,
            )
        try:
            return cls(LSOpenAPIReadOnlyTransport.from_env())
        except Exception as exc:  # noqa: BLE001 - 자격/설정 실패를 한 코드로 접는다
            raise BarResolverError(
                "CONDITIONAL_BAR_PROVIDER_UNAVAILABLE",
                "LS PAPER chart provider is unavailable",
            ) from exc

    # ------------------------------------------------------------------
    # 전송
    # ------------------------------------------------------------------
    def _throttled_call(self, *, tr_code: str, payload: dict[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(MAX_ATTEMPTS):
            with self._rate_lock:
                wait = MIN_CALL_INTERVAL_SECONDS - (time.monotonic() - self._last_call)
                if wait > 0:
                    time.sleep(wait)
                self._last_call = time.monotonic()
            try:
                return self._transport.request_sync(
                    path=CHART_PATH, tr_code=tr_code, payload=payload
                )
            except (LSReadOnlyTransportError, TimeoutError) as exc:
                # 급속 반복에서 실측된 일시적 스로틀. 재시도로 회복된다.
                last_error = exc
                time.sleep(MIN_CALL_INTERVAL_SECONDS * (attempt + 1))
        raise BarResolverError(
            "CONDITIONAL_BAR_PROVIDER_UNAVAILABLE",
            "LS chart request failed after retries",
        ) from last_error

    def _fetch_minute_bars(self, symbol: str, needed: int) -> list[Candle]:
        """확정 1분봉을 ``needed`` 개 모을 때까지만 과거로 연속조회한다."""

        now = datetime.now(KST)
        sdate = _start_date_for(needed, now).strftime("%Y%m%d")
        edate = now.date().strftime("%Y%m%d")

        collected: dict[datetime, Candle] = {}
        cts_date, cts_time = "", ""
        for _page in range(MAX_PAGES):
            block = {
                "shcode": symbol,
                "ncnt": 1,
                # qrycnt 는 응답 크기를 줄이지 못한다(실측). 페이지 상한만 지킨다.
                "qrycnt": PAGE_MAX,
                "nday": "0",
                "sdate": sdate,
                "edate": edate,
                "cts_date": cts_date,
                "cts_time": cts_time,
                "comp_yn": "N",
                "exchgubun": "U",
            }
            response = self._throttled_call(
                tr_code=TR_MINUTE, payload={f"{TR_MINUTE}InBlock": block}
            )
            code = str(response.get("rsp_cd") or "").strip()
            if code and code != "00000":
                raise BarResolverError(
                    "CONDITIONAL_BAR_PROVIDER_REJECTED",
                    f"LS chart rejected the request: {response.get('rsp_msg') or code}",
                )
            rows = response.get(f"{TR_MINUTE}OutBlock1") or []
            for row in rows:
                if _is_no_trade(row):
                    continue
                bucket = _bucket_start(row, 1)
                if bucket in collected:
                    continue
                # 진행 중인 봉은 버린다. time 이 봉의 끝이므로 끝이 아직
                # 지나지 않았으면 확정이 아니다.
                if bucket + timedelta(minutes=1) > now:
                    continue
                collected[bucket] = Candle(
                    bucket_time=bucket,
                    open=_decimal(row["open"]),
                    high=_decimal(row["high"]),
                    low=_decimal(row["low"]),
                    close=_decimal(row["close"]),
                    volume=_decimal(row.get("jdiff_vol", 0)),
                    is_final=True,
                )
            if len(collected) >= needed:
                break
            out_block = response.get(f"{TR_MINUTE}OutBlock") or {}
            next_date = str(out_block.get("cts_date") or "").strip()
            next_time = str(out_block.get("cts_time") or "").strip()
            if not rows or (not next_date and not next_time):
                break
            if (next_date, next_time) == (cts_date, cts_time):
                break
            cts_date, cts_time = next_date, next_time

        return sorted(collected.values(), key=lambda candle: candle.bucket_time)

    def _fetch_daily_bars(self, symbol: str, needed: int) -> list[Candle]:
        """Fetch final adjusted daily candles through integrated LS t8451."""

        now = datetime.now(KST)
        sdate = _start_date_for_daily(needed, now).strftime("%Y%m%d")
        edate = now.date().strftime("%Y%m%d")
        collected: dict[datetime, Candle] = {}
        cts_date = ""
        for _page in range(MAX_PAGES):
            block = {
                "shcode": symbol,
                "gubun": "2",
                "qrycnt": PAGE_MAX,
                "sdate": sdate,
                "edate": edate,
                "cts_date": cts_date,
                "comp_yn": "N",
                "sujung": "Y",
                "exchgubun": "U",
            }
            response = self._throttled_call(
                tr_code=TR_DAILY, payload={f"{TR_DAILY}InBlock": block}
            )
            code = str(response.get("rsp_cd") or "").strip()
            if code and code != "00000":
                raise BarResolverError(
                    "CONDITIONAL_BAR_PROVIDER_REJECTED",
                    f"LS daily chart rejected the request: {response.get('rsp_msg') or code}",
                )
            rows = response.get(f"{TR_DAILY}OutBlock1") or []
            for row in rows:
                if _is_no_trade(row):
                    continue
                try:
                    bucket = datetime.strptime(str(row["date"]), "%Y%m%d").replace(tzinfo=KST)
                except (KeyError, TypeError, ValueError) as exc:
                    raise BarResolverError(
                        "CONDITIONAL_BAR_TIME_INVALID",
                        "LS daily chart row has an invalid date",
                        retryable=False,
                    ) from exc
                # LS returns an in-progress daily row while the session is
                # open.  A daily indicator must never see that partial bar.
                if bucket.date() == now.date() and now.time() < MARKET_CLOSE_KST:
                    continue
                if bucket in collected:
                    continue
                collected[bucket] = Candle(
                    bucket_time=bucket,
                    open=_decimal(row["open"]),
                    high=_decimal(row["high"]),
                    low=_decimal(row["low"]),
                    close=_decimal(row["close"]),
                    volume=_decimal(row.get("jdiff_vol", 0)),
                    is_final=True,
                )
            if len(collected) >= needed:
                break
            out_block = response.get(f"{TR_DAILY}OutBlock") or {}
            next_date = str(out_block.get("cts_date") or "").strip()
            if not rows or not next_date or next_date == cts_date:
                break
            cts_date = next_date
        return sorted(collected.values(), key=lambda candle: candle.bucket_time)

    # ------------------------------------------------------------------
    # 집계
    # ------------------------------------------------------------------
    @staticmethod
    def _aggregate(minute_bars: list[Candle], step: int) -> list[Candle]:
        if step == 1:
            return minute_bars
        grouped: dict[datetime, list[Candle]] = {}
        for candle in minute_bars:
            anchor = candle.bucket_time.astimezone(KST)
            floored = anchor.replace(
                minute=(anchor.minute // step) * step if step < 60 else 0,
                second=0,
                microsecond=0,
            )
            grouped.setdefault(floored, []).append(candle)

        now = datetime.now(KST)
        out: list[Candle] = []
        for bucket, members in sorted(grouped.items()):
            # 내부 공백이 있는 bucket 은 쓰지 않는다 - 설계 문서가 중복·공백
            # bucket 을 Indicator Engine 입력에서 배제하라고 못박는다.
            if len(members) != step:
                continue
            if bucket + timedelta(minutes=step) > now:
                continue
            members.sort(key=lambda candle: candle.bucket_time)
            out.append(
                Candle(
                    bucket_time=bucket,
                    open=members[0].open,
                    high=max(member.high for member in members),
                    low=min(member.low for member in members),
                    close=members[-1].close,
                    volume=sum((member.volume for member in members), Decimal(0)),
                    is_final=True,
                )
            )
        return out

    # ------------------------------------------------------------------
    # 공개 API
    # ------------------------------------------------------------------
    def bars(
        self, symbol: str, timeframe: Timeframe, required: int
    ) -> list[Candle]:
        normalized = symbol.strip().upper()
        if len(normalized) != 6 or not normalized.isalnum():
            raise BarResolverError(
                "CONDITIONAL_BAR_SYMBOL_INVALID",
                "conditional bar symbol is invalid",
                retryable=False,
            )
        step = _FRAME_MINUTES.get(timeframe)
        if step is None and timeframe is not Timeframe.D1:
            raise BarResolverError(
                "CONDITIONAL_BAR_TIMEFRAME_UNSUPPORTED",
                f"{timeframe.value} is not served by the LS chart resolver",
                retryable=False,
            )

        # 봉은 봉 마감에만 바뀐다. 같은 bucket 안에서는 다시 부르지 않는다 -
        # 워커는 사이클마다 모든 규칙을 도는데, 그때마다 REST 를 열면 초당 1회
        # 한도를 즉시 넘는다.
        now = datetime.now(KST)
        if timeframe is Timeframe.D1:
            # Invalidate the cache when today's daily candle becomes final.
            bucket_key = now.replace(
                hour=16 if now.time() >= MARKET_CLOSE_KST else 0,
                minute=0,
                second=0,
                microsecond=0,
            )
        else:
            assert step is not None
            bucket_key = now.replace(
                minute=(now.minute // step) * step if step < 60 else 0,
                second=0,
                microsecond=0,
            )
        cache_key = (normalized, timeframe.value)
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and cached[0] == bucket_key and len(cached[1]) >= required:
                return list(cached[1])

        if timeframe is Timeframe.D1:
            candles = self._fetch_daily_bars(normalized, required + 1)
        else:
            assert step is not None
            minute_bars = self._fetch_minute_bars(normalized, required * step + step)
            candles = self._aggregate(minute_bars, step)
        with self._cache_lock:
            self._cache[cache_key] = (bucket_key, candles)
        return list(candles)
