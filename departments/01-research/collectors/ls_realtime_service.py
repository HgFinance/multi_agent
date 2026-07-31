#!/usr/bin/env python3
"""상주형 LS 실시간 시세 수집 서비스 - Docker Container 의 entrypoint (F03 Runtime).

담당: 재일 (리서치/퀀트)
근거: 재일님 지시(2026-07-31) "호가·체결 수집기도 Docker 에" +
      TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md Sprint J1 (장시간 실행 Runtime)

구조 - 컨테이너는 24시간 떠 있고, 소켓은 세션 창에서만 연다
    대기 (개장 전/휴장)  ->  수집 (동시호가 35분 전 ~ 마감 +10분, 소켓 1개 유지)
      ^                       |  WebSocket -> normalize -> MarketSink -> TimescaleDB
      +-----------------------+  세션 끝/오류 -> 통계 출력 -> 다음 세션 대기

  ▶ 뉴스(news_watch_service)와 달리 **세션 창 개념이 있다.** 뉴스는 24시간
    폴링이지만 시세는 장 밖에서 소켓을 열어 봐야 조용한 연결만 유지된다.
    세션 판정은 Calendar 를 따르되, Calendar 가 오늘을 모르면
    make_trading_day_check 와 같은 원칙으로 **평일은 거래일로 간주**하고
    calendar_unverified 로 기록한다 (주말은 KRX 개장 전례가 없어 비거래로 본다).

  ▶ 구독은 news 와 같은 watchlist 파일을 쓴다 (시가총액 상위 70종목 = 140구독,
    소켓당 한도 200 이내). venue 별 tr_cd 는 subscription_plan.TR_MATRIX 가
    권위다 - 코스피 S3_/H1_, 코스닥 K3_/HA_ 를 섞어 한 소켓으로 받는다.

  ▶ 종료·오류 시 Sink 꼬리를 밀어 넣는다. worker 내부 재접속(MAX_RECONNECTS)이
    소진되면 서비스가 백오프 후 세션 창이 남아 있는 동안 전부 다시 만든다.

환경변수 (전부 선택)
  LS_WATCH_SYMBOLS            쉼표 구분 KRX 종목코드 (최우선)
  LS_WATCH_SYMBOLS_FILE       기본 config/news_watchlist.txt (뉴스와 같은 70종목)
  LS_PRE_OPEN_MINUTES         개장 전 수집 시작 여유, 기본 35 (동시호가 08:30 포함)
  LS_POST_CLOSE_MINUTES       마감 후 잔여 수신 여유, 기본 10

실행
  python collectors/ls_realtime_service.py            # 상주 실행
  python collectors/ls_realtime_service.py --check    # 자체 점검 (접속 없음)
"""
from __future__ import annotations

import asyncio
import signal
import sys
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from ls_realtime_worker import (  # noqa: E402
    KST,
    SOURCE_ID,
    LsRealtimeError,
    LsRealtimeWorker,
    MarketSink,
    make_trading_day_check,
)
from news_watch_service import parse_watchlist_file  # noqa: E402
from source_registry import SourceRegistry, load_project_env  # noqa: E402
from subscription_plan import (  # noqa: E402
    SUBSCRIPTIONS_PER_SOCKET,
    TR_MATRIX,
    WEBSOCKET_PATH,
    AssetClass,
    DataKind,
    Venue,
)

SERVICE_VERSION = "research-ls-realtime-service-v1"

DEFAULT_OPEN = time(9, 0)
DEFAULT_CLOSE = time(15, 30)
SESSION_RETRY_BACKOFF = 30.0
IDLE_RECHECK_SECONDS = 4 * 3600.0  # 휴장일에 세션 판정을 다시 하는 주기


@dataclass(frozen=True)
class SessionWindow:
    trade_date: date
    is_trading: bool
    starts_at: datetime | None   # 수집 시작 (개장 - pre_open)
    ends_at: datetime | None     # 수집 종료 (마감 + post_close)
    calendar_verified: bool

    def describe(self) -> str:
        if not self.is_trading:
            return f"{self.trade_date} 비거래일"
        tag = "" if self.calendar_verified else " (calendar_unverified - 평일 간주)"
        return (f"{self.trade_date} 수집창 {self.starts_at:%H:%M}~{self.ends_at:%H:%M}"
                f"{tag}")


