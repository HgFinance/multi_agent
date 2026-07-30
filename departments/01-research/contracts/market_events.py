#!/usr/bin/env python3
"""Sprint J0: 공통 instrument_id / 시간 / Event Envelope / 정규 Market Event 계약.

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 4.1, 4.2, 6.2, 8.1, 9(Sprint J0)
      docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F04(Market Event 정규화)
      timescaledb/migrations/001_initial_market_data.sql (market.market_ticks/market_quotes)

여기가 공급자 Payload와 시계열 원장의 경계다. LS Payload는 이 계층을 통과하지 않으면
TimescaleDB에 들어갈 수 없다. Adapter가 바뀌어도 하위 Feature Schema는 유지된다(F04 완료 조건).

세 가지를 이 계층에서 못 하게 막는다.
  - 종목코드 문자열을 영구 식별자로 쓰기 (가이드 3.3, 8.1 - instrument_id만 영구 ID다)
  - 가격·수량에 float 사용 (가이드 4.2, 7.1 - Decimal만)
  - 시각 하나로 뭉치기 (가이드 4.2 - event/received/observed 를 분리해야 PIT 재현이 된다)

Field 이름과 제약은 마이그레이션과 1:1로 맞춘다. 한쪽만 바꾸면 적재가 조용히 깨진다.

자체 점검: python departments/01-research/contracts/market_events.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 마이그레이션의 schema_version 은 integer 다(numeric 아님, 문자열 아님).
# 계약을 깨는 변경에서만 올린다. 필드 추가는 올리지 않는다.
SCHEMA_VERSION = 1

# 사람이 읽는 계약 식별자. Event Envelope 의 schema_version 문자열로 나간다(가이드 6.2).
CONTRACT_ID = "research-market-events-v1"

# numeric(30, 10) 과 정확히 대응시킨다. 초과하면 DB가 아니라 여기서 막는다.
Price = Annotated[Decimal, Field(ge=0, max_digits=30, decimal_places=10)]
Size = Annotated[Decimal, Field(ge=0, max_digits=30, decimal_places=10)]
Cumulative = Annotated[Decimal, Field(ge=0, max_digits=38, decimal_places=10)]

# 10단계 호가. 마이그레이션의 cardinality between 1 and 10 과 같은 범위다.
QUOTE_DEPTH_MAX = 10

# 마이그레이션 check (received_at >= event_time - interval '1 day') 와 동일.
# 공급자 시각이 우리보다 미래로 찍히는 경우를 이만큼만 허용한다.
MAX_CLOCK_SKEW = timedelta(days=1)


class Side(IntEnum):
    """체결 방향. 마이그레이션 check (side in (-1, 0, 1)) 과 같은 값이다.

    공급자마다 의미가 다르므로 여기서 정규화한다. UNKNOWN 을 매수로 추정하지 않는다 -
    추정값과 관측값을 섞으면 Microstructure Feature 가 조용히 오염된다.
    """

    SELL = -1
    UNKNOWN = 0
    BUY = 1


class Market(StrEnum):
    """Venue. LS 와 의미가 다른 가격 Source 를 같은 price 에 섞지 않기 위한 구분(가이드 3.3)."""

    KRX = "KRX"
    NXT = "NXT"


class SessionType(StrEnum):
    """장 구간. 동시호가와 정규장 체결을 같은 통계에 넣지 않기 위해 필요하다(가이드 8.2)."""

    PRE_AUCTION = "PRE_AUCTION"
    REGULAR = "REGULAR"
    CLOSING_AUCTION = "CLOSING_AUCTION"
    AFTER_HOURS = "AFTER_HOURS"
    HALTED = "HALTED"


class TradingStatus(StrEnum):
    """거래상태. HALTED/SUSPENDED 종목은 Signal 과 신규 주문에서 제외된다(F02 완료 조건)."""

    NORMAL = "NORMAL"
    HALTED = "HALTED"
    SUSPENDED = "SUSPENDED"
    DELISTED = "DELISTED"


class QualityFlag(StrEnum):
    """정규화 중 발견한 이상. 값을 버리지 않고 Flag 로 남긴다(F04 - 중복·역전·비정상 Flag).

    Flag 가 붙은 Row 도 적재한다. 버리면 나중에 왜 구멍이 났는지 재현할 수 없다.
    소비 쪽에서 Flag 를 보고 제외 여부를 정한다.
    """

    DUPLICATE = "DUPLICATE"
    SEQUENCE_GAP = "SEQUENCE_GAP"
    TIMESTAMP_REVERSED = "TIMESTAMP_REVERSED"
    CLOCK_SKEW = "CLOCK_SKEW"
    PRICE_OUT_OF_BAND = "PRICE_OUT_OF_BAND"
    CROSSED_QUOTE = "CROSSED_QUOTE"
    STALE = "STALE"
    CORRECTED = "CORRECTED"
    # Calendar 가 비거래일로 판정한 날에 Event 가 들어왔다. 버리지 않는 이유 -
    # Calendar 가 관측 역산이라 틀릴 수 있고(임시 개장), 실제 Event 가 더 강한 증거다.
    # 소비 쪽에서 Flag 를 보고 제외 여부를 정한다.
    NON_TRADING_DAY = "NON_TRADING_DAY"


class QuarantineReason(StrEnum):
    """정규화 자체가 불가능한 경우. 재처리 대상으로 격리한다(F04 - Quarantine 과 재처리).

    Flag 와 다르다. Flag 는 "적재하되 표시", Quarantine 은 "적재 불가, 원본 보존".
    """

    UNMAPPED_SYMBOL = "UNMAPPED_SYMBOL"
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNPARSEABLE_VALUE = "UNPARSEABLE_VALUE"
    UNKNOWN_TR_CODE = "UNKNOWN_TR_CODE"
    CONTRACT_VIOLATION = "CONTRACT_VIOLATION"


class Base(BaseModel):
    """공통 설정. 계약 밖 필드를 조용히 통과시키지 않는다."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class InstrumentRef(Base):
    """종목 참조. instrument_id 가 영구 식별자이고 공급자 코드는 Alias 다(가이드 8.1).

    provider_symbol 은 추적·디버깅용으로만 들고 다닌다. 이것으로 Join 하지 않는다 -
    Ticker 변경 후에도 동일 Instrument 를 추적해야 한다(F02 완료 조건).
    """

    instrument_id: UUID
    provider: str = Field(min_length=1, max_length=32)
    provider_symbol: str = Field(min_length=1, max_length=32)
    market: Market


