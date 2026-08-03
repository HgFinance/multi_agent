#!/usr/bin/env python3
"""상주형 NAVER 뉴스 수집 서비스 - Docker Container 의 entrypoint.

담당: 재일 (리서치/퀀트)
근거: 재일님 지시(2026-07-31) "수집기들 Docker 컨테이너로 띄워 DB 즉시 적재" +
      "기사 게시 시각·적재 시각을 DB 에서 확인할 수 있어야 함"
      TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md Sprint J3 (뉴스 Stream 계약)

구조
    NaverNewsClient -> make_watch_stream (한 sweep = Watchlist 1바퀴)
                    -> NewsSink (즉시 적재, 실패 시 버퍼 유지)

  **sweep 간 간격은 이 서비스가 소유한다.** stream.run(max_seconds=0.1) 은 내부
  sleep 직전에 반환하므로 정확히 한 sweep 만 돌고 돌아온다. 간격 대기는
  stop_event.wait() 라 SIGTERM 이 대기를 즉시 깨우고, 종료 경로에서 Sink.close()
  가 버퍼 꼬리를 밀어 넣는다.

  적재 지연은 코드가 아니라 DB 가 증언한다 - research.news_ingest_latency(_hourly)
  가 published_at(게시) / observed_at(관측) / created_at(적재) 차이를 보여준다.

장애 처리 (한 경로다)
  NAVER 오류든 DB 오류든 run_loop 예외로 올라오고, 바깥에서 지수 백오프 후
  Repository 를 재접속해 NewsSink.rebind() 로 갈아끼운다. **Sink 를 새로 만들지
  않는다** - 만들면 flush 실패로 남아 있던 버퍼를 잃는다.

환경변수 (전부 선택, .env 보다 프로세스 환경변수가 우선)
  NEWS_WATCH_SYMBOLS            쉼표 구분 KRX 종목코드 (최우선)
  NEWS_WATCH_SYMBOLS_FILE       watchlist 파일 경로 (watchlist_builder.py 가 생성 -
                                시가총액 상위). 상대 경로는 실행 위치(부서 루트) 기준.
                                지정했는데 파일이 없거나 비어 있으면 **기동 거부**한다
                                - 조용히 다른 목록으로 대체하지 않는다.
  NEWS_WATCH_TOP                위 둘 다 없을 때 공시건수 상위 N, 기본 20
                                (⚠ 증권사로 쏠린다 - 위 두 방법 권장)
  NEWS_WATCH_DISPLAY            페이지당 기사 수, 기본 20
  NEWS_POLL_INTERVAL_SECONDS    sweep 간격, 기본 120 (일 한도 검산과 묶여 있다)
  NEWS_SINK_MAX_BATCH           기본 20
  NEWS_SINK_MAX_DELAY_SECONDS   기본 5.0

실행
  python collectors/news_watch_service.py            # 상주 실행
  python collectors/news_watch_service.py --check    # 자체 점검 (호출·DB 없음)
"""
from __future__ import annotations

import signal
import sys
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from naver_news_collector import (  # noqa: E402
    DAILY_QUOTA,
    DEDUP_WINDOW,
    SOURCE_ID,
    NaverNewsClient,
    NaverNewsError,
    load_watchlist,
    make_watch_stream,
)
from news_events import StreamCursor  # noqa: E402
from news_watch_tiers import plan_tiers, sweep_symbols  # noqa: E402
from news_pipeline import (  # noqa: E402
    NewsSink,
    krx_symbol_resolver,
    seed_title_window_from_db,
)
from source_registry import load_project_env  # noqa: E402

SERVICE_VERSION = "research-news-watch-service-v1"
KST = timezone(timedelta(hours=9))

# 일 한도의 90% 를 넘기지 않는다. 초당 버스트 제한은 Client 의 RateLimiter 가,
# 일 한도는 이 서비스가 지킨다 - 한도 초과로 밤에 조용히 죽는 것보다 낫다.
QUOTA_SOFT_RATIO = 0.9
QUOTA_PAUSE_SECONDS = 600.0


