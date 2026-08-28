from __future__ import annotations

import io
import json
import sys
import urllib.error
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import orchestration.langsmith_retention as retention
from orchestration.langsmith_retention import (
    LangSmithRetentionRateLimited,
    LangSmithRetentionWorker,
)


@pytest.fixture(autouse=True)
def _clear_process_retention_scope(monkeypatch) -> None:
    """Keep repository defaults independent from a developer's .env file."""

    monkeypatch.delenv("LANGSMITH_RETENTION_SCOPES", raising=False)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b"{}"


def _fake_client_module(monkeypatch):
    class Client:
        def __init__(self, **_kwargs):
            pass

    monkeypatch.setitem(sys.modules, "langsmith", SimpleNamespace(Client=Client))


def test_retention_dry_run_targets_only_named_projects(monkeypatch) -> None:
    _fake_client_module(monkeypatch)
    project_names: list[str] = []
    monkeypatch.setattr(
        retention,
        "resolve_project_id",
        lambda _client, project_name: f"id-{project_name}",
    )

    def fake_query(_client, **kwargs):
        project_names.append(kwargs["project_name"])
        return [
            SimpleNamespace(
                id=f"trace-{kwargs['project_name']}-{index}",
                start_time=datetime(2026, 8, 26 - index, tzinfo=timezone.utc),
            )
            for index in range(3)
        ]

    monkeypatch.setattr(retention, "query_runs", fake_query)
    worker = LangSmithRetentionWorker(
        api_key="key",
        enabled=True,
        dry_run=True,
        max_traces=2,
        scan_window_days=30,
    )

    summary = worker.run_once(
        now=datetime(2026, 8, 26, tzinfo=timezone.utc),
    )

    assert project_names == ["First", "HgFinance-Metrics", "HgFinance-Evals"]
    assert "default" not in project_names
    assert summary.scanned == 9
    assert summary.eligible == 3
    assert summary.deleted == 0
    assert summary.queued == 0
    assert summary.visible_overflow == 3
    assert summary.dry_run is True


def test_retention_delete_is_bounded_and_uses_trace_delete_endpoint(monkeypatch) -> None:
    _fake_client_module(monkeypatch)
    monkeypatch.setattr(
        retention,
        "resolve_project_id",
        lambda _client, project_name: f"id-{project_name}",
    )
    monkeypatch.setattr(
        retention,
        "query_runs",
        lambda _client, **kwargs: [
            SimpleNamespace(id="trace-new", start_time=datetime(2026, 8, 26, tzinfo=timezone.utc)),
            SimpleNamespace(id="trace-old", start_time=datetime(2026, 8, 25, tzinfo=timezone.utc)),
        ],
    )
    requests = []
    worker = LangSmithRetentionWorker(
        api_key="key",
        endpoint="https://example.test",
        enabled=True,
        dry_run=False,
        max_traces=1,
        opener=lambda request, timeout: requests.append(request) or _Response(),
    )

    summary = worker.run_once(now=datetime(2026, 8, 26, tzinfo=timezone.utc))

    assert summary.deleted == 3
    assert summary.queued == 3
    assert summary.pending_visible == 3
    assert summary.visible_overflow == 3
    assert len(requests) == 3
    request = requests[0]
    assert request.full_url == "https://example.test/api/v1/runs/delete"
    body = json.loads(request.data)
    assert body == {"trace_ids": ["trace-old"], "session_id": "id-First"}


def test_retention_delete_splits_large_batches(monkeypatch) -> None:
    requests = []
    worker = LangSmithRetentionWorker(
        api_key="key",
        endpoint="https://example.test",
        enabled=True,
        dry_run=False,
        opener=lambda request, timeout: requests.append(request) or _Response(),
    )

    deleted = worker._delete_trace_ids(
        "id-First",
        [f"trace-{index}" for index in range(205)],
    )

    assert deleted == 205
    assert [len(json.loads(request.data)["trace_ids"]) for request in requests] == [100, 100, 5]


