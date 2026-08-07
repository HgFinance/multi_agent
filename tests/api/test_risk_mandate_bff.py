"""Risk mandate BFF-to-domain proxy tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from apps.api.main import app


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
