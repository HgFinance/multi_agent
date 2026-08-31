"""Bounded, blocking connection-pool access for synchronous domain APIs.

``psycopg2.pool.ThreadedConnectionPool.getconn()`` raises immediately when
all connections are leased.  That is a poor fit for FastAPI's synchronous
request workers: a short burst turns into avoidable 503/500 responses even
when the database is healthy and a connection will be returned shortly.

This module keeps the existing psycopg2 pool and adds one shared policy:
callers wait for a bounded amount of time for a lease, and every successful
lease must be returned exactly once.  It does not create a second database
client or change transaction semantics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
import math
from threading import Lock, Semaphore
from typing import Any, Callable


class ConnectionPoolAcquireTimeout(TimeoutError):
    """No connection was returned before the bounded acquire deadline."""


@dataclass(frozen=True)
class ConnectionPoolSettings:
    """Validated settings shared by all synchronous domain repositories."""

    max_connections: int
    acquire_timeout_seconds: float


def _env_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, *, minimum: float, maximum: float) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    if not math.isfinite(value):
        value = default
    return max(minimum, min(value, maximum))


def connection_pool_settings(
    *,
    env_prefix: str,
    default_max_connections: int,
    default_acquire_timeout_seconds: float = 2.0,
) -> ConnectionPoolSettings:
    """Read bounded pool settings without allowing invalid env to crash startup."""

    max_connections = _env_int(
        f"{env_prefix}_DB_POOL_MAX_CONNECTIONS",
        default_max_connections,
        minimum=1,
        maximum=32,
    )
    acquire_timeout_seconds = _env_float(
        f"{env_prefix}_DB_POOL_ACQUIRE_TIMEOUT_SECONDS",
        default_acquire_timeout_seconds,
        minimum=0.1,
        maximum=30.0,
    )
    return ConnectionPoolSettings(max_connections, acquire_timeout_seconds)


class BlockingConnectionPool:
    """Add bounded blocking leases around a psycopg2-compatible pool.

    The semaphore is acquired before touching the underlying pool, so the
    underlying ``getconn`` never sees more than ``max_connections`` active
    callers.  A lease is released in ``putconn`` even when the underlying
    pool reports a return error, preventing a bookkeeping deadlock on the
    next request.
    """

    def __init__(self, pool: Any, settings: ConnectionPoolSettings) -> None:
        self._pool = pool
        self._settings = settings
        self._slots = Semaphore(settings.max_connections)
        self._state_lock = Lock()
        self._leased: set[int] = set()
        self._closed = False

    def getconn(self) -> Any:
        with self._state_lock:
            if self._closed:
                raise RuntimeError("connection pool is closed")

        if not self._slots.acquire(timeout=self._settings.acquire_timeout_seconds):
            raise ConnectionPoolAcquireTimeout(
                "database connection pool acquire timed out"
            )

        try:
            connection = self._pool.getconn()
            if connection is None:
                raise RuntimeError("database connection pool returned no connection")
            with self._state_lock:
                if self._closed:
                    self._return(connection, close=True)
                    raise RuntimeError("connection pool is closed")
                self._leased.add(id(connection))
            return connection
        except BaseException:
            self._slots.release()
            raise

    def putconn(self, connection: Any, *, close: bool = False) -> None:
        lease_id = id(connection)
        with self._state_lock:
            if lease_id not in self._leased:
                raise RuntimeError("connection was returned without an active lease")
            self._leased.remove(lease_id)
        try:
            self._return(connection, close=close)
        finally:
            self._slots.release()

    def _return(self, connection: Any, *, close: bool) -> None:
        try:
            self._pool.putconn(connection, close=close)
        except TypeError:
            # Keep injected/simple pool doubles compatible with the production
            # interface; psycopg2's pool supports the close keyword.
            self._pool.putconn(connection)

    def closeall(self) -> None:
        with self._state_lock:
            self._closed = True
        self._pool.closeall()


def create_blocking_connection_pool(
    pool_type: Callable[[int, int, str], Any],
    dsn: str,
    *,
    minconn: int,
    default_maxconn: int,
    env_prefix: str,
) -> BlockingConnectionPool:
    """Construct one configured blocking wrapper around a psycopg2 pool."""

    settings = connection_pool_settings(
        env_prefix=env_prefix,
        default_max_connections=max(minconn, default_maxconn),
    )
    raw_pool = pool_type(minconn, settings.max_connections, dsn)
    return BlockingConnectionPool(raw_pool, settings)


__all__ = [
    "BlockingConnectionPool",
    "ConnectionPoolAcquireTimeout",
    "ConnectionPoolSettings",
    "connection_pool_settings",
    "create_blocking_connection_pool",
]
