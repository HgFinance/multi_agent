from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
import time

import pytest

from orchestration.readiness_cache import SingleFlightTTLCache


def test_concurrent_misses_share_one_successful_read(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TEST_READINESS_CACHE_SECONDS", "2")
    cache = SingleFlightTTLCache(
        env_var="TEST_READINESS_CACHE_SECONDS",
        default_seconds=2,
    )
    calls = 0
    calls_lock = Lock()
    start = Barrier(16)

    def load() -> dict[str, int]:
        nonlocal calls
        with calls_lock:
            calls += 1
        time.sleep(0.01)
        return {"calls": calls}

    def request(_item: int) -> dict[str, int]:
        start.wait()
        return cache.get_or_compute(load)

    with ThreadPoolExecutor(max_workers=16) as executor:
        values = list(executor.map(request, range(16)))

    assert calls == 1
    assert values == [{"calls": 1}] * 16


def test_failed_read_is_not_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = SingleFlightTTLCache(
        env_var="TEST_READINESS_CACHE_SECONDS",
        default_seconds=2,
    )
    calls = 0

    def load() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        return "ready"

    with pytest.raises(RuntimeError, match="database unavailable"):
        cache.get_or_compute(load)
    assert cache.get_or_compute(load) == "ready"
    assert calls == 2