class ObservationTimes(Base):
    """가이드 4.2의 시각 규칙. Backtest 는 event_time 이 아니라 observed_at 을 본다.

    Reference/Policy 의 valid_from/valid_to 는 시계열 Observation 이 아니라
    reference 스키마의 관심사이므로 여기 두지 않는다.
    published_at 은 문서(공시·뉴스) Observation 용이라 Market Event 에는 없다.
    """

    event_time: datetime
    received_at: datetime
    observed_at: datetime

    @model_validator(mode="after")
    def _check_order(self):
        for name in ("event_time", "received_at", "observed_at"):
            if getattr(self, name).tzinfo is None:
                raise ValueError(f"{name} 는 tz-aware 여야 한다. UTC 로 변환해서 넣으세요")

        # 마이그레이션 check (observed_at >= received_at)
        if self.observed_at < self.received_at:
            raise ValueError(
                f"observed_at({self.observed_at}) < received_at({self.received_at}). "
                "검증 완료 시각이 수신 시각보다 이를 수 없다"
            )
        # 마이그레이션 check (received_at >= event_time - interval '1 day')
        if self.received_at < self.event_time - MAX_CLOCK_SKEW:
            raise ValueError(
                f"received_at({self.received_at}) 이 event_time({self.event_time}) 보다 "
                f"{MAX_CLOCK_SKEW} 이상 이르다. 공급자 시각 파싱을 확인하세요"
            )
        return self

    @property
    def ingest_latency(self) -> timedelta:
        """공급자 Event 시각 대비 수신 지연. DQ 의 수신 지연 감시 입력이다(가이드 8.2)."""
        return self.received_at - self.event_time


def build_source_event_id(
    *,
    provider: str,
    provider_symbol: str,
    event_time: datetime,
    payload_identity: str,
) -> str:
    """재접속·재수집 중복 방지용 멱등 ID(가이드 8.1).

    마이그레이션의 primary key (event_time, source_event_id) 가 이 값으로 멱등성을 만든다.
    같은 Event 를 두 번 받으면 같은 ID 가 나와야 하므로 수신 시각이나 uuid4 를 섞지 않는다.

    payload_identity 는 공급자 Sequence 가 있으면 그것을, 없으면 값 조합을 넣는다.
    """
    if not payload_identity:
        raise ValueError("payload_identity 가 비었다. Sequence 나 값 조합을 넣으세요")

    material = "\x1f".join(
        (provider, provider_symbol, event_time.astimezone(timezone.utc).isoformat(), payload_identity)
    )
    return hashlib.sha256(material.encode()).hexdigest()[:40]


