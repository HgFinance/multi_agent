"""Risk mandate BFF-to-domain proxy tests."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from apps.api.main import _repo, app
from apps.api.risk import _publish_assessment_statuses

try:
    from agent_status import AGENT_STATUS_PROJECTOR
except ImportError:  # pragma: no cover - package import path
    from apps.api.agent_status import AGENT_STATUS_PROJECTOR


def test_risk_mandate_is_proxied_to_risk_domain_api() -> None:
    body = {"mandate_id": "MND-1", "order_mode": "MANUAL_APPROVAL"}
    expected = {"mandate_id": "MND-1", "pipeline_status": "DEGRADED"}

    with patch("apps.api.risk._risk_request", new_callable=AsyncMock) as request:
        request.return_value = expected
        response = TestClient(app).post("/ui/risk/mandates/MND-1/assess", json=body)

    assert response.status_code == 200, response.text
    assert response.json() == expected
    request.assert_awaited_once_with(
        "POST",
        "/risk/v1/mandates/MND-1/assess",
        body=body,
    )


def test_risk_mandate_bff_rejects_path_body_id_mismatch() -> None:
    response = TestClient(app).post(
        "/ui/risk/mandates/MND-1/assess",
        json={"mandate_id": "MND-2"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "mandate_id_mismatch"


def test_risk_mandate_projects_workers_into_office_operations_read_model() -> None:
    """The Risk assessment and Office worker projection share one request."""

    AGENT_STATUS_PROJECTOR.reset()
    payload = {
        "mandate_id": "MND-OFFICE-1",
        "trace_id": "TRACE-OFFICE-1",
        "decision": "APPROVE",
        "employees": {
            "risk-runner": {"status": "COMPLETED", "verdict": "APPROVE"},
            "compliance-policy-worker": {"status": "COMPLETED", "verdict": "PASS"},
        },
        "risk_head": {"manual_approval_required": True},
    }
    try:
        with patch("apps.api.risk._risk_request", new_callable=AsyncMock) as request:
            request.return_value = payload
            response = TestClient(app).post(
                "/ui/risk/mandates/MND-OFFICE-1/assess",
                json={"mandate_id": "MND-OFFICE-1"},
            )

        assert response.status_code == 200, response.text
        with patch.dict(
            os.environ,
            {
                "DATABASE_URL": "",
                "ACCOUNTING_MODE": "OFFLINE",
                "ACCOUNTING_DURABLE_REQUIRED": "",
                "PAPER_DB": "",
            },
            clear=False,
        ):
            _repo.cache_clear()
            operations = TestClient(app).get("/ui/snapshot").json()["operations"]
        risk = next(
            item
            for item in operations["departments"]
            if item["department_code"] == "risk-management"
        )
        assert risk["active_workers"] == ["risk-runner"]
        assert operations["runtime"]["active_workers"][0]["worker_id"] == "risk-runner"
        assert {item["worker_id"] for item in operations["agent_statuses"]} == {
            "risk-runner",
            "compliance-policy-worker",
        }
    finally:
        AGENT_STATUS_PROJECTOR.reset()
        _repo.cache_clear()


def test_risk_worker_projection_keeps_route_and_head_trace_metadata() -> None:
    AGENT_STATUS_PROJECTOR.reset()
    try:
        _publish_assessment_statuses(
            "MND-TRACE-1",
            {
                "trace_id": "TRACE-1",
                "risk_head_state": {
                    "run_id": "RISK-RUN-1",
                    "routing": {
                        "query_mode": "LEGAL_QUERY",
                        "routing_rationale": "법률 질의",
                        "routing_by_llm": True,
                    },
                },
                "employee_runtime": {
                    "workers": {
                        "compliance-policy-worker": {"status": "COMPLETED"}
                    }
                },
                "employees": {
                    "risk-runner": {"status": "COMPLETED", "verdict": "APPROVE"},
                    "compliance-policy-worker": {
                        "status": "COMPLETED",
                        "verdict": "ESCALATE",
                    },
                },
            },
        )

        states = {
            item["worker_id"]: item
            for item in AGENT_STATUS_PROJECTOR.snapshot()["agents"]
        }
        compliance = states["compliance-policy-worker"]
        assert compliance["trace_id"] == "TRACE-1"
        assert compliance["metadata"]["run_id"] == "RISK-RUN-1"
        assert compliance["metadata"]["query_mode"] == "LEGAL_QUERY"
        assert compliance["metadata"]["routing_by_llm"] is True
    finally:
        AGENT_STATUS_PROJECTOR.reset()
