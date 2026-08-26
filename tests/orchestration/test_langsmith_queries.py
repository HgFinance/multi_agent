from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from orchestration.langsmith_queries import query_runs


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