def resolve_window(
    trade_date: date,
    session,  # ref.market_session 반환: (is_trading, opens, closes) | None
    *,
    pre_open_minutes: float,
    post_close_minutes: float,
) -> SessionWindow:
    """오늘의 수집 창을 정한다.

    Calendar 에 오늘이 있으면 그대로 따른다. 없으면 - 관측 역산 Calendar 는 당일을
    모른다 - **평일은 거래일로 간주**한다 (ls_realtime_worker.make_trading_day_check
    와 같은 원칙: 판정 불가를 휴장으로 단정해 수집을 멈추는 쪽이 더 나쁘다).
    주말은 KRX 정규장 개장 전례가 없으므로 비거래로 본다.
    """
    pre = timedelta(minutes=pre_open_minutes)
    post = timedelta(minutes=post_close_minutes)

    if session is not None:
        is_trading, opens, closes = session
        if not is_trading:
            return SessionWindow(trade_date, False, None, None, True)
        opens = opens.astimezone(KST) if opens else datetime.combine(trade_date, DEFAULT_OPEN, tzinfo=KST)
        closes = closes.astimezone(KST) if closes else datetime.combine(trade_date, DEFAULT_CLOSE, tzinfo=KST)
        return SessionWindow(trade_date, True, opens - pre, closes + post, True)

    if trade_date.isoweekday() >= 6:
        return SessionWindow(trade_date, False, None, None, True)

    opens = datetime.combine(trade_date, DEFAULT_OPEN, tzinfo=KST)
    closes = datetime.combine(trade_date, DEFAULT_CLOSE, tzinfo=KST)
    return SessionWindow(trade_date, True, opens - pre, closes + post, False)


def build_subscriptions(
    rows: list[tuple[str, str]],  # (symbol, venue)
) -> list[tuple[str, str]]:
    """venue 별 tr_cd 로 (tr_cd, 종목) 구독 목록을 만든다. 모르는 venue 는 즉시 실패."""
    subs: list[tuple[str, str]] = []
    for symbol, venue_raw in rows:
        try:
            venue = Venue(venue_raw)
        except ValueError:
            raise LsRealtimeError(f"모르는 venue 다: {venue_raw} ({symbol})")
        tick = TR_MATRIX.get((venue, AssetClass.EQUITY, DataKind.TICK))
        quote = TR_MATRIX.get((venue, AssetClass.EQUITY, DataKind.QUOTE))
        if not tick or not quote:
            raise LsRealtimeError(f"TR_MATRIX 에 {venue} EQUITY 매핑이 없다 ({symbol})")
        subs.append((tick, symbol))
        subs.append((quote, symbol))
    if len(subs) > SUBSCRIPTIONS_PER_SOCKET:
        raise LsRealtimeError(
            f"구독 {len(subs)}건이 소켓 한도 {SUBSCRIPTIONS_PER_SOCKET}를 넘는다 - "
            f"종목을 줄이거나 socket_batches 분할을 붙여야 한다"
        )
    return subs


def load_symbols(env: dict) -> tuple[str, ...]:
    explicit = tuple(
        s.strip() for s in (env.get("LS_WATCH_SYMBOLS") or "").split(",") if s.strip()
    )
    if explicit:
        return explicit
    path = Path((env.get("LS_WATCH_SYMBOLS_FILE") or "config/news_watchlist.txt").strip())
    if not path.exists():
        raise LsRealtimeError(f"LS_WATCH_SYMBOLS_FILE 이 없다: {path}")
    symbols = parse_watchlist_file(path.read_text(encoding="utf-8"))
    if not symbols:
        raise LsRealtimeError(f"LS_WATCH_SYMBOLS_FILE 이 비어 있다: {path}")
    return symbols


# ---------------------------------------------------------------------------
# 세션 실행
# ---------------------------------------------------------------------------

