"""Load one durable trigger execution and derive its directive server-side."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import psycopg2
from psycopg2.extras import register_uuid

from directives.auth import DirectiveProof, EXECUTE_SCOPE
from directives.contracts import DirectiveAction, UserDirectiveRequest
from orchestration.conditional_rules import (
    ConditionalRuleSpec,
    execution_idempotency_key,
    rule_fingerprint,
)


register_uuid()


class ConditionalRuleAdmissionError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class ConditionalRuleAdmission:
    rule_execution_id: UUID
    spec: ConditionalRuleSpec
    request: UserDirectiveRequest
    proof: DirectiveProof


def _fresh_proof_jti(execution_id: object) -> str:
    """Mint a one-use proof identity for each HTTP submission attempt.

    The durable execution idempotency key, not the proof JTI, identifies the
    order. Reusing a deterministic JTI after a response timeout makes the
    directive repository correctly reject the retry as a replay, hiding the
    already-created authoritative directive from the caller.
    """

    return f"conditional-rule:{execution_id}:{uuid4()}"


def _conditional_order_payload(
    spec: ConditionalRuleSpec, *, quantity: Decimal
) -> dict[str, Any]:
    """Derive the trusted PAPER directive payload from the confirmed spec."""

    return {
        "instrument_id": str(spec.instrument_id),
        "symbol": spec.symbol,
        "side": spec.action.side.value,
        "quantity": str(quantity),
        "order_type": spec.action.order_type,
        "limit_price": (
            str(spec.action.limit_price)
            if spec.action.limit_price is not None
            else None
        ),
        "time_in_force": spec.action.time_in_force,
    }



def _assert_recent_evaluation(
    spec: ConditionalRuleSpec,
    evaluated_at: datetime,
    *,
    now: datetime,
) -> None:
    if evaluated_at.tzinfo is None:
        raise ConditionalRuleAdmissionError(
            "TRADING_CONDITIONAL_RULE_EVALUATION_TIME_INVALID",
            "conditional evaluation timestamp is not timezone-aware",
            500,
        )
    age = (
        now.astimezone(timezone.utc)
        - evaluated_at.astimezone(timezone.utc)
    ).total_seconds()
    if age < -5 or age > spec.evaluation.max_data_age_seconds:
        raise ConditionalRuleAdmissionError(
            "TRADING_CONDITIONAL_RULE_EVALUATION_STALE",
            "conditional evaluation is outside the confirmed freshness window",
            409,
        )

class PostgresConditionalRuleAdmissionRepository:
    def __init__(self, dsn: str, *, role: str = "svc_trading_api") -> None:
        if not dsn.strip():
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_DB_UNAVAILABLE",
                "conditional rule database is not configured",
                503,
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_DB_ROLE_INVALID",
                "conditional rule database role is invalid",
                503,
            )
        self.dsn = dsn
        self.role = role

    def load(
        self,
        rule_execution_id: UUID,
        *,
        now: datetime | None = None,
    ) -> ConditionalRuleAdmission:
        current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        try:
            with psycopg2.connect(self.dsn, connect_timeout=5) as connection:
                with connection.cursor() as cursor:
                    cursor.execute(f'SET LOCAL ROLE "{self.role}"')
                    cursor.execute(
                        """
                        select execution.rule_execution_id,execution.state,
                               execution.side,execution.quantity,
                               execution.idempotency_key,execution.rule_version,
                               trigger.trigger_id,trigger.state,
                               rule.rule_id,rule.user_id,rule.fund_id,rule.book_id,
                               rule.state,rule.current_version,rule.execution_mode,
                               rule.confirmation_sha256,
                               version.spec,version.spec_sha256,evaluation.created_at
                          from execution.conditional_rule_executions execution
                          join execution.conditional_rule_triggers trigger
                            on trigger.trigger_id=execution.trigger_id
                          join execution.conditional_rule_evaluations evaluation
                            on evaluation.evaluation_id=trigger.evaluation_id
                          join execution.conditional_trade_rules rule
                            on rule.rule_id=execution.rule_id
                          join execution.conditional_trade_rule_versions version
                            on version.rule_id=rule.rule_id
                           and version.rule_version=execution.rule_version
                         where execution.rule_execution_id=%s
                        """,
                        (rule_execution_id,),
                    )
                    row = cursor.fetchone()
        except ConditionalRuleAdmissionError:
            raise
        except psycopg2.Error as exc:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_DB_UNAVAILABLE",
                "conditional rule execution could not be loaded",
                503,
            ) from exc
        if row is None:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_EXECUTION_NOT_FOUND",
                "conditional rule execution was not found",
                404,
            )

        (
            execution_id,
            execution_state,
            side,
            quantity,
            idempotency_key,
            execution_rule_version,
            trigger_identity,
            trigger_state,
            rule_id,
            user_id,
            fund_id,
            book_id,
            rule_state,
            current_version,
            execution_mode,
            confirmation_sha256,
            raw_spec,
            spec_sha256,
            evaluation_created_at,
        ) = row
        if execution_state not in {"PENDING", "SUBMITTING", "SUBMITTED"}:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_EXECUTION_STATE_DENIED",
                "conditional rule execution is not submit-ready",
                409,
            )
        if trigger_state not in {"EXECUTION_PENDING", "SUBMITTED"}:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_TRIGGER_STATE_DENIED",
                "conditional rule trigger is not submit-ready",
                409,
            )
        if rule_state not in {"TRIGGERED", "EXECUTION_PENDING"}:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_STATE_DENIED",
                "conditional rule is not in an executable lifecycle state",
                409,
            )
        if execution_mode != "PAPER":
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_LIVE_FORBIDDEN",
                "conditional rules are PAPER-only",
                403,
            )
        if int(execution_rule_version) != int(current_version):
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_VERSION_CHANGED",
                "conditional rule changed after this trigger was evaluated",
                409,
            )
        if not confirmation_sha256 or str(confirmation_sha256) != str(spec_sha256):
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_CONFIRMATION_MISMATCH",
                "active rule does not match the exact user-confirmed fingerprint",
                409,
            )
        spec_payload = json.loads(raw_spec) if isinstance(raw_spec, str) else raw_spec
        try:
            spec = ConditionalRuleSpec.model_validate(spec_payload)
        except Exception as exc:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_SPEC_INVALID",
                "stored conditional rule spec is invalid",
                500,
            ) from exc
        if rule_fingerprint(spec) != str(spec_sha256):
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_FINGERPRINT_INVALID",
                "stored conditional rule payload does not match its fingerprint",
                500,
            )
        _assert_recent_evaluation(spec, evaluation_created_at, now=current)
        expected_scope = (
            str(spec.authority.user_id),
            str(spec.authority.fund_id),
            str(spec.authority.book_id),
        )
        if expected_scope != (str(user_id), str(fund_id), str(book_id)):
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_AUTHORITY_MISMATCH",
                "stored conditional rule authority does not match its row",
                500,
            )
        if current >= spec.expires_at:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_EXPIRED",
                "conditional rule expired before Trading admission",
                409,
            )
        if spec.action.side.value != side or quantity is None or Decimal(quantity) <= 0:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_ACTION_MISMATCH",
                "stored conditional action does not match its execution",
                500,
            )
        expected_key = execution_idempotency_key(
            str(rule_id), int(current_version), str(trigger_identity)
        )
        if str(idempotency_key) != expected_key:
            raise ConditionalRuleAdmissionError(
                "TRADING_CONDITIONAL_RULE_IDEMPOTENCY_MISMATCH",
                "conditional rule idempotency key is invalid",
                500,
            )

        request = UserDirectiveRequest.model_validate(
            {
                "fund_id": fund_id,
                "book_id": book_id,
                "action": DirectiveAction.PLACE_ORDER,
                "instruction_ref": f"conditional:{rule_id}:v{current_version}",
                "idempotency_key": idempotency_key,
                "payload": _conditional_order_payload(spec, quantity=Decimal(quantity)),
            }
        )
        issued = current.timestamp()
        proof = DirectiveProof(
            subject=UUID(str(user_id)),
            fund_id=UUID(str(fund_id)),
            book_id=UUID(str(book_id)),
            action=DirectiveAction.PLACE_ORDER,
            instruction_ref=request.instruction_ref,
            idempotency_key=request.idempotency_key,
            payload_sha256=request.payload_sha256(),
            jti=_fresh_proof_jti(execution_id),
            issued_at=issued,
            not_before=issued,
            expires_at=issued + 60,
            scope=frozenset({EXECUTE_SCOPE}),
        )
        return ConditionalRuleAdmission(
            rule_execution_id=UUID(str(execution_id)),
            spec=spec,
            request=request,
            proof=proof,
        )


def admission_repository_from_env() -> PostgresConditionalRuleAdmissionRepository:
    dsn = os.environ.get("PAPER_DATABASE_URL") or os.environ.get("DATABASE_URL") or ""
    return PostgresConditionalRuleAdmissionRepository(
        dsn,
        role=os.environ.get("TRADING_DATABASE_ROLE", "svc_trading_api").strip(),
    )


__all__ = [
    "ConditionalRuleAdmission",
    "ConditionalRuleAdmissionError",
    "PostgresConditionalRuleAdmissionRepository",
    "admission_repository_from_env",
]
