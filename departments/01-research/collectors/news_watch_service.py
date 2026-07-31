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
  NEWS_WATCH_SYMBOLS            쉼표 구분 KRX 종목코드. 비우면 공시건수 상위
                                NEWS_WATCH_TOP 종목 (⚠ 증권사로 쏠린다 - 명시 권장)
  NEWS_WATCH_TOP                기본 20
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))

from naver_news_collector import (  # noqa: E402
    DAILY_QUOTA,
    SOURCE_ID,
    NaverNewsClient,
    NaverNewsError,
    load_watchlist,
    make_watch_stream,
)
from news_events import StreamCursor  # noqa: E402
from news_pipeline import NewsSink, krx_symbol_resolver  # noqa: E402
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

    @property
    def watch_count(self) -> int:
        """감시 종목 수의 상한. top 모드는 top 개 이하가 나온다."""
        return len(self.symbols) if self.symbols else self.top


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
    return WatchConfig(
        symbols=symbols,
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

    items = load_watchlist(ref, top=cfg.top, symbols=cfg.symbols)
    print(
        f"{SERVICE_VERSION}: {len(items)}종목 / {cfg.interval_seconds:.0f}초 간격 / "
        f"예상 {projected:,}회/일 (한도 {DAILY_QUOTA:,})",
        flush=True,
    )
    preview = ", ".join(f"{it.name}({it.symbol})" for it in items[:10])
    print(f"  감시: {preview}{' ...' if len(items) > 10 else ''}", flush=True)

    client = NaverNewsClient()
    stream = make_watch_stream(
        client, items, display=cfg.display,
        interval_seconds=cfg.interval_seconds, cursor=StreamCursor(),
    )
    resolver = krx_symbol_resolver(
        {it.symbol: it.instrument_id for it in items},
        dedicated_names={it.symbol: it.name for it in items},
    )
    sink = NewsSink(
        ref, source_id=source_id, link_resolver=resolver,
        max_batch=cfg.max_batch, max_delay_seconds=cfg.max_delay_seconds,
    )

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
    print("  설정 파싱                OK")


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
        print("상주 서비스 4개 영역 통과. 상주 실행은 인자 없이")
        raise SystemExit(0)

    raise SystemExit(main())
