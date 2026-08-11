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
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID, uuid4

_DEPT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_DEPT / "contracts"))
sys.path.insert(0, str(_DEPT / "multileg"))
sys.path.insert(0, str(_DEPT / "capability"))

from contracts import (
    BROKER_TERMINAL_STATES,
    BrokerOrderState,
    ExecutionAuthority,
    IntentState,
    OrderIntent,
    RiskDecision,
    Side,
    can_transition,
)
from strategy_switch import SWITCHBOARD, StrategyDisabled, StrategySwitchboard
from derivatives import (
    CERTIFICATION_REQUIRED,
    CapabilityProfile,
    DerivativeContract,
    check_order_capability,
)
from intent_group import (
    GroupStatus,
    IntentGroup,
    LegOutcome,
    RecoveryPlan,
)
from intent_group import evaluate_group as _evaluate_group


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
    # ▶ 실행 권한 출처 (2026-08-11)
    #   Risk 승인은 "한도 안인가" 만 답한다. 자본을 걸어도 되는지는 사람 서명이
    #   답하고, 그 서명이 **전략 승격**인지 **건별 승인**인지가 여기 남는다.
    #   기본이 None 인 것이 핵심이다 - 아무도 허락하지 않은 주문은 나가지 않는다.
    authority: ExecutionAuthority | None = None
    authority_ref: str | None = None          # promotion_id 또는 approval_id
    authority_expires_at: datetime | None = None
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
    """인메모리 저장소. **OMS가 쓰는 저장소 인터페이스가 곧 이 클래스의 메서드다.**

    OMS는 dict를 직접 만지지 않고 아래 메서드만 부른다. 그래야 같은 인터페이스의
    psycopg 구현(`store_postgres.PostgresOrderStore`)을 끼워 넣을 수 있고, 그때
    OMS의 상태 머신 코드는 한 줄도 안 바뀐다 - 회계 원장에서 `post()` 하나만
    가로챘던 것과 같은 이유다.

    `intents`/`orders`/`events` 속성은 인메모리 구현의 내부 자료구조이며
    **저장소 인터페이스가 아니다.** 밖에서 읽어야 하면 메서드를 쓴다.
    """

    def __init__(self) -> None:
        self.intents: dict[UUID, OrderIntentRecord] = {}
        self.orders: dict[UUID, BrokerOrder] = {}
        self.events: list[StateEvent] = []
        self.groups: dict[UUID, IntentGroup] = {}
        self._by_idempotency: dict[str, UUID] = {}
        self._order_by_intent: dict[UUID, UUID] = {}
        self._seen_broker_events: set[tuple[str, str]] = set()
        self._group_by_idempotency: dict[str, UUID] = {}
        # (intent_group_id, leg_index) -> order_id. F30 판정이 실제 주문 상태를
        # 읽으려면 Leg와 BrokerOrder가 연결돼 있어야 한다.
        self._order_by_leg: dict[tuple[UUID, int], UUID] = {}
        self._intent_bodies: dict[UUID, OrderIntent] = {}

    # -- 쓰기 -----------------------------------------------------------------

    def add_intent(self, rec: OrderIntentRecord, intent: OrderIntent) -> None:
        """심사 기록과 원본 계약을 함께 받는다.

        인메모리는 idempotency_key만 쓰지만, DB 구현은 `execution.order_intents`의
        NOT NULL 컬럼(instrument_id, side, order_type, book_id...)을 채워야 해서
        원본이 필요하다. 저장소마다 다른 인자를 받게 하면 그게 곧 두 인터페이스다.
        """
        self.intents[rec.order_intent_id] = rec
        self._by_idempotency[intent.idempotency_key] = rec.order_intent_id
        self._intent_bodies[rec.order_intent_id] = intent

    def add_order(self, order: BrokerOrder) -> None:
        self.orders[order.order_id] = order
        self._order_by_intent[order.order_intent_id] = order.order_id

    def add_group(self, group: IntentGroup) -> None:
        self.groups[group.intent_group_id] = group
        self._group_by_idempotency[group.idempotency_key] = group.intent_group_id

    def link_order_to_leg(self, group_id: UUID, leg_index: int, order_id: UUID) -> None:
        self._order_by_leg[(group_id, leg_index)] = order_id

    def add_event(self, event: StateEvent) -> None:
        """상태 변화가 지나가는 유일한 길목. DB 구현은 여기서 Projection도 갱신한다."""
        self.events.append(event)
        if event.broker_event_id is not None:
            self._seen_broker_events.add((event.broker_adapter, event.broker_event_id))

    def add_fill(self, order: BrokerOrder, fill: Fill) -> None:
        """체결 사실. 주문을 함께 받는 것은 DB 구현이 instrument/side/통화를 알아야
        `execution.fills`를 채울 수 있기 때문이다 - Fill 자체는 그걸 모른다."""
        order.fills.append(fill)

    def apply_fill(
        self,
        order: BrokerOrder,
        event: StateEvent,
        fill: Fill,
        *,
        filled_quantity: Decimal,
        average_fill_price: Decimal,
    ) -> None:
        """Apply one Fill as a caller-owned atomic operation."""
        if self.seen_broker_event(event.broker_adapter, event.broker_event_id):
            return
        prior_state, prior_version, prior_filled = (
            order.state, order.version, order.filled_quantity
        )
        prior_event_count, prior_fill_count = len(self.events), len(order.fills)
        try:
            self.add_event(event)
            order.state = BrokerOrderState(event.to_state)
            order.version += 1
            order.filled_quantity = filled_quantity
            self.add_fill(order, fill)
            if order.average_fill_price != average_fill_price:
                raise OMSError("누적 체결 평균가 계산이 일치하지 않습니다")
        except Exception:
            order.state, order.version, order.filled_quantity = (
                prior_state, prior_version, prior_filled
            )
            del self.events[prior_event_count:]
            del order.fills[prior_fill_count:]
            self._seen_broker_events = {
                (e.broker_adapter, e.broker_event_id)
                for e in self.events
                if e.broker_event_id is not None
            }
            raise

    def reconstruct_intent(self, intent_id: UUID) -> OrderIntent | None:
        return self._intent_bodies.get(intent_id)

    def existing_broker_event(
        self, adapter: str, broker_event_id: str | None
    ) -> StateEvent | None:
        if broker_event_id is None:
            return None
        return next(
            (
                event for event in self.events
                if event.broker_adapter == adapter
                and event.broker_event_id == broker_event_id
            ),
            None,
        )

    def existing_fill(self, order_id: UUID, broker_fill_id: str | None) -> Fill | None:
        if broker_fill_id is None:
            return None
        order = self.orders.get(order_id)
        if order is None:
            return None
        return next(
            (fill for fill in order.fills if fill.broker_fill_id == broker_fill_id),
            None,
        )
    def validate_risk_decision(
        self, rec: OrderIntentRecord, decision: RiskDecision
    ) -> None:
        return None

    def save_risk_decision(
        self, rec: OrderIntentRecord, decision: RiskDecision
    ) -> None:
        return None

    def revalidate_risk_decision(
        self, rec: OrderIntentRecord, order: BrokerOrder
    ) -> None:
        return None
    # -- 읽기 -----------------------------------------------------------------

    def get_intent(self, intent_id: UUID) -> OrderIntentRecord | None:
        return self.intents.get(intent_id)

    def get_order(self, order_id: UUID) -> BrokerOrder | None:
        return self.orders.get(order_id)

    def list_intents(self, limit: int | None = None) -> list[OrderIntentRecord]:
        values = list(self.intents.values())
        return values[:limit] if limit else values

    def list_orders(self, limit: int | None = None) -> list[BrokerOrder]:
        values = list(self.orders.values())
        return values[:limit] if limit else values

    def find_unknown_order(self, fund_id: UUID) -> BrokerOrder | None:
        """가이드 4.3: UNKNOWN이면 그 Fund의 신규 주문을 막는다.

        ponytail: 전체 주문 스캔이다. DB 구현에서는 orders(fund_id, state) 조회
                  한 방이 된다. 인메모리 Paper 규모에서는 이걸로 충분하다.
        """
        return next(
            (o for o in self.orders.values()
             if o.fund_id == fund_id and o.state is BrokerOrderState.UNKNOWN),
            None,
        )

    def find_intent_by_idempotency(self, key: str) -> OrderIntentRecord | None:
        intent_id = self._by_idempotency.get(key)
        return self.intents.get(intent_id) if intent_id else None

    def find_group_by_idempotency(self, key: str) -> IntentGroup | None:
        group_id = self._group_by_idempotency.get(key)
        return self.groups.get(group_id) if group_id else None

    def find_order_by_leg(self, group_id: UUID, leg_index: int) -> BrokerOrder | None:
        order_id = self._order_by_leg.get((group_id, leg_index))
        return self.orders.get(order_id) if order_id else None

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
    def __init__(self, store: OrderStore | None = None, adapter: str = "paper",
                 capability: CapabilityProfile | None = None,
                 strategy_switchboard: StrategySwitchboard | None = None) -> None:
        self.store = store or OrderStore()
        self.adapter = adapter
        # 전략 가동 스위치. 기본은 공용 판이지만 시험에서는 갈아끼운다.
        # 전송 시점에 여기를 다시 본다(`submit`).
        self.strategy_switchboard = strategy_switchboard or SWITCHBOARD
        # F31. 선언이 없으면 현물(EQUITY)만 받는다 - 파생·공매도는 Profile과
        # Certification이 있어야 열린다. 없는 것이 곧 허용이 되지 않게 한다.
        self.capability = capability

    # -- 1단계: Order Intent 심사 ------------------------------------------------

    def register_intent(
        self,
        intent: OrderIntent,
        *,
        asset_class: str = "EQUITY",
        contract: DerivativeContract | None = None,
        as_of: date | None = None,
    ) -> OrderIntentRecord:
        """Agent가 만든 Intent를 심사 대기로 등록한다.

        같은 idempotency_key로 다시 부르면 기존 기록을 그대로 돌려준다.
        네트워크 재시도로 주문이 두 배가 나가는 사고를 막는다.

        **여기가 F31 Capability 게이트다.** 팀 가이드 1.1: "실행·회계 Capability가
        없는 Strategy Version은 승인 신호가 있어도 주문 접수 단계에서 차단한다."
        Risk 승인보다 앞이라, 막히면 Risk 심사 자체가 시작되지 않는다.
        """
        self._check_capability(asset_class, contract, as_of)

        existing = self.store.find_intent_by_idempotency(intent.idempotency_key)
        if existing is not None:
            existing_body = self.store.reconstruct_intent(existing.order_intent_id)
            if existing_body is not None and existing_body.evidence_hash() != intent.evidence_hash():
                raise OMSError("같은 idempotency_key의 다른 Intent 사실입니다")
            return existing

        rec = OrderIntentRecord(
            order_intent_id=intent.order_intent_id,
            fund_id=intent.fund_id,
            idempotency_key=intent.idempotency_key,
            requested_quantity=intent.quantity,
            valid_until=intent.valid_until,
        )
        self.store.add_intent(rec, intent)
        self._append("intent", rec.order_intent_id, "intent_registered",
                     IntentState.DRAFT, IntentState.DRAFT,
                     {"trace_id": intent.trace_id, "evidence_hash": intent.evidence_hash()})
        return rec

    def _check_capability(
        self, asset_class: str, contract: DerivativeContract | None, as_of: date | None
    ) -> None:
        """접수 게이트. 막히면 Intent가 만들어지지 않는다."""
        if self.capability is None:
            # Profile 선언이 없다 = 아무것도 인증되지 않았다. 현물만 통과시킨다.
            # 초기 시장은 KOSPI/KOSDAQ 현물 Long-only다(마스터플랜 10장).
            if asset_class in CERTIFICATION_REQUIRED:
                raise OMSError(
                    f"{asset_class}는 Capability Profile 없이 주문할 수 없습니다. "
                    "Broker/Risk/Accounting Certification이 선행 조건입니다"
                )
            return

        verdict = check_order_capability(
            self.capability, asset_class, contract=contract, as_of=as_of
        )
        if not verdict.allowed:
            raise OMSError(f"Capability 게이트 차단: {verdict.reason}")

    def request_risk_review(self, rec: OrderIntentRecord) -> OrderIntentRecord:
        return self._move_intent(rec, IntentState.RISK_PENDING, "risk_requested", {})

    def apply_risk_decision(self, rec: OrderIntentRecord, decision: RiskDecision) -> OrderIntentRecord:
        """리스크본부 판정을 반영한다. 판정 없이는 Intent가 앞으로 못 간다.

        판정은 우리가 만들지 않는다. 받아서 검증하고 기록만 한다.
        """
        if decision.order_intent_id != rec.order_intent_id:
            raise OMSError("다른 Intent의 Risk 판정입니다")
        self.store.validate_risk_decision(rec, decision)

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

        self.store.save_risk_decision(rec, decision)
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

        blocked = self.store.find_unknown_order(rec.fund_id)
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
        self.store.add_order(order)
        self._append("broker_order", order.order_id, "order_created",
                     BrokerOrderState.CREATED, BrokerOrderState.CREATED,
                     {"order_intent_id": str(rec.order_intent_id),
                      "risk_decision_id": str(rec.risk_decision_id)})
        return order

    # -- Multi-leg (F30) --------------------------------------------------------

    def register_group(self, group: IntentGroup) -> IntentGroup:
        """Intent Group을 등록한다. 같은 idempotency_key면 기존 것을 돌려준다."""
        existing = self.store.find_group_by_idempotency(group.idempotency_key)
        if existing is not None:
            return existing

        self.store.add_group(group)
        self._append("intent_group", group.intent_group_id, "group_registered",
                     GroupStatus.DRAFT, GroupStatus.DRAFT,
                     {"atomicity": str(group.atomicity_policy),
                      "failure_policy": str(group.failure_policy),
                      "leg_count": group.leg_count})
        return group

    def link_leg(self, group: IntentGroup, leg_index: int, order: BrokerOrder) -> None:
        """Leg와 Broker Order를 연결한다. 판정이 실제 주문 상태를 읽으려면 필요하다."""
        group.leg_at(leg_index)  # 없는 leg_index면 여기서 걸린다
        linked = self.store.find_order_by_leg(group.intent_group_id, leg_index)
        if linked is not None and linked.order_id != order.order_id:
            raise OMSError(f"leg {leg_index}에 이미 다른 주문이 연결돼 있습니다")
        self.store.link_order_to_leg(group.intent_group_id, leg_index, order.order_id)

    def evaluate_group(self, group: IntentGroup) -> RecoveryPlan:
        """연결된 Broker Order 상태를 읽어 그룹 판정을 낸다.

        결과를 지어내지 않는다 - 아직 연결 안 된 Leg는 판정에서 빠지고,
        그러면 evaluate_group이 EXECUTING을 돌려준다. 덜 온 결과를
        "미체결"로 읽어 성급하게 복구를 시작하지 않기 위해서다.
        """
        outcomes: dict[int, LegOutcome] = {}
        for leg in group.legs:
            order = self.store.find_order_by_leg(group.intent_group_id, leg.leg_index)
            if order is None:
                continue
            outcomes[leg.leg_index] = LegOutcome(
                leg_index=leg.leg_index,
                filled_quantity=order.filled_quantity,
                requested_quantity=order.requested_quantity,
                is_terminal=order.is_terminal,
                state=str(order.state),
            )

        plan = _evaluate_group(group, outcomes)
        if plan.group_status is not group.status:
            self._append("intent_group", group.intent_group_id, "group_evaluated",
                         group.status, plan.group_status,
                         {"reason": plan.reason,
                          "cancel": list(plan.cancel_leg_indexes),
                          "unwind": list(plan.unwind_leg_indexes),
                          "retry": list(plan.retry_leg_indexes),
                          "reduce_only": plan.reduce_only})
            group.status = plan.group_status
            group.version += 1
        return plan

    def submit(self, order: BrokerOrder, rec: OrderIntentRecord,
               when: datetime | None = None) -> BrokerOrder:
        """브로커 전송. 여기가 Risk Gate의 마지막 관문이다.

        승인 시점과 전송 시점 사이에 판정이 만료되거나 수량이 바뀌었을 수 있다.
        생성 때 통과했다고 전송 때도 통과라고 가정하지 않는다 (가이드 4.3).
        """
        when = when or _now()

        if rec.risk_decision_id is not None:
            self.store.revalidate_risk_decision(rec, order)
        if rec.risk_decision_id is None:
            raise OMSError("Risk 승인이 없는 주문은 전송할 수 없습니다")
        # ▶ 실행 권한 관문 (2026-08-11)
        #   Risk 승인은 "한도 안인가" 만 답한다. **자본을 걸어도 되는가** 는
        #   사람 서명이 답하고, 그 서명이 어디 있는지가 authority 다.
        #   없으면 보내지 않는다 - 기본값이 None 인 것이 이 관문의 핵심이다.
        if rec.authority is None or not (rec.authority_ref or "").strip():
            raise OMSError(
                "실행 권한 출처가 없는 주문은 전송할 수 없습니다 "
                "(전략 승격 또는 사용자 건별 승인이 필요합니다)"
            )
        if rec.authority_expires_at is not None and when >= rec.authority_expires_at:
            raise OMSError("실행 권한이 만료됐습니다. 재승인이 필요합니다")
        if rec.authority is ExecutionAuthority.STRATEGY:
            # **전송 시점에 다시 본다.** 생성 때 켜져 있었다고 지금도 켜져 있다고
            # 가정하지 않는다 - 사용자가 스위치를 내린 순간, 아직 안 나간 주문은
            # 나가면 안 된다(Risk 재검증과 같은 이유).
            try:
                self.strategy_switchboard.require_enabled(rec.authority_ref or "")
            except StrategyDisabled as exc:
                raise OMSError(str(exc)) from exc
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
        existing_event = self.store.existing_broker_event(self.adapter, broker_event_id)
        if existing_event is not None:
            same_payload = (
                existing_event.payload.get("quantity") == payload.get("quantity")
                and existing_event.payload.get("price") == payload.get("price")
                if event_type == "fill"
                else existing_event.payload == payload
            )
            if existing_event.stream_id != order.order_id or (
                existing_event.event_type != event_type or not same_payload
            ):
                raise OMSError("같은 broker_event_id의 내용이 서로 다릅니다")
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
            raise OMSError(
                f"초과 체결: 기체결 {order.filled_quantity} + {qty} > 주문 {order.requested_quantity}"
            )

        fee = Decimal(str(payload.get("fee", 0)))
        tax = Decimal(str(payload.get("tax", 0)))
        broker_fill_id = payload.get("broker_fill_id")
        existing_fill = self.store.existing_fill(order.order_id, broker_fill_id)
        if existing_fill is not None:
            if (
                existing_fill.quantity != qty
                or existing_fill.price != price
                or existing_fill.fee != fee
                or existing_fill.tax != tax
            ):
                raise OMSError("같은 broker_fill_id의 내용이 서로 다릅니다")
            return order

        target = (
            BrokerOrderState.FILLED
            if order.filled_quantity + qty == order.requested_quantity
            else BrokerOrderState.PARTIALLY_FILLED
        )
        event = self._build_event(
            "broker_order", order.order_id, "fill", order.state, target, payload,
            broker_event_id=broker_event_id, event_time=event_time,
        )
        fill = Fill(
            fill_id=uuid4(),
            order_id=order.order_id,
            event_id=event.event_id,
            quantity=qty,
            price=price,
            fee=fee,
            tax=tax,
            event_time=event_time,
            broker_fill_id=broker_fill_id,
        )
        new_filled = order.filled_quantity + qty
        notional = sum(f.quantity * f.price for f in order.fills) + qty * price
        average = notional / new_filled
        self.store.apply_fill(
            order, event, fill, filled_quantity=new_filled,
            average_fill_price=average,
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

    def _build_event(self, stream, stream_id, event_type, from_state, to_state, payload, *,
                     broker_event_id=None, event_time=None) -> StateEvent:
        return StateEvent(
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

    def _append(self, stream, stream_id, event_type, from_state, to_state, payload, *,
                broker_event_id=None, event_time=None) -> StateEvent:
        event = self._build_event(
            stream, stream_id, event_type, from_state, to_state, payload,
            broker_event_id=broker_event_id, event_time=event_time,
        )
        self.store.add_event(event)
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
                          bid=Decimal(70000), ask=Decimal(70100))
    FUND = uuid4()
    common = {
        "trade_case_id": uuid4(), "fund_id": FUND, "book_id": uuid4(),
        "strategy_id": uuid4(), "instrument_id": uuid4(),
        "side": Side.BUY, "order_type": OrderType.LIMIT, "limit_price": Decimal(70000),
        "time_in_force": TimeInForce.DAY, "valid_until": now + timedelta(hours=1),
        "snapshot": snap, "created_by": "trader-pm-agent", "trace_id": "t1", "created_at": now,
    }

    def make_intent(key="idem_0001", qty="100", **over) -> OrderIntent:
        return OrderIntent(**{**common, **over}, quantity=Decimal(qty), idempotency_key=key)

    def approval(intent, qty="100", verdict=RiskVerdict.APPROVE, minutes=5) -> RiskDecision:
        return RiskDecision(
            order_intent_id=intent.order_intent_id, verdict=verdict,
            approved_quantity=Decimal(qty), expires_at=now + timedelta(minutes=minutes),
            decided_by="risk-supervisor", decided_at=now,
        )

    def raises(fn, why, exc=OMSError):
        try:
            fn()
        except exc:
            return
        raise AssertionError(f"막혔어야 함: {why}")

    _TEST_STRATEGY = "STRAT-TEST"

    def grant(oms, rec):
        """자체 점검용: 전략 권한 + 스위치 ON. 본 코드 경로가 아니라 시험 준비다."""
        rec.authority = ExecutionAuthority.STRATEGY
        rec.authority_ref = _TEST_STRATEGY
        oms.strategy_switchboard.enable(
            _TEST_STRATEGY, actor="user:self-check", reason="자체 점검"
        )
        return rec

    def approved_order(oms, key, qty="100", **over):
        """심사를 통과시켜 전송 직전까지 만든다.

        Risk 승인만으로는 전송되지 않는다(2026-08-11) - 자본을 걸어도 되는지는
        사람 서명이 답한다. 여기서는 전략 권한을 붙이고 스위치를 켜 둔다.
        """
        i = make_intent(key, qty, **over)
        rec = oms.register_intent(i)
        oms.request_risk_review(rec)
        oms.apply_risk_decision(rec, approval(i, qty))
        rec.authority = ExecutionAuthority.STRATEGY
        rec.authority_ref = _TEST_STRATEGY
        oms.strategy_switchboard.enable(
            _TEST_STRATEGY, actor="user:self-check", reason="자체 점검"
        )
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
    # ▶ Risk 승인만으로는 못 나간다 - 실행 권한 관문(2026-08-11)
    raises(lambda: oms.submit(o, rec), "실행 권한 없는 전송")
    rec.authority = ExecutionAuthority.STRATEGY
    rec.authority_ref = _TEST_STRATEGY
    # ▶ 승격(자격)만으로도 못 나간다 - 사용자가 스위치를 켜야 한다
    oms.strategy_switchboard.disable(_TEST_STRATEGY, actor="user:self-check", reason="시험")
    raises(lambda: oms.submit(o, rec), "꺼진 전략의 전송")
    oms.strategy_switchboard.enable(_TEST_STRATEGY, actor="user:self-check", reason="시험")
    oms.submit(o, rec)
    oms.on_broker_event(o, "ack", "brk_ack_1", now, {"broker_order_id": "B-1"})
    assert o.broker_order_id == "B-1"
    oms.on_broker_event(o, "fill", "brk_f1", now, {"quantity": "40", "price": "70000", "fee": "10"})
    assert o.state is BrokerOrderState.PARTIALLY_FILLED and o.filled_quantity == Decimal(40)
    assert o.leaves_quantity == Decimal(60)
    oms.on_broker_event(o, "fill", "brk_f2", now, {"quantity": "60", "price": "70050", "fee": "15"})
    assert o.state is BrokerOrderState.FILLED and o.filled_quantity == Decimal(100)
    assert o.average_fill_price == Decimal(70030)
    # Intent 상태는 브로커 체결에 끌려가지 않는다. 두 머신은 분리돼 있다.
    assert rec.state is IntentState.READY_TO_SUBMIT, "Broker 상태가 Intent로 새어나갔다"

    # 4. 멱등성 - 같은 브로커 체결 이벤트 재수신 (DoD 3번)
    oms.on_broker_event(o, "fill", "brk_f2", now, {"quantity": "60", "price": "70050"})
    assert o.filled_quantity == Decimal(100), "중복 체결이 잡혔다"
    assert len(o.fills) == 2

    # 5. 멱등성 - 같은 idempotency_key 재등록, 같은 Intent로 주문 재생성
    again = oms.register_intent(make_intent("idem_0001"))
    assert again.order_intent_id == rec.order_intent_id, "중복 Intent가 생겼다"
    assert len(oms.store.list_intents()) == 1
    assert oms.create_broker_order(rec, i).order_id == o.order_id, "중복 Broker Order가 생겼다"
    assert len(oms.store.list_orders()) == 1

    # 6. 초과 체결 거부 (DoD - filled <= requested)
    oms3 = OMS()
    i3, rec3, o3 = approved_order(oms3, "idem_0003")
    grant(oms3, rec3)

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
    grant(oms4, rec4)

    raises(lambda: oms4.submit(o4, rec4, when=now + timedelta(minutes=2)), "만료된 승인")

    # 8. 축소 승인(RESIZED)은 Broker Order 수량을 줄인다
    oms5 = OMS()
    i5 = make_intent("idem_0005")
    rec5 = oms5.register_intent(i5)
    oms5.request_risk_review(rec5)
    oms5.apply_risk_decision(rec5, approval(i5, qty="40", verdict=RiskVerdict.RESIZE))
    assert rec5.state is IntentState.READY_TO_SUBMIT
    o5 = oms5.create_broker_order(rec5, i5)
    assert o5.requested_quantity == Decimal(40), "축소 승인이 반영 안 됨"
    grant(oms5, rec5)

    oms5.submit(o5, rec5)
    assert o5.state is BrokerOrderState.SUBMITTED

    # 9. 상태 불명은 추정하지 않고, Reconciliation으로만 벗어난다
    oms6 = OMS()
    i6, rec6, o6 = approved_order(oms6, "idem_0006")
    grant(oms6, rec6)

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
    grant(oms7, rec7)

    oms7.submit(o7, rec7)
    oms7.on_broker_event(o7, "ack", "b7_ack", now)
    oms7.request_cancel(o7, "장 마감 임박")
    assert o7.state is BrokerOrderState.CANCEL_PENDING and not o7.is_terminal
    # 취소 요청 중에도 체결이 올 수 있다
    oms7.on_broker_event(o7, "fill", "b7_f1", now, {"quantity": "30", "price": "70000"})
    assert o7.state is BrokerOrderState.PARTIALLY_FILLED
    oms7.request_cancel(o7, "재요청")
    oms7.on_broker_event(o7, "cancel", "b7_cxl", now, {"leaves": "70"})
    assert o7.state is BrokerOrderState.CANCELLED and o7.filled_quantity == Decimal(30)

    # 11. 이벤트에서 상태 재구축 (DoD 4번) - 두 스트림 모두
    for oms_i, rec_i, ord_i in ((oms, rec, o), (oms3, rec3, o3), (oms7, rec7, o7)):
        assert oms_i.rebuild_state("intent", rec_i.order_intent_id) == str(rec_i.state)
        assert oms_i.rebuild_state("broker_order", ord_i.order_id) == str(ord_i.state)

    # 12. F31 Capability 게이트 — Profile이 없으면 파생·공매도가 접수에서 막힌다
    from derivatives import (
        DERIVATIVE_CERTIFIERS,
        ContractKind,
        ProfileStatus,
        SettlementType,
    )

    plain = OMS()
    for blocked_class in ("FUTURE", "OPTION", "SHORT", "WARRANT"):
        raises(lambda a=blocked_class: plain.register_intent(
            make_intent(f"idem_cap_{a}"), asset_class=a),
            f"Profile 없이 {blocked_class} 접수")
    # 현물은 그대로 통과한다 (초기 시장 KOSPI/KOSDAQ 현물)
    assert plain.register_intent(make_intent("idem_cap_eq")).state is IntentState.DRAFT

    # Certification이 다 있어야 파생이 열린다
    def cap(**kw) -> CapabilityProfile:
        base = {"capability_profile_id": uuid4(), "profile_code": "p", "version": 1,
                    "status": ProfileStatus.ACTIVE,
                    "required_instruments": frozenset({"EQUITY", "FUTURE"}),
                    "execution_capabilities": frozenset({"limit"}),
                    "risk_capabilities": frozenset({"position_limit"}),
                    "accounting_capabilities": frozenset({"double_entry"})}
        return CapabilityProfile(**{**base, **kw})

    half = OMS(capability=cap(certified_by=frozenset({"broker"})))
    raises(lambda: half.register_intent(make_intent("idem_cap_half"),
                                        asset_class="FUTURE"),
           "일부만 Certification된 파생 접수")

    full = OMS(capability=cap(certified_by=DERIVATIVE_CERTIFIERS))
    assert full.register_intent(make_intent("idem_cap_full"), asset_class="FUTURE")

    # 만기 지난 계약은 Certification이 있어도 막힌다
    expired = DerivativeContract(
        instrument_id=uuid4(), contract_kind=ContractKind.FUTURE,
        expiry_date=date(2020, 1, 1), contract_multiplier=Decimal(250000),
        settlement_type=SettlementType.CASH)
    raises(lambda: full.register_intent(make_intent("idem_cap_exp"),
                                        asset_class="FUTURE", contract=expired,
                                        as_of=date(2026, 7, 31)), "만기 지난 계약 접수")

    # 게이트에서 막히면 Intent 자체가 안 생긴다 — Risk 심사가 시작되지 않는다
    assert plain.store.find_intent_by_idempotency("idem_cap_FUTURE") is None, \
        "차단됐는데 Intent가 저장됐다"

    # 13. F30 Multi-leg 배선 — 실제 Broker Order 상태에서 판정이 나온다
    from intent_group import (
        AtomicityPolicy,
        FailurePolicy,
        Leg,
        PositionEffect,
    )

    oms8 = OMS()
    inst_a, inst_b = uuid4(), uuid4()
    i8a, rec8a, o8a = approved_order(oms8, "idem_leg_a")
    i8b, rec8b, o8b = approved_order(oms8, "idem_leg_b")

    grp = IntentGroup(
        intent_group_id=uuid4(), trade_case_id=uuid4(), fund_id=i8a.fund_id,
        atomicity_policy=AtomicityPolicy.ALL_OR_NONE,
        failure_policy=FailurePolicy.CANCEL_ALL,
        idempotency_key="grp_wired",
        legs=(Leg(leg_index=0, instrument_id=inst_a, side=Side.BUY,
                  quantity=Decimal(100), position_effect=PositionEffect.OPEN),
              Leg(leg_index=1, instrument_id=inst_b, side=Side.SELL,
                  quantity=Decimal(100), position_effect=PositionEffect.OPEN)),
    )
    oms8.register_group(grp)
    # 멱등 — 같은 키로 다시 부르면 같은 그룹이다
    assert oms8.register_group(grp).intent_group_id == grp.intent_group_id
    assert len(oms8.store.groups) == 1

    oms8.link_leg(grp, 0, o8a)
    oms8.link_leg(grp, 1, o8b)
    # 계약 위반은 MultiLegError 그대로 올라온다 - OMSError로 감싸 사유를 흐리지 않는다
    from intent_group import MultiLegError
    raises(lambda: oms8.link_leg(grp, 9, o8a), "없는 leg_index 연결", MultiLegError)

    # 아직 아무 체결도 없다 -> 판정을 서두르지 않는다
    assert oms8.evaluate_group(grp).group_status is GroupStatus.EXECUTING

    grant(oms8, rec8a)

    oms8.submit(o8a, rec8a)
    grant(oms8, rec8b)

    oms8.submit(o8b, rec8b)
    oms8.on_broker_event(o8a, "ack", "wf_a_ack", now)
    oms8.on_broker_event(o8b, "ack", "wf_b_ack", now)
    oms8.on_broker_event(o8a, "fill", "wf_a", now, {"quantity": "100", "price": "70000"})
    oms8.request_cancel(o8b, "짝 Leg가 안 채워짐")
    oms8.on_broker_event(o8b, "cancel", "wf_b", now, {"leaves": "100"})

    # 한쪽만 체결 -> COMPLETED가 아니라 PARTIAL_RECOVERY (상태를 숨기지 않는다)
    plan = oms8.evaluate_group(grp)
    assert plan.group_status is GroupStatus.PARTIAL_RECOVERY, plan.group_status
    assert plan.unwind_leg_indexes == (0,), plan.unwind_leg_indexes
    assert grp.status is GroupStatus.PARTIAL_RECOVERY, "그룹 상태가 갱신 안 됐다"

    # 그룹 전이도 event store에 남는다. 중간 상태를 건너뛰지 않는다
    group_events = oms8.store.events_for("intent_group", grp.intent_group_id)
    assert [(e.from_state, e.to_state) for e in group_events] == [
        ("DRAFT", "DRAFT"),                    # group_registered
        ("DRAFT", "EXECUTING"),                # 연결만 되고 체결 전
        ("EXECUTING", "PARTIAL_RECOVERY"),     # 한쪽만 체결
    ], [(e.event_type, e.from_state, e.to_state) for e in group_events]

    # 같은 상태로 다시 판정해도 중복 이벤트가 쌓이지 않는다
    before = len(group_events)
    oms8.evaluate_group(grp)
    assert len(oms8.store.events_for("intent_group", grp.intent_group_id)) == before, \
        "상태가 안 바뀌었는데 이벤트가 쌓였다"

    print("ok - OMS 불변식 13개 점검 통과")
