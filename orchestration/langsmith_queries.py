"""Small SmithDB v2 query adapter shared by LangSmith read paths.

LangSmith's ``Client.list_runs`` uses the retired v1 query endpoint.  The
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
import threading
import time
from datetime import datetime
from typing import Any, Sequence


_PROJECT_ID_CACHE_TTL_SECONDS = 600.0
_PROJECT_ID_CACHE: dict[str, tuple[float, str]] = {}
_PROJECT_ID_CACHE_LOCK = threading.Lock()


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
    project = await reader(project_name=name)
    project_id = str(getattr(project, "id", "") or "").strip()
    if not project_id:
        raise RuntimeError("langsmith_project_id_missing")
    _cache_project_id(client, name, project_id)
    return project_id


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

    # SmithDB v2 returns an awaitable async paginator.  Awaiting it is
    # required before consuming pages and avoids the v1 compatibility layer.
    paginator = await query(**kwargs)
    results: list[Any] = []
    async for run in paginator:
        results.append(run)
        if len(results) >= max(1, int(max_results)):
            break
    return results


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
