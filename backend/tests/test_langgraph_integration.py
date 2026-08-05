from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from backend.app.api.v1.endpoints.agent import get_langgraph_service
from backend.app.core.config import Settings
from backend.app.main import app
from backend.app.services.langgraph_service import LangGraphService


def _mock_transport(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/health":
        return httpx.Response(200, json={"status": "ok"})
    if request.url.path == "/api/v1/agent/invoke":
        return httpx.Response(
            200,
            json={"run_id": "mock-run", "status": "completed", "summary": "worker packet ready"},
        )
    return httpx.Response(404, json={"detail": "not found"})


def _mock_service() -> LangGraphService:
    return LangGraphService(
        Settings(langgraph_base_url="http://langgraph.test"),
        transport=httpx.MockTransport(_mock_transport),
    )


def test_langgraph_health_is_checked_through_async_adapter() -> None:
    app.dependency_overrides[get_langgraph_service] = _mock_service
    try:
        response = TestClient(app).get("/api/v1/agent/health")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"


def test_langgraph_invoke_proxies_department_request() -> None:
    app.dependency_overrides[get_langgraph_service] = _mock_service
    try:
        response = TestClient(app).post(
            "/api/v1/agent/invoke",
            json={
                "department": "research-department",
                "query": "국내 반도체 리서치 패킷을 만들어줘",
                "context": {"run_id": "test-run"},
            },
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "status": "accepted",
        "department": "research-department",
        "result": {"run_id": "mock-run", "status": "completed", "summary": "worker packet ready"},
    }
