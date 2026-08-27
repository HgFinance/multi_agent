"""Small SmithDB v2 query adapter shared by LangSmith read paths.

The legacy LangSmith SDK listing helper uses a retired v1 query endpoint. The
runtime still owns synchronous pollers and FastAPI threadpool handlers, while
the SmithDB v2 SDK exposes an awaitable paginator.  This module keeps that
boundary in one place: resolve a project name to its UUID once per process,
query with an explicit time window, and return a bounded list to callers.

Only run metadata selected by the caller is requested.  No run retrieval or
payload fallback is allowed here because the QA and dashboard paths are
metadata-only by contract.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Collection, Mapping, Sequence
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

logger = logging.getLogger(__name__)

_PROJECT_ID_CACHE_TTL_SECONDS = 600.0
_PROJECT_ID_CACHE: dict[str, tuple[float, str]] = {}
_PROJECT_ID_CACHE_LOCK = threading.Lock()


def _http_status_code(error: BaseException) -> int | None:
    """Extract an HTTP status without importing a transport-specific client."""

    current: BaseException | None = error
    visited: set[int] = set()
    for _ in range(3):
        if current is None or id(current) in visited:
            return None
        visited.add(id(current))
        for candidate in (
            getattr(current, "status_code", None),
            getattr(current, "status", None),
            getattr(getattr(current, "response", None), "status_code", None),
        ):
            try:
                if candidate is not None:
                    return int(candidate)
            except (TypeError, ValueError):
                continue
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, BaseException) else None
    return None


def _is_sdk_transport_compatibility_error(error: BaseException) -> bool:
    """Allow REST fallback only for known pre-response SDK incompatibilities.

    A server response, authentication failure, validation error, timeout, or
    socket failure may already have reached LangSmith. Retrying it through a
    second client can duplicate the request and doubles query latency. The
    fallback exists only for the deployed SDK's pre-response async adapter
    failures (httpx/anyio/paginator shape problems).
    """

    if _http_status_code(error) is not None:
        return False
    chain: list[BaseException] = []
    current: BaseException | None = error
    visited: set[int] = set()
    while isinstance(current, BaseException) and id(current) not in visited:
        visited.add(id(current))
        chain.append(current)
        next_error = current.__cause__ or current.__context__
        current = next_error if isinstance(next_error, BaseException) else None

    # The SDK wraps pre-response httpx/transport failures in
    # ``APIConnectionError``.  Looking only at the outer RuntimeError misses
    # that wrapper and incorrectly reports an existing trace as absent. A raw
    # socket/timeout error remains non-retryable; only the SDK wrapper gets the
    # direct v2 fallback.
    class_names = {type(item).__name__.casefold() for item in chain}
    messages = " ".join(str(item) for item in chain).casefold()
    if "apiconnectionerror" in class_names:
        return True
    if isinstance(error, (OSError, TimeoutError, ConnectionError)):
        return False
    if isinstance(error, (TypeError, AttributeError, NotImplementedError)):
        return True
    if not isinstance(error, RuntimeError):
        return False
    return any(
        marker in messages
        for marker in (
            "async",
            "anyio",
            "httpx",
            "paginator",
            "transport",
            "coroutine",
            "event loop",
        )
    )


def _cache_key(client: Any, project_name: str) -> str:
    # The endpoint is non-secret and prevents a long-lived process that talks
    # to more than one workspace from reusing the wrong UUID.
    endpoint = str(getattr(client, "api_url", "") or "").strip()
    return f"{endpoint}|{project_name}"


def _cached_project_id(client: Any, project_name: str) -> str | None:
    cache_key = _cache_key(client, project_name)
    now = time.monotonic()
    with _PROJECT_ID_CACHE_LOCK:
        cached = _PROJECT_ID_CACHE.get(cache_key)
        if cached is None:
            return None
        expires_at, project_id = cached
        if expires_at <= now:
            _PROJECT_ID_CACHE.pop(cache_key, None)
            return None
        return project_id


def _cache_project_id(client: Any, project_name: str, project_id: str) -> None:
    cache_key = _cache_key(client, project_name)
    with _PROJECT_ID_CACHE_LOCK:
        _PROJECT_ID_CACHE[cache_key] = (
            time.monotonic() + _PROJECT_ID_CACHE_TTL_SECONDS,
            project_id,
        )


async def _resolve_project_id(client: Any, project_name: str) -> str:
    name = str(project_name or "").strip()
    if not name:
        raise ValueError("langsmith_project_name_required")
    cached = _cached_project_id(client, name)
    if cached:
        return cached

    reader = getattr(client, "aread_project", None)
    if not callable(reader):
        raise RuntimeError(  # noqa: TRY004 - SDK capability, not caller input.
            "langsmith_sdk_missing_aread_project"
        )
    try:
        project = await reader(project_name=name)
    except Exception as exc:
        # langsmith 0.11.x can load a workspace through the synchronous
        # client while its generated async transport fails under some
        # httpx2/anyio combinations. Project listing is not a run-query path;
        # use it only to resolve the UUID and keep the v2 query boundary below.
        if not _is_sdk_transport_compatibility_error(exc):
            raise

        def _read_sync() -> Any:
            projects = list(client.list_projects(name=name, limit=1))
            return projects[0] if projects else None

        project = await asyncio.to_thread(_read_sync)
    if project is None:
        raise RuntimeError("langsmith_project_not_found")
    project_id = str(getattr(project, "id", "") or "").strip()
    if not project_id:
        raise RuntimeError("langsmith_project_id_missing")
    _cache_project_id(client, name, project_id)
    return project_id


def _api_url(client: Any, path: str) -> str:
    base = str(
        os.getenv("LANGSMITH_ENDPOINT", "").strip()
        or getattr(client, "api_url", "")
        or "https://api.smith.langchain.com"
    ).rstrip("/")
    if base.endswith("/api/v2"):
        return f"{base}{path}"
    if base.endswith("/api"):
        return f"{base}/v2{path}"
    return f"{base}/api/v2{path}"


def _as_datetime(value: Any) -> Any:
    if not isinstance(value, str) or not value:
        return value
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _run_record(item: Any) -> Any:
    if not isinstance(item, dict):
        return item
    values = {str(key).lower(): value for key, value in item.items()}
    for key in ("start_time", "end_time"):
        if key in values:
            values[key] = _as_datetime(values[key])
    return SimpleNamespace(**values)


def _direct_v2_query(
    client: Any,
    kwargs: dict[str, Any],
    *,
    max_results: int,
) -> list[Any]:
    """Use the documented v2 REST route when generated async transport is broken."""

    api_key = os.getenv("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("langsmith_api_key_missing")
    payload = dict(kwargs)
    cursor: str | None = None
    rows: list[Any] = []
    while len(rows) < max(1, int(max_results)):
        if cursor:
            payload["cursor"] = cursor
        encoded = json.dumps(
            payload,
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
            separators=(",", ":"),
        ).encode("utf-8")
        request = urllib.request.Request(
            _api_url(client, "/runs/query"),
            data=encoded,
            headers={
                "X-API-KEY": api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                decoded = json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"langsmith_v2_query_http_{exc.code}") from exc
        if not isinstance(decoded, dict):
            raise RuntimeError(  # noqa: TRY004 - remote contract violation.
                "langsmith_v2_query_non_object_response"
            )
        items = decoded.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError(  # noqa: TRY004 - remote contract violation.
                "langsmith_v2_query_items_invalid"
            )
        rows.extend(_run_record(item) for item in items)
        if len(rows) >= max(1, int(max_results)):
            break
        cursor = str(decoded.get("next_cursor") or "") or None
        if not cursor or not items:
            break
    return rows[: max(1, int(max_results))]


async def _query_runs_async_impl(
    client: Any,
    *,
    project_name: str,
    min_start_time: datetime,
    max_start_time: datetime | None,
    is_root: bool | None,
    filter_expression: str | None,
    page_size: int,
    max_results: int,
    selects: Sequence[str],
) -> list[Any]:
    project_id = await _resolve_project_id(client, project_name)
    query = getattr(getattr(client, "runs", None), "query", None)
    if not callable(query):
        raise RuntimeError(  # noqa: TRY004 - SDK capability, not caller input.
            "langsmith_sdk_missing_smithdb_query"
        )

    kwargs: dict[str, Any] = {
        "project_ids": [project_id],
        "min_start_time": min_start_time,
        "page_size": max(1, min(int(page_size), 100)),
        "selects": [str(value).upper() for value in selects],
    }
    if max_start_time is not None:
        kwargs["max_start_time"] = max_start_time
    if is_root is not None:
        kwargs["is_root"] = is_root
    if filter_expression:
        kwargs["filter"] = filter_expression

    # SmithDB v2 returns an awaitable async paginator. Await it first so the
    # normal SDK path never touches the retired v1 compatibility layer.
    try:
        paginator = await query(**kwargs)
        results: list[Any] = []
        async for run in paginator:
            results.append(run)
            if len(results) >= max(1, int(max_results)):
                break
        return results
    except Exception as exc:
        # Some deployed langsmith/httpx2 combinations fail before an HTTP
        # request is made. The fallback is still exactly /api/v2/runs/query;
        # it never falls back to a retired SDK compatibility path.
        if not _is_sdk_transport_compatibility_error(exc):
            raise
        return await asyncio.to_thread(
            _direct_v2_query,
            client,
            kwargs,
            max_results=max_results,
        )


async def _close_query_transport_async(client: Any) -> None:
    """Close the generated async SDK transport on the loop that owns it."""

    api = getattr(client, "_langsmith_api", None)
    closer = getattr(api, "close", None)
    if not callable(closer):
        return
    result = closer()
    if inspect.isawaitable(result):
        await result


async def _release_query_transport_async(client: Any) -> None:
    """Best-effort close and detach one loop-bound generated transport."""

    api = getattr(client, "_langsmith_api", None)
    try:
        await _close_query_transport_async(client)
    except Exception as exc:  # noqa: BLE001 - cleanup is fail-open by contract.
        # Query success/failure must not be replaced by a best-effort
        # observability transport cleanup error.
        logger.debug(
            "langsmith_query_transport_close_failed error=%s",
            type(exc).__name__,
        )
    if api is not None and getattr(client, "_langsmith_api", None) is api:
        try:
            client._langsmith_api = None
        except Exception as exc:  # noqa: BLE001 - test doubles may be immutable.
            logger.debug(
                "langsmith_query_transport_detach_failed error=%s",
                type(exc).__name__,
            )


def close_query_client(client: Any) -> None:
    """Release a query client's generated async transport without leaking loops.

    ``Client.runs.query`` lazily creates an async HTTPX client, while this
    repository's callers are synchronous. Callers invoke this once after all
    queries using that client; the helper is a no-op for test doubles and SDKs
    that do not expose the generated transport.
    """

    api = getattr(client, "_langsmith_api", None)
    if callable(getattr(api, "close", None)):
        try:
            asyncio.run(_release_query_transport_async(client))
        except Exception as exc:  # noqa: BLE001 - cleanup is fail-open by contract.
            # Cleanup must never turn an already completed metadata read into a
            # workflow failure. The SDK's own destructor remains the last resort.
            logger.debug(
                "langsmith_query_client_release_failed error=%s",
                type(exc).__name__,
            )
    closer = getattr(client, "close", None)
    if callable(closer):
        try:
            closer(timeout=0)
        except TypeError:
            try:
                closer()
            except Exception as exc:  # noqa: BLE001 - cleanup is fail-open.
                logger.debug(
                    "langsmith_query_client_close_failed error=%s",
                    type(exc).__name__,
                )
                return
        except Exception as exc:  # noqa: BLE001 - cleanup is fail-open.
            logger.debug(
                "langsmith_query_client_close_failed error=%s",
                type(exc).__name__,
            )
            return


async def _query_runs_async(
    client: Any,
    *,
    project_name: str,
    min_start_time: datetime,
    max_start_time: datetime | None,
    is_root: bool | None,
    filter_expression: str | None,
    page_size: int,
    max_results: int,
    selects: Sequence[str],
) -> list[Any]:
    """Run one query and close its generated transport on the same event loop."""

    try:
        return await _query_runs_async_impl(
            client,
            project_name=project_name,
            min_start_time=min_start_time,
            max_start_time=max_start_time,
            is_root=is_root,
            filter_expression=filter_expression,
            page_size=page_size,
            max_results=max_results,
            selects=selects,
        )
    finally:
        # Feedback and retention may issue multiple project queries through
        # one Client. Force the SDK to create the next transport on the next
        # query's event loop instead of reusing a closed loop-bound client.
        await _release_query_transport_async(client)


def query_runs(
    client: Any,
    *,
    project_name: str,
    min_start_time: datetime,
    max_start_time: datetime | None = None,
    is_root: bool | None = None,
    filter_expression: str | None = None,
    page_size: int = 100,
    max_results: int = 100,
    selects: Sequence[str] = (),
) -> list[Any]:
    """Query bounded metadata through the SmithDB v2 SDK.

    Callers in this repository are synchronous background/threadpool paths,
    so the SDK's async paginator is bridged exactly once here.  A running
    event loop is intentionally not hidden with nested loop machinery; async
    callers should use the private coroutine boundary directly instead of
    reintroducing a second SDK adapter.
    """

    return asyncio.run(
        _query_runs_async(
            client,
            project_name=project_name,
            min_start_time=min_start_time,
            max_start_time=max_start_time,
            is_root=is_root,
            filter_expression=filter_expression,
            page_size=page_size,
            max_results=max_results,
            selects=selects,
        )
    )


def resolve_project_id(client: Any, project_name: str) -> str:
    """Resolve one configured project name through the same bounded cache."""

    async def _resolve_and_release() -> str:
        try:
            return await _resolve_project_id(client, project_name)
        finally:
            await _release_query_transport_async(client)

    return asyncio.run(_resolve_and_release())


def query_correlated_trace_metadata(
    *,
    correlation_ids: Collection[str],
    min_start_time: datetime,
    max_start_time: datetime | None = None,
    project_name: str | None = None,
    max_results: int = 300,
) -> dict[str, Any]:
    """Read a bounded, metadata-only LangSmith workflow slice.

    This is the single shared reader for QA's trace evidence.  Correlation is
    performed locally against the allowlisted ``root_id``, ``task_id``,
    ``request_id`` and ``trace_id`` metadata fields; inputs, outputs and
    reasoning are never selected or returned.
    """

    normalized_ids = {
        str(value).strip().casefold()
        for value in correlation_ids
        if str(value).strip()
    }
    if not normalized_ids:
        return {
            "status": "UNAVAILABLE",
            "metadata_only": True,
            "raw_payloads_sent": False,
            "error_code": "correlation_ids_missing",
            "trace_count": 0,
            "traces": [],
        }
    if str(os.getenv("LANGSMITH_TRACING", "")).casefold() not in {
        "1",
        "true",
        "yes",
        "on",
    } or not os.getenv("LANGSMITH_API_KEY", "").strip():
        return {
            "status": "UNAVAILABLE",
            "metadata_only": True,
            "raw_payloads_sent": False,
            "error_code": "langsmith_not_configured",
            "trace_count": 0,
            "traces": [],
        }

    client: Any | None = None
    try:
        from langsmith import Client

        client = Client(
            hide_inputs=True,
            hide_outputs=True,
            hide_metadata=False,
            omit_traced_runtime_info=True,
        )
        runs = query_runs(
            client,
            project_name=(
                str(project_name or os.getenv("LANGSMITH_PROJECT") or "First").strip()
                or "First"
            ),
            min_start_time=min_start_time,
            max_start_time=max_start_time,
            is_root=None,
            page_size=min(max(int(max_results), 1), 100),
            max_results=min(max(int(max_results), 1), 500),
            selects=["ID", "NAME", "STATUS", "START_TIME", "END_TIME", "EXTRA"],
        )
    except Exception as exc:  # noqa: BLE001 - QA evidence is fail-open.
        return {
            "status": "UNAVAILABLE",
            "metadata_only": True,
            "raw_payloads_sent": False,
            "error_code": type(exc).__name__,
            "trace_count": 0,
            "traces": [],
        }
    finally:
        if client is not None:
            close_query_client(client)

    traces: list[dict[str, Any]] = []
    seen: set[str] = set()
    correlation_keys = ("root_id", "root_task_id", "task_id", "request_id", "trace_id")
    for run in runs:
        extra = getattr(run, "extra", None) or {}
        metadata = extra.get("metadata") or {}
        if not isinstance(metadata, Mapping):
            metadata = {}
        matched = any(
            str(metadata.get(key) or "").strip().casefold() in normalized_ids
            for key in correlation_keys
        )
        if not matched:
            continue
        run_id = str(getattr(run, "id", "") or "").strip()
        if not run_id or run_id in seen:
            continue
        seen.add(run_id)
        started = getattr(run, "start_time", None)
        ended = getattr(run, "end_time", None)
        latency_ms = metadata.get("latency_ms")
        if latency_ms is None and started is not None and ended is not None:
            try:
                latency_ms = max(0, int((ended - started).total_seconds() * 1000))
            except (AttributeError, TypeError, ValueError):
                latency_ms = None
        traces.append(
            {
                "run_id": run_id,
                "name": str(getattr(run, "name", "") or "")[:120],
                "stage": str(metadata.get("stage") or "")[:64],
                "department": str(
                    metadata.get("department") or metadata.get("profile") or ""
                )[:64],
                "status": str(metadata.get("status") or getattr(run, "status", "") or "")[:32],
                "error_code": str(
                    metadata.get("error_code") or metadata.get("error_class") or ""
                )[:80]
                or None,
                "latency_ms": latency_ms if isinstance(latency_ms, (int, float)) else None,
                "attempts": metadata.get("attempts"),
                "retries": metadata.get("retries"),
                "llm_calls": metadata.get("llm_calls"),
                "tool_calls": metadata.get("tool_calls"),
                "tool_error_count": metadata.get("tool_error_count"),
                "trace_kind": str(metadata.get("trace_kind") or "")[:80],
                "observation_unit": str(metadata.get("observation_unit") or "")[:32],
                "task_id": str(metadata.get("task_id") or "")[:160],
                "root_id": str(metadata.get("root_id") or "")[:160],
                "request_id": str(metadata.get("request_id") or "")[:160],
                "raw_payloads_sent": metadata.get("raw_payloads_sent") is True,
            }
        )
        if len(traces) >= 100:
            break

    return {
        "status": "READY" if traces else "NOT_FOUND",
        "metadata_only": True,
        "raw_payloads_sent": False,
        "trace_count": len(traces),
        "department_count": len({item["department"] for item in traces if item["department"]}),
        "stages": sorted({item["stage"] for item in traces if item["stage"]}),
        "traces": traces,
    }


__all__ = [
    "close_query_client",
    "query_correlated_trace_metadata",
    "query_runs",
    "resolve_project_id",
]
