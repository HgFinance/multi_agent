"""Profile-scoped, durable idempotency for Discord gateway delivery.

This module deliberately has no Hermes or Discord dependency.  The gateway
image installs a small adapter shim which calls it at the inbound and
outbound boundaries.  The existing Discord recovery database is reused, but
the tables are separate so recovery/backfill semantics are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_DB_FILENAME = "discord_message_recovery.db"
_RETENTION = timedelta(days=30)
_ACTIVE_LEASE = timedelta(minutes=30)


def canonical_discord_dedup_key(
    guild_id: str | int | None,
    channel_id: str | int | None,
    message_id: str | int | None,
) -> str:
    """Return the stable per-profile Discord inbound key."""

    guild = str(guild_id or "dm")
    channel = str(channel_id or "unknown")
    message = str(message_id or "unknown")
    return f"discord:{guild}:{channel}:{message}"


@dataclass(frozen=True)
class ClaimResult:
    admitted: bool
    dedup_hit: bool
    state: str | None = None
    response_message_id: str | None = None


class IdempotencyStoreUnavailable(RuntimeError):
    """Raised when the durable dedup ledger cannot be used safely."""


class DiscordIdempotencyStore:
    """Atomic, profile-local inbound and outbound delivery ledger.

    The database lives under the active Hermes home.  Separate profile
    containers therefore do not share claims, while two processes using the
    same profile serialize claims through SQLite's write lock.
    """

    def __init__(
        self,
        hermes_home: str | Path,
        *,
        retention: timedelta = _RETENTION,
    ) -> None:
        self._hermes_home = Path(hermes_home)
        self._retention = retention
        self._lock = threading.Lock()
        self._initialized = False

    @property
    def path(self) -> Path:
        directory = self._hermes_home / "gateway"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / _DB_FILENAME

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=5.0)
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        if not self._initialized:
            self._initialize(conn)
            self._initialized = True
        return conn

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_idempotency_inbound (
                dedup_key TEXT NOT NULL,
                message_id TEXT NOT NULL,
                guild_id TEXT,
                channel_id TEXT,
                thread_id TEXT,
                profile TEXT NOT NULL,
                handler TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile, dedup_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_idempotency_outbound (
                response_key TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                profile TEXT NOT NULL,
                state TEXT NOT NULL,
                response_message_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile, response_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discord_idem_inbound_message "
            "ON discord_idempotency_inbound(message_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discord_idem_inbound_updated "
            "ON discord_idempotency_inbound(updated_at)"
        )
        cutoff = (datetime.now(timezone.utc) - self._retention).isoformat()
        conn.execute(
            "DELETE FROM discord_idempotency_inbound WHERE updated_at < ?",
            (cutoff,),
        )
        conn.execute(
            "DELETE FROM discord_idempotency_outbound WHERE updated_at < ?",
            (cutoff,),
        )
        conn.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            logger.debug("Could not chmod Discord idempotency ledger", exc_info=True)

    def _run(self, operation: Any, *, default: Any = None) -> Any:
        for attempt in range(5):
            try:
                with self._lock:
                    conn = self._connect()
                    try:
                        result = operation(conn)
                        conn.commit()
                        return result
                    finally:
                        conn.close()
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    logger.error(
                        "Discord idempotency ledger unavailable; refusing duplicate-prone operation",
                        extra={
                            "db_label": "discord_message_recovery.db",
                            "error_type": type(exc).__name__,
                        },
                    )
                    if default is not None:
                        return default
                    raise IdempotencyStoreUnavailable from exc
                time.sleep(0.02 * (attempt + 1))
            except Exception as exc:
                logger.error(
                    "Discord idempotency ledger unavailable; refusing duplicate-prone operation",
                    extra={
                        "db_label": "discord_message_recovery.db",
                        "error_type": type(exc).__name__,
                    },
                )
                if default is not None:
                    return default
                raise IdempotencyStoreUnavailable from exc

    @staticmethod
    def _is_active(state: str, updated_at: str | None) -> bool:
        if state == "COMPLETED":
            return True
        if state not in {"RECEIVED", "PROCESSING"}:
            return False
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(str(updated_at))
        except (TypeError, ValueError):
            return False
        return age < _ACTIVE_LEASE

    def claim_inbound(
        self,
        *,
        dedup_key: str,
        message_id: str,
        guild_id: str,
        channel_id: str,
        thread_id: str | None,
        profile: str,
        handler: str,
    ) -> ClaimResult:
        """Atomically admit one inbound message, fail-closed on ledger errors."""

        now = self._now()

        def operation(conn: sqlite3.Connection) -> ClaimResult:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, attempts, updated_at "
                "FROM discord_idempotency_inbound WHERE profile=? AND dedup_key=?",
                (profile, dedup_key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if self._is_active(state, row[2]):
                    return ClaimResult(
                        admitted=False,
                        dedup_hit=True,
                        state=state,
                    )
                # FAILED is retryable, but not unbounded.  The Discord
                # gateway's own recovery policy remains responsible for when
                # a failed event is presented again.
                attempts = int(row[1] or 0)
                if attempts >= 3:
                    return ClaimResult(admitted=False, dedup_hit=True, state=state)
                conn.execute(
                    "UPDATE discord_idempotency_inbound SET message_id=?, guild_id=?, "
                    "channel_id=?, thread_id=?, profile=?, handler=?, state='RECEIVED', "
                "attempts=attempts+1, updated_at=? WHERE profile=? AND dedup_key=?",
                    (
                        message_id,
                        guild_id,
                        channel_id,
                        thread_id,
                        profile,
                        handler,
                        now,
                        profile,
                        dedup_key,
                    ),
                )
                return ClaimResult(admitted=True, dedup_hit=False, state="RECEIVED")

            conn.execute(
                "INSERT INTO discord_idempotency_inbound "
                "(dedup_key, message_id, guild_id, channel_id, thread_id, profile, "
                "handler, state, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'RECEIVED', ?)",
                (
                    dedup_key,
                    message_id,
                    guild_id,
                    channel_id,
                    thread_id,
                    profile,
                    handler,
                    now,
                ),
            )
            return ClaimResult(admitted=True, dedup_hit=False, state="RECEIVED")

        result = self._run(operation)
        if not isinstance(result, ClaimResult):
            raise IdempotencyStoreUnavailable("invalid inbound claim result")
        return result

    def mark_inbound(self, dedup_key: str, state: str, profile: str) -> None:
        if state not in {"RECEIVED", "PROCESSING", "COMPLETED", "FAILED"}:
            raise ValueError(f"invalid inbound state: {state}")
        now = self._now()

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE discord_idempotency_inbound SET state=?, updated_at=? "
                "WHERE profile=? AND dedup_key=?",
                (state, now, profile, dedup_key),
            )

        self._run(operation)

    def inbound_key_for_message(self, message_id: str, profile: str) -> str | None:
        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT dedup_key FROM discord_idempotency_inbound "
                "WHERE profile=? AND message_id=? ORDER BY updated_at DESC LIMIT 1",
                (profile, str(message_id)),
            ).fetchone()
            return str(row[0]) if row else None

        return self._run(operation)

    def inbound_context(self, dedup_key: str, profile: str) -> dict[str, str | None]:
        def operation(conn: sqlite3.Connection) -> dict[str, str | None]:
            row = conn.execute(
                "SELECT guild_id, channel_id, thread_id, message_id "
                "FROM discord_idempotency_inbound WHERE profile=? AND dedup_key=?",
                (profile, dedup_key),
            ).fetchone()
            if row is None:
                return {}
            return {
                "guild_id": row[0],
                "channel_id": row[1],
                "thread_id": row[2],
                "message_id": row[3],
            }

        return self._run(operation)

    def inbound_state(self, dedup_key: str, profile: str) -> str | None:
        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT state FROM discord_idempotency_inbound "
                "WHERE profile=? AND dedup_key=?",
                (profile, dedup_key),
            ).fetchone()
            return str(row[0]) if row else None

        return self._run(operation)

    def claim_outbound(self, *, response_key: str, dedup_key: str, profile: str) -> ClaimResult:
        """Atomically reserve one final response publication."""

        now = self._now()

        def operation(conn: sqlite3.Connection) -> ClaimResult:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT state, response_message_id, attempts, updated_at FROM "
                "discord_idempotency_outbound WHERE profile=? AND response_key=?",
                (profile, response_key),
            ).fetchone()
            if row is not None:
                state = str(row[0])
                if self._is_active(state, row[3]):
                    return ClaimResult(False, True, state, row[1])
                attempts = int(row[2] or 0)
                if attempts >= 3:
                    return ClaimResult(False, True, state, row[1])
                conn.execute(
                    "UPDATE discord_idempotency_outbound SET state='PROCESSING', "
                    "attempts=attempts+1, updated_at=? WHERE profile=? AND response_key=?",
                    (now, profile, response_key),
                )
                return ClaimResult(True, False, "PROCESSING", row[1])
            conn.execute(
                "INSERT INTO discord_idempotency_outbound "
                "(response_key, dedup_key, profile, state, updated_at) "
                "VALUES (?, ?, ?, 'PROCESSING', ?)",
                (response_key, dedup_key, profile, now),
            )
            return ClaimResult(True, False, "PROCESSING")

        result = self._run(operation)
        if not isinstance(result, ClaimResult):
            raise IdempotencyStoreUnavailable("invalid outbound claim result")
        return result

    def mark_outbound(
        self,
        response_key: str,
        state: str,
        profile: str,
        response_message_id: str | None = None,
    ) -> None:
        if state not in {"PROCESSING", "COMPLETED", "FAILED"}:
            raise ValueError(f"invalid outbound state: {state}")
        now = self._now()

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE discord_idempotency_outbound SET state=?, "
                "response_message_id=COALESCE(?, response_message_id), updated_at=? "
                "WHERE profile=? AND response_key=?",
                (state, response_message_id, now, profile, response_key),
            )

        self._run(operation)


def safe_json_log_fields(**fields: Any) -> str:
    """Serialize only non-secret structured gateway fields for one log line."""

    return json.dumps(fields, sort_keys=True, separators=(",", ":"), default=str)