# ---------------------------------------------------------------------------
# 설정
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class WatchConfig:
    symbols: tuple[str, ...]
    top: int
    display: int
    interval_seconds: float
    max_batch: int
    max_delay_seconds: float
    tier2_symbols: tuple[str, ...] = ()

    @property
    def watch_count(self) -> int:
        """감시 종목 수의 상한. top 모드는 top 개 이하가 나온다."""
        return len(self.symbols) if self.symbols else self.top


def parse_watchlist_file(text: str) -> tuple[str, ...]:
    """news_watchlist.txt -> 종목코드 튜플. 한 줄에 하나, # 뒤는 주석이다.

    생성은 watchlist_builder.py 가 한다(LS t1444 시가총액 상위). 파서가 여기 있는
    이유: builder 가 이 모듈의 한도 검산을 import 하므로 역방향 import 를 만들지
    않기 위해서다.
    """
    symbols = []
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if body:
            symbols.append(body)
    return tuple(symbols)


def parse_config(env: dict) -> WatchConfig:
    def _num(key: str, default, cast, minimum):
        raw = (env.get(key) or "").strip()
        if not raw:
            return default
        try:
            value = cast(raw)
        except ValueError as e:
            raise NaverNewsError(f"{key} 가 숫자가 아니다: {raw!r}") from e
        if value < minimum:
            raise NaverNewsError(f"{key} 는 {minimum} 이상이어야 한다: {value}")
        return value

    symbols = tuple(
        s.strip() for s in (env.get("NEWS_WATCH_SYMBOLS") or "").split(",") if s.strip()
    )
    symbols_file = (env.get("NEWS_WATCH_SYMBOLS_FILE") or "").strip()
    if not symbols and symbols_file:
        path = Path(symbols_file)
        if not path.exists():
            raise NaverNewsError(f"NEWS_WATCH_SYMBOLS_FILE 이 없다: {path}")
        symbols = parse_watchlist_file(path.read_text(encoding="utf-8"))
        if not symbols:
            raise NaverNewsError(f"NEWS_WATCH_SYMBOLS_FILE 이 비어 있다: {path}")
    # ▶ 2계층 (2026-08-03). 이 파일이 지정될 때만 켜진다 - 없으면 기존 동작이다.
    tier2 = ()
    tier2_file = (env.get("NEWS_TIER2_SYMBOLS_FILE") or "").strip()
    if tier2_file:
        tpath = Path(tier2_file)
        if not tpath.exists():
            raise NaverNewsError(f"NEWS_TIER2_SYMBOLS_FILE 이 없다: {tpath}")
        tier2 = parse_watchlist_file(tpath.read_text(encoding="utf-8"))
        if not tier2:
            raise NaverNewsError(f"NEWS_TIER2_SYMBOLS_FILE 이 비어 있다: {tpath}")
        if not symbols:
            raise NaverNewsError(
                "2계층은 Tier1(핵심 바스켓) 없이 성립하지 않는다 - "
                "NEWS_WATCH_SYMBOLS_FILE 을 함께 지정해야 한다")

    return WatchConfig(
        symbols=symbols,
        tier2_symbols=tier2,
        top=_num("NEWS_WATCH_TOP", 20, int, 1),
        display=_num("NEWS_WATCH_DISPLAY", 20, int, 1),
        # 간격 5초 미만은 거부한다 - RateLimiter 가 있어도 sweep 자체가 겹친다
        interval_seconds=_num("NEWS_POLL_INTERVAL_SECONDS", 120.0, float, 5.0),
        max_batch=_num("NEWS_SINK_MAX_BATCH", 20, int, 1),
        max_delay_seconds=_num("NEWS_SINK_MAX_DELAY_SECONDS", 5.0, float, 0.5),
    )