class MarketTick(Base):
    """정규화된 체결. market.market_ticks 와 1:1 대응한다.

    cumulative_volume/value 는 공급자가 주는 누적값이다. 우리가 재계산하지 않는다 -
    재계산하면 장중 재접속 시점에 값이 어긋난다.
    """

    times: ObservationTimes
    instrument: InstrumentRef
    tr_code: str | None = Field(default=None, max_length=16)
    session_type: SessionType | None = None
    price: Price
    quantity: Size
    side: Side = Side.UNKNOWN
    cumulative_volume: Cumulative | None = None
    cumulative_value: Cumulative | None = None
    sequence_no: str | None = Field(default=None, max_length=64)
    source_event_id: str = Field(min_length=8, max_length=64)
    correction_code: str | None = Field(default=None, max_length=16)
    trading_status: TradingStatus | None = None
    quality_flags: tuple[QualityFlag, ...] = ()
    schema_version: int = Field(default=SCHEMA_VERSION, gt=0)
    trace_id: UUID | None = None

    def to_row(self) -> dict[str, Any]:
        """market.market_ticks INSERT 용 dict. Repository 가 이것만 받는다."""
        return {
            "event_time": self.times.event_time,
            "received_at": self.times.received_at,
            "observed_at": self.times.observed_at,
            "instrument_id": self.instrument.instrument_id,
            "provider": self.instrument.provider,
            "tr_code": self.tr_code,
            "market": self.instrument.market.value,
            "session_type": self.session_type.value if self.session_type else None,
            "price": self.price,
            "quantity": self.quantity,
            "side": int(self.side),
            "cumulative_volume": self.cumulative_volume,
            "cumulative_value": self.cumulative_value,
            "sequence_no": self.sequence_no,
            "source_event_id": self.source_event_id,
            "correction_code": self.correction_code,
            "trading_status": self.trading_status.value if self.trading_status else None,
            # 마이그레이션이 raw_flags jsonb not null default '{}' 이므로 dict 로 넣는다.
            "raw_flags": {"quality_flags": [f.value for f in self.quality_flags]},
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
        }


