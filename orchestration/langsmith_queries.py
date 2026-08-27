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
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from typing import Any, Sequence


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
    if isinstance(error, (OSError, TimeoutError, ConnectionError)):
        return False
    if isinstance(error, (TypeError, AttributeError, NotImplementedError)):
        return True
    if not isinstance(error, RuntimeError):
        return False
    message = str(error).casefold()
    return any(
        marker in message
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
        raise RuntimeError("langsmith_sdk_missing_aread_project")
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
            raise RuntimeError("langsmith_v2_query_non_object_response")
        items = decoded.get("items") or []
        if not isinstance(items, list):
            raise RuntimeError("langsmith_v2_query_items_invalid")
        rows.extend(_run_record(item) for item in items)
        if len(rows) >= max(1, int(max_results)):
            break
        cursor = str(decoded.get("next_cursor") or "") or None
        if not cursor or not items:
            break
    return rows[: max(1, int(max_results))]


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
    project_id = await _resolve_project_id(client, project_name)
    query = getattr(getattr(client, "runs", None), "query", None)
    if not callable(query):
        raise RuntimeError("langsmith_sdk_missing_smithdb_query")

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

    return asyncio.run(_resolve_project_id(client, project_name))


__all__ = ["query_runs", "resolve_project_id"]