def ensure_quota_headroom(
    n_symbols: int, interval_seconds: float, *, quota: int = DAILY_QUOTA
) -> int:
    """이 설정이 켜자마자 일 한도로 달려가는지 시작 전에 검산한다.

    한 sweep = 종목 수만큼 호출(종목당 1페이지). 반환은 예상 일 호출 수.
    """
    projected = int(86400.0 / interval_seconds) * n_symbols
    ceiling = int(quota * QUOTA_SOFT_RATIO)
    if projected > ceiling:
        raise NaverNewsError(
            f"예상 {projected:,}회/일 > 한도의 {QUOTA_SOFT_RATIO:.0%}({ceiling:,}) - "
            f"NEWS_POLL_INTERVAL_SECONDS 를 늘리거나 종목을 줄여야 한다"
        )
    return projected


# ---------------------------------------------------------------------------
# 수집 루프
# ---------------------------------------------------------------------------

class DailyQuotaTracker:
    """오늘 몇 회 호출했는지를 프로세스 수명 동안 추적한다.

    run_loop 안에서 기준을 잡으면 안 된다 - 오류로 루프를 재진입할 때마다
    사용량이 0 으로 리셋되어, 장애가 반복되는 날일수록 한도 Guard 가 무력해진다
    (자체 점검이 실제로 잡아낸 버그다). 기준은 여기, 루프 밖에 산다.
    """

    def __init__(self, client, *, today=None) -> None:
        self._client = client
        self._today = today or (lambda: datetime.now(KST).date())
        self._day = self._today()
        self._base = client.calls

    def used_today(self) -> int:
        day = self._today()
        if day != self._day:  # NAVER 일 한도는 날짜별 - 날이 바뀌면 기준 리셋
            self._day, self._base = day, self._client.calls
        return self._client.calls - self._base


def run_loop(
    stream,
    sink,
    *,
    interval_seconds: float,
    stop: threading.Event,
    quota_state: DailyQuotaTracker,
    quota: int = DAILY_QUOTA,
    pause_seconds: float = QUOTA_PAUSE_SECONDS,
    log=print,
) -> tuple[int, int]:
    """sweep 를 반복한다. 예외는 올린다 - 호출부가 재접속 후 다시 부른다.

    반환 (sweeps, emitted). stop 이 서면 간격 대기 중이라도 즉시 나온다.
    """
    sweeps = emitted = 0

    while not stop.is_set():
        used_today = quota_state.used_today()
        if used_today >= quota * QUOTA_SOFT_RATIO:
            log(
                f"⚠ NAVER 일 한도 {QUOTA_SOFT_RATIO:.0%} 도달"
                f" ({used_today:,}/{quota:,}) - {pause_seconds:.0f}초 감시 정지"
            )
            stop.wait(pause_seconds)
            continue

        stats = stream.run(on_record=sink, max_seconds=0.1)  # 정확히 한 sweep
        sink.tick()  # 기사가 안 와도 시간 Flush
        sweeps += 1
        emitted += stats.emitted

        if stats.emitted:
            log(f"[{datetime.now(KST):%H:%M:%S}] sweep {sweeps}:"
                f" +{stats.emitted}건 | {sink.stats.summary()}")
        elif sweeps % 30 == 0:
            log(f"[{datetime.now(KST):%H:%M:%S}] sweep {sweeps}:"
                f" 신규 없음 | 오늘 호출 {used_today:,} | {sink.stats.summary()}")

        stop.wait(interval_seconds)

    return sweeps, emitted