def test_retention_caps_complete_run_rows_and_deletes_whole_trees(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_RETENTION_SCOPES", "workflow")
    _fake_client_module(monkeypatch)
    monkeypatch.setattr(
        retention,
        "resolve_project_id",
        lambda _client, project_name: f"id-{project_name}",
    )
    monkeypatch.setattr(
        retention,
        "query_runs",
        lambda _client, **_kwargs: [
            # Newest two trees fit exactly inside the four-run cap.
            SimpleNamespace(
                id="new-root",
                trace_id="new-root",
                is_root=True,
                start_time=datetime(2026, 8, 26, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                id="new-child",
                trace_id="new-root",
                is_root=False,
                start_time=datetime(2026, 8, 26, 0, 0, 1, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                id="middle-root",
                trace_id="middle-root",
                is_root=True,
                start_time=datetime(2026, 8, 25, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                id="middle-child",
                trace_id="middle-root",
                is_root=False,
                start_time=datetime(2026, 8, 25, 0, 0, 1, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                id="old-root",
                trace_id="old-root",
                is_root=True,
                start_time=datetime(2026, 8, 24, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                id="old-child-1",
                trace_id="old-root",
                is_root=False,
                start_time=datetime(2026, 8, 24, 0, 0, 1, tzinfo=timezone.utc),
            ),
            SimpleNamespace(
                id="old-child-2",
                trace_id="old-root",
                is_root=False,
                start_time=datetime(2026, 8, 24, 0, 0, 2, tzinfo=timezone.utc),
            ),
        ],
    )
    requests = []
    worker = LangSmithRetentionWorker(
        api_key="key",
        endpoint="https://example.test",
        enabled=True,
        dry_run=False,
        max_runs=4,
        max_delete_per_pass=1,
        opener=lambda request, timeout: requests.append(request) or _Response(),
    )

    summary = worker.run_once(now=datetime(2026, 8, 26, tzinfo=timezone.utc))

    assert summary.scanned == 7
    assert summary.visible_overflow == 3
    assert summary.eligible == 1
    assert summary.deleted == 1
    assert summary.queued_runs == 3
    assert json.loads(requests[0].data)["trace_ids"] == ["old-root"]


def test_retention_stops_retrying_hourly_delete_limit() -> None:
    def rate_limited(_request, timeout):
        raise urllib.error.HTTPError(
            "https://example.test/api/v1/runs/delete",
            429,
            "rate limited",
            {},
            io.BytesIO(b'{"detail":"Hourly trace deletion limit exceeded"}'),
        )

    worker = LangSmithRetentionWorker(
        api_key="key",
        endpoint="https://example.test",
        enabled=True,
        dry_run=False,
        opener=rate_limited,
    )

    with pytest.raises(LangSmithRetentionRateLimited, match="TRACE_DELETE_HOURLY_LIMIT"):
        worker._delete_trace_ids("id-First", ["trace-old"])


def test_retention_scope_can_be_restricted_to_first(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_RETENTION_SCOPES", "workflow")
    worker = LangSmithRetentionWorker(api_key="key", enabled=True, dry_run=True)

    assert [project_name for _, project_name, _ in worker._projects()] == ["First"]


def test_retention_deletes_only_the_oldest_budget_per_pass(monkeypatch) -> None:
    monkeypatch.setenv("LANGSMITH_RETENTION_SCOPES", "workflow")
    _fake_client_module(monkeypatch)
    monkeypatch.setattr(
        retention,
        "resolve_project_id",
        lambda _client, project_name: f"id-{project_name}",
    )
    monkeypatch.setattr(
        retention,
        "query_runs",
        lambda _client, **_kwargs: [
            SimpleNamespace(
                id=f"trace-{index}",
                start_time=datetime(2026, 8, 26 - index, tzinfo=timezone.utc),
            )
            for index in range(6)
        ],
    )
    requests = []
    worker = LangSmithRetentionWorker(
        api_key="key",
        endpoint="https://example.test",
        enabled=True,
        dry_run=False,
        max_traces=3,
        max_delete_per_pass=1,
        opener=lambda request, timeout: requests.append(request) or _Response(),
    )

    summary = worker.run_once(now=datetime(2026, 8, 26, tzinfo=timezone.utc))

    assert summary.deleted == 1
    assert len(requests) == 1
    assert json.loads(requests[0].data)["trace_ids"] == ["trace-3"]


def test_retention_does_not_requeue_pending_trace_ids(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LANGSMITH_RETENTION_SCOPES", "workflow")
    _fake_client_module(monkeypatch)
    monkeypatch.setattr(
        retention,
        "resolve_project_id",
        lambda _client, project_name: f"id-{project_name}",
    )
    monkeypatch.setattr(
        retention,
        "query_runs",
        lambda _client, **_kwargs: [
            SimpleNamespace(
                id=f"trace-{index}",
                start_time=datetime(2026, 8, 26 - index, tzinfo=timezone.utc),
            )
            for index in range(6)
        ],
    )
    requests = []
    worker = LangSmithRetentionWorker(
        api_key="key",
        endpoint="https://example.test",
        enabled=True,
        dry_run=False,
        max_traces=3,
        max_delete_per_pass=1,
        pending_state_path=tmp_path / "pending.json",
        opener=lambda request, timeout: requests.append(request) or _Response(),
    )

    first = worker.run_once(now=datetime(2026, 8, 26, tzinfo=timezone.utc))
    second = worker.run_once(now=datetime(2026, 8, 26, tzinfo=timezone.utc))

    assert first.deleted == 1
    assert second.deleted == 0
    assert second.queued == 0
    assert second.skipped == 1
    assert len(requests) == 1
