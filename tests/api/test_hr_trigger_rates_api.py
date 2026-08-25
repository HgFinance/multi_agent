"""HTTP boundary tests for the HR departments-trigger-rates endpoint."""

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
sys.path.insert(0, str(ROOT / "departments/07-agent-workforce"))

from workforce_api_loader import load_workforce_api  # noqa: E402

workforce_api = load_workforce_api()
from observability import WorkerRegistryUnavailable  # noqa: E402


def test_trigger_rates_returns_200_with_unavailable_departments_without_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/trigger-rates"
    )
    assert response.status_code == 200
    reports = response.json()["trigger_rates"]
    assert reports
    assert all(report["status"] == "UNAVAILABLE" for report in reports)
    assert all(report["execution_count"] is None for report in reports)
    assert all(report["opportunity_count"] is None for report in reports)
    assert all(report["fire_rate"] is None for report in reports)


def test_trigger_rates_keeps_registry_failure_as_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**_kwargs: object) -> list[object]:
        raise WorkerRegistryUnavailable("worker_registry_invalid:test")

    monkeypatch.setattr(workforce_api, "check_worker_trigger_rates", unavailable)
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/trigger-rates"
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "worker_registry_unavailable"


def test_trigger_rates_rejects_non_positive_lookback_hours() -> None:
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/trigger-rates", params={"lookback_hours": 0}
    )
    assert response.status_code == 422
