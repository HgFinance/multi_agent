from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.modules.pop("app", None)
from app import app


def test_model_risk_and_internal_audit_contracts_are_exposed() -> None:
    client = TestClient(app)
    model_result = client.post(
        "/qa/v1/model-risk/evaluate",
        json={
            "model_id": str(uuid4()),
            "model_version": "model-v1",
            "prompt_version": "prompt-v1",
            "dataset_version": "dataset-v1",
            "evaluation_count": 500,
            "accuracy": 0.9,
            "calibration_error": 0.02,
            "drift_score": 0.04,
            "protected_failure_rate": 0.01,
        },
    )
    assert model_result.status_code == 200, model_result.text
    assert model_result.json()["decision"] == "PASS"

    audit_result = client.post(
        "/qa/v1/internal-audit/evaluate",
        json={
            "events": [
                {
                    "action": "qa.evidence.check",
                    "department": "qa",
                    "trace_id": str(uuid4()),
                    "profile_status": "ACTIVE",
                    "authorized": True,
                }
            ]
        },
    )
    assert audit_result.status_code == 200, audit_result.text
    assert audit_result.json()["decision"] == "PASS"