# ---------------------------------------------------------------------------
# entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    cfg = parse_config(load_project_env())
    projected = ensure_quota_headroom(cfg.watch_count, cfg.interval_seconds)

    stop = threading.Event()

    def _handle(signum, _frame):
        print(f"신호 {signum} 수신 - 꼬리 Flush 후 종료한다", flush=True)
        stop.set()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)

    from reference_repository import SupabaseReferenceRepository

    ref = SupabaseReferenceRepository()
    _, _, id_by_source = ref.sync_data_sources()
    source_id = id_by_source.get(SOURCE_ID)
    if source_id is None:
        raise NaverNewsError(f"data_sources 에 {SOURCE_ID} 가 없다")

    # ▶ 2계층 (2026-08-03) - NEWS_TIER2_SYMBOLS_FILE 이 있을 때만 켜진다.
    #   없으면 기존 동작 그대로다(설정 안 하면 아무것도 안 바뀐다).
    #   전종목 2,595를 같은 빈도로 돌면 한도의 6.6배라 간격이 185분이 된다 -
    #   속보성이 사라진다. 핵심은 짧게 유지하고 나머지는 순회로 훑는다.
    tier_plan = None
    if cfg.tier2_symbols:
        # plan_tiers 가 한도를 넘으면 **예외를 낸다** - 조용히 줄이지 않는다
        tier_plan = plan_tiers(list(cfg.symbols), list(cfg.tier2_symbols),
                               core_interval_seconds=cfg.interval_seconds)
        print(f"{SERVICE_VERSION}: 2계층 - {tier_plan['note']}", flush=True)
        print(f"  예상 {tier_plan['projected_daily_calls']:,}회/일 "
              f"(상한 {tier_plan['quota_ceiling']:,}, 여유 {tier_plan['headroom']:,})",
              flush=True)

    items = load_watchlist(ref, top=cfg.top, symbols=cfg.symbols)   # Tier1
    sweep_fn = None
    if tier_plan is not None:
        rest = load_watchlist(ref, symbols=tuple(tier_plan["tier2"]))
        # ▶ 계획을 **해석된 종목**으로 다시 세운다. reference 에 없는 종목은
        #   load_watchlist 가 조용히 떨어뜨리므로, 처음 계획의 순회 주기는 실제와
        #   다르다. 보고하는 숫자가 실제와 달라지는 것을 허용하지 않는다.
        tier_plan = plan_tiers([it.symbol for it in items],
                               [it.symbol for it in items] + [it.symbol for it in rest],
                               core_interval_seconds=cfg.interval_seconds)
        by_sym = {it.symbol: it for it in list(items) + list(rest)}

        def sweep_fn(i, _p=tier_plan, _m=by_sym):
            return [_m[s] for s in sweep_symbols(_p, i) if s in _m]

        sweep_size = len(tier_plan["tier1"]) + tier_plan["tier2_per_sweep"]
        print(f"  해석됨 Tier1 {len(tier_plan['tier1'])} / Tier2 "
              f"{len(tier_plan['tier2'])} / sweep {sweep_size}종목 / "
              f"Tier2 한바퀴 {tier_plan['tier2_cycle_hours']}시간", flush=True)
        # 링크 해석과 dedup 창은 **전체** 를 기준으로 만든다 - Tier2 종목이 걸린
        # 기사를 종목에 못 붙이면 순회가 무의미하다.
        items = list(items) + list(rest)
        projected = tier_plan["projected_daily_calls"]
    print(
        f"{SERVICE_VERSION}: {len(items)}종목 / {cfg.interval_seconds:.0f}초 간격 / "
        f"예상 {projected:,}회/일 (한도 {DAILY_QUOTA:,})",
        flush=True,
    )
    preview = ", ".join(f"{it.name}({it.symbol})" for it in items[:10])
    print(f"  감시: {preview}{' ...' if len(items) > 10 else ''}", flush=True)

    client = NaverNewsClient()
    # cursor 는 make_watch_stream 이 sweep 규모에 맞게 만든다 - 여기서 기본
    # StreamCursor() 를 넘기면 창 2,000짜리가 바스켓 sweep(최대 7,000)에 밀려
    # 매번 재방출된다 (news_events.StreamCursor.sized 주석).
    stream = make_watch_stream(
        client, items, display=cfg.display,
        interval_seconds=cfg.interval_seconds,
        sweep_items=sweep_fn,
        # 2계층에서는 창을 **한 sweep** 기준으로 잡는다. items 전체(2,595)로 잡으면
        # 창이 15만 건이 되고, 정작 Tier2 는 한 바퀴 뒤에 와서 어차피 창 밖이다.
        cursor=(StreamCursor.sized(
            max(DEDUP_WINDOW,
                (len(tier_plan["tier1"]) + tier_plan["tier2_per_sweep"])
                * cfg.display * 2))
            if tier_plan is not None else None),
    )
    # '더 긴 이름' 오탐 검사는 감시 목록이 아니라 전 상장사 이름으로 한다 -
    # 제목의 '두산로보틱스' 가 감시 밖이어도 '두산' DEDICATED 를 막아야 한다.
    with ref._conn.cursor() as cur:
        cur.execute(
            "select display_name from reference.instruments "
            "where market = 'KRX' and instrument_type = 'STOCK' and status = 'ACTIVE'"
        )
        known_names = {r[0] for r in cur.fetchall()}
    resolver = krx_symbol_resolver(
        {it.symbol: it.instrument_id for it in items},
        dedicated_names={it.symbol: it.name for it in items},
        known_names=known_names,
    )
    sink = NewsSink(
        ref, source_id=source_id, link_resolver=resolver,
        max_batch=cfg.max_batch, max_delay_seconds=cfg.max_delay_seconds,
        title_dedup_window=5000,  # 같은 기사 다른 URL 재게재 차단 (중복 270행 재발 방지)
    )
    # 재기동 예열 - 창이 빈 첫 몇 분이 중복 구멍이 되지 않게 (07-31 재배포 때 42행)
    seeded = seed_title_window_from_db(sink, ref._conn, source_id)
    print(f"제목 창 예열 {seeded:,}건", flush=True)

    quota_state = DailyQuotaTracker(client)
    backoff = 5.0
    try:
        while not stop.is_set():
            try:
                run_loop(
                    stream, sink,
                    interval_seconds=cfg.interval_seconds, stop=stop,
                    quota_state=quota_state,
                )
            except Exception as e:
                # NAVER 오류든 DB 오류든 같은 경로다. DB 재접속이 NAVER 오류에는
                # 불필요하지만 무해하고, 경로가 하나면 놓치는 조합이 없다.
                print(
                    f"⚠ 수집 루프 오류: {type(e).__name__}: {e} - "
                    f"{backoff:.0f}초 후 재접속", flush=True,
                )
                # ▶ 스택을 함께 남긴다 (2026-08-03). 메시지만 찍었더니 DB
                #   SyntaxError 가 **어느 문장에서** 났는지 알 수 없어 재현에
                #   시간을 썼다. 재시도 루프는 원인을 삼키기 가장 쉬운 자리다.
                traceback.print_exc()
                if stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 300.0)
                try:
                    ref.close()
                except Exception:
                    pass
                ref = SupabaseReferenceRepository()
                sink.rebind(ref)  # 버퍼·통계 유지 - flush 실패분이 재시도된다
            else:
                backoff = 5.0
    finally:
        try:
            sink.close()  # 꼬리를 밀어 넣는다
        except Exception as e:
            # 종료 Flush 까지 실패하면 몇 건을 잃는지 숨기지 않는다
            print(f"⚠ 종료 Flush 실패 - 버퍼 {sink.pending}건 유실: {e}", flush=True)
        try:
            ref.close()
        except Exception:
            pass

    print(f"종료: {sink.stats.summary()}", flush=True)
    return 0


