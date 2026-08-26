"""Legacy read-only QA trace timeseries for the department card.

이 모듈은 dashboard compatibility reader다. feedback evaluator, QA approval
ledger, offline benchmark gate, CEO advisory의 source가 아니다. 새 평가 흐름은
``orchestration.langsmith_feedback``가 담당한다. 자격증명과 raw payload는
읽거나 출력하지 않는다.

실제 Worker LangGraph root run은 `LANGSMITH_PROJECT`(production `First`)에
남고, `orchestration/llm_observability.py`의 기존 `publish_metric()` 요약 핑은
고빈도 성능 데이터이므로 `LANGSMITH_METRICS_PROJECT`(기본
`HgFinance-Metrics`)로 분리된다. 따라서 이 QA 화면은 metric ping이 아니라
`First`에 있는 실제 `stage=qa` Worker trace를 읽는다(prompt/output은 절대
전송하지 않는다). run 자체의 `tags`는 실측(2026-08-24) 결과
비어 있고 - `redacted_trace()`가 여는 `tracing_context`의 태그가 LangGraph 자체
root run까지 전파되지 않는다 - 부서 구분은 오직 `extra.metadata.stage`에만 있다.
그래서 여기서는 `stage:qa` 태그가 아니라 이 metadata 필드로 판정한다.

같은 이유로 run 자체의 `error` 필드도 쓰지 않는다 - `publish_metric()`은
`create_run()`에 `error=`를 넘기지 않으므로 항상 비어 있다. 실패 여부는
`extra.metadata.status`(AgentLogsView.tsx의 `degraded` 판정과 같은 집합:
DEGRADED/BLOCKED/ERROR)와 `error_count`로 판정한다.

2026-08-24 정정: 위 root run 전파 문제는 각 부서 Worker의 실제 LangGraph
실행 자체(`departments/employee_worker_runtime.py`, `.../qa_employee_workers.py`,
`.../risk_employee_workers.py`의 `invoke()`/`ainvoke()`)도 `config=` 없이 맨 호출
이라 root run에 `stage`가 전혀 안 붙고 있었다 - 실측 결과 프로젝트 root run의
91%가 이름 `LangGraph`·tags 없음·metadata 없음이었고, 그래서 이 화면의 Trace
Count가 실제 QA 실행량의 1%도 못 세고 있었다. 지금은 Worker 그래프
invoke 자체가 `extra.metadata.stage`를 남기므로
(`orchestration/llm_observability.py`의 `worker_graph_trace_config()`), 아래
`_QA_STAGE` 필터가 실제 실행량을 온전히 잡는다.

LangSmith는 선택적 추적 어댑터다(`docs/02-engineering/TECH_STACK_DECISIONS.md`).
자격증명이 없거나 API 호출이 실패해도 이 모듈은 예외를 던지지 않고 상태 문자열로만
알린다 - 관측 실패가 카드 렌더를 막으면 안 된다.
"""

from __future__ import annotations

import os
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from starlette.concurrency import run_in_threadpool

from orchestration.langsmith_queries import query_runs

_QA_STAGE = "qa"
# AgentLogsView.tsx의 `degraded` 판정과 같은 집합 - 화면 전체에서 "실패"의 뜻을
# 하나로 맞춘다.
_ERROR_STATUSES = {"DEGRADED", "BLOCKED", "ERROR"}
_MAX_DAYS = 30
_MAX_RUNS = 3000
_TRACE_CACHE_TTL_SECONDS = 120.0
_TRACE_RATE_LIMIT_BACKOFF_SECONDS = 300.0
_TRACE_CACHE: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_TRACE_RATE_LIMIT_UNTIL: dict[tuple[int, str], float] = {}
_TRACE_INFLIGHT: set[tuple[int, str]] = set()
_TRACE_CACHE_LOCK = threading.Lock()


def _configured() -> bool:
    tracing = os.getenv("LANGSMITH_TRACING", "").casefold() in {"1", "true", "yes", "on"}
    return tracing and bool(os.getenv("LANGSMITH_API_KEY", "").strip())