def _fetch_reference(symbols: tuple[str, ...]):
    """(symbol -> instrument_id, [(symbol, venue)], 최근 거래일 집합, 오늘 세션)."""
    from reference_repository import SupabaseReferenceRepository

    ref = SupabaseReferenceRepository()
    try:
        with ref._conn.cursor() as cur:
            cur.execute(
                """
                select s.symbol, i.instrument_id, i.venue
                from reference.instruments i
                join reference.instrument_symbols s using (instrument_id)
                where i.market = 'KRX' and i.instrument_type = 'STOCK'
                  and s.symbol = any(%s) and s.is_primary
                """,
                (list(symbols),),
            )
            rows = cur.fetchall()
        missing = set(symbols) - {r[0] for r in rows}
        if missing:
            raise LsRealtimeError(f"Instrument Master 에 없는 종목이다: {sorted(missing)}")
        iid_by_symbol = {sym: iid for sym, iid, _v in rows}
        venue_rows = [(sym, venue) for sym, _iid, venue in rows]
        trading_days = {d for d, _o, _c in ref.recent_trading_sessions(limit=40)}
        today = datetime.now(KST).date()
        session = ref.market_session(today)
    finally:
        ref.close()
    return iid_by_symbol, venue_rows, trading_days, session


async def run_capture(window: SessionWindow, symbols: tuple[str, ...], stop: asyncio.Event) -> None:
    """세션 창 하나를 수집한다. 창이 끝나거나 stop 이 설 때까지."""
    from ls_client import LsRestClient
    from market_repository import TimescaleMarketRepository

    env = load_project_env()
    SourceRegistry(env=env).require(SOURCE_ID)
    mode = (env.get("LS_ENV") or "PAPER").upper()
    ws_base = env["LS_WS_BASE_URL_PAPER"] if mode == "PAPER" else env["LS_WS_BASE_URL"]
    ws_url = ws_base.rstrip("/") + WEBSOCKET_PATH
    dsn = (env.get("TIMESCALE_DATABASE_URL") or "").strip()
    if not dsn:
        raise LsRealtimeError("TIMESCALE_DATABASE_URL 이 없다")

    iid_by_symbol, venue_rows, trading_days, _ = _fetch_reference(symbols)
    subs = build_subscriptions(venue_rows)
    print(f"  구독 {len(subs)}건 ({len(iid_by_symbol)}종목, 소켓 1개) {mode} {ws_url}",
          flush=True)

    client = LsRestClient()
    while not stop.is_set():
        remaining = (window.ends_at - datetime.now(KST)).total_seconds()
        if remaining <= 0:
            return
        repo = TimescaleMarketRepository(dsn)
        try:
            with MarketSink(repo) as sink:
                worker = LsRealtimeWorker(
                    subscriptions=subs,
                    token_provider=client.token,
                    ws_url=ws_url,
                    resolve_instrument=lambda s: iid_by_symbol.get(s),
                    sink=sink,
                    is_trading_day=make_trading_day_check(trading_days, stats=sink.stats),
                )
                run_task = asyncio.create_task(worker.run(max_seconds=remaining))
                stop_task = asyncio.create_task(stop.wait())
                done, pending = await asyncio.wait(
                    {run_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
                )
                for t in pending:
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass
                if run_task in done:
                    run_task.result()  # 재접속 소진 예외를 여기서 드러낸다
            # with 블록이 닫히며 Sink 꼬리가 나간다
            print(f"  세션 통계: {sink.stats.summary()}", flush=True)
            if stop.is_set():
                return
            # max_seconds 만료로 정상 반환 - 창도 끝났는지 위에서 재확인한다
        except (LsRealtimeError, Exception) as e:
            print(f"  ⚠ 수집 오류: {type(e).__name__}: {e} - "
                  f"{SESSION_RETRY_BACKOFF:.0f}초 후 재시작", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=SESSION_RETRY_BACKOFF)
                return
            except asyncio.TimeoutError:
                pass
        finally:
            repo.close()


async def main_async() -> int:
    env = load_project_env()
    pre = float(env.get("LS_PRE_OPEN_MINUTES") or 35)
    post = float(env.get("LS_POST_CLOSE_MINUTES") or 10)
    symbols = load_symbols(env)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows 호스트 직접 실행
            signal.signal(sig, lambda *_: stop.set())

    print(f"{SERVICE_VERSION}: {len(symbols)}종목, 수집창 = 개장-{pre:.0f}분 ~ "
          f"마감+{post:.0f}분", flush=True)

    while not stop.is_set():
        now = datetime.now(KST)
        try:
            _, _, _, session = _fetch_reference(symbols[:1])
        except LsRealtimeError:
            raise
        except Exception as e:
            print(f"⚠ 세션 판정용 Reference 조회 실패: {e} - "
                  f"{SESSION_RETRY_BACKOFF:.0f}초 후 재시도", flush=True)
            try:
                await asyncio.wait_for(stop.wait(), timeout=SESSION_RETRY_BACKOFF)
            except asyncio.TimeoutError:
                continue
            break
        window = resolve_window(now.date(), session,
                                pre_open_minutes=pre, post_close_minutes=post)
        print(f"[{now:%m-%d %H:%M}] {window.describe()}", flush=True)

        if not window.is_trading or now >= window.ends_at:
            # 다음 날 아침까지 대기 (휴장 연속이어도 아침마다 재판정)
            tomorrow = datetime.combine(now.date() + timedelta(days=1),
                                        time(6, 0), tzinfo=KST)
            delay = min((tomorrow - now).total_seconds(), IDLE_RECHECK_SECONDS)
        elif now < window.starts_at:
            delay = (window.starts_at - now).total_seconds()
            print(f"  개장 대기 {delay / 60:.0f}분", flush=True)
        else:
            await run_capture(window, symbols, stop)
            continue

        try:
            await asyncio.wait_for(stop.wait(), timeout=max(delay, 1.0))
        except asyncio.TimeoutError:
            pass

    print("종료", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 접속 없이
# ---------------------------------------------------------------------------

def _check_window():
    kst = lambda *a: datetime(*a, tzinfo=KST)  # noqa: E731
    # Calendar 가 오늘을 안다 - 그대로 따른다
    w = resolve_window(date(2026, 7, 31), (True, kst(2026, 7, 31, 9), kst(2026, 7, 31, 15, 30)),
                       pre_open_minutes=35, post_close_minutes=10)
    assert w.is_trading and w.calendar_verified
    assert w.starts_at == kst(2026, 7, 31, 8, 25) and w.ends_at == kst(2026, 7, 31, 15, 40)
    # Calendar 가 휴장이라 한다 - 소켓을 열지 않는다
    w = resolve_window(date(2026, 8, 17), (False, None, None),
                       pre_open_minutes=35, post_close_minutes=10)
    assert not w.is_trading
    # Calendar 미상 + 평일 - 거래일 간주하되 unverified 로 드러낸다
    w = resolve_window(date(2026, 7, 31), None, pre_open_minutes=35, post_close_minutes=10)
    assert w.is_trading and not w.calendar_verified
    # Calendar 미상 + 주말 - 비거래
    w = resolve_window(date(2026, 8, 1), None, pre_open_minutes=35, post_close_minutes=10)
    assert not w.is_trading
    print("  세션 창 판정             OK")


def _check_subscriptions():
    subs = build_subscriptions([("005930", "KOSPI"), ("196170", "KOSDAQ")])
    assert subs == [("S3_", "005930"), ("H1_", "005930"),
                    ("K3_", "196170"), ("HA_", "196170")], subs
    try:
        build_subscriptions([("000001", "NYSE")])
        raise AssertionError("모르는 venue 가 통과했다")
    except LsRealtimeError:
        pass
    try:
        build_subscriptions([(f"{i:06d}", "KOSPI") for i in range(101)])  # 202건
        raise AssertionError("소켓 한도 초과가 통과했다")
    except LsRealtimeError:
        pass
    print("  구독 생성/한도           OK")


def _check_symbols_precedence(tmp_path: Path):
    f = tmp_path / "wl.txt"
    f.write_text("# c\n005930\n196170  # 알테오젠\n", encoding="utf-8")
    assert load_symbols({"LS_WATCH_SYMBOLS_FILE": str(f)}) == ("005930", "196170")
    assert load_symbols({"LS_WATCH_SYMBOLS": "000660",
                         "LS_WATCH_SYMBOLS_FILE": str(f)}) == ("000660",)
    try:
        load_symbols({"LS_WATCH_SYMBOLS_FILE": str(tmp_path / "없음.txt")})
        raise AssertionError("없는 파일이 통과했다")
    except LsRealtimeError:
        pass
    print("  Watchlist 로드           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--check" in sys.argv:
        import tempfile

        print(f"{SERVICE_VERSION} 자체 점검 (접속 없음)")
        _check_window()
        _check_subscriptions()
        with tempfile.TemporaryDirectory() as td:
            _check_symbols_precedence(Path(td))
        print("상주 시세 서비스 3개 영역 통과. 상주 실행은 인자 없이")
        raise SystemExit(0)

    raise SystemExit(asyncio.run(main_async()))
