from __future__ import annotations

import threading

from orchestration.adapters.notion_schema_cache import BoundedNotionSchemaCache


def test_slow_schema_lookup_does_not_hold_cache_lock_for_other_database() -> None:
    cache = BoundedNotionSchemaCache(ttl_seconds=60, max_entries=4)
    lookup_started = threading.Event()
    release_lookup = threading.Event()
    first_result: list[tuple[dict, bool]] = []

    def slow_loader() -> dict:
        lookup_started.set()
        assert release_lookup.wait(timeout=2)
        return {"properties": {"제목": {"type": "title"}}}

    worker = threading.Thread(
        target=lambda: first_result.append(cache.get("db-slow", slow_loader))
    )
    worker.start()
    assert lookup_started.wait(timeout=2)

    second = cache.get("db-fast", lambda: {"properties": {"상태": {"type": "select"}}})
    release_lookup.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert second[1] is False
    assert first_result == [({"properties": {"제목": {"type": "title"}}}, False)]


def test_newer_concurrent_schema_result_is_not_overwritten() -> None:
    cache = BoundedNotionSchemaCache(ttl_seconds=60, max_entries=4)
    first_lookup_started = threading.Event()
    release_first = threading.Event()
    results: list[tuple[dict, bool]] = []

    def first_loader() -> dict:
        first_lookup_started.set()
        assert release_first.wait(timeout=2)
        return {"version": "first"}

    first = threading.Thread(
        target=lambda: results.append(cache.get("db", first_loader))
    )
    first.start()
    assert first_lookup_started.wait(timeout=2)

    second = cache.get("db", lambda: {"version": "second"})
    release_first.set()
    first.join(timeout=2)

    assert second == ({"version": "second"}, False)
    assert results == [({"version": "second"}, True)]
