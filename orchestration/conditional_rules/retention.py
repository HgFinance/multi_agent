"""Bounded retention for conditional PAPER rule audit detail.

The retention lane is deliberately separate from the hot evaluator.  It only
removes terminal-rule detail that is no longer needed for idempotency or order
reconciliation.  The parent rule row is retained as a compact idempotency
tombstone, and any execution linked to a PAPER directive is always preserved.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import psycopg2
from psycopg2 import sql

TERMINAL_STATES = ("COMPLETED", "EXPIRED", "CANCELLED", "FAILED")


@dataclass(frozen=True)
class ConditionalRuleRetentionRun:
    enabled: bool
    available: bool
    outbox_deleted: int = 0
    executions_deleted: int = 0
    triggers_deleted: int = 0
    evaluations_deleted: int = 0
    events_deleted: int = 0
    error_code: str | None = None

    @property
    def deleted_total(self) -> int:
        return sum(
            (
                self.outbox_deleted,
                self.executions_deleted,
                self.triggers_deleted,
                self.evaluations_deleted,
                self.events_deleted,
            )
        )


def _env_int(name: str, default: int, *, minimum: int = 1, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class ConditionalRuleRetentionStore:
    """Run one small, transactional cleanup pass against the control DB."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        enabled: bool | None = None,
        batch_size: int | None = None,
        evaluation_retention_days: int | None = None,
        detail_retention_days: int | None = None,
        outbox_retention_days: int | None = None,
        role: str = "svc_conditional_rule_worker",
        connect_timeout: int = 8,
        statement_timeout_ms: int = 1500,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.dsn = (dsn or "").strip()
        self.enabled = (
            _env_bool("CONDITIONAL_RULE_RETENTION_ENABLED", True)
            if enabled is None
            else bool(enabled)
        )
        self.batch_size = batch_size or _env_int(
            "CONDITIONAL_RULE_RETENTION_BATCH_SIZE", 500, minimum=1, maximum=5000
        )
        self.evaluation_retention_days = evaluation_retention_days or _env_int(
            "CONDITIONAL_RULE_EVALUATION_RETENTION_DAYS", 30, minimum=1, maximum=3650
        )
        self.detail_retention_days = detail_retention_days or _env_int(
            "CONDITIONAL_RULE_DETAIL_RETENTION_DAYS", 90, minimum=1, maximum=3650
        )
        self.outbox_retention_days = outbox_retention_days or _env_int(
            "CONDITIONAL_RULE_OUTBOX_RETENTION_DAYS", 7, minimum=1, maximum=3650
        )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", role):
            raise ValueError("conditional rule retention database role is invalid")
        self.role = role
        self.connect_timeout = max(1, int(connect_timeout))
        self.statement_timeout_ms = max(100, min(int(statement_timeout_ms), 10000))
        self.connect_factory = connect_factory

    @classmethod
    def from_env(cls) -> ConditionalRuleRetentionStore:
        dsn = (
            os.getenv("CONDITIONAL_RULE_DATABASE_URL", "").strip()
            or os.getenv("DATABASE_URL", "").strip()
        )
        return cls(
            dsn,
            role=os.getenv(
                "CONDITIONAL_RULE_WORKER_DATABASE_ROLE",
                "svc_conditional_rule_worker",
            ).strip(),
            statement_timeout_ms=_env_int(
                "CONDITIONAL_RULE_RETENTION_STATEMENT_TIMEOUT_MS", 1500, minimum=100, maximum=10000
            ),
        )

    def _connect(self) -> Any:
        if self.connect_factory is not None:
            return self.connect_factory(self.dsn, connect_timeout=self.connect_timeout)
        return psycopg2.connect(self.dsn, connect_timeout=self.connect_timeout)

    def check_ready(self) -> None:
        """Perform a read-only readiness check without running cleanup."""

        if not self.enabled:
            raise RuntimeError("conditional rule retention is disabled")
        if not self.dsn:
            raise RuntimeError("conditional rule retention database is not configured")
        connection = None
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute(
                    "set local statement_timeout = %s",
                    (f"{self.statement_timeout_ms}ms",),
                )
                cursor.execute(
                    "select to_regclass('execution.conditional_trade_rules')"
                )
                row = cursor.fetchone()
                if not row or str(row[0]) not in {
                    "execution.conditional_trade_rules",
                    "conditional_trade_rules",
                }:
                    raise RuntimeError("conditional rule tables are not available")
        finally:
            if connection is not None:
                self._close(connection)

    @staticmethod
    def _close(connection: Any) -> None:
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    def _set_role(self, cursor: Any) -> None:
        cursor.execute(
            sql.SQL("set local role {}").format(
                sql.Identifier(self.role)
            )
        )

    def _terminal_rule_cte(self, *, retention_days: int) -> tuple[str, tuple[Any, ...]]:
        states = ",".join("%s" for _ in TERMINAL_STATES)
        return (
            f"""WITH eligible_rules AS (
                SELECT rule.rule_id
                  FROM execution.conditional_trade_rules rule
                 WHERE rule.state IN ({states})
                   AND rule.execution_mode='PAPER'
                   AND rule.completed_at IS NOT NULL
                   AND rule.completed_at < now() - (%s * interval '1 day')
                   AND NOT EXISTS (
                         SELECT 1
                           FROM execution.conditional_rule_executions protected
                          WHERE protected.rule_id=rule.rule_id
                            AND protected.directive_id IS NOT NULL
                   )
            ) """,
            (*TERMINAL_STATES, retention_days),
        )

    @staticmethod
    def _rowcount(cursor: Any) -> int:
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def _delete_published_outbox(self, cursor: Any) -> int:
        cursor.execute(
            """
            WITH doomed AS (
                SELECT event_id
                  FROM execution.conditional_rule_outbox
                 WHERE published_at IS NOT NULL
                   AND published_at < now() - (%s * interval '1 day')
                 ORDER BY published_at, event_id
                 LIMIT %s
            )
            DELETE FROM execution.conditional_rule_outbox
             USING doomed
             WHERE execution.conditional_rule_outbox.event_id=doomed.event_id
            """,
            (self.outbox_retention_days, self.batch_size),
        )
        return self._rowcount(cursor)

    def _delete_executions(self, cursor: Any) -> int:
        cte, args = self._terminal_rule_cte(retention_days=self.detail_retention_days)
        cursor.execute(
            cte
            + """
            , doomed AS (
                SELECT execution.rule_execution_id
                  FROM execution.conditional_rule_executions execution
                  JOIN eligible_rules eligible ON eligible.rule_id=execution.rule_id
                 WHERE execution.directive_id IS NULL
                   AND execution.created_at < now() - (%s * interval '1 day')
                 ORDER BY execution.created_at, execution.rule_execution_id
                 LIMIT %s
            )
            DELETE FROM execution.conditional_rule_executions execution
             USING doomed
             WHERE execution.rule_execution_id=doomed.rule_execution_id
            """,
            (*args, self.detail_retention_days, self.batch_size),
        )
        return self._rowcount(cursor)

    def _delete_triggers(self, cursor: Any) -> int:
        cte, args = self._terminal_rule_cte(retention_days=self.detail_retention_days)
        cursor.execute(
            cte
            + """
            , doomed AS (
                SELECT trigger.trigger_id
                  FROM execution.conditional_rule_triggers trigger
                  JOIN eligible_rules eligible ON eligible.rule_id=trigger.rule_id
                 WHERE trigger.created_at < now() - (%s * interval '1 day')
                 ORDER BY trigger.created_at, trigger.trigger_id
                 LIMIT %s
            )
            DELETE FROM execution.conditional_rule_triggers trigger
             USING doomed
             WHERE trigger.trigger_id=doomed.trigger_id
            """,
            (*args, self.detail_retention_days, self.batch_size),
        )
        return self._rowcount(cursor)

    def _delete_evaluations(self, cursor: Any) -> int:
        cte, args = self._terminal_rule_cte(retention_days=self.evaluation_retention_days)
        cursor.execute(
            cte
            + """
            , doomed AS (
                SELECT evaluation.evaluation_id
                  FROM execution.conditional_rule_evaluations evaluation
                  JOIN eligible_rules eligible ON eligible.rule_id=evaluation.rule_id
                 WHERE evaluation.created_at < now() - (%s * interval '1 day')
                   AND NOT EXISTS (
                         SELECT 1
                           FROM execution.conditional_rule_triggers trigger
                          WHERE trigger.evaluation_id=evaluation.evaluation_id
                   )
                 ORDER BY evaluation.created_at, evaluation.evaluation_id
                 LIMIT %s
            )
            DELETE FROM execution.conditional_rule_evaluations evaluation
             USING doomed
             WHERE evaluation.evaluation_id=doomed.evaluation_id
            """,
            (*args, self.evaluation_retention_days, self.batch_size),
        )
        return self._rowcount(cursor)

    def _delete_events(self, cursor: Any) -> int:
        cte, args = self._terminal_rule_cte(retention_days=self.detail_retention_days)
        cursor.execute(
            cte
            + """
            , doomed AS (
                SELECT event.event_id
                  FROM execution.conditional_trade_rule_events event
                  JOIN eligible_rules eligible ON eligible.rule_id=event.rule_id
                 WHERE event.created_at < now() - (%s * interval '1 day')
                 ORDER BY event.created_at, event.event_id
                 LIMIT %s
            )
            DELETE FROM execution.conditional_trade_rule_events event
             USING doomed
             WHERE event.event_id=doomed.event_id
            """,
            (*args, self.detail_retention_days, self.batch_size),
        )
        return self._rowcount(cursor)

    def run_once(self) -> ConditionalRuleRetentionRun:
        if not self.enabled:
            return ConditionalRuleRetentionRun(enabled=False, available=False, error_code="DISABLED")
        if not self.dsn:
            return ConditionalRuleRetentionRun(
                enabled=True,
                available=False,
                error_code="CONDITIONAL_RULE_RETENTION_DATABASE_REQUIRED",
            )

        connection = None
        try:
            connection = self._connect()
            with connection.cursor() as cursor:
                self._set_role(cursor)
                cursor.execute("set local statement_timeout = %s", (f"{self.statement_timeout_ms}ms",))
                outbox_deleted = self._delete_published_outbox(cursor)
                executions_deleted = self._delete_executions(cursor)
                triggers_deleted = self._delete_triggers(cursor)
                evaluations_deleted = self._delete_evaluations(cursor)
                events_deleted = self._delete_events(cursor)
            connection.commit()
            return ConditionalRuleRetentionRun(
                enabled=True,
                available=True,
                outbox_deleted=outbox_deleted,
                executions_deleted=executions_deleted,
                triggers_deleted=triggers_deleted,
                evaluations_deleted=evaluations_deleted,
                events_deleted=events_deleted,
            )
        except Exception:  # noqa: BLE001 - maintenance must fail open on DB errors
            if connection is not None:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
            return ConditionalRuleRetentionRun(
                enabled=True,
                available=False,
                error_code="CONDITIONAL_RULE_RETENTION_UNAVAILABLE",
            )
        finally:
            if connection is not None:
                self._close(connection)


__all__ = ["ConditionalRuleRetentionRun", "ConditionalRuleRetentionStore"]
