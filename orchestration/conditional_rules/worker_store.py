"""Durable exactly-once state transitions for conditional PAPER rules."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json, register_uuid

from .contracts import ConditionalRuleSpec, rule_fingerprint
from .identities import evaluation_id, execution_idempotency_key, trigger_id
from .semantic import validate_rule_spec
from .contracts import expression_fingerprint


register_uuid()


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
class SubmitReadyExecution:
    rule_execution_id: UUID
    trigger_id: str
    rule_id: UUID
    rule_version: int
    idempotency_key: str


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
                           version.spec_sha256
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
                for bundle_id, rule_id, request_state, rule_state, rule_version, spec_sha in cursor.fetchall():
                    if request_state == "COMPLETED" and str(rule_state) == "PENDING_CONFIRMATION":
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
                        cursor.execute(
                            """
                            insert into execution.conditional_trade_rule_events (
                              event_id,rule_id,rule_version,event_type,from_state,
                              to_state,payload
                            ) values (%s,%s,%s,'BUNDLE_ACTIVATED',
                                      'PENDING_CONFIRMATION','ACTIVE',%s)
                            on conflict (event_id) do nothing
                            """,
                            (
                                _stable_id("dep_", bundle_id, "ACTIVATED"),
                                rule_id,
                                rule_version,
                                Json({"bundle_id": str(bundle_id)}),
                            ),
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
                    update execution.conditional_trade_rules
                       set state='EXPIRED',version=version+1,completed_at=now()
                     where state in ('PENDING_CONFIRMATION','ACTIVE','PAUSED')
                       and expires_at<=now()
                    """
                )
                return int(cursor.rowcount)
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
                cursor.execute(
                    """
                    insert into execution.conditional_trade_rule_events (
                      event_id,rule_id,rule_version,event_type,from_state,to_state,payload
                    ) values (%s,%s,%s,'TRIGGER_CLAIMED','ACTIVE','TRIGGERED',%s)
                    on conflict (event_id) do nothing
                    """,
                    (
                        _stable_id("cre_", trigger_identity, "claimed"),
                        rule.rule_id,
                        rule.rule_version,
                        Json({"trigger_id": trigger_identity}),
                    ),
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

    def mark_submitting(self, rule_execution_id: UUID) -> None:
        self._set_execution_retry_state(rule_execution_id, "SUBMITTING", None, None)

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
                cursor.execute(
                    """
                    select trigger_id,rule_id,rule_version
                      from execution.conditional_rule_executions
                     where rule_execution_id=%s
                       and state in ('PENDING','SUBMITTING')
                     for update
                    """,
                    (rule_execution_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    return
                trigger_identity, rule_id, rule_version = row
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
                    (state, code, message[:1000] if message else None, rule_execution_id),
                )
        except psycopg2.Error as exc:
            raise RuleWorkerStoreError(
                "CONDITIONAL_RULE_DATABASE_UNAVAILABLE",
                "could not update conditional execution retry state",
                retryable=True,
            ) from exc

    def mark_submitted(self, rule_execution_id: UUID, *, directive_id: UUID) -> None:
        try:
            with self._connect() as connection, connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    """
                    select execution.trigger_id,execution.rule_id,
                           execution.rule_version,execution.state
                      from execution.conditional_rule_executions execution
                     where execution.rule_execution_id=%s
                     for update
                    """,
                    (rule_execution_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuleWorkerStoreError(
                        "CONDITIONAL_RULE_EXECUTION_MISSING",
                        "conditional execution disappeared",
                    )
                trigger_identity, rule_id, rule_version, state = row
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
                payload = {
                    "rule_execution_id": str(rule_execution_id),
                    "directive_id": str(directive_id),
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


__all__ = [
    "ActiveRule",
    "PostgresRuleWorkerStore",
    "RuleWorkerStoreError",
    "SubmitReadyExecution",
    "TriggerClaim",
]
