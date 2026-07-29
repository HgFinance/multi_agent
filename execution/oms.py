#!/usr/bin/env python3
"""Sprint D1: 결정론적 OMS.

소유: 도현 (트레이딩본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.3, 8.1, 11(DoD)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.3(Risk/OMS 분리), 12(OMS 및 체결)

여기에 LLM은 없다. 주문 상태는 결정론적 코드만 바꾼다 (마스터플랜 5.3).
Agent 런타임이 죽어도 이 모듈은 계속 동작해야 한다.

불변식:
  1. Risk 승인 없이 SUBMITTED로 갈 수 없다.
  2. 같은 idempotency_key로 주문이 두 번 생기지 않는다.
  3. 같은 broker event를 두 번 받아도 체결이 두 번 잡히지 않는다.
  4. filled_quantity가 requested_quantity를 넘을 수 없다.
  5. 상태는 event store에서 재구축 가능하다.
  6. 응답이 없으면 UNKNOWN이다. FILLED나 CANCELLED로 추정하지 않는다.

자체 점검: python execution/oms.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from trading.contracts import (  # noqa: E402
    TERMINAL_STATES,
    OrderIntent,
    OrderState,
    RiskDecision,
    Side,
    can_transition,
)


class OMSError(Exception):
    """주문 상태를 바꿀 수 없는 경우. 조용히 넘어가지 않는다."""


@dataclass(frozen=True)
class OrderEvent:
    order_event_id: UUID
    order_id: UUID
    event_type: str
    sequence: int
    from_state: OrderState
    to_state: OrderState
    event_time: datetime
    received_at: datetime
    broker_adapter: str
    broker_event_id: str | None
    payload: dict


@dataclass
class Fill:
    fill_id: UUID
    order_id: UUID
    order_event_id: UUID
    quantity: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    event_time: datetime
    broker_fill_id: str | None


@dataclass
class Order:
    order_id: UUID
    order_intent_id: UUID
    client_order_id: str
    broker_adapter: str
    side: Side
    instrument_id: UUID
    requested_quantity: Decimal
    limit_price: Decimal | None
    state: OrderState = OrderState.DRAFT
    risk_decision_id: UUID | None = None
    risk_approved_qty: Decimal | None = None
    risk_expires_at: datetime | None = None
    filled_quantity: Decimal = Decimal(0)
    fills: list[Fill] = field(default_factory=list)
    version: int = 0

    @property
    def average_fill_price(self) -> Decimal | None:
        if not self.fills:
            return None
        notional = sum(f.quantity * f.price for f in self.fills)
        return Decimal(notional) / self.filled_quantity

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def leaves_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity


class OrderStore:
    """인메모리 저장소.

    ponytail: DB 자격증명이 확보되면 db/001_execution.sql 위의 psycopg 구현으로
              교체한다. 상태 전이 강제는 이미 order_state_transitions FK가 하므로
              그때는 이 클래스의 검증이 이중 방어가 된다. 로직은 그대로 둔다.
    """

    def __init__(self) -> None:
        self.orders: dict[UUID, Order] = {}
        self.events: list[OrderEvent] = []
        self._by_idempotency: dict[str, UUID] = {}
        self._seen_broker_events: set[tuple[str, str]] = set()

    def find_by_idempotency(self, key: str) -> Order | None:
        order_id = self._by_idempotency.get(key)
        return self.orders.get(order_id) if order_id else None

    def seen_broker_event(self, adapter: str, broker_event_id: str | None) -> bool:
        return broker_event_id is not None and (adapter, broker_event_id) in self._seen_broker_events

    def next_sequence(self, order_id: UUID) -> int:
        return sum(1 for e in self.events if e.order_id == order_id) + 1

    def events_for(self, order_id: UUID) -> list[OrderEvent]:
        return sorted((e for e in self.events if e.order_id == order_id), key=lambda e: e.sequence)


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OMS:
    def __init__(self, store: OrderStore | None = None, adapter: str = "paper") -> None:
        self.store = store or OrderStore()
        self.adapter = adapter

    # -- 명령 -----------------------------------------------------------------

    def create_order(self, intent: OrderIntent) -> Order:
        """Order Intent를 주문으로 등록한다. 아직 Risk 심사 전이다.

        같은 idempotency_key로 다시 부르면 기존 주문을 그대로 돌려준다.
        네트워크 재시도로 주문이 두 배가 나가는 사고를 막는다.
        """
        existing = self.store.find_by_idempotency(intent.idempotency_key)
        if existing is not None:
            return existing

        order = Order(
            order_id=uuid4(),
            order_intent_id=intent.order_intent_id,
            client_order_id=f"cli_{intent.idempotency_key}",
            broker_adapter=self.adapter,
            side=intent.side,
            instrument_id=intent.instrument_id,
            requested_quantity=intent.quantity,
            limit_price=intent.limit_price,
        )
        self.store.orders[order.order_id] = order
        self.store._by_idempotency[intent.idempotency_key] = order.order_id
        self._append(order, "order_created", OrderState.DRAFT, OrderState.DRAFT,
                     {"order_intent_id": str(intent.order_intent_id)}, initial=True)
        return order

    def request_risk_review(self, order: Order) -> Order:
        return self._transition(order, OrderState.RISK_PENDING, "risk_requested", {})

    def apply_risk_decision(self, order: Order, decision: RiskDecision, intent: OrderIntent) -> Order:
        """리스크본부 판정을 반영한다. 판정 없이는 주문이 앞으로 못 간다."""
        if decision.order_intent_id != order.order_intent_id:
            raise OMSError("다른 주문의 Risk 판정입니다")

        verdict_state = {
            "approve": OrderState.APPROVED,
            "resize": OrderState.RESIZED,
            "reject": OrderState.REJECTED,
        }[decision.verdict.value]

        if verdict_state is OrderState.RESIZED:
            # 축소 승인이면 주문 수량을 승인 수량으로 줄인다. 이미 체결분이 있으면 안 된다.
            assert decision.approved_quantity is not None
            if decision.approved_quantity < order.filled_quantity:
                raise OMSError("승인 수량이 이미 체결된 수량보다 적습니다")
            order.requested_quantity = decision.approved_quantity

        order.risk_decision_id = decision.risk_decision_id
        order.risk_approved_qty = decision.approved_quantity
        order.risk_expires_at = decision.expires_at

        self._transition(order, verdict_state, "risk_decided",
                         {"verdict": decision.verdict.value, "reason": decision.reason})
        if verdict_state is not OrderState.REJECTED:
            self._transition(order, OrderState.READY_TO_SUBMIT, "ready", {})
        return order

    def submit(self, order: Order, intent: OrderIntent, when: datetime | None = None) -> Order:
        """브로커 전송. 여기가 Risk Gate의 마지막 관문이다."""
        when = when or _now()

        if order.risk_decision_id is None:
            raise OMSError("Risk 승인이 없는 주문은 전송할 수 없습니다")
        if order.risk_expires_at is not None and when >= order.risk_expires_at:
            raise OMSError("Risk 승인이 만료됐습니다. 재심사가 필요합니다")
        if order.risk_approved_qty is not None and order.requested_quantity > order.risk_approved_qty:
            raise OMSError(
                f"승인 수량({order.risk_approved_qty})을 초과한 주문({order.requested_quantity})입니다"
            )
        if when >= intent.valid_until:
            raise OMSError("Order Intent가 만료됐습니다")

        return self._transition(order, OrderState.SUBMITTED, "submitted",
                                {"client_order_id": order.client_order_id}, event_time=when)

    # -- 브로커 이벤트 수신 ------------------------------------------------------

    def on_broker_event(
        self,
        order: Order,
        event_type: str,
        broker_event_id: str,
        event_time: datetime,
        payload: dict | None = None,
    ) -> Order:
        """브로커가 보낸 사실만 반영한다. 추정하지 않는다.

        같은 broker_event_id를 두 번 받으면 두 번째는 무시한다. 브로커 재전송과
        우리 쪽 재처리 모두에서 발생한다.
        """
        payload = payload or {}
        if self.store.seen_broker_event(self.adapter, broker_event_id):
            return order

        handler = {
            "ack": self._on_ack,
            "fill": self._on_fill,
            "reject": self._on_reject,
            "cancel": self._on_cancel,
            "expire": self._on_expire,
        }.get(event_type)
        if handler is None:
            raise OMSError(f"알 수 없는 브로커 이벤트: {event_type}")

        return handler(order, broker_event_id, event_time, payload)

    def _on_ack(self, order, broker_event_id, event_time, payload) -> Order:
        return self._transition(order, OrderState.ACKNOWLEDGED, "ack", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def _on_fill(self, order, broker_event_id, event_time, payload) -> Order:
        qty = Decimal(str(payload["quantity"]))
        price = Decimal(str(payload["price"]))
        if qty <= 0:
            raise OMSError("체결 수량이 0 이하입니다")
        if order.filled_quantity + qty > order.requested_quantity:
            # 초과 체결은 조용히 받아들이면 포지션이 틀어진다. 예외로 올려 Break를 만든다.
            raise OMSError(
                f"초과 체결: 기체결 {order.filled_quantity} + {qty} > 주문 {order.requested_quantity}"
            )

        target = (
            OrderState.FILLED
            if order.filled_quantity + qty == order.requested_quantity
            else OrderState.PARTIALLY_FILLED
        )
        event = self._transition(order, target, "fill", payload,
                                 broker_event_id=broker_event_id, event_time=event_time,
                                 return_event=True)
        order.filled_quantity += qty
        order.fills.append(
            Fill(
                fill_id=uuid4(),
                order_id=order.order_id,
                order_event_id=event.order_event_id,
                quantity=qty,
                price=price,
                fee=Decimal(str(payload.get("fee", 0))),
                tax=Decimal(str(payload.get("tax", 0))),
                event_time=event_time,
                broker_fill_id=payload.get("broker_fill_id"),
            )
        )
        return order

    def _on_reject(self, order, broker_event_id, event_time, payload) -> Order:
        return self._transition(order, OrderState.REJECTED, "reject", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def _on_cancel(self, order, broker_event_id, event_time, payload) -> Order:
        return self._transition(order, OrderState.CANCELLED, "cancel", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def _on_expire(self, order, broker_event_id, event_time, payload) -> Order:
        return self._transition(order, OrderState.EXPIRED, "expire", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def mark_unknown(self, order: Order, reason: str) -> Order:
        """브로커 응답이 없거나 상태가 불명확할 때.

        FILLED나 CANCELLED로 추정하는 것이 사고의 시작이다. 모르면 모른다고 쓴다.
        """
        return self._transition(order, OrderState.UNKNOWN, "unknown", {"reason": reason})

    # -- 내부 ------------------------------------------------------------------

    def _transition(self, order, to_state, event_type, payload, *, broker_event_id=None,
                    event_time=None, return_event=False, initial=False):
        from_state = order.state
        if not initial and not can_transition(from_state, to_state):
            raise OMSError(f"허용되지 않은 전이: {from_state} -> {to_state}")
        event = self._append(order, event_type, from_state, to_state, payload,
                             broker_event_id=broker_event_id, event_time=event_time,
                             initial=initial)
        order.state = to_state
        order.version += 1
        return event if return_event else order

    def _append(self, order, event_type, from_state, to_state, payload, *,
                broker_event_id=None, event_time=None, initial=False) -> OrderEvent:
        event = OrderEvent(
            order_event_id=uuid4(),
            order_id=order.order_id,
            event_type=event_type,
            sequence=self.store.next_sequence(order.order_id),
            from_state=from_state,
            to_state=to_state,
            event_time=event_time or _now(),
            received_at=_now(),
            broker_adapter=self.adapter,
            broker_event_id=broker_event_id,
            payload=payload,
        )
        self.store.events.append(event)
        if broker_event_id is not None:
            self.store._seen_broker_events.add((self.adapter, broker_event_id))
        return event

    # -- Projection 재구축 -------------------------------------------------------

    def rebuild_state(self, order_id: UUID) -> OrderState:
        """이벤트만으로 현재 상태를 복원한다 (팀 가이드 DoD 4번).

        orders.state는 캐시일 뿐이다. 둘이 어긋나면 이벤트가 맞다.
        """
        events = self.store.events_for(order_id)
        if not events:
            raise OMSError("이벤트가 없는 주문입니다")
        return events[-1].to_state


if __name__ == "__main__":
    from datetime import timedelta

    from trading.contracts import (
        MarketSnapshot,
        OrderType,
        RiskVerdict,
        TimeInForce,
    )

    now = datetime.now(timezone.utc)
    snap = MarketSnapshot(market_snapshot_id="s1", as_of=now,
                          bid=Decimal("70000"), ask=Decimal("70100"))
    common = dict(
        trade_case_id=uuid4(), fund_id=uuid4(), book_id=uuid4(),
        strategy_id=uuid4(), instrument_id=uuid4(),
        side=Side.BUY, order_type=OrderType.LIMIT, limit_price=Decimal("70000"),
        time_in_force=TimeInForce.DAY, valid_until=now + timedelta(hours=1),
        snapshot=snap, created_by="trader-pm-agent", trace_id="t1", created_at=now,
    )

    def make_intent(key="idem_0001", qty="100") -> OrderIntent:
        return OrderIntent(**common, quantity=Decimal(qty), idempotency_key=key)

    def approval(intent, qty="100", verdict=RiskVerdict.APPROVE, minutes=5) -> RiskDecision:
        return RiskDecision(
            order_intent_id=intent.order_intent_id, verdict=verdict,
            approved_quantity=Decimal(qty), expires_at=now + timedelta(minutes=minutes),
            decided_by="risk-supervisor", decided_at=now,
        )

    def raises(fn, why):
        try:
            fn()
        except OMSError:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    # 1. Risk 승인 없는 Submit 차단 (DoD 2번)
    oms = OMS()
    i = make_intent()
    o = oms.create_order(i)
    raises(lambda: oms.submit(o, i), "심사 전 전송")
    oms.request_risk_review(o)
    raises(lambda: oms.submit(o, i), "심사 중 전송")

    # 2. 거부된 주문은 되살아나지 않는다
    oms2 = OMS()
    i2 = make_intent("idem_0002")
    o2 = oms2.create_order(i2)
    oms2.request_risk_review(o2)
    oms2.apply_risk_decision(o2, RiskDecision(
        order_intent_id=i2.order_intent_id, verdict=RiskVerdict.REJECT,
        expires_at=now + timedelta(minutes=5), decided_by="risk", reason="한도 초과",
    ), i2)
    assert o2.state is OrderState.REJECTED
    raises(lambda: oms2.submit(o2, i2), "거부된 주문 전송")

    # 3. 정상 경로 + 부분 체결 누적
    oms.apply_risk_decision(o, approval(i), i)
    assert o.state is OrderState.READY_TO_SUBMIT
    oms.submit(o, i)
    oms.on_broker_event(o, "ack", "brk_ack_1", now)
    oms.on_broker_event(o, "fill", "brk_f1", now, {"quantity": "40", "price": "70000", "fee": "10"})
    assert o.state is OrderState.PARTIALLY_FILLED and o.filled_quantity == Decimal("40")
    assert o.leaves_quantity == Decimal("60")
    oms.on_broker_event(o, "fill", "brk_f2", now, {"quantity": "60", "price": "70050", "fee": "15"})
    assert o.state is OrderState.FILLED and o.filled_quantity == Decimal("100")
    assert o.average_fill_price == Decimal("70030")

    # 4. 멱등성 - 같은 브로커 체결 이벤트 재수신 (DoD 3번)
    oms.on_broker_event(o, "fill", "brk_f2", now, {"quantity": "60", "price": "70050"})
    assert o.filled_quantity == Decimal("100"), "중복 체결이 잡혔다"
    assert len(o.fills) == 2

    # 5. 멱등성 - 같은 idempotency_key로 주문 재생성
    again = oms.create_order(make_intent("idem_0001"))
    assert again.order_id == o.order_id, "중복 주문이 생겼다"
    assert len(oms.store.orders) == 1

    # 6. 초과 체결 거부 (DoD - filled <= requested)
    oms3 = OMS()
    i3 = make_intent("idem_0003")
    o3 = oms3.create_order(i3)
    oms3.request_risk_review(o3)
    oms3.apply_risk_decision(o3, approval(i3), i3)
    oms3.submit(o3, i3)
    oms3.on_broker_event(o3, "ack", "b3_ack", now)
    raises(lambda: oms3.on_broker_event(o3, "fill", "b3_f1", now,
                                        {"quantity": "101", "price": "70000"}), "초과 체결")
    assert o3.filled_quantity == Decimal(0)

    # 7. Risk 승인 만료 후 전송 차단
    oms4 = OMS()
    i4 = make_intent("idem_0004")
    o4 = oms4.create_order(i4)
    oms4.request_risk_review(o4)
    oms4.apply_risk_decision(o4, approval(i4, minutes=1), i4)
    raises(lambda: oms4.submit(o4, i4, when=now + timedelta(minutes=2)), "만료된 승인")

    # 8. 축소 승인(RESIZED)은 주문 수량을 줄인다
    oms5 = OMS()
    i5 = make_intent("idem_0005")
    o5 = oms5.create_order(i5)
    oms5.request_risk_review(o5)
    oms5.apply_risk_decision(o5, approval(i5, qty="40", verdict=RiskVerdict.RESIZE), i5)
    assert o5.requested_quantity == Decimal("40"), "축소 승인이 반영 안 됨"
    oms5.submit(o5, i5)
    assert o5.state is OrderState.SUBMITTED

    # 9. 상태 불명은 추정하지 않는다
    oms6 = OMS()
    i6 = make_intent("idem_0006")
    o6 = oms6.create_order(i6)
    oms6.request_risk_review(o6)
    oms6.apply_risk_decision(o6, approval(i6), i6)
    oms6.submit(o6, i6)
    oms6.mark_unknown(o6, "브로커 응답 없음")
    assert o6.state is OrderState.UNKNOWN
    assert not o6.is_terminal, "UNKNOWN을 종료 상태로 취급했다"
    oms6.on_broker_event(o6, "fill", "b6_f1", now, {"quantity": "100", "price": "70000"})
    assert o6.state is OrderState.FILLED, "조회 후 상태 복구 실패"

    # 10. 이벤트에서 상태 재구축 (DoD 4번)
    for oms_i, order_i in ((oms, o), (oms3, o3), (oms6, o6)):
        assert oms_i.rebuild_state(order_i.order_id) is order_i.state, "projection과 이벤트 불일치"

    print("ok - OMS 불변식 10개 점검 통과")
