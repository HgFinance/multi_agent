"""HTTP boundary tests for the HR departments-capacity endpoint."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("dotenv")

from fastapi.testclient import TestClient  # noqa: E402

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce/api"))
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce/scorecard"))

import app as workforce_api  # noqa: E402
from observability import WorkerRegistryUnavailable  # noqa: E402


def test_capacity_returns_200_with_unavailable_departments_without_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/capacity"
    )
    assert response.status_code == 200
    reports = response.json()["capacity"]
    assert reports
    assert all(report["status"] == "UNAVAILABLE" for report in reports)
    assert all(report["arrivals"] is None for report in reports)
    assert all(report["queue_p95_ms"] is None for report in reports)


def test_capacity_keeps_registry_failure_as_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**_kwargs: object) -> list[object]:
        raise WorkerRegistryUnavailable("worker_registry_invalid:test")

    monkeypatch.setattr(workforce_api, "check_department_capacity", unavailable)
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/capacity"
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "worker_registry_unavailable"


def test_capacity_rejects_non_positive_lookback_hours() -> None:
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/capacity", params={"lookback_hours": 0}
    )
    assert response.status_code == 422
