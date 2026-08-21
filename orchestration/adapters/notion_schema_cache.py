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

        now = time.monotonic()
        with self._lock:
            cached = self._entries.get(key)
            if cached is not None and now - cached[0] < self.ttl_seconds:
                return cached[1], True

            try:
                schema = loader()
            except Exception:
                # A failed read must not preserve stale metadata as if it were
                # authoritative. The projection retains its existing retry /
                # fail-closed behavior.
                self._entries.pop(key, None)
                raise

            if not isinstance(schema, Mapping):
                self._entries.pop(key, None)
                raise TypeError("Notion schema must be a mapping")

            if key not in self._entries and len(self._entries) >= self.max_entries:
                oldest_key = min(
                    self._entries,
                    key=lambda entry_key: self._entries[entry_key][0],
                )
                self._entries.pop(oldest_key, None)
            self._entries[key] = (now, schema)
            return schema, False

    def invalidate(self, database_id: str) -> None:
        with self._lock:
            self._entries.pop(str(database_id or "").strip(), None)


__all__ = ["BoundedNotionSchemaCache"]
