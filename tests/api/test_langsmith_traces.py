from __future__ import annotations

import asyncio
import time

from apps.api import langsmith_traces


def _reset_trace_state() -> None:
    with langsmith_traces._TRACE_CACHE_LOCK:
        langsmith_traces._TRACE_CACHE.clear()
        langsmith_traces._TRACE_RATE_LIMIT_UNTIL.clear()
        langsmith_traces._TRACE_INFLIGHT.clear()


def test_first_success_is_live_and_second_success_is_cached(monkeypatch) -> None:
    _reset_trace_state()
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")
    monkeypatch.setattr(langsmith_traces, "_configured", lambda: True)
    calls = 0

    async def fake_run_in_threadpool(function, *args, **kwargs):
        nonlocal calls
        calls += 1
        return {
            "status": "READY",
            "configured": True,
            "project": args[1],
            "days": args[0],
            "generated_at": "2026-08-24T00:00:00+00:00",
            "trace_count": 1,
            "error_rate_pct": 0.0,
            "daily": [],
            "latency": [],
        }

    monkeypatch.setattr(langsmith_traces, "run_in_threadpool", fake_run_in_threadpool)

    first = asyncio.run(langsmith_traces.qa_trace_timeseries(days=2))
    second = asyncio.run(langsmith_traces.qa_trace_timeseries(days=2))

    assert calls == 1
    assert first["status"] == "READY"
    assert first["cached"] is False
    assert first["cache_age_seconds"] == 0.0
    assert first["cache_reason"] is None
    assert second["status"] == "READY"
    assert second["cached"] is True
    assert second["cache_age_seconds"] >= 0.0
    assert second["cache_reason"] == "ttl"


def test_rate_limit_reuses_expired_success_cache(monkeypatch) -> None:
    _reset_trace_state()
    monkeypatch.setenv("LANGSMITH_PROJECT", "First")
    monkeypatch.setattr(langsmith_traces, "_configured", lambda: True)
    cache_key = (2, "First")
    cached_payload = {
        "status": "READY",
        "configured": True,
        "cached": False,
        "project": "First",
        "days": 2,
        "generated_at": "2026-08-24T00:00:00+00:00",
        "trace_count": 7,
        "error_rate_pct": 14.29,
        "daily": [],
        "latency": [],
    }
    with langsmith_traces._TRACE_CACHE_LOCK:
        langsmith_traces._TRACE_CACHE[cache_key] = (
            time.monotonic() - langsmith_traces._TRACE_CACHE_TTL_SECONDS - 1,
            cached_payload,
        )

    class RateLimitError(Exception):
        status_code = 429

    async def raise_rate_limit(function, *args, **kwargs):
        raise RateLimitError("throttled")

    monkeypatch.setattr(langsmith_traces, "run_in_threadpool", raise_rate_limit)

    result = asyncio.run(langsmith_traces.qa_trace_timeseries(days=2))

    assert result["status"] == "READY"
    assert result["cached"] is True
    assert result["trace_count"] == 7
    assert result["cache_reason"] == "rate_limit"
    assert result["cache_age_seconds"] >= 0.0
    assert "last successful aggregate" in result["detail"]