def _percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    rank = (len(ordered) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    if lower == upper:
        return round(ordered[lower], 3)
    interpolated = ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)
    return round(interpolated, 3)


def _day_range(days: int) -> list[str]:
    today = datetime.now(timezone.utc).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def _is_error(metadata: dict[str, Any]) -> bool:
    status = str(metadata.get("status") or "").upper()
    if status in _ERROR_STATUSES:
        return True
    error_count = metadata.get("error_count")
    return isinstance(error_count, (int, float)) and error_count > 0


def _latency_seconds(metadata: dict[str, Any], started: datetime, ended: datetime | None) -> float | None:
    # publish_metric()이 이미 계산해 보낸 값을 우선 쓴다 - 이 run 자체는 실행이
    # 끝난 뒤에야 사후 기록되므로(redacted metric), start/end_time 차이가 실제
    # Worker 지연시간과 다를 수 있다.
    latency_ms = metadata.get("latency_ms")
    if isinstance(latency_ms, (int, float)) and latency_ms > 0:
        return latency_ms / 1000
    if ended is not None:
        return (ended - started).total_seconds()
    return None


def _collect(days: int, project: str | None) -> dict[str, Any]:
    from langsmith import Client

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=days)
    client = Client()
    # SmithDB v2 requires a project UUID and an explicit time window. The
    # adapter resolves the configured name once per process and enforces the
    # server page-size bound; the total result cap remains local.
    # 부서 구분이 태그가 아니라 metadata에 있어서(머리말) 서버 필터를 걸 수 없고,
    # root run을 받아 이 안에서 stage=="qa"만 추린다.
    from orchestration.llm_observability import langsmith_project

    runs = query_runs(
        client,
        project_name=project or langsmith_project("workflow") or "First",
        min_start_time=since,
        max_start_time=now,
        is_root=True,
        page_size=100,
        max_results=_MAX_RUNS,
        selects=["START_TIME", "END_TIME", "EXTRA"],
    )

    by_day: dict[str, dict[str, Any]] = defaultdict(lambda: {"success": 0, "error": 0, "latencies": []})
    for seen, run in enumerate(runs):
        if seen >= _MAX_RUNS:
            break
        extra = getattr(run, "extra", None) or {}
        metadata = extra.get("metadata") or {}
        if metadata.get("stage") != _QA_STAGE:
            continue
        started = getattr(run, "start_time", None)
        if started is None:
            continue
        bucket = by_day[started.date().isoformat()]
        if _is_error(metadata):
            bucket["error"] += 1
        else:
            bucket["success"] += 1
        latency = _latency_seconds(metadata, started, getattr(run, "end_time", None))
        if latency is not None:
            bucket["latencies"].append(latency)

    date_keys = _day_range(days)
    daily = [
        {"date": day, "success": by_day[day]["success"], "error": by_day[day]["error"]}
        for day in date_keys
    ]
    latency = [
        {
            "date": day,
            "p50_seconds": _percentile(by_day[day]["latencies"], 0.5),
            "p99_seconds": _percentile(by_day[day]["latencies"], 0.99),
        }
        for day in date_keys
    ]
    total = sum(row["success"] + row["error"] for row in daily)
    total_error = sum(row["error"] for row in daily)

    return {
        "status": "READY",
        "configured": True,
        "project": project,
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_count": total,
        "error_rate_pct": round((total_error / total) * 100, 2) if total else 0.0,
        "daily": daily,
        "latency": latency,
    }


def _empty(status: str, days: int, *, configured: bool, detail: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "configured": configured,
        "cached": False,
        "project": os.getenv("LANGSMITH_PROJECT") or None,
        "days": days,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "trace_count": 0,
        "error_rate_pct": None,
        "daily": [{"date": day, "success": 0, "error": 0} for day in _day_range(days)],
        "latency": [{"date": day, "p50_seconds": None, "p99_seconds": None} for day in _day_range(days)],
    }
    if detail:
        payload["detail"] = detail
    return payload


