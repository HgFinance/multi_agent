from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Event
import time

import pytest

from orchestration.connection_pool import (
    BlockingConnectionPool,
    ConnectionPoolAcquireTimeout,
    ConnectionPoolSettings,
    create_blocking_connection_pool,
)


class _Connection:
    pass


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()
        self.returned = 0
        self.closed = False

    def getconn(self):
        return self.connection

    def putconn(self, _connection, *, close=False):
        del close
        self.returned += 1

    def closeall(self):
        self.closed = True


def test_pool_waits_for_a_returned_connection_instead_of_failing_immediately() -> None:
    raw_pool = _Pool()
    pool = BlockingConnectionPool(
        raw_pool, ConnectionPoolSettings(max_connections=1, acquire_timeout_seconds=1)
    )
    first = pool.getconn()
    acquired = Event()

    def borrow_after_release():
        connection = pool.getconn()
        acquired.set()
        pool.putconn(connection)

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(borrow_after_release)
        assert not acquired.wait(0.05)
        pool.putconn(first)
        assert acquired.wait(1)
        future.result()

    assert raw_pool.returned == 2


def test_pool_timeout_does_not_lose_the_slot() -> None:
    raw_pool = _Pool()
    pool = BlockingConnectionPool(
        raw_pool, ConnectionPoolSettings(max_connections=1, acquire_timeout_seconds=0.05)
    )
    connection = pool.getconn()
    started = time.monotonic()
    with pytest.raises(ConnectionPoolAcquireTimeout):
        pool.getconn()
    assert time.monotonic() - started >= 0.04
    pool.putconn(connection)
    pool.putconn(pool.getconn())


def test_underlying_pool_failure_releases_the_guard_slot() -> None:
    class FlakyPool(_Pool):
        def __init__(self):
            super().__init__()
            self.fail = True

        def getconn(self):
            if self.fail:
                self.fail = False
                raise RuntimeError("driver failure")
            return super().getconn()

    raw_pool = FlakyPool()
    pool = BlockingConnectionPool(
        raw_pool, ConnectionPoolSettings(max_connections=1, acquire_timeout_seconds=0.1)
    )
    with pytest.raises(RuntimeError, match="driver failure"):
        pool.getconn()
    pool.putconn(pool.getconn())


def test_factory_keeps_existing_pool_size_by_default_and_accepts_env_override(monkeypatch):
    calls = []
    raw_pool = _Pool()

    def factory(minconn, maxconn, dsn):
        calls.append((minconn, maxconn, dsn))
        return raw_pool

    monkeypatch.setenv("RISK_QA_DB_POOL_MAX_CONNECTIONS", "8")
    pool = create_blocking_connection_pool(
        factory,
        "postgresql://control",
        minconn=0,
        default_maxconn=4,
        env_prefix="RISK_QA",
    )

    assert calls == [(0, 8, "postgresql://control")]
    connection = pool.getconn()
    pool.putconn(connection)