# ---------------------------------------------------------------------------
# 자체 점검 - 호출·DB 없이
# ---------------------------------------------------------------------------

class _FakeStream:
    """run() 계약만 흉내낸다. pages 가 소진되면 stop 을 세운다."""

    def __init__(self, pages, stop: threading.Event) -> None:
        self._pages = list(pages)
        self._stop = stop
        self.runs = 0

    def run(self, *, on_record, max_seconds=None, max_records=None):
        from types import SimpleNamespace

        self.runs += 1
        page = self._pages.pop(0) if self._pages else []
        for r in page:
            on_record(r)
        if not self._pages:
            self._stop.set()
        return SimpleNamespace(emitted=len(page))


class _FakeSink:
    def __init__(self) -> None:
        self.got = []
        self.ticks = 0
        from news_pipeline import SinkStats

        self.stats = SinkStats()

    def __call__(self, record) -> None:
        self.got.append(record)

    def tick(self) -> None:
        self.ticks += 1


class _FakeClient:
    def __init__(self, calls: int = 0) -> None:
        self.calls = calls


def _check_config():
    cfg = parse_config({})
    assert cfg.symbols == () and cfg.top == 20 and cfg.interval_seconds == 120.0
    assert cfg.watch_count == 20

    cfg = parse_config({
        "NEWS_WATCH_SYMBOLS": " 005930 , 000660 ",
        "NEWS_POLL_INTERVAL_SECONDS": "60",
    })
    assert cfg.symbols == ("005930", "000660") and cfg.watch_count == 2
    assert cfg.interval_seconds == 60.0

    for bad in ({"NEWS_WATCH_TOP": "abc"}, {"NEWS_POLL_INTERVAL_SECONDS": "1"},
                {"NEWS_SINK_MAX_BATCH": "0"}):
        try:
            parse_config(bad)
            raise AssertionError(f"{bad} 가 통과했다")
        except NaverNewsError:
            pass

    # watchlist 파일 경로 - 있으면 읽고, 명시 SYMBOLS 가 이기고, 없거나 비면 거부
    import os
    import tempfile

    fd, path = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("# 주석\n005930  # 삼성전자\n000660\n")
        cfg = parse_config({"NEWS_WATCH_SYMBOLS_FILE": path})
        assert cfg.symbols == ("005930", "000660"), cfg.symbols
        cfg = parse_config({"NEWS_WATCH_SYMBOLS_FILE": path,
                            "NEWS_WATCH_SYMBOLS": "017670"})
        assert cfg.symbols == ("017670",), "명시 SYMBOLS 가 파일에 졌다"
    finally:
        os.unlink(path)
    for bad_env in ({"NEWS_WATCH_SYMBOLS_FILE": path},):  # 방금 지운 경로
        try:
            parse_config(bad_env)
            raise AssertionError("없는 watchlist 파일이 통과했다")
        except NaverNewsError:
            pass
    print("  설정 파싱                OK")