def _cached_response(
    payload: dict[str, Any],
    cached_at: float,
    *,
    reason: str,
    detail: str | None = None,
) -> dict[str, Any]:
    """Annotate a reused aggregate without mutating the shared cache value."""

    result = {
        **payload,
        "cached": True,
        "cache_age_seconds": round(max(0.0, time.monotonic() - cached_at), 1),
        "cache_reason": reason,
    }
    if detail:
        result["detail"] = detail
    return result


def _is_rate_limited(exc: Exception) -> bool:
    """Recognize LangSmith throttling without depending on one SDK version."""

    status_code = getattr(exc, "status_code", None)
    if str(status_code) == "429":
        return True
    response = getattr(exc, "response", None)
    if str(getattr(response, "status_code", None)) == "429":
        return True
    text = f"{type(exc).__name__} {exc}".casefold()
    return "ratelimit" in text or "rate_limit" in text or "too many requests" in text


async def qa_trace_timeseries(days: int = 7) -> dict[str, Any]:
    """`metadata.stage == "qa"`인 LangSmith root run을 날짜별로 집계한다."""

    days = max(1, min(int(days), _MAX_DAYS))
    if not _configured():
        return _empty("NOT_CONFIGURED", days, configured=False)
    project = os.getenv("LANGSMITH_PROJECT") or ""
    cache_key = (days, project)
    now = time.monotonic()
    with _TRACE_CACHE_LOCK:
        cached = _TRACE_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < _TRACE_CACHE_TTL_SECONDS:
            return _cached_response(cached[1], cached[0], reason="ttl")
        if now < _TRACE_RATE_LIMIT_UNTIL.get(cache_key, 0.0):
            if cached is not None:
                return _cached_response(
                    cached[1],
                    cached[0],
                    reason="rate_limit",
                    detail="LangSmith rate limit backoff; serving the last successful aggregate",
                )
            return _empty("DEGRADED", days, configured=True, detail="LangSmith rate limit backoff")
        if cache_key in _TRACE_INFLIGHT:
            if cached is not None:
                return _cached_response(
                    cached[1],
                    cached[0],
                    reason="inflight",
                    detail="LangSmith aggregate query in progress",
                )
            return _empty("DEGRADED", days, configured=True, detail="LangSmith aggregate query in progress")
        _TRACE_INFLIGHT.add(cache_key)
    try:
        result = await run_in_threadpool(_collect, days, project or None)
        # Make the cache contract explicit for the first successful response.
        # This lets the UI distinguish a live aggregate from a reused one
        # without inferring it from ``generated_at``.
        result = {**result, "cached": False, "cache_age_seconds": 0.0, "cache_reason": None}
        with _TRACE_CACHE_LOCK:
            _TRACE_CACHE[cache_key] = (time.monotonic(), result)
            _TRACE_RATE_LIMIT_UNTIL.pop(cache_key, None)
        return result
    except Exception as exc:  # noqa: BLE001 - 관측 실패가 카드 렌더를 막으면 안 된다
        detail = type(exc).__name__
        is_rate_limited = _is_rate_limited(exc)
        with _TRACE_CACHE_LOCK:
            if is_rate_limited:
                _TRACE_RATE_LIMIT_UNTIL[cache_key] = time.monotonic() + _TRACE_RATE_LIMIT_BACKOFF_SECONDS
            cached = _TRACE_CACHE.get(cache_key)
        if cached is not None:
            return _cached_response(
                cached[1],
                cached[0],
                reason="rate_limit" if is_rate_limited else "error",
                detail=f"{detail}; serving the last successful aggregate",
            )
        return _empty("DEGRADED" if is_rate_limited else "ERROR", days, configured=True, detail=detail)
    finally:
        with _TRACE_CACHE_LOCK:
            _TRACE_INFLIGHT.discard(cache_key)


__all__ = ["qa_trace_timeseries"]
