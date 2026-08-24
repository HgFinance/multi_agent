"""Bounded maintenance worker for the MemoHarness D5 Experience Bank.

This worker is intentionally separate from request handling and Kanban
retention.  It only touches ``experience.workflow_experiences`` and fails
open when the optional D5 database is unavailable.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import time
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from orchestration.experience_bank import TABLE_NAME
from orchestration.experience_retention_policy import (
    D5_CLEANUP_RELATION_BYTES,
    D5_FAILURE_RETENTION_DAYS,
    D5_OPERATIONAL_RETENTION_DAYS,
    D5_PRESERVE_LATEST_PER_GROUP,
    D5_PRESERVE_RECENT_DAYS,
    D5_SUCCESS_RETENTION_DAYS,
    OPERATIONAL_FAILURE_CODES,
    capacity_band,
)


LOG = logging.getLogger(__name__)


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


@dataclass(frozen=True)
class RetentionRun:
    enabled: bool
    available: bool
    relation_size_before: int
    relation_size_after: int
    capacity: str
    expired_deleted: int = 0
    pressure_deleted: int = 0
    vacuum_analyzed: bool = False
    error_code: str | None = None


class ExperienceRetentionWorker:
    """Run one bounded D5 cleanup pass."""

    def __init__(
        self,
        dsn: str | None = None,
        *,
        enabled: bool | None = None,
        batch_size: int | None = None,
        max_pressure_batches: int | None = None,
        connect_timeout: int = 8,
        statement_timeout_ms: int = 1500,
        vacuum_analyze: bool | None = None,
        connect_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.dsn = (dsn or "").strip()
        self.enabled = _env_bool("MEMOHARNESS_D5_RETENTION_ENABLED", True) if enabled is None else bool(enabled)
        self.batch_size = batch_size or _env_int(
            "MEMOHARNESS_D5_RETENTION_BATCH_SIZE", 500, minimum=1, maximum=5000
        )
        self.max_pressure_batches = max_pressure_batches or _env_int(
            "MEMOHARNESS_D5_RETENTION_MAX_PRESSURE_BATCHES", 10, minimum=1, maximum=100
        )
        self.connect_timeout = max(1, int(connect_timeout))
        self.statement_timeout_ms = max(100, min(int(statement_timeout_ms), 10000))
        self.vacuum_analyze = (
            _env_bool("MEMOHARNESS_D5_RETENTION_VACUUM_ANALYZE", True)
            if vacuum_analyze is None
            else bool(vacuum_analyze)
        )
        self.connect_factory = connect_factory

    @classmethod
    def from_env(cls) -> "ExperienceRetentionWorker":
        dsn = (
            os.getenv("MEMOHARNESS_D5_DATABASE_URL", "").strip()
            or os.getenv("MEMOHARNESS_EXPERIENCE_DATABASE_URL", "").strip()
            or os.getenv("CONTROL_DATABASE_URL", "").strip()
            or os.getenv("DATABASE_URL", "").strip()
        )
        try:
            statement_timeout_ms = int(os.getenv("MEMOHARNESS_D5_STATEMENT_TIMEOUT_MS", "1500"))
        except (TypeError, ValueError):
            statement_timeout_ms = 1500
        return cls(dsn, statement_timeout_ms=statement_timeout_ms)

    def _connect(self) -> Any:
        if self.connect_factory is not None:
            return self.connect_factory(self.dsn, connect_timeout=self.connect_timeout)
        import psycopg2

        return psycopg2.connect(self.dsn, connect_timeout=self.connect_timeout)

    @staticmethod
    def _close(connection: Any) -> None:
        close = getattr(connection, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _size(cursor: Any) -> int:
        cursor.execute("SELECT pg_total_relation_size(%s::regclass)", (TABLE_NAME,))
        row = cursor.fetchone()
        return max(0, int((row or (0,))[0] or 0))

    def _delete_batch(self, cursor: Any, *, pressure: bool) -> int:
        if pressure:
            query = f"""
                WITH ranked AS (
                    SELECT experience_id,
                           row_number() OVER (
                               PARTITION BY case_type, binding, orchestration_policy
                               ORDER BY created_at DESC, experience_id DESC
                           ) AS row_number
                      FROM {TABLE_NAME}
                ), eligible AS (
                    SELECT e.experience_id
                      FROM {TABLE_NAME} e
                      JOIN ranked r ON r.experience_id = e.experience_id
                     WHERE r.row_number > %s
                       AND e.created_at < now() - (%s * interval '1 day')
                     ORDER BY
                       CASE
                           WHEN e.failure_codes && %s::text[] THEN 0
                           WHEN NOT e.success THEN 1
                           ELSE 2
                       END,
                       e.created_at ASC,
                       e.experience_id ASC
                     LIMIT %s
                )
                DELETE FROM {TABLE_NAME} e
                 USING eligible
                 WHERE e.experience_id = eligible.experience_id
            """
            args = (
                D5_PRESERVE_LATEST_PER_GROUP,
                D5_PRESERVE_RECENT_DAYS,
                list(OPERATIONAL_FAILURE_CODES),
                self.batch_size,
            )
        else:
            query = f"""
                WITH ranked AS (
                    SELECT experience_id,
                           row_number() OVER (
                               PARTITION BY case_type, binding, orchestration_policy
                               ORDER BY created_at DESC, experience_id DESC
                           ) AS row_number
                      FROM {TABLE_NAME}
                ), eligible AS (
                    SELECT e.experience_id
                      FROM {TABLE_NAME} e
                      JOIN ranked r ON r.experience_id = e.experience_id
                     WHERE r.row_number > %s
                       AND e.created_at < now() - CASE
                           WHEN e.failure_codes && %s::text[]
                               THEN (%s * interval '1 day')
                           WHEN NOT e.success
                               THEN (%s * interval '1 day')
                           ELSE (%s * interval '1 day')
                       END
                     ORDER BY
                       CASE
                           WHEN e.failure_codes && %s::text[] THEN 0
                           WHEN NOT e.success THEN 1
                           ELSE 2
                       END,
                       e.created_at ASC,
                       e.experience_id ASC
                     LIMIT %s
                )
                DELETE FROM {TABLE_NAME} e
                 USING eligible
                 WHERE e.experience_id = eligible.experience_id
            """
            args = (
                D5_PRESERVE_LATEST_PER_GROUP,
                list(OPERATIONAL_FAILURE_CODES),
                D5_OPERATIONAL_RETENTION_DAYS,
                D5_FAILURE_RETENTION_DAYS,
                D5_SUCCESS_RETENTION_DAYS,
                list(OPERATIONAL_FAILURE_CODES),
                self.batch_size,
            )
        cursor.execute(query, args)
        return max(0, int(getattr(cursor, "rowcount", 0) or 0))

    def _vacuum_analyze(self, connection: Any) -> bool:
        if not self.vacuum_analyze:
            return False
        previous_autocommit = getattr(connection, "autocommit", False)
        try:
            connection.autocommit = True
            cursor = connection.cursor()
            try:
                cursor.execute(f"VACUUM (ANALYZE) {TABLE_NAME}")
            finally:
                close = getattr(cursor, "close", None)
                if callable(close):
                    close()
            return True
        except Exception as exc:  # maintenance is best effort and fail-open
            LOG.warning("memo-harness-d5-retention-vacuum-failed error=%s", type(exc).__name__)
            return False
        finally:
            try:
                connection.autocommit = previous_autocommit
            except Exception:
                pass

    def run_once(self, *, dry_run: bool = False) -> RetentionRun:
        if not self.enabled:
            return RetentionRun(False, False, 0, 0, "disabled")
        if not self.dsn:
            return RetentionRun(True, False, 0, 0, "unavailable", error_code="D5_DATABASE_URL_MISSING")

        connection = None
        try:
            connection = self._connect()
            cursor = connection.cursor()
            try:
                cursor.execute("SET LOCAL statement_timeout = %s", (self.statement_timeout_ms,))
                before = self._size(cursor)
                band = capacity_band(before)
                expired = 0
                pressure = 0
                if not dry_run:
                    expired = self._delete_batch(cursor, pressure=False)
                    if before >= D5_CLEANUP_RELATION_BYTES:
                        for _ in range(self.max_pressure_batches):
                            deleted = self._delete_batch(cursor, pressure=True)
                            pressure += deleted
                            if deleted < self.batch_size:
                                break
                    connection.commit()
                after = self._size(cursor)
                vacuumed = False
                if not dry_run and (expired or pressure):
                    # _size() starts a read transaction after the delete
                    # commit. End it before switching to autocommit for
                    # VACUUM, which PostgreSQL requires.
                    connection.commit()
                    vacuumed = self._vacuum_analyze(connection)
                LOG.info(
                    "memo-harness-d5-retention enabled=true dry_run=%s capacity=%s "
                    "relation_size_before=%d relation_size_after=%d expired_deleted=%d "
                    "pressure_deleted=%d vacuum_analyzed=%s",
                    str(bool(dry_run)).lower(),
                    capacity_band(after),
                    before,
                    after,
                    expired,
                    pressure,
                    str(bool(vacuumed)).lower(),
                )
                return RetentionRun(
                    True,
                    True,
                    before,
                    after,
                    capacity_band(after),
                    expired,
                    pressure,
                    vacuumed,
                )
            finally:
                close = getattr(cursor, "close", None)
                if callable(close):
                    close()
        except Exception as exc:  # D5 retention must never affect request paths.
            if connection is not None:
                rollback = getattr(connection, "rollback", None)
                if callable(rollback):
                    rollback()
            LOG.warning("memo-harness-d5-retention-failed error=%s", type(exc).__name__)
            return RetentionRun(True, False, 0, 0, "unavailable", error_code=type(exc).__name__)
        finally:
            if connection is not None:
                self._close(connection)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="MemoHarness D5 Experience Bank retention worker")
    parser.add_argument(
        "--interval",
        type=float,
        default=float(os.getenv("MEMOHARNESS_D5_RETENTION_INTERVAL_SECONDS", "86400")),
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.getenv("MEMOHARNESS_D5_RETENTION_LOG_LEVEL", "INFO"), format="%(message)s")
    stop = False

    def shutdown(_signum: int, _frame: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)
    worker = ExperienceRetentionWorker.from_env()
    while not stop:
        started = time.perf_counter()
        worker.run_once(dry_run=args.dry_run)
        if args.once:
            break
        elapsed = time.perf_counter() - started
        stop = stop or args.interval <= 0
        if not stop:
            time.sleep(max(0.1, args.interval - elapsed))
    return 0


__all__ = ["ExperienceRetentionWorker", "RetentionRun", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
