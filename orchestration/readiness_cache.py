"""Small single-flight TTL cache for read-only readiness projections.

Readiness endpoints are polled by Docker, load balancers, dashboards, and
dependent workers at the same time.  A successful probe is safe to share for a
short period; a failed probe is deliberately never cached so recovery is
visible on the next attempt.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from threading import Lock
from typing import Generic, TypeVar, cast


Value = TypeVar("Value")


class SingleFlightTTLCache(Generic[Value]):
    """Serialize cache misses and share only successful values.

    The lock is held while ``loader`` runs on purpose: concurrent callers must
    wait for the same bounded read instead of opening one database connection
    each.  Exceptions leave the cache empty and are propagated unchanged.
    """

    def __init__(
        self,
        *,
        env_var: str,
        default_seconds: float,
        minimum_seconds: float = 1.0,
        maximum_seconds: float = 30.0,
    ) -> None:
        if minimum_seconds <= 0 or maximum_seconds < minimum_seconds:
            raise ValueError("invalid readiness cache bounds")
        self._env_var = env_var
        self._default_seconds = default_seconds
        self._minimum_seconds = minimum_seconds
        self._maximum_seconds = maximum_seconds
        self._value: tuple[float, Value] | None = None
        self._lock = Lock()

    def _ttl_seconds(self) -> float:
        try:
            configured = float(os.environ.get(self._env_var, self._default_seconds))
        except (TypeError, ValueError):
            configured = self._default_seconds
        return max(self._minimum_seconds, min(configured, self._maximum_seconds))

    def _cached(self) -> tuple[bool, Value | None]:
        cached = self._value
        if cached is None:
            return False, None
        if time.monotonic() >= cached[0]:
            return False, None
        return True, cached[1]

    def get_or_compute(self, loader: Callable[[], Value]) -> Value:
        """Return a live value, single-flighting a successful cache miss."""

        found, cached = self._cached()
        if found:
            return cast(Value, cached)
        with self._lock:
            found, cached = self._cached()
            if found:
                return cast(Value, cached)
            value = loader()
            self._value = (time.monotonic() + self._ttl_seconds(), value)
            return value

    def clear(self) -> None:
        """Clear the value for tests or an explicit lifecycle reset."""

        with self._lock:
            self._value = None


__all__ = ["SingleFlightTTLCache"]
