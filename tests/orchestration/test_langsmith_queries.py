from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from orchestration import langsmith_queries
from orchestration.langsmith_queries import (
    close_query_client,
    query_correlated_trace_metadata,
    query_runs,
)


class _Paginator:
    def __init__(self, rows):
        self.rows = rows

    def __aiter__(self):
        self._iterator = iter(self.rows)
        return self

    async def __anext__(self):
        try:
            return next(self._iterator)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _Runs:
    def __init__(self, owner):
        self.owner = owner

    async def query(self, **kwargs):
        self.owner.query_calls.append(kwargs)
        return _Paginator([SimpleNamespace(id="run-1"), SimpleNamespace(id="run-2")])


class _Client:
    def __init__(self):
        self.project_reads = 0
        self.query_calls = []
        self.runs = _Runs(self)

    async def aread_project(self, *, project_name):
        self.project_reads += 1
        return SimpleNamespace(id=f"project-{project_name}")


def test_query_runs_uses_smithdb_v2_and_bounds_results() -> None:
    client = _Client()
    start = datetime(2026, 8, 26, tzinfo=timezone.utc)
    rows = query_runs(
        client,
        project_name="Test-SmithDB",
        min_start_time=start,
        max_start_time=start,
        is_root=True,
        filter_expression='eq(status, "success")',
        page_size=999,
        max_results=1,
        selects=["id", "extra"],
    )

    assert [row.id for row in rows] == ["run-1"]
    call = client.query_calls[0]
    assert call["project_ids"] == ["project-Test-SmithDB"]
    assert call["min_start_time"] == start
    assert call["max_start_time"] == start
    assert call["is_root"] is True
    assert call["filter"] == 'eq(status, "success")'
    assert call["page_size"] == 100
    assert call["selects"] == ["ID", "EXTRA"]


def test_query_runs_caches_project_uuid() -> None:
    client = _Client()
    start = datetime(2026, 8, 26, tzinfo=timezone.utc)

    query_runs(client, project_name="Cached-SmithDB", min_start_time=start, max_results=1)
    query_runs(client, project_name="Cached-SmithDB", min_start_time=start, max_results=1)

    assert client.project_reads == 1


class _HttpStatusError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"http {status_code}")
        self.status_code = status_code


class _FailingRuns:
    def __init__(self, error):
        self.error = error
        self.query_calls = 0

    async def query(self, **_kwargs):
        self.query_calls += 1
        raise self.error


class _ErrorClient(_Client):
    def __init__(self, error):
        super().__init__()
        self.runs = _FailingRuns(error)


def test_query_runs_does_not_rest_fallback_after_http_error(monkeypatch) -> None:
    client = _ErrorClient(_HttpStatusError(422))
    monkeypatch.setattr(
        langsmith_queries,
        "_direct_v2_query",
        lambda *_args, **_kwargs: pytest.fail("HTTP validation must not be retried"),
    )

    with pytest.raises(_HttpStatusError):
        query_runs(
            client,
            project_name="No-Duplicate-Query",
            min_start_time=datetime(2026, 8, 26, tzinfo=timezone.utc),
            max_results=1,
        )


def test_query_runs_keeps_rest_fallback_for_async_transport_compatibility(
    monkeypatch,
) -> None:
    client = _ErrorClient(RuntimeError("async paginator transport unavailable"))
    monkeypatch.setattr(
        langsmith_queries,
        "_direct_v2_query",
        lambda *_args, **_kwargs: [SimpleNamespace(id="rest-run")],
    )

    rows = query_runs(
        client,
        project_name="Transport-Fallback",
        min_start_time=datetime(2026, 8, 26, tzinfo=timezone.utc),
        max_results=1,
    )

    assert [row.id for row in rows] == ["rest-run"]


def test_query_runs_falls_back_for_pre_response_api_connection_error(monkeypatch) -> None:
    class APIConnectionError(Exception):
        pass

    client = _ErrorClient(APIConnectionError("async transport could not connect"))
    monkeypatch.setattr(
        langsmith_queries,
        "_direct_v2_query",
        lambda *_args, **_kwargs: [SimpleNamespace(id="rest-run")],
    )

    rows = query_runs(
        client,
        project_name="API-Connection-Fallback",
        min_start_time=datetime(2026, 8, 26, tzinfo=timezone.utc),
        max_results=1,
    )

    assert [row.id for row in rows] == ["rest-run"]


def test_query_runs_falls_back_for_wrapped_api_connection_error(monkeypatch) -> None:
    class APIConnectionError(Exception):
        pass

    wrapped = APIConnectionError("request failed")
    wrapped.__cause__ = OSError("httpx transport closed")
    client = _ErrorClient(wrapped)
    monkeypatch.setattr(
        langsmith_queries,
        "_direct_v2_query",
        lambda *_args, **_kwargs: [SimpleNamespace(id="wrapped-rest-run")],
    )

    rows = query_runs(
        client,
        project_name="Wrapped-API-Connection-Fallback",
        min_start_time=datetime(2026, 8, 26, tzinfo=timezone.utc),
        max_results=1,
    )

    assert [row.id for row in rows] == ["wrapped-rest-run"]


def test_close_query_client_closes_generated_async_transport() -> None:
    class _AsyncTransport:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    class _ClientWithTransport:
        def __init__(self):
            self._langsmith_api = _AsyncTransport()

    client = _ClientWithTransport()
    transport = client._langsmith_api

    close_query_client(client)

    assert transport.closed is True
    assert client._langsmith_api is None


def test_correlated_trace_metadata_is_bounded_and_redacted(monkeypatch) -> None:
    class _Rows:
        async def query(self, **_kwargs):
            return _Paginator(
                [
                    SimpleNamespace(
                        id="trace-1",
                        name="research.worker",
                        status="success",
                        start_time=datetime(2026, 8, 27, tzinfo=timezone.utc),
                        end_time=datetime(2026, 8, 27, 0, 0, 2, tzinfo=timezone.utc),
                        extra={
                            "metadata": {
                                "root_id": "t_root",
                                "task_id": "t_research",
                                "request_id": "req-1",
                                "stage": "research",
                                "department": "research",
                                "status": "COMPLETED",
                                "latency_ms": 2000,
                                "tool_error_count": 0,
                                "raw_payloads_sent": False,
                            }
                        },
                    ),
                    SimpleNamespace(
                        id="trace-foreign",
                        name="foreign.worker",
                        extra={"metadata": {"root_id": "t_other"}},
                    ),
                ]
            )

    client = _Client()
    client.runs = _Rows()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
    monkeypatch.setattr("langsmith.Client", lambda **_kwargs: client)

    result = query_correlated_trace_metadata(
        correlation_ids=("t_root", "t_research"),
        min_start_time=datetime(2026, 8, 27, tzinfo=timezone.utc),
        project_name="Correlated-Metadata-Test",
        max_results=999,
    )

    assert result["status"] == "READY"
    assert result["trace_count"] == 1
    assert result["department_count"] == 1
    assert result["traces"][0]["task_id"] == "t_research"
    assert "inputs" not in result["traces"][0]
    assert "outputs" not in result["traces"][0]


def test_correlated_trace_metadata_fails_open_when_langsmith_is_unconfigured(monkeypatch) -> None:
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)

    result = query_correlated_trace_metadata(
        correlation_ids=("t_root",),
        min_start_time=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )

    assert result["status"] == "UNAVAILABLE"
    assert result["error_code"] == "langsmith_not_configured"
