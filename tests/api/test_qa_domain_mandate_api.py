"""QA Domain verification-mandate endpoint tests without external DB or Pinecone."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

QA_API_DIR = Path(__file__).resolve().parents[2] / "departments" / "06-ai-qa-audit" / "api"
sys.path.insert(0, str(QA_API_DIR))
sys.modules.pop("app", None)

from app import app


def _body(verification_id: str = "VER-20260807-001") -> dict[str, object]:
    now = datetime.now(timezone.utc).isoformat()
    return {
        "verification_id": verification_id,
        "artifact": {
            "artifact_version_id": str(uuid4()),
            "artifact_type": "research_packet",
            "producer": "research-supervisor",
            "fund_id": str(uuid4()),
            "trace_id": str(uuid4()),
            "claims": [
                {
                    "claim_index": 0,
                    "text": "AAPL은 반등한다",
                    "kind": "fact",
                    "subject": "AAPL",
                }
            ],
        },
        "decision_time": now,
    }


def test_domain_endpoint_dispatches_to_qa_head_and_three_employees() -> None:
    response = TestClient(app).post(
        "/qa/v1/verifications/VER-20260807-001/assess",
        json=_body(),
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["dispatch"]["dispatcher"] == "qa-head"
    assert set(payload["employees"]) == {
        "qa-runner",
        "hallucination-critic-worker",
        "incident-postmortem-worker",
    }
    hashes = {report["input_hash"] for report in payload["employees"].values()}
    assert len(hashes) == 1, "all three employees must share the same input_hash"
    assert payload["employees"]["qa-runner"]["decision"] == "FAIL"
    assert payload["employees"]["hallucination-critic-worker"]["status"] == "DEGRADED"
    assert payload["employees"]["hallucination-critic-worker"]["namespace"] == "qa-hallucination-reference"
    assert payload["qa_head"]["binding"] is False
    assert payload["dispatch"]["mutation_allowed"] is False


def test_domain_endpoint_rejects_mismatched_path_verification_id() -> None:
    response = TestClient(app).post(
        "/qa/v1/verifications/VER-OTHER/assess",
        json=_body(),
    )

    assert response.status_code == 409
    assert response.json()["detail"]["error_code"] == "VERIFICATION_ID_MISMATCH"
