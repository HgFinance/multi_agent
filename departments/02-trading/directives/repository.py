"""Durable repositories for the local PAPER user-directive lane."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from types import SimpleNamespace
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterator, Protocol
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from broker.paper_policy import fill_costs
from broker.paper_broker import PaperBroker

from .auth import DirectiveProof
from .contracts import (
    DirectiveAction,
    DirectiveLegState,
    DirectiveState,
    UserDirectiveRequest,
)


ACTIVE_LEG_STATES = {
    DirectiveLegState.PENDING,
    DirectiveLegState.ACKNOWLEDGED,
    DirectiveLegState.PARTIALLY_FILLED,
    DirectiveLegState.UNKNOWN,
}
ACTIVE_DIRECTIVE_STATES = {
    DirectiveState.RECEIVED,
    DirectiveState.RUNNING,
    DirectiveState.IN_PROGRESS,
    DirectiveState.UNKNOWN,
}


def _cash_affordable_quantity(
    proposed: Decimal,
    budget: Decimal,
    price: Decimal,
    lot_size: Decimal,
) -> Decimal:
    quantity = min(proposed, (budget / price // lot_size) * lot_size)
    while quantity > 0:
        fee, tax = fill_costs(quantity, price, "BUY")
        if quantity * price + fee + tax <= budget:
            return quantity
        quantity -= lot_size
    return Decimal(0)


class DirectiveRepositoryError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class InstrumentRef:
    instrument_id: UUID
    symbol: str
    lot_size: Decimal
    tick_size: Decimal | None
    currency: str


@dataclass
class DirectiveLeg:
    leg_id: UUID
    directive_id: UUID
    leg_index: int
    instrument_id: UUID | None
    symbol: str | None
    side: str | None
    order_type: str | None
    requested_quantity: Decimal | None
    limit_price: Decimal | None
    filled_quantity: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None
    reduce_only: bool = False
    state: DirectiveLegState = DirectiveLegState.PENDING
    linked_order_id: UUID | None = None
    client_order_id: str | None = None
    broker_order_id: str | None = None
    broker_event_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    expires_at: datetime | None = None
    # Cancellation audit legs keep the already-filled amount of their target
    # here.  ``filled_quantity`` remains reserved for executable order legs.
    target_filled_quantity: Decimal = Decimal(0)

    def view(self) -> dict[str, Any]:
        return {
            "leg_id": str(self.leg_id),
            "leg_index": self.leg_index,
            "instrument_id": str(self.instrument_id) if self.instrument_id else None,
            "symbol": self.symbol,
            "side": self.side,
            "order_type": self.order_type,
            "requested_quantity": str(self.requested_quantity) if self.requested_quantity is not None else None,
            "limit_price": str(self.limit_price) if self.limit_price is not None else None,
            "filled_quantity": str(self.filled_quantity),
            "average_fill_price": (
                str(self.average_fill_price)
                if self.average_fill_price is not None
                else None
            ),
            "reduce_only": self.reduce_only,
            "state": self.state.value,
            "linked_order_id": str(self.linked_order_id) if self.linked_order_id else None,
            "client_order_id": self.client_order_id,
            "broker_order_id": self.broker_order_id,
            "broker_event_id": self.broker_event_id,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "target_filled_quantity": str(self.target_filled_quantity),
        }


@dataclass
class DirectiveRecord:
    directive_id: UUID
    user_id: UUID
    fund_id: UUID
    book_id: UUID
    action: DirectiveAction
    instruction_ref: str
    idempotency_key: str
    payload: dict[str, Any]
    payload_sha256: str
    priority: int
    state: DirectiveState = DirectiveState.RECEIVED
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    error_code: str | None = None
    error_message: str | None = None
    legs: list[DirectiveLeg] = field(default_factory=list)

    def view(self) -> dict[str, Any]:
        return {
            "directive_id": str(self.directive_id),
            "state": self.state.value,
            "action": self.action.value,
            "priority": self.priority,
            "fund_id": str(self.fund_id),
            "book_id": str(self.book_id),
            "instruction_ref": self.instruction_ref,
            "idempotency_key": self.idempotency_key,
            "payload_sha256": self.payload_sha256,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "legs": [leg.view() for leg in sorted(self.legs, key=lambda item: item.leg_index)],
        }


@dataclass
class PaperDirectiveFill:
    """Immutable direct-lane PAPER fill evidence.

    It deliberately references a user-directive leg, not a strategy
    ``order_intent`` or ``order``.  This prevents the direct user lane from
    fabricating Alpha/Risk evidence merely to enter the accounting pipeline.
    """

    fill_id: UUID
    leg_id: UUID
    directive_id: UUID
    quote_event_key: str
    broker_fill_id: str
    instrument_id: UUID
    side: str
    quantity: Decimal
    price: Decimal
    fee: Decimal
    tax: Decimal
    currency: str
    event_time: datetime
    source: str
    accounting_acknowledged: bool = False


class DirectiveRepository(Protocol):
    def accept(self, request: UserDirectiveRequest, proof: DirectiveProof) -> tuple[DirectiveRecord, bool]: ...
    def get(self, directive_id: UUID) -> DirectiveRecord | None: ...
    def claim(self, directive_id: UUID) -> bool: ...
    def set_state(self, directive_id: UUID, state: DirectiveState, *, error_code: str | None = None, error_message: str | None = None) -> DirectiveRecord: ...
    def touch_active(self, directive_id: UUID) -> bool: ...
    def book_guard(self, fund_id: UUID, book_id: UUID) -> Iterator[None]: ...
    def activate_barrier(self, record: DirectiveRecord, *, reduce_only: bool) -> None: ...
    def release_barrier(self, record: DirectiveRecord) -> None: ...
    def resolve_instrument(self, fund_id: UUID, book_id: UUID, instrument_id: UUID | None, symbol: str) -> InstrumentRef: ...
    def available_cash(self, fund_id: UUID, book_id: UUID, currency: str) -> Decimal: ...
    def sellable_quantity(self, fund_id: UUID, book_id: UUID, instrument_id: UUID) -> Decimal: ...
    def average_cost(self, fund_id: UUID, book_id: UUID, instrument_id: UUID) -> Decimal: ...
    def positions(self, fund_id: UUID, book_id: UUID) -> list[tuple[InstrumentRef, Decimal]]: ...
    def market_session_close(self, *, now: datetime) -> datetime: ...
    def create_pending_leg(self, record: DirectiveRecord, instrument: InstrumentRef, *, side: str, order_type: str, quantity: Decimal, limit_price: Decimal | None, reserve_cash: Decimal | None, reduce_only: bool, expires_at: datetime) -> DirectiveLeg: ...
    def create_acknowledged_leg(self, record: DirectiveRecord, instrument: InstrumentRef, *, side: str, order_type: str, quantity: Decimal, limit_price: Decimal | None, reserve_cash: Decimal | None, reduce_only: bool, expires_at: datetime) -> DirectiveLeg: ...
    def acknowledge_broker_leg(self, record: DirectiveRecord, leg: DirectiveLeg, *, broker_order_id: str, broker_event_id: str) -> DirectiveLeg: ...
    def mark_broker_leg_unknown(self, record: DirectiveRecord, leg: DirectiveLeg, *, error_code: str, error_message: str) -> DirectiveLeg: ...
    def terminate_broker_leg(self, record: DirectiveRecord, leg: DirectiveLeg, *, state: DirectiveLegState, error_code: str | None = None, error_message: str | None = None) -> DirectiveLeg: ...
    def record_paper_fill(self, record: DirectiveRecord, leg: DirectiveLeg, instrument: InstrumentRef, *, quote_event_key: str, price: Decimal, executable_quantity: Decimal, event_time: datetime, source: str) -> DirectiveLeg: ...
    def external_open_legs(self, record: DirectiveRecord, *, below_priority: int | None) -> list[tuple[DirectiveRecord, DirectiveLeg]]: ...
    def record_external_cancel(self, record: DirectiveRecord, target_record: DirectiveRecord, target_leg: DirectiveLeg, *, target_state: DirectiveLegState | None, audit_state: DirectiveLegState, broker_cancel_order_id: str | None = None, error_code: str | None = None, error_message: str | None = None) -> DirectiveLeg: ...
    def external_cancel_targets(self, record: DirectiveRecord) -> list[tuple[DirectiveLeg, DirectiveRecord, DirectiveLeg]]: ...
    def cancel_open_orders(self, record: DirectiveRecord, *, below_priority: int | None, include_direct_legs: bool = True) -> list[DirectiveLeg]: ...
    def reconcile_cancel_legs(self, record: DirectiveRecord) -> DirectiveRecord: ...
    def expire_open_legs(self, record: DirectiveRecord, *, now: datetime) -> list[DirectiveLeg]: ...
    def expire_scope_legs(self, fund_id: UUID, book_id: UUID, *, now: datetime) -> list[DirectiveRecord]: ...
    def open_sell_quantity(self, fund_id: UUID, book_id: UUID) -> Decimal: ...
    def active_directives(self, *, limit: int = 100) -> list[DirectiveRecord]: ...
    def has_unaccounted_fills(self, directive_id: UUID) -> bool: ...
    def has_unaccounted_buy_fills(self, fund_id: UUID, book_id: UUID) -> bool: ...


@dataclass
class _MemoryState:
    directives: dict[UUID, DirectiveRecord] = field(default_factory=dict)
    idempotency: dict[tuple[UUID, UUID, UUID, str], UUID] = field(default_factory=dict)
    proof_jtis: dict[str, UUID] = field(default_factory=dict)
    memberships: set[tuple[UUID, UUID]] = field(default_factory=set)
    books: set[tuple[UUID, UUID]] = field(default_factory=set)
    instruments: dict[UUID, InstrumentRef] = field(default_factory=dict)
    symbol_map: dict[str, set[UUID]] = field(default_factory=dict)
    session_opens_at: datetime | None = None
    session_closes_at: datetime | None = None
    positions: dict[tuple[UUID, UUID, UUID], Decimal] = field(default_factory=dict)
    average_costs: dict[tuple[UUID, UUID, UUID], Decimal] = field(default_factory=dict)
    cash: dict[tuple[UUID, UUID, str], Decimal] = field(default_factory=dict)
    reservations: dict[UUID, tuple[str, UUID, UUID, UUID | None, Decimal, str | None, bool]] = field(default_factory=dict)
    barriers: dict[tuple[UUID, UUID], tuple[UUID, int, bool]] = field(default_factory=dict)
    lower_orders: list[DirectiveLeg] = field(default_factory=list)
    direct_fills: dict[tuple[UUID, str], PaperDirectiveFill] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock)
    book_locks: dict[tuple[UUID, UUID], threading.RLock] = field(default_factory=dict)


class InMemoryDirectiveRepository:
    """Explicit test/fixture repository. Never selected silently in production."""

    def __init__(self, state: _MemoryState | None = None) -> None:
        self.state = state or _MemoryState()

    def grant(self, user_id: UUID, fund_id: UUID, book_id: UUID) -> None:
        with self.state.lock:
            self.state.memberships.add((user_id, fund_id))
            self.state.books.add((fund_id, book_id))

    def add_instrument(self, instrument: InstrumentRef) -> None:
        with self.state.lock:
            self.state.instruments[instrument.instrument_id] = instrument
            self.state.symbol_map.setdefault(instrument.symbol, set()).add(instrument.instrument_id)

    def set_market_session(self, opens_at: datetime, closes_at: datetime) -> None:
        self.state.session_opens_at = opens_at
        self.state.session_closes_at = closes_at

    def set_position(
        self,
        fund_id: UUID,
        book_id: UUID,
        instrument_id: UUID,
        quantity: Decimal,
        *,
        average_cost: Decimal | None = None,
    ) -> None:
        self.state.positions[(fund_id, book_id, instrument_id)] = Decimal(quantity)
        if average_cost is not None:
            self.state.average_costs[(fund_id, book_id, instrument_id)] = Decimal(average_cost)

    def set_cash(self, fund_id: UUID, book_id: UUID, currency: str, amount: Decimal) -> None:
        self.state.cash[(fund_id, book_id, currency)] = Decimal(amount)

    def add_lower_order(self, leg: DirectiveLeg) -> None:
        self.state.lower_orders.append(leg)

    def accept(self, request: UserDirectiveRequest, proof: DirectiveProof) -> tuple[DirectiveRecord, bool]:
        key = (proof.subject, request.fund_id, request.book_id, request.idempotency_key)
        with self.state.lock:
            if (proof.subject, request.fund_id) not in self.state.memberships:
                raise DirectiveRepositoryError("TRADING_FUND_ACCESS_DENIED", "user is not an active fund member", 403)
            if (request.fund_id, request.book_id) not in self.state.books:
                raise DirectiveRepositoryError("TRADING_BOOK_SCOPE_DENIED", "book is not active in fund", 403)
            existing_id = self.state.idempotency.get(key)
            existing = self.state.directives.get(existing_id) if existing_id else None
            used_by = self.state.proof_jtis.get(proof.jti)
            if used_by is not None:
                raise DirectiveRepositoryError("TRADING_PROOF_REPLAY", "proof jti was already consumed", 409)
            if existing is not None:
                if (
                    existing.action != request.action
                    or existing.instruction_ref != request.instruction_ref
                    or existing.payload_sha256 != request.payload_sha256()
                ):
                    raise DirectiveRepositoryError(
                        "TRADING_IDEMPOTENCY_CONFLICT",
                        "same idempotency key has different canonical directive content",
                        409,
                    )
                self.state.proof_jtis[proof.jti] = existing.directive_id
                return existing, False
            record = DirectiveRecord(
                directive_id=uuid4(),
                user_id=proof.subject,
                fund_id=request.fund_id,
                book_id=request.book_id,
                action=request.action,
                instruction_ref=request.instruction_ref,
                idempotency_key=request.idempotency_key,
                payload=request.canonical_payload(),
                payload_sha256=request.payload_sha256(),
                priority=request.priority,
            )
            self.state.directives[record.directive_id] = record
            self.state.idempotency[key] = record.directive_id
            self.state.proof_jtis[proof.jti] = record.directive_id
            return record, True

    def get(self, directive_id: UUID) -> DirectiveRecord | None:
        return self.state.directives.get(directive_id)

    def claim(self, directive_id: UUID) -> bool:
        with self.state.lock:
            record = self.state.directives[directive_id]
            if record.state is not DirectiveState.RECEIVED:
                return False
            record.state = DirectiveState.RUNNING
            record.updated_at = datetime.now(timezone.utc)
            return True

    def set_state(self, directive_id: UUID, state: DirectiveState, *, error_code: str | None = None, error_message: str | None = None) -> DirectiveRecord:
        with self.state.lock:
            record = self.state.directives[directive_id]
            record.state = state
            record.error_code = error_code
            record.error_message = error_message
            record.updated_at = datetime.now(timezone.utc)
            record.completed_at = record.updated_at if state is DirectiveState.COMPLETED else None
            return record

    def touch_active(self, directive_id: UUID) -> bool:
        with self.state.lock:
            record = self.state.directives.get(directive_id)
            if record is None or record.state not in ACTIVE_DIRECTIVE_STATES:
                return False
            record.updated_at = datetime.now(timezone.utc)
            return True

    @contextmanager
    def book_guard(self, fund_id: UUID, book_id: UUID) -> Iterator[None]:
        key = (fund_id, book_id)
        with self.state.lock:
            lock = self.state.book_locks.setdefault(key, threading.RLock())
        with lock:
            yield

    def activate_barrier(self, record: DirectiveRecord, *, reduce_only: bool) -> None:
        key = (record.fund_id, record.book_id)
        current = self.state.barriers.get(key)
        if current and current[0] != record.directive_id and current[1] > record.priority:
            raise DirectiveRepositoryError("TRADING_HIGHER_PRIORITY_ACTIVE", "higher-priority directive is active", 409)
        self.state.barriers[key] = (record.directive_id, record.priority, reduce_only)

    def release_barrier(self, record: DirectiveRecord) -> None:
        key = (record.fund_id, record.book_id)
        if self.state.barriers.get(key, (None,))[0] == record.directive_id:
            self.state.barriers.pop(key, None)
            candidates = [
                item
                for item in self.state.directives.values()
                if item.directive_id != record.directive_id
                and item.fund_id == record.fund_id
                and item.book_id == record.book_id
                and item.state in ACTIVE_DIRECTIVE_STATES
            ]
            if candidates:
                elected = max(
                    candidates,
                    key=lambda item: (item.priority, item.created_at, str(item.directive_id)),
                )
                reduce_only = (
                    elected.action is DirectiveAction.SELL_ALL
                    or (
                        elected.action is DirectiveAction.PLACE_ORDER
                        and elected.payload.get("side") == "SELL"
                    )
                )
                self.state.barriers[key] = (
                    elected.directive_id,
                    elected.priority,
                    reduce_only,
                )

    def resolve_instrument(self, fund_id: UUID, book_id: UUID, instrument_id: UUID | None, symbol: str) -> InstrumentRef:
        symbol_ids = self.state.symbol_map.get(symbol, set())
        if len(symbol_ids) > 1:
            raise DirectiveRepositoryError(
                "TRADING_INSTRUMENT_AMBIGUOUS",
                "multiple active instruments match the KRX symbol",
                409,
            )
        resolved_id = instrument_id or (next(iter(symbol_ids)) if symbol_ids else None)
        instrument = self.state.instruments.get(resolved_id) if resolved_id else None
        if instrument is None or instrument.symbol != symbol or instrument.instrument_id not in symbol_ids:
            raise DirectiveRepositoryError("TRADING_INSTRUMENT_NOT_FOUND", "active canonical instrument was not found", 422)
        return instrument

    def available_cash(self, fund_id: UUID, book_id: UUID, currency: str) -> Decimal:
        gross = self.state.cash.get((fund_id, book_id, currency), Decimal(0))
        reserved = sum(
            amount for kind, f, b, _instrument, amount, cur, active in self.state.reservations.values()
            if active and kind == "CASH" and f == fund_id and b == book_id and cur == currency
        )
        return max(gross - reserved, Decimal(0))

    def sellable_quantity(self, fund_id: UUID, book_id: UUID, instrument_id: UUID) -> Decimal:
        gross = self.state.positions.get((fund_id, book_id, instrument_id), Decimal(0))
        reserved = sum(
            amount for kind, f, b, inst, amount, _cur, active in self.state.reservations.values()
            if active and kind == "POSITION" and f == fund_id and b == book_id and inst == instrument_id
        )
        return max(gross - reserved, Decimal(0))

    def average_cost(self, fund_id: UUID, book_id: UUID, instrument_id: UUID) -> Decimal:
        return self.state.average_costs.get(
            (fund_id, book_id, instrument_id), Decimal(0)
        )

    def positions(self, fund_id: UUID, book_id: UUID) -> list[tuple[InstrumentRef, Decimal]]:
        result = []
        for (fund, book, instrument_id), quantity in self.state.positions.items():
            if fund == fund_id and book == book_id and quantity > 0:
                instrument = self.state.instruments.get(instrument_id)
                if instrument is None:
                    raise DirectiveRepositoryError("TRADING_INSTRUMENT_NOT_FOUND", "position instrument is not active", 409)
                result.append((instrument, quantity))
        return sorted(result, key=lambda item: item[0].symbol)

    def market_session_close(self, *, now: datetime) -> datetime:
        opens_at, closes_at = self.state.session_opens_at, self.state.session_closes_at
        if (
            opens_at is None
            or closes_at is None
            or opens_at.tzinfo is None
            or closes_at.tzinfo is None
            or not (opens_at <= now < closes_at)
        ):
            raise DirectiveRepositoryError(
                "TRADING_MARKET_SESSION_CLOSED",
                "a current open KRX REGULAR session is required",
                409,
            )
        return closes_at

    def create_pending_leg(self, record: DirectiveRecord, instrument: InstrumentRef, *, side: str, order_type: str, quantity: Decimal, limit_price: Decimal | None, reserve_cash: Decimal | None, reduce_only: bool, expires_at: datetime) -> DirectiveLeg:
        index = len(record.legs)
        leg_id = uuid5(NAMESPACE_URL, f"paper-directive:{record.directive_id}:leg:{index}:{instrument.instrument_id}")
        leg = DirectiveLeg(
            leg_id=leg_id,
            directive_id=record.directive_id,
            leg_index=index,
            instrument_id=instrument.instrument_id,
            symbol=instrument.symbol,
            side=side,
            order_type=order_type,
            requested_quantity=quantity,
            limit_price=limit_price,
            reduce_only=reduce_only,
            state=DirectiveLegState.PENDING,
            client_order_id=f"paper_user_{leg_id.hex}",
            expires_at=expires_at,
        )
        record.legs.append(leg)
        if side == "SELL":
            self.state.reservations[leg_id] = (
                "POSITION", record.fund_id, record.book_id, instrument.instrument_id,
                quantity, None, True,
            )
        elif reserve_cash is not None:
            self.state.reservations[leg_id] = (
                "CASH", record.fund_id, record.book_id, None,
                reserve_cash, instrument.currency, True,
            )
        return leg

    def create_acknowledged_leg(self, record: DirectiveRecord, instrument: InstrumentRef, *, side: str, order_type: str, quantity: Decimal, limit_price: Decimal | None, reserve_cash: Decimal | None, reduce_only: bool, expires_at: datetime) -> DirectiveLeg:
        leg = self.create_pending_leg(
            record,
            instrument,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            reserve_cash=reserve_cash,
            reduce_only=reduce_only,
            expires_at=expires_at,
        )
        return self.acknowledge_broker_leg(
            record,
            leg,
            broker_order_id=f"paper:{leg.leg_id}",
            broker_event_id=f"paper:ack:{leg.leg_id}",
        )

    def acknowledge_broker_leg(self, record: DirectiveRecord, leg: DirectiveLeg, *, broker_order_id: str, broker_event_id: str) -> DirectiveLeg:
        with self.state.lock:
            if leg.state not in {DirectiveLegState.PENDING, DirectiveLegState.UNKNOWN}:
                return leg
            leg.state = DirectiveLegState.ACKNOWLEDGED
            leg.broker_order_id = broker_order_id
            leg.broker_event_id = broker_event_id
            leg.error_code = None
            leg.error_message = None
            return leg

    def mark_broker_leg_unknown(self, record: DirectiveRecord, leg: DirectiveLeg, *, error_code: str, error_message: str) -> DirectiveLeg:
        with self.state.lock:
            if leg.state in {
                DirectiveLegState.FILLED,
                DirectiveLegState.CANCELLED,
                DirectiveLegState.REJECTED,
                DirectiveLegState.EXPIRED,
            }:
                return leg
            leg.state = DirectiveLegState.UNKNOWN
            leg.error_code = error_code
            leg.error_message = error_message
            return leg

    def terminate_broker_leg(self, record: DirectiveRecord, leg: DirectiveLeg, *, state: DirectiveLegState, error_code: str | None = None, error_message: str | None = None) -> DirectiveLeg:
        if state not in {
            DirectiveLegState.REJECTED,
            DirectiveLegState.CANCELLED,
            DirectiveLegState.EXPIRED,
        }:
            raise DirectiveRepositoryError(
                "TRADING_BROKER_STATE_INVALID", "broker terminal state is invalid", 500
            )
        with self.state.lock:
            leg.state = state
            leg.error_code = error_code
            leg.error_message = error_message
            reservation = self.state.reservations.get(leg.leg_id)
            if reservation is not None:
                pending = [
                    fill
                    for fill in self.state.direct_fills.values()
                    if fill.leg_id == leg.leg_id and not fill.accounting_acknowledged
                ]
                if not pending:
                    self.state.reservations.pop(leg.leg_id, None)
            return leg

    def record_paper_fill(
        self,
        record: DirectiveRecord,
        leg: DirectiveLeg,
        instrument: InstrumentRef,
        *,
        quote_event_key: str,
        price: Decimal,
        executable_quantity: Decimal,
        event_time: datetime,
        source: str,
    ) -> DirectiveLeg:
        """Record at most one fill for a canonical quote event.

        The in-memory implementation mirrors the database constraints closely
        enough for deterministic unit tests.  Reservations remain active until
        the accounting acknowledgement boundary; seeing a fill is not the same
        thing as seeing its Journal projection.
        """
        key = (leg.leg_id, quote_event_key)
        with self.state.lock:
            if key in self.state.direct_fills:
                return leg
            if leg.state not in {
                DirectiveLegState.ACKNOWLEDGED,
                DirectiveLegState.PARTIALLY_FILLED,
            }:
                return leg
            if price <= 0 or executable_quantity <= 0:
                return leg
            remaining = (leg.requested_quantity or Decimal(0)) - leg.filled_quantity
            quantity = min(remaining, executable_quantity)
            quantity = (quantity // instrument.lot_size) * instrument.lot_size
            if leg.side == "BUY":
                reservation = self.state.reservations.get(leg.leg_id)
                if reservation is None or not reservation[-1] or reservation[0] != "CASH":
                    raise DirectiveRepositoryError(
                        "TRADING_PAPER_RESERVATION_MISSING",
                        "active BUY reservation is required before PAPER fill",
                        409,
                    )
                pending_gross = sum(
                    item.quantity * item.price + item.fee + item.tax
                    for item in self.state.direct_fills.values()
                    if item.leg_id == leg.leg_id and not item.accounting_acknowledged
                )
                fill_budget = max(Decimal(0), reservation[4] - pending_gross)
                affordable = _cash_affordable_quantity(
                    quantity, fill_budget, price, instrument.lot_size
                )
                quantity = min(quantity, affordable)
            elif leg.side == "SELL":
                reservation = self.state.reservations.get(leg.leg_id)
                if reservation is None or not reservation[-1] or reservation[0] != "POSITION":
                    raise DirectiveRepositoryError(
                        "TRADING_PAPER_RESERVATION_MISSING",
                        "active SELL reservation is required before PAPER fill",
                        409,
                    )
                pending_quantity = sum(
                    item.quantity
                    for item in self.state.direct_fills.values()
                    if item.leg_id == leg.leg_id and not item.accounting_acknowledged
                )
                quantity = min(quantity, max(Decimal(0), reservation[4] - pending_quantity))
            if quantity <= 0:
                return leg
            fee, tax = fill_costs(quantity, price, str(leg.side))
            fill_id = uuid5(
                NAMESPACE_URL,
                f"paper-user-fill:{leg.leg_id}:{quote_event_key}",
            )
            fill = PaperDirectiveFill(
                fill_id=fill_id,
                leg_id=leg.leg_id,
                directive_id=record.directive_id,
                quote_event_key=quote_event_key,
                broker_fill_id=f"paper-user:{fill_id}",
                instrument_id=instrument.instrument_id,
                side=str(leg.side),
                quantity=quantity,
                price=price,
                fee=fee,
                tax=tax,
                currency=instrument.currency,
                event_time=event_time,
                source=source,
            )
            self.state.direct_fills[key] = fill
            prior_filled = leg.filled_quantity
            prior_notional = (leg.average_fill_price or Decimal(0)) * prior_filled
            leg.filled_quantity += quantity
            leg.average_fill_price = (
                prior_notional + quantity * price
            ) / leg.filled_quantity
            leg.broker_event_id = f"paper:fill:{fill_id}"
            leg.state = (
                DirectiveLegState.FILLED
                if leg.filled_quantity == leg.requested_quantity
                else DirectiveLegState.PARTIALLY_FILLED
            )
            return leg

    def acknowledge_direct_fills(self, leg_id: UUID) -> None:
        """Explicit fixture hook modelling Journal projection then ack."""
        with self.state.lock:
            reservation = self.state.reservations.get(leg_id)
            if reservation is None:
                return
            fills = [
                item
                for item in self.state.direct_fills.values()
                if item.leg_id == leg_id and not item.accounting_acknowledged
            ]
            for fill in fills:
                if fill.side == "BUY":
                    self.state.cash[(reservation[1], reservation[2], fill.currency)] = (
                        self.state.cash.get((reservation[1], reservation[2], fill.currency), Decimal(0))
                        - fill.quantity * fill.price
                    )
                    position_key = (reservation[1], reservation[2], fill.instrument_id)
                    self.state.positions[position_key] = (
                        self.state.positions.get(position_key, Decimal(0)) + fill.quantity
                    )
                    amount = fill.quantity * fill.price + fill.fee + fill.tax
                else:
                    position_key = (reservation[1], reservation[2], fill.instrument_id)
                    self.state.positions[position_key] = (
                        self.state.positions.get(position_key, Decimal(0)) - fill.quantity
                    )
                    amount = fill.quantity
                current = self.state.reservations.get(leg_id)
                if current is not None:
                    self.state.reservations[leg_id] = (
                        *current[:4],
                        max(Decimal(0), current[4] - amount),
                        current[5],
                        current[6],
                    )
                fill.accounting_acknowledged = True
            current = self.state.reservations.get(leg_id)
            directive_leg = next(
                (
                    candidate
                    for directive in self.state.directives.values()
                    for candidate in directive.legs
                    if candidate.leg_id == leg_id
                ),
                None,
            )
            if current is not None and directive_leg is not None and directive_leg.state in {
                DirectiveLegState.FILLED,
                DirectiveLegState.CANCELLED,
                DirectiveLegState.EXPIRED,
                DirectiveLegState.REJECTED,
            }:
                self.state.reservations[leg_id] = (*current[:-1], False)

    def _retain_unaccounted_fill_reservation(self, leg: DirectiveLeg) -> None:
        reservation = self.state.reservations.get(leg.leg_id)
        if reservation is None:
            return
        pending = [
            item
            for item in self.state.direct_fills.values()
            if item.leg_id == leg.leg_id and not item.accounting_acknowledged
        ]
        amount = sum(
            (
                item.quantity * item.price + item.fee + item.tax
                if reservation[0] == "CASH"
                else item.quantity
            )
            for item in pending
        )
        if amount > 0:
            self.state.reservations[leg.leg_id] = (
                *reservation[:4], amount, reservation[5], True
            )
        else:
            self.state.reservations[leg.leg_id] = (*reservation[:-1], False)

    def expire_open_legs(self, record: DirectiveRecord, *, now: datetime) -> list[DirectiveLeg]:
        expired: list[DirectiveLeg] = []
        for leg in record.legs:
            if (
                leg.state not in ACTIVE_LEG_STATES
                or leg.state is DirectiveLegState.UNKNOWN
                or leg.expires_at is None
                or leg.expires_at > now
            ):
                continue
            leg.state = DirectiveLegState.EXPIRED
            leg.error_code = "TRADING_PAPER_ORDER_EXPIRED"
            leg.error_message = "DAY order expired at the canonical KRX session close"
            self._retain_unaccounted_fill_reservation(leg)
            expired.append(leg)
        return expired

    def expire_scope_legs(self, fund_id: UUID, book_id: UUID, *, now: datetime) -> list[DirectiveRecord]:
        changed: list[DirectiveRecord] = []
        for record in list(self.state.directives.values()):
            if record.fund_id != fund_id or record.book_id != book_id:
                continue
            if self.expire_open_legs(record, now=now):
                changed.append(record)
        return changed

    def external_open_legs(
        self,
        record: DirectiveRecord,
        *,
        below_priority: int | None,
    ) -> list[tuple[DirectiveRecord, DirectiveLeg]]:
        candidates: list[tuple[DirectiveRecord, DirectiveLeg]] = []
        for other in self.state.directives.values():
            if (
                other.directive_id == record.directive_id
                or other.fund_id != record.fund_id
                or other.book_id != record.book_id
            ):
                continue
            if below_priority is not None and other.priority >= below_priority:
                continue
            candidates.extend(
                (other, leg)
                for leg in other.legs
                if leg.side is not None and leg.state in ACTIVE_LEG_STATES
            )
        return candidates

    def record_external_cancel(
        self,
        record: DirectiveRecord,
        target_record: DirectiveRecord,
        target_leg: DirectiveLeg,
        *,
        target_state: DirectiveLegState | None,
        audit_state: DirectiveLegState,
        broker_cancel_order_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> DirectiveLeg:
        if target_state is not None and target_leg.state in ACTIVE_LEG_STATES:
            target_leg.state = target_state
            target_leg.error_code = error_code
            target_leg.error_message = error_message
            if target_state is not DirectiveLegState.UNKNOWN:
                self._retain_unaccounted_fill_reservation(target_leg)

        event_id = f"ls-paper:cancel:{record.directive_id}:{target_leg.leg_id}"
        existing = next(
            (leg for leg in record.legs if leg.broker_event_id == event_id),
            None,
        )
        if existing is None:
            existing = DirectiveLeg(
                leg_id=uuid5(
                    NAMESPACE_URL,
                    f"paper-directive:{record.directive_id}:cancel:{target_leg.leg_id}",
                ),
                directive_id=record.directive_id,
                leg_index=len(record.legs),
                instrument_id=target_leg.instrument_id,
                symbol=target_leg.symbol,
                side=None,
                order_type=None,
                requested_quantity=None,
                limit_price=None,
                state=audit_state,
                broker_event_id=event_id,
                target_filled_quantity=target_leg.filled_quantity,
            )
            record.legs.append(existing)
        existing.state = audit_state
        existing.broker_order_id = (
            "ls-paper-cancel:" + broker_cancel_order_id
            if broker_cancel_order_id
            else existing.broker_order_id
        )
        existing.error_code = error_code
        existing.error_message = error_message
        existing.target_filled_quantity = target_leg.filled_quantity

        order_legs = [leg for leg in target_record.legs if leg.side is not None]
        if order_legs and not any(leg.state in ACTIVE_LEG_STATES for leg in order_legs):
            target_record.state = (
                DirectiveState.PARTIAL
                if any(leg.filled_quantity > 0 for leg in order_legs)
                else DirectiveState.FAILED
            )
            target_record.error_code = "TRADING_USER_CANCELLED"
            target_record.error_message = (
                "superseded by a higher-priority USER directive"
            )
            target_record.updated_at = datetime.now(timezone.utc)
        return existing

    def external_cancel_targets(
        self,
        record: DirectiveRecord,
    ) -> list[tuple[DirectiveLeg, DirectiveRecord, DirectiveLeg]]:
        result: list[tuple[DirectiveLeg, DirectiveRecord, DirectiveLeg]] = []
        prefix = f"ls-paper:cancel:{record.directive_id}:"
        for audit in record.legs:
            if audit.side is not None or not str(audit.broker_event_id or "").startswith(prefix):
                continue
            raw_target = str(audit.broker_event_id).removeprefix(prefix)
            try:
                target_id = UUID(raw_target)
            except ValueError:
                continue
            for target_record in self.state.directives.values():
                target = next(
                    (leg for leg in target_record.legs if leg.leg_id == target_id),
                    None,
                )
                if target is not None:
                    result.append((audit, target_record, target))
                    break
        return result

    def cancel_open_orders(
        self,
        record: DirectiveRecord,
        *,
        below_priority: int | None,
        include_direct_legs: bool = True,
    ) -> list[DirectiveLeg]:
        cancelled: list[DirectiveLeg] = []
        candidates: list[DirectiveLeg] = []
        if include_direct_legs:
            for other in self.state.directives.values():
                if other.directive_id == record.directive_id or other.fund_id != record.fund_id or other.book_id != record.book_id:
                    continue
                if below_priority is not None and other.priority >= below_priority:
                    continue
                candidates.extend(
                    leg
                    for leg in other.legs
                    if leg.side is not None and leg.state in ACTIVE_LEG_STATES
                )
        candidates.extend(
            leg
            for leg in self.state.lower_orders
            if leg.side is not None and leg.state in ACTIVE_LEG_STATES
        )
        affected_directives = {
            target.directive_id
            for target in candidates
            if target.directive_id in self.state.directives
        }
        for target in candidates:
            index = len(record.legs)
            if target.state is DirectiveLegState.UNKNOWN:
                state = DirectiveLegState.UNKNOWN
                code = "TRADING_ORDER_RECONCILIATION_REQUIRED"
            else:
                target.state = DirectiveLegState.CANCELLED
                self._retain_unaccounted_fill_reservation(target)
                state = DirectiveLegState.CANCELLED
                code = (
                    "TRADING_CANCEL_LOST_RACE"
                    if target.filled_quantity > 0
                    else None
                )
            cancel_leg = DirectiveLeg(
                leg_id=uuid5(NAMESPACE_URL, f"paper-directive:{record.directive_id}:cancel:{target.leg_id}"),
                directive_id=record.directive_id,
                leg_index=index,
                instrument_id=target.instrument_id,
                symbol=target.symbol,
                side=None,
                order_type=None,
                requested_quantity=None,
                limit_price=None,
                state=state,
                linked_order_id=target.linked_order_id,
                broker_event_id=f"paper:cancel:{record.directive_id}:{target.leg_id}",
                error_code=code,
                error_message=(
                    "order partially filled before its remainder was cancelled"
                    if code == "TRADING_CANCEL_LOST_RACE"
                    else ("order state is UNKNOWN" if code else None)
                ),
                target_filled_quantity=target.filled_quantity,
            )
            record.legs.append(cancel_leg)
            cancelled.append(cancel_leg)
        for other in self.state.directives.values():
            if (
                other.directive_id == record.directive_id
                or other.directive_id not in affected_directives
                or other.fund_id != record.fund_id
                or other.book_id != record.book_id
                or other.state not in ACTIVE_DIRECTIVE_STATES
            ):
                continue
            order_legs = [leg for leg in other.legs if leg.side is not None]
            if order_legs and not any(leg.state in ACTIVE_LEG_STATES for leg in order_legs):
                partially_filled = any(leg.filled_quantity > 0 for leg in order_legs)
                other.state = (
                    DirectiveState.PARTIAL if partially_filled else DirectiveState.FAILED
                )
                other.error_code = "TRADING_USER_CANCELLED"
                other.error_message = "superseded by a higher-priority USER directive"
                other.updated_at = datetime.now(timezone.utc)
        return cancelled

    def reconcile_cancel_legs(self, record: DirectiveRecord) -> DirectiveRecord:
        return record

    def open_sell_quantity(self, fund_id: UUID, book_id: UUID) -> Decimal:
        return sum(
            amount for kind, f, b, _inst, amount, _cur, active in self.state.reservations.values()
            if active and kind == "POSITION" and f == fund_id and b == book_id
        )

    def active_directives(self, *, limit: int = 100) -> list[DirectiveRecord]:
        if limit <= 0:
            return []
        values = [
            record
            for record in self.state.directives.values()
            if (
                record.state in ACTIVE_DIRECTIVE_STATES
                and not (
                    record.state is DirectiveState.UNKNOWN
                    and record.action is DirectiveAction.PLACE_ORDER
                    and any(leg.side is not None for leg in record.legs)
                    and all(
                        leg.state is DirectiveLegState.UNKNOWN
                        and not leg.broker_order_id
                        for leg in record.legs
                        if leg.side is not None
                    )
                )
            )
            or (
                record.state is DirectiveState.PARTIAL
                and record.action is DirectiveAction.PLACE_ORDER
                and record.error_code == "TRADING_DIRECTIVE_INTERNAL_ERROR"
                and any(leg.side is not None for leg in record.legs)
                and all(
                    leg.state is DirectiveLegState.FILLED
                    for leg in record.legs
                    if leg.side is not None
                )
                and not any(
                    leg.side is None and leg.state is DirectiveLegState.UNKNOWN
                    for leg in record.legs
                )
            )
        ]
        values.sort(
            key=lambda item: (
                -item.priority,
                item.updated_at,
                item.created_at,
                str(item.directive_id),
            )
        )
        return values[:limit]

    def has_unaccounted_fills(self, directive_id: UUID) -> bool:
        """Return whether a direct Fill still lacks accounting acknowledgement.

        The fixture flag represents the same boundary as the direct-fill ACK or
        exact ``accounting-ledger`` consumer receipt in PostgreSQL.  Recording
        a broker Fill alone is deliberately not final portfolio state.
        """

        return any(
            fill.directive_id == directive_id and not fill.accounting_acknowledged
            for fill in self.state.direct_fills.values()
        )

    def has_unaccounted_buy_fills(self, fund_id: UUID, book_id: UUID) -> bool:
        return any(
            not fill.accounting_acknowledged
            and fill.side == "BUY"
            and self.state.directives[fill.directive_id].fund_id == fund_id
            and self.state.directives[fill.directive_id].book_id == book_id
            for fill in self.state.direct_fills.values()
        )


def _load_driver():
    try:
        import psycopg2  # type: ignore[import-not-found]
        from psycopg2.extras import Json  # type: ignore[import-not-found]
    except ImportError as exc:
        raise DirectiveRepositoryError("TRADING_DIRECTIVE_DB_UNAVAILABLE", "psycopg2 is required", 503) from exc
    return psycopg2, Json


class PostgresDirectiveRepository:
    """Canonical local-control-Postgres repository for PAPER directives."""

    def __init__(self, dsn: str) -> None:
        if not str(dsn or "").strip():
            raise DirectiveRepositoryError("TRADING_DIRECTIVE_DB_UNAVAILABLE", "DATABASE_URL is required", 503)
        self.dsn = dsn
        self._local = threading.local()
        self.database_role = os.environ.get("TRADING_DATABASE_ROLE", "svc_trading_api").strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", self.database_role):
            raise DirectiveRepositoryError(
                "TRADING_DIRECTIVE_DB_ROLE_INVALID",
                "TRADING_DATABASE_ROLE is not a valid PostgreSQL role name",
                503,
            )

    def _connect(self):
        psycopg2, _ = _load_driver()
        return psycopg2.connect(self.dsn, connect_timeout=5)

    def _reduce_role(self, cur: Any) -> None:
        # The role name is validated in __init__. SET LOCAL is transaction
        # scoped and therefore remains safe with a transaction-pool endpoint.
        cur.execute(f'SET LOCAL ROLE "{self.database_role}"')

    @contextmanager
    def _cursor(self):
        inherited = getattr(self._local, "connection", None)
        if inherited is not None:
            with inherited.cursor() as cur:
                depth = int(getattr(self._local, "savepoint_depth", 0)) + 1
                self._local.savepoint_depth = depth
                savepoint = f"directive_operation_{depth}"
                cur.execute(f"SAVEPOINT {savepoint}")
                try:
                    yield cur
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                except Exception:
                    # A failed statement must not poison the book-guard
                    # transaction.  The service can now persist an honest
                    # FAILED/UNKNOWN projection while retaining the same lock.
                    cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                    raise
                finally:
                    self._local.savepoint_depth = depth - 1
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                self._reduce_role(cur)
                yield cur
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @contextmanager
    def book_guard(self, fund_id: UUID, book_id: UUID) -> Iterator[None]:
        if getattr(self._local, "connection", None) is not None:
            raise DirectiveRepositoryError("TRADING_BOOK_LOCK_REENTRANT", "book lock is already held", 500)
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                self._reduce_role(cur)
                cur.execute(
                    "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                    (f"paper:{fund_id}:{book_id}",),
                )
            self._local.connection = conn
            yield
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            self._local.connection = None
            conn.close()

    def _record(self, row, legs: list[DirectiveLeg] | None = None) -> DirectiveRecord:
        return DirectiveRecord(
            directive_id=row[0], user_id=row[1], fund_id=row[2], book_id=row[3],
            action=DirectiveAction(row[4]), instruction_ref=row[5], idempotency_key=row[6],
            payload=dict(row[7]), payload_sha256=row[8], priority=row[9],
            state=DirectiveState(row[10]), error_code=row[11], error_message=row[12],
            created_at=row[13], updated_at=row[14], completed_at=row[15], legs=legs or [],
        )

    def _leg(self, row) -> DirectiveLeg:
        return DirectiveLeg(
            leg_id=row[0], directive_id=row[1], leg_index=row[2], instrument_id=row[3],
            symbol=row[4], side=row[5], order_type=row[6], requested_quantity=row[7],
            limit_price=row[8], filled_quantity=row[9], reduce_only=row[10],
            state=DirectiveLegState(row[11]), linked_order_id=row[12],
            client_order_id=row[13], broker_order_id=row[14], broker_event_id=row[15],
            error_code=row[16], error_message=row[17], expires_at=row[18],
            target_filled_quantity=row[19], average_fill_price=row[20],
        )

    def accept(self, request: UserDirectiveRequest, proof: DirectiveProof) -> tuple[DirectiveRecord, bool]:
        _, Json = _load_driver()
        issuer = __import__("os").environ["TRADING_SERVICE_AUTH_ISSUER"]
        audience = __import__("os").environ["TRADING_SERVICE_AUTH_AUDIENCE"]
        with self._cursor() as cur:
            cur.execute(
                "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"directive:{proof.subject}:{request.fund_id}:{request.book_id}:{request.idempotency_key}",),
            )
            cur.execute(
                """
                select 1 from governance.fund_memberships fm
                 join governance.user_profiles u on u.user_id=fm.user_id and u.status='ACTIVE'
                 join accounting.books b on b.book_id=%s and b.fund_id=fm.fund_id and b.status='ACTIVE'
                 join accounting.funds f on f.fund_id=fm.fund_id and f.status='ACTIVE'
                where fm.user_id=%s and fm.fund_id=%s and fm.status='ACTIVE'
                  and fm.role in ('OWNER','CIO','TRADER')
                  and fm.effective_from <= now()
                  and (fm.effective_to is null or fm.effective_to > now())
                """,
                (request.book_id, proof.subject, request.fund_id),
            )
            if cur.fetchone() is None:
                raise DirectiveRepositoryError("TRADING_FUND_ACCESS_DENIED", "user/fund/book membership denied", 403)
            cur.execute(
                """
                select directive_id from execution.user_directives
                 where user_id=%s and fund_id=%s and book_id=%s and idempotency_key=%s
                 for update
                """,
                (proof.subject, request.fund_id, request.book_id, request.idempotency_key),
            )
            existing_row = cur.fetchone()
            created = existing_row is None
            if created:
                directive_id = uuid4()
                cur.execute(
                    """
                    insert into execution.user_directives
                      (directive_id,user_id,fund_id,book_id,action,instruction_ref,idempotency_key,
                       payload,payload_sha256,proof_issuer,proof_audience,proof_issued_at,
                       proof_not_before,proof_expires_at,priority,state)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                            to_timestamp(%s),to_timestamp(%s),to_timestamp(%s),%s,'RECEIVED')
                    """,
                    (directive_id, proof.subject, request.fund_id, request.book_id,
                     request.action.value, request.instruction_ref, request.idempotency_key,
                     Json(request.canonical_payload()), request.payload_sha256(), issuer, audience,
                     proof.issued_at, proof.not_before, proof.expires_at, request.priority),
                )
            else:
                directive_id = existing_row[0]
                cur.execute(
                    "select action,instruction_ref,payload_sha256 from execution.user_directives where directive_id=%s",
                    (directive_id,),
                )
                action, instruction_ref, digest = cur.fetchone()
                if (action, instruction_ref, digest) != (
                    request.action.value, request.instruction_ref, request.payload_sha256()
                ):
                    raise DirectiveRepositoryError(
                        "TRADING_IDEMPOTENCY_CONFLICT",
                        "same idempotency key has different canonical directive content",
                        409,
                    )
            # The proof ledger is immutable.  A SELECT ... FOR UPDATE would
            # require an UPDATE privilege the Trading role deliberately does
            # not have, and still leaves a check-then-insert race.  Let the
            # primary key arbitrate concurrent consumption atomically instead.
            cur.execute(
                """
                insert into execution.user_directive_proofs
                  (proof_jti,directive_id,user_id,fund_id,book_id,action,instruction_ref,
                   idempotency_key,payload_sha256,issued_at,expires_at)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,to_timestamp(%s),to_timestamp(%s))
                on conflict (proof_jti) do nothing
                returning directive_id
                """,
                (proof.jti, directive_id, proof.subject, request.fund_id, request.book_id,
                 request.action.value, request.instruction_ref, request.idempotency_key,
                 request.payload_sha256(), proof.issued_at, proof.expires_at),
            )
            if cur.fetchone() is None:
                raise DirectiveRepositoryError(
                    "TRADING_PROOF_REPLAY", "proof jti was already consumed", 409
                )
        record = self.get(directive_id)
        assert record is not None
        return record, created

    def get(self, directive_id: UUID) -> DirectiveRecord | None:
        with self._cursor() as cur:
            cur.execute(
                """
                select directive_id,user_id,fund_id,book_id,action,instruction_ref,idempotency_key,
                       payload,payload_sha256,priority,state,error_code,error_message,
                       created_at,updated_at,completed_at
                  from execution.user_directives where directive_id=%s
                """,
                (directive_id,),
            )
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(
                """
                select leg_id,directive_id,leg_index,instrument_id,symbol,side,order_type,
                       requested_quantity,limit_price,filled_quantity,reduce_only,state,
                       linked_order_id,client_order_id,broker_order_id,broker_event_id,
                       error_code,error_message,expires_at,target_filled_quantity,
                       (select sum(fill.quantity*fill.price)/nullif(sum(fill.quantity),0)
                          from execution.paper_user_directive_fills fill
                         where fill.leg_id=leg.leg_id) as average_fill_price
                  from execution.user_directive_legs leg
                 where leg.directive_id=%s order by leg.leg_index
                """,
                (directive_id,),
            )
            legs = [self._leg(item) for item in cur.fetchall()]
        return self._record(row, legs)

    def claim(self, directive_id: UUID) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """update execution.user_directives
                       set state='RUNNING',updated_at=now(),version=version+1
                     where directive_id=%s and state='RECEIVED'""",
                (directive_id,),
            )
            return cur.rowcount == 1

    def set_state(self, directive_id: UUID, state: DirectiveState, *, error_code: str | None = None, error_message: str | None = None) -> DirectiveRecord:
        with self._cursor() as cur:
            cur.execute(
                """
                update execution.user_directives
                   set state=%s,error_code=%s,error_message=%s,updated_at=now(),
                       completed_at=case when %s='COMPLETED' then now() else null end,
                       version=version+1
                 where directive_id=%s
                """,
                (state.value, error_code, error_message, state.value, directive_id),
            )
            if cur.rowcount != 1:
                raise DirectiveRepositoryError("TRADING_DIRECTIVE_NOT_FOUND", "directive not found", 404)
        record = self.get(directive_id)
        assert record is not None
        return record

    def touch_active(self, directive_id: UUID) -> bool:
        """Move a transiently blocked row behind its same-priority peers.

        The state predicate prevents a concurrent terminal transition from
        being overwritten merely to implement retry fairness.
        """
        with self._cursor() as cur:
            cur.execute(
                """
                update execution.user_directives
                   set updated_at=now(),version=version+1
                 where directive_id=%s
                   and state in ('RECEIVED','RUNNING','IN_PROGRESS','UNKNOWN')
                """,
                (directive_id,),
            )
            return cur.rowcount == 1

    def activate_barrier(self, record: DirectiveRecord, *, reduce_only: bool) -> None:
        with self._cursor() as cur:
            cur.execute(
                "select active_directive_id,priority from execution.paper_directive_barriers where fund_id=%s and book_id=%s for update",
                (record.fund_id, record.book_id),
            )
            current = cur.fetchone()
            if current and current[0] != record.directive_id and current[1] > record.priority:
                raise DirectiveRepositoryError("TRADING_HIGHER_PRIORITY_ACTIVE", "higher-priority directive is active", 409)
            cur.execute(
                """
                insert into execution.paper_directive_barriers
                    (fund_id,book_id,active_directive_id,priority,mode)
                values (%s,%s,%s,%s,%s)
                on conflict (fund_id,book_id) do update
                    set active_directive_id=excluded.active_directive_id,
                        priority=excluded.priority,mode=excluded.mode,updated_at=now()
                """,
                (record.fund_id, record.book_id, record.directive_id, record.priority,
                 "REDUCE_ONLY" if reduce_only else "USER_PRIORITY"),
            )

    def release_barrier(self, record: DirectiveRecord) -> None:
        with self._cursor() as cur:
            cur.execute(
                "delete from execution.paper_directive_barriers where fund_id=%s and book_id=%s and active_directive_id=%s",
                (record.fund_id, record.book_id, record.directive_id),
            )
            if cur.rowcount != 1:
                return
            cur.execute(
                """
                select directive_id,priority,
                       case
                         when action='SELL_ALL' then 'REDUCE_ONLY'
                         when action='PLACE_ORDER' and payload->>'side'='SELL'
                           then 'REDUCE_ONLY'
                         else 'USER_PRIORITY'
                       end as mode
                  from execution.user_directives
                 where fund_id=%s and book_id=%s and directive_id<>%s
                   and state in ('RECEIVED','RUNNING','IN_PROGRESS','UNKNOWN')
                 order by priority desc,created_at desc,directive_id desc
                 limit 1
                 for update
                """,
                (record.fund_id, record.book_id, record.directive_id),
            )
            elected = cur.fetchone()
            if elected is not None:
                cur.execute(
                    """
                    insert into execution.paper_directive_barriers
                      (fund_id,book_id,active_directive_id,priority,mode)
                    values (%s,%s,%s,%s,%s)
                    on conflict (fund_id,book_id) do update
                      set active_directive_id=excluded.active_directive_id,
                          priority=excluded.priority,mode=excluded.mode,
                          updated_at=now()
                    """,
                    (record.fund_id, record.book_id, elected[0], elected[1], elected[2]),
                )

    def resolve_instrument(self, fund_id: UUID, book_id: UUID, instrument_id: UUID | None, symbol: str) -> InstrumentRef:
        with self._cursor() as cur:
            cur.execute(
                """
                select distinct i.instrument_id,sy.symbol,i.lot_size,i.tick_size,i.currency
                  from reference.instruments i
                  join reference.instrument_symbols sy on sy.instrument_id=i.instrument_id
                 where i.status='ACTIVE' and i.asset_class='EQUITY'
                   and i.instrument_type='STOCK' and i.market='KRX'
                   and sy.symbol=%s and sy.symbol ~ '^[0-9A-Z]{6}$'
                   and sy.valid_from<=now()
                   and (sy.valid_to is null or sy.valid_to>now())
                   and (%s::uuid is null or i.instrument_id=%s::uuid)
                """,
                (symbol, instrument_id, instrument_id),
            )
            rows = cur.fetchall()
        if len(rows) != 1:
            code = "TRADING_INSTRUMENT_NOT_FOUND" if not rows else "TRADING_INSTRUMENT_AMBIGUOUS"
            raise DirectiveRepositoryError(code, "canonical active instrument resolution failed", 422 if not rows else 409)
        return InstrumentRef(rows[0][0], rows[0][1], rows[0][2], rows[0][3], rows[0][4])

    def market_session_close(self, *, now: datetime) -> datetime:
        if now.tzinfo is None:
            raise DirectiveRepositoryError(
                "TRADING_MARKET_TIME_INVALID", "timezone-aware market time required", 500
            )
        with self._cursor() as cur:
            cur.execute(
                """
                select s.opens_at,s.closes_at
                  from reference.market_sessions s
                  join reference.market_calendar_versions v
                    on v.calendar_version_id=s.calendar_version_id
                 where v.market='KRX' and s.market='KRX'
                   and s.session_type='REGULAR' and s.is_trading_day
                   and v.effective_from <= (%s at time zone 'Asia/Seoul')::date
                   and (v.effective_to is null
                        or v.effective_to >= (%s at time zone 'Asia/Seoul')::date)
                   and s.trade_date=(%s at time zone 'Asia/Seoul')::date
                   and s.opens_at <= %s and s.closes_at > %s
                 order by v.version desc
                 limit 1
                """,
                (now, now, now, now, now),
            )
            rows = cur.fetchall()
        if len(rows) != 1 or rows[0][0] is None or rows[0][1] is None:
            raise DirectiveRepositoryError(
                "TRADING_MARKET_SESSION_UNAVAILABLE",
                "exactly one current open KRX REGULAR session is required",
                409,
            )
        return rows[0][1]

    def available_cash(self, fund_id: UUID, book_id: UUID, currency: str) -> Decimal:
        with self._cursor() as cur:
            cur.execute(
                """
                select coalesce(sum(cb.settled_amount+cb.unsettled_amount-cb.reserved_amount),0)
                  from accounting.cash_balances cb
                  join accounting.ledger_accounts la on la.account_id=cb.account_id
                 where cb.fund_id=%s and cb.book_id=%s and cb.currency=%s
                   and la.account_code='1000' and la.status='ACTIVE'
                """,
                (fund_id, book_id, currency),
            )
            gross = Decimal(cur.fetchone()[0])
            cur.execute(
                """select coalesce(sum(reserved_cash),0)
                     from execution.paper_order_reservations
                    where fund_id=%s and book_id=%s and currency=%s
                      and reservation_type='CASH' and state='ACTIVE'""",
                (fund_id, book_id, currency),
            )
            reserved = Decimal(cur.fetchone()[0])
        return max(gross - reserved, Decimal(0))

    def sellable_quantity(self, fund_id: UUID, book_id: UUID, instrument_id: UUID) -> Decimal:
        with self._cursor() as cur:
            cur.execute(
                "select coalesce(sum(quantity),0) from accounting.positions where fund_id=%s and book_id=%s and instrument_id=%s",
                (fund_id, book_id, instrument_id),
            )
            gross = Decimal(cur.fetchone()[0])
            cur.execute(
                """select coalesce(sum(reserved_quantity),0)
                     from execution.paper_order_reservations
                    where fund_id=%s and book_id=%s and instrument_id=%s
                      and reservation_type='POSITION' and state='ACTIVE'""",
                (fund_id, book_id, instrument_id),
            )
            reserved = Decimal(cur.fetchone()[0])
        return max(gross - reserved, Decimal(0))

    def average_cost(self, fund_id: UUID, book_id: UUID, instrument_id: UUID) -> Decimal:
        with self._cursor() as cur:
            cur.execute(
                """
                select coalesce(
                         sum(quantity * average_cost)
                           / nullif(sum(quantity), 0),
                         0
                       )
                  from accounting.positions
                 where fund_id=%s and book_id=%s and instrument_id=%s
                   and quantity > 0 and average_cost is not null
                """,
                (fund_id, book_id, instrument_id),
            )
            return Decimal(cur.fetchone()[0] or 0)

    def positions(self, fund_id: UUID, book_id: UUID) -> list[tuple[InstrumentRef, Decimal]]:
        with self._cursor() as cur:
            cur.execute(
                """
                with position_totals as (
                  select instrument_id,sum(quantity) as quantity
                    from accounting.positions
                   where fund_id=%s and book_id=%s
                   group by instrument_id having sum(quantity)>0
                )
                select p.instrument_id,p.quantity,i.lot_size,i.tick_size,i.currency,
                       array_agg(distinct sy.symbol order by sy.symbol)
                         filter (where sy.symbol is not null)
                  from position_totals p
                  left join reference.instruments i on i.instrument_id=p.instrument_id
                       and i.status='ACTIVE' and i.asset_class='EQUITY'
                       and i.instrument_type='STOCK' and i.market='KRX'
                  left join reference.instrument_symbols sy on sy.instrument_id=i.instrument_id
                       and sy.symbol ~ '^[0-9A-Z]{6}$'
                       and sy.valid_from<=now() and (sy.valid_to is null or sy.valid_to>now())
                 group by p.instrument_id,p.quantity,i.lot_size,i.tick_size,i.currency
                 order by min(sy.symbol)
                """,
                (fund_id, book_id),
            )
            rows = cur.fetchall()
        result: list[tuple[InstrumentRef, Decimal]] = []
        for row in rows:
            symbols = list(row[5] or [])
            if row[2] is None or row[4] is None or len(symbols) != 1:
                raise DirectiveRepositoryError(
                    "TRADING_POSITION_INSTRUMENT_UNSUPPORTED",
                    "positive position requires exactly one active KRX stock identity",
                    409,
                )
            result.append(
                (InstrumentRef(row[0], symbols[0], row[2], row[3], row[4]), Decimal(row[1]))
            )
        return result

    def create_pending_leg(self, record: DirectiveRecord, instrument: InstrumentRef, *, side: str, order_type: str, quantity: Decimal, limit_price: Decimal | None, reserve_cash: Decimal | None, reduce_only: bool, expires_at: datetime) -> DirectiveLeg:
        _, Json = _load_driver()
        with self._cursor() as cur:
            cur.execute("select coalesce(max(leg_index),-1)+1 from execution.user_directive_legs where directive_id=%s", (record.directive_id,))
            index = int(cur.fetchone()[0])
            leg_id = uuid5(NAMESPACE_URL, f"paper-directive:{record.directive_id}:leg:{index}:{instrument.instrument_id}")
            client_id = f"paper_user_{leg_id.hex}"
            cur.execute(
                """
                insert into execution.user_directive_legs
                  (leg_id,directive_id,leg_index,instrument_id,symbol,side,order_type,
                   time_in_force,requested_quantity,limit_price,reduce_only,state,client_order_id,
                   broker_order_id,broker_event_id,expires_at)
                values (%s,%s,%s,%s,%s,%s,%s,'DAY',%s,%s,%s,'PENDING',%s,%s,%s,%s)
                on conflict (directive_id,leg_index) do nothing
                """,
                (leg_id, record.directive_id, index, instrument.instrument_id,
                 instrument.symbol, side, order_type, quantity, limit_price,
                 reduce_only, client_id, None, None, expires_at),
            )
            if side == "SELL":
                cur.execute(
                    """insert into execution.paper_order_reservations
                         (directive_id,leg_id,fund_id,book_id,instrument_id,reservation_type,reserved_quantity)
                         values (%s,%s,%s,%s,%s,'POSITION',%s)
                         on conflict (leg_id) do nothing""",
                    (record.directive_id, leg_id, record.fund_id, record.book_id,
                     instrument.instrument_id, quantity),
                )
            elif reserve_cash is not None:
                cur.execute(
                    """insert into execution.paper_order_reservations
                         (directive_id,leg_id,fund_id,book_id,reservation_type,reserved_cash,currency)
                         values (%s,%s,%s,%s,'CASH',%s,%s)
                         on conflict (leg_id) do nothing""",
                    (record.directive_id, leg_id, record.fund_id, record.book_id,
                     reserve_cash, instrument.currency),
                )
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return next(leg for leg in refreshed.legs if leg.leg_id == leg_id)

    def create_acknowledged_leg(self, record: DirectiveRecord, instrument: InstrumentRef, *, side: str, order_type: str, quantity: Decimal, limit_price: Decimal | None, reserve_cash: Decimal | None, reduce_only: bool, expires_at: datetime) -> DirectiveLeg:
        leg = self.create_pending_leg(
            record,
            instrument,
            side=side,
            order_type=order_type,
            quantity=quantity,
            limit_price=limit_price,
            reserve_cash=reserve_cash,
            reduce_only=reduce_only,
            expires_at=expires_at,
        )
        return self.acknowledge_broker_leg(
            record,
            leg,
            broker_order_id=f"paper:{leg.leg_id}",
            broker_event_id=f"paper:ack:{leg.leg_id}",
        )

    def acknowledge_broker_leg(self, record: DirectiveRecord, leg: DirectiveLeg, *, broker_order_id: str, broker_event_id: str) -> DirectiveLeg:
        with self._cursor() as cur:
            cur.execute(
                """
                update execution.user_directive_legs
                   set state='ACKNOWLEDGED',broker_order_id=%s,broker_event_id=%s,
                       error_code=null,error_message=null,updated_at=now()
                 where leg_id=%s and directive_id=%s
                   and state in ('PENDING','UNKNOWN')
                """,
                (broker_order_id, broker_event_id, leg.leg_id, record.directive_id),
            )
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return next(item for item in refreshed.legs if item.leg_id == leg.leg_id)

    def mark_broker_leg_unknown(self, record: DirectiveRecord, leg: DirectiveLeg, *, error_code: str, error_message: str) -> DirectiveLeg:
        with self._cursor() as cur:
            cur.execute(
                """
                update execution.user_directive_legs
                   set state='UNKNOWN',error_code=%s,error_message=%s,updated_at=now()
                 where leg_id=%s and directive_id=%s
                   and state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')
                """,
                (error_code, error_message[:300], leg.leg_id, record.directive_id),
            )
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return next(item for item in refreshed.legs if item.leg_id == leg.leg_id)

    def terminate_broker_leg(self, record: DirectiveRecord, leg: DirectiveLeg, *, state: DirectiveLegState, error_code: str | None = None, error_message: str | None = None) -> DirectiveLeg:
        if state not in {
            DirectiveLegState.REJECTED,
            DirectiveLegState.CANCELLED,
            DirectiveLegState.EXPIRED,
        }:
            raise DirectiveRepositoryError(
                "TRADING_BROKER_STATE_INVALID", "broker terminal state is invalid", 500
            )
        with self._cursor() as cur:
            cur.execute(
                """
                update execution.user_directive_legs
                   set state=%s,error_code=%s,error_message=%s,updated_at=now()
                 where leg_id=%s and directive_id=%s
                   and state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')
                """,
                (
                    state.value,
                    error_code,
                    error_message[:300] if error_message else None,
                    leg.leg_id,
                    record.directive_id,
                ),
            )
            self._retain_unaccounted_fill_reservation(cur, leg.leg_id)
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return next(item for item in refreshed.legs if item.leg_id == leg.leg_id)

    def record_paper_fill(
        self,
        record: DirectiveRecord,
        leg: DirectiveLeg,
        instrument: InstrumentRef,
        *,
        quote_event_key: str,
        price: Decimal,
        executable_quantity: Decimal,
        event_time: datetime,
        source: str,
    ) -> DirectiveLeg:
        """Atomically persist direct Fill evidence, leg projection and outbox."""
        if not re.fullmatch(r"[0-9a-f]{64}", quote_event_key):
            raise DirectiveRepositoryError(
                "TRADING_PAPER_QUOTE_ID_INVALID",
                "quote event fingerprint must be canonical sha256",
                500,
            )
        _, Json = _load_driver()
        with self._cursor() as cur:
            cur.execute(
                """
                select state,requested_quantity,filled_quantity,side
                  from execution.user_directive_legs
                 where leg_id=%s and directive_id=%s
                 for update
                """,
                (leg.leg_id, record.directive_id),
            )
            locked = cur.fetchone()
            if locked is None:
                raise DirectiveRepositoryError(
                    "TRADING_PAPER_LEG_NOT_FOUND", "direct PAPER leg was not found", 409
                )
            state, requested_quantity, filled_quantity, side = locked
            cur.execute(
                """
                select fill_id from execution.paper_user_directive_fills
                 where leg_id=%s and quote_event_key=%s
                """,
                (leg.leg_id, quote_event_key),
            )
            if cur.fetchone() is not None:
                refreshed = self.get(record.directive_id)
                assert refreshed is not None
                return next(item for item in refreshed.legs if item.leg_id == leg.leg_id)
            if state not in ("ACKNOWLEDGED", "PARTIALLY_FILLED"):
                refreshed = self.get(record.directive_id)
                assert refreshed is not None
                return next(item for item in refreshed.legs if item.leg_id == leg.leg_id)

            remaining = Decimal(requested_quantity) - Decimal(filled_quantity)
            quantity = min(remaining, Decimal(executable_quantity))
            quantity = (quantity // instrument.lot_size) * instrument.lot_size
            cur.execute(
                """
                select reservation_type,reserved_quantity,reserved_cash,state
                  from execution.paper_order_reservations
                 where leg_id=%s
                 for update
                """,
                (leg.leg_id,),
            )
            reservation = cur.fetchone()
            expected_type = "CASH" if side == "BUY" else "POSITION"
            if reservation is None or reservation[3] != "ACTIVE" or reservation[0] != expected_type:
                raise DirectiveRepositoryError(
                    "TRADING_PAPER_RESERVATION_MISSING",
                    f"active {expected_type} reservation is required before PAPER fill",
                    409,
                )
            cur.execute(
                """
                select coalesce(sum(
                         case when %s='CASH'
                              then gross_amount+fee_amount+tax_amount
                              else quantity end
                       ),0)
                  from execution.paper_user_directive_fills
                 where leg_id=%s and accounting_acknowledged_at is null
                """,
                (expected_type, leg.leg_id),
            )
            pending = Decimal(cur.fetchone()[0])
            reservation_amount = Decimal(
                reservation[2] if expected_type == "CASH" else reservation[1]
            )
            available_reservation = max(Decimal(0), reservation_amount - pending)
            if expected_type == "CASH":
                affordable = _cash_affordable_quantity(
                    quantity,
                    available_reservation,
                    Decimal(price),
                    instrument.lot_size,
                )
                quantity = min(quantity, affordable)
            else:
                quantity = min(quantity, available_reservation)
            if quantity <= 0:
                refreshed = self.get(record.directive_id)
                assert refreshed is not None
                return next(item for item in refreshed.legs if item.leg_id == leg.leg_id)

            fill_id = uuid5(
                NAMESPACE_URL,
                f"paper-user-fill:{leg.leg_id}:{quote_event_key}",
            )
            broker_fill_id = f"paper-user:{fill_id}"
            gross = quantity * Decimal(price)
            fee, tax = fill_costs(quantity, Decimal(price), str(side))
            trace_id = uuid5(NAMESPACE_URL, f"paper-user-directive-trace:{record.directive_id}")
            cur.execute(
                """
                insert into execution.paper_user_directive_fills
                  (fill_id,leg_id,directive_id,quote_event_key,broker_fill_id,
                   instrument_id,side,quantity,price,gross_amount,fee_amount,
                   tax_amount,currency,event_time,received_at,quote_source,trace_id)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,now(),%s,%s)
                """,
                (
                    fill_id, leg.leg_id, record.directive_id, quote_event_key,
                    broker_fill_id, instrument.instrument_id, side, quantity,
                    price, gross, fee, tax, instrument.currency, event_time, source, trace_id,
                ),
            )
            new_filled = Decimal(filled_quantity) + quantity
            new_state = "FILLED" if new_filled == Decimal(requested_quantity) else "PARTIALLY_FILLED"
            cur.execute(
                """
                update execution.user_directive_legs
                   set filled_quantity=%s,state=%s,broker_event_id=%s,updated_at=now()
                 where leg_id=%s
                """,
                (new_filled, new_state, f"paper:fill:{fill_id}", leg.leg_id),
            )
            content = {
                "fill_id": str(fill_id),
                "leg_id": str(leg.leg_id),
                "directive_id": str(record.directive_id),
                "instrument_id": str(instrument.instrument_id),
                "side": side,
                "quantity": str(quantity),
                "price": str(price),
                "fee": str(fee),
                "tax": str(tax),
                "quote_event_key": quote_event_key,
            }
            content_hash = "sha256:" + hashlib.sha256(
                json.dumps(content, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
            payload_ref = {
                "artifact_type": "FILL",
                "artifact_id": str(fill_id),
                "artifact_schema": "trading-user-directive-fill-v1",
                "content_hash": content_hash,
            }
            event_id = uuid5(NAMESPACE_URL, f"paper-user-fill-event:{fill_id}")
            outbox_key = f"fill_paper_user_{broker_fill_id}"
            cur.execute(
                """
                insert into execution.outbox
                  (event_id,event_type,schema_version,case_id,trace_id,producer,
                   occurred_at,idempotency_key,payload_ref)
                values (%s,'trading.fill.v1','event-envelope-v1',null,%s,
                        'trading-user-directive',%s,%s,%s)
                on conflict (idempotency_key) do nothing
                returning outbox_id
                """,
                (event_id, trace_id, event_time, outbox_key, Json(payload_ref)),
            )
            inserted = cur.fetchone()
            if inserted is None:
                cur.execute(
                    """
                    select event_id,payload_ref from execution.outbox
                     where idempotency_key=%s
                    """,
                    (outbox_key,),
                )
                existing = cur.fetchone()
                if existing is None or existing[0] != event_id or dict(existing[1] or {}) != payload_ref:
                    raise DirectiveRepositoryError(
                        "TRADING_PAPER_FILL_OUTBOX_CONFLICT",
                        "direct fill outbox identity conflicts with durable evidence",
                        409,
                    )
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return next(item for item in refreshed.legs if item.leg_id == leg.leg_id)

    def _insert_cancel_leg(self, cur, record: DirectiveRecord, *, linked_order_id: UUID | None, instrument_id: UUID | None, symbol: str | None, state: DirectiveLegState, target_ref: str, error_code: str | None = None, error_message: str | None = None, target_filled_quantity: Decimal = Decimal(0)) -> None:
        cur.execute("select coalesce(max(leg_index),-1)+1 from execution.user_directive_legs where directive_id=%s", (record.directive_id,))
        index = int(cur.fetchone()[0])
        leg_id = uuid5(NAMESPACE_URL, f"paper-directive:{record.directive_id}:cancel:{target_ref}")
        cur.execute(
            """
            insert into execution.user_directive_legs
              (leg_id,directive_id,leg_index,instrument_id,symbol,state,linked_order_id,
               broker_event_id,error_code,error_message,target_filled_quantity)
            values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            on conflict (leg_id) do nothing
            """,
            (leg_id, record.directive_id, index, instrument_id, symbol, state.value,
             linked_order_id, f"paper:cancel:{record.directive_id}:{target_ref}",
             error_code, error_message, target_filled_quantity),
        )

    def _event_hash(self, payload: dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def _retain_unaccounted_fill_reservation(self, cur, leg_id: UUID) -> None:
        """Release only the unfilled remainder; pending Journals stay reserved."""
        cur.execute(
            """
            select reservation_type
              from execution.paper_order_reservations
             where leg_id=%s and state='ACTIVE'
             for update
            """,
            (leg_id,),
        )
        row = cur.fetchone()
        if row is None:
            return
        reservation_type = row[0]
        cur.execute(
            """
            select coalesce(sum(
                     case when %s='CASH'
                          then gross_amount+fee_amount+tax_amount
                          else quantity end
                   ),0)
              from execution.paper_user_directive_fills
             where leg_id=%s and accounting_acknowledged_at is null
            """,
            (reservation_type, leg_id),
        )
        pending = Decimal(cur.fetchone()[0])
        if pending > 0:
            if reservation_type == "CASH":
                cur.execute(
                    """
                    update execution.paper_order_reservations
                       set reserved_cash=%s,version=version+1
                     where leg_id=%s and state='ACTIVE'
                    """,
                    (pending, leg_id),
                )
            else:
                cur.execute(
                    """
                    update execution.paper_order_reservations
                       set reserved_quantity=%s,version=version+1
                     where leg_id=%s and state='ACTIVE'
                    """,
                    (pending, leg_id),
                )
        else:
            cur.execute(
                """
                update execution.paper_order_reservations
                   set state='RELEASED',released_at=now(),version=version+1
                 where leg_id=%s and state='ACTIVE'
                """,
                (leg_id,),
            )

    def expire_open_legs(self, record: DirectiveRecord, *, now: datetime) -> list[DirectiveLeg]:
        with self._cursor() as cur:
            cur.execute(
                """
                update execution.user_directive_legs
                   set state='EXPIRED',error_code='TRADING_PAPER_ORDER_EXPIRED',
                       error_message='DAY order expired at the canonical KRX session close',
                       updated_at=now()
                 where directive_id=%s
                   and state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED')
                   and expires_at is not null and expires_at <= %s
                returning leg_id
                """,
                (record.directive_id, now),
            )
            expired_ids = [row[0] for row in cur.fetchall()]
            for leg_id in expired_ids:
                self._retain_unaccounted_fill_reservation(cur, leg_id)
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return [leg for leg in refreshed.legs if leg.leg_id in set(expired_ids)]

    def expire_scope_legs(self, fund_id: UUID, book_id: UUID, *, now: datetime) -> list[DirectiveRecord]:
        with self._cursor() as cur:
            cur.execute(
                """
                update execution.user_directive_legs l
                   set state='EXPIRED',error_code='TRADING_PAPER_ORDER_EXPIRED',
                       error_message='DAY order expired at the canonical KRX session close',
                       updated_at=now()
                  from execution.user_directives d
                 where d.directive_id=l.directive_id
                   and d.fund_id=%s and d.book_id=%s
                   and l.state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED')
                   and l.expires_at is not null and l.expires_at <= %s
                returning l.leg_id,l.directive_id
                """,
                (fund_id, book_id, now),
            )
            changed = cur.fetchall()
            leg_ids = [row[0] for row in changed]
            directive_ids = sorted({row[1] for row in changed}, key=str)
            for leg_id in leg_ids:
                self._retain_unaccounted_fill_reservation(cur, leg_id)
        records: list[DirectiveRecord] = []
        for directive_id in directive_ids:
            record = self.get(directive_id)
            if record is not None:
                records.append(record)
        return records

    def _transition_order(self, cur, record: DirectiveRecord, order_id: UUID, state: str, target_state: str, stage: str) -> str:
        _, Json = _load_driver()
        event_id = uuid5(NAMESPACE_URL, f"paper-directive:{record.directive_id}:order:{order_id}:{stage}")
        broker_event_id = f"paper:{stage}:{record.directive_id}:{order_id}"
        cur.execute("select coalesce(max(sequence),0)+1 from execution.order_events where order_id=%s", (order_id,))
        sequence = int(cur.fetchone()[0])
        payload = {"directive_id": str(record.directive_id), "reason": record.action.value}
        cur.execute(
            """
            insert into execution.order_events
              (order_event_id,order_id,event_type,event_time,received_at,broker_adapter,
               broker_event_id,from_state,to_state,payload,payload_hash,sequence,trace_id)
            values (%s,%s,%s,now(),now(),'paper',%s,%s,%s,%s,%s,%s,%s)
            on conflict (broker_adapter,broker_event_id) do nothing
            """,
            (event_id, order_id, stage, broker_event_id, state, target_state,
             Json(payload), self._event_hash(payload), sequence,
             uuid5(NAMESPACE_URL, f"paper-directive-trace:{record.directive_id}")),
        )
        cur.execute("update execution.orders set state=%s,last_event_at=now(),version=version+1 where order_id=%s and state=%s", (target_state, order_id, state))
        return target_state

    def external_open_legs(
        self,
        record: DirectiveRecord,
        *,
        below_priority: int | None,
    ) -> list[tuple[DirectiveRecord, DirectiveLeg]]:
        query = """
            select distinct d.directive_id
              from execution.user_directive_legs l
              join execution.user_directives d on d.directive_id=l.directive_id
             where d.fund_id=%s and d.book_id=%s and d.directive_id<>%s
               and l.side is not null
               and l.state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')
        """
        params: list[Any] = [record.fund_id, record.book_id, record.directive_id]
        if below_priority is not None:
            query += " and d.priority < %s"
            params.append(below_priority)
        query += " order by d.directive_id"
        with self._cursor() as cur:
            cur.execute(query, tuple(params))
            directive_ids = [row[0] for row in cur.fetchall()]

        candidates: list[tuple[DirectiveRecord, DirectiveLeg]] = []
        for directive_id in directive_ids:
            target_record = self.get(directive_id)
            if target_record is None:
                continue
            candidates.extend(
                (target_record, leg)
                for leg in target_record.legs
                if leg.side is not None and leg.state in ACTIVE_LEG_STATES
            )
        return candidates

    def record_external_cancel(
        self,
        record: DirectiveRecord,
        target_record: DirectiveRecord,
        target_leg: DirectiveLeg,
        *,
        target_state: DirectiveLegState | None,
        audit_state: DirectiveLegState,
        broker_cancel_order_id: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> DirectiveLeg:
        if target_state not in {
            None,
            DirectiveLegState.CANCELLED,
            DirectiveLegState.UNKNOWN,
        } or audit_state not in {
            DirectiveLegState.CANCELLED,
            DirectiveLegState.UNKNOWN,
            DirectiveLegState.SKIPPED,
        }:
            raise DirectiveRepositoryError(
                "TRADING_EXTERNAL_CANCEL_STATE_INVALID",
                "external cancellation state is invalid",
                500,
            )
        event_id = f"ls-paper:cancel:{record.directive_id}:{target_leg.leg_id}"
        leg_id = uuid5(
            NAMESPACE_URL,
            f"paper-directive:{record.directive_id}:cancel:{target_leg.leg_id}",
        )
        broker_cancel_ref = (
            "ls-paper-cancel:" + broker_cancel_order_id
            if broker_cancel_order_id
            else None
        )
        with self._cursor() as cur:
            if target_state is not None:
                cur.execute(
                    """
                    update execution.user_directive_legs
                       set state=%s,error_code=%s,error_message=%s,updated_at=now()
                     where leg_id=%s and directive_id=%s
                       and state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')
                    """,
                    (
                        target_state.value,
                        error_code,
                        error_message[:300] if error_message else None,
                        target_leg.leg_id,
                        target_record.directive_id,
                    ),
                )
                if target_state is DirectiveLegState.CANCELLED:
                    self._retain_unaccounted_fill_reservation(cur, target_leg.leg_id)

            cur.execute(
                "select coalesce(max(leg_index),-1)+1 from execution.user_directive_legs where directive_id=%s",
                (record.directive_id,),
            )
            index = int(cur.fetchone()[0])
            cur.execute(
                """
                insert into execution.user_directive_legs
                  (leg_id,directive_id,leg_index,instrument_id,symbol,state,
                   broker_order_id,broker_event_id,error_code,error_message,
                   target_filled_quantity)
                values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                on conflict (leg_id) do update
                   set state=excluded.state,
                       broker_order_id=coalesce(excluded.broker_order_id,
                                                execution.user_directive_legs.broker_order_id),
                       error_code=excluded.error_code,
                       error_message=excluded.error_message,
                       target_filled_quantity=excluded.target_filled_quantity,
                       updated_at=now()
                """,
                (
                    leg_id,
                    record.directive_id,
                    index,
                    target_leg.instrument_id,
                    target_leg.symbol,
                    audit_state.value,
                    broker_cancel_ref,
                    event_id,
                    error_code,
                    error_message[:300] if error_message else None,
                    target_leg.filled_quantity,
                ),
            )
            cur.execute(
                """
                update execution.user_directives target
                   set state=case when exists (
                         select 1 from execution.user_directive_legs leg
                          where leg.directive_id=target.directive_id
                            and leg.side is not null and leg.filled_quantity>0
                       ) then 'PARTIAL' else 'FAILED' end,
                       error_code='TRADING_USER_CANCELLED',
                       error_message='superseded by a higher-priority USER directive',
                       completed_at=null,updated_at=now(),version=version+1
                 where target.directive_id=%s
                   and not exists (
                     select 1 from execution.user_directive_legs active_leg
                      where active_leg.directive_id=target.directive_id
                        and active_leg.side is not null
                        and active_leg.state in
                          ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')
                   )
                """,
                (target_record.directive_id,),
            )
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return next(leg for leg in refreshed.legs if leg.leg_id == leg_id)

    def external_cancel_targets(
        self,
        record: DirectiveRecord,
    ) -> list[tuple[DirectiveLeg, DirectiveRecord, DirectiveLeg]]:
        prefix = f"ls-paper:cancel:{record.directive_id}:"
        result: list[tuple[DirectiveLeg, DirectiveRecord, DirectiveLeg]] = []
        for audit in record.legs:
            event_id = str(audit.broker_event_id or "")
            if audit.side is not None or not event_id.startswith(prefix):
                continue
            try:
                target_id = UUID(event_id.removeprefix(prefix))
            except ValueError:
                continue
            with self._cursor() as cur:
                cur.execute(
                    "select directive_id from execution.user_directive_legs where leg_id=%s",
                    (target_id,),
                )
                row = cur.fetchone()
            if row is None:
                continue
            target_record = self.get(row[0])
            if target_record is None:
                continue
            target = next(
                (leg for leg in target_record.legs if leg.leg_id == target_id),
                None,
            )
            if target is not None:
                result.append((audit, target_record, target))
        return result

    def cancel_open_orders(
        self,
        record: DirectiveRecord,
        *,
        below_priority: int | None,
        include_direct_legs: bool = True,
    ) -> list[DirectiveLeg]:
        with self._cursor() as cur:
            # Direct-lane orders are durable legs. SELL_ALL cancels only lower
            # priority directives; CANCEL_ALL passes None and cancels all peers.
            query = """
                select l.leg_id,l.instrument_id,l.symbol,l.state,d.priority,l.directive_id,
                       l.filled_quantity
                  from execution.user_directive_legs l
                  join execution.user_directives d on d.directive_id=l.directive_id
                 where d.fund_id=%s and d.book_id=%s and d.directive_id<>%s
                   and %s
                   and l.side is not null
                   and l.state in ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')
            """
            params: list[Any] = [
                record.fund_id,
                record.book_id,
                record.directive_id,
                include_direct_legs,
            ]
            if below_priority is not None:
                query += " and d.priority < %s"
                params.append(below_priority)
            query += " for update"
            cur.execute(query, tuple(params))
            affected_directives: set[UUID] = set()
            for target_id, instrument_id, symbol, state, _priority, target_directive_id, target_filled in cur.fetchall():
                affected_directives.add(target_directive_id)
                if state == "UNKNOWN":
                    self._insert_cancel_leg(
                        cur, record, linked_order_id=None, instrument_id=instrument_id,
                        symbol=symbol, state=DirectiveLegState.UNKNOWN,
                        target_ref=str(target_id), error_code="TRADING_ORDER_RECONCILIATION_REQUIRED",
                        error_message="order state is UNKNOWN",
                    )
                    continue
                cur.execute("update execution.user_directive_legs set state='CANCELLED',updated_at=now() where leg_id=%s", (target_id,))
                self._retain_unaccounted_fill_reservation(cur, target_id)
                lost_race = Decimal(target_filled) > 0
                self._insert_cancel_leg(
                    cur, record, linked_order_id=None, instrument_id=instrument_id,
                    symbol=symbol, state=DirectiveLegState.CANCELLED, target_ref=str(target_id),
                    error_code="TRADING_CANCEL_LOST_RACE" if lost_race else None,
                    error_message=(
                        "order partially filled before its remainder was cancelled"
                        if lost_race else None
                    ),
                    target_filled_quantity=Decimal(target_filled),
                )
            for target_directive_id in affected_directives:
                cur.execute(
                    """
                    update execution.user_directives target
                       set state=case when exists (
                             select 1 from execution.user_directive_legs leg
                              where leg.directive_id=target.directive_id
                                and leg.side is not null and leg.filled_quantity>0
                           ) then 'PARTIAL' else 'FAILED' end,
                           error_code='TRADING_USER_CANCELLED',
                           error_message='superseded by a higher-priority USER directive',
                           completed_at=null,updated_at=now(),version=version+1
                     where target.directive_id=%s
                       and not exists (
                         select 1 from execution.user_directive_legs active_leg
                          where active_leg.directive_id=target.directive_id
                            and active_leg.side is not null
                            and active_leg.state in
                              ('PENDING','ACKNOWLEDGED','PARTIALLY_FILLED','UNKNOWN')
                       )
                    """,
                    (target_directive_id,),
                )

            # Existing strategy OMS orders have no priority column: they are
            # lower priority than every authenticated USER directive.
            cur.execute(
                """
                select o.order_id,o.state,i.instrument_id,sy.symbol,o.filled_quantity
                  from execution.orders o
                  join execution.order_intents i on i.order_intent_id=o.order_intent_id
                  left join lateral (
                    select case when count(distinct symbol)=1 then min(symbol) end as symbol
                      from reference.instrument_symbols s
                     where s.instrument_id=i.instrument_id and s.valid_from<=now()
                       and (s.valid_to is null or s.valid_to>now())
                       and s.symbol ~ '^[0-9A-Z]{6}$'
                  ) sy on true
                 where i.fund_id=%s and i.book_id=%s
                   and o.broker_adapter='paper'
                   and o.state in ('CREATED','SUBMITTED','ACKNOWLEDGED','PARTIALLY_FILLED','CANCEL_PENDING','UNKNOWN')
                 for update of o
                """,
                (record.fund_id, record.book_id),
            )
            for order_id, state, instrument_id, symbol, target_filled in cur.fetchall():
                if state == "UNKNOWN":
                    self._insert_cancel_leg(
                        cur, record, linked_order_id=order_id, instrument_id=instrument_id,
                        symbol=symbol, state=DirectiveLegState.UNKNOWN,
                        target_ref=str(order_id), error_code="TRADING_ORDER_RECONCILIATION_REQUIRED",
                        error_message="order state is UNKNOWN",
                        target_filled_quantity=Decimal(target_filled),
                    )
                    continue
                if state == "CREATED":
                    self._transition_order(cur, record, order_id, state, "CANCELLED", "cancel")
                    self._insert_cancel_leg(
                        cur, record, linked_order_id=order_id, instrument_id=instrument_id,
                        symbol=symbol, state=DirectiveLegState.CANCELLED, target_ref=str(order_id),
                        target_filled_quantity=Decimal(target_filled),
                    )
                    continue
                if state != "CANCEL_PENDING":
                    self._transition_order(
                        cur, record, order_id, state, "CANCEL_PENDING", "cancel_requested"
                    )
                self._insert_cancel_leg(
                    cur, record, linked_order_id=order_id, instrument_id=instrument_id,
                    symbol=symbol, state=DirectiveLegState.UNKNOWN, target_ref=str(order_id),
                    error_code="TRADING_CANCEL_CONFIRMATION_PENDING",
                    error_message="PAPER broker cancellation is awaiting confirmation",
                    target_filled_quantity=Decimal(target_filled),
                )
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return refreshed.legs

    def reconcile_cancel_legs(self, record: DirectiveRecord) -> DirectiveRecord:
        with self._cursor() as cur:
            # This is the deterministic local PAPER adapter completion step.
            # The request transaction only records CANCEL_PENDING/UNKNOWN;
            # a later status/worker transaction emits the canonical cancel
            # event before the directive can claim success.
            cur.execute(
                """
                select target.order_id,target.state,
                       target.requested_quantity,target.filled_quantity
                  from execution.orders target
                 where exists (
                       select 1
                         from execution.user_directive_legs leg
                        where leg.directive_id=%s and leg.side is null
                          and leg.state='UNKNOWN'
                          and leg.linked_order_id=target.order_id
                 )
                   and target.broker_adapter='paper'
                   and target.state='CANCEL_PENDING'
                 for update of target
                """,
                (record.directive_id,),
            )
            for order_id, state, requested, filled in cur.fetchall():
                broker_event = PaperBroker().cancel(
                    SimpleNamespace(
                        requested_quantity=Decimal(requested),
                        filled_quantity=Decimal(filled),
                    ),
                    when=datetime.now(timezone.utc),
                )
                target_state = "CANCELLED" if broker_event is not None else "FILLED"
                self._transition_order(
                    cur,
                    record,
                    order_id,
                    state,
                    target_state,
                    "cancel_confirmed" if target_state == "CANCELLED" else "cancel_lost_race",
                )
            cur.execute(
                """
                update execution.user_directive_legs leg
                   set state=case when target.state='CANCELLED' then 'CANCELLED'
                                  else 'SKIPPED' end,
                       target_filled_quantity=target.filled_quantity,
                       error_code=case when target.filled_quantity>0
                                         then 'TRADING_CANCEL_LOST_RACE'
                                       else null end,
                       error_message=case when target.filled_quantity>0
                                           then 'order filled in whole or part before cancel confirmation'
                                         else null end,
                       updated_at=now()
                  from execution.orders target
                 where leg.directive_id=%s and leg.side is null
                   and leg.state='UNKNOWN' and leg.linked_order_id=target.order_id
                   and target.broker_adapter='paper'
                   and target.state in ('CANCELLED','FILLED','REJECTED','EXPIRED')
                """,
                (record.directive_id,),
            )
        refreshed = self.get(record.directive_id)
        assert refreshed is not None
        return refreshed

    def open_sell_quantity(self, fund_id: UUID, book_id: UUID) -> Decimal:
        with self._cursor() as cur:
            cur.execute(
                """select coalesce(sum(reserved_quantity),0)
                     from execution.paper_order_reservations
                    where fund_id=%s and book_id=%s and reservation_type='POSITION' and state='ACTIVE'""",
                (fund_id, book_id),
            )
            return Decimal(cur.fetchone()[0])

    def active_directives(self, *, limit: int = 100) -> list[DirectiveRecord]:
        bounded = min(max(int(limit), 1), 1000)
        with self._cursor() as cur:
            cur.execute(
                """
                select directive_id
                  from execution.user_directives
                 where (
                       state in ('RECEIVED','RUNNING','IN_PROGRESS','UNKNOWN')
                   and not (
                           state='UNKNOWN'
                       and action='PLACE_ORDER'
                       and exists (
                             select 1
                               from execution.user_directive_legs unknown_order_leg
                              where unknown_order_leg.directive_id=
                                    execution.user_directives.directive_id
                                and unknown_order_leg.side is not null
                       )
                       and not exists (
                             select 1
                               from execution.user_directive_legs identified_order_leg
                              where identified_order_leg.directive_id=
                                    execution.user_directives.directive_id
                                and identified_order_leg.side is not null
                                and identified_order_leg.broker_order_id is not null
                       )
                   )
                 )
                    or (
                         state='PARTIAL'
                     and action='PLACE_ORDER'
                     and error_code='TRADING_DIRECTIVE_INTERNAL_ERROR'
                     and exists (
                           select 1
                             from execution.user_directive_legs recoverable_leg
                            where recoverable_leg.directive_id=
                                  execution.user_directives.directive_id
                              and recoverable_leg.side is not null
                     )
                     and not exists (
                           select 1
                             from execution.user_directive_legs unfinished_leg
                            where unfinished_leg.directive_id=
                                  execution.user_directives.directive_id
                              and unfinished_leg.side is not null
                              and unfinished_leg.state<>'FILLED'
                     )
                     and not exists (
                           select 1
                             from execution.user_directive_legs unknown_cancel
                            where unknown_cancel.directive_id=
                                  execution.user_directives.directive_id
                              and unknown_cancel.side is null
                              and unknown_cancel.state='UNKNOWN'
                     )
                    )
                 order by priority desc,updated_at,created_at,directive_id
                 limit %s
                """,
                (bounded,),
            )
            directive_ids = [row[0] for row in cur.fetchall()]
        records: list[DirectiveRecord] = []
        for directive_id in directive_ids:
            record = self.get(directive_id)
            if record is not None:
                records.append(record)
        return records

    def has_unaccounted_fills(self, directive_id: UUID) -> bool:
        """Require the direct-fill ACK or its exact accounting receipt.

        ``ack_fill_events`` writes both markers atomically in current releases.
        Treating either marker as acknowledgement also permits safe recovery of
        historical rows from a deployment that completed only one side.  With
        neither marker the directive remains active for autonomous reconciliation.
        """

        with self._cursor() as cur:
            cur.execute(
                """
                select exists (
                  select 1
                    from execution.paper_user_directive_fills fill
                   where fill.directive_id=%s
                     and fill.accounting_acknowledged_at is null
                     and not exists (
                       select 1
                         from execution.outbox envelope
                         join execution.outbox_consumed consumed
                           on consumed.event_id=envelope.event_id
                          and consumed.consumer='accounting-ledger'
                        where envelope.event_type='trading.fill.v1'
                          and envelope.payload_ref->>'artifact_schema'=
                              'trading-user-directive-fill-v1'
                          and envelope.payload_ref->>'artifact_id'=fill.fill_id::text
                     )
                )
                """,
                (directive_id,),
            )
            return bool(cur.fetchone()[0])

    def has_unaccounted_buy_fills(self, fund_id: UUID, book_id: UUID) -> bool:
        with self._cursor() as cur:
            cur.execute(
                """
                select exists (
                  select 1
                    from execution.paper_user_directive_fills fill
                    join execution.user_directives directive
                      on directive.directive_id=fill.directive_id
                   where directive.fund_id=%s and directive.book_id=%s
                     and fill.side='BUY'
                     and fill.accounting_acknowledged_at is null
                  union all
                  select 1
                    from execution.fills fill
                    join execution.orders broker_order
                      on broker_order.order_id=fill.order_id
                    join execution.order_intents intent
                      on intent.order_intent_id=broker_order.order_intent_id
                    left join execution.outbox envelope
                      on envelope.event_type='trading.fill.v1'
                     and envelope.payload_ref->>'artifact_type'='FILL'
                     and envelope.payload_ref->>'artifact_id'=fill.fill_id::text
                    left join execution.outbox_consumed consumed
                      on consumed.event_id=envelope.event_id
                     and consumed.consumer='accounting-ledger'
                   where intent.fund_id=%s and intent.book_id=%s
                     and fill.side='BUY'
                     and (envelope.event_id is null or consumed.event_id is null)
                )
                """,
                (fund_id, book_id, fund_id, book_id),
            )
            return bool(cur.fetchone()[0])


__all__ = [
    "ACTIVE_LEG_STATES",
    "ACTIVE_DIRECTIVE_STATES",
    "DirectiveLeg",
    "DirectiveRecord",
    "DirectiveRepository",
    "DirectiveRepositoryError",
    "InMemoryDirectiveRepository",
    "InstrumentRef",
    "PostgresDirectiveRepository",
    "_MemoryState",
]
