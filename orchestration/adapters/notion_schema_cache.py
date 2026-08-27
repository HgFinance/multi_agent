"""Small bounded cache for stable Notion database metadata."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any


class BoundedNotionSchemaCache:
    """Cache successful schema reads for one projection owner.

    The cache is intentionally instance-scoped.  It has a short TTL and a
    bounded number of database entries, so a stale or unbounded global cache
    cannot become a source of Notion property truth.
    """

    def __init__(self, *, ttl_seconds: float = 60.0, max_entries: int = 8) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self.max_entries = int(max_entries)
        self._entries: dict[str, tuple[float, Mapping[str, Any]]] = {}
        self._lock = threading.RLock()

    def get(
        self,
        database_id: str,
        loader: Callable[[], Mapping[str, Any]],
    ) -> tuple[Mapping[str, Any], bool]:
        key = str(database_id or "").strip()
        if not key:
            raise ValueError("database_id is required")

        started_at = time.monotonic()
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and started_at - cached[0] < self.ttl_seconds:
                return cached[1], True

        # Do not hold the cache lock over the network-bound loader. A slow
        # Notion request for one database must not serialize other owners.
        try:
            schema = loader()
        except Exception:
            with self._lock:
                current = self._entries.get(key)
                if current is cached or (
                    current is not None and current[0] < started_at
                ):
                    self._entries.pop(key, None)
            raise

        if not isinstance(schema, Mapping):
            with self._lock:
                current = self._entries.get(key)
                if current is cached or (
                    current is not None and current[0] < started_at
                ):
                    self._entries.pop(key, None)
            raise TypeError("Notion schema must be a mapping")

        loaded_at = time.monotonic()
        with self._lock:
            # Another caller may have completed a newer lookup while this
            # request was in flight. Prefer that result rather than replacing
            # it with an older response.
            current = self._entries.get(key)
            if current is not None and current[0] >= started_at:
                return current[1], True
            if len(self._entries) >= self.max_entries and key not in self._entries:
                oldest_key = min(
                    self._entries,
                    key=lambda entry_key: self._entries[entry_key][0],
                )
                self._entries.pop(oldest_key, None)
            self._entries[key] = (loaded_at, schema)
            return schema, False

    def invalidate(self, database_id: str) -> None:
        with self._lock:
            self._entries.pop(str(database_id or "").strip(), None)


__all__ = ["BoundedNotionSchemaCache"]
