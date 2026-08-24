"""HTTP boundary tests for the HR idle-agents endpoint."""

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


def test_idle_agents_returns_200_with_unavailable_workers_without_langfuse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/idle-agents"
    )
    assert response.status_code == 200
    workers = response.json()["idle_agents"]
    assert workers
    assert all(worker["status"] == "UNAVAILABLE" for worker in workers)


def test_idle_agents_keeps_registry_failure_as_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(**_kwargs: object) -> list[object]:
        raise WorkerRegistryUnavailable("worker_registry_invalid:test")

    monkeypatch.setattr(workforce_api, "check_idle_agents", unavailable)
    response = TestClient(workforce_api.app).get(
        "/workforce/v1/departments/idle-agents"
    )
    assert response.status_code == 503
    assert response.json()["detail"]["error"] == "worker_registry_unavailable"