class MarketQuote(Base):
    """정규화된 10단계 호가. market.market_quotes 와 1:1 대응한다.

    Raw Payload JSON 만 저장해 Query 때마다 파싱하는 방식은 쓰지 않는다(가이드 4.1).
    best/mid/spread/depth_imbalance 는 파생값이며 여기서 결정론적으로 계산한다 -
    소비 쪽마다 다르게 계산하면 같은 Snapshot 이 다른 값을 갖는다.
    """

    times: ObservationTimes
    instrument: InstrumentRef
    tr_code: str | None = Field(default=None, max_length=16)
    session_type: SessionType | None = None
    bid_prices: tuple[Price, ...] = Field(min_length=1, max_length=QUOTE_DEPTH_MAX)
    bid_sizes: tuple[Size, ...] = Field(min_length=1, max_length=QUOTE_DEPTH_MAX)
    ask_prices: tuple[Price, ...] = Field(min_length=1, max_length=QUOTE_DEPTH_MAX)
    ask_sizes: tuple[Size, ...] = Field(min_length=1, max_length=QUOTE_DEPTH_MAX)
    total_bid_size: Cumulative | None = None
    total_ask_size: Cumulative | None = None
    sequence_no: str | None = Field(default=None, max_length=64)
    source_event_id: str = Field(min_length=8, max_length=64)
    trading_status: TradingStatus | None = None
    quality_flags: tuple[QualityFlag, ...] = ()
    schema_version: int = Field(default=SCHEMA_VERSION, gt=0)
    trace_id: UUID | None = None

    @model_validator(mode="after")
    def _check_book(self):
        # 마이그레이션 check (cardinality(bid_prices) = cardinality(bid_sizes)) 등
        if len(self.bid_prices) != len(self.bid_sizes):
            raise ValueError("bid_prices 와 bid_sizes 길이가 다르다")
        if len(self.ask_prices) != len(self.ask_sizes):
            raise ValueError("ask_prices 와 ask_sizes 길이가 다르다")

        # 호가 단계 정렬 검증(가이드 8.2). 매수는 내림차순, 매도는 오름차순이다.
        # 0 은 "단계 없음"을 의미하는 공급자가 있어서 정렬 검사에서 제외한다.
        bids = [p for p in self.bid_prices if p > 0]
        asks = [p for p in self.ask_prices if p > 0]
        if bids != sorted(bids, reverse=True):
            raise ValueError(f"bid_prices 가 내림차순이 아니다: {bids}")
        if asks != sorted(asks):
            raise ValueError(f"ask_prices 가 오름차순이 아니다: {asks}")

        # Bid/Ask Cross 는 Flag 로 남기고 막지 않는다(가이드 8.2). 동시호가 구간에
        # 실제로 교차가 관측되므로 계약 위반으로 처리하면 정상 데이터를 버린다.
        # 단 마이그레이션이 best_ask >= best_bid 를 강제하므로 to_row 에서 조정한다.
        return self

    @property
    def best_bid(self) -> Decimal | None:
        candidates = [p for p in self.bid_prices if p > 0]
        return max(candidates) if candidates else None

    @property
    def best_ask(self) -> Decimal | None:
        candidates = [p for p in self.ask_prices if p > 0]
        return min(candidates) if candidates else None

    @property
    def is_crossed(self) -> bool:
        b, a = self.best_bid, self.best_ask
        return b is not None and a is not None and a < b

    @property
    def spread(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else a - b

    @property
    def mid_price(self) -> Decimal | None:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else (a + b) / Decimal(2)

    @property
    def depth_imbalance(self) -> float | None:
        """(bid - ask) / (bid + ask). 마이그레이션이 -1~1 을 강제한다.

        double precision Column 이므로 여기서만 float 를 쓴다. 가격·수량은 Decimal 이다.
        """
        bid = sum(self.bid_sizes)
        ask = sum(self.ask_sizes)
        total = bid + ask
        return None if total == 0 else float((bid - ask) / total)

    def to_row(self) -> dict[str, Any]:
        """market.market_quotes INSERT 용 dict.

        교차 호가는 best_bid/best_ask 를 null 로 내린다 - 마이그레이션이
        best_ask >= best_bid 를 강제하고, 교차 상태의 best 값은 파생 지표로
        쓸 수 없기 때문이다. 원본 배열은 그대로 남으므로 정보는 잃지 않는다.
        """
        crossed = self.is_crossed
        flags = list(self.quality_flags)
        if crossed and QualityFlag.CROSSED_QUOTE not in flags:
            flags.append(QualityFlag.CROSSED_QUOTE)

        return {
            "event_time": self.times.event_time,
            "received_at": self.times.received_at,
            "observed_at": self.times.observed_at,
            "instrument_id": self.instrument.instrument_id,
            "provider": self.instrument.provider,
            "tr_code": self.tr_code,
            "market": self.instrument.market.value,
            "session_type": self.session_type.value if self.session_type else None,
            "bid_prices": list(self.bid_prices),
            "bid_sizes": list(self.bid_sizes),
            "ask_prices": list(self.ask_prices),
            "ask_sizes": list(self.ask_sizes),
            "total_bid_size": self.total_bid_size,
            "total_ask_size": self.total_ask_size,
            "best_bid": None if crossed else self.best_bid,
            "best_ask": None if crossed else self.best_ask,
            "mid_price": None if crossed else self.mid_price,
            "spread": None if crossed else self.spread,
            "depth_imbalance": self.depth_imbalance,
            "sequence_no": self.sequence_no,
            "source_event_id": self.source_event_id,
            "trading_status": self.trading_status.value if self.trading_status else None,
            "raw_flags": {"quality_flags": [f.value for f in flags]},
            "schema_version": self.schema_version,
            "trace_id": self.trace_id,
        }


class QuarantinedEvent(Base):
    """정규화 실패 Event. 원본을 보존해서 재처리한다(F04 - Quarantine 과 재처리).

    Payload 를 그대로 들고 있는 유일한 계약이다. 정상 경로에서는 Raw Payload 를
    Domain 객체에 담지 않는다.
    """

    reason: QuarantineReason
    detail: str = Field(min_length=1, max_length=512)
    provider: str = Field(min_length=1, max_length=32)
    tr_code: str | None = Field(default=None, max_length=16)
    provider_symbol: str | None = Field(default=None, max_length=32)
    received_at: datetime
    raw_payload: dict[str, Any]
    trace_id: UUID | None = None

    _MAX_PAYLOAD_BYTES = 64 * 1024

    @model_validator(mode="after")
    def _check_payload(self):
        size = len(json.dumps(self.raw_payload, ensure_ascii=False, default=str).encode())
        if size > self._MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"raw_payload {size}B > {self._MAX_PAYLOAD_BYTES}B. "
                "Object Storage 에 올리고 경로만 남기세요"
            )
        return self