def _check_tier2_config():
    """2계층 설정 - 켜지는 조건과 거부 조건."""
    import os
    import tempfile

    fd, core_path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(("005930", "000660")))
    fd, full_path = tempfile.mkstemp(suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(("005930", "000660", "035720")))
    try:
        # 지정 안 하면 꺼진 상태 - 기존 동작과 같다
        cfg = parse_config({"NEWS_WATCH_SYMBOLS_FILE": core_path})
        assert cfg.tier2_symbols == (), "설정하지 않았는데 2계층이 켜졌다"

        cfg = parse_config({"NEWS_WATCH_SYMBOLS_FILE": core_path,
                            "NEWS_TIER2_SYMBOLS_FILE": full_path})
        assert cfg.tier2_symbols == ("005930", "000660", "035720"), cfg.tier2_symbols

        # Tier1 없이 Tier2 만 지정하면 거부 - '전종목을 다 본다'는 착시가 된다
        try:
            parse_config({"NEWS_TIER2_SYMBOLS_FILE": full_path})
            raise AssertionError("Tier1 없는 2계층이 통과했다")
        except NaverNewsError:
            pass

        # 계획이 실제 sweep 대상을 만들어내는가 (Tier1 은 매번, Tier2 는 순회)
        plan = plan_tiers(list(cfg.symbols), list(cfg.tier2_symbols),
                          core_interval_seconds=cfg.interval_seconds)
        first = sweep_symbols(plan, 0)
        assert set(cfg.symbols) <= set(first), first
        assert "035720" in first, "Tier2 가 첫 sweep 에 안 들어왔다"
    finally:
        os.unlink(core_path)
        os.unlink(full_path)
    # 없는 파일은 조용히 넘어가지 않는다
    try:
        parse_config({"NEWS_WATCH_SYMBOLS_FILE": core_path,
                      "NEWS_TIER2_SYMBOLS_FILE": full_path})
        raise AssertionError("없는 Tier2 파일이 통과했다")
    except NaverNewsError:
        pass
    print("  2계층 설정               OK")


