"""HR API hiring-requests 엔드포인트 계약 테스트.

workforce.hiring_request.propose 도구가 실제로 도달하는 경로(POST
/workforce/v1/hiring-requests)부터, 상태 전이·자기승인 차단까지 HTTP
계층에서 검증한다. InMemoryHiringRequestRepository로 실 DB 없이 돈다
(test_platform_iam_service.py와 동일 패턴 - DATABASE_URL="" 강제).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

os.environ.setdefault("DATABASE_URL", "")

ROOT = Path(__file__).resolve().parents[1]
_HR_API_DIR = ROOT / "departments" / "07-agent-workforce" / "api"


def _load_hr_app():
    import importlib.util

    spec = importlib.util.spec_from_file_location("hr_app_hiring_test", _HR_API_DIR / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["hr_app_hiring_test"] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def hr_app():
    os.environ["DATABASE_URL"] = ""  # InMemoryHiringRequestRepository 강제 - 실 DB 미접촉
    module = _load_hr_app()
    assert type(module._hiring_repo).__name__ == "InMemoryHiringRequestRepository"
    return module


@pytest.fixture()
def client(hr_app):
    from fastapi.testclient import TestClient

    return TestClient(hr_app.app)


def _propose(client, *, department_id="research-department", requested_by="hr-department"):
    body = {
        "department_id": department_id,
        "business_problem": "Queue 깊이 12, SLA 위반 3%",
        "evidence": {"queue_depth": 12},
        "required_capabilities": {"skills": ["python"]},
        "budget": {"usd": 500},
        "requested_by": requested_by,
        "trace_id": "trace-1",
        "created_at": "2026-08-10T00:00:00Z",
    }
    resp = client.post("/workforce/v1/hiring-requests", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_propose_creates_open_request(client) -> None:
    created = _propose(client)
    assert created["status"] == "OPEN"
    assert created["department_id"] == "research-department"
    assert created["requested_by"] == "hr-department"


def test_get_by_id_roundtrips(client) -> None:
    created = _propose(client)
    fetched = client.get(f"/workforce/v1/hiring-requests/{created['request_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == created


def test_get_by_id_missing_is_404(client) -> None:
    resp = client.get("/workforce/v1/hiring-requests/does-not-exist")
    assert resp.status_code == 404


def test_list_filters_by_status(client) -> None:
    created = _propose(client)
    open_list = client.get("/workforce/v1/hiring-requests", params={"status": "OPEN"})
    ids = [r["request_id"] for r in open_list.json()["hiring_requests"]]
    assert created["request_id"] in ids

    approved_list = client.get("/workforce/v1/hiring-requests", params={"status": "APPROVED"})
    assert created["request_id"] not in [
        r["request_id"] for r in approved_list.json()["hiring_requests"]
    ]


def test_list_unknown_status_is_422(client) -> None:
    resp = client.get("/workforce/v1/hiring-requests", params={"status": "NOT_A_STATUS"})
    assert resp.status_code == 422


def test_transition_open_to_evaluating(client) -> None:
    created = _propose(client)
    resp = client.post(
        f"/workforce/v1/hiring-requests/{created['request_id']}/transitions",
        json={"to_status": "EVALUATING", "actor": "qa-department", "at": "2026-08-10T01:00:00Z"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "EVALUATING"


def test_self_approval_is_rejected(client) -> None:
    created = _propose(client, requested_by="hr-department")
    client.post(
        f"/workforce/v1/hiring-requests/{created['request_id']}/transitions",
        json={"to_status": "EVALUATING", "actor": "qa-department", "at": "2026-08-10T01:00:00Z"},
    )
    resp = client.post(
        f"/workforce/v1/hiring-requests/{created['request_id']}/transitions",
        json={"to_status": "APPROVED", "actor": "hr-department", "at": "2026-08-10T02:00:00Z"},
    )
    assert resp.status_code == 403


def test_ceo_approval_by_different_actor_succeeds(client) -> None:
    created = _propose(client, requested_by="hr-department")
    client.post(
        f"/workforce/v1/hiring-requests/{created['request_id']}/transitions",
        json={"to_status": "EVALUATING", "actor": "qa-department", "at": "2026-08-10T01:00:00Z"},
    )
    resp = client.post(
        f"/workforce/v1/hiring-requests/{created['request_id']}/transitions",
        json={
            "to_status": "APPROVED", "actor": "ceo-agent", "at": "2026-08-10T02:00:00Z",
            "reason": "Queue 근거 충분",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "APPROVED"
    assert body["decided_by"] == "ceo-agent"
    assert body["decision_reason"] == "Queue 근거 충분"


def test_illegal_transition_is_409(client) -> None:
    created = _propose(client)
    resp = client.post(
        f"/workforce/v1/hiring-requests/{created['request_id']}/transitions",
        json={"to_status": "APPROVED", "actor": "ceo-agent", "at": "2026-08-10T01:00:00Z"},
    )
    assert resp.status_code == 409


def test_transition_missing_request_is_404(client) -> None:
    resp = client.post(
        "/workforce/v1/hiring-requests/does-not-exist/transitions",
        json={"to_status": "EVALUATING", "actor": "qa-department", "at": "2026-08-10T01:00:00Z"},
    )
    assert resp.status_code == 404
