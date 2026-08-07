"""Small durable SQLite snapshot store for advisory runtime state.

The store path is configurable so deployments can mount a persistent volume.
It is deliberately limited to advisory projections and never persists
credentials or external write intents.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


# Windows: os.kill(dead_pid, 0) raises ERROR_INVALID_PARAMETER, not ProcessLookupError.
_WIN_ERROR_INVALID_PARAMETER = 87


def _process_alive(pid: int | None) -> bool:
    """Is this PID still running? Unknown answers count as *alive*.

    The caller uses this to decide whether a crashed worker's durable slot may
    be stolen. Guessing "dead" on a probe failure would hand a live owner's slot
    to a second instance, so only a definite answer releases it.

    This is the single definition — ``portfolio_runtime`` imports it. Two copies
    used to exist and a test patching one silently left the other live.
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True                      # exists, just not ours to signal
    except OSError as exc:
        # Windows reports a vanished PID as an invalid parameter.
        if getattr(exc, "winerror", None) == _WIN_ERROR_INVALID_PARAMETER:
            return False
        return True                      # unknown -> never steal the slot
    return True


class PortfolioRuntimeStore:
    def __init__(self, path: str | None) -> None:
        self.path = path.strip() if path else ""
        self._lock = threading.RLock()
        if self.path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self._session() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS portfolio_runtime_snapshots ("
                    "run_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS portfolio_runtime_active ("
                    "slot INTEGER PRIMARY KEY CHECK (slot = 1), "
                    "run_id TEXT NOT NULL, updated_at TEXT NOT NULL, "
                    "owner_pid INTEGER NOT NULL DEFAULT 0, "
                    "owner_token TEXT NOT NULL DEFAULT '')"
                )
                columns = {
                    str(row["name"])
                    for row in connection.execute(
                        "PRAGMA table_info(portfolio_runtime_active)"
                    ).fetchall()
                }
                if "owner_pid" not in columns:
                    connection.execute(
                        "ALTER TABLE portfolio_runtime_active "
                        "ADD COLUMN owner_pid INTEGER NOT NULL DEFAULT 0"
                    )
                if "owner_token" not in columns:
                    connection.execute(
                        "ALTER TABLE portfolio_runtime_active "
                        "ADD COLUMN owner_token TEXT NOT NULL DEFAULT ''"
                    )
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS portfolio_runtime_queue ("
                    "run_id TEXT PRIMARY KEY, enqueued_at REAL NOT NULL, "
                    "claimed_by TEXT, lease_until REAL, attempts INTEGER NOT NULL DEFAULT 0)"
                )

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """Transaction *and* close.

        ``with sqlite3.connect(...) as conn`` commits or rolls back but **never
        closes** — that is the documented contract of ``Connection.__exit__``.
        Every call site here used that form, so each store operation leaked an
        open handle until garbage collection. POSIX hides it (an open file can
        still be unlinked); Windows raises ``WinError 32`` when the temp
        directory holding ``runtime.sqlite3`` is cleaned up. Either way a
        long-running BFF leaks descriptors, so close explicitly.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def save(self, job: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        payload = json.dumps(dict(job), ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock, self._session() as connection:
            connection.execute(
                "INSERT INTO portfolio_runtime_snapshots(run_id, updated_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload",
                (str(job["run_id"]), str(job.get("updated_at", "")), payload),
            )
    def enqueue(self, run_id: str) -> None:
        if not self.enabled:
            return
        with self._lock, self._session() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO portfolio_runtime_queue(run_id, enqueued_at) VALUES (?, ?)",
                (run_id, time.time()),
            )
    def requeue_nonterminal(self) -> int:
        """Ensure queued or interrupted snapshots have a durable queue row."""
        if not self.enabled:
            return 0
        with self._lock, self._session() as connection:
            rows = connection.execute(
                "SELECT run_id, payload FROM portfolio_runtime_snapshots"
            ).fetchall()
            recovered = 0
            for row in rows:
                try:
                    payload = json.loads(row["payload"])
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict) or payload.get("status") not in {"QUEUED", "RUNNING"}:
                    continue
                result = connection.execute(
                    "INSERT OR IGNORE INTO portfolio_runtime_queue(run_id, enqueued_at) VALUES (?, ?)",
                    (str(row["run_id"]), time.time()),
                )
                recovered += result.rowcount
            return recovered

    def claim_next(self, worker_id: str, lease_seconds: float = 60.0) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        now = time.time()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT run_id FROM portfolio_runtime_queue "
                    "WHERE claimed_by IS NULL OR lease_until < ? "
                    "ORDER BY enqueued_at LIMIT 1",
                    (now,),
                ).fetchone()
                if row is None:
                    connection.rollback()
                    return None
                run_id = str(row["run_id"])
                connection.execute(
                    "UPDATE portfolio_runtime_queue "
                    "SET claimed_by = ?, lease_until = ?, attempts = attempts + 1 "
                    "WHERE run_id = ?",
                    (worker_id, now + lease_seconds, run_id),
                )
                payload_row = connection.execute(
                    "SELECT payload FROM portfolio_runtime_snapshots WHERE run_id = ?",
                    (run_id,),
                ).fetchone()
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
        if payload_row is None:
            return None
        try:
            value = json.loads(payload_row["payload"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def heartbeat(self, run_id: str, worker_id: str, lease_seconds: float = 60.0) -> bool:
        if not self.enabled:
            return False
        with self._lock, self._session() as connection:
            # rowcount 는 커서 속성이다. 연결이 닫힌 뒤에 읽지 않도록 블록 안에서 뽑는다
            # (Row 와 달리 커서는 연결 수명에 묶인다).
            updated = connection.execute(
                "UPDATE portfolio_runtime_queue SET lease_until = ? "
                "WHERE run_id = ? AND claimed_by = ?",
                (time.time() + lease_seconds, run_id, worker_id),
            ).rowcount
        return updated == 1
    def is_claim_owner(self, run_id: str, worker_id: str) -> bool:
        if not self.enabled:
            return False
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT 1 FROM portfolio_runtime_queue "
                "WHERE run_id = ? AND claimed_by = ? AND lease_until >= ?",
                (run_id, worker_id, time.time()),
            ).fetchone()
        return row is not None

    def release_claim(self, run_id: str, worker_id: str | None = None) -> None:
        if not self.enabled:
            return
        with self._lock, self._session() as connection:
            if worker_id is None:
                connection.execute(
                    "DELETE FROM portfolio_runtime_queue WHERE run_id = ?",
                    (run_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM portfolio_runtime_queue WHERE run_id = ? AND claimed_by = ?",
                    (run_id, worker_id),
                )


    def active_run_id(self) -> str | None:
        """Return the durable active-run owner, if one is currently reserved."""

        if not self.enabled:
            return None
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT run_id FROM portfolio_runtime_active WHERE slot = 1"
            ).fetchone()
        return str(row["run_id"]) if row is not None else None


    def active_run_owner_pid(self) -> int | None:
        """Return the process that reserved the durable execution slot."""
        if not self.enabled:
            return None
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT owner_pid FROM portfolio_runtime_active WHERE slot = 1"
            ).fetchone()
        return int(row["owner_pid"]) if row is not None else None
    def reserve_active_run(
        self,
        run_id: str,
        updated_at: str,
        owner_pid: int,
        owner_token: str | None = None,
    ) -> bool:
        """Atomically reserve the single advisory execution slot."""
        if not self.enabled:
            return True
        token = str(owner_token or owner_pid)
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT run_id, owner_pid, owner_token "
                    "FROM portfolio_runtime_active WHERE slot = 1"
                ).fetchone()
                if row is not None:
                    current_run_id = str(row["run_id"])
                    current_token = str(row["owner_token"] or "")
                    current_pid = int(row["owner_pid"])
                    current = connection.execute(
                        "SELECT payload FROM portfolio_runtime_snapshots WHERE run_id = ?",
                        (current_run_id,),
                    ).fetchone()
                    status = None
                    if current is not None:
                        try:
                            value = json.loads(current["payload"])
                            status = value.get("status") if isinstance(value, dict) else None
                        except json.JSONDecodeError:
                            status = None
                    same_owner = current_run_id == run_id and current_token == token
                    stale_owner = not _process_alive(current_pid)
                    if not same_owner and status not in {"COMPLETED", "HOLD", "DEGRADED", "ERROR"} and not stale_owner:
                        connection.rollback()
                        return False
                    connection.execute("DELETE FROM portfolio_runtime_active WHERE slot = 1")
                connection.execute(
                    "INSERT INTO portfolio_runtime_active("
                    "slot, run_id, updated_at, owner_pid, owner_token"
                    ") VALUES (1, ?, ?, ?, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET "
                    "run_id=excluded.run_id, updated_at=excluded.updated_at, "
                    "owner_pid=excluded.owner_pid, owner_token=excluded.owner_token",
                    (run_id, updated_at, owner_pid, token),
                )
                connection.commit()
                return True
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()

    def release_active_run(self, run_id: str, owner_token: str | None = None) -> None:
        """Release the durable execution slot only when owned by ``run_id``."""
        if not self.enabled:
            return
        with self._lock, self._session() as connection:
            if owner_token is None:
                connection.execute(
                    "DELETE FROM portfolio_runtime_active WHERE slot = 1 AND run_id = ?",
                    (run_id,),
                )
            else:
                connection.execute(
                    "DELETE FROM portfolio_runtime_active "
                    "WHERE slot = 1 AND run_id = ? AND owner_token = ?",
                    (run_id, owner_token),
                )

    def latest(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT payload FROM portfolio_runtime_snapshots ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def get(self, run_id: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock, self._session() as connection:
            row = connection.execute(
                "SELECT payload FROM portfolio_runtime_snapshots WHERE run_id = ?",
                (run_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["payload"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def find_by_idempotency(self, owner_id: str, idempotency_key: str) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock, self._session() as connection:
            rows = connection.execute(
                "SELECT payload FROM portfolio_runtime_snapshots ORDER BY updated_at DESC"
            ).fetchall()
        for row in rows:
            try:
                value = json.loads(row["payload"])
            except json.JSONDecodeError:
                continue
            if (
                isinstance(value, dict)
                and value.get("profile_user_id") == owner_id
                and value.get("idempotency_key") == idempotency_key
            ):
                return value
        return None
