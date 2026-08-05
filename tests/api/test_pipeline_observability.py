"""BFF acceptance checks for live process-local MAS events and handoffs."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from apps.api.main import app


def test_bff_runtime_keeps_worker_events_and_dynamic_skip_states() -> None:
    client = TestClient(app)
    response = client.post(
        "/ui/portfolio-recommendations",
        json={
            "user_id": "observability-test",
            "mindset": "BALANCED",
            "experience": "BEGINNER",
            "investment_horizon_years": 3,
            "max_drawdown_pct": "0.10",
            "investment_amount": "1000000",
            "currency": "KRW",
            "query": "국내 주식 후보의 근거와 위험을 검토해줘",
        },
    )
    assert response.status_code == 202, response.text
    run_id = response.json()["run_id"]

    runtime = None
    for _ in range(160):
        time.sleep(0.05)
        runtime = client.get(f"/ui/portfolio-recommendations/{run_id}").json()
        if runtime.get("result") is not None:
            break

    assert runtime is not None
    assert runtime["status"] == "COMPLETED"
    assert runtime["pipeline_events"]
    assert any(
        (event.get("handoff") or {}).get("from_role") == "ceo:head"
        and (event.get("handoff") or {}).get("to_role") == "research:head"
        for event in runtime["pipeline_events"]
    )
    assert any(message["kind"] == "worker_started" for message in runtime["messages"])
    assert any(message["kind"] == "worker_summary" for message in runtime["messages"])
    assert runtime["departments"]["trading-department"]["status"] == "SKIPPED"
    assert runtime["departments"]["accounting-portfolio-department"]["status"] == "SKIPPED"

    result = runtime["result"]
    assert result["pipeline_event_count"] == len(result["pipeline_events"])
    assert result["replay"]["replayable"] is True
    assert result["binding"] is False
    assert result["external_writes"] is False
