"""Application service for authenticated user-priority PAPER directives."""
from __future__ import annotations

import os
import hashlib
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import UUID

from broker.ls_paper_broker import LSPaperBroker, LSPaperBrokerError
from broker.paper_policy import participation_cap

from .auth import (
    DirectiveAuthError,
    DirectiveProof,
    bind_proof,
    bind_read_proof,
    decode_directive_proof,
    decode_directive_read_proof,
)
from .contracts import (
    DirectiveAction,
    DirectiveLegState,
    DirectiveState,
    UserDirectiveRequest,
)
from .market_data import MarketDataError, MarketDataProvider
from .repository import DirectiveRecord, DirectiveRepository, DirectiveRepositoryError, InstrumentRef


class DirectiveServiceError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        status_code: int,
        *,
        directive_id: UUID | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.directive_id = directive_id


def require_paper_execution_mode() -> None:
    if os.environ.get("TRADING_EXECUTION_MODE", "").strip().upper() != "PAPER":
        raise DirectiveServiceError(
            "TRADING_PAPER_MODE_REQUIRED",
            "TRADING_EXECUTION_MODE must be exactly PAPER; LIVE adapters are forbidden",
            503,
        )


def _krx_tick_size(price: Decimal) -> Decimal:
    value = int(price)
    if value < 2_000:
        return Decimal(1)
    if value < 5_000:
        return Decimal(5)
    if value < 20_000:
        return Decimal(10)
    if value < 50_000:
        return Decimal(50)
    if value < 200_000:
        return Decimal(100)
    if value < 500_000:
        return Decimal(500)
    return Decimal(1000)


def _cost_buffer() -> Decimal:
    try:
        bps = Decimal(os.environ.get("TRADING_PAPER_BUY_COST_BUFFER_BPS", "100"))
    except InvalidOperation as exc:
        raise DirectiveServiceError(
            "TRADING_PAPER_COST_POLICY_INVALID", "buy cost buffer is invalid", 503
        ) from exc
    if not bps.is_finite() or bps < 0 or bps > 1000:
        raise DirectiveServiceError(
            "TRADING_PAPER_COST_POLICY_INVALID", "buy cost buffer is outside bounds", 503
        )
    return Decimal(1) + bps / Decimal(10_000)


def _mechanical_order_rules(
    instrument: InstrumentRef,
    *,
    quantity: Decimal,
    limit_price: Decimal | None,
) -> None:
    if quantity <= 0 or quantity % instrument.lot_size != 0:
        raise DirectiveServiceError(
            "TRADING_LOT_SIZE_DENIED",
            f"quantity must be an exact multiple of lot size {instrument.lot_size}",
            422,
        )
    if limit_price is None:
        return
    if limit_price % _krx_tick_size(limit_price) != 0:
        raise DirectiveServiceError(
            "TRADING_TICK_SIZE_DENIED", "limit price violates the KRX tick ladder", 422
        )
    if instrument.tick_size is not None and limit_price % instrument.tick_size != 0:
        raise DirectiveServiceError(
            "TRADING_TICK_SIZE_DENIED", "limit price violates the instrument tick size", 422
        )


