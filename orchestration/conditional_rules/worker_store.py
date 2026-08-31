"""Durable exactly-once state transitions for conditional PAPER rules."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, register_uuid

from .contracts import ConditionalRuleSpec, expression_fingerprint, rule_fingerprint
from .identities import evaluation_id, execution_idempotency_key, trigger_id
from .semantic import TrailingStopParameters, trailing_stop_parameters, validate_rule_spec

register_uuid()

_OCO_SUBMISSION_LEASE_SECONDS = 60
_OUTBOX_CLAIM_LEASE_SECONDS = 300


class RuleWorkerStoreError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class ActiveRule:
    rule_id: UUID
    rule_version: int
    row_version: int
    spec_sha256: str
    spec: ConditionalRuleSpec


@dataclass(frozen=True)
class TriggerClaim:
    trigger_id: str
    evaluation_id: str


@dataclass(frozen=True)
class TrailingStopState:
    high_price: Decimal
    armed_at: datetime | None
    last_observed_at: datetime


@dataclass(frozen=True)
class TrailingStopObservation:
    state: TrailingStopState
    condition_result: bool
    ignored_stale_quote: bool = False


def advance_trailing_stop(
    state: TrailingStopState | None,
    *,
    parameters: TrailingStopParameters,
    last_price: Decimal,
    average_entry_price: Decimal,
    observed_at: datetime,
) -> TrailingStopObservation:
    """Apply one quote to a high-water exit without any external side effect.

    The database caller locks the row before persisting this result.  Quotes at
    or behind the saved watermark are deliberately ignored: a delayed lower
    quote must never fire a stop after a newer observation has already moved
    the high-water mark forward.
    """

    if observed_at.tzinfo is None:
        raise RuleWorkerStoreError(
            "TRAILING_STOP_TIME_INVALID",
            "trailing stop quote time must include timezone",
        )
    if not last_price.is_finite() or last_price <= 0:
        raise RuleWorkerStoreError(
            "TRAILING_STOP_PRICE_INVALID",
            "trailing stop requires a positive finite last price",
        )
    if parameters.activation_return is not None and (
        not average_entry_price.is_finite() or average_entry_price <= 0
    ):
        raise RuleWorkerStoreError(
            "TRAILING_STOP_COST_BASIS_UNAVAILABLE",
            "activation return requires a positive average entry price",
        )
    if state is not None and observed_at <= state.last_observed_at:
        return TrailingStopObservation(
            state=state,
            condition_result=False,
            ignored_stale_quote=True,
        )
    high_price = max(state.high_price, last_price) if state is not None else last_price
    armed_at = state.armed_at if state is not None else None
    if armed_at is None and (
        parameters.activation_return is None
        or last_price
        >= average_entry_price * (Decimal("1") + parameters.activation_return)
    ):
        armed_at = observed_at
    next_state = TrailingStopState(
        high_price=high_price,
        armed_at=armed_at,
        last_observed_at=observed_at,
    )
    return TrailingStopObservation(
        state=next_state,
        condition_result=(
            armed_at is not None
            and last_price <= high_price * (Decimal("1") - parameters.drawdown)
        ),
    )


@dataclass(frozen=True)
class SubmitReadyExecution:
    rule_execution_id: UUID
    trigger_id: str
    rule_id: UUID
    rule_version: int
    idempotency_key: str


@dataclass(frozen=True)
class ConditionalRuleOutboxRow:
    event_id: str
    aggregate_id: str
    event_type: str
    payload: dict[str, Any]
    created_at: datetime
    attempts: int


@dataclass(frozen=True)
class ConditionalNotificationContext:
    rule_id: str
    rule_execution_id: str
    directive_id: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    order_request_id: str | None
    ceo_root_task_id: str | None
    trading_task_id: str | None


@dataclass(frozen=True)
class EntryPositionMismatchNotificationContext:
    """DB-authoritative context for a cancelled entry-originated trail."""

    rule_id: str
    symbol: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    order_request_id: str | None
    ceo_root_task_id: str | None
    trading_task_id: str | None
    expected_position_quantity: str
    actual_position_quantity: str
    occurred_at: datetime
    lifecycle_event_id: str


@dataclass(frozen=True)
class ExpiredRuleNotificationContext:
    """DB-authoritative context for a condition that ended without an order."""

    rule_id: str
    symbol: str
    action_side: str
    prior_state: str
    expires_at: datetime
    occurred_at: datetime
    lifecycle_event_id: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    order_request_id: str | None
    ceo_root_task_id: str | None
    trading_task_id: str | None
    is_compound_entry_exit: bool


@dataclass(frozen=True)
class ActivationBlockedNotificationContext:
    """DB-owned context for a fully filled entry whose exit never armed."""

    rule_id: str
    symbol: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    order_request_id: str | None
    ceo_root_task_id: str | None
    trading_task_id: str | None
    failure_code: str
    occurred_at: datetime
    lifecycle_event_id: str


@dataclass(frozen=True)
class BundleActivatedNotificationContext:
    """DB-owned confirmation that a protective exit is currently armed."""

    rule_id: str
    symbol: str
    action_side: str
    current_state: str
    expires_at: datetime
    activation_lifetime_trading_days: int | None
    occurred_at: datetime
    lifecycle_event_id: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    order_request_id: str | None
    ceo_root_task_id: str | None
    trading_task_id: str | None


@dataclass(frozen=True)
class TriggerClaimedNotificationContext:
    """DB-owned proof that a PAPER condition evaluated true exactly once."""

    rule_id: str
    symbol: str
    action_side: str
    current_state: str
    trigger_id: str
    data_watermark: datetime
    occurred_at: datetime
    lifecycle_event_id: str
    user_id: str
    fund_id: str
    book_id: str
    client_request_id: str
    order_request_id: str | None
    ceo_root_task_id: str | None
    trading_task_id: str | None


def _stable_id(prefix: str, *parts: object) -> str:
    raw = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return prefix + hashlib.sha256(raw).hexdigest()[:48]


class PostgresRuleWorkerStore:
    """Own worker-side lifecycle transitions under a dedicated database role."""

    def __init__(self, dsn: str, *, role: str = "svc_conditional_rule_worker") -> None:
        if not dsn.strip():
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_REQUIRED",
                "conditional rule worker database URL is required",
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_ROLE_INVALID",
                "conditional rule worker database role is invalid",
            )
        self.dsn = dsn
        self.role = role

    def _connect(self):
        return psycopg2.connect(self.dsn, connect_timeout=8)

    def _set_role(self, cursor: Any) -> None:
        cursor.execute(sql.SQL("set local role {}").format(sql.Identifier(self.role)))

    def healthcheck(self) -> None:
        """Check the database boundary without scanning domain tables."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute("select 1")
                cursor.fetchone()
                # The relay's claim/finalize path depends on the lease columns.
                # LIMIT 0 validates the deployed schema without reading rows.
                cursor.execute(
                    """
                    select claim_token, claim_expires_at
                      from execution.conditional_rule_outbox
                     limit 0
                    """
                )
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "conditional rule database healthcheck failed",
                retryable=True,
            ) from exc

    def notification_context(
        self, *, rule_id: str, directive_id: str
    ) -> ConditionalNotificationContext:
        """Resolve legacy/minimal outbox payloads without a second status store."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,execution.rule_execution_id,
                           execution.directive_id,rule.user_id,rule.fund_id,
                           rule.book_id,
                           coalesce(request.client_request_id,
                                    rule.client_request_id),
                           request.order_request_id,request.ceo_root_task_id,
                           request.trading_task_id
                      from execution.conditional_trade_rules rule
                      join execution.conditional_rule_executions execution
                        on execution.rule_id=rule.rule_id
                      left join lateral (
                        select admitted.order_request_id,
                               admitted.client_request_id,
                               admitted.ceo_root_task_id,
                               admitted.trading_task_id
                          from execution.user_order_requests admitted
                         where admitted.user_id=rule.user_id
                           and (
                             admitted.client_request_id=rule.client_request_id
                             or admitted.canonical_payload->>'rule_id'
                                  = rule.rule_id::text
                             or coalesce(
                                  admitted.canonical_payload->'rule_ids',
                                  '[]'::jsonb
                                ) ? rule.rule_id::text
                           )
                         order by
                           (admitted.client_request_id=rule.client_request_id) desc,
                           admitted.updated_at desc
                         limit 1
                      ) request on true
                     where rule.rule_id=%s and execution.directive_id=%s
                     order by execution.created_at desc
                     limit 1
                    """,
                    (UUID(str(rule_id)), UUID(str(directive_id))),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_NOTIFICATION_CONTEXT_MISSING",
                        "conditional directive correlation was not found",
                        retryable=True,
                    )
                return ConditionalNotificationContext(
                    rule_id=str(row[0]),
                    rule_execution_id=str(row[1]),
                    directive_id=str(row[2]),
                    user_id=str(row[3]),
                    fund_id=str(row[4]),
                    book_id=str(row[5]),
                    client_request_id=str(row[6]),
                    order_request_id=str(row[7]) if row[7] else None,
                    ceo_root_task_id=str(row[8]) if row[8] else None,
                    trading_task_id=str(row[9]) if row[9] else None,
                )
        except RuleWorkerStoreError:
            raise
        except (ValueError, psycopg2.Error) as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not resolve conditional notification context",
                retryable=True,
            ) from exc

    def entry_position_mismatch_notification_context(
        self, *, rule_id: str
    ) -> EntryPositionMismatchNotificationContext:
        """Resolve a terminal entry-trailing event without trusting Redis data.

        The outbox/Redis envelope carries only routing identity.  Quantities,
        account scope, and workflow links are reloaded from the immutable
        lifecycle event and the bundle's admitted immediate order.
        """

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,rule.symbol,rule.user_id,rule.fund_id,
                           rule.book_id,
                           coalesce(bundle_request.client_request_id,
                                    fallback.client_request_id,
                                    rule.client_request_id),
                           coalesce(bundle_request.order_request_id,
                                    fallback.order_request_id),
                           coalesce(bundle_request.ceo_root_task_id,
                                    fallback.ceo_root_task_id),
                           coalesce(bundle_request.trading_task_id,
                                    fallback.trading_task_id),
                           mismatch.payload->>'expected_position_quantity',
                           mismatch.payload->>'actual_position_quantity',
                           mismatch.created_at,mismatch.event_id
                      from execution.conditional_trade_rules rule
                      join lateral (
                        select event.event_id,event.payload,event.created_at
                          from execution.conditional_trade_rule_events event
                         where event.rule_id=rule.rule_id
                           and event.event_type='ENTRY_POSITION_MISMATCH'
                         order by event.created_at desc,event.event_id desc
                         limit 1
                      ) mismatch on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_paper_order_bundles bundle
                          join execution.user_order_requests request
                            on request.order_request_id=
                               bundle.immediate_order_request_id
                         where bundle.conditional_rule_id=rule.rule_id
                           and bundle.user_id=rule.user_id
                           and bundle.fund_id=rule.fund_id
                           and bundle.book_id=rule.book_id
                         order by bundle.updated_at desc,bundle.bundle_id desc
                         limit 1
                      ) bundle_request on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_order_requests request
                         where request.user_id=rule.user_id
                           and (
                             request.client_request_id=rule.client_request_id
                             or request.canonical_payload->>'rule_id'
                                  = rule.rule_id::text
                             or coalesce(
                                  request.canonical_payload->'rule_ids',
                                  '[]'::jsonb
                                ) ? rule.rule_id::text
                           )
                         order by
                           (request.client_request_id=rule.client_request_id) desc,
                           request.updated_at desc
                         limit 1
                      ) fallback on true
                     where rule.rule_id=%s
                    """,
                    (UUID(str(rule_id)),),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "ENTRY_POSITION_MISMATCH_CONTEXT_MISSING",
                        "entry-position mismatch lifecycle event was not found",
                        retryable=True,
                    )
                expected = str(row[9] or "").strip()
                actual = str(row[10] or "").strip()
                if not expected or not actual:
                    raise RuleWorkerStoreError(
                        "ENTRY_POSITION_MISMATCH_CONTEXT_INVALID",
                        "entry-position mismatch lifecycle event lacks quantities",
                        retryable=True,
                    )
                return EntryPositionMismatchNotificationContext(
                    rule_id=str(row[0]),
                    symbol=str(row[1]),
                    user_id=str(row[2]),
                    fund_id=str(row[3]),
                    book_id=str(row[4]),
                    client_request_id=str(row[5]),
                    order_request_id=str(row[6]) if row[6] else None,
                    ceo_root_task_id=str(row[7]) if row[7] else None,
                    trading_task_id=str(row[8]) if row[8] else None,
                    expected_position_quantity=expected,
                    actual_position_quantity=actual,
                    occurred_at=row[11],
                    lifecycle_event_id=str(row[12]),
                )
        except RuleWorkerStoreError:
            raise
        except (ValueError, psycopg2.Error) as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not resolve entry-position mismatch notification context",
                retryable=True,
            ) from exc

    def expired_rule_notification_context(
        self, *, rule_id: str
    ) -> ExpiredRuleNotificationContext:
        """Resolve one durable expiry without trusting its Redis envelope."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,rule.symbol,version.spec->'action'->>'side',
                           expired.from_state,rule.expires_at,expired.created_at,
                           expired.event_id,rule.user_id,rule.fund_id,rule.book_id,
                           coalesce(bundle_request.client_request_id,
                                    fallback.client_request_id,
                                    rule.client_request_id),
                           coalesce(bundle_request.order_request_id,
                                    fallback.order_request_id),
                           coalesce(bundle_request.ceo_root_task_id,
                                    fallback.ceo_root_task_id),
                           coalesce(bundle_request.trading_task_id,
                                    fallback.trading_task_id),
                           (bundle_request.order_request_id is not null)
                      from execution.conditional_trade_rules rule
                      join execution.conditional_trade_rule_versions version
                        on version.rule_id=rule.rule_id
                       and version.rule_version=rule.current_version
                      join lateral (
                        select event.event_id,event.from_state,event.created_at
                          from execution.conditional_trade_rule_events event
                         where event.rule_id=rule.rule_id
                           and event.event_type='CONDITIONAL_RULE_EXPIRED'
                         order by event.created_at desc,event.event_id desc
                         limit 1
                      ) expired on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_paper_order_bundles bundle
                          join execution.user_order_requests request
                            on request.order_request_id=
                               bundle.immediate_order_request_id
                         where bundle.conditional_rule_id=rule.rule_id
                           and bundle.user_id=rule.user_id
                           and bundle.fund_id=rule.fund_id
                           and bundle.book_id=rule.book_id
                         order by bundle.updated_at desc,bundle.bundle_id desc
                         limit 1
                      ) bundle_request on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_order_requests request
                         where request.user_id=rule.user_id
                           and (
                             request.client_request_id=rule.client_request_id
                             or request.canonical_payload->>'rule_id'
                                  = rule.rule_id::text
                             or coalesce(
                                  request.canonical_payload->'rule_ids',
                                  '[]'::jsonb
                                ) ? rule.rule_id::text
                           )
                         order by
                           (request.client_request_id=rule.client_request_id) desc,
                           request.updated_at desc
                         limit 1
                      ) fallback on true
                     where rule.rule_id=%s
                    """,
                    (UUID(str(rule_id)),),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_EXPIRY_CONTEXT_MISSING",
                        "conditional-rule expiry lifecycle event was not found",
                        retryable=True,
                    )
                action_side = str(row[2] or "").upper()
                prior_state = str(row[3] or "")
                if action_side not in {"BUY", "SELL"} or prior_state not in {
                    "PENDING_CONFIRMATION",
                    "ACTIVE",
                    "PAUSED",
                }:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_EXPIRY_CONTEXT_INVALID",
                        "conditional-rule expiry lifecycle event has invalid rule context",
                        retryable=True,
                    )
                if not isinstance(row[4], datetime) or not isinstance(row[5], datetime):
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_EXPIRY_CONTEXT_INVALID",
                        "conditional-rule expiry context is missing timestamps",
                        retryable=True,
                    )
                return ExpiredRuleNotificationContext(
                    rule_id=str(row[0]),
                    symbol=str(row[1]),
                    action_side=action_side,
                    prior_state=prior_state,
                    expires_at=row[4],
                    occurred_at=row[5],
                    lifecycle_event_id=str(row[6]),
                    user_id=str(row[7]),
                    fund_id=str(row[8]),
                    book_id=str(row[9]),
                    client_request_id=str(row[10]),
                    order_request_id=str(row[11]) if row[11] else None,
                    ceo_root_task_id=str(row[12]) if row[12] else None,
                    trading_task_id=str(row[13]) if row[13] else None,
                    is_compound_entry_exit=bool(row[14]),
                )
        except RuleWorkerStoreError:
            raise
        except (ValueError, psycopg2.Error) as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not resolve conditional-rule expiry notification context",
                retryable=True,
            ) from exc

    def activation_blocked_notification_context(
        self, *, rule_id: str
    ) -> ActivationBlockedNotificationContext:
        """Resolve a failed protective activation from its durable event."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,rule.symbol,rule.user_id,rule.fund_id,
                           rule.book_id,
                           coalesce(bundle_request.client_request_id,
                                    fallback.client_request_id,
                                    rule.client_request_id),
                           coalesce(bundle_request.order_request_id,
                                    fallback.order_request_id),
                           coalesce(bundle_request.ceo_root_task_id,
                                    fallback.ceo_root_task_id),
                           coalesce(bundle_request.trading_task_id,
                                    fallback.trading_task_id),
                           blocked.payload->>'code',blocked.created_at,
                           blocked.event_id
                      from execution.conditional_trade_rules rule
                      join lateral (
                        select event.event_id,event.payload,event.created_at
                          from execution.conditional_trade_rule_events event
                         where event.rule_id=rule.rule_id
                           and event.event_type='BUNDLE_ACTIVATION_BLOCKED'
                         order by event.created_at desc,event.event_id desc
                         limit 1
                      ) blocked on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_paper_order_bundles bundle
                          join execution.user_order_requests request
                            on request.order_request_id=
                               bundle.immediate_order_request_id
                         where bundle.conditional_rule_id=rule.rule_id
                           and bundle.user_id=rule.user_id
                           and bundle.fund_id=rule.fund_id
                           and bundle.book_id=rule.book_id
                         order by bundle.updated_at desc,bundle.bundle_id desc
                         limit 1
                      ) bundle_request on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_order_requests request
                         where request.user_id=rule.user_id
                           and (
                             request.client_request_id=rule.client_request_id
                             or request.canonical_payload->>'rule_id'
                                  = rule.rule_id::text
                             or coalesce(
                                  request.canonical_payload->'rule_ids',
                                  '[]'::jsonb
                                ) ? rule.rule_id::text
                           )
                         order by
                           (request.client_request_id=rule.client_request_id) desc,
                           request.updated_at desc
                         limit 1
                      ) fallback on true
                     where rule.rule_id=%s
                    """,
                    (UUID(str(rule_id)),),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_ACTIVATION_BLOCKED_CONTEXT_MISSING",
                        "conditional-rule activation-blocked lifecycle event was not found",
                        retryable=True,
                    )
                failure_code = str(row[9] or "").strip()
                if failure_code not in {
                    "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE",
                    "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_INVALID",
                } or not isinstance(row[10], datetime):
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_ACTIVATION_BLOCKED_CONTEXT_INVALID",
                        "conditional-rule activation-blocked context is invalid",
                        retryable=True,
                    )
                return ActivationBlockedNotificationContext(
                    rule_id=str(row[0]),
                    symbol=str(row[1]),
                    user_id=str(row[2]),
                    fund_id=str(row[3]),
                    book_id=str(row[4]),
                    client_request_id=str(row[5]),
                    order_request_id=str(row[6]) if row[6] else None,
                    ceo_root_task_id=str(row[7]) if row[7] else None,
                    trading_task_id=str(row[8]) if row[8] else None,
                    failure_code=failure_code,
                    occurred_at=row[10],
                    lifecycle_event_id=str(row[11]),
                )
        except RuleWorkerStoreError:
            raise
        except (ValueError, psycopg2.Error) as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not resolve conditional-rule activation-blocked context",
                retryable=True,
            ) from exc

    def bundle_activated_notification_context(
        self, *, rule_id: str
    ) -> BundleActivatedNotificationContext:
        """Resolve one currently armed protective exit from durable state."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,rule.symbol,version.spec->'action'->>'side',
                           rule.state,rule.expires_at,
                           activated.payload->>'activation_lifetime_trading_days',
                           activated.created_at,activated.event_id,
                           rule.user_id,rule.fund_id,rule.book_id,
                           coalesce(bundle_request.client_request_id,
                                    fallback.client_request_id,
                                    rule.client_request_id),
                           coalesce(bundle_request.order_request_id,
                                    fallback.order_request_id),
                           coalesce(bundle_request.ceo_root_task_id,
                                    fallback.ceo_root_task_id),
                           coalesce(bundle_request.trading_task_id,
                                    fallback.trading_task_id)
                      from execution.conditional_trade_rules rule
                      join execution.conditional_trade_rule_versions version
                        on version.rule_id=rule.rule_id
                       and version.rule_version=rule.current_version
                      join lateral (
                        select event.event_id,event.payload,event.created_at
                          from execution.conditional_trade_rule_events event
                         where event.rule_id=rule.rule_id
                           and event.event_type='BUNDLE_ACTIVATED'
                         order by event.created_at desc,event.event_id desc
                         limit 1
                      ) activated on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_paper_order_bundles bundle
                          join execution.user_order_requests request
                            on request.order_request_id=
                               bundle.immediate_order_request_id
                         where bundle.conditional_rule_id=rule.rule_id
                           and bundle.user_id=rule.user_id
                           and bundle.fund_id=rule.fund_id
                           and bundle.book_id=rule.book_id
                         order by bundle.updated_at desc,bundle.bundle_id desc
                         limit 1
                      ) bundle_request on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_order_requests request
                         where request.user_id=rule.user_id
                           and (
                             request.client_request_id=rule.client_request_id
                             or request.canonical_payload->>'rule_id'
                                  = rule.rule_id::text
                             or coalesce(
                                  request.canonical_payload->'rule_ids',
                                  '[]'::jsonb
                                ) ? rule.rule_id::text
                           )
                         order by
                           (request.client_request_id=rule.client_request_id) desc,
                           request.updated_at desc
                         limit 1
                      ) fallback on true
                     where rule.rule_id=%s
                    """,
                    (UUID(str(rule_id)),),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_BUNDLE_ACTIVATION_CONTEXT_MISSING",
                        "conditional-rule activation lifecycle event was not found",
                        retryable=True,
                    )
                action_side = str(row[2] or "").upper()
                current_state = str(row[3] or "")
                lifetime_raw = row[5]
                try:
                    lifetime = (
                        None
                        if lifetime_raw in {None, ""}
                        else int(str(lifetime_raw))
                    )
                except (TypeError, ValueError) as exc:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_BUNDLE_ACTIVATION_CONTEXT_INVALID",
                        "conditional-rule activation lifetime is invalid",
                        retryable=True,
                    ) from exc
                if (
                    action_side not in {"BUY", "SELL"}
                    or current_state
                    not in {
                        "DRAFT",
                        "NEEDS_CLARIFICATION",
                        "VALIDATED",
                        "PENDING_CONFIRMATION",
                        "ACTIVE",
                        "TRIGGERED",
                        "EXECUTION_PENDING",
                        "COMPLETED",
                        "PAUSED",
                        "EXPIRED",
                        "CANCELLED",
                        "FAILED",
                    }
                    or (lifetime is not None and not 1 <= lifetime <= 20)
                    or not isinstance(row[4], datetime)
                    or not isinstance(row[6], datetime)
                ):
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_BUNDLE_ACTIVATION_CONTEXT_INVALID",
                        "conditional-rule activation context is invalid",
                        retryable=True,
                    )
                return BundleActivatedNotificationContext(
                    rule_id=str(row[0]),
                    symbol=str(row[1]),
                    action_side=action_side,
                    current_state=current_state,
                    expires_at=row[4],
                    activation_lifetime_trading_days=lifetime,
                    occurred_at=row[6],
                    lifecycle_event_id=str(row[7]),
                    user_id=str(row[8]),
                    fund_id=str(row[9]),
                    book_id=str(row[10]),
                    client_request_id=str(row[11]),
                    order_request_id=str(row[12]) if row[12] else None,
                    ceo_root_task_id=str(row[13]) if row[13] else None,
                    trading_task_id=str(row[14]) if row[14] else None,
                )
        except RuleWorkerStoreError:
            raise
        except (ValueError, psycopg2.Error) as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not resolve conditional-rule activation notification context",
                retryable=True,
            ) from exc

    def trigger_claimed_notification_context(
        self, *, rule_id: str
    ) -> TriggerClaimedNotificationContext:
        """Resolve one true condition from durable trigger/evaluation facts."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,rule.symbol,version.spec->'action'->>'side',
                           rule.state,trigger.trigger_id,evaluation.data_watermark,
                           claimed.created_at,claimed.event_id,
                           rule.user_id,rule.fund_id,rule.book_id,
                           coalesce(bundle_request.client_request_id,
                                    fallback.client_request_id,
                                    rule.client_request_id),
                           coalesce(bundle_request.order_request_id,
                                    fallback.order_request_id),
                           coalesce(bundle_request.ceo_root_task_id,
                                    fallback.ceo_root_task_id),
                           coalesce(bundle_request.trading_task_id,
                                    fallback.trading_task_id)
                      from execution.conditional_trade_rules rule
                      join execution.conditional_trade_rule_versions version
                        on version.rule_id=rule.rule_id
                       and version.rule_version=rule.current_version
                      join lateral (
                        select event.event_id,event.payload,event.created_at
                          from execution.conditional_trade_rule_events event
                         where event.rule_id=rule.rule_id
                           and event.event_type='TRIGGER_CLAIMED'
                         order by event.created_at desc,event.event_id desc
                         limit 1
                      ) claimed on true
                      join execution.conditional_rule_triggers trigger
                        on trigger.trigger_id=claimed.payload->>'trigger_id'
                       and trigger.rule_id=rule.rule_id
                      join execution.conditional_rule_evaluations evaluation
                        on evaluation.evaluation_id=trigger.evaluation_id
                       and evaluation.outcome='TRUE'
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_paper_order_bundles bundle
                          join execution.user_order_requests request
                            on request.order_request_id=
                               bundle.immediate_order_request_id
                         where bundle.conditional_rule_id=rule.rule_id
                           and bundle.user_id=rule.user_id
                           and bundle.fund_id=rule.fund_id
                           and bundle.book_id=rule.book_id
                         order by bundle.updated_at desc,bundle.bundle_id desc
                         limit 1
                      ) bundle_request on true
                      left join lateral (
                        select request.order_request_id,request.client_request_id,
                               request.ceo_root_task_id,request.trading_task_id
                          from execution.user_order_requests request
                         where request.user_id=rule.user_id
                           and (
                             request.client_request_id=rule.client_request_id
                             or request.canonical_payload->>'rule_id'
                                  = rule.rule_id::text
                             or coalesce(
                                  request.canonical_payload->'rule_ids',
                                  '[]'::jsonb
                                ) ? rule.rule_id::text
                           )
                         order by
                           (request.client_request_id=rule.client_request_id) desc,
                           request.updated_at desc
                         limit 1
                      ) fallback on true
                     where rule.rule_id=%s
                    """,
                    (UUID(str(rule_id)),),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_TRIGGER_CONTEXT_MISSING",
                        "conditional-rule true trigger was not found",
                        retryable=True,
                    )
                action_side = str(row[2] or "").upper()
                current_state = str(row[3] or "")
                trigger = str(row[4] or "").strip()
                if (
                    action_side not in {"BUY", "SELL"}
                    or current_state
                    not in {
                        "DRAFT",
                        "NEEDS_CLARIFICATION",
                        "VALIDATED",
                        "PENDING_CONFIRMATION",
                        "ACTIVE",
                        "TRIGGERED",
                        "EXECUTION_PENDING",
                        "COMPLETED",
                        "PAUSED",
                        "EXPIRED",
                        "CANCELLED",
                        "FAILED",
                    }
                    or not trigger
                    or not isinstance(row[5], datetime)
                    or not isinstance(row[6], datetime)
                ):
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_TRIGGER_CONTEXT_INVALID",
                        "conditional-rule true trigger context is invalid",
                        retryable=True,
                    )
                return TriggerClaimedNotificationContext(
                    rule_id=str(row[0]),
                    symbol=str(row[1]),
                    action_side=action_side,
                    current_state=current_state,
                    trigger_id=trigger,
                    data_watermark=row[5],
                    occurred_at=row[6],
                    lifecycle_event_id=str(row[7]),
                    user_id=str(row[8]),
                    fund_id=str(row[9]),
                    book_id=str(row[10]),
                    client_request_id=str(row[11]),
                    order_request_id=str(row[12]) if row[12] else None,
                    ceo_root_task_id=str(row[13]) if row[13] else None,
                    trading_task_id=str(row[14]) if row[14] else None,
                )
        except RuleWorkerStoreError:
            raise
        except (ValueError, psycopg2.Error) as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not resolve conditional-rule trigger notification context",
                retryable=True,
            ) from exc

    @staticmethod
    def _active_row(row: tuple[Any, ...]) -> ActiveRule:
        raw_spec = json.loads(row[5]) if isinstance(row[5], str) else row[5]
        try:
            spec = validate_rule_spec(ConditionalRuleSpec.model_validate(raw_spec))
        except Exception as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_STORED_SPEC_INVALID",
                "stored conditional rule spec is invalid",
            ) from exc
        if str(row[4]) != str(row[6]):
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_CONFIRMATION_MISMATCH",
                "active rule does not match the confirmed fingerprint",
            )
        if rule_fingerprint(spec) != str(row[6]):
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_FINGERPRINT_INVALID",
                "stored rule payload does not match its fingerprint",
            )
        return ActiveRule(
            rule_id=UUID(str(row[0])),
            rule_version=int(row[1]),
            row_version=int(row[2]),
            spec_sha256=str(row[4]),
            spec=spec,
        )

    def list_active(self, *, limit: int = 100, offset: int = 0) -> list[ActiveRule]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,rule.current_version,rule.version,
                           rule.state,rule.confirmation_sha256,version.spec,
                           version.spec_sha256
                      from execution.conditional_trade_rules rule
                      join execution.conditional_trade_rule_versions version
                        on version.rule_id=rule.rule_id
                       and version.rule_version=rule.current_version
                     where rule.state='ACTIVE' and rule.execution_mode='PAPER'
                       and rule.repeat_policy='ONCE' and rule.expires_at>now()
                       and rule.confirmation_sha256=version.spec_sha256
                     order by rule.updated_at,rule.rule_id
                     limit %s offset %s
                    """,
                    (
                        max(1, min(limit, 1000)),
                        max(0, min(int(offset), 1_000_000)),
                    ),
                )
                return [self._active_row(row) for row in cursor.fetchall()]
        except RuleWorkerStoreError:
            raise
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not list active conditional rules",
                retryable=True,
            ) from exc

    def activate_ready_bundles(self, *, limit: int = 100) -> int:
        """Activate a deferred rule only after its immediate request completed."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select bundle.bundle_id, bundle.conditional_rule_id,
                           request.state, rule.state, rule.current_version,
                           version.spec_sha256, version.spec
                      from execution.user_paper_order_bundles bundle
                      join execution.user_order_requests request
                        on request.order_request_id=bundle.immediate_order_request_id
                      join execution.conditional_trade_rules rule
                        on rule.rule_id=bundle.conditional_rule_id
                      join execution.conditional_trade_rule_versions version
                        on version.rule_id=rule.rule_id
                       and version.rule_version=rule.current_version
                     where bundle.state='WAITING_FOR_IMMEDIATE_FILL'
                       and rule.state in ('PENDING_CONFIRMATION','EXPIRED')
                     order by bundle.updated_at,bundle.bundle_id
                     limit %s
                     -- The request row is read-only here.  Lock only the rows
                     -- transitioned by this worker so its read-only RLS policy
                     -- is sufficient and another worker cannot claim the same
                     -- bundle/rule transition.
                     for update of bundle,rule skip locked
                    """,
                    (max(1, min(limit, 1000)),),
                )
                changed = 0
                for (
                    bundle_id,
                    rule_id,
                    request_state,
                    rule_state,
                    rule_version,
                    spec_sha,
                    raw_spec,
                ) in cursor.fetchall():
                    if (
                        request_state == "COMPLETED"
                        and str(rule_state) == "PENDING_CONFIRMATION"
                    ):
                        try:
                            decoded_spec = (
                                json.loads(raw_spec)
                                if isinstance(raw_spec, str)
                                else raw_spec
                            )
                            spec = validate_rule_spec(
                                ConditionalRuleSpec.model_validate(decoded_spec)
                            )
                        except Exception as exc:
                            raise RuleWorkerStoreError(
                                "CONDITIONAL_RULE_STORED_SPEC_INVALID",
                                "deferred compound rule has an invalid stored specification",
                            ) from exc
                        if rule_fingerprint(spec) != str(spec_sha):
                            raise RuleWorkerStoreError(
                                "CONDITIONAL_RULE_STORED_SPEC_INVALID",
                                "deferred compound rule fingerprint does not match its specification",
                            )

                        active_expires_at = None
                        if spec.activation_lifetime_trading_days is not None:
                            try:
                                active_expires_at = self._krx_close_after_activation(
                                    cursor,
                                    trading_days=spec.activation_lifetime_trading_days,
                                )
                            except RuleWorkerStoreError as exc:
                                failure_codes = {
                                    "CONDITIONAL_RULE_KRX_CALENDAR_UNAVAILABLE": (
                                        "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE"
                                    ),
                                    "CONDITIONAL_RULE_KRX_CALENDAR_INVALID": (
                                        "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_INVALID"
                                    ),
                                }
                                failure_code = failure_codes.get(exc.code)
                                if failure_code is None:
                                    raise
                                if self._fail_bundle_activation(
                                    cursor,
                                    bundle_id=bundle_id,
                                    rule_id=rule_id,
                                    rule_version=rule_version,
                                    failure_code=failure_code,
                                ):
                                    changed += 1
                                continue
                            cursor.execute(
                                """
                                update execution.conditional_trade_rules
                                   set state='ACTIVE',confirmation_sha256=%s,
                                       confirmed_at=now(),expires_at=%s,
                                       version=version+1
                                 where rule_id=%s and state='PENDING_CONFIRMATION'
                                """,
                                (str(spec_sha), active_expires_at, rule_id),
                            )
                        else:
                            cursor.execute(
                                """
                                update execution.conditional_trade_rules
                                   set state='ACTIVE',confirmation_sha256=%s,
                                       confirmed_at=now(),version=version+1
                                 where rule_id=%s and state='PENDING_CONFIRMATION'
                                """,
                                (str(spec_sha), rule_id),
                            )
                        if cursor.rowcount != 1:
                            continue
                        cursor.execute(
                            """
                            update execution.user_paper_order_bundles
                               set state='CONDITIONAL_ACTIVE',version=version+1
                             where bundle_id=%s and state='WAITING_FOR_IMMEDIATE_FILL'
                            """,
                            (bundle_id,),
                        )
                        self._record_lifecycle_event(
                            cursor,
                            event_id=_stable_id("dep_", bundle_id, "ACTIVATED"),
                            rule_id=rule_id,
                            rule_version=rule_version,
                            event_type="BUNDLE_ACTIVATED",
                            from_state="PENDING_CONFIRMATION",
                            to_state="ACTIVE",
                            payload={
                                "bundle_id": str(bundle_id),
                                "activation_lifetime_trading_days": (
                                    spec.activation_lifetime_trading_days
                                ),
                                "active_expires_at": (
                                    active_expires_at.isoformat()
                                    if active_expires_at is not None
                                    else None
                                ),
                                "order_submitted": False,
                            },
                        )
                        changed += 1
                    elif str(rule_state) == "EXPIRED" or request_state in {
                        "FAILED",
                        "UNKNOWN",
                        "CLARIFICATION_REQUIRED",
                        "REJECTED",
                    }:
                        cursor.execute(
                            """
                            update execution.conditional_trade_rules
                               set state='FAILED',version=version+1,
                                   completed_at=now()
                             where rule_id=%s and state='PENDING_CONFIRMATION'
                            """,
                            (rule_id,),
                        )
                        cursor.execute(
                            """
                            update execution.user_paper_order_bundles
                               set state='FAILED',error_code='IMMEDIATE_ORDER_NOT_FILLED',
                                   error_message='immediate PAPER order did not complete',
                                   completed_at=now(),version=version+1
                             where bundle_id=%s and state='WAITING_FOR_IMMEDIATE_FILL'
                            """,
                            (bundle_id,),
                        )
                        changed += 1
                return changed
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not activate deferred compound PAPER rules",
                retryable=True,
            ) from exc

    def _fail_bundle_activation(
        self,
        cursor: Any,
        *,
        bundle_id: UUID,
        rule_id: UUID,
        rule_version: int,
        failure_code: str,
    ) -> bool:
        """Fail closed after a full entry when its protective exit cannot arm.

        Retrying a calendar-less activation in the background leaves the user
        believing an unarmed exit is watching.  Terminating the bundle makes
        the missing protection visible and creates no compensating order.
        """

        if failure_code not in {
            "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_UNAVAILABLE",
            "ENTRY_EXIT_ACTIVATION_KRX_CALENDAR_INVALID",
        }:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_ACTIVATION_FAILURE_CODE_INVALID",
                "activation-blocked failure code is invalid",
            )
        cursor.execute(
            """
            update execution.conditional_trade_rules
               set state='FAILED',version=version+1,completed_at=now()
             where rule_id=%s and current_version=%s
               and state='PENDING_CONFIRMATION'
            returning rule_id
            """,
            (rule_id, rule_version),
        )
        if cursor.fetchone() is None:
            return False
        lifecycle_event_id = _stable_id(
            "blk_", bundle_id, rule_id, rule_version, failure_code
        )
        self._record_lifecycle_event(
            cursor,
            event_id=lifecycle_event_id,
            rule_id=rule_id,
            rule_version=int(rule_version),
            event_type="BUNDLE_ACTIVATION_BLOCKED",
            from_state="PENDING_CONFIRMATION",
            to_state="FAILED",
            payload={"code": failure_code, "order_submitted": False},
        )
        cursor.execute(
            """
            update execution.user_paper_order_bundles
               set state='FAILED',error_code=%s,
                   error_message='protective PAPER exit could not activate from the official KRX calendar',
                   completed_at=now(),version=version+1
             where bundle_id=%s and conditional_rule_id=%s
               and state='WAITING_FOR_IMMEDIATE_FILL'
            """,
            (failure_code, bundle_id, rule_id),
        )
        return True

    @staticmethod
    def _krx_close_after_activation(cursor: Any, *, trading_days: int) -> datetime:
        """Read the Nth eligible KRX close from the governed session calendar.

        This intentionally avoids a weekday approximation.  A full fill might
        be detected after the current regular session has already closed, in
        which case ``closes_at > now()`` starts from the next official session.
        A missing or incomplete calendar fails closed: the protective exit is
        not activated with a guessed expiry.
        """

        if not 1 <= trading_days <= 20:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_ACTIVATION_LIFETIME_INVALID",
                "activation lifetime must be between one and twenty KRX sessions",
            )
        cursor.execute(
            """
            with governed_sessions as (
              select distinct on (session.trade_date)
                     session.trade_date,session.closes_at
                from reference.market_sessions session
                join reference.market_calendar_versions calendar
                  on calendar.calendar_version_id=session.calendar_version_id
               where session.market='KRX'
                 and session.session_type='REGULAR'
                 and session.is_trading_day
                 and calendar.market='KRX'
                 and calendar.effective_from <= session.trade_date
                 and (calendar.effective_to is null
                      or calendar.effective_to >= session.trade_date)
                 and session.trade_date >= (now() at time zone 'Asia/Seoul')::date
                 and session.closes_at > now()
               order by session.trade_date,calendar.version desc
            )
            select closes_at
              from governed_sessions
             order by trade_date
             offset %s
             limit 1
            """,
            (trading_days - 1,),
        )
        row = cursor.fetchone()
        if row is None or row[0] is None or not isinstance(row[0], datetime):
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_KRX_CALENDAR_UNAVAILABLE",
                "official KRX regular-session calendar cannot supply the requested exit lifetime",
                retryable=True,
            )
        if row[0].tzinfo is None:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_KRX_CALENDAR_INVALID",
                "official KRX regular-session close must include timezone",
            )
        return row[0]

    def list_claimed(
        self, *, limit: int = 100
    ) -> list[tuple[ActiveRule, TriggerClaim]]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.rule_id,rule.current_version,rule.version,
                           rule.state,rule.confirmation_sha256,version.spec,
                           version.spec_sha256,trigger.trigger_id,
                           trigger.evaluation_id
                      from execution.conditional_rule_triggers trigger
                      join execution.conditional_trade_rules rule
                        on rule.rule_id=trigger.rule_id
                       and rule.current_version=trigger.rule_version
                      join execution.conditional_trade_rule_versions version
                        on version.rule_id=rule.rule_id
                       and version.rule_version=rule.current_version
                     where trigger.state='CLAIMED' and rule.state='TRIGGERED'
                       and rule.execution_mode='PAPER'
                       and rule.confirmation_sha256=version.spec_sha256
                     order by trigger.created_at,trigger.trigger_id
                     limit %s
                    """,
                    (max(1, min(limit, 1000)),),
                )
                return [
                    (
                        self._active_row(row[:7]),
                        TriggerClaim(str(row[7]), str(row[8])),
                    )
                    for row in cursor.fetchall()
                ]
        except RuleWorkerStoreError:
            raise
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not list claimed conditional triggers",
                retryable=True,
            ) from exc

    def expire_due(self) -> int:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule_id,current_version,state,expires_at
                      from execution.conditional_trade_rules
                     where state in ('PENDING_CONFIRMATION','ACTIVE','PAUSED')
                       and expires_at<=now()
                     order by expires_at,rule_id
                     limit 1000
                     for update skip locked
                    """
                )
                changed = 0
                for rule_id, rule_version, prior_state, expires_at in cursor.fetchall():
                    cursor.execute(
                        """
                        update execution.conditional_trade_rules
                           set state='EXPIRED',version=version+1,completed_at=now()
                         where rule_id=%s and current_version=%s
                           and state=%s and expires_at<=now()
                        """,
                        (rule_id, rule_version, prior_state),
                    )
                    if cursor.rowcount != 1:
                        continue
                    lifecycle_event_id = _stable_id(
                        "exp_", rule_id, rule_version, "CONDITIONAL_RULE_EXPIRED"
                    )
                    payload = {
                        "expires_at": expires_at.isoformat(),
                        "prior_state": str(prior_state),
                        "order_submitted": False,
                    }
                    self._record_lifecycle_event(
                        cursor,
                        event_id=lifecycle_event_id,
                        rule_id=rule_id,
                        rule_version=int(rule_version),
                        event_type="CONDITIONAL_RULE_EXPIRED",
                        from_state=str(prior_state),
                        to_state="EXPIRED",
                        payload=payload,
                    )
                    # A linked entry/exit bundle must not continue to present
                    # itself as actively protected once its child rule stopped
                    # watching.  No PAPER order is created by this transition.
                    cursor.execute(
                        """
                        update execution.user_paper_order_bundles
                           set state='FAILED',
                               error_code='CONDITIONAL_EXIT_EXPIRED',
                               error_message='conditional PAPER exit expired without submitting an order',
                               completed_at=now(),version=version+1
                         where conditional_rule_id=%s
                           and state in ('WAITING_FOR_IMMEDIATE_FILL','CONDITIONAL_ACTIVE')
                        """,
                        (rule_id,),
                    )
                    changed += 1
                return changed
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not expire conditional rules",
                retryable=True,
            ) from exc

    def record_false(
        self,
        rule: ActiveRule,
        *,
        evaluation_key: str,
        context_sha256: str,
        data_watermark: datetime,
    ) -> bool:
        return self._record_evaluation(
            rule,
            evaluation_key=evaluation_key,
            condition_result=False,
            outcome="FALSE",
            context_sha256=context_sha256,
            data_watermark=data_watermark,
        )

    def record_error(
        self,
        rule: ActiveRule,
        *,
        evaluation_key: str,
        context_sha256: str,
        data_watermark: datetime,
        error_code: str,
        error_message: str,
    ) -> bool:
        return self._record_evaluation(
            rule,
            evaluation_key=evaluation_key,
            condition_result=None,
            outcome="ERROR",
            context_sha256=context_sha256,
            data_watermark=data_watermark,
            error_code=error_code,
            error_message=error_message[:1000],
        )

    def _record_evaluation(
        self,
        rule: ActiveRule,
        *,
        evaluation_key: str,
        condition_result: bool | None,
        outcome: str,
        context_sha256: str,
        data_watermark: datetime,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> bool:
        identity = evaluation_id(str(rule.rule_id), rule.rule_version, evaluation_key)
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    insert into execution.conditional_rule_evaluations (
                      evaluation_id,rule_id,rule_version,evaluation_key,
                      evaluation_clock,condition_result,outcome,context_sha256,
                      data_watermark,error_code,error_message
                    ) values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (rule_id,rule_version,evaluation_key) do nothing
                    returning evaluation_id
                    """,
                    (
                        identity,
                        rule.rule_id,
                        rule.rule_version,
                        evaluation_key,
                        rule.spec.evaluation.clock.value,
                        condition_result,
                        outcome,
                        context_sha256,
                        data_watermark,
                        error_code,
                        error_message,
                    ),
                )
                return cursor.fetchone() is not None
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not record conditional rule evaluation",
                retryable=True,
            ) from exc

    def observe_trailing_stop(
        self,
        rule: ActiveRule,
        *,
        last_price: Decimal,
        average_entry_price: Decimal,
        observed_at: datetime,
    ) -> TrailingStopObservation:
        """Durably advance one trailing high-water state under its row lock."""

        parameters = trailing_stop_parameters(rule.spec.condition)
        initial = advance_trailing_stop(
            None,
            parameters=parameters,
            last_price=last_price,
            average_entry_price=average_entry_price,
            observed_at=observed_at,
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    insert into execution.conditional_rule_trailing_states (
                      rule_id,rule_version,high_price,armed_at,last_observed_at
                    ) values (%s,%s,%s,%s,%s)
                    on conflict (rule_id,rule_version) do nothing
                    """,
                    (
                        rule.rule_id,
                        rule.rule_version,
                        initial.state.high_price,
                        initial.state.armed_at,
                        initial.state.last_observed_at,
                    ),
                )
                if cursor.rowcount == 1:
                    return initial
                cursor.execute(
                    """
                    select high_price,armed_at,last_observed_at
                      from execution.conditional_rule_trailing_states
                     where rule_id=%s and rule_version=%s
                     for update
                    """,
                    (rule.rule_id, rule.rule_version),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "TRAILING_STOP_STATE_MISSING",
                        "trailing stop state disappeared during observation",
                        retryable=True,
                    )
                existing = TrailingStopState(
                    high_price=Decimal(str(row[0])),
                    armed_at=row[1],
                    last_observed_at=row[2],
                )
                observation = advance_trailing_stop(
                    existing,
                    parameters=parameters,
                    last_price=last_price,
                    average_entry_price=average_entry_price,
                    observed_at=observed_at,
                )
                if observation.ignored_stale_quote:
                    return observation
                cursor.execute(
                    """
                    update execution.conditional_rule_trailing_states
                       set high_price=%s,armed_at=%s,last_observed_at=%s
                     where rule_id=%s and rule_version=%s
                    """,
                    (
                        observation.state.high_price,
                        observation.state.armed_at,
                        observation.state.last_observed_at,
                        rule.rule_id,
                        rule.rule_version,
                    ),
                )
                return observation
        except RuleWorkerStoreError:
            raise
        except (ValueError, psycopg2.Error) as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not persist trailing stop state",
                retryable=True,
            ) from exc

    def cancel_entry_trailing_on_position_mismatch(
        self,
        rule: ActiveRule,
        *,
        expected_position_quantity: Decimal,
        actual_position_quantity: Decimal,
        evaluation_key: str,
        context_sha256: str,
        data_watermark: datetime,
    ) -> bool:
        """Terminally retire an already-started entry-originated trailing rule.

        A bundle is permitted to wait for its first post-fill holding snapshot.
        Once its high-water row exists, though, a position quantity mismatch
        proves that the original entry no longer maps one-to-one to the
        account position.  This transaction records the diagnostic, cancels
        the rule, fails the linked bundle, and emits one outbox event.  It is
        intentionally not a pause: a later coincidental quantity match must
        not re-arm an automatic exit for a manually changed position.
        """

        identity = evaluation_id(str(rule.rule_id), rule.rule_version, evaluation_key)
        event_id = _stable_id(
            "trail_", rule.rule_id, rule.rule_version, "ENTRY_POSITION_QUANTITY_MISMATCH"
        )
        payload = {
            "code": "ENTRY_POSITION_QUANTITY_MISMATCH",
            "expected_position_quantity": str(expected_position_quantity),
            "actual_position_quantity": str(actual_position_quantity),
            "evaluation_key": evaluation_key,
        }
        message = (
            "entry-originated trailing exit was cancelled because the current "
            f"position quantity {actual_position_quantity} no longer matches the "
            f"fully filled entry quantity {expected_position_quantity}"
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                # Lock the aggregate before its state row.  A concurrent true
                # trigger therefore cannot pass ACTIVE after this terminal
                # transition commits.
                cursor.execute(
                    """
                    select state
                      from execution.conditional_trade_rules
                     where rule_id=%s and current_version=%s and version=%s
                     for update
                    """,
                    (rule.rule_id, rule.rule_version, rule.row_version),
                )
                aggregate = cursor.fetchone()
                if aggregate is None or str(aggregate[0]) != "ACTIVE":
                    return False
                cursor.execute(
                    """
                    select 1
                      from execution.conditional_rule_trailing_states
                     where rule_id=%s and rule_version=%s
                     for update
                    """,
                    (rule.rule_id, rule.rule_version),
                )
                # No high-water state yet means settlement may still be
                # propagating.  Keep waiting rather than cancelling a fresh
                # full-fill bundle before its first usable snapshot.
                if cursor.fetchone() is None:
                    return False
                cursor.execute(
                    """
                    insert into execution.conditional_rule_evaluations (
                      evaluation_id,rule_id,rule_version,evaluation_key,
                      evaluation_clock,condition_result,outcome,context_sha256,
                      data_watermark,error_code,error_message
                    ) values (%s,%s,%s,%s,%s,null,'ERROR',%s,%s,%s,%s)
                    on conflict (rule_id,rule_version,evaluation_key) do nothing
                    """,
                    (
                        identity,
                        rule.rule_id,
                        rule.rule_version,
                        evaluation_key,
                        rule.spec.evaluation.clock.value,
                        context_sha256,
                        data_watermark,
                        "ENTRY_POSITION_QUANTITY_MISMATCH",
                        message,
                    ),
                )
                cursor.execute(
                    """
                    update execution.conditional_trade_rules
                       set state='CANCELLED',version=version+1,completed_at=now()
                     where rule_id=%s and current_version=%s and version=%s
                       and state='ACTIVE'
                    returning rule_id
                    """,
                    (rule.rule_id, rule.rule_version, rule.row_version),
                )
                if cursor.fetchone() is None:
                    return False
                self._record_lifecycle_event(
                    cursor,
                    event_id=event_id,
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    event_type="ENTRY_POSITION_MISMATCH",
                    from_state="ACTIVE",
                    to_state="CANCELLED",
                    payload=payload,
                )
                cursor.execute(
                    """
                    update execution.user_paper_order_bundles
                       set state='FAILED',error_code='ENTRY_POSITION_QUANTITY_MISMATCH',
                           error_message=%s,completed_at=now(),version=version+1
                     where conditional_rule_id=%s and state='CONDITIONAL_ACTIVE'
                    """,
                    (message, rule.rule_id),
                )
                return True
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not terminally cancel entry-originated trailing rule",
                retryable=True,
            ) from exc

    def claim_true(
        self,
        rule: ActiveRule,
        *,
        evaluation_key: str,
        context_sha256: str,
        data_watermark: datetime,
    ) -> TriggerClaim | None:
        evaluation_identity = evaluation_id(
            str(rule.rule_id), rule.rule_version, evaluation_key
        )
        condition_sha256 = expression_fingerprint(rule.spec.condition)
        trigger_identity = trigger_id(
            str(rule.rule_id),
            rule.rule_version,
            evaluation_key,
            condition_sha256,
        )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    insert into execution.conditional_rule_evaluations (
                      evaluation_id,rule_id,rule_version,evaluation_key,
                      evaluation_clock,condition_result,outcome,context_sha256,
                      data_watermark
                    ) values (%s,%s,%s,%s,%s,true,'TRUE',%s,%s)
                    on conflict (rule_id,rule_version,evaluation_key) do nothing
                    returning evaluation_id
                    """,
                    (
                        evaluation_identity,
                        rule.rule_id,
                        rule.rule_version,
                        evaluation_key,
                        rule.spec.evaluation.clock.value,
                        context_sha256,
                        data_watermark,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    """
                    update execution.conditional_trade_rules
                       set state='TRIGGERED',version=version+1
                     where rule_id=%s and current_version=%s and version=%s
                       and state='ACTIVE' and confirmation_sha256=%s
                    returning version
                    """,
                    (
                        rule.rule_id,
                        rule.rule_version,
                        rule.row_version,
                        rule.spec_sha256,
                    ),
                )
                if cursor.fetchone() is None:
                    return None
                cursor.execute(
                    """
                    insert into execution.conditional_rule_triggers (
                      trigger_id,rule_id,rule_version,evaluation_id,
                      condition_sha256,state
                    ) values (%s,%s,%s,%s,%s,'CLAIMED')
                    """,
                    (
                        trigger_identity,
                        rule.rule_id,
                        rule.rule_version,
                        evaluation_identity,
                        condition_sha256,
                    ),
                )
                self._record_lifecycle_event(
                    cursor,
                    event_id=_stable_id("cre_", trigger_identity, "claimed"),
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    event_type="TRIGGER_CLAIMED",
                    from_state="ACTIVE",
                    to_state="TRIGGERED",
                    payload={
                        "trigger_id": trigger_identity,
                        "order_submitted": False,
                    },
                )
                return TriggerClaim(trigger_identity, evaluation_identity)
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not claim conditional rule trigger",
                retryable=True,
            ) from exc

    def create_execution(
        self,
        rule: ActiveRule,
        claim: TriggerClaim,
        *,
        allowed: bool,
        guard_code: str,
        quantity: Decimal | None,
    ) -> SubmitReadyExecution | None:
        key = execution_idempotency_key(
            str(rule.rule_id), rule.rule_version, claim.trigger_id
        )
        state = "PENDING" if allowed else "GUARD_REJECTED"
        if allowed and (quantity is None or quantity <= 0):
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_EXECUTION_QUANTITY_INVALID",
                "allowed execution requires a positive quantity",
            )
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule.state,trigger.state
                      from execution.conditional_trade_rules rule
                      join execution.conditional_rule_triggers trigger
                        on trigger.rule_id=rule.rule_id
                     where rule.rule_id=%s and trigger.trigger_id=%s
                     for update of rule,trigger
                    """,
                    (rule.rule_id, claim.trigger_id),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_TRIGGER_MISSING",
                        "claimed conditional trigger disappeared",
                    )
                if tuple(str(value) for value in row) != ("TRIGGERED", "CLAIMED"):
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_CONCURRENT_TRANSITION",
                        "conditional trigger is no longer guard-ready",
                        retryable=True,
                    )
                cursor.execute(
                    """
                    insert into execution.conditional_rule_executions (
                      trigger_id,rule_id,rule_version,state,side,quantity,
                      idempotency_key,guard_code
                    ) values (%s,%s,%s,%s,%s,%s,%s,%s)
                    on conflict (trigger_id) do nothing
                    returning rule_execution_id
                    """,
                    (
                        claim.trigger_id,
                        rule.rule_id,
                        rule.rule_version,
                        state,
                        rule.spec.action.side.value,
                        quantity,
                        key,
                        guard_code,
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    cursor.execute(
                        """
                        select rule_execution_id
                          from execution.conditional_rule_executions
                         where trigger_id=%s
                        """,
                        (claim.trigger_id,),
                    )
                    existing = cursor.fetchone()
                    if existing is None:
                        raise RuleWorkerStoreError(
                            "CONDITIONAL_RULE_EXECUTION_MISSING",
                            "conditional execution conflict could not be resolved",
                        )
                    execution_id = UUID(str(existing[0]))
                else:
                    execution_id = UUID(str(inserted[0]))
                if allowed:
                    cursor.execute(
                        """
                        update execution.conditional_rule_triggers
                           set state='EXECUTION_PENDING',guard_code=%s
                         where trigger_id=%s and state='CLAIMED'
                        """,
                        (guard_code, claim.trigger_id),
                    )
                    cursor.execute(
                        """
                        update execution.conditional_trade_rules
                           set state='EXECUTION_PENDING',version=version+1
                         where rule_id=%s and state='TRIGGERED'
                           and current_version=%s
                        """,
                        (rule.rule_id, rule.rule_version),
                    )
                    event_type, target = "EXECUTION_READY", "EXECUTION_PENDING"
                else:
                    cursor.execute(
                        """
                        update execution.conditional_rule_triggers
                           set state='GUARD_REJECTED',guard_code=%s
                         where trigger_id=%s and state='CLAIMED'
                        """,
                        (guard_code, claim.trigger_id),
                    )
                    cursor.execute(
                        """
                        update execution.conditional_trade_rules
                           set state='FAILED',version=version+1,completed_at=now()
                         where rule_id=%s and state='TRIGGERED'
                           and current_version=%s
                        """,
                        (rule.rule_id, rule.rule_version),
                    )
                    event_type, target = "GUARD_REJECTED", "FAILED"
                if cursor.rowcount != 1:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_CONCURRENT_TRANSITION",
                        "conditional rule changed during execution creation",
                        retryable=True,
                    )
                payload = {
                    "trigger_id": claim.trigger_id,
                    "rule_execution_id": str(execution_id),
                    "guard_code": guard_code,
                    "quantity": str(quantity) if quantity is not None else None,
                }
                cursor.execute(
                    """
                    insert into execution.conditional_trade_rule_events (
                      event_id,rule_id,rule_version,event_type,from_state,to_state,payload
                    ) values (%s,%s,%s,%s,'TRIGGERED',%s,%s)
                    on conflict (event_id) do nothing
                    """,
                    (
                        _stable_id("cre_", claim.trigger_id, event_type),
                        rule.rule_id,
                        rule.rule_version,
                        event_type,
                        target,
                        Json(payload),
                    ),
                )
                cursor.execute(
                    """
                    insert into execution.conditional_rule_outbox (
                      event_id,aggregate_id,event_type,payload
                    ) values (%s,%s,%s,%s)
                    on conflict (event_id) do nothing
                    """,
                    (
                        _stable_id("cro_", claim.trigger_id, event_type),
                        str(rule.rule_id),
                        event_type,
                        Json(payload),
                    ),
                )
                if not allowed:
                    return None
                return SubmitReadyExecution(
                    rule_execution_id=execution_id,
                    trigger_id=claim.trigger_id,
                    rule_id=rule.rule_id,
                    rule_version=rule.rule_version,
                    idempotency_key=key,
                )
        except RuleWorkerStoreError:
            raise
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not create conditional rule execution",
                retryable=True,
            ) from exc

    def list_submit_ready(self, *, limit: int = 100) -> list[SubmitReadyExecution]:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select execution.rule_execution_id,execution.trigger_id,
                           execution.rule_id,execution.rule_version,
                           execution.idempotency_key
                      from execution.conditional_rule_executions execution
                      join execution.conditional_trade_rules rule
                        on rule.rule_id=execution.rule_id
                     where execution.state in ('PENDING','SUBMITTING')
                       and rule.state='EXECUTION_PENDING'
                       and rule.execution_mode='PAPER'
                     order by execution.created_at,execution.rule_execution_id
                     limit %s
                    """,
                    (max(1, min(limit, 1000)),),
                )
                return [
                    SubmitReadyExecution(
                        rule_execution_id=UUID(str(row[0])),
                        trigger_id=str(row[1]),
                        rule_id=UUID(str(row[2])),
                        rule_version=int(row[3]),
                        idempotency_key=str(row[4]),
                    )
                    for row in cursor.fetchall()
                ]
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not list submit-ready conditional executions",
                retryable=True,
            ) from exc

    def _record_lifecycle_event(
        self,
        cursor: Any,
        *,
        event_id: str,
        rule_id: UUID,
        rule_version: int,
        event_type: str,
        from_state: str,
        to_state: str,
        payload: Mapping[str, Any],
    ) -> None:
        """Persist one conditional-rule lifecycle event and its outbox row."""

        cursor.execute(
            """
            insert into execution.conditional_trade_rule_events (
              event_id,rule_id,rule_version,event_type,from_state,to_state,payload
            ) values (%s,%s,%s,%s,%s,%s,%s)
            on conflict (event_id) do nothing
            """,
            (
                event_id,
                rule_id,
                rule_version,
                event_type,
                from_state,
                to_state,
                Json(dict(payload)),
            ),
        )
        cursor.execute(
            """
            insert into execution.conditional_rule_outbox (
              event_id,aggregate_id,event_type,payload
            ) values (%s,%s,%s,%s)
            on conflict (event_id) do nothing
            """,
            (
                _stable_id("cro_", event_id),
                str(rule_id),
                event_type,
                Json(dict(payload)),
            ),
        )

    def _select_submission_row(
        self, cursor: Any, rule_execution_id: UUID, *, for_update: bool = False
    ) -> Any:
        query = """
            select execution.rule_id,execution.trigger_id,execution.rule_version,
                   execution.state,execution.updated_at,
                   version.spec->>'oco_group_id',
                   rule.user_id,rule.fund_id,rule.book_id
              from execution.conditional_rule_executions execution
              join execution.conditional_trade_rules rule
                on rule.rule_id=execution.rule_id
              join execution.conditional_trade_rule_versions version
                on version.rule_id=execution.rule_id
               and version.rule_version=execution.rule_version
             where execution.rule_execution_id=%s
        """
        if for_update:
            query += " for update of execution,rule"
        cursor.execute(query, (rule_execution_id,))
        return cursor.fetchone()

    def _pause_oco_siblings(
        self,
        cursor: Any,
        *,
        rule_id: UUID,
        group_id: str,
        user_id: UUID,
        fund_id: UUID,
        book_id: UUID,
    ) -> int:
        """Temporarily disarm armed siblings while one leg is submitted."""

        cursor.execute(
            """
            update execution.conditional_trade_rules sibling
               set state='PAUSED',version=sibling.version+1
              from execution.conditional_trade_rule_versions sibling_version
             where sibling_version.rule_id=sibling.rule_id
               and sibling_version.rule_version=sibling.current_version
               and sibling_version.spec->>'oco_group_id'=%s
               and sibling.rule_id<>%s
               and sibling.state='ACTIVE'
               and sibling.user_id=%s
               and sibling.fund_id=%s
               and sibling.book_id=%s
            returning sibling.rule_id,sibling.current_version
            """,
            (group_id, rule_id, user_id, fund_id, book_id),
        )
        paused = cursor.fetchall()
        for sibling_id, sibling_version in paused:
            payload = {
                "reserved_by_rule_id": str(rule_id),
                "oco_group_id": group_id,
            }
            self._record_lifecycle_event(
                cursor,
                event_id=_stable_id("oco_", sibling_id, rule_id, "reserved"),
                rule_id=sibling_id,
                rule_version=int(sibling_version),
                event_type="OCO_RESERVED",
                from_state="ACTIVE",
                to_state="PAUSED",
                payload=payload,
            )
        return len(paused)

    def _supersede_oco_execution(
        self,
        cursor: Any,
        *,
        rule_execution_id: UUID,
        trigger_id: str,
        rule_id: UUID,
        rule_version: int,
        winner_rule_id: UUID,
    ) -> None:
        """Close a losing pending leg without calling the broker."""

        payload = {
            "superseded_by_rule_id": str(winner_rule_id),
            "rule_execution_id": str(rule_execution_id),
        }
        cursor.execute(
            """
            update execution.conditional_rule_executions
               set state='FAILED',error_code='OCO_SUPERSEDED',
                   error_message='OCO sibling already owns submission slot',
                   completed_at=now()
             where rule_execution_id=%s and state in ('PENDING','SUBMITTING')
            """,
            (rule_execution_id,),
        )
        cursor.execute(
            """
            update execution.conditional_rule_triggers
               set state='FAILED'
             where trigger_id=%s and state='EXECUTION_PENDING'
            """,
            (trigger_id,),
        )
        cursor.execute(
            """
            update execution.conditional_trade_rules
               set state='FAILED',version=version+1,completed_at=now()
             where rule_id=%s and current_version=%s
               and state='EXECUTION_PENDING'
            """,
            (rule_id, rule_version),
        )
        self._record_lifecycle_event(
            cursor,
            event_id=_stable_id("oco_", rule_execution_id, winner_rule_id, "superseded"),
            rule_id=rule_id,
            rule_version=rule_version,
            event_type="OCO_SUPERSEDED",
            from_state="EXECUTION_PENDING",
            to_state="FAILED",
            payload=payload,
        )

    def mark_submitting(self, rule_execution_id: UUID) -> bool:
        """Acquire the external submission slot, serializing one OCO group."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                row = self._select_submission_row(cursor, rule_execution_id)
                if row is None:
                    return False
                initial_group_id = row[5]
                if initial_group_id:
                    cursor.execute(
                        "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
                        (str(initial_group_id),),
                    )
                row = self._select_submission_row(
                    cursor, rule_execution_id, for_update=True
                )
                if row is None:
                    return False
                (
                    rule_id,
                    trigger_identity,
                    rule_version,
                    state,
                    _updated_at,
                    group_id,
                    user_id,
                    fund_id,
                    book_id,
                ) = row
                if state not in {"PENDING", "SUBMITTING"}:
                    return False

                if group_id:
                    cursor.execute(
                        """
                        select exists(
                                 select 1
                                   from execution.conditional_rule_executions sibling_execution
                                   join execution.conditional_trade_rules sibling_rule
                                     on sibling_rule.rule_id=sibling_execution.rule_id
                                   join execution.conditional_trade_rule_versions sibling_version
                                     on sibling_version.rule_id=sibling_execution.rule_id
                                    and sibling_version.rule_version=sibling_execution.rule_version
                                  where sibling_execution.rule_execution_id<>%s
                                    and sibling_rule.user_id=%s
                                    and sibling_rule.fund_id=%s
                                    and sibling_rule.book_id=%s
                                    and sibling_version.spec->>'oco_group_id'=%s
                                    and sibling_execution.state in ('SUBMITTED','COMPLETED')
                               ),
                               exists(
                                 select 1
                                   from execution.conditional_rule_executions sibling_execution
                                   join execution.conditional_trade_rules sibling_rule
                                     on sibling_rule.rule_id=sibling_execution.rule_id
                                   join execution.conditional_trade_rule_versions sibling_version
                                     on sibling_version.rule_id=sibling_execution.rule_id
                                    and sibling_version.rule_version=sibling_execution.rule_version
                                  where sibling_execution.rule_execution_id<>%s
                                    and sibling_rule.user_id=%s
                                    and sibling_rule.fund_id=%s
                                    and sibling_rule.book_id=%s
                                    and sibling_version.spec->>'oco_group_id'=%s
                                    and sibling_execution.state='SUBMITTING'
                                    and sibling_execution.updated_at > now() - (%s * interval '1 second')
                               )
                        """,
                        (
                            rule_execution_id,
                            user_id,
                            fund_id,
                            book_id,
                            str(group_id),
                            rule_execution_id,
                            user_id,
                            fund_id,
                            book_id,
                            str(group_id),
                            _OCO_SUBMISSION_LEASE_SECONDS,
                        ),
                    )
                    submitted, inflight = cursor.fetchone()
                    if submitted:
                        cursor.execute(
                            """
                            select sibling_execution.rule_id
                              from execution.conditional_rule_executions sibling_execution
                              join execution.conditional_trade_rules sibling_rule
                                on sibling_rule.rule_id=sibling_execution.rule_id
                              join execution.conditional_trade_rule_versions sibling_version
                                on sibling_version.rule_id=sibling_execution.rule_id
                               and sibling_version.rule_version=sibling_execution.rule_version
                             where sibling_execution.rule_execution_id<>%s
                               and sibling_rule.user_id=%s
                               and sibling_rule.fund_id=%s
                               and sibling_rule.book_id=%s
                               and sibling_version.spec->>'oco_group_id'=%s
                               and sibling_execution.state in ('SUBMITTED','COMPLETED')
                             order by sibling_execution.updated_at desc,
                                      sibling_execution.rule_execution_id desc
                             limit 1
                            """,
                            (
                                rule_execution_id,
                                user_id,
                                fund_id,
                                book_id,
                                str(group_id),
                            ),
                        )
                        winner_row = cursor.fetchone()
                        self._supersede_oco_execution(
                            cursor,
                            rule_execution_id=rule_execution_id,
                            trigger_id=str(trigger_identity),
                            rule_id=rule_id,
                            rule_version=int(rule_version),
                            winner_rule_id=(
                                UUID(str(winner_row[0]))
                                if winner_row and winner_row[0]
                                else rule_id
                            ),
                        )
                        return False
                    if inflight:
                        # Leave this leg PENDING. It can win if the current
                        # winner later fails terminally; no external call is
                        # made while another fresh submission is in flight.
                        return False
                    self._pause_oco_siblings(
                        cursor,
                        rule_id=rule_id,
                        group_id=str(group_id),
                        user_id=user_id,
                        fund_id=fund_id,
                        book_id=book_id,
                    )

                cursor.execute(
                    """
                    update execution.conditional_rule_executions
                       set state='SUBMITTING',error_code=null,error_message=null
                     where rule_execution_id=%s
                       and (
                         state='PENDING'
                         or (
                           state='SUBMITTING'
                           and updated_at <= now() - (%s * interval '1 second')
                         )
                       )
                    """,
                    (rule_execution_id, _OCO_SUBMISSION_LEASE_SECONDS),
                )
                return cursor.rowcount == 1
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not acquire conditional submission slot",
                retryable=True,
            ) from exc

    def mark_retryable_failure(
        self, rule_execution_id: UUID, *, code: str, message: str
    ) -> None:
        self._set_execution_retry_state(rule_execution_id, "PENDING", code, message)

    def mark_terminal_failure(
        self, rule_execution_id: UUID, *, code: str, message: str
    ) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                row = self._select_submission_row(cursor, rule_execution_id)
                if row is None:
                    return
                rule_id = row[0]
                self._lock_oco_group(cursor, rule_id=rule_id)
                row = self._select_submission_row(
                    cursor, rule_execution_id, for_update=True
                )
                if row is None:
                    return
                (
                    rule_id,
                    trigger_identity,
                    rule_version,
                    state,
                    _updated_at,
                    _group_id,
                    _user_id,
                    _fund_id,
                    _book_id,
                ) = row
                if state not in {"PENDING", "SUBMITTING"}:
                    return
                cursor.execute(
                    """
                    update execution.conditional_rule_executions
                       set state='FAILED',error_code=%s,error_message=%s,
                           completed_at=now()
                     where rule_execution_id=%s
                    """,
                    (code, message[:1000], rule_execution_id),
                )
                cursor.execute(
                    """
                    update execution.conditional_rule_triggers set state='FAILED'
                     where trigger_id=%s and state='EXECUTION_PENDING'
                    """,
                    (trigger_identity,),
                )
                cursor.execute(
                    """
                    update execution.conditional_trade_rules
                       set state='FAILED',version=version+1,completed_at=now()
                     where rule_id=%s and current_version=%s
                       and state='EXECUTION_PENDING'
                    """,
                    (rule_id, rule_version),
                )
                self._release_oco_siblings(cursor, rule_id=rule_id)
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not record terminal conditional execution failure",
                retryable=True,
            ) from exc

    def _set_execution_retry_state(
        self,
        rule_execution_id: UUID,
        state: str,
        code: str | None,
        message: str | None,
    ) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    update execution.conditional_rule_executions
                       set state=%s,error_code=%s,error_message=%s
                     where rule_execution_id=%s
                       and state in ('PENDING','SUBMITTING')
                    """,
                    (
                        state,
                        code,
                        message[:1000] if message else None,
                        rule_execution_id,
                    ),
                )
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not update conditional execution retry state",
                retryable=True,
            ) from exc

    def _lock_oco_group(
        self, cursor: Any, *, rule_id: UUID
    ) -> tuple[str, UUID, UUID, UUID] | None:
        """Serialize one OCO group and return its authority tuple."""

        cursor.execute(
            """
            select version.spec->>'oco_group_id',rule.user_id,rule.fund_id,rule.book_id
              from execution.conditional_trade_rules rule
              join execution.conditional_trade_rule_versions version
                on version.rule_id=rule.rule_id
               and version.rule_version=rule.current_version
             where rule.rule_id=%s
            """,
            (rule_id,),
        )
        row = cursor.fetchone()
        if row is None or not row[0]:
            return None
        group_id, user_id, fund_id, book_id = row
        cursor.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (str(group_id),),
        )
        return str(group_id), user_id, fund_id, book_id

    def _release_oco_siblings(self, cursor: Any, *, rule_id: UUID) -> int:
        """Re-arm siblings when the reserved winner failed terminally."""

        context = self._lock_oco_group(cursor, rule_id=rule_id)
        if context is None:
            return 0
        group_id, user_id, fund_id, book_id = context
        cursor.execute(
            """
            update execution.conditional_trade_rules sibling
               set state='ACTIVE',version=sibling.version+1
              from execution.conditional_trade_rule_versions sibling_version
             where sibling_version.rule_id=sibling.rule_id
               and sibling_version.rule_version=sibling.current_version
               and sibling_version.spec->>'oco_group_id'=%s
               and sibling.rule_id<>%s
               and sibling.state='PAUSED'
               and sibling.user_id=%s
               and sibling.fund_id=%s
               and sibling.book_id=%s
               and exists (
                 select 1
                   from execution.conditional_trade_rule_events reservation
                  where reservation.rule_id=sibling.rule_id
                    and reservation.event_type='OCO_RESERVED'
                    and reservation.payload->>'reserved_by_rule_id'=%s
               )
            returning sibling.rule_id,sibling.current_version
            """,
            (group_id, rule_id, user_id, fund_id, book_id, str(rule_id)),
        )
        released = cursor.fetchall()
        for sibling_id, sibling_version in released:
            self._record_lifecycle_event(
                cursor,
                event_id=_stable_id("oco_", sibling_id, rule_id, "released"),
                rule_id=sibling_id,
                rule_version=int(sibling_version),
                event_type="OCO_RELEASED",
                from_state="PAUSED",
                to_state="ACTIVE",
                payload={
                    "released_by_rule_id": str(rule_id),
                    "oco_group_id": group_id,
                },
            )
        return len(released)

    def _cancel_oco_siblings(self, cursor: Any, *, rule_id: UUID) -> int:
        """Retire the alternatives once this rule's order actually went out.

        A take-profit and a stop-loss on one position are two ways for the same
        exit to happen.  Leaving the loser armed sells the position a second
        time, and on a one-share position the second leg simply fails - either
        way the book stops matching what the user asked for.

        Runs inside the submitting transaction so the cancel cannot be lost if
        the worker dies right after the broker accepted the order.  ACTIVE and
        PAUSED siblings are retired; one already past its own trigger is in
        flight at the broker and is not ours to revoke.
        """

        context = self._lock_oco_group(cursor, rule_id=rule_id)
        if context is None:
            return 0
        group_id, user_id, fund_id, book_id = context

        cursor.execute(
            """
            with candidates as materialized (
                select sibling.rule_id,sibling.state as previous_state
                  from execution.conditional_trade_rules sibling
                  join execution.conditional_trade_rule_versions sibling_version
                    on sibling_version.rule_id=sibling.rule_id
                   and sibling_version.rule_version=sibling.current_version
                 where sibling_version.spec->>'oco_group_id'=%s
                   and sibling.rule_id<>%s
                   and sibling.state in ('ACTIVE','PAUSED')
                   and sibling.user_id=%s
                   and sibling.fund_id=%s
                   and sibling.book_id=%s
            )
            update execution.conditional_trade_rules sibling
               set state='CANCELLED',version=sibling.version+1,completed_at=now()
              from candidates
             where sibling.rule_id=candidates.rule_id
            returning sibling.rule_id,sibling.current_version,candidates.previous_state
            """,
            (group_id, rule_id, user_id, fund_id, book_id),
        )
        cancelled = cursor.fetchall()
        for sibling_id, sibling_version, previous_state in cancelled:
            self._record_lifecycle_event(
                cursor,
                event_id=_stable_id("oco_", sibling_id, rule_id, "cancelled"),
                rule_id=sibling_id,
                rule_version=int(sibling_version),
                event_type="OCO_CANCELLED",
                from_state=str(previous_state),
                to_state="CANCELLED",
                payload={
                    "cancelled_by_rule_id": str(rule_id),
                    "oco_group_id": group_id,
                },
            )
        return len(cancelled)

    def mark_submitted(self, rule_execution_id: UUID, *, directive_id: UUID) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select rule_id
                      from execution.conditional_rule_executions
                     where rule_execution_id=%s
                    """,
                    (rule_execution_id,),
                )
                identity_row = cursor.fetchone()
                if identity_row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_EXECUTION_MISSING",
                        "conditional execution disappeared",
                    )
                # All OCO transitions take the group lock before locking an
                # execution row, otherwise two legs can deadlock while each
                # holds its own row and waits for the other leg's group lock.
                self._lock_oco_group(cursor, rule_id=identity_row[0])
                cursor.execute(
                    """
                    select execution.trigger_id,execution.rule_id,
                           execution.rule_version,execution.state,
                           rule.user_id,rule.fund_id,rule.book_id,
                           coalesce(request.client_request_id,
                                    rule.client_request_id),
                           request.order_request_id,
                           request.ceo_root_task_id,request.trading_task_id
                      from execution.conditional_rule_executions execution
                      join execution.conditional_trade_rules rule
                        on rule.rule_id=execution.rule_id
                      left join lateral (
                        select admitted.order_request_id,
                               admitted.client_request_id,
                               admitted.ceo_root_task_id,
                               admitted.trading_task_id
                          from execution.user_order_requests admitted
                         where admitted.user_id=rule.user_id
                           and (
                             admitted.client_request_id=rule.client_request_id
                             or admitted.canonical_payload->>'rule_id'
                                  = rule.rule_id::text
                             or coalesce(
                                  admitted.canonical_payload->'rule_ids',
                                  '[]'::jsonb
                                ) ? rule.rule_id::text
                           )
                         order by
                           (admitted.client_request_id=rule.client_request_id) desc,
                           admitted.updated_at desc
                         limit 1
                      ) request on true
                     where execution.rule_execution_id=%s
                     for update of execution
                    """,
                    (rule_execution_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_EXECUTION_MISSING",
                        "conditional execution disappeared",
                    )
                (
                    trigger_identity,
                    rule_id,
                    rule_version,
                    state,
                    user_id,
                    fund_id,
                    book_id,
                    client_request_id,
                    order_request_id,
                    ceo_root_task_id,
                    trading_task_id,
                ) = row
                if state == "SUBMITTED":
                    return
                if state not in {"PENDING", "SUBMITTING"}:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_EXECUTION_STATE_INVALID",
                        "conditional execution is not submit-ready",
                    )
                cursor.execute(
                    """
                    update execution.conditional_rule_executions
                       set state='SUBMITTED',directive_id=%s,error_code=null,
                           error_message=null,completed_at=now()
                     where rule_execution_id=%s
                    """,
                    (directive_id, rule_execution_id),
                )
                cursor.execute(
                    """
                    update execution.conditional_rule_triggers
                       set state='SUBMITTED'
                     where trigger_id=%s and state='EXECUTION_PENDING'
                    """,
                    (trigger_identity,),
                )
                cursor.execute(
                    """
                    update execution.conditional_trade_rules
                       set state='COMPLETED',version=version+1,completed_at=now()
                     where rule_id=%s and current_version=%s
                       and state='EXECUTION_PENDING'
                    """,
                    (rule_id, rule_version),
                )
                if cursor.rowcount != 1:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_CONCURRENT_TRANSITION",
                        "conditional rule changed before submission was recorded",
                    )
                self._cancel_oco_siblings(cursor, rule_id=rule_id)
                payload = {
                    "rule_execution_id": str(rule_execution_id),
                    "directive_id": str(directive_id),
                    "user_id": str(user_id),
                    "fund_id": str(fund_id),
                    "book_id": str(book_id),
                    "client_request_id": str(client_request_id),
                    "order_request_id": (
                        str(order_request_id) if order_request_id else None
                    ),
                    "ceo_root_task_id": (
                        str(ceo_root_task_id) if ceo_root_task_id else None
                    ),
                    "trading_task_id": (
                        str(trading_task_id) if trading_task_id else None
                    ),
                }
                cursor.execute(
                    """
                    insert into execution.conditional_trade_rule_events (
                      event_id,rule_id,rule_version,event_type,from_state,to_state,payload
                    ) values (%s,%s,%s,'DIRECTIVE_SUBMITTED',
                              'EXECUTION_PENDING','COMPLETED',%s)
                    on conflict (event_id) do nothing
                    """,
                    (
                        _stable_id("cre_", rule_execution_id, "submitted"),
                        rule_id,
                        rule_version,
                        Json(payload),
                    ),
                )
                cursor.execute(
                    """
                    insert into execution.conditional_rule_outbox (
                      event_id,aggregate_id,event_type,payload
                    ) values (%s,%s,'DIRECTIVE_SUBMITTED',%s)
                    on conflict (event_id) do nothing
                    """,
                    (
                        _stable_id("cro_", rule_execution_id, "submitted"),
                        str(rule_id),
                        Json(payload),
                    ),
                )
        except RuleWorkerStoreError:
            raise
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not record conditional directive submission",
                retryable=True,
            ) from exc

    def _claim_outbox_rows(
        self, *, limit: int
    ) -> tuple[str, list[ConditionalRuleOutboxRow]]:
        """Claim rows in a short transaction, returning a durable lease token."""

        claim_token = str(uuid4())
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    with claimable as (
                        select outbox.event_id
                          from execution.conditional_rule_outbox outbox
                         where outbox.published_at is null
                           and (
                             outbox.claim_token is null
                             or outbox.claim_expires_at <= now()
                           )
                         order by outbox.created_at,outbox.event_id
                         limit %s
                           for update skip locked
                    )
                    update execution.conditional_rule_outbox outbox
                       set claim_token=%s,
                           claim_expires_at=now() + (%s * interval '1 second')
                      from claimable
                     where outbox.event_id=claimable.event_id
                       and outbox.published_at is null
                    returning outbox.event_id,outbox.aggregate_id,outbox.event_type,
                              outbox.payload,outbox.created_at,outbox.attempts
                    """,
                    (
                        max(1, min(int(limit), 1000)),
                        claim_token,
                        _OUTBOX_CLAIM_LEASE_SECONDS,
                    ),
                )
                rows = [
                    ConditionalRuleOutboxRow(
                        event_id=str(row[0]),
                        aggregate_id=str(row[1]),
                        event_type=str(row[2]),
                        payload=dict(row[3] or {}),
                        created_at=row[4],
                        attempts=int(row[5]),
                    )
                    for row in cursor.fetchall()
                ]
                rows.sort(key=lambda row: (row.created_at, row.event_id))
                return claim_token, rows
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_OUTBOX_UNAVAILABLE",
                "could not claim conditional rule outbox",
                retryable=True,
            ) from exc

    def _finalize_outbox_claim(
        self,
        row: ConditionalRuleOutboxRow,
        *,
        claim_token: str,
        error: str | None,
    ) -> bool:
        """Mark one claimed row and release its lease in a short transaction."""

        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                if error is None:
                    cursor.execute(
                        """
                        update execution.conditional_rule_outbox
                           set published_at=now(),attempts=attempts+1,
                               last_error=null,claim_token=null,
                               claim_expires_at=null
                         where event_id=%s and published_at is null
                           and claim_token=%s
                        """,
                        (row.event_id, claim_token),
                    )
                else:
                    cursor.execute(
                        """
                        update execution.conditional_rule_outbox
                           set attempts=attempts+1,last_error=%s,
                               claim_token=null,claim_expires_at=null
                         where event_id=%s and published_at is null
                           and claim_token=%s
                        """,
                        (error[:2000], row.event_id, claim_token),
                    )
                return cursor.rowcount == 1
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_OUTBOX_UNAVAILABLE",
                "could not finalize conditional rule outbox claim",
                retryable=True,
            ) from exc

    def drain_outbox(
        self,
        publisher: Callable[[ConditionalRuleOutboxRow], None],
        *,
        limit: int = 100,
    ) -> dict[str, int]:
        """Publish conditional-rule events with short DB leases.

        Claim and finalization transactions never contain the external publish
        call. A crash after Redis accepts an event but before finalization can
        produce a duplicate after the lease expires, so consumers must dedupe
        by ``event_id``. Marking an event as published before the external write
        would lose events.
        """

        counts = {"picked": 0, "published": 0, "failed": 0, "lost": 0}
        claim_token, rows = self._claim_outbox_rows(limit=limit)
        counts["picked"] = len(rows)
        for row in rows:
            try:
                publisher(row)
            except Exception as exc:  # noqa: BLE001 - preserve retryable row
                finalized = self._finalize_outbox_claim(
                    row, claim_token=claim_token, error=str(exc)
                )
                counts["failed" if finalized else "lost"] += 1
            else:
                finalized = self._finalize_outbox_claim(
                    row, claim_token=claim_token, error=None
                )
                counts["published" if finalized else "lost"] += 1
        return counts


__all__ = [
    "ActiveRule",
    "ConditionalNotificationContext",
    "ConditionalRuleOutboxRow",
    "EntryPositionMismatchNotificationContext",
    "PostgresRuleWorkerStore",
    "RuleWorkerStoreError",
    "SubmitReadyExecution",
    "TriggerClaim",
]
