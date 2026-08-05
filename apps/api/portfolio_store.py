"""Small durable SQLite snapshot store for advisory runtime state.

The store path is configurable so deployments can mount a persistent volume.
It is deliberately limited to advisory projections and never persists
credentials or external write intents.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class PortfolioRuntimeStore:
    def __init__(self, path: str | None) -> None:
        self.path = path.strip() if path else ""
        self._lock = threading.RLock()
        if self.path:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as connection:
                connection.execute(
                    "CREATE TABLE IF NOT EXISTS portfolio_runtime_snapshots ("
                    "run_id TEXT PRIMARY KEY, updated_at TEXT NOT NULL, payload TEXT NOT NULL)"
                )

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        return connection

    def save(self, job: Mapping[str, Any]) -> None:
        if not self.enabled:
            return
        payload = json.dumps(dict(job), ensure_ascii=False, default=str, separators=(",", ":"))
        with self._lock, self._connect() as connection:
            connection.execute(
                "INSERT INTO portfolio_runtime_snapshots(run_id, updated_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(run_id) DO UPDATE SET updated_at=excluded.updated_at, payload=excluded.payload",
                (str(job["run_id"]), str(job.get("updated_at", "")), payload),
            )

    def latest(self) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        with self._lock, self._connect() as connection:
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
        with self._lock, self._connect() as connection:
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
        with self._lock, self._connect() as connection:
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