class ResearchEventEnvelope(Base):
    """본부 간 전달 Event 봉투. 가이드 6.2의 필수 필드를 그대로 따른다.

    트레이딩본부의 EventEnvelope(departments/02-trading/contracts/contracts.py)와
    필드가 다르다 - 그쪽은 event_time/idempotency_key, 이쪽은 occurred_at/producer/
    payload_ref 다. 전사 contracts/ 경계가 생길 때 통합 대상이며, 그때까지 본부
    경계를 넘어 import 하지 않는다(REPOSITORY_DEPARTMENT_STRUCTURE 8절 의존성 방향).

    대용량 문서·Dataset 을 Body 에 넣지 않는다(가이드 6.2). payload_ref 만 넣는다.
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(pattern=r"^[a-z_]+\.[a-z_]+\.v\d+$")
    schema_version: str = Field(default=CONTRACT_ID, min_length=1)
    occurred_at: datetime
    observed_at: datetime
    producer: str = Field(min_length=1, max_length=64)
    trace_id: UUID
    payload_ref: str | None = Field(default=None, max_length=512)
    payload: dict[str, Any] = Field(default_factory=dict)

    _MAX_PAYLOAD_BYTES = 16 * 1024

    @model_validator(mode="after")
    def _check(self):
        if self.observed_at < self.occurred_at:
            raise ValueError("observed_at 이 occurred_at 보다 이를 수 없다")
        size = len(json.dumps(self.payload, ensure_ascii=False, default=str).encode())
        if size > self._MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload {size}B > {self._MAX_PAYLOAD_BYTES}B. payload_ref 를 쓰세요"
            )
        return self


# ---------------------------------------------------------------------------
# 자체 점검 - 계약 6개 영역
# ---------------------------------------------------------------------------

def _utc(y=2026, mo=7, d=30, h=1, mi=0, s=0, us=0) -> datetime:
    return datetime(y, mo, d, h, mi, s, us, tzinfo=timezone.utc)


def _instrument() -> InstrumentRef:
    return InstrumentRef(
        instrument_id=UUID("10000000-0000-0000-0000-000000000001"),
        provider="LS",
        provider_symbol="005930",
        market=Market.KRX,
    )


def _times(**kw) -> ObservationTimes:
    base = {
        "event_time": _utc(),
        "received_at": _utc(us=10_000),
        "observed_at": _utc(us=20_000),
    }
    base.update(kw)
    return ObservationTimes(**base)


def _check_times():
    t = _times()
    assert t.ingest_latency == timedelta(microseconds=10_000)

    # observed_at < received_at 은 막는다
    try:
        _times(observed_at=_utc(us=5_000))
        raise AssertionError("observed_at < received_at 이 통과했다")
    except ValueError:
        pass

    # naive datetime 은 막는다
    try:
        ObservationTimes(
            event_time=datetime(2026, 7, 30, 1, 0), received_at=_utc(), observed_at=_utc()
        )
        raise AssertionError("naive datetime 이 통과했다")
    except ValueError:
        pass

    # 1일 넘는 역방향 skew 는 막는다
    try:
        _times(event_time=_utc(d=28), received_at=_utc(d=26), observed_at=_utc(d=26))
        raise AssertionError("clock skew 초과가 통과했다")
    except ValueError:
        pass
    print("  시각 규칙            OK")


def _check_idempotency():
    a = build_source_event_id(
        provider="LS", provider_symbol="005930", event_time=_utc(), payload_identity="seq-1"
    )
    b = build_source_event_id(
        provider="LS", provider_symbol="005930", event_time=_utc(), payload_identity="seq-1"
    )
    assert a == b, "같은 Event 가 다른 source_event_id 를 냈다"

    # 다른 시간대 표기로 같은 순간을 주면 같은 ID 여야 한다(재접속 중복 방지)
    kst = timezone(timedelta(hours=9))
    c = build_source_event_id(
        provider="LS",
        provider_symbol="005930",
        event_time=_utc().astimezone(kst),
        payload_identity="seq-1",
    )
    assert a == c, "동일 순간의 다른 tz 표기가 다른 ID 를 냈다"

    d = build_source_event_id(
        provider="LS", provider_symbol="005930", event_time=_utc(), payload_identity="seq-2"
    )
    assert a != d, "다른 Event 가 같은 ID 를 냈다"

    try:
        build_source_event_id(
            provider="LS", provider_symbol="005930", event_time=_utc(), payload_identity=""
        )
        raise AssertionError("빈 payload_identity 가 통과했다")
    except ValueError:
        pass
    print("  멱등 source_event_id  OK")


def _tick(**kw) -> MarketTick:
    base = {
        "times": _times(),
        "instrument": _instrument(),
        "tr_code": "S3_",
        "session_type": SessionType.REGULAR,
        "price": Decimal("70000"),
        "quantity": Decimal("10"),
        "side": Side.BUY,
        "source_event_id": build_source_event_id(
            provider="LS", provider_symbol="005930", event_time=_utc(), payload_identity="seq-1"
        ),
    }
    base.update(kw)
    return MarketTick(**base)


def _check_tick():
    t = _tick()
    row = t.to_row()
    assert row["side"] == 1 and isinstance(row["side"], int)
    assert row["market"] == "KRX"
    assert row["price"] == Decimal("70000")
    assert row["raw_flags"] == {"quality_flags": []}
    assert row["schema_version"] == SCHEMA_VERSION

    # 마이그레이션 Column 과 이름이 어긋나면 적재가 깨진다
    expected = {
        "event_time", "received_at", "observed_at", "instrument_id", "provider", "tr_code",
        "market", "session_type", "price", "quantity", "side", "cumulative_volume",
        "cumulative_value", "sequence_no", "source_event_id", "correction_code",
        "trading_status", "raw_flags", "schema_version", "trace_id",
    }
    assert set(row) == expected, f"Column 불일치: {set(row) ^ expected}"

    # 음수 가격은 막는다 (마이그레이션 check price >= 0)
    try:
        _tick(price=Decimal("-1"))
        raise AssertionError("음수 가격이 통과했다")
    except ValueError:
        pass

    # float 를 넣으면 Decimal 로 강제되며 소수점 초과는 막힌다
    try:
        _tick(price=Decimal("1.00000000001"))
        raise AssertionError("decimal_places 초과가 통과했다")
    except ValueError:
        pass

    # 계약 밖 필드는 막는다
    try:
        MarketTick(**{**_tick().model_dump(), "unexpected": 1})
        raise AssertionError("extra 필드가 통과했다")
    except ValueError:
        pass

    # Flag 는 값을 버리지 않고 표시만 한다
    f = _tick(quality_flags=(QualityFlag.CORRECTED,), correction_code="C1")
    assert f.to_row()["raw_flags"]["quality_flags"] == ["CORRECTED"]
    print("  MarketTick            OK")


def _quote(**kw) -> MarketQuote:
    base = {
        "times": _times(),
        "instrument": _instrument(),
        "tr_code": "H1_",
        "bid_prices": (Decimal("69900"), Decimal("69800")),
        "bid_sizes": (Decimal("100"), Decimal("200")),
        "ask_prices": (Decimal("70000"), Decimal("70100")),
        "ask_sizes": (Decimal("150"), Decimal("50")),
        "source_event_id": build_source_event_id(
            provider="LS", provider_symbol="005930", event_time=_utc(), payload_identity="h-1"
        ),
    }
    base.update(kw)
    return MarketQuote(**base)


def _check_quote():
    q = _quote()
    assert q.best_bid == Decimal("69900")
    assert q.best_ask == Decimal("70000")
    assert q.spread == Decimal("100")
    assert q.mid_price == Decimal("69950")
    assert q.is_crossed is False
    # (300 - 200) / 500
    assert abs(q.depth_imbalance - 0.2) < 1e-12

    row = q.to_row()
    assert -1.0 <= row["depth_imbalance"] <= 1.0
    expected = {
        "event_time", "received_at", "observed_at", "instrument_id", "provider", "tr_code",
        "market", "session_type", "bid_prices", "bid_sizes", "ask_prices", "ask_sizes",
        "total_bid_size", "total_ask_size", "best_bid", "best_ask", "mid_price", "spread",
        "depth_imbalance", "sequence_no", "source_event_id", "trading_status", "raw_flags",
        "schema_version", "trace_id",
    }
    assert set(row) == expected, f"Column 불일치: {set(row) ^ expected}"

    # 길이 불일치는 막는다
    try:
        _quote(bid_sizes=(Decimal("100"),))
        raise AssertionError("bid 길이 불일치가 통과했다")
    except ValueError:
        pass

    # 10단계 초과는 막는다
    try:
        _quote(
            bid_prices=tuple(Decimal(70000 - i) for i in range(11)),
            bid_sizes=tuple(Decimal("1") for _ in range(11)),
        )
        raise AssertionError("11단계가 통과했다")
    except ValueError:
        pass

    # 정렬이 깨지면 막는다
    try:
        _quote(bid_prices=(Decimal("69800"), Decimal("69900")))
        raise AssertionError("bid 오름차순이 통과했다")
    except ValueError:
        pass

    # 0 단계는 정렬 검사에서 제외한다(공급자가 빈 단계를 0 으로 채운다)
    padded = _quote(
        bid_prices=(Decimal("69900"), Decimal("0")), bid_sizes=(Decimal("100"), Decimal("0"))
    )
    assert padded.best_bid == Decimal("69900")
    print("  MarketQuote           OK")


def _check_crossed_quote():
    """동시호가 교차는 버리지 않고 Flag + best null 로 처리한다."""
    q = _quote(
        bid_prices=(Decimal("70100"),),
        bid_sizes=(Decimal("100"),),
        ask_prices=(Decimal("70000"),),
        ask_sizes=(Decimal("100"),),
        session_type=SessionType.CLOSING_AUCTION,
    )
    assert q.is_crossed is True
    row = q.to_row()
    # 마이그레이션 check (best_ask >= best_bid) 를 위반하지 않아야 한다
    assert row["best_bid"] is None and row["best_ask"] is None
    assert row["spread"] is None and row["mid_price"] is None
    assert "CROSSED_QUOTE" in row["raw_flags"]["quality_flags"]
    # 원본 배열은 그대로 남는다
    assert row["bid_prices"] == [Decimal("70100")]
    print("  교차 호가 처리        OK")


def _check_quarantine_and_envelope():
    q = QuarantinedEvent(
        reason=QuarantineReason.UNMAPPED_SYMBOL,
        detail="005930 이 instrument_symbols 에 없다",
        provider="LS",
        tr_code="S3_",
        provider_symbol="005930",
        received_at=_utc(),
        raw_payload={"body": "..."},
    )
    assert q.reason is QuarantineReason.UNMAPPED_SYMBOL

    try:
        QuarantinedEvent(
            reason=QuarantineReason.UNPARSEABLE_VALUE,
            detail="too big",
            provider="LS",
            received_at=_utc(),
            raw_payload={"blob": "x" * 70_000},
        )
        raise AssertionError("64KB 초과 payload 가 통과했다")
    except ValueError:
        pass

    e = ResearchEventEnvelope(
        event_type="market.snapshot.v1",
        occurred_at=_utc(),
        observed_at=_utc(us=1),
        producer="market-collector",
        trace_id=uuid4(),
        payload_ref="market-archive-private/2026/07/30/005930.parquet",
    )
    assert e.schema_version == CONTRACT_ID

    # 가이드 6.2의 event_type 형식을 강제한다
    try:
        ResearchEventEnvelope(
            event_type="MarketSnapshot",
            occurred_at=_utc(),
            observed_at=_utc(),
            producer="x",
            trace_id=uuid4(),
        )
        raise AssertionError("잘못된 event_type 이 통과했다")
    except ValueError:
        pass

    # 본문 대신 참조를 쓰게 강제한다
    try:
        ResearchEventEnvelope(
            event_type="research.document.v1",
            occurred_at=_utc(),
            observed_at=_utc(),
            producer="x",
            trace_id=uuid4(),
            payload={"text": "x" * 20_000},
        )
        raise AssertionError("16KB 초과 payload 가 통과했다")
    except ValueError:
        pass
    print("  Quarantine/Envelope   OK")


if __name__ == "__main__":
    print(f"{CONTRACT_ID} (schema_version={SCHEMA_VERSION}) 자체 점검")
    _check_times()
    _check_idempotency()
    _check_tick()
    _check_quote()
    _check_crossed_quote()
    _check_quarantine_and_envelope()
    print("계약 6개 영역 통과")
