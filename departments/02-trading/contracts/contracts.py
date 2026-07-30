#!/usr/bin/env python3
"""Sprint D0: Order Intent / Risk Decision / Event Envelope 계약.

소유: 도현 (트레이딩본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 3.1, 4.2, 6, 8.1
      docs/HEDGE_FUND_MASTER_PLAN.md 10(구조화된 의사결정 계약)

여기가 Agent와 결정론적 서비스의 경계다. Agent는 OrderIntent까지만 만들 수 있고
Broker를 직접 호출하지 않는다. LLM이 만든 값이 그대로 DB로 들어가지 않도록
이 계층에서 전부 검증한다 - 신뢰 경계이므로 검증을 줄이지 않는다.

가격·수량은 float를 쓰지 않는다. Decimal만 사용한다(팀 가이드 4.2, 7.1).

자체 점검: python departments/02-trading/contracts/contracts.py
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

SCHEMA_VERSION = "trading-contracts-v1"

Qty = Annotated[Decimal, Field(gt=0, max_digits=20, decimal_places=4)]
Price = Annotated[Decimal, Field(gt=0, max_digits=20, decimal_places=4)]


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    MARKET = "MARKET"


class TimeInForce(StrEnum):
    DAY = "DAY"
    IOC = "IOC"
    FOK = "FOK"


class SnapshotQuality(StrEnum):
    OK = "ok"
    STALE = "stale"
    WIDE = "wide"
    SUSPECT = "suspect"


class OrderState(StrEnum):
    DRAFT = "DRAFT"
    RISK_PENDING = "RISK_PENDING"
    APPROVED = "APPROVED"
    RESIZED = "RESIZED"
    REJECTED = "REJECTED"
    READY_TO_SUBMIT = "READY_TO_SUBMIT"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    UNKNOWN = "UNKNOWN"


class RiskVerdict(StrEnum):
    APPROVE = "approve"
    RESIZE = "resize"
    REJECT = "reject"


TERMINAL_STATES = frozenset(
    {OrderState.FILLED, OrderState.CANCELLED, OrderState.REJECTED, OrderState.EXPIRED}
)


class Base(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)


class MarketSnapshot(Base):
    """주문 시점 시장 상태. Tick 전체가 아니라 재현에 필요한 값만 박제한다.

    트레이딩본부는 시세를 수집하지 않는다. market-api가 발급한 snapshot_id를
    그대로 들고 다니며, 나중에 그 ID로 당시 상태를 복원할 수 있어야 한다.
    """

    market_snapshot_id: str = Field(min_length=1)
    as_of: datetime
    bid: Price | None = None
    ask: Price | None = None
    quality: SnapshotQuality = SnapshotQuality.OK

    @property
    def mid(self) -> Decimal | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> Decimal | None:
        mid = self.mid
        if mid is None or mid == 0:
            return None
        return (self.ask - self.bid) / mid * 10000  # type: ignore[operator]

    @model_validator(mode="after")
    def _bid_not_above_ask(self):
        if self.bid is not None and self.ask is not None and self.bid > self.ask:
            raise ValueError(f"bid({self.bid}) > ask({self.ask}): 역전된 호가")
        return self


class OrderIntent(Base):
    """Agent가 만들 수 있는 유일한 산출물.

    Risk 심사 입력이며, 이것 자체로는 아무것도 체결되지 않는다.
    """

    order_intent_id: UUID = Field(default_factory=uuid4)
    trade_case_id: UUID
    fund_id: UUID
    book_id: UUID
    strategy_id: UUID
    instrument_id: UUID

    side: Side
    order_type: OrderType
    quantity: Qty
    limit_price: Price | None = None
    time_in_force: TimeInForce = TimeInForce.DAY
    valid_until: datetime

    snapshot: MarketSnapshot
    idempotency_key: str = Field(min_length=8, max_length=128)
    schema_version: str = SCHEMA_VERSION
    created_by: str = Field(min_length=1)
    trace_id: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _check(self):
        if self.order_type is OrderType.LIMIT and self.limit_price is None:
            raise ValueError("LIMIT 주문에 limit_price가 없습니다")
        if self.order_type is OrderType.MARKET and self.limit_price is not None:
            raise ValueError("MARKET 주문에 limit_price를 넣을 수 없습니다")
        if self.valid_until <= self.created_at:
            raise ValueError("valid_until이 생성 시각보다 앞섭니다")
        # 데이터 품질이 나쁘면 주문 후보 자체를 만들지 않는다.
        # 마스터플랜 11.2 - 비정상 스프레드/데이터 단절 시 신규 진입 금지.
        if self.snapshot.quality is not SnapshotQuality.OK:
            raise ValueError(f"시장 데이터 품질 '{self.snapshot.quality}': 신규 주문 차단")
        return self

    def evidence_hash(self) -> str:
        """의도의 지문. 같은 내용이면 같은 해시가 나와야 재현·대사가 가능하다."""
        payload = self.model_dump(mode="json", exclude={"order_intent_id", "created_at", "trace_id"})
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()


class RiskDecision(Base):
    """리스크본부(risk-api)가 돌려주는 판정.

    **우리가 만드는 것이 아니라 받아서 검증하는 것이다.** 판정 권한은 리스크본부에
    있고, 트레이딩본부는 `risk.decision.v1`을 소비해(팀 가이드 6.2) Decision ID를
    저장하고(3.1) 전송 직전에 Scope와 만료를 다시 확인한다(4.3).
    이 모델은 그 역직렬화 대상이며, 필드는 팀 가이드 10장이 요구하는 최소 집합이다:
    order_intent_id, 승인된 최대 수량·가격, 만료. 이 셋이 없으면 OMS가 무엇을
    얼마나 언제까지 보내도 되는지 알 수 없다.

    동규님 스키마 확정 시 필드명이 바뀔 수 있다. 그때 고칠 곳은 여기와
    departments/02-trading/oms/oms.py의 apply_risk_decision / submit 두 군데다.
    """

    risk_decision_id: UUID = Field(default_factory=uuid4)
    order_intent_id: UUID
    verdict: RiskVerdict
    approved_quantity: Decimal | None = Field(default=None, ge=0, max_digits=20, decimal_places=4)
    max_price: Price | None = None
    expires_at: datetime
    reason: str = Field(default="", max_length=2000)
    decided_by: str = Field(min_length=1)
    decided_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def _check(self):
        if self.verdict is RiskVerdict.REJECT:
            if self.approved_quantity not in (None, Decimal(0)):
                raise ValueError("reject인데 승인 수량이 있습니다")
            if not self.reason:
                raise ValueError("reject에는 사유가 필요합니다")
        else:
            if self.approved_quantity is None or self.approved_quantity <= 0:
                raise ValueError(f"{self.verdict}에는 양수 승인 수량이 필요합니다")
        return self

    def is_valid_at(self, when: datetime) -> bool:
        return when < self.expires_at

    def authorizes(self, intent: OrderIntent, when: datetime) -> bool:
        """이 판정이 해당 주문의 전송을 허가하는가.

        OMS가 SUBMIT 직전에 다시 확인한다. 승인 시점과 전송 시점 사이에
        판정이 만료되거나 수량이 바뀌었을 수 있다 (팀 가이드 4.3).
        """
        return (
            self.verdict is not RiskVerdict.REJECT
            and self.order_intent_id == intent.order_intent_id
            and self.is_valid_at(when)
            and self.approved_quantity is not None
            and intent.quantity <= self.approved_quantity
        )


class EventEnvelope(Base):
    """본부 간 전달 이벤트의 공통 봉투 (팀 가이드 6.2, 6.3, 8.1).

    세 시각을 분리한다. 브로커 시각과 우리 수신 시각이 다르고, 순서가
    뒤바뀌어 도착할 수 있어 하나로 뭉치면 재현이 불가능해진다.

    Payload에 전체 Statement나 보고서를 넣지 않는다. object_path와 hash만 넣는다.
    """

    event_id: UUID = Field(default_factory=uuid4)
    event_type: str = Field(pattern=r"^[a-z_]+\.[a-z_]+\.v\d+$")
    event_time: datetime
    received_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    processed_at: datetime | None = None
    trace_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=8, max_length=128)
    payload: dict[str, Any] = Field(default_factory=dict)
    object_path: str | None = None
    content_hash: str | None = None

    _MAX_PAYLOAD_BYTES = 16 * 1024

    @model_validator(mode="after")
    def _check(self):
        size = len(json.dumps(self.payload, ensure_ascii=False).encode())
        if size > self._MAX_PAYLOAD_BYTES:
            raise ValueError(
                f"payload {size}B > {self._MAX_PAYLOAD_BYTES}B. "
                "본문 대신 object_path와 content_hash를 넣으세요"
            )
        if self.object_path and not self.content_hash:
            raise ValueError("object_path에는 content_hash가 함께 있어야 합니다")
        return self


# OMS 허용 상태 전이. db/001_execution.sql의 order_state_transitions와 같은 내용이며
# DB가 최종 강제 지점이다. 여기 있는 사본은 DB 왕복 없이 사전 검증하기 위한 것.
_S = OrderState
ALLOWED_TRANSITIONS: frozenset[tuple[OrderState, OrderState]] = frozenset(
    {
        (_S.DRAFT, _S.RISK_PENDING),
        (_S.RISK_PENDING, _S.APPROVED),
        (_S.RISK_PENDING, _S.RESIZED),
        (_S.RISK_PENDING, _S.REJECTED),
        (_S.APPROVED, _S.READY_TO_SUBMIT),
        (_S.RESIZED, _S.READY_TO_SUBMIT),
        (_S.READY_TO_SUBMIT, _S.SUBMITTED),
        (_S.READY_TO_SUBMIT, _S.EXPIRED),
        (_S.SUBMITTED, _S.ACKNOWLEDGED),
        (_S.SUBMITTED, _S.REJECTED),
        (_S.SUBMITTED, _S.UNKNOWN),
        (_S.ACKNOWLEDGED, _S.PARTIALLY_FILLED),
        (_S.ACKNOWLEDGED, _S.FILLED),
        (_S.ACKNOWLEDGED, _S.CANCELLED),
        (_S.ACKNOWLEDGED, _S.EXPIRED),
        (_S.ACKNOWLEDGED, _S.UNKNOWN),
        (_S.PARTIALLY_FILLED, _S.PARTIALLY_FILLED),
        (_S.PARTIALLY_FILLED, _S.FILLED),
        (_S.PARTIALLY_FILLED, _S.CANCELLED),
        (_S.PARTIALLY_FILLED, _S.EXPIRED),
        (_S.PARTIALLY_FILLED, _S.UNKNOWN),
        (_S.UNKNOWN, _S.ACKNOWLEDGED),
        (_S.UNKNOWN, _S.PARTIALLY_FILLED),
        (_S.UNKNOWN, _S.FILLED),
        (_S.UNKNOWN, _S.CANCELLED),
        (_S.UNKNOWN, _S.REJECTED),
    }
)


def can_transition(from_state: OrderState, to_state: OrderState) -> bool:
    return (from_state, to_state) in ALLOWED_TRANSITIONS


if __name__ == "__main__":
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    ids = {k: uuid4() for k in ("case", "fund", "book", "strategy", "instrument")}
    snap = MarketSnapshot(
        market_snapshot_id="snap_01",
        as_of=now,
        bid=Decimal("70000"),
        ask=Decimal("70100"),
    )

    def intent(**over) -> OrderIntent:
        kw = dict(
            trade_case_id=ids["case"],
            fund_id=ids["fund"],
            book_id=ids["book"],
            strategy_id=ids["strategy"],
            instrument_id=ids["instrument"],
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=Decimal("100"),
            limit_price=Decimal("70000"),
            valid_until=now + timedelta(hours=1),
            snapshot=snap,
            idempotency_key="idem_0001",
            created_by="trader-pm-agent",
            trace_id="trace_01",
            created_at=now,
        )
        kw.update(over)
        return OrderIntent(**kw)

    def rejects(fn, why: str):
        try:
            fn()
        except (ValueError, Exception) as e:  # pydantic ValidationError 포함
            assert e is not None
            return
        raise AssertionError(f"통과하면 안 되는 입력: {why}")

    # 1. 정상 Intent
    ok = intent()
    assert ok.snapshot.spread_bps is not None and 14 < ok.snapshot.spread_bps < 15
    assert ok.evidence_hash() == intent(trace_id="다른trace").evidence_hash(), "trace는 지문에 영향 없어야"

    # 2. 신뢰 경계 검증 - LLM이 만들 법한 잘못된 값들
    rejects(lambda: intent(quantity=Decimal("0")), "수량 0")
    rejects(lambda: intent(quantity=Decimal("-5")), "음수 수량")
    rejects(lambda: intent(limit_price=None), "LIMIT인데 가격 없음")
    rejects(lambda: intent(order_type=OrderType.MARKET), "MARKET인데 가격 있음")
    rejects(lambda: intent(valid_until=now - timedelta(minutes=1)), "이미 만료")
    rejects(lambda: intent(idempotency_key="짧음"), "idempotency_key 길이 미달")
    rejects(lambda: OrderIntent(**{**ok.model_dump(), "hallucinated": 1}), "모르는 필드")

    # 3. 데이터 품질 게이트 - stale 시세로는 주문 후보를 못 만든다
    stale = MarketSnapshot(market_snapshot_id="s2", as_of=now, bid=Decimal("1"),
                           ask=Decimal("2"), quality=SnapshotQuality.STALE)
    rejects(lambda: intent(snapshot=stale), "stale 시세")
    rejects(
        lambda: MarketSnapshot(market_snapshot_id="s3", as_of=now,
                               bid=Decimal("100"), ask=Decimal("90")),
        "역전 호가",
    )

    # 4. Risk Decision
    approve = RiskDecision(
        order_intent_id=ok.order_intent_id, verdict=RiskVerdict.APPROVE,
        approved_quantity=Decimal("100"), expires_at=now + timedelta(minutes=5),
        decided_by="risk-supervisor", decided_at=now,
    )
    assert approve.authorizes(ok, now)
    assert not approve.authorizes(ok, now + timedelta(minutes=6)), "만료된 승인이 통과됨"

    resized = RiskDecision(
        order_intent_id=ok.order_intent_id, verdict=RiskVerdict.RESIZE,
        approved_quantity=Decimal("40"), expires_at=now + timedelta(minutes=5),
        decided_by="risk-supervisor", decided_at=now,
    )
    assert not resized.authorizes(ok, now), "축소 승인인데 원래 수량이 통과됨"
    assert resized.authorizes(
        intent(order_intent_id=ok.order_intent_id, quantity=Decimal("40")), now
    ), "축소된 수량으로 다시 낸 주문이 거부됨"

    other = RiskDecision(
        order_intent_id=uuid4(), verdict=RiskVerdict.APPROVE,
        approved_quantity=Decimal("100"), expires_at=now + timedelta(minutes=5),
        decided_by="risk-supervisor", decided_at=now,
    )
    assert not other.authorizes(ok, now), "다른 주문의 승인이 재사용됨"

    rejects(
        lambda: RiskDecision(order_intent_id=ok.order_intent_id, verdict=RiskVerdict.REJECT,
                             approved_quantity=Decimal("100"),
                             expires_at=now + timedelta(minutes=5), decided_by="r", reason="x"),
        "reject인데 승인 수량 존재",
    )
    rejects(
        lambda: RiskDecision(order_intent_id=ok.order_intent_id, verdict=RiskVerdict.APPROVE,
                             expires_at=now + timedelta(minutes=5), decided_by="r"),
        "approve인데 승인 수량 없음",
    )

    # 5. Event Envelope
    env = EventEnvelope(event_type="trading.order_intent.v1", event_time=now,
                        trace_id="t", idempotency_key="idem_0001")
    assert env.processed_at is None
    rejects(lambda: EventEnvelope(event_type="badtype", event_time=now, trace_id="t",
                                  idempotency_key="idem_0001"), "이벤트 타입 형식 위반")
    rejects(lambda: EventEnvelope(event_type="a.b.v1", event_time=now, trace_id="t",
                                  idempotency_key="idem_0001",
                                  payload={"blob": "x" * 20000}), "payload 과대")
    rejects(lambda: EventEnvelope(event_type="a.b.v1", event_time=now, trace_id="t",
                                  idempotency_key="idem_0001",
                                  object_path="s3://x"), "object_path에 hash 없음")

    # 6. 상태 전이 - Risk 우회 경로가 막혀 있는가
    assert can_transition(_S.READY_TO_SUBMIT, _S.SUBMITTED)
    assert not can_transition(_S.DRAFT, _S.SUBMITTED), "Risk 심사를 건너뛰었다"
    assert not can_transition(_S.RISK_PENDING, _S.SUBMITTED), "승인 없이 전송됐다"
    assert not can_transition(_S.APPROVED, _S.SUBMITTED), "READY_TO_SUBMIT을 건너뛰었다"
    assert not can_transition(_S.REJECTED, _S.READY_TO_SUBMIT), "거부된 주문이 되살아났다"
    for terminal in TERMINAL_STATES:
        assert not any(f == terminal for f, _ in ALLOWED_TRANSITIONS), f"{terminal}에서 나가는 전이 존재"

    print("ok - 계약 6개 영역 점검 통과")
