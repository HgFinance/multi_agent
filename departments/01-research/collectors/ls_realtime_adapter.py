#!/usr/bin/env python3
"""Sprint J1/F04: LS 실시간 payload 정규화 Adapter.

소유: 재일 (리서치본부)
근거: docs/06-integrations/ls-openapi/03-stock/16-9a2800c3.md
        S3_ KOSPI체결 / K3_ KOSDAQ체결 / H1_ KOSPI호가잔량 / HA_ KOSDAQ호가잔량
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F04(Market Event 정규화)
      docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 4.1, 4.2, 8.2

F04 완료 조건 - "Adapter 가 바뀌어도 하위 Feature Schema 는 유지된다". 그래서 공급자
필드 이름은 이 파일 안에서만 살고, 밖으로는 contracts.market_events 의 계약만 나간다.

▶ 시각 문제 (이 파일에서 가장 조심할 부분)
  `chetime`/`hotime` 은 **HHMMSS 6자리로 날짜가 없다.** 그래서 거래일을 조합해야 한다.
  received_at(수신 시각) 의 날짜를 쓰되, 조합 결과가 수신 시각보다 미래면 하루를 뺀다 -
  장 마감 직전 Event 가 자정을 넘겨 수신되는 경우가 있기 때문이다.
  거래일 여부는 Calendar 를 주입받아 확인하고, 비거래일로 판정되면 값을 버리지 않고
  NON_TRADING_DAY Flag 를 남긴다(가이드 8.2 - Flag 는 표시, Quarantine 은 적재 불가).

▶ 추정하지 않는 것
  `cgubun`(체결구분)과 `donsigubun`(동시호가구분)의 실제 코드 값이 수집 문서에 없다
  (String(1) 까지만 적혀 있다). 그래서 알려진 값만 매핑하고 나머지는 UNKNOWN 으로 둔다.
  체결 방향을 추정해서 채우면 Microstructure Feature 가 조용히 오염된다.

자체 점검: python departments/01-research/collectors/ls_realtime_adapter.py
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol
from uuid import UUID

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "contracts"))
from market_events import (
    QUOTE_DEPTH_MAX,
    InstrumentRef,
    Market,
    MarketQuote,
    MarketTick,
    ObservationTimes,
    QualityFlag,
    QuarantinedEvent,
    QuarantineReason,
    SessionType,
    Side,
    build_source_event_id,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ls_unified_parser import exchname_to_market, normalize_symbol
from subscription_plan import DataKind, Venue

ADAPTER_VERSION = "research-ls-realtime-adapter-v1"

KST = timezone(timedelta(hours=9))
PROVIDER_LS = "LS"

# 어느 TR 이 무엇인지. subscription_plan.TR_MATRIX 의 역방향이다.
TR_TO_KIND: dict[str, tuple[DataKind, Venue]] = {
    "S3_": (DataKind.TICK, Venue.KOSPI),
    "K3_": (DataKind.TICK, Venue.KOSDAQ),
    "H1_": (DataKind.QUOTE, Venue.KOSPI),
    "HA_": (DataKind.QUOTE, Venue.KOSDAQ),
    # ▶ 통합시세 US3/UH1 (2026-08-11 이식)
    #   위 넷은 **KRX 단독**이다. 실측: 8/11 우리 수집 11,733,292행이 전부 KRX 였고
    #   같은 날 NXT 3,842,266행이 통째로 빠졌다. NXT 로 흘러간 체결을 못 보면
    #   거래량·OFI·유효스프레드가 전부 한쪽만 본 값이 된다.
    #
    #   Venue 자리에 KOSPI 를 넣지만 **이 값은 쓰이지 않는다** - 통합시세는
    #   거래소가 payload 의 exchname 으로 오므로 market 은 거기서 읽는다
    #   (아래 _market_of). 보드(코스피/코스닥) 구분은 reference 쪽 속성이라
    #   시계열에는 애초에 안 들어간다.
    "US3": (DataKind.TICK, Venue.KOSPI),
    "UH1": (DataKind.QUOTE, Venue.KOSPI),
}

# 통합시세는 한 TR 이 두 거래소를 실어 온다 - TR 로는 거래소를 알 수 없다.
UNIFIED_TRS = frozenset({"US3", "UH1"})

# 시계열 Venue 는 거래소 단위다. Board(KOSPI/KOSDAQ)는 reference 쪽 속성이므로
# market.market_ticks.market 에는 KRX 가 들어간다(reference_repository 관례와 같다).
VENUE_TO_MARKET = {Venue.KOSPI: Market.KRX, Venue.KOSDAQ: Market.KRX}


def _market_of(tr_cd: str, payload: dict, venue: Venue) -> Market:
    """이 이벤트가 **어느 거래소에서 난 것인가.**

    KRX 단독 TR 은 TR 이 곧 거래소다. 통합시세(US3/UH1)는 한 스트림에 두
    거래소가 섞여 오므로 payload 의 `exchname` 을 읽어야 한다.

    ▶ 미상은 KRX 로 본다 (ls_unified_parser 와 같은 규칙)
      NXT 로 추정하지 않는다 - 통합시세 이전의 이력이 전부 KRX 이고, 모르는
      것을 새 거래소로 몰면 없던 NXT 물량이 생긴다. 반대 방향의 오류는
      '기존과 같다' 로 수렴하므로 덜 위험하다.
    """
    if tr_cd not in UNIFIED_TRS:
        return VENUE_TO_MARKET[venue]
    return Market.NXT if exchname_to_market(payload.get("exchname")) == "N" \
        else Market.KRX

# cgubun 체결구분. 수집 문서에 값이 없어 실제 payload 로 확인해야 한다.
# 확인 전까지 여기 없는 값은 전부 Side.UNKNOWN 이다 - 매수로 추정하지 않는다.
CGUBUN_TO_SIDE: dict[str, Side] = {
    "+": Side.BUY,
    "-": Side.SELL,
}

# status 장상태 / donsigubun 동시호가구분도 값 미확인이다. 알려진 것만 매핑한다.
DONSI_TO_SESSION: dict[str, SessionType] = {
    "1": SessionType.PRE_AUCTION,
}

# event_time 이 received_at 보다 이만큼 미래면 날짜 조합이 틀린 것으로 본다.
# 공급자 시각이 우리보다 약간 앞설 수 있어 여유를 둔다.
CLOCK_TOLERANCE = timedelta(minutes=5)
# 수신이 이만큼 늦으면 날짜 조합을 신뢰할 수 없다.
MAX_INGEST_LAG = timedelta(hours=12)


class InstrumentResolver(Protocol):
    """provider_symbol -> instrument_id. reference_repository 가 구현한다."""

    def __call__(self, symbol: str) -> UUID | None: ...


TradingDayCheck = Callable[[date], bool]


@dataclass(frozen=True)
class AdapterStats:
    """정규화 결과 집계. Quarantine 을 숨기지 않는다(가이드 8.2)."""

    normalized: int = 0
    quarantined: int = 0
    flagged: int = 0

    def merge(self, *, normalized=0, quarantined=0, flagged=0) -> AdapterStats:
        return AdapterStats(
            self.normalized + normalized,
            self.quarantined + quarantined,
            self.flagged + flagged,
        )


class TimeResolutionError(ValueError):
    """HHMMSS 와 거래일을 조합할 수 없다."""


def resolve_event_time(hhmmss: str, received_at: datetime) -> datetime:
    """HHMMSS(KST) + 수신 시각 -> UTC event_time.

    날짜가 없는 시각을 다루는 유일한 곳이다. 규칙:
      1. 수신 시각을 KST 로 바꿔 그 날짜를 후보로 쓴다.
      2. 조합 결과가 수신 시각보다 CLOCK_TOLERANCE 이상 미래면 하루를 뺀다.
         장 마감 직전 Event 가 자정 넘겨 도착하는 경우를 잡는다.
      3. 그래도 MAX_INGEST_LAG 를 넘게 과거면 조합을 신뢰하지 않고 예외다.
         추측으로 날짜를 밀면 잘못된 거래일에 적재된다.
    """
    text = (hhmmss or "").strip()
    if len(text) != 6 or not text.isdigit():
        raise TimeResolutionError(f"HHMMSS 형식이 아니다: {hhmmss!r}")
    hh, mm, ss = int(text[0:2]), int(text[2:4]), int(text[4:6])
    if hh > 23 or mm > 59 or ss > 59:
        raise TimeResolutionError(f"시각 범위를 벗어났다: {hhmmss!r}")

    if received_at.tzinfo is None:
        raise TimeResolutionError("received_at 이 tz-aware 여야 한다")
    recv_kst = received_at.astimezone(KST)

    candidate = datetime.combine(recv_kst.date(), time(hh, mm, ss), tzinfo=KST)
    if candidate > recv_kst + CLOCK_TOLERANCE:
        candidate -= timedelta(days=1)

    lag = recv_kst - candidate
    if lag > MAX_INGEST_LAG:
        raise TimeResolutionError(
            f"수신 지연 {lag} 이 한계 {MAX_INGEST_LAG} 를 넘는다 "
            f"(hhmmss={text}, received_at={received_at.isoformat()})"
        )
    return candidate.astimezone(timezone.utc)


def _dec(raw, field: str, *, allow_blank=True) -> Decimal | None:
    """공급자 숫자를 Decimal 로. 파싱 실패는 0 으로 떨어지지 않고 예외다."""
    if raw is None:
        if allow_blank:
            return None
        raise ValueError(f"{field} 가 없다")
    text = str(raw).strip()
    if not text:
        if allow_blank:
            return None
        raise ValueError(f"{field} 가 비었다")
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"{field} 를 숫자로 읽을 수 없다: {raw!r}") from None


def _quarantine(reason, detail, *, tr_cd, payload, received_at, symbol=None) -> QuarantinedEvent:
    # 계약은 tr_code, LS payload 는 tr_cd 다. 이름 변환은 이 Adapter 안에서만 한다 -
    # 공급자 필드 이름이 계약 밖으로 새지 않게 하는 것이 F04 의 목적이다.
    return QuarantinedEvent(
        reason=reason,
        detail=detail[:512],
        provider=PROVIDER_LS,
        tr_code=tr_cd,
        provider_symbol=symbol,
        received_at=received_at,
        raw_payload=payload,
    )


def normalize(
    tr_cd: str,
    payload: dict,
    *,
    received_at: datetime,
    resolve_instrument: InstrumentResolver,
    is_trading_day: TradingDayCheck | None = None,
    observed_at: datetime | None = None,
    trace_id: UUID | None = None,
) -> MarketTick | MarketQuote | QuarantinedEvent:
    """실시간 payload 하나를 정규 계약으로 바꾼다.

    실패는 예외가 아니라 QuarantinedEvent 다 - 한 건 때문에 스트림을 멈추면 안 되고,
    원본을 보존해 재처리해야 한다(F04). 대신 조용히 버리지 않는다.
    """
    kind_venue = TR_TO_KIND.get(tr_cd)
    if kind_venue is None:
        return _quarantine(
            QuarantineReason.UNKNOWN_TR_CODE,
            f"등록되지 않은 TR: {tr_cd}. TR_TO_KIND 에 추가하세요",
            tr_cd=tr_cd, payload=payload, received_at=received_at,
        )
    kind, venue = kind_venue

    # ▶ 통합시세의 shcode 는 `' U005930  '` 처럼 온다 (실측 2026-08-12)
    #   `.strip()` 만 하면 'U005930' 이 남아 Instrument Master 조회가 전부
    #   빗나가고, 수신은 되는데 **전건이 UNMAPPED_SYMBOL 로 격리**된다.
    #   격리는 조용하지 않지만(그건 잘 만들어 뒀다) 원인은 종목코드다.
    #   파서가 이미 같은 정규화를 갖고 있으므로 그것을 쓴다 - 여기에 두 벌째를
    #   만들면 언젠가 한쪽만 고쳐진다.
    symbol = normalize_symbol(payload.get("shcode")) or ""
    if not symbol:
        return _quarantine(
            QuarantineReason.MISSING_REQUIRED_FIELD,
            "shcode 가 없다",
            tr_cd=tr_cd, payload=payload, received_at=received_at,
        )

    instrument_id = resolve_instrument(symbol)
    if instrument_id is None:
        return _quarantine(
            QuarantineReason.UNMAPPED_SYMBOL,
            f"{symbol} 이 reference.instrument_symbols 에 없다. Instrument Master 를 먼저 수집하세요",
            tr_cd=tr_cd, payload=payload, received_at=received_at, symbol=symbol,
        )

    time_field = "chetime" if kind is DataKind.TICK else "hotime"
    try:
        event_time = resolve_event_time(payload.get(time_field, ""), received_at)
    except TimeResolutionError as e:
        return _quarantine(
            QuarantineReason.UNPARSEABLE_VALUE,
            f"{time_field}: {e}",
            tr_cd=tr_cd, payload=payload, received_at=received_at, symbol=symbol,
        )

    flags: list[QualityFlag] = []
    if is_trading_day is not None and not is_trading_day(event_time.astimezone(KST).date()):
        # 버리지 않는다. Calendar 가 틀렸을 수도 있고 임시 개장일 수도 있다.
        flags.append(QualityFlag.NON_TRADING_DAY)

    ref = InstrumentRef(
        instrument_id=instrument_id,
        provider=PROVIDER_LS,
        provider_symbol=symbol,
        market=_market_of(tr_cd, payload, venue),
    )
    times = ObservationTimes(
        event_time=event_time,
        received_at=received_at,
        observed_at=observed_at or received_at,
    )

    try:
        if kind is DataKind.TICK:
            return _tick(payload, tr_cd, ref, times, flags, trace_id)
        return _quote(payload, tr_cd, ref, times, flags, trace_id)
    except ValueError as e:
        return _quarantine(
            QuarantineReason.CONTRACT_VIOLATION,
            f"{tr_cd} 계약 위반: {e}",
            tr_cd=tr_cd, payload=payload, received_at=received_at, symbol=symbol,
        )


def _tick(payload, tr_cd, ref, times, flags, trace_id) -> MarketTick:
    price = _dec(payload.get("price"), "price", allow_blank=False)
    qty = _dec(payload.get("cvolume"), "cvolume", allow_blank=False)

    cgubun = str(payload.get("cgubun", "")).strip()
    side = CGUBUN_TO_SIDE.get(cgubun, Side.UNKNOWN)

    # 누적값은 공급자가 주는 것을 그대로 쓴다. 재계산하면 재접속 시점에 어긋난다.
    seq = str(payload.get("volume", "")).strip() or None

    return MarketTick(
        times=times,
        instrument=ref,
        tr_code=tr_cd,
        session_type=_session_from_status(payload),
        price=price,
        quantity=qty,
        side=side,
        cumulative_volume=_dec(payload.get("volume"), "volume"),
        cumulative_value=_dec(payload.get("value"), "value"),
        sequence_no=seq,
        source_event_id=build_source_event_id(
            provider=PROVIDER_LS,
            provider_symbol=ref.provider_symbol,
            event_time=times.event_time,
            # 같은 초에 여러 체결이 오므로 누적거래량을 Identity 에 넣는다.
            # 공급자 Sequence 가 따로 없어 값 조합이 유일성을 만든다.
            payload_identity=f"{seq or ''}|{price}|{qty}|{cgubun}",
        ),
        quality_flags=tuple(flags),
        trace_id=trace_id,
    )


def _session_from_status(payload) -> SessionType | None:
    donsi = str(payload.get("donsigubun", "")).strip()
    if donsi in DONSI_TO_SESSION:
        return DONSI_TO_SESSION[donsi]
    # status(장상태) 값이 문서에 없어 추정하지 않는다. 확인 후 매핑을 추가한다.
    return None


def _quote(payload, tr_cd, ref, times, flags, trace_id) -> MarketQuote:
    # ▶ 통합시세(UH1)의 잔량은 `unt_*` 다 - **이걸 틀리면 조용히 반쪽이 된다.**
    #   UH1 은 같은 메시지에 `bidrem{i}`(KRX 단독)와 `unt_bidrem{i}`(KRX+NXT
    #   합산)를 함께 싣는다. 가격 필드는 이름이 같아서, 잘못 읽어도 호가는
    #   멀쩡히 들어오고 **깊이만 KRX 쪽으로 줄어든다.** 예외도 경고도 없다.
    #   유효스프레드·호가불균형이 전부 한쪽만 본 값이 되는데 티가 안 난다.
    #   ls_unified_parser 가 `unt_*` 를 쓰는 것과 같은 이유다.
    rem = "unt_" if tr_cd in UNIFIED_TRS else ""
    bid_p, bid_s, ask_p, ask_s = [], [], [], []
    for i in range(1, QUOTE_DEPTH_MAX + 1):
        bp = _dec(payload.get(f"bidho{i}"), f"bidho{i}")
        bs = _dec(payload.get(f"{rem}bidrem{i}"), f"{rem}bidrem{i}")
        ap = _dec(payload.get(f"offerho{i}"), f"offerho{i}")
        asz = _dec(payload.get(f"{rem}offerrem{i}"), f"{rem}offerrem{i}")
        # 단계가 없으면(필드 부재) 거기서 멈춘다. 0 으로 채우지 않는다 -
        # 계약의 정렬 검사가 0 을 '단계 없음'으로 이미 처리한다.
        if bp is None and ap is None:
            break
        bid_p.append(bp if bp is not None else Decimal(0))
        bid_s.append(bs if bs is not None else Decimal(0))
        ask_p.append(ap if ap is not None else Decimal(0))
        ask_s.append(asz if asz is not None else Decimal(0))

    if not bid_p:
        raise ValueError("호가 단계가 하나도 없다 (bidho1/offerho1 확인)")

    seq = str(payload.get("volume", "")).strip() or None
    return MarketQuote(
        times=times,
        instrument=ref,
        tr_code=tr_cd,
        session_type=_session_from_status(payload),
        bid_prices=tuple(bid_p),
        bid_sizes=tuple(bid_s),
        ask_prices=tuple(ask_p),
        ask_sizes=tuple(ask_s),
        total_bid_size=_dec(payload.get(f"{rem}totbidrem"), f"{rem}totbidrem"),
        total_ask_size=_dec(payload.get(f"{rem}totofferrem"), f"{rem}totofferrem"),
        sequence_no=seq,
        source_event_id=build_source_event_id(
            provider=PROVIDER_LS,
            provider_symbol=ref.provider_symbol,
            event_time=times.event_time,
            payload_identity=f"{seq or ''}|{bid_p[0]}|{ask_p[0]}|{len(bid_p)}",
        ),
        quality_flags=tuple(flags),
        trace_id=trace_id,
    )


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

IID = UUID("10000000-0000-0000-0000-000000000001")


def _resolver(symbol: str) -> UUID | None:
    return IID if symbol in ("005930", "086520") else None


def _recv(h=10, m=30, s=0) -> datetime:
    return datetime(2026, 7, 30, h, m, s, tzinfo=KST)


S3 = {
    "shcode": "005930", "chetime": "103000", "price": "70000", "cvolume": "10",
    "cgubun": "+", "volume": "1234567", "value": "8600000000", "status": "00",
}
H1 = {
    "shcode": "005930", "hotime": "103000",
    "bidho1": "69900", "bidho2": "69800", "bidrem1": "100", "bidrem2": "200",
    "offerho1": "70000", "offerho2": "70100", "offerrem1": "150", "offerrem2": "50",
    "totbidrem": "300", "totofferrem": "200", "volume": "1234567",
}


def _check_time_resolution():
    # 정상 - 같은 날
    ev = resolve_event_time("103000", _recv())
    assert ev.astimezone(KST).date() == date(2026, 7, 30)
    assert ev.tzinfo == timezone.utc

    # 자정 넘겨 수신된 장 마감 Event -> 하루 앞으로 되돌린다
    ev2 = resolve_event_time("152959", datetime(2026, 7, 31, 0, 5, tzinfo=KST))
    assert ev2.astimezone(KST).date() == date(2026, 7, 30), ev2

    # 형식 오류
    for bad in ("", "10300", "abcdef", "253000", "106000"):
        try:
            resolve_event_time(bad, _recv())
            raise AssertionError(f"{bad!r} 가 통과했다")
        except TimeResolutionError:
            pass

    # naive received_at
    try:
        resolve_event_time("103000", datetime(2026, 7, 30, 10, 30))  # noqa: DTZ001 - intentionally invalid input
        raise AssertionError("naive received_at 이 통과했다")
    except TimeResolutionError:
        pass

    # 수신 지연이 한계를 넘으면 조합을 신뢰하지 않는다
    try:
        resolve_event_time("090000", datetime(2026, 7, 30, 23, 0, tzinfo=KST))
        raise AssertionError("14시간 지연이 통과했다")
    except TimeResolutionError:
        pass
    print("  시각 조합 (HHMMSS+거래일)   OK")


def _check_tick_normalize():
    r = normalize("S3_", S3, received_at=_recv(), resolve_instrument=_resolver)
    assert isinstance(r, MarketTick), r
    assert r.instrument.instrument_id == IID
    assert r.instrument.market is Market.KRX, "시계열 market 은 거래소 단위(KRX)다"
    assert r.price == Decimal(70000) and r.quantity == Decimal(10)
    assert r.side is Side.BUY
    assert r.cumulative_volume == Decimal(1234567)
    assert r.tr_code == "S3_"
    row = r.to_row()
    assert row["side"] == 1 and row["market"] == "KRX"

    # KOSDAQ TR 도 같은 계약으로 나온다
    r2 = normalize("K3_", {**S3, "shcode": "086520"}, received_at=_recv(),
                   resolve_instrument=_resolver)
    assert isinstance(r2, MarketTick) and r2.tr_code == "K3_"
    assert r2.instrument.market is Market.KRX
    print("  체결 정규화 (S3_/K3_)       OK")


def _check_quote_normalize():
    r = normalize("H1_", H1, received_at=_recv(), resolve_instrument=_resolver)
    assert isinstance(r, MarketQuote), r
    assert r.bid_prices == (Decimal(69900), Decimal(69800))
    assert r.ask_sizes == (Decimal(150), Decimal(50))
    assert r.total_bid_size == Decimal(300) and r.total_ask_size == Decimal(200)
    assert r.best_bid == Decimal(69900) and r.best_ask == Decimal(70000)
    assert r.spread == Decimal(100)
    assert abs(r.depth_imbalance - 0.2) < 1e-12
    row = r.to_row()
    assert len(row["bid_prices"]) == 2
    print("  호가 정규화 (H1_/HA_)       OK")


def _check_side_not_guessed():
    """cgubun 값이 문서에 없다. 모르는 값을 매수로 추정하지 않는다."""
    for cg, expected in (("+", Side.BUY), ("-", Side.SELL), ("", Side.UNKNOWN),
                         ("2", Side.UNKNOWN), ("X", Side.UNKNOWN)):
        r = normalize("S3_", {**S3, "cgubun": cg}, received_at=_recv(),
                      resolve_instrument=_resolver)
        assert isinstance(r, MarketTick) and r.side is expected, f"cgubun={cg!r}"
    print("  체결방향 추정 금지          OK")


def _check_quarantine():
    cases = [
        ("ZZ_", S3, QuarantineReason.UNKNOWN_TR_CODE),
        ("S3_", {k: v for k, v in S3.items() if k != "shcode"}, QuarantineReason.MISSING_REQUIRED_FIELD),
        ("S3_", {**S3, "shcode": "999999"}, QuarantineReason.UNMAPPED_SYMBOL),
        ("S3_", {**S3, "chetime": "zzz"}, QuarantineReason.UNPARSEABLE_VALUE),
        ("S3_", {**S3, "price": ""}, QuarantineReason.CONTRACT_VIOLATION),
        ("S3_", {**S3, "price": "abc"}, QuarantineReason.CONTRACT_VIOLATION),
        ("H1_", {k: v for k, v in H1.items() if not k.startswith(("bidho", "offerho"))},
         QuarantineReason.CONTRACT_VIOLATION),
    ]
    for tr, payload, reason in cases:
        r = normalize(tr, payload, received_at=_recv(), resolve_instrument=_resolver)
        assert isinstance(r, QuarantinedEvent), f"{tr} {reason} 가 Quarantine 되지 않았다"
        assert r.reason is reason, f"{tr}: {r.reason} != {reason}"
        # 원본이 보존돼야 재처리할 수 있다
        assert r.raw_payload == payload
    print(f"  Quarantine {len(cases)}종           OK")


def _check_non_trading_day_flag():
    """비거래일 Event 는 버리지 않고 Flag 로 남긴다."""
    weekend = datetime(2026, 8, 1, 10, 30, tzinfo=KST)  # 토요일
    r = normalize("S3_", S3, received_at=weekend, resolve_instrument=_resolver,
                  is_trading_day=lambda d: d.weekday() < 5)
    assert isinstance(r, MarketTick), r
    assert QualityFlag.NON_TRADING_DAY in r.quality_flags
    assert r.to_row()["raw_flags"]["quality_flags"] == ["NON_TRADING_DAY"]

    # 거래일이면 Flag 가 없다
    r2 = normalize("S3_", S3, received_at=_recv(), resolve_instrument=_resolver,
                   is_trading_day=lambda d: d.weekday() < 5)
    assert r2.quality_flags == ()
    print("  비거래일 Flag               OK")


def _check_padded_symbol_is_normalised():
    """통합시세 shcode 는 `' U005930  '` 로 온다 (실측 2026-08-12).

    `.strip()` 만 하면 `U005930` 이 남아 Instrument Master 조회가 전부 빗나가고,
    수신은 되는데 **전건이 UNMAPPED_SYMBOL 로 격리**된다. 격리 자체는 조용하지
    않지만, 원인이 종목코드라는 것은 payload 를 열어봐야 안다.
    """
    for raw in (" U005930  ", "U005930", "005930", " 005930 "):
        r = normalize("US3", {**S3, "shcode": raw, "exchname": "KRX"},
                      received_at=_recv(), resolve_instrument=_resolver)
        assert isinstance(r, MarketTick), f"{raw!r} 이 격리됐다: {r}"
        assert r.instrument.provider_symbol == "005930", (raw, r.instrument)
    # 숫자가 모자라면 여전히 격리된다 - 아무 값이나 통과시키지 않는다
    bad = normalize("US3", {**S3, "shcode": "U12"}, received_at=_recv(),
                    resolve_instrument=_resolver)
    assert isinstance(bad, QuarantinedEvent), bad
    print("  패딩 종목코드 정규화        OK")


def _check_unified_carries_nxt_and_unified_depth():
    """통합시세(US3/UH1)가 **NXT 를 살려 오는가, 그리고 깊이가 통합인가.**

    ▶ 왜 이 둘인가 (2026-08-11)
      거래소 단독 TR(S3_/H1_)만 받던 동안 NXT 체결 하루 3,842,266행 - 전체의
      25% - 이 통째로 빠져 있었다. 시장의 4분의 3만 보고 찾은 알파였다.

      그리고 UH1 은 같은 메시지에 `bidrem{i}`(KRX 단독)와 `unt_bidrem{i}`
      (KRX+NXT 합산)를 **함께** 싣는다. 가격 필드는 이름이 같아서 잔량을 잘못
      읽어도 예외도 경고도 없다 - 호가는 멀쩡히 들어오고 **깊이만 조용히
      반쪽**이 된다. 조용한 오차라서 자체 점검으로 고정한다.
    """
    us3 = {**S3, "exchname": "NXT"}
    r = normalize("US3", us3, received_at=_recv(), resolve_instrument=_resolver)
    assert isinstance(r, MarketTick), r
    assert r.instrument.market is Market.NXT, "NXT 체결이 KRX 로 들어갔다"
    assert normalize("US3", {**S3, "exchname": "KRX"}, received_at=_recv(),
                     resolve_instrument=_resolver).instrument.market is Market.KRX
    # 거래소 미상은 KRX 로 본다 - 모르는 것을 새 거래소로 몰면 없던 물량이 생긴다
    assert normalize("US3", S3, received_at=_recv(),
                     resolve_instrument=_resolver).instrument.market is Market.KRX

    # 통합 호가: unt_* 를 읽어야 한다. KRX 단독 잔량과 다른 값을 넣어 구분한다.
    uh1 = {**H1, "unt_bidrem1": "900", "unt_bidrem2": "800",
           "unt_offerrem1": "700", "unt_offerrem2": "600",
           "unt_totbidrem": "1700", "unt_totofferrem": "1300"}
    q = normalize("UH1", uh1, received_at=_recv(), resolve_instrument=_resolver)
    assert isinstance(q, MarketQuote), q
    assert q.bid_sizes[0] == 900 and q.ask_sizes[0] == 700, \
        f"KRX 단독 잔량을 읽었다 - 깊이가 반쪽이다: {q.bid_sizes} {q.ask_sizes}"
    assert q.total_bid_size == 1700 and q.total_ask_size == 1300, \
        "총잔량이 KRX 단독이다"

    # 거래소 단독 TR 은 예전 필드를 그대로 읽는다 (되돌아가도 깨지지 않는다)
    q2 = normalize("H1_", uh1, received_at=_recv(), resolve_instrument=_resolver)
    assert q2.bid_sizes[0] == 100, f"단독 TR 이 통합 잔량을 읽었다: {q2.bid_sizes}"
    print("  통합시세 NXT·통합깊이       OK")


def _check_idempotent_source_event_id():
    """같은 payload 를 두 번 받으면 같은 source_event_id 다(재접속 중복 방지)."""
    a = normalize("S3_", S3, received_at=_recv(), resolve_instrument=_resolver)
    b = normalize("S3_", S3, received_at=_recv(10, 30, 5), resolve_instrument=_resolver)
    assert a.source_event_id == b.source_event_id, "수신 시각이 달라도 같아야 한다"

    # 값이 다르면 다른 ID
    c = normalize("S3_", {**S3, "price": "70100"}, received_at=_recv(),
                  resolve_instrument=_resolver)
    assert a.source_event_id != c.source_event_id
    d = normalize("S3_", {**S3, "volume": "1234568"}, received_at=_recv(),
                  resolve_instrument=_resolver)
    assert a.source_event_id != d.source_event_id
    print("  멱등 source_event_id        OK")


if __name__ == "__main__":
    print(f"{ADAPTER_VERSION} 자체 점검")
    _check_time_resolution()
    _check_tick_normalize()
    _check_quote_normalize()
    _check_side_not_guessed()
    _check_quarantine()
    _check_non_trading_day_flag()
    _check_padded_symbol_is_normalised()
    _check_unified_carries_nxt_and_unified_depth()
    _check_idempotent_source_event_id()
    print("Adapter 7개 영역 통과")