def _quote_event_key(quote: Any) -> str:
    """Fingerprint the complete executable quote event, not just its price."""
    payload = {
        "instrument_id": quote.instrument_id,
        "symbol": quote.symbol,
        "observed_at": quote.observed_at.astimezone(timezone.utc).isoformat(),
        "bid": str(quote.bid),
        "ask": str(quote.ask),
        "bid_size": str(quote.bid_size),
        "ask_size": str(quote.ask_size),
        "source": quote.source,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
def _translate(exc: Exception, directive_id: UUID | None = None) -> DirectiveServiceError:
    if isinstance(exc, DirectiveServiceError):
        if exc.directive_id is None:
            exc.directive_id = directive_id
        return exc
    if isinstance(exc, (DirectiveRepositoryError, MarketDataError, DirectiveAuthError)):
        return DirectiveServiceError(
            exc.code,
            str(exc),
            exc.status_code,
            directive_id=directive_id,
        )
    return DirectiveServiceError(
        "TRADING_DIRECTIVE_INTERNAL_ERROR",
        "directive execution failed closed",
        500,
        directive_id=directive_id,
    )


class UserDirectiveService:
    """Execute structured USER directives without fabricating Risk evidence.

    The direct lane deliberately does not call OMS Risk or strategy switches.
    It still holds a per-book lock and enforces reference, market-session,
    quote, tick/lot, cash/position, reservation, and PAPER-only gates.
    """

    def __init__(
        self,
        repository: DirectiveRepository,
        market_data: MarketDataProvider,
        *,
        external_broker: LSPaperBroker | None = None,
    ) -> None:
        require_paper_execution_mode()
        self.repository = repository
        self.market_data = market_data
        self.external_broker = external_broker

    def submit(
        self,
        request: UserDirectiveRequest,
        authorization: str | None,
        *,
        now: datetime | None = None,
    ) -> DirectiveRecord:
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            proof = decode_directive_proof(authorization, now=current_time.timestamp())
            bind_proof(proof, request)
        except Exception as exc:  # authentication/admission is fail-closed
            raise _translate(exc) from exc

        return self._submit_bound(request, proof, current_time=current_time)

    def submit_trusted_rule(
        self,
        request: UserDirectiveRequest,
        proof: DirectiveProof,
        *,
        now: datetime | None = None,
    ) -> DirectiveRecord:
        """Submit one DB-verified standing rule through the same mechanical gate.

        Only the internal conditional-rule route constructs ``proof`` after it
        verifies the durable rule/trigger/execution lineage.  No Hermes or
        browser field can select the authority identifiers on this path.
        """

        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            bind_proof(proof, request)
        except Exception as exc:
            raise _translate(exc) from exc
        return self._submit_bound(request, proof, current_time=current_time)

    def _submit_bound(
        self,
        request: UserDirectiveRequest,
        proof: DirectiveProof,
        *,
        current_time: datetime,
    ) -> DirectiveRecord:
        try:
            record, created = self.repository.accept(request, proof)
        except Exception as exc:
            raise _translate(exc) from exc

        if not created and record.state not in {DirectiveState.RECEIVED, DirectiveState.RUNNING}:
            return self._status(record, now=current_time)

        failure: DirectiveServiceError | None = None
        result: DirectiveRecord | None = None
        with self.repository.book_guard(record.fund_id, record.book_id):
            try:
                self._reconcile_expired_scope(record.fund_id, record.book_id, now=current_time)
                record = self.repository.get(record.directive_id) or record
                if record.state is DirectiveState.RECEIVED:
                    if not self.repository.claim(record.directive_id):
                        record = self.repository.get(record.directive_id) or record
                    else:
                        record = self.repository.get(record.directive_id) or record
                if record.state is DirectiveState.RUNNING and record.legs:
                    # A pre-upgrade RUNNING row may have durable effects even
                    # though its final projection was not written. Reconcile;
                    # never duplicate those effects.
                    result = self._status_locked(record, now=current_time)
                elif record.state is not DirectiveState.RUNNING:
                    result = self._status_locked(record, now=current_time)
                elif request.action is DirectiveAction.PLACE_ORDER:
                    result = self._place(record, request, now=current_time)
                elif request.action is DirectiveAction.CANCEL_ALL:
                    result = self._cancel_all(record)
                else:
                    result = self._sell_all(record, now=current_time)
            except Exception as exc:
                failure = _translate(exc, record.directive_id)
                current = self.repository.get(record.directive_id) or record
                active_order_effect = any(
                    leg.side is not None
                    and leg.state in {
                        DirectiveLegState.PENDING,
                        DirectiveLegState.ACKNOWLEDGED,
                        DirectiveLegState.PARTIALLY_FILLED,
                        DirectiveLegState.UNKNOWN,
                    }
                    for leg in current.legs
                )
                mixed_effect = bool(current.legs)
                if mixed_effect and active_order_effect:
                    terminal = DirectiveState.IN_PROGRESS
                elif mixed_effect:
                    terminal = DirectiveState.PARTIAL
                else:
                    terminal = DirectiveState.FAILED
                self.repository.set_state(
                    record.directive_id,
                    terminal,
                    error_code=failure.code,
                    error_message=str(failure),
                )
                if terminal in {DirectiveState.PARTIAL, DirectiveState.FAILED}:
                    self.repository.release_barrier(record)
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def get_status(
        self,
        directive_id: UUID,
        authorization: str | None,
        *,
        now: datetime | None = None,
    ) -> DirectiveRecord:
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            proof = decode_directive_read_proof(
                authorization,
                directive_id,
                now=current_time.timestamp(),
            )
            record = self.repository.get(directive_id)
            if record is None:
                raise DirectiveRepositoryError(
                    "TRADING_DIRECTIVE_NOT_FOUND", "directive not found", 404
                )
            bind_read_proof(proof, record)
            return self._status(record, now=current_time)
        except Exception as exc:
            raise _translate(exc, directive_id) from exc

    def reconcile_active(
        self,
        *,
        now: datetime | None = None,
        limit: int = 100,
    ) -> tuple[list[DirectiveRecord], list[str]]:
        """Advance accepted durable directives without a browser poll.

        This path never creates a directive and therefore needs no delegated
        user proof.  It may only resume rows whose proof/admission was already
        committed by ``accept``.
        """
        current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        reconciled: list[DirectiveRecord] = []
        errors: list[str] = []
        for queued in self.repository.active_directives(limit=limit):
            try:
                with self.repository.book_guard(queued.fund_id, queued.book_id):
                    self._reconcile_expired_scope(
                        queued.fund_id, queued.book_id, now=current_time
                    )
                    current = self.repository.get(queued.directive_id) or queued
                    if current.state is DirectiveState.RECEIVED:
                        self.repository.claim(current.directive_id)
                        current = self.repository.get(current.directive_id) or current
                    if current.state is DirectiveState.RUNNING and not current.legs:
                        if current.action is DirectiveAction.PLACE_ORDER:
                            request = UserDirectiveRequest.model_validate(
                                {
                                    "fund_id": current.fund_id,
                                    "book_id": current.book_id,
                                    "action": current.action,
                                    "instruction_ref": current.instruction_ref,
                                    "idempotency_key": current.idempotency_key,
                                    "payload": current.payload,
                                }
                            )
                            current = self._place(current, request, now=current_time)
                        elif current.action is DirectiveAction.CANCEL_ALL:
                            current = self._cancel_all(current)
                        else:
                            current = self._sell_all(current, now=current_time)
                    else:
                        current = self._status_locked(current, now=current_time)
                    reconciled.append(current)
            except Exception as exc:
                translated = _translate(exc, queued.directive_id)
                errors.append(f"{queued.directive_id}:{translated.code}")
                try:
                    # A bad/stale quote for one row must not permanently occupy
                    # the bounded worker batch and starve same-priority peers.
                    self.repository.touch_active(queued.directive_id)
                except Exception:
                    # The original error remains the useful diagnostic. A DB
                    # outage can prevent the fairness touch as well.
                    pass
        return reconciled, errors

    def _reconcile_expired_scope(
        self,
        fund_id: UUID,
        book_id: UUID,
        *,
        now: datetime,
    ) -> None:
        for expired in self.repository.expire_scope_legs(fund_id, book_id, now=now):
            if expired.action is DirectiveAction.PLACE_ORDER:
                if any(leg.filled_quantity > 0 for leg in expired.legs):
                    self.repository.set_state(
                        expired.directive_id,
                        DirectiveState.PARTIAL,
                        error_code="TRADING_PAPER_ORDER_EXPIRED",
                        error_message="DAY order expired after a partial fill",
                    )
                    self.repository.release_barrier(expired)
                else:
                    self.repository.set_state(
                        expired.directive_id,
                        DirectiveState.FAILED,
                        error_code="TRADING_PAPER_ORDER_EXPIRED",
                        error_message="DAY order expired before completion",
                    )
                    self.repository.release_barrier(expired)
            elif expired.action is DirectiveAction.SELL_ALL:
                self.repository.set_state(
                    expired.directive_id,
                    DirectiveState.PARTIAL,
                    error_code="TRADING_PAPER_ORDER_EXPIRED",
                    error_message="SELL_ALL remains incomplete after a DAY leg expired",
                )
                self.repository.release_barrier(expired)

    def _fill_from_quote(
        self,
        record: DirectiveRecord,
        leg: Any,
        instrument: InstrumentRef,
        quote: Any,
    ) -> Any:
        if leg.side == "BUY":
            marketable = leg.order_type == "MARKET" or (
                leg.limit_price is not None and leg.limit_price >= quote.ask
            )
            price, executable = quote.ask, quote.ask_size
        else:
            marketable = leg.order_type == "MARKET" or (
                leg.limit_price is not None and leg.limit_price <= quote.bid
            )
            price, executable = quote.bid, quote.bid_size
        if not marketable:
            return leg
        executable = participation_cap(executable, instrument.lot_size)
        return self.repository.record_paper_fill(
            record,
            leg,
            instrument,
            quote_event_key=_quote_event_key(quote),
            price=price,
            executable_quantity=executable,
            event_time=quote.observed_at,
            source=quote.source,
        )

    def _fill_active_direct_legs(
        self,
        record: DirectiveRecord,
        *,
        now: datetime,
    ) -> DirectiveRecord:
        """Retry marketable direct legs from a fresh quote during status polling."""
        for leg in list(record.legs):
            eligible_states = {
                DirectiveLegState.ACKNOWLEDGED,
                DirectiveLegState.PARTIALLY_FILLED,
            }
            if self.external_broker is not None:
                eligible_states |= {
                    DirectiveLegState.PENDING,
                    DirectiveLegState.UNKNOWN,
                }
            if leg.side is None or leg.state not in eligible_states:
                continue
            if leg.instrument_id is None or leg.symbol is None:
                raise DirectiveServiceError(
                    "TRADING_PAPER_LEG_BINDING_INVALID",
                    "direct order leg lacks canonical instrument identity",
                    500,
                    directive_id=record.directive_id,
                )
            instrument = self.repository.resolve_instrument(
                record.fund_id,
                record.book_id,
                leg.instrument_id,
                leg.symbol,
            )
            if self.external_broker is not None:
                self._sync_external_leg(record, leg, instrument)
                record = self.repository.get(record.directive_id) or record
                continue
            quote = self.market_data.quote(instrument, now=now)
            self._fill_from_quote(record, leg, instrument, quote)
            record = self.repository.get(record.directive_id) or record
        return record

    def _submit_external_leg(
        self,
        record: DirectiveRecord,
        leg: Any,
        instrument: InstrumentRef,
    ) -> Any:
        """Submit once.  Ambiguous transport outcomes are never retried."""
        assert self.external_broker is not None
        try:
            ack = self.external_broker.place_order(
                symbol=instrument.symbol,
                side=str(leg.side),
                order_type=str(leg.order_type),
                quantity=Decimal(leg.requested_quantity),
                limit_price=leg.limit_price,
            )
        except LSPaperBrokerError as exc:
            if exc.ambiguous:
                return self.repository.mark_broker_leg_unknown(
                    record,
                    leg,
                    error_code=exc.code,
                    error_message=(
                        "LS PAPER submission outcome is ambiguous; automatic retry is forbidden"
                    ),
                )
            return self.repository.terminate_broker_leg(
                record,
                leg,
                state=DirectiveLegState.REJECTED,
                error_code=exc.code,
                error_message=str(exc),
            )
        return self.repository.acknowledge_broker_leg(
            record,
            leg,
            broker_order_id="ls-paper:" + ack.broker_order_id,
            broker_event_id="ls-paper:ack:" + ack.broker_order_id,
        )

    def _sync_external_leg(
        self,
        record: DirectiveRecord,
        leg: Any,
        instrument: InstrumentRef,
    ) -> Any:
        """Project the broker's cumulative order state without synthetic fills."""
        assert self.external_broker is not None
        broker_order_id = str(leg.broker_order_id or "")
        if not broker_order_id.startswith("ls-paper:"):
            if leg.state is DirectiveLegState.PENDING:
                return self.repository.mark_broker_leg_unknown(
                    record,
                    leg,
                    error_code="LS_PAPER_SUBMISSION_RECONCILIATION_REQUIRED",
                    error_message=(
                        "durable leg has no LS order number; automatic submission is forbidden"
                    ),
                )
            return leg
        raw_order_id = broker_order_id.split(":", 1)[1]
        try:
            status = self.external_broker.order_status(raw_order_id)
        except LSPaperBrokerError:
            # An acknowledged broker id remains authoritative.  A transient
            # status-query failure must not turn into a second placement.
            return leg
        if status is None:
            return leg
        delta = status.filled_quantity - Decimal(leg.filled_quantity)
        if delta > 0:
            if status.fill_price is None or status.fill_price <= 0:
                return self.repository.mark_broker_leg_unknown(
                    record,
                    leg,
                    error_code="LS_PAPER_FILL_PRICE_MISSING",
                    error_message="LS reports a fill quantity without a usable fill price",
                )
            event_identity = {
                "broker_order_id": raw_order_id,
                "filled_quantity": str(status.filled_quantity),
                "fill_price": str(status.fill_price),
                "order_date": status.order_date.isoformat(),
            }
            quote_event_key = hashlib.sha256(
                json.dumps(
                    event_identity, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            leg = self.repository.record_paper_fill(
                record,
                leg,
                instrument,
                quote_event_key=quote_event_key,
                price=status.fill_price,
                executable_quantity=delta,
                event_time=datetime.now(timezone.utc),
                source="ls-paper:CSPAQ13700",
            )
        if status.state in {"REJECTED", "CANCELLED"}:
            terminal = (
                DirectiveLegState.REJECTED
                if status.state == "REJECTED"
                else DirectiveLegState.CANCELLED
            )
            return self.repository.terminate_broker_leg(
                record,
                leg,
                state=terminal,
                error_code="LS_PAPER_ORDER_" + status.state,
                error_message="LS PAPER broker reported a terminal order state",
            )
        return leg

    @staticmethod
    def _external_cancel_event_id(record: DirectiveRecord, target_leg: Any) -> str:
        return f"ls-paper:cancel:{record.directive_id}:{target_leg.leg_id}"

    def _record_external_terminal_cancel(
        self,
        record: DirectiveRecord,
        target_record: DirectiveRecord,
        target_leg: Any,
    ) -> Any:
        if target_leg.state is DirectiveLegState.CANCELLED:
            return self.repository.record_external_cancel(
                record,
                target_record,
                target_leg,
                target_state=None,
                audit_state=DirectiveLegState.CANCELLED,
            )
        if target_leg.state is DirectiveLegState.FILLED:
            return self.repository.record_external_cancel(
                record,
                target_record,
                target_leg,
                target_state=None,
                audit_state=DirectiveLegState.SKIPPED,
                error_code="TRADING_CANCEL_LOST_RACE",
                error_message="order filled before LS PAPER cancellation completed",
            )
        return self.repository.record_external_cancel(
            record,
            target_record,
            target_leg,
            target_state=None,
            audit_state=DirectiveLegState.SKIPPED,
            error_message="order was already terminal before cancellation",
        )

    def _reconcile_external_cancel_targets(
        self,
        record: DirectiveRecord,
    ) -> DirectiveRecord:
        if self.external_broker is None:
            return record
        for audit, target_record, target_leg in self.repository.external_cancel_targets(record):
            if audit.state is not DirectiveLegState.UNKNOWN:
                continue
            if (
                target_leg.instrument_id is not None
                and target_leg.symbol is not None
                and str(target_leg.broker_order_id or "").startswith("ls-paper:")
            ):
                instrument = self.repository.resolve_instrument(
                    target_record.fund_id,
                    target_record.book_id,
                    target_leg.instrument_id,
                    target_leg.symbol,
                )
                self._sync_external_leg(target_record, target_leg, instrument)
                refreshed_target = self.repository.get(target_record.directive_id)
                if refreshed_target is not None:
                    target_record = refreshed_target
                    target_leg = next(
                        leg
                        for leg in target_record.legs
                        if leg.leg_id == target_leg.leg_id
                    )
            if target_leg.state not in {
                DirectiveLegState.PENDING,
                DirectiveLegState.ACKNOWLEDGED,
                DirectiveLegState.PARTIALLY_FILLED,
                DirectiveLegState.UNKNOWN,
            }:
                self._record_external_terminal_cancel(record, target_record, target_leg)
                record = self.repository.get(record.directive_id) or record
        return record

    def _cancel_external_direct_legs(
        self,
        record: DirectiveRecord,
        *,
        below_priority: int | None,
    ) -> list[Any]:
        assert self.external_broker is not None
        record = self._reconcile_external_cancel_targets(record)
        results: list[Any] = []
        candidates = self.repository.external_open_legs(
            record,
            below_priority=below_priority,
        )
        for target_record, target_leg in candidates:
            actor = self.repository.get(record.directive_id) or record
            event_id = self._external_cancel_event_id(actor, target_leg)
            existing_audit = next(
                (
                    leg
                    for leg in actor.legs
                    if leg.broker_event_id == event_id
                ),
                None,
            )

            if (
                target_leg.instrument_id is not None
                and target_leg.symbol is not None
                and str(target_leg.broker_order_id or "").startswith("ls-paper:")
            ):
                instrument = self.repository.resolve_instrument(
                    target_record.fund_id,
                    target_record.book_id,
                    target_leg.instrument_id,
                    target_leg.symbol,
                )
                self._sync_external_leg(target_record, target_leg, instrument)
                refreshed_target = self.repository.get(target_record.directive_id)
                if refreshed_target is not None:
                    target_record = refreshed_target
                    target_leg = next(
                        leg
                        for leg in target_record.legs
                        if leg.leg_id == target_leg.leg_id
                    )

            if target_leg.state not in {
                DirectiveLegState.PENDING,
                DirectiveLegState.ACKNOWLEDGED,
                DirectiveLegState.PARTIALLY_FILLED,
                DirectiveLegState.UNKNOWN,
            }:
                results.append(
                    self._record_external_terminal_cancel(
                        actor, target_record, target_leg
                    )
                )
                continue
            if existing_audit is not None:
                # A prior cancellation outcome was ambiguous.  Querying broker
                # state is safe; issuing a second cancellation is not.
                results.append(existing_audit)
                continue

            broker_ref = str(target_leg.broker_order_id or "")
            quantity = Decimal(target_leg.requested_quantity or 0) - Decimal(
                target_leg.filled_quantity
            )
            if (
                not broker_ref.startswith("ls-paper:")
                or target_leg.symbol is None
                or quantity <= 0
            ):
                results.append(
                    self.repository.record_external_cancel(
                        actor,
                        target_record,
                        target_leg,
                        target_state=DirectiveLegState.UNKNOWN,
                        audit_state=DirectiveLegState.UNKNOWN,
                        error_code="TRADING_EXTERNAL_CANCEL_RECONCILIATION_REQUIRED",
                        error_message=(
                            "active order lacks a safe LS PAPER cancellation binding"
                        ),
                    )
                )
                continue

            try:
                ack = self.external_broker.cancel_order(
                    broker_order_id=broker_ref.split(":", 1)[1],
                    symbol=target_leg.symbol,
                    quantity=quantity,
                )
            except LSPaperBrokerError:
                # A cancel transport failure is just as ambiguous as a place
                # failure.  Persist UNKNOWN and never issue it again.
                results.append(
                    self.repository.record_external_cancel(
                        actor,
                        target_record,
                        target_leg,
                        target_state=DirectiveLegState.UNKNOWN,
                        audit_state=DirectiveLegState.UNKNOWN,
                        error_code="LS_PAPER_CANCEL_AMBIGUOUS",
                        error_message=(
                            "LS PAPER cancellation outcome is ambiguous; automatic retry is forbidden"
                        ),
                    )
                )
                continue
            results.append(
                self.repository.record_external_cancel(
                    actor,
                    target_record,
                    target_leg,
                    target_state=DirectiveLegState.CANCELLED,
                    audit_state=DirectiveLegState.CANCELLED,
                    broker_cancel_order_id=ack.broker_order_id,
                )
            )
        return results

    def _cancel_open_orders(
        self,
        record: DirectiveRecord,
        *,
        below_priority: int | None,
    ) -> list[Any]:
        external_legs: list[Any] = []
        if self.external_broker is not None:
            external_legs = self._cancel_external_direct_legs(
                record,
                below_priority=below_priority,
            )
        local_legs = self.repository.cancel_open_orders(
            record,
            below_priority=below_priority,
            include_direct_legs=self.external_broker is None,
        )
        return [*external_legs, *local_legs]

    def _place(
        self,
        record: DirectiveRecord,
        request: UserDirectiveRequest,
        *,
        now: datetime,
    ) -> DirectiveRecord:
        payload = request.place_order()
        instrument = self.repository.resolve_instrument(
            record.fund_id,
            record.book_id,
            payload.instrument_id,
            payload.symbol,
        )
        _mechanical_order_rules(
            instrument,
            quantity=payload.quantity,
            limit_price=payload.limit_price,
        )
        # Fail the economic preflight before mutating any lower-priority order.
        # The same book lock remains held for the subsequent revalidation.
        expires_at = self.repository.market_session_close(now=now)
        trusted = self.market_data.quote(instrument, now=now)
        reserve_cash: Decimal | None = None
        reduce_only = payload.side == "SELL"
        if payload.side == "BUY":
            reservation_price = payload.limit_price if payload.limit_price is not None else trusted.ask
            reserve_cash = payload.quantity * reservation_price * _cost_buffer()
            available = self.repository.available_cash(
                record.fund_id, record.book_id, instrument.currency
            )
            if available < reserve_cash:
                raise DirectiveServiceError(
                    "TRADING_INSUFFICIENT_CASH",
                    f"available cash {available} is below required reservation {reserve_cash}",
                    409,
                )
        else:
            sellable = self.repository.sellable_quantity(
                record.fund_id, record.book_id, instrument.instrument_id
            )
            if sellable < payload.quantity:
                raise DirectiveServiceError(
                    "TRADING_INSUFFICIENT_SELLABLE_POSITION",
                    f"sellable quantity {sellable} is below requested {payload.quantity}",
                    409,
                )

        self.repository.activate_barrier(record, reduce_only=payload.side == "SELL")
        preemption_legs = self._cancel_open_orders(
            record,
            below_priority=record.priority,
        )
        if any(leg.state is DirectiveLegState.UNKNOWN for leg in preemption_legs):
            return self.repository.set_state(
                record.directive_id,
                DirectiveState.UNKNOWN,
                error_code="TRADING_CANCEL_CONFIRMATION_PENDING",
                error_message="lower-priority PAPER orders must cancel before USER sizing",
            )
        if any(leg.error_code == "TRADING_CANCEL_LOST_RACE" for leg in preemption_legs):
            partial = self.repository.set_state(
                record.directive_id,
                DirectiveState.PARTIAL,
                error_code="TRADING_CANCEL_LOST_RACE",
                error_message="a lower-priority order filled during USER preemption",
            )
            self.repository.release_barrier(record)
            return partial
        # Revalidate after preemption.  This catches any projection/reservation
        # change made while cancelling a lower PAPER order without relying on
        # the earlier observation for sizing.
        if payload.side == "BUY":
            available = self.repository.available_cash(
                record.fund_id, record.book_id, instrument.currency
            )
            if available < reserve_cash:
                raise DirectiveServiceError(
                    "TRADING_INSUFFICIENT_CASH",
                    f"available cash {available} is below required reservation {reserve_cash}",
                    409,
                )
        else:
            sellable = self.repository.sellable_quantity(
                record.fund_id, record.book_id, instrument.instrument_id
            )
            if sellable < payload.quantity:
                raise DirectiveServiceError(
                    "TRADING_INSUFFICIENT_SELLABLE_POSITION",
                    f"sellable quantity {sellable} is below requested {payload.quantity}",
                    409,
                )

        if self.external_broker is None:
            leg = self.repository.create_acknowledged_leg(
                record,
                instrument,
                side=payload.side,
                order_type=payload.order_type,
                quantity=payload.quantity,
                limit_price=payload.limit_price,
                reserve_cash=reserve_cash,
                reduce_only=reduce_only,
                expires_at=expires_at,
            )
            # The local simulator consumes the admission quote.  External LS
            # mode never reaches this path; only a broker-reported execution
            # may create its fill evidence.
            self._fill_from_quote(record, leg, instrument, trusted)
        else:
            leg = self.repository.create_pending_leg(
                record,
                instrument,
                side=payload.side,
                order_type=payload.order_type,
                quantity=payload.quantity,
                limit_price=payload.limit_price,
                reserve_cash=reserve_cash,
                reduce_only=reduce_only,
                expires_at=expires_at,
            )
            leg = self._submit_external_leg(record, leg, instrument)
            if leg.state in {
                DirectiveLegState.ACKNOWLEDGED,
                DirectiveLegState.PARTIALLY_FILLED,
            }:
                self._sync_external_leg(record, leg, instrument)
        self.repository.set_state(record.directive_id, DirectiveState.IN_PROGRESS)
        refreshed = self.repository.get(record.directive_id) or record
        return self._status_locked(refreshed, now=now)

    def _cancel_all(self, record: DirectiveRecord) -> DirectiveRecord:
        self.repository.activate_barrier(record, reduce_only=True)
        legs = self._cancel_open_orders(record, below_priority=None)
        if any(leg.state is DirectiveLegState.UNKNOWN for leg in legs):
            return self.repository.set_state(
                record.directive_id,
                DirectiveState.UNKNOWN,
                error_code="TRADING_ORDER_RECONCILIATION_REQUIRED",
                error_message="at least one PAPER order remains UNKNOWN",
            )
        if any(leg.error_code == "TRADING_CANCEL_LOST_RACE" for leg in legs):
            partial = self.repository.set_state(
                record.directive_id,
                DirectiveState.PARTIAL,
                error_code="TRADING_CANCEL_LOST_RACE",
                error_message="at least one PAPER order partially filled before cancellation",
            )
            self.repository.release_barrier(record)
            return partial
        completed = self.repository.set_state(record.directive_id, DirectiveState.COMPLETED)
        self.repository.release_barrier(record)
        return completed

    def _sell_all(self, record: DirectiveRecord, *, now: datetime) -> DirectiveRecord:
        self.repository.activate_barrier(record, reduce_only=True)
        cancellation_legs = self._cancel_open_orders(
            record,
            below_priority=record.priority,
        )
        if any(leg.state is DirectiveLegState.UNKNOWN for leg in cancellation_legs):
            return self.repository.set_state(
                record.directive_id,
                DirectiveState.UNKNOWN,
                error_code="TRADING_ORDER_RECONCILIATION_REQUIRED",
                error_message="SELL_ALL cannot size safely while an order is UNKNOWN",
            )

        if self.repository.has_unaccounted_buy_fills(record.fund_id, record.book_id):
            return self.repository.set_state(
                record.directive_id,
                DirectiveState.IN_PROGRESS,
                error_code="TRADING_INBOUND_FILL_ACCOUNTING_PENDING",
                error_message="SELL_ALL is waiting for a durable BUY fill projection",
            )

        positions = self.repository.positions(record.fund_id, record.book_id)
        if not positions and self.repository.open_sell_quantity(record.fund_id, record.book_id) == 0:
            completed = self.repository.set_state(record.directive_id, DirectiveState.COMPLETED)
            self.repository.release_barrier(record)
            return completed

        expires_at = self.repository.market_session_close(now=now)
        unsellable = False
        for instrument, _gross_quantity in positions:
            sellable = self.repository.sellable_quantity(
                record.fund_id, record.book_id, instrument.instrument_id
            )
            quantity = (sellable // instrument.lot_size) * instrument.lot_size
            if quantity <= 0:
                # An already-open same-priority reduce-only leg may own the
                # reservation.  Do not duplicate it.
                continue
            if quantity != sellable:
                unsellable = True
            _mechanical_order_rules(instrument, quantity=quantity, limit_price=None)
            trusted = self.market_data.quote(instrument, now=now)
            if self.external_broker is None:
                leg = self.repository.create_acknowledged_leg(
                    record,
                    instrument,
                    side="SELL",
                    order_type="MARKET",
                    quantity=quantity,
                    limit_price=None,
                    reserve_cash=None,
                    reduce_only=True,
                    expires_at=expires_at,
                )
                self._fill_from_quote(record, leg, instrument, trusted)
            else:
                leg = self.repository.create_pending_leg(
                    record,
                    instrument,
                    side="SELL",
                    order_type="MARKET",
                    quantity=quantity,
                    limit_price=None,
                    reserve_cash=None,
                    reduce_only=True,
                    expires_at=expires_at,
                )
                leg = self._submit_external_leg(record, leg, instrument)
                if leg.state in {
                    DirectiveLegState.ACKNOWLEDGED,
                    DirectiveLegState.PARTIALLY_FILLED,
                }:
                    self._sync_external_leg(record, leg, instrument)

        if unsellable:
            refreshed = self.repository.get(record.directive_id) or record
            has_active_leg = any(
                leg.side is not None
                and leg.state in {
                    DirectiveLegState.PENDING,
                    DirectiveLegState.ACKNOWLEDGED,
                    DirectiveLegState.PARTIALLY_FILLED,
                    DirectiveLegState.UNKNOWN,
                }
                for leg in refreshed.legs
            )
            state = DirectiveState.IN_PROGRESS if has_active_leg else DirectiveState.PARTIAL
            result = self.repository.set_state(
                record.directive_id,
                state,
                error_code="TRADING_ODD_LOT_REMAINDER",
                error_message="a position remainder is below the canonical lot size",
            )
            if state is DirectiveState.PARTIAL:
                self.repository.release_barrier(record)
            return result
        return self.repository.set_state(record.directive_id, DirectiveState.IN_PROGRESS)

    def _status(self, record: DirectiveRecord, *, now: datetime) -> DirectiveRecord:
        if record.state in {DirectiveState.COMPLETED, DirectiveState.FAILED}:
            return record
        with self.repository.book_guard(record.fund_id, record.book_id):
            self._reconcile_expired_scope(record.fund_id, record.book_id, now=now)
            current = self.repository.get(record.directive_id) or record
            return self._status_locked(current, now=now)

    def _status_locked(self, current: DirectiveRecord, *, now: datetime) -> DirectiveRecord:
        """Reconcile a directive while its canonical book lock is already held."""
        if current.state in {DirectiveState.COMPLETED, DirectiveState.FAILED}:
            return current
        if current.state is DirectiveState.PARTIAL:
            order_legs = [leg for leg in current.legs if leg.side is not None]
            recoverable_internal_failure = (
                current.action is DirectiveAction.PLACE_ORDER
                and current.error_code == "TRADING_DIRECTIVE_INTERNAL_ERROR"
                and bool(order_legs)
                and all(
                    leg.state is DirectiveLegState.FILLED for leg in order_legs
                )
                and not any(
                    leg.side is None and leg.state is DirectiveLegState.UNKNOWN
                    for leg in current.legs
                )
            )
            if not recoverable_internal_failure:
                self.repository.release_barrier(current)
                return current
        current = self.repository.reconcile_cancel_legs(current)
        current = self._reconcile_external_cancel_targets(current)
        if current.action in {DirectiveAction.PLACE_ORDER, DirectiveAction.SELL_ALL}:
            current = self._fill_active_direct_legs(current, now=now)
        if current.action is DirectiveAction.PLACE_ORDER:
            cancel_legs = [leg for leg in current.legs if leg.side is None]
            order_legs = [leg for leg in current.legs if leg.side is not None]
            if any(leg.state is DirectiveLegState.UNKNOWN for leg in cancel_legs):
                return self.repository.set_state(current.directive_id, DirectiveState.UNKNOWN)
            if any(leg.error_code == "TRADING_CANCEL_LOST_RACE" for leg in cancel_legs):
                partial = self.repository.set_state(
                    current.directive_id,
                    DirectiveState.PARTIAL,
                    error_code="TRADING_CANCEL_LOST_RACE",
                    error_message="a lower-priority order filled during USER preemption",
                )
                self.repository.release_barrier(current)
                return partial
            if not order_legs:
                request = UserDirectiveRequest.model_validate(
                    {
                        "fund_id": current.fund_id,
                        "book_id": current.book_id,
                        "action": current.action,
                        "instruction_ref": current.instruction_ref,
                        "idempotency_key": current.idempotency_key,
                        "payload": current.payload,
                    }
                )
                return self._place(current, request, now=now)
            states = {leg.state for leg in order_legs}
            if DirectiveLegState.UNKNOWN in states:
                return self.repository.set_state(current.directive_id, DirectiveState.UNKNOWN)
            if states and states <= {DirectiveLegState.FILLED}:
                if self.repository.has_unaccounted_fills(current.directive_id):
                    return self.repository.set_state(
                        current.directive_id,
                        DirectiveState.IN_PROGRESS,
                        error_code="TRADING_FILL_ACCOUNTING_PENDING",
                        error_message=(
                            "PAPER fills are waiting for durable accounting acknowledgement"
                        ),
                    )
                completed = self.repository.set_state(current.directive_id, DirectiveState.COMPLETED)
                self.repository.release_barrier(current)
                return completed
            if states & {
                DirectiveLegState.EXPIRED,
                DirectiveLegState.REJECTED,
                DirectiveLegState.CANCELLED,
            }:
                if any(leg.filled_quantity > 0 for leg in order_legs):
                    partial = self.repository.set_state(
                        current.directive_id,
                        DirectiveState.PARTIAL,
                        error_code="TRADING_PAPER_ORDER_TERMINATED",
                        error_message="PAPER order terminated after a partial fill",
                    )
                    self.repository.release_barrier(current)
                    return partial
                failed = self.repository.set_state(
                    current.directive_id,
                    DirectiveState.FAILED,
                    error_code="TRADING_PAPER_ORDER_TERMINATED",
                    error_message="PAPER order terminated before a complete fill",
                )
                self.repository.release_barrier(current)
                return failed
            if DirectiveLegState.PARTIALLY_FILLED in states:
                return self.repository.set_state(current.directive_id, DirectiveState.IN_PROGRESS)
            # Empty RUNNING roots are not accepted as successful work. They
            # are resumed by submit(), while status remains honest UNKNOWN.
            if not states:
                return self.repository.set_state(
                    current.directive_id,
                    DirectiveState.UNKNOWN,
                    error_code="TRADING_DIRECTIVE_RECONCILIATION_REQUIRED",
                    error_message="directive has no durable execution leg",
                )
            return self.repository.set_state(current.directive_id, DirectiveState.IN_PROGRESS)

        if current.action is DirectiveAction.CANCEL_ALL:
            if any(leg.state is DirectiveLegState.UNKNOWN for leg in current.legs):
                return self.repository.set_state(current.directive_id, DirectiveState.UNKNOWN)
            if any(leg.error_code == "TRADING_CANCEL_LOST_RACE" for leg in current.legs):
                partial = self.repository.set_state(
                    current.directive_id,
                    DirectiveState.PARTIAL,
                    error_code="TRADING_CANCEL_LOST_RACE",
                    error_message="at least one PAPER order filled before cancellation",
                )
                self.repository.release_barrier(current)
                return partial
            completed = self.repository.set_state(current.directive_id, DirectiveState.COMPLETED)
            self.repository.release_barrier(current)
            return completed

        states = {leg.state for leg in current.legs}
        if DirectiveLegState.UNKNOWN in states:
            return self.repository.set_state(current.directive_id, DirectiveState.UNKNOWN)
        if self.repository.has_unaccounted_buy_fills(current.fund_id, current.book_id):
            return self.repository.set_state(
                current.directive_id,
                DirectiveState.IN_PROGRESS,
                error_code="TRADING_INBOUND_FILL_ACCOUNTING_PENDING",
                error_message="SELL_ALL is waiting for a durable BUY fill projection",
            )
        positions = self.repository.positions(current.fund_id, current.book_id)
        open_sell = self.repository.open_sell_quantity(current.fund_id, current.book_id)
        if not positions and open_sell == 0:
            completed = self.repository.set_state(current.directive_id, DirectiveState.COMPLETED)
            self.repository.release_barrier(current)
            return completed
        has_active_sell = any(
            leg.side == "SELL"
            and leg.state in {
                DirectiveLegState.PENDING,
                DirectiveLegState.ACKNOWLEDGED,
                DirectiveLegState.PARTIALLY_FILLED,
                DirectiveLegState.UNKNOWN,
            }
            for leg in current.legs
        )
        if positions and open_sell == 0 and not has_active_sell:
            # The cancel phase has now reconciled. Resume the durable SELL_ALL
            # sizing phase under the same book lock without duplicating legs.
            return self._sell_all(current, now=now)
        active_states = {
            DirectiveLegState.PENDING,
            DirectiveLegState.ACKNOWLEDGED,
            DirectiveLegState.PARTIALLY_FILLED,
            DirectiveLegState.UNKNOWN,
        }
        if states & active_states:
            return self.repository.set_state(
                current.directive_id,
                DirectiveState.UNKNOWN
                if DirectiveLegState.UNKNOWN in states
                else DirectiveState.IN_PROGRESS,
                error_code=current.error_code,
                error_message=current.error_message,
            )
        if states & {DirectiveLegState.EXPIRED, DirectiveLegState.REJECTED}:
            partial = self.repository.set_state(current.directive_id, DirectiveState.PARTIAL)
            self.repository.release_barrier(current)
            return partial
        if DirectiveLegState.PARTIALLY_FILLED in states:
            return self.repository.set_state(
                current.directive_id,
                DirectiveState.IN_PROGRESS,
                error_code=current.error_code,
                error_message=current.error_message,
            )
        if current.error_code == "TRADING_ODD_LOT_REMAINDER" and open_sell == 0:
            partial = self.repository.set_state(
                current.directive_id,
                DirectiveState.PARTIAL,
                error_code=current.error_code,
                error_message=current.error_message,
            )
            self.repository.release_barrier(current)
            return partial
        return self.repository.set_state(
            current.directive_id,
            DirectiveState.IN_PROGRESS,
            error_code=current.error_code,
            error_message=current.error_message,
        )


__all__ = [
    "DirectiveServiceError",
    "UserDirectiveService",
    "require_paper_execution_mode",
]
