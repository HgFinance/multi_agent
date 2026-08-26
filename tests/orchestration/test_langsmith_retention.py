from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace

import orchestration.langsmith_retention as retention
from orchestration.langsmith_retention import LangSmithRetentionWorker


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
        return [SimpleNamespace(id=f"trace-{kwargs['project_name']}")]

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

    assert project_names == ["First", "HgFinance-Metrics"]
    assert "default" not in project_names
    assert summary.scanned == 2
    assert summary.eligible == 2
    assert summary.deleted == 0
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
        lambda _client, **kwargs: [SimpleNamespace(id="trace-1")],
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

    assert summary.deleted == 1
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://example.test/api/v1/runs/delete"
    body = json.loads(request.data)
    assert body == {"trace_ids": ["trace-1"], "session_id": "id-First"}
