"""Profile-scoped, durable idempotency for Discord gateway delivery.

This module deliberately has no Hermes or Discord dependency.  The gateway
image installs a small adapter shim which calls it at the inbound and
outbound boundaries.  The existing Discord recovery database is reused, but
the tables are separate so recovery/backfill semantics are unchanged.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_DB_FILENAME = "discord_message_recovery.db"
_RETENTION = timedelta(days=30)
_ACTIVE_LEASE = timedelta(minutes=30)
_INBOUND_TERMINAL_STATES = frozenset({"COMPLETED", "EXPIRED"})


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
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.row_factory = sqlite3.Row
            if not self._initialized:
                self._initialize(conn)
                self._initialized = True
            return conn
        except Exception:
            # Concurrent first-use can fail while switching WAL mode or
            # applying the idempotent schema.  `_run` retries that operation,
            # but it cannot close a connection that `_connect` never returned.
            # Close here so Windows/OneDrive does not retain a leaked file
            # handle and production retries do not accumulate descriptors.
            conn.close()
            raise

    def _initialize(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_idempotency_inbound (
                dedup_key TEXT NOT NULL,
                message_id TEXT NOT NULL,
                guild_id TEXT,
                channel_id TEXT,
                thread_id TEXT,
                session_id TEXT,
                profile TEXT NOT NULL,
                handler TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile, dedup_key)
            )
            """
        )
        # Existing production ledgers predate session correlation.  Migrate
        # the existing table in place; the inbound/outbound ledger remains the
        # single source of Discord recovery state.
        inbound_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(discord_idempotency_inbound)"
            ).fetchall()
        }
        if "session_id" not in inbound_columns:
            conn.execute(
                "ALTER TABLE discord_idempotency_inbound ADD COLUMN session_id TEXT"
            )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS discord_idempotency_outbound (
                response_key TEXT NOT NULL,
                dedup_key TEXT NOT NULL,
                source_message_id TEXT,
                profile TEXT NOT NULL,
                state TEXT NOT NULL,
                response_message_id TEXT,
                attempts INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (profile, response_key)
            )
            """
        )
        outbound_columns = {
            str(row[1])
            for row in conn.execute(
                "PRAGMA table_info(discord_idempotency_outbound)"
            ).fetchall()
        }
        if "source_message_id" not in outbound_columns:
            conn.execute(
                "ALTER TABLE discord_idempotency_outbound "
                "ADD COLUMN source_message_id TEXT"
            )
        # Backfill the exact correlation already encoded in every canonical
        # dedup key.  Discord ids contain no colon, so the final segment is
        # deterministic and lets future lookups use an indexed equality join.
        for row in conn.execute(
            "SELECT profile, response_key, dedup_key "
            "FROM discord_idempotency_outbound "
            "WHERE source_message_id IS NULL OR source_message_id=''"
        ).fetchall():
            source_message_id = str(row[2] or "").rsplit(":", 1)[-1]
            if source_message_id:
                conn.execute(
                    "UPDATE discord_idempotency_outbound "
                    "SET source_message_id=? WHERE profile=? AND response_key=?",
                    (source_message_id, row[0], row[1]),
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discord_idem_inbound_message "
            "ON discord_idempotency_inbound(message_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discord_idem_inbound_session "
            "ON discord_idempotency_inbound(profile, session_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discord_idem_inbound_updated "
            "ON discord_idempotency_inbound(updated_at)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discord_idem_inbound_conversation "
            "ON discord_idempotency_inbound(profile, guild_id, channel_id, thread_id)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_discord_idem_outbound_source "
            "ON discord_idempotency_outbound(profile, source_message_id, updated_at)"
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
        if state in _INBOUND_TERMINAL_STATES:
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
        session_id: str | None = None,
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
                # FAILED is retryable, but not unbounded. EXPIRED is handled
                # by _is_active above and is never replayed: it is reserved
                # for an audited stale lease with no delivery evidence.
                attempts = int(row[1] or 0)
                if attempts >= 3:
                    return ClaimResult(admitted=False, dedup_hit=True, state=state)
                conn.execute(
                    "UPDATE discord_idempotency_inbound SET message_id=?, guild_id=?, "
                    "channel_id=?, thread_id=?, session_id=?, profile=?, handler=?, "
                    "state='RECEIVED', "
                    "attempts=attempts+1, updated_at=? WHERE profile=? AND dedup_key=?",
                    (
                        message_id,
                        guild_id,
                        channel_id,
                        thread_id,
                        session_id,
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
                "(dedup_key, message_id, guild_id, channel_id, thread_id, session_id, "
                "profile, handler, state, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'RECEIVED', ?)",
                (
                    dedup_key,
                    message_id,
                    guild_id,
                    channel_id,
                    thread_id,
                    session_id,
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
        if state not in {"RECEIVED", "PROCESSING", "COMPLETED", "FAILED", "EXPIRED"}:
            raise ValueError(f"invalid inbound state: {state}")
        now = self._now()

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE discord_idempotency_inbound SET state=?, updated_at=? "
                "WHERE profile=? AND dedup_key=?",
                (state, now, profile, dedup_key),
            )

        self._run(operation)

    def reconcile_stale_inbound(
        self,
        *,
        profile: str | None = None,
        older_than: timedelta = _ACTIVE_LEASE,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """Close only stale inbound leases with exact local evidence.

        This is the one reconciliation boundary for historical gateway rows.
        A matching completed outbound row is the only evidence that permits a
        ``COMPLETED`` repair. Every other stale ``PROCESSING`` row becomes
        ``EXPIRED`` and is permanently excluded from replay. Active rows,
        malformed timestamps, and rows outside the requested profile are left
        untouched. The whole decision runs under the same SQLite write lock as
        normal claims, so a live delivery cannot be converted underneath it.
        """

        if older_than.total_seconds() <= 0:
            raise ValueError("older_than must be positive")
        observed_at = now or datetime.now(timezone.utc)
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)

        def operation(conn: sqlite3.Connection) -> dict[str, int]:
            query = (
                "SELECT dedup_key, profile, updated_at "
                "FROM discord_idempotency_inbound "
                "WHERE state='PROCESSING'"
            )
            parameters: list[str] = []
            if profile:
                query += " AND profile=?"
                parameters.append(profile)
            rows = conn.execute(query, parameters).fetchall()
            result = {"scanned": 0, "completed": 0, "expired": 0, "skipped": 0}
            for row in rows:
                try:
                    updated_at = datetime.fromisoformat(str(row[2]))
                except (TypeError, ValueError):
                    result["skipped"] += 1
                    continue
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=timezone.utc)
                if observed_at - updated_at <= older_than:
                    result["skipped"] += 1
                    continue

                result["scanned"] += 1
                has_completed_delivery = conn.execute(
                    "SELECT 1 FROM discord_idempotency_outbound "
                    "WHERE profile=? AND dedup_key=? AND state='COMPLETED' LIMIT 1",
                    (str(row[1]), str(row[0])),
                ).fetchone()
                next_state = "COMPLETED" if has_completed_delivery else "EXPIRED"
                updated = conn.execute(
                    "UPDATE discord_idempotency_inbound SET state=?, updated_at=? "
                    "WHERE profile=? AND dedup_key=? AND state='PROCESSING'",
                    (
                        next_state,
                        observed_at.isoformat(),
                        str(row[1]),
                        str(row[0]),
                    ),
                ).rowcount
                if updated != 1:
                    result["skipped"] += 1
                else:
                    result["completed" if next_state == "COMPLETED" else "expired"] += 1
            return result

        return self._run(operation)

    def inbound_key_for_message(self, message_id: str, profile: str) -> str | None:
        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT dedup_key FROM discord_idempotency_inbound "
                "WHERE profile=? AND message_id=? ORDER BY updated_at DESC LIMIT 1",
                (profile, str(message_id)),
            ).fetchone()
            return str(row[0]) if row else None

        return self._run(operation)

    def inbound_key_for_session(self, session_id: str, profile: str) -> str | None:
        """Resolve only an exact profile-local Hermes session correlation."""

        if not session_id:
            return None

        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT dedup_key FROM discord_idempotency_inbound "
                "WHERE profile=? AND session_id=? "
                "ORDER BY updated_at DESC LIMIT 1",
                (profile, session_id),
            ).fetchone()
            return str(row[0]) if row else None

        return self._run(operation)

    def bind_inbound_session(
        self, message_id: str, session_id: str, profile: str
    ) -> None:
        """Attach a session to an already-claimed exact inbound message."""

        if not message_id or not session_id:
            return
        now = self._now()

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE discord_idempotency_inbound SET session_id=?, updated_at=? "
                "WHERE profile=? AND message_id=?",
                (session_id, now, profile, message_id),
            )

        self._run(operation)

    # hgfinance-bind-inbound-thread-v1
    def bind_inbound_thread(
        self, message_id: str, thread_id: str, profile: str
    ) -> None:
        """Attach the exact Discord request thread to an admitted inbound message."""

        if not message_id or not thread_id:
            return

        now = self._now()

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE discord_idempotency_inbound "
                "SET thread_id=?, updated_at=? "
                "WHERE profile=? AND message_id=?",
                (str(thread_id), now, profile, str(message_id)),
            )

        self._run(operation)

    def inbound_context(self, dedup_key: str, profile: str) -> dict[str, str | None]:
        def operation(conn: sqlite3.Connection) -> dict[str, str | None]:
            row = conn.execute(
                "SELECT guild_id, channel_id, thread_id, message_id, session_id "
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
                "session_id": row[4],
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

    def outbound_message_id(
        self,
        response_key: str,
        profile: str,
    ) -> str | None:
        """Return the Discord message created for an outbound response."""

        def operation(conn: sqlite3.Connection) -> str | None:
            row = conn.execute(
                "SELECT response_message_id "
                "FROM discord_idempotency_outbound "
                "WHERE profile=? AND response_key=? "
                "LIMIT 1",
                (profile, response_key),
            ).fetchone()

            if row is None or not row[0]:
                return None

            return str(row[0])

        return self._run(operation)

    def latest_completed_response(
        self,
        *,
        profile: str,
        guild_id: str,
        channel_id: str,
    ) -> tuple[str, str] | None:
        """Return the latest CEO answer in the same Discord conversation.

        The outbound row owns the published message id while the inbound row
        owns parent/thread correlation.  Joining those existing ledgers keeps
        a short ``대답`` control message from creating another workflow or a
        second response cache.
        """

        def operation(conn: sqlite3.Connection) -> tuple[str, str] | None:
            row = conn.execute(
                """
                SELECT o.response_message_id,
                       COALESCE(i.thread_id, i.channel_id) AS response_channel_id
                  FROM discord_idempotency_outbound o
                  JOIN discord_idempotency_inbound i
                    ON i.profile=o.profile
                   AND o.source_message_id=i.message_id
                 WHERE o.profile=?
                   AND o.state='COMPLETED'
                   AND o.response_message_id IS NOT NULL
                   AND i.guild_id=?
                   AND (i.channel_id=? OR i.thread_id=?)
                   AND (
                        o.response_key LIKE '%:synthesis-detail:%'
                        OR o.response_key LIKE '%:ceo-direct:%'
                        OR o.response_key LIKE '%:ceo-blocked:%'
                        OR o.response_key LIKE '%:final'
                   )
                 ORDER BY o.updated_at DESC
                 LIMIT 1
                """,
                (profile, guild_id, channel_id, channel_id),
            ).fetchone()
            if row is None or not row[0] or not row[1]:
                return None
            return str(row[0]), str(row[1])

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
                "(response_key, dedup_key, source_message_id, profile, state, updated_at) "
                "VALUES (?, ?, ?, ?, 'PROCESSING', ?)",
                (
                    response_key,
                    dedup_key,
                    str(dedup_key).rsplit(":", 1)[-1],
                    profile,
                    now,
                ),
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