def _check_quota_math():
    # 20종목 120초 = 14,400회/일 < 22,500 - 통과
    assert ensure_quota_headroom(20, 120.0) == 14_400
    # 20종목 60초 = 28,800회/일 > 22,500 - 시작 전에 거부해야 한다
    try:
        ensure_quota_headroom(20, 60.0)
        raise AssertionError("한도 초과 설정이 통과했다")
    except NaverNewsError:
        pass
    print("  일 한도 사전 검산        OK")


def _check_loop_sweeps():
    """sweep 마다 Sink 로 넘기고, stream 이 비면 stop 을 존중하는지."""
    stop = threading.Event()
    stream = _FakeStream([["r1", "r2"], [], ["r3"]], stop)
    sink = _FakeSink()
    tracker = DailyQuotaTracker(_FakeClient(), today=lambda: datetime(2026, 7, 31).date())
    sweeps, emitted = run_loop(
        stream, sink, interval_seconds=0.01, stop=stop,
        quota_state=tracker, log=lambda *_: None,
    )
    assert sweeps == 3 and emitted == 3, (sweeps, emitted)
    assert sink.got == ["r1", "r2", "r3"]
    assert sink.ticks == 3, "sweep 마다 tick 이 불려야 시간 Flush 가 산다"
    print("  루프/정지                OK")


def _check_quota_guard():
    """한도 도달 시 정지, 재진입에도 기준 유지, 날짜 변경 시 리셋."""
    client = _FakeClient()
    seq = {"d": datetime(2026, 7, 31).date()}
    tracker = DailyQuotaTracker(client, today=lambda: seq["d"])
    client.calls = 22_500  # tracker 생성 *후* 사용된 호출 - 25,000 의 90%

    stop = threading.Event()
    stream = _FakeStream([["x"]], stop)
    warned = []

    def log(msg):
        warned.append(msg)
        stop.set()  # 첫 경고에서 끝낸다 - 점검이 pause 를 기다리지 않게

    run_loop(stream, _FakeSink(), interval_seconds=0.01, stop=stop,
             quota_state=tracker, pause_seconds=0.01, log=log)
    assert stream.runs == 0, "한도에 닿았는데 sweep 을 돌았다"
    assert warned and "한도" in warned[0]

    # **재접속 재진입에도 기준이 남아야 한다** - run_loop 를 새로 불러도
    # tracker 가 살아 있으므로 여전히 한도다 (루프 안에서 기준을 잡던 버그 회귀)
    stop2 = threading.Event()
    stream2 = _FakeStream([["y"]], stop2)
    warned2 = []

    def log2(msg):
        warned2.append(msg)
        stop2.set()

    run_loop(stream2, _FakeSink(), interval_seconds=0.01, stop=stop2,
             quota_state=tracker, pause_seconds=0.01, log=log2)
    assert stream2.runs == 0, "재진입에서 일 사용량이 리셋됐다 - Guard 무력화"
    assert warned2

    # 날이 바뀌면 오늘 사용량은 0 부터다
    seq["d"] = datetime(2026, 8, 1).date()
    stop3 = threading.Event()
    stream3 = _FakeStream([["z"]], stop3)
    run_loop(stream3, _FakeSink(), interval_seconds=0.01, stop=stop3,
             quota_state=tracker, log=lambda *_: None)
    assert stream3.runs == 1, "날이 바뀌었는데 한도 기준이 리셋되지 않았다"
    print("  일 한도 Guard/리셋       OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--check" in sys.argv:
        print(f"{SERVICE_VERSION} 자체 점검 (호출·DB 없음)")
        _check_config()
        _check_quota_math()
        _check_loop_sweeps()
        _check_quota_guard()
        _check_tier2_config()
        print("상주 서비스 5개 영역 통과. 상주 실행은 인자 없이")
        raise SystemExit(0)

    raise SystemExit(main())
