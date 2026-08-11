#!/usr/bin/env python3
"""F03: LS WebSocket 실시간 수집 Worker - 체결·호가를 받아 TimescaleDB 로 넣는다.

소유: 재일 (리서치본부)
근거: docs/06-integrations/ls-openapi/03-stock/16-9a2800c3.md (S3_/K3_/H1_/HA_)
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F03
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1, 4.2, 8.2
      departments/01-research/collectors/ls_realtime_adapter.py (정규화)
      departments/01-research/collectors/subscription_plan.py (무엇을 구독할지)

▶ 실측으로 확인한 것 (2026-07-31 09:38 KST, 장중, 모의 Domain)
  - **WebSocket 경로는 `/websocket` 하나뿐이다.** 문서 README 의
    `/websocket/stock`, `/websocket/indtp`, `/websocket/futureoption` 은 REST 표기
    관례를 따른 것이고 실제 경로가 아니다. 그 경로로 붙으면 TLS 는 되는데
    **업그레이드에서 타임아웃** 이라 인증 문제로 오진하기 쉽다.
  - 같은 소켓에서 S3_(코스피 체결), K3_(코스닥 체결), H1_(호가), BM_(업종)이
    전부 rsp_cd=00000 으로 등록됐다. 카테고리는 경로가 아니라 tr_cd 가 가른다.
  - 구독 요청은 {"header":{"token","tr_type"},"body":{"tr_cd","tr_key"}} 이고
    tr_type=3 이 등록, 4 가 해제다.
  - 등록 직후 header 에 rsp_cd/rsp_msg 가 담긴 ack 가 오고, 그 뒤부터 body 에
    실시간 데이터가 온다. **ack 와 데이터가 같은 tr_cd 로 오므로 rsp_cd 유무로
    구분한다.**
  - 삼성전자 25초에 체결 18건 + 호가 12건.

▶ 설계에서 조심한 것
  1. **정규화 실패로 스트림을 멈추지 않는다.** normalize() 가 QuarantinedEvent 를
     돌려주면 세어서 보고하고 계속 받는다. 대신 조용히 버리지 않는다.
  2. **버퍼에 남은 것을 잃지 않는다.** 종료·예외 어느 경로로든 Flush 한다.
  3. **재접속하면 반드시 재구독한다.** 소켓만 다시 열고 구독을 안 하면 조용히
     아무것도 안 받는 상태가 된다 - 그게 제일 위험하다.
  4. **적재 실패를 삼키지 않는다.** 예외를 올린다(개발 원칙 9).

자체 점검(접속 없음): python departments/01-research/collectors/ls_realtime_worker.py
실제 수집:            python departments/01-research/collectors/ls_realtime_worker.py --run [--seconds N] [--symbols 005930,000660]
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "repository"))
# ▶ `contracts.market_events` 로 임포트한다. ls_realtime_adapter 가 그렇게 하기
#   때문이다. `market_events` 로 받으면 **같은 파일이 두 모듈로 로드되어 클래스가
#   갈리고 isinstance 가 전부 False 가 된다.** 정규화 결과를 타입으로 분기하는
#   이 파일에서는 조용히 모든 이벤트가 버려지는 형태로 터진다.
from contracts.market_events import (
    MarketQuote,
    MarketTick,
    QuarantinedEvent,
)
from ls_realtime_adapter import normalize
from source_registry import SourceRegistry, load_project_env
from subscription_plan import WEBSOCKET_PATH
from typing_extensions import Self

WORKER_VERSION = "research-ls-realtime-worker-v1"
SOURCE_ID = "ls_openapi_ws"

# 통합시세 TR - `tr_key` 규격이 다르다(U + 6자리 + 공백 3개). 어댑터의
# UNIFIED_TRS 와 같은 목록이지만, 수집기는 어댑터를 import 하지 않으므로
# 여기 둔다. 한쪽만 늘리면 구독은 되고 정규화가 안 되거나 그 반대가 된다.
UNIFIED_TR_KEYS = frozenset({"US3", "UH1"})

# 시세 수집 환경. **주문 환경(LS_ENV)과 분리한다** (재일님 결정 2026-08-12).
MARKET_ENV_VAR = "LS_MARKET_ENV"
MARKET_ENV_DEFAULT = "LIVE"


def market_env(env: dict) -> str:
    """시세를 어디서 받을지. `LS_ENV`(주문)와 **다른 스위치**다.

    ▶ 왜 갈랐나 (2026-08-12 실측)
      하나의 `LS_ENV` 가 주문과 시세를 같이 정하고 있었다. 주문을 PAPER 로
      두려고 `LS_ENV=PAPER` 를 쓰면 **시세까지 모의 서버로 붙는다.** 실제로
      그날 아침 수집기가 `:29443`(PAPER)에 붙어 프리마켓 한 시간 동안 체결이
      0건이었다. 그 위에서 찾은 알파는 검증할 수 없는 알파다.

      시세 구독은 주문 권한과 무관하다 - LIVE 키로 시세를 받아도 주문이
      나가지 않는다. derivatives_collector 가 이미 같은 이유로 `LS_ENV` 를
      무시하고 LIVE 를 강제하고 있었는데, 그 예외를 이름 있는 스위치로
      일반화한 것이다.

    ▶ 기본값이 LIVE 인 이유 (안전한 실패 방향)
      주문은 PAPER 로 떨어지는 것이 안전하지만, 시세는 반대다. 모의 시세는
      **진짜처럼 생긴 가짜**라 조용히 흘러가면 백테스트가 그걸 사실로 읽는다.
      못 받는 것은 티가 나고, 잘못 받는 것은 티가 안 난다.
    """
    return (env.get(MARKET_ENV_VAR) or MARKET_ENV_DEFAULT).strip().upper()


def market_rest_client(env: dict):
    """시세용 REST 클라이언트(토큰 발급). **소켓과 같은 환경을 쓴다.**

    토큰은 `LS_ENV`(주문)로 받고 소켓만 `LS_MARKET_ENV` 로 붙으면, 모의 토큰으로
    실전 소켓에 접속하는 꼴이 된다 - 붙긴 붙고 데이터만 안 온다. 두 곳이 같은
    스위치를 보게 여기서 한 번에 만든다.
    """
    from ls_client import LsEnvironment, LsRestClient  # noqa: PLC0415

    return LsRestClient(LsEnvironment.from_env({**env, "LS_ENV": market_env(env)}))

KST = timezone(timedelta(hours=9))

# 구독 등록/해제. 문서는 "거래 Type" String(1) 이라고만 적어놨고 값이 없어서
# 실측으로 확인했다(3 으로 보내면 rsp_cd=00000).
TR_TYPE_SUBSCRIBE = "3"
TR_TYPE_UNSUBSCRIBE = "4"

# 적재 배치. 체결은 초당 수십 건까지 오므로 건건이 넣으면 DB 왕복이 병목이다.
DEFAULT_MAX_BATCH = 200
DEFAULT_MAX_DELAY_SECONDS = 2.0

# 재접속. 무한정 재시도하지 않는다 - 계속 실패하면 사람이 봐야 한다.
MAX_RECONNECTS = 5
RECONNECT_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 20.0)


class LsRealtimeError(RuntimeError):
    """실시간 수집 실패. 빈 결과로 바꾸지 않는다."""


@dataclass
class WorkerStats:
    """무엇을 받고 무엇을 넣었는지. 축소를 숨기지 않는다(가이드 8.2)."""

    acks: int = 0
    ack_failures: int = 0
    messages: int = 0
    ticks: int = 0
    quotes: int = 0
    quarantined: int = 0
    written_ticks: int = 0
    written_quotes: int = 0
    duplicates: int = 0
    flushes: int = 0
    reconnects: int = 0
    quarantine_reasons: dict = field(default_factory=dict)
    unknown_tr: dict = field(default_factory=dict)
    # 재접속 사유. 조용히 다시 붙기만 하면 왜 끊겼는지 영영 모른다.
    disconnect_reasons: dict = field(default_factory=dict)
    # 관찰용 콜백에서 난 예외. 수집은 계속하되 숨기지 않는다.
    observer_errors: dict = field(default_factory=dict)
    # Calendar 가 아직 안 채운 날짜. 거래일로 간주했다는 사실을 드러낸다.
    calendar_unverified: set = field(default_factory=set)

    def summary(self) -> str:
        s = (
            f"ack {self.acks}(실패 {self.ack_failures}) 수신 {self.messages} "
            f"체결 {self.ticks} 호가 {self.quotes} 격리 {self.quarantined} "
            f"적재 {self.written_ticks}+{self.written_quotes} 중복 {self.duplicates} "
            f"Flush {self.flushes}"
        )
        if self.reconnects:
            s += f" 재접속 {self.reconnects}"
        if self.disconnect_reasons:
            s += f" 끊김사유 {self.disconnect_reasons}"
        if self.observer_errors:
            s += f" 관찰오류 {self.observer_errors}"
        if self.calendar_unverified:
            s += f" Calendar미확정 {sorted(str(d) for d in self.calendar_unverified)}"
        if self.quarantine_reasons:
            s += f" 격리사유 {self.quarantine_reasons}"
        if self.unknown_tr:
            s += f" 미등록TR {self.unknown_tr}"
        return s


class MarketSink:
    """정규화된 Tick/Quote 를 모아 TimescaleDB 로 넣는다.

    news_pipeline.NewsSink 와 같은 원칙이다 - 크기 또는 시간 중 먼저 오는 쪽으로
    Flush 하고, 남은 것을 절대 버리지 않으며, 실패를 삼키지 않는다.
    """

    def __init__(
        self,
        repo,
        *,
        max_batch: int = DEFAULT_MAX_BATCH,
        max_delay_seconds: float = DEFAULT_MAX_DELAY_SECONDS,
        clock=None,
    ) -> None:
        if max_batch < 1:
            raise ValueError("max_batch 는 1 이상이어야 한다")
        if max_delay_seconds <= 0:
            raise ValueError("max_delay_seconds 는 0 보다 커야 한다")
        self._repo = repo
        self._max_batch = max_batch
        self._max_delay = max_delay_seconds
        self._clock = clock
        self._ticks: list[MarketTick] = []
        self._quotes: list[MarketQuote] = []
        self._started: float | None = None
        self._closed = False
        self.stats = WorkerStats()

    def add(self, event) -> None:
        if self._closed:
            raise LsRealtimeError("닫힌 Sink 에 이벤트를 넣으려 했다")
        if isinstance(event, MarketTick):
            self._ticks.append(event)
        elif isinstance(event, MarketQuote):
            self._quotes.append(event)
        else:
            raise LsRealtimeError(f"Sink 가 모르는 이벤트다: {type(event).__name__}")

        now = self._now()
        if self._started is None:
            self._started = now
        if self._pending >= self._max_batch or (now - self._started) >= self._max_delay:
            self.flush()

    def tick(self) -> None:
        """이벤트가 안 와도 시간이 지나면 Flush 한다."""
        if (
            self._pending
            and self._started is not None
            and (self._now() - self._started) >= self._max_delay
        ):
            self.flush()

    @property
    def _pending(self) -> int:
        return len(self._ticks) + len(self._quotes)

    def flush(self) -> None:
        if not self._pending:
            return
        ticks, quotes = self._ticks, self._quotes

        wt = self._repo.write_ticks(ticks) if ticks else None
        wq = self._repo.write_quotes(quotes) if quotes else None

        # 성공한 뒤에 비운다 - 실패하면 버퍼가 남아 재시도된다.
        self._ticks, self._quotes, self._started = [], [], None
        if wt:
            self.stats.written_ticks += wt.inserted
            self.stats.duplicates += wt.duplicates
        if wq:
            self.stats.written_quotes += wq.inserted
            self.stats.duplicates += wq.duplicates
        self.stats.flushes += 1

    def close(self) -> None:
        if self._closed:
            return
        try:
            self.flush()
        finally:
            self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.close()
        return False

    def _now(self) -> float:
        if self._clock is not None:
            return self._clock()
        import time as _time

        return _time.monotonic()


def make_trading_day_check(trading_days: set, *, stats: WorkerStats | None = None):
    """Calendar 를 is_trading_day 판정 함수로 바꾼다.

    ▶ **모르는 날짜를 휴장으로 단정하지 않는다** (2026-07-31 실측으로 잡은 문제)
      Calendar 는 t8410 일봉 관측 역산이라 **당일이 절대 들어 있지 않다.** 그런데
      `d in trading_days` 로 판정하면 오늘이 False 가 되어 장중에 받은 체결 전부에
      NON_TRADING_DAY Flag 가 붙는다. 실측에서 733건 전부가 그랬다 - 거래가
      일어나고 있는데 "비거래일" 로 기록되는, 사실과 정반대인 데이터다.

      Calendar 가 아는 마지막 날 이후는 **판정 불가** 이지 휴장이 아니다. 그리고
      이 함수가 쓰이는 맥락은 실시간 체결을 받고 있는 상황이다 - 거래소가 휴장일에
      체결을 흘리지 않으므로, 데이터가 흐른다는 것 자체가 거래일이라는 증거다.
      그래서 거래일로 간주하되 **간주했다는 사실을 stats 에 남긴다.**

      Calendar 가 이미 덮은 구간에서는 원래대로 엄격히 판정한다.
    """
    known_max = max(trading_days) if trading_days else None

    def check(d: date) -> bool:
        if d in trading_days:
            return True
        if known_max is not None and d > known_max:
            if stats is not None:
                stats.calendar_unverified.add(d)
            return True
        return False

    return check


def subscribe_key(tr_cd: str, symbol: str) -> str:
    """그 TR 이 받는 `tr_key` 형태로 종목코드를 바꾼다.

    ▶ 통합시세(US3/UH1)는 **`U` + 6자리 + 공백 3개** 다 (실측 2026-08-12)
      거래소 단독 TR(S3_/H1_ 등)은 종목코드를 그대로 받는데, 통합시세는
      접두사와 패딩이 붙은 10자 키를 받는다. 그냥 `005930` 을 보내면 소켓은
      멀쩡히 붙고 등록도 거부 없이 지나간 뒤 **데이터만 안 온다** - 그날
      아침 26개 소켓이 전부 연결된 채 40분간 수신 0건이었다.

      티가 안 나는 실패라 자체 점검으로 고정한다. 같은 시각 같은 TR 로
      받고 있던 다른 수집기의 로그(`tr_key='U476830   '`)가 규격의 근거다.
    """
    if tr_cd in UNIFIED_TR_KEYS:
        return f"U{symbol}   "
    return symbol


def build_subscribe_message(tr_cd: str, tr_key: str, token: str, *, subscribe: bool = True) -> str:
    """구독 등록/해제 메시지. 실측으로 확인한 형태 그대로다.

    `tr_key` 는 이미 `subscribe_key()` 를 거친 값이어야 한다 - 여기서 다시
    변환하면 해제 경로가 등록과 다른 키를 보낼 수 있다.
    """
    return json.dumps(
        {
            "header": {
                "token": token,
                "tr_type": TR_TYPE_SUBSCRIBE if subscribe else TR_TYPE_UNSUBSCRIBE,
            },
            "body": {"tr_cd": tr_cd, "tr_key": tr_key},
        }
    )


def classify_message(msg: dict) -> tuple[str, str | None, dict | None]:
    """수신 메시지를 (종류, tr_cd, body) 로 나눈다.

    ack 와 실시간 데이터가 **같은 tr_cd 로 온다.** header 에 rsp_cd 가 있으면
    ack 이고 없으면 데이터다. 이걸 구분하지 않으면 ack 를 데이터로 정규화하려다
    전부 Quarantine 된다.
    """
    header = msg.get("header") or {}
    tr_cd = header.get("tr_cd")
    if header.get("rsp_cd") is not None:
        return ("ack", tr_cd, header)
    body = msg.get("body")
    if isinstance(body, dict):
        return ("data", tr_cd, body)
    return ("other", tr_cd, None)


class LsRealtimeWorker:
    """구독 목록을 받아 한 소켓으로 실시간 수집한다."""

    def __init__(
        self,
        *,
        subscriptions: list[tuple[str, str]],
        token_provider,
        ws_url: str,
        resolve_instrument,
        sink: MarketSink,
        is_trading_day=None,
        on_event=None,
    ) -> None:
        if not subscriptions:
            raise LsRealtimeError("구독 목록이 비었다 - 빈 수집을 정상으로 보지 않는다")
        self._subs = subscriptions
        self._token_provider = token_provider
        self._ws_url = ws_url
        self._resolve = resolve_instrument
        self._sink = sink
        self._is_trading_day = is_trading_day
        self._on_event = on_event
        self.stats = sink.stats

    async def run(self, *, max_seconds: float | None = None, max_messages: int | None = None):
        import asyncio


        loop = asyncio.get_running_loop()
        started = loop.time()

        attempt = 0
        while True:
            remaining = None if max_seconds is None else max_seconds - (loop.time() - started)
            if remaining is not None and remaining <= 0:
                return self.stats
            if max_messages is not None and self.stats.messages >= max_messages:
                return self.stats
            try:
                await self._session(remaining, max_messages)
                return self.stats
            except (LsRealtimeError, KeyboardInterrupt):
                raise
            except Exception as e:  # noqa: BLE001 - intentional fallback boundary
                attempt += 1
                self.stats.reconnects += 1
                key = f"{type(e).__name__}: {str(e)[:70]}" if str(e) else type(e).__name__
                self.stats.disconnect_reasons[key] = (
                    self.stats.disconnect_reasons.get(key, 0) + 1
                )
                if attempt > MAX_RECONNECTS:
                    raise LsRealtimeError(
                        f"재접속 {MAX_RECONNECTS}회 후에도 실패한다: {type(e).__name__}: {e}"
                    ) from None
                backoff = RECONNECT_BACKOFF_SECONDS[
                    min(attempt - 1, len(RECONNECT_BACKOFF_SECONDS) - 1)
                ]
                if remaining is not None and backoff >= remaining:
                    return self.stats
                await asyncio.sleep(backoff)

    async def _session(self, max_seconds, max_messages):
        import asyncio

        import websockets

        token = self._token_provider()
        async with websockets.connect(self._ws_url, open_timeout=20, ping_interval=30) as ws:
            # **재접속하면 반드시 재구독한다.** 소켓만 열고 구독을 빠뜨리면
            # 조용히 아무것도 안 받는 상태가 된다.
            for tr_cd, symbol in self._subs:
                await ws.send(build_subscribe_message(
                    tr_cd, subscribe_key(tr_cd, symbol), token))

            loop = asyncio.get_running_loop()
            started = loop.time()
            while True:
                if max_messages is not None and self.stats.messages >= max_messages:
                    return
                timeout = None
                if max_seconds is not None:
                    timeout = max_seconds - (loop.time() - started)
                    if timeout <= 0:
                        return
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    return
                self._handle(raw)
                # 조용한 구간에서도 버퍼가 나가야 한다
                self._sink.tick()

    def _handle(self, raw: str) -> None:
        received_at = datetime.now(timezone.utc)
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            self.stats.quarantined += 1
            self.stats.quarantine_reasons["JSON_PARSE"] = (
                self.stats.quarantine_reasons.get("JSON_PARSE", 0) + 1
            )
            return

        kind, tr_cd, payload = classify_message(msg)
        if kind == "ack":
            self.stats.acks += 1
            if str(payload.get("rsp_cd")) != "00000":
                self.stats.ack_failures += 1
                # 구독이 거절됐는데 계속 도는 것은 "받는 줄 알았는데 안 받는" 상태다.
                raise LsRealtimeError(
                    f"구독 거절 tr_cd={tr_cd} rsp={payload.get('rsp_cd')} "
                    f"{payload.get('rsp_msg')}"
                )
            return
        if kind != "data" or tr_cd is None:
            return

        self.stats.messages += 1
        event = normalize(
            tr_cd,
            payload,
            received_at=received_at,
            resolve_instrument=self._resolve,
            is_trading_day=self._is_trading_day,
        )
        if isinstance(event, QuarantinedEvent):
            self.stats.quarantined += 1
            reason = str(getattr(event, "reason", "UNKNOWN"))
            self.stats.quarantine_reasons[reason] = (
                self.stats.quarantine_reasons.get(reason, 0) + 1
            )
            if "UNKNOWN_TR" in reason:
                self.stats.unknown_tr[tr_cd] = self.stats.unknown_tr.get(tr_cd, 0) + 1
            return

        if isinstance(event, MarketTick):
            self.stats.ticks += 1
        else:
            self.stats.quotes += 1
        # 적재는 실패하면 올린다(데이터 손실이므로). 관찰용 콜백은 격리한다 -
        # 소비자 버그가 수집을 죽이면 안 되고, 죽으면 재접속으로 위장돼 원인을
        # 못 찾는다. 대신 조용히 넘기지 않고 센다.
        self._sink.add(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception as e:  # noqa: BLE001 - intentional fallback boundary
                key = f"{type(e).__name__}: {str(e)[:60]}"
                self.stats.observer_errors[key] = (
                    self.stats.observer_errors.get(key, 0) + 1
                )


# ---------------------------------------------------------------------------
# 자체 점검 - 접속 없음
# ---------------------------------------------------------------------------

class _FakeRepo:
    def __init__(self):
        self.ticks, self.quotes, self.calls = [], [], 0

    def write_ticks(self, rows):
        from market_repository import WriteResult

        self.calls += 1
        self.ticks.extend(rows)
        return WriteResult(len(rows), len(rows), 0)

    def write_quotes(self, rows):
        from market_repository import WriteResult

        self.calls += 1
        self.quotes.extend(rows)
        return WriteResult(len(rows), len(rows), 0)


def _now_hhmmss() -> str:
    """지금 KST 시:분:초. 고정 문자열을 쓰면 adapter 의 수신 지연 한계에 걸려
    **실행 시각에 따라 테스트가 깨진다**(실제로 겪었다)."""
    return f"{datetime.now(KST):%H%M%S}"


def _sample_tick_payload(price="70000", t=None):
    t = t or _now_hhmmss()
    return {
        "chetime": t, "sign": "2", "change": "100", "drate": "0.14", "price": price,
        "opentime": "090000", "open": "69900", "hightime": "100000", "high": "70100",
        "lowtime": "091000", "low": "69800", "cgubun": "+", "cvolume": "10",
        "volume": "1000", "value": "70000", "mdvolume": "500", "mdchecnt": "5",
        "msvolume": "500", "mschecnt": "5", "cpower": "100.00", "w_avrg": "70000",
        "offerho": "70100", "bidho": "69900", "status": "00", "jnilvolume": "900",
        "shcode": "005930", "exchname": "KRX",
    }


def _check_subscribe_message():
    m = json.loads(build_subscribe_message("S3_", "005930", "tok"))
    assert m == {"header": {"token": "tok", "tr_type": "3"},
                 "body": {"tr_cd": "S3_", "tr_key": "005930"}}, m
    u = json.loads(build_subscribe_message("S3_", "005930", "tok", subscribe=False))
    assert u["header"]["tr_type"] == "4"

    # ▶ **통합시세는 tr_key 규격이 다르다** (실측 2026-08-12)
    #   `U` + 6자리 + 공백 3개. 종목코드를 그대로 보내면 소켓은 붙고 등록도
    #   거부 없이 지나간 뒤 **데이터만 안 온다** - 26개 소켓이 전부 연결된 채
    #   40분간 수신 0건이었다. 티가 안 나는 실패라 여기서 고정한다.
    assert subscribe_key("US3", "005930") == "U005930   ", subscribe_key("US3", "005930")
    assert subscribe_key("UH1", "196170") == "U196170   "
    assert len(subscribe_key("US3", "005930")) == 10
    # 거래소 단독 TR 은 예전 그대로 - 되돌아가도 깨지지 않는다
    for tr in ("S3_", "K3_", "H1_", "HA_"):
        assert subscribe_key(tr, "005930") == "005930", tr
    # 등록과 해제가 **같은 키**를 써야 한다 - 다르면 해제가 안 먹는다
    key = subscribe_key("US3", "005930")
    for on in (True, False):
        m2 = json.loads(build_subscribe_message("US3", key, "tok", subscribe=on))
        assert m2["body"]["tr_key"] == "U005930   ", m2
    print("  구독 메시지              OK")


def _check_classify():
    # ack 와 데이터가 같은 tr_cd 로 온다. rsp_cd 유무로만 갈린다.
    ack = {"header": {"tr_cd": "S3_", "rsp_cd": "00000", "rsp_msg": "정상처리되었습니다"}}
    assert classify_message(ack)[0] == "ack"
    data = {"header": {"tr_cd": "S3_"}, "body": _sample_tick_payload()}
    kind, tr, body = classify_message(data)
    assert kind == "data" and tr == "S3_" and body["shcode"] == "005930"
    # body 가 없으면 데이터가 아니다
    assert classify_message({"header": {"tr_cd": "S3_"}})[0] == "other"
    print("  ack/데이터 구분          OK")


def _check_sink_batching():
    repo = _FakeRepo()
    sink = MarketSink(repo, max_batch=3, clock=lambda: 0.0)
    from ls_realtime_adapter import normalize as _n

    def res(sym):
        from uuid import NAMESPACE_URL, uuid5

        return uuid5(NAMESPACE_URL, f"ls://KOSPI/{sym}")

    recv = datetime(2026, 7, 31, 1, 30, tzinfo=timezone.utc)
    evs = [
        _n("S3_", _sample_tick_payload(price=str(70000 + i), t=f"1030{i:02d}"),
            received_at=recv, resolve_instrument=res)
        for i in range(3)
    ]
    assert all(isinstance(e, MarketTick) for e in evs), [type(e).__name__ for e in evs]
    for e in evs[:2]:
        sink.add(e)
    assert repo.calls == 0, "배치가 안 찼는데 Flush 했다"
    sink.add(evs[2])
    assert repo.calls == 1 and len(repo.ticks) == 3
    assert sink.stats.written_ticks == 3 and sink.stats.flushes == 1

    # 남은 것을 잃지 않는다
    repo2 = _FakeRepo()
    with MarketSink(repo2, max_batch=100, clock=lambda: 0.0) as s2:
        s2.add(evs[0])
        assert repo2.calls == 0
    assert len(repo2.ticks) == 1, "close 에서 버퍼를 잃었다"

    # 닫힌 Sink 는 조용히 받아먹지 않는다
    try:
        s2.add(evs[0])
        raise AssertionError("닫힌 Sink 가 이벤트를 받았다")
    except LsRealtimeError:
        pass
    print("  Sink 배치/꼬리           OK")


def _check_sink_time_flush():
    t = {"v": 0.0}
    repo = _FakeRepo()
    sink = MarketSink(repo, max_batch=1000, max_delay_seconds=2.0, clock=lambda: t["v"])
    from uuid import NAMESPACE_URL, uuid5

    ev = normalize("S3_", _sample_tick_payload(),
                   received_at=datetime(2026, 7, 31, 1, 30, tzinfo=timezone.utc),
                   resolve_instrument=lambda s: uuid5(NAMESPACE_URL, f"ls://KOSPI/{s}"))
    sink.add(ev)
    assert repo.calls == 0
    t["v"] = 3.0
    sink.tick()
    assert repo.calls == 1, "시간 경과 Flush 가 안 됐다"
    print("  Sink 시간 Flush          OK")


def _check_worker_handle():
    from uuid import NAMESPACE_URL, uuid5

    repo = _FakeRepo()
    sink = MarketSink(repo, max_batch=1, clock=lambda: 0.0)
    w = LsRealtimeWorker(
        subscriptions=[("S3_", "005930")],
        token_provider=lambda: "tok",
        ws_url="wss://x/websocket",
        resolve_instrument=lambda s: uuid5(NAMESPACE_URL, f"ls://KOSPI/{s}"),
        sink=sink,
    )
    # ack 는 데이터로 정규화하지 않는다
    w._handle(json.dumps({"header": {"tr_cd": "S3_", "rsp_cd": "00000", "rsp_msg": "ok"}}))
    assert w.stats.acks == 1 and w.stats.messages == 0 and w.stats.quarantined == 0

    w._handle(json.dumps({"header": {"tr_cd": "S3_"}, "body": _sample_tick_payload()}))
    assert w.stats.messages == 1 and w.stats.ticks == 1
    assert len(repo.ticks) == 1

    # 등록 안 된 TR 은 격리하고 세지만 스트림을 멈추지 않는다
    w._handle(json.dumps({"header": {"tr_cd": "ZZZ"}, "body": _sample_tick_payload()}))
    assert w.stats.quarantined == 1 and w.stats.unknown_tr.get("ZZZ") == 1

    # 깨진 JSON 도 마찬가지다
    w._handle("{not json")
    assert w.stats.quarantine_reasons.get("JSON_PARSE") == 1

    # 관찰용 콜백이 터져도 수집은 계속된다. 다만 숨기지 않는다 - 이게 안 되면
    # 소비자 버그가 '재접속' 으로 위장돼 원인을 못 찾는다(실제로 겪었다).
    boom = LsRealtimeWorker(
        subscriptions=[("S3_", "005930")], token_provider=lambda: "tok",
        ws_url="wss://x/websocket",
        resolve_instrument=lambda s: uuid5(NAMESPACE_URL, f"ls://KOSPI/{s}"),
        sink=MarketSink(_FakeRepo(), max_batch=1, clock=lambda: 0.0),
        on_event=lambda e: e.event_time,  # 존재하지 않는 속성
    )
    boom._handle(json.dumps({"header": {"tr_cd": "S3_"}, "body": _sample_tick_payload()}))
    assert boom.stats.ticks == 1, "관찰 콜백 오류가 수집을 죽였다"
    assert any("AttributeError" in k for k in boom.stats.observer_errors), \
        boom.stats.observer_errors

    # 구독 거절은 반대로 즉시 멈춘다 - '받는 줄 알았는데 안 받는' 상태가 제일 위험하다
    try:
        w._handle(json.dumps({"header": {"tr_cd": "S3_", "rsp_cd": "99999",
                                         "rsp_msg": "권한없음"}}))
        raise AssertionError("구독 거절이 조용히 지나갔다")
    except LsRealtimeError as e:
        assert "거절" in str(e) and "99999" in str(e)
    assert w.stats.ack_failures == 1
    print("  메시지 처리              OK")


def _check_trading_day_check():
    """모르는 날짜를 휴장으로 단정하지 않는지 - 실측에서 733건 전부가 오염됐던 문제."""
    days = {date(2026, 7, 29), date(2026, 7, 30)}
    st = WorkerStats()
    check = make_trading_day_check(days, stats=st)

    assert check(date(2026, 7, 30)) is True
    # Calendar 가 아는 마지막 날 이후는 판정 불가지 휴장이 아니다
    assert check(date(2026, 7, 31)) is True
    assert st.calendar_unverified == {date(2026, 7, 31)}, st.calendar_unverified
    # 이미 덮은 구간에서는 엄격히 판정한다(2026-07-28 은 없으므로 False)
    assert check(date(2026, 7, 28)) is False
    assert date(2026, 7, 28) not in st.calendar_unverified

    # Calendar 가 비면 아무 것도 단정하지 않는다
    empty = make_trading_day_check(set())
    assert empty(date(2026, 7, 31)) is False

    # 실제 정규화까지 태워서 Flag 가 안 붙는지 본다
    from uuid import NAMESPACE_URL, uuid5

    today = datetime.now(KST).date()
    ev = normalize(
        "S3_", _sample_tick_payload(), received_at=datetime.now(timezone.utc),
        resolve_instrument=lambda s: uuid5(NAMESPACE_URL, f"ls://KOSPI/{s}"),
        is_trading_day=make_trading_day_check({today - timedelta(days=1)}),
    )
    assert isinstance(ev, MarketTick), type(ev).__name__
    flags = {str(f) for f in ev.quality_flags}
    assert "NON_TRADING_DAY" not in flags, flags
    print("  거래일 판정              OK")


def _check_guards():
    repo = _FakeRepo()
    try:
        LsRealtimeWorker(subscriptions=[], token_provider=lambda: "t",
                         ws_url="wss://x", resolve_instrument=lambda s: None,
                         sink=MarketSink(repo))
        raise AssertionError("빈 구독 목록이 통과했다")
    except LsRealtimeError:
        pass
    for kw in ({"max_batch": 0}, {"max_delay_seconds": 0}):
        try:
            MarketSink(repo, **kw)
            raise AssertionError(f"{kw} 가 통과했다")
        except ValueError:
            pass
    # 경로는 구독 계획과 같은 상수를 쓴다
    assert WEBSOCKET_PATH == "/websocket"
    s = WorkerStats(acks=2, messages=30, ticks=18, quotes=12, written_ticks=18,
                    written_quotes=12, flushes=3)
    out = s.summary()
    for token in ("ack 2", "수신 30", "체결 18", "호가 12", "적재 18+12"):
        assert token in out, f"{token} 이 요약에 없다: {out}"
    print("  가드/요약                OK")


# ---------------------------------------------------------------------------
# 실제 수집
# ---------------------------------------------------------------------------

def _run(seconds: float, symbols: tuple[str, ...]) -> int:
    import asyncio

    from ls_client import LsRestClient
    from market_repository import TimescaleMarketRepository
    from reference_repository import SupabaseReferenceRepository

    env = load_project_env()
    SourceRegistry(env=env).require(SOURCE_ID)
    mode = market_env(env)
    ws_base = env["LS_WS_BASE_URL_PAPER"] if mode == "PAPER" else env["LS_WS_BASE_URL"]
    ws_url = ws_base.rstrip("/") + WEBSOCKET_PATH

    now_kst = datetime.now(KST)
    print(f"  LS_MARKET_ENV={mode}  {ws_url}")
    print(f"  현재 KST {now_kst:%Y-%m-%d %H:%M:%S} (정규장 09:00~15:30)")

    ref = SupabaseReferenceRepository()
    try:
        iid_by_symbol = ref.resolve_symbols(list(symbols))
        missing = set(symbols) - set(iid_by_symbol)
        if missing:
            raise LsRealtimeError(f"Instrument Master 에 없는 종목이다: {sorted(missing)}")
        trading_days = {
            d for d, _o, _c in ref.recent_trading_sessions(limit=40)
        }
    finally:
        ref.close()
    print(f"  종목 {len(iid_by_symbol)}개 해결, 최근 거래일 {len(trading_days)}일 확보")

    # 체결(S3_)과 호가(H1_)를 종목마다 등록한다. 코스닥이면 K3_/HA_ 다.
    subs: list[tuple[str, str]] = []
    for sym in symbols:
        subs.append(("S3_", sym))
        subs.append(("H1_", sym))
    print(f"  구독 {len(subs)}건: {', '.join(f'{t}:{k}' for t, k in subs[:6])}"
          + (" ..." if len(subs) > 6 else ""))

    client = market_rest_client(env)
    dsn = env.get("TIMESCALE_DATABASE_URL")
    if not dsn:
        raise LsRealtimeError("TIMESCALE_DATABASE_URL 이 없다")
    repo = TimescaleMarketRepository(dsn)

    def on_event(e):
        # 시각과 종목은 중첩 객체다(times / instrument). 평평하게 있다고 가정하면
        # AttributeError 가 나는데, 그게 on_event 안에서 터지면 세션이 죽고
        # **재접속으로 위장된다.** 실제로 겪었다 - 그래서 disconnect_reasons 를 남긴다.
        if isinstance(e, MarketTick):
            print(f"    체결 {e.times.event_time.astimezone(KST):%H:%M:%S} "
                  f"{e.instrument.provider_symbol} {e.price} x{e.quantity} "
                  f"side={e.side.name}")

    shown = {"n": 0}

    def limited(e):
        if shown["n"] < 6:
            on_event(e)
            shown["n"] += 1

    try:
        with MarketSink(repo) as sink:
            worker = LsRealtimeWorker(
                subscriptions=subs,
                token_provider=client.token,
                ws_url=ws_url,
                resolve_instrument=lambda s: iid_by_symbol.get(s),
                sink=sink,
                is_trading_day=make_trading_day_check(trading_days, stats=sink.stats),
                on_event=limited,
            )
            print(f"  {seconds}초 수집 시작...\n")
            asyncio.run(worker.run(max_seconds=seconds))
        stats = sink.stats
        print(f"\n  {stats.summary()}")
        total = repo.count_ticks_all() if hasattr(repo, "count_ticks_all") else None
        if total is not None:
            print(f"  market_ticks 총 {total}행")
        if stats.messages and not (stats.written_ticks + stats.written_quotes):
            print("  ⚠ 수신은 됐는데 적재가 0이다 - 정규화나 적재 경로를 확인할 것")
            return 1
    finally:
        repo.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        secs = 30.0
        if "--seconds" in sys.argv:
            secs = float(sys.argv[sys.argv.index("--seconds") + 1])
        syms = ("005930", "000660")
        if "--symbols" in sys.argv:
            syms = tuple(
                s.strip() for s in sys.argv[sys.argv.index("--symbols") + 1].split(",") if s.strip()
            )
        print(f"{WORKER_VERSION} 실시간 수집")
        raise SystemExit(_run(secs, syms))

    print(f"{WORKER_VERSION} 자체 점검 (접속 없음)")
    _check_subscribe_message()
    _check_classify()
    _check_sink_batching()
    _check_sink_time_flush()
    _check_worker_handle()
    _check_trading_day_check()
    _check_guards()
    print("Worker 7개 영역 통과. 실제 수집은 --run")
