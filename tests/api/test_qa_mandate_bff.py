"""QA verification BFF-to-domain proxy tests."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from apps.api.main import app


def test_qa_verification_is_proxied_to_qa_domain_api() -> None:
    body = {"verification_id": "VER-1", "artifact": {}}
    expected = {"verification_id": "VER-1", "pipeline_status": "DEGRADED"}

    with patch("apps.api.qa._qa_request", new_callable=AsyncMock) as request:
        request.return_value = expected
        response = TestClient(app).post("/ui/qa/verifications/VER-1/assess", json=body)

    assert response.status_code == 200, response.text
    assert response.json() == expected
    request.assert_awaited_once_with(
        "POST",
        "/qa/v1/verifications/VER-1/assess",
        body=body,
    )


def test_qa_verification_bff_rejects_path_body_id_mismatch() -> None:
    response = TestClient(app).post(
        "/ui/qa/verifications/VER-1/assess",
        json={"verification_id": "VER-2"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "verification_id_mismatch"
