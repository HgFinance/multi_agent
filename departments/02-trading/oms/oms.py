#!/usr/bin/env python3
"""Sprint D1: 결정론적 OMS. (F14)

소유: 도현 (트레이딩본부)
근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.3(v1.2), 8.1, 11(DoD)
      docs/HEDGE_FUND_MASTER_PLAN.md 5.3(Risk/OMS 분리), 12(OMS 및 체결)

여기에 LLM은 없다. 주문 상태는 결정론적 코드만 바꾼다 (마스터플랜 5.3).
Agent 런타임이 죽어도 이 모듈은 계속 동작해야 한다.

**팀 가이드 v1.2에서 상태 머신이 둘로 분리됐다.** 하나로 두면 "리스크본부가 거부한
것"과 "브로커가 거부한 것"이 같은 REJECTED로 뭉개지고, 우리 심사 절차가 브로커
사실과 같은 타임라인에 섞인다. 그래서 두 객체를 따로 둔다.

  OrderIntentRecord  우리 쪽 심사 절차   DRAFT -> ... -> READY_TO_SUBMIT
  BrokerOrder        브로커의 사실       CREATED -> SUBMITTED -> ... -> FILLED

둘 사이에 상태 전이는 없다. READY_TO_SUBMIT에 도달한 Intent로 별도의 Broker Order를
만들 뿐이다. `RISK_APPROVED`는 상태가 아니라 전제조건이다 - 유효한 risk_decision_id
없이는 Broker Order 자체가 생기지 않는다.

불변식:
  1. Risk 승인 없이 Broker Order가 생기지 않고, SUBMITTED로도 갈 수 없다.
  2. 같은 idempotency_key로 Intent가 두 번 생기지 않는다.
  3. 하나의 Intent에 Broker Order는 하나뿐이다.
  4. 같은 broker event를 두 번 받아도 체결이 두 번 잡히지 않는다.
  5. filled_quantity가 requested_quantity를 넘을 수 없다.
  6. 상태는 event store에서 재구축 가능하다.
  7. 응답이 없으면 UNKNOWN이다. FILLED나 CANCELLED로 추정하지 않는다.
  8. UNKNOWN 탈출은 Reconciliation 확정으로만 한다. 그 사이 같은 Fund의 신규 주문은 막는다.

자체 점검: python departments/02-trading/oms/oms.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "contracts"))

from contracts import (  # noqa: E402
    BROKER_TERMINAL_STATES,
    BrokerOrderState,
    IntentState,
    OrderIntent,
    RiskDecision,
    Side,
    can_transition,
)


class OMSError(Exception):
    """주문 상태를 바꿀 수 없는 경우. 조용히 넘어가지 않는다."""


@dataclass(frozen=True)
class StateEvent:
    """두 머신 공용 append-only 이벤트.

    stream이 어느 머신인지 가른다. 하나의 로그에 담아야 한 주문의 생애를
    시간순으로 이어 볼 수 있다 - Audit/Replay(F16)가 이걸 읽는다.
    """

    event_id: UUID
    stream: str              # intent | broker_order
    stream_id: UUID
    event_type: str
    sequence: int
    from_state: str
    to_state: str
    event_time: datetime
    received_at: datetime
    broker_adapter: str
    broker_event_id: str | None
    payload: dict


@dataclass
class Fill:
    fill_id: UUID
    order_id: UUID
    event_id: UUID
    quantity: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    event_time: datetime
    broker_fill_id: str | None


@dataclass
class OrderIntentRecord:
    """Intent의 심사 진행 상태. Intent 자체(불변 계약)와 분리한다.

    OrderIntent는 frozen이라 상태를 담을 수 없고, 담아서도 안 된다.
    Agent가 만든 원본은 증거로 그대로 보존한다.
    """

    order_intent_id: UUID
    fund_id: UUID
    idempotency_key: str
    requested_quantity: Decimal
    valid_until: datetime
    state: IntentState = IntentState.DRAFT
    risk_decision_id: UUID | None = None
    risk_approved_qty: Decimal | None = None
    risk_expires_at: datetime | None = None
    version: int = 0

    @property
    def is_terminal(self) -> bool:
        return self.state in (IntentState.REJECTED, IntentState.EXPIRED)


@dataclass
class BrokerOrder:
    order_id: UUID
    order_intent_id: UUID
    fund_id: UUID
    client_order_id: str
    broker_adapter: str
    side: Side
    instrument_id: UUID
    requested_quantity: Decimal
    limit_price: Decimal | None
    state: BrokerOrderState = BrokerOrderState.CREATED
    broker_order_id: str | None = None
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
        return self.state in BROKER_TERMINAL_STATES

    @property
    def leaves_quantity(self) -> Decimal:
        return self.requested_quantity - self.filled_quantity


class OrderStore:
    """인메모리 저장소.

    ponytail: DB 자격증명이 확보되면 supabase/migrations/ 위의 psycopg 구현으로
              교체한다. 상태 전이 강제는 이미 execution.validate_order_state_transition()
              트리거가 하므로 그때는 이 클래스의 검증이 이중 방어가 된다.
              로직은 그대로 둔다.
    """

    def __init__(self) -> None:
        self.intents: dict[UUID, OrderIntentRecord] = {}
        self.orders: dict[UUID, BrokerOrder] = {}
        self.events: list[StateEvent] = []
        self._by_idempotency: dict[str, UUID] = {}
        self._order_by_intent: dict[UUID, UUID] = {}
        self._seen_broker_events: set[tuple[str, str]] = set()

    def find_intent_by_idempotency(self, key: str) -> OrderIntentRecord | None:
        intent_id = self._by_idempotency.get(key)
        return self.intents.get(intent_id) if intent_id else None

    def find_order_by_intent(self, intent_id: UUID) -> BrokerOrder | None:
        order_id = self._order_by_intent.get(intent_id)
        return self.orders.get(order_id) if order_id else None

    def seen_broker_event(self, adapter: str, broker_event_id: str | None) -> bool:
        return broker_event_id is not None and (adapter, broker_event_id) in self._seen_broker_events

    def next_sequence(self, stream: str, stream_id: UUID) -> int:
        return sum(1 for e in self.events if e.stream == stream and e.stream_id == stream_id) + 1

    def events_for(self, stream: str, stream_id: UUID) -> list[StateEvent]:
        return sorted(
            (e for e in self.events if e.stream == stream and e.stream_id == stream_id),
            key=lambda e: e.sequence,
        )


def _now() -> datetime:
    return datetime.now(timezone.utc)


class OMS:
    def __init__(self, store: OrderStore | None = None, adapter: str = "paper") -> None:
        self.store = store or OrderStore()
        self.adapter = adapter

    # -- 1단계: Order Intent 심사 ------------------------------------------------

    def register_intent(self, intent: OrderIntent) -> OrderIntentRecord:
        """Agent가 만든 Intent를 심사 대기로 등록한다.

        같은 idempotency_key로 다시 부르면 기존 기록을 그대로 돌려준다.
        네트워크 재시도로 주문이 두 배가 나가는 사고를 막는다.
        """
        existing = self.store.find_intent_by_idempotency(intent.idempotency_key)
        if existing is not None:
            return existing

        rec = OrderIntentRecord(
            order_intent_id=intent.order_intent_id,
            fund_id=intent.fund_id,
            idempotency_key=intent.idempotency_key,
            requested_quantity=intent.quantity,
            valid_until=intent.valid_until,
        )
        self.store.intents[rec.order_intent_id] = rec
        self.store._by_idempotency[intent.idempotency_key] = rec.order_intent_id
        self._append("intent", rec.order_intent_id, "intent_registered",
                     IntentState.DRAFT, IntentState.DRAFT,
                     {"trace_id": intent.trace_id, "evidence_hash": intent.evidence_hash()})
        return rec

    def request_risk_review(self, rec: OrderIntentRecord) -> OrderIntentRecord:
        return self._move_intent(rec, IntentState.RISK_PENDING, "risk_requested", {})

    def apply_risk_decision(self, rec: OrderIntentRecord, decision: RiskDecision) -> OrderIntentRecord:
        """리스크본부 판정을 반영한다. 판정 없이는 Intent가 앞으로 못 간다.

        판정은 우리가 만들지 않는다. 받아서 검증하고 기록만 한다.
        """
        if decision.order_intent_id != rec.order_intent_id:
            raise OMSError("다른 Intent의 Risk 판정입니다")

        verdict_state = {
            "approve": IntentState.APPROVED,
            "resize": IntentState.RESIZED,
            "reject": IntentState.REJECTED,
        }[decision.verdict.value]

        if verdict_state is IntentState.RESIZED:
            assert decision.approved_quantity is not None
            rec.requested_quantity = decision.approved_quantity

        rec.risk_decision_id = decision.risk_decision_id
        rec.risk_approved_qty = decision.approved_quantity
        rec.risk_expires_at = decision.expires_at

        self._move_intent(rec, verdict_state, "risk_decided",
                          {"verdict": decision.verdict.value, "reason": decision.reason,
                           "risk_decision_id": str(decision.risk_decision_id)})
        if verdict_state is not IntentState.REJECTED:
            self._move_intent(rec, IntentState.READY_TO_SUBMIT, "ready", {})
        return rec

    def expire_intent(self, rec: OrderIntentRecord, reason: str) -> OrderIntentRecord:
        return self._move_intent(rec, IntentState.EXPIRED, "intent_expired", {"reason": reason})

    # -- 2단계: Broker Order ----------------------------------------------------

    def create_broker_order(self, rec: OrderIntentRecord, intent: OrderIntent) -> BrokerOrder:
        """심사를 통과한 Intent로만 Broker Order를 만든다.

        여기가 두 머신의 유일한 접점이자 Risk Gate다. 상태 전이가 아니라
        새 객체 생성이라는 점이 v1.2의 핵심이다.
        """
        if rec.state is not IntentState.READY_TO_SUBMIT:
            raise OMSError(f"READY_TO_SUBMIT이 아닌 Intent입니다: {rec.state}")
        if rec.risk_decision_id is None:
            raise OMSError("Risk 판정이 없는 Intent입니다")

        existing = self.store.find_order_by_intent(rec.order_intent_id)
        if existing is not None:
            return existing  # 불변식 3: Intent 하나에 Broker Order 하나

        blocked = self._unknown_order_for(rec.fund_id)
        if blocked is not None:
            raise OMSError(
                f"같은 Fund에 상태 불명 주문({blocked.client_order_id})이 있어 신규 주문을 막습니다. "
                "Broker Reconciliation으로 확정한 뒤 다시 시도하세요"
            )

        order = BrokerOrder(
            order_id=uuid4(),
            order_intent_id=rec.order_intent_id,
            fund_id=rec.fund_id,
            client_order_id=f"cli_{rec.idempotency_key}",
            broker_adapter=self.adapter,
            side=intent.side,
            instrument_id=intent.instrument_id,
            requested_quantity=rec.requested_quantity,  # 축소 승인이 반영된 수량
            limit_price=intent.limit_price,
        )
        self.store.orders[order.order_id] = order
        self.store._order_by_intent[rec.order_intent_id] = order.order_id
        self._append("broker_order", order.order_id, "order_created",
                     BrokerOrderState.CREATED, BrokerOrderState.CREATED,
                     {"order_intent_id": str(rec.order_intent_id),
                      "risk_decision_id": str(rec.risk_decision_id)})
        return order

    def submit(self, order: BrokerOrder, rec: OrderIntentRecord,
               when: datetime | None = None) -> BrokerOrder:
        """브로커 전송. 여기가 Risk Gate의 마지막 관문이다.

        승인 시점과 전송 시점 사이에 판정이 만료되거나 수량이 바뀌었을 수 있다.
        생성 때 통과했다고 전송 때도 통과라고 가정하지 않는다 (가이드 4.3).
        """
        when = when or _now()

        if rec.risk_decision_id is None:
            raise OMSError("Risk 승인이 없는 주문은 전송할 수 없습니다")
        if rec.state is not IntentState.READY_TO_SUBMIT:
            raise OMSError(f"Intent가 전송 가능한 상태가 아닙니다: {rec.state}")
        if rec.risk_expires_at is not None and when >= rec.risk_expires_at:
            raise OMSError("Risk 승인이 만료됐습니다. 재심사가 필요합니다")
        if rec.risk_approved_qty is not None and order.requested_quantity > rec.risk_approved_qty:
            raise OMSError(
                f"승인 수량({rec.risk_approved_qty})을 초과한 주문({order.requested_quantity})입니다"
            )
        if when >= rec.valid_until:
            raise OMSError("Order Intent가 만료됐습니다")

        return self._move_order(order, BrokerOrderState.SUBMITTED, "submitted",
                                {"client_order_id": order.client_order_id}, event_time=when)

    def request_cancel(self, order: BrokerOrder, reason: str = "") -> BrokerOrder:
        """취소 요청. 아직 취소된 것이 아니다.

        브로커가 cancel 이벤트를 돌려주기 전까지 CANCELLED로 쓰지 않는다.
        요청과 체결이 교차하면 취소 대신 체결이 올 수도 있다.
        """
        return self._move_order(order, BrokerOrderState.CANCEL_PENDING, "cancel_requested",
                                {"reason": reason, "leaves": str(order.leaves_quantity)})

    # -- 브로커 이벤트 수신 ------------------------------------------------------

    def on_broker_event(
        self,
        order: BrokerOrder,
        event_type: str,
        broker_event_id: str,
        event_time: datetime,
        payload: dict | None = None,
        *,
        reconciled: bool = False,
    ) -> BrokerOrder:
        """브로커가 보낸 사실만 반영한다. 추정하지 않는다.

        같은 broker_event_id를 두 번 받으면 두 번째는 무시한다. 브로커 재전송과
        우리 쪽 재처리 모두에서 발생한다.

        UNKNOWN 상태에서는 Reconciliation이 확정한 결과(reconciled=True)만 받는다.
        상태를 모르는 채로 흘러들어온 이벤트로 상태를 확정하면 UNKNOWN을 둔 의미가 없다.
        """
        payload = payload or {}
        if self.store.seen_broker_event(self.adapter, broker_event_id):
            return order
        if order.state is BrokerOrderState.UNKNOWN and not reconciled:
            raise OMSError(
                "상태 불명 주문입니다. Broker Reconciliation 결과로만 확정할 수 있습니다"
            )

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

    def _on_ack(self, order, broker_event_id, event_time, payload) -> BrokerOrder:
        order.broker_order_id = payload.get("broker_order_id")
        return self._move_order(order, BrokerOrderState.ACKNOWLEDGED, "ack", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def _on_fill(self, order, broker_event_id, event_time, payload) -> BrokerOrder:
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
            BrokerOrderState.FILLED
            if order.filled_quantity + qty == order.requested_quantity
            else BrokerOrderState.PARTIALLY_FILLED
        )
        event = self._move_order(order, target, "fill", payload,
                                 broker_event_id=broker_event_id, event_time=event_time,
                                 return_event=True)
        order.filled_quantity += qty
        order.fills.append(
            Fill(
                fill_id=uuid4(),
                order_id=order.order_id,
                event_id=event.event_id,
                quantity=qty,
                price=price,
                fee=Decimal(str(payload.get("fee", 0))),
                tax=Decimal(str(payload.get("tax", 0))),
                event_time=event_time,
                broker_fill_id=payload.get("broker_fill_id"),
            )
        )
        return order

    def _on_reject(self, order, broker_event_id, event_time, payload) -> BrokerOrder:
        return self._move_order(order, BrokerOrderState.REJECTED, "reject", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def _on_cancel(self, order, broker_event_id, event_time, payload) -> BrokerOrder:
        return self._move_order(order, BrokerOrderState.CANCELLED, "cancel", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def _on_expire(self, order, broker_event_id, event_time, payload) -> BrokerOrder:
        return self._move_order(order, BrokerOrderState.EXPIRED, "expire", payload,
                                broker_event_id=broker_event_id, event_time=event_time)

    def mark_unknown(self, order: BrokerOrder, reason: str) -> BrokerOrder:
        """브로커 응답이 없거나 상태가 불명확할 때.

        FILLED나 CANCELLED로 추정하는 것이 사고의 시작이다. 모르면 모른다고 쓴다.
        """
        return self._move_order(order, BrokerOrderState.UNKNOWN, "unknown", {"reason": reason})

    def _unknown_order_for(self, fund_id: UUID) -> BrokerOrder | None:
        """가이드 4.3: UNKNOWN이면 그 Fund의 신규 주문을 막는다.

        ponytail: 전체 주문 스캔이다. DB로 옮기면 orders(fund_id, state) 부분 인덱스
                  한 방 조회가 된다. 인메모리 Paper 규모에서는 이걸로 충분하다.
        """
        return next(
            (o for o in self.store.orders.values()
             if o.fund_id == fund_id and o.state is BrokerOrderState.UNKNOWN),
            None,
        )

    # -- 내부 ------------------------------------------------------------------

    def _move_intent(self, rec, to_state, event_type, payload) -> OrderIntentRecord:
        if not can_transition(rec.state, to_state):
            raise OMSError(f"허용되지 않은 Intent 전이: {rec.state} -> {to_state}")
        self._append("intent", rec.order_intent_id, event_type, rec.state, to_state, payload)
        rec.state = to_state
        rec.version += 1
        return rec

    def _move_order(self, order, to_state, event_type, payload, *, broker_event_id=None,
                    event_time=None, return_event=False):
        if not can_transition(order.state, to_state):
            raise OMSError(f"허용되지 않은 Broker Order 전이: {order.state} -> {to_state}")
        event = self._append("broker_order", order.order_id, event_type, order.state, to_state,
                             payload, broker_event_id=broker_event_id, event_time=event_time)
        order.state = to_state
        order.version += 1
        return event if return_event else order

    def _append(self, stream, stream_id, event_type, from_state, to_state, payload, *,
                broker_event_id=None, event_time=None) -> StateEvent:
        event = StateEvent(
            event_id=uuid4(),
            stream=stream,
            stream_id=stream_id,
            event_type=event_type,
            sequence=self.store.next_sequence(stream, stream_id),
            from_state=str(from_state),
            to_state=str(to_state),
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

    def rebuild_state(self, stream: str, stream_id: UUID) -> str:
        """이벤트만으로 현재 상태를 복원한다 (팀 가이드 DoD 4번).

        레코드의 state는 캐시일 뿐이다. 둘이 어긋나면 이벤트가 맞다.
        """
        events = self.store.events_for(stream, stream_id)
        if not events:
            raise OMSError("이벤트가 없는 스트림입니다")
        return events[-1].to_state


if __name__ == "__main__":
    from datetime import timedelta

    from contracts import (
        MarketSnapshot,
        OrderType,
        RiskVerdict,
        TimeInForce,
    )

    now = datetime.now(timezone.utc)
    snap = MarketSnapshot(market_snapshot_id="s1", as_of=now,
                          bid=Decimal("70000"), ask=Decimal("70100"))
    FUND = uuid4()
    common = dict(
        trade_case_id=uuid4(), fund_id=FUND, book_id=uuid4(),
        strategy_id=uuid4(), instrument_id=uuid4(),
        side=Side.BUY, order_type=OrderType.LIMIT, limit_price=Decimal("70000"),
        time_in_force=TimeInForce.DAY, valid_until=now + timedelta(hours=1),
        snapshot=snap, created_by="trader-pm-agent", trace_id="t1", created_at=now,
    )

    def make_intent(key="idem_0001", qty="100", **over) -> OrderIntent:
        return OrderIntent(**{**common, **over}, quantity=Decimal(qty), idempotency_key=key)

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

    def approved_order(oms, key, qty="100", **over):
        """심사를 통과시켜 전송 직전까지 만든다."""
        i = make_intent(key, qty, **over)
        rec = oms.register_intent(i)
        oms.request_risk_review(rec)
        oms.apply_risk_decision(rec, approval(i, qty))
        return i, rec, oms.create_broker_order(rec, i)

    # 1. Risk 승인 전에는 Broker Order 자체가 생기지 않는다 (DoD 2번)
    oms = OMS()
    i = make_intent()
    rec = oms.register_intent(i)
    raises(lambda: oms.create_broker_order(rec, i), "심사 전 주문 생성")
    oms.request_risk_review(rec)
    raises(lambda: oms.create_broker_order(rec, i), "심사 중 주문 생성")

    # 2. 거부된 Intent는 되살아나지 않는다
    oms2 = OMS()
    i2 = make_intent("idem_0002")
    rec2 = oms2.register_intent(i2)
    oms2.request_risk_review(rec2)
    oms2.apply_risk_decision(rec2, RiskDecision(
        order_intent_id=i2.order_intent_id, verdict=RiskVerdict.REJECT,
        expires_at=now + timedelta(minutes=5), decided_by="risk", reason="한도 초과",
    ))
    assert rec2.state is IntentState.REJECTED and rec2.is_terminal
    raises(lambda: oms2.create_broker_order(rec2, i2), "거부된 Intent로 주문 생성")

    # 3. 정상 경로 + 부분 체결 누적
    oms.apply_risk_decision(rec, approval(i))
    assert rec.state is IntentState.READY_TO_SUBMIT
    o = oms.create_broker_order(rec, i)
    assert o.state is BrokerOrderState.CREATED
    oms.submit(o, rec)
    oms.on_broker_event(o, "ack", "brk_ack_1", now, {"broker_order_id": "B-1"})
    assert o.broker_order_id == "B-1"
    oms.on_broker_event(o, "fill", "brk_f1", now, {"quantity": "40", "price": "70000", "fee": "10"})
    assert o.state is BrokerOrderState.PARTIALLY_FILLED and o.filled_quantity == Decimal("40")
    assert o.leaves_quantity == Decimal("60")
    oms.on_broker_event(o, "fill", "brk_f2", now, {"quantity": "60", "price": "70050", "fee": "15"})
    assert o.state is BrokerOrderState.FILLED and o.filled_quantity == Decimal("100")
    assert o.average_fill_price == Decimal("70030")
    # Intent 상태는 브로커 체결에 끌려가지 않는다. 두 머신은 분리돼 있다.
    assert rec.state is IntentState.READY_TO_SUBMIT, "Broker 상태가 Intent로 새어나갔다"

    # 4. 멱등성 - 같은 브로커 체결 이벤트 재수신 (DoD 3번)
    oms.on_broker_event(o, "fill", "brk_f2", now, {"quantity": "60", "price": "70050"})
    assert o.filled_quantity == Decimal("100"), "중복 체결이 잡혔다"
    assert len(o.fills) == 2

    # 5. 멱등성 - 같은 idempotency_key 재등록, 같은 Intent로 주문 재생성
    again = oms.register_intent(make_intent("idem_0001"))
    assert again.order_intent_id == rec.order_intent_id, "중복 Intent가 생겼다"
    assert len(oms.store.intents) == 1
    assert oms.create_broker_order(rec, i).order_id == o.order_id, "중복 Broker Order가 생겼다"
    assert len(oms.store.orders) == 1

    # 6. 초과 체결 거부 (DoD - filled <= requested)
    oms3 = OMS()
    i3, rec3, o3 = approved_order(oms3, "idem_0003")
    oms3.submit(o3, rec3)
    oms3.on_broker_event(o3, "ack", "b3_ack", now)
    raises(lambda: oms3.on_broker_event(o3, "fill", "b3_f1", now,
                                        {"quantity": "101", "price": "70000"}), "초과 체결")
    assert o3.filled_quantity == Decimal(0)

    # 7. Risk 승인 만료 후 전송 차단
    oms4 = OMS()
    i4 = make_intent("idem_0004")
    rec4 = oms4.register_intent(i4)
    oms4.request_risk_review(rec4)
    oms4.apply_risk_decision(rec4, approval(i4, minutes=1))
    o4 = oms4.create_broker_order(rec4, i4)
    raises(lambda: oms4.submit(o4, rec4, when=now + timedelta(minutes=2)), "만료된 승인")

    # 8. 축소 승인(RESIZED)은 Broker Order 수량을 줄인다
    oms5 = OMS()
    i5 = make_intent("idem_0005")
    rec5 = oms5.register_intent(i5)
    oms5.request_risk_review(rec5)
    oms5.apply_risk_decision(rec5, approval(i5, qty="40", verdict=RiskVerdict.RESIZE))
    assert rec5.state is IntentState.READY_TO_SUBMIT
    o5 = oms5.create_broker_order(rec5, i5)
    assert o5.requested_quantity == Decimal("40"), "축소 승인이 반영 안 됨"
    oms5.submit(o5, rec5)
    assert o5.state is BrokerOrderState.SUBMITTED

    # 9. 상태 불명은 추정하지 않고, Reconciliation으로만 벗어난다
    oms6 = OMS()
    i6, rec6, o6 = approved_order(oms6, "idem_0006")
    oms6.submit(o6, rec6)
    oms6.mark_unknown(o6, "브로커 응답 없음")
    assert o6.state is BrokerOrderState.UNKNOWN
    assert not o6.is_terminal, "UNKNOWN을 종료 상태로 취급했다"
    raises(lambda: oms6.on_broker_event(o6, "fill", "b6_f0", now,
                                        {"quantity": "100", "price": "70000"}),
           "확정 안 된 이벤트로 UNKNOWN 탈출")
    # 같은 Fund의 신규 주문도 막힌다 (가이드 4.3)
    i6b = make_intent("idem_0006b")
    rec6b = oms6.register_intent(i6b)
    oms6.request_risk_review(rec6b)
    oms6.apply_risk_decision(rec6b, approval(i6b))
    raises(lambda: oms6.create_broker_order(rec6b, i6b), "UNKNOWN 미해소 상태의 신규 주문")
    oms6.on_broker_event(o6, "fill", "b6_f1", now, {"quantity": "100", "price": "70000"},
                         reconciled=True)
    assert o6.state is BrokerOrderState.FILLED, "Reconciliation 후 상태 복구 실패"
    oms6.create_broker_order(rec6b, i6b)  # 해소됐으므로 이제 통과한다

    # 10. 취소는 CANCEL_PENDING을 거친다. 요청 즉시 취소로 쓰지 않는다
    oms7 = OMS()
    i7, rec7, o7 = approved_order(oms7, "idem_0007")
    oms7.submit(o7, rec7)
    oms7.on_broker_event(o7, "ack", "b7_ack", now)
    oms7.request_cancel(o7, "장 마감 임박")
    assert o7.state is BrokerOrderState.CANCEL_PENDING and not o7.is_terminal
    # 취소 요청 중에도 체결이 올 수 있다
    oms7.on_broker_event(o7, "fill", "b7_f1", now, {"quantity": "30", "price": "70000"})
    assert o7.state is BrokerOrderState.PARTIALLY_FILLED
    oms7.request_cancel(o7, "재요청")
    oms7.on_broker_event(o7, "cancel", "b7_cxl", now, {"leaves": "70"})
    assert o7.state is BrokerOrderState.CANCELLED and o7.filled_quantity == Decimal("30")

    # 11. 이벤트에서 상태 재구축 (DoD 4번) - 두 스트림 모두
    for oms_i, rec_i, ord_i in ((oms, rec, o), (oms3, rec3, o3), (oms7, rec7, o7)):
        assert oms_i.rebuild_state("intent", rec_i.order_intent_id) == str(rec_i.state)
        assert oms_i.rebuild_state("broker_order", ord_i.order_id) == str(ord_i.state)

    print("ok - OMS 불변식 11개 점검 통과")
