"""QA api/app.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (AI QA/감사본부). evidence/check(OPENAI_API_KEY 필요)는 원본과 동일하게
제외한다. app의 recorder/timeline/evidence_store는 모듈 단위 싱글턴이라 테스트 간
Unauthorized Tool Call 집계(3.4)는 정의 순서(3.3 -> 3.4)에 의존한다 - 원본 흐름과 동일.

실행: python -m pytest departments/06-ai-qa-audit/tests/test_qa_app.py -v
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))

sys.modules.pop("app", None)  # 03-risk도 모듈명 app이라 캐시 충돌 방지
from app import app, evidence_store  # noqa: E402
from evidence_qa_engine import EvidenceChunk  # noqa: E402

now = datetime.now(timezone.utc)
client = TestClient(app)


def test_01_qa_check_pass_and_fail():
    ev_id = uuid4()
    evidence_store.chunks[ev_id] = EvidenceChunk(
        evidence_id=ev_id, source="research-api", published_at=now - timedelta(hours=1),
        observed_at=now - timedelta(hours=1), excerpt="근거 원문",
        numeric_value=Decimal("70000"), unit="KRW",
    )
    fund, trace, artifact_id = uuid4(), uuid4(), uuid4()

    r1 = client.post(
        "/investment-cases/case-1/qa-check",
        json={
            "artifact": {
                "artifact_version_id": str(artifact_id), "artifact_type": "research_packet",
                "producer": "research-supervisor", "fund_id": str(fund), "trace_id": str(trace),
                "claims": [{
                    "claim_index": 0, "text": "AAPL 종가는 70000원", "kind": "fact", "subject": "AAPL",
                    "numeric_value": "70000", "unit": "KRW", "evidence_ids": [str(ev_id)],
                }],
            },
            "context": {"decision_time": now.isoformat()},
        },
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["decision"] == "PASS", r1.json()

    r2 = client.post(
        "/investment-cases/case-1/qa-check",
        json={
            "artifact": {
                "artifact_version_id": str(uuid4()), "artifact_type": "research_packet",
                "producer": "research-supervisor", "fund_id": str(fund), "trace_id": str(trace),
                "claims": [{"claim_index": 0, "text": "AAPL은 반등한다", "kind": "fact", "subject": "AAPL"}],
            },
            "context": {"decision_time": now.isoformat()},
        },
    )
    assert r2.json()["decision"] == "FAIL", r2.json()
    assert len(r2.json()["findings"]) == 1


def test_02_ops_evaluate_critical_and_validation_error():
    ops_body = {
        "metrics": {
            "scope": "research-department", "window_start": (now - timedelta(minutes=5)).isoformat(),
            "window_end": now.isoformat(), "request_count": 1000, "error_count": 150,
            "p95_latency_ms": "800", "cost_usd": "2.5",
        },
        "thresholds": {
            "max_error_rate": "0.02", "critical_error_rate": "0.10", "max_p95_latency_ms": "2000",
            "critical_p95_latency_ms": "5000", "max_cost_usd_per_window": "10",
        },
    }
    r3 = client.post("/qa/v1/ops/evaluate", json=ops_body)
    assert r3.json()["status"] == "critical", r3.json()
    assert r3.json()["incident"]["severity"] == "SEV2"

    r3_bad = client.post("/qa/v1/ops/evaluate", json={"metrics": ops_body["metrics"]})
    assert r3_bad.status_code == 422, r3_bad.text
    assert r3_bad.json()["error_code"] == "RequestValidationError", r3_bad.json()


def test_03_agent_tool_trace_full_flow():
    agent_id, profile_id, trace_id = uuid4(), uuid4(), uuid4()
    run = client.post("/qa/v1/runs", json={
        "trace_id": str(trace_id), "agent_id": str(agent_id), "profile_version_id": str(profile_id),
        "input_hash": "hash_1",
    }).json()
    call = client.post(f"/qa/v1/runs/{run['agent_run_id']}/tool-calls", json={
        "tool_name": "market-api", "scope": {"symbol": "AAPL"}, "input_hash": "call_hash_1",
    }).json()
    client.post(f"/qa/v1/tool-calls/{call['tool_call_id']}/allow")
    client.post(f"/qa/v1/tool-calls/{call['tool_call_id']}/complete", json={"output_hash": "out_1"})
    finished = client.post(f"/qa/v1/runs/{run['agent_run_id']}/complete").json()
    assert finished["status"] == "COMPLETED", finished


def test_04_tool_permission_allowed_denied_and_unauthorized_count():
    agent_id, profile_id, trace_id = uuid4(), uuid4(), uuid4()
    policy = {"agent_id": str(agent_id), "profile_version_id": str(profile_id), "allowed_tools": ["market-api"]}
    ok_check = client.post("/qa/v1/tool-permission/check", json={"policy": policy, "tool_name": "market-api"}).json()
    assert ok_check["result"] == "ALLOWED"
    bad_check = client.post(
        "/qa/v1/tool-permission/check", json={"policy": policy, "tool_name": "broker-adapter-submit"},
    ).json()
    assert bad_check["result"] == "DENIED"
    run2 = client.post("/qa/v1/runs", json={
        "trace_id": str(trace_id), "agent_id": str(agent_id), "profile_version_id": str(profile_id),
        "input_hash": "hash_2",
    }).json()
    denied_call = client.post(f"/qa/v1/runs/{run2['agent_run_id']}/tool-calls:checked", json={
        "policy": policy, "tool_name": "broker-adapter-submit", "scope": {}, "input_hash": "call_hash_2",
    }).json()
    assert denied_call["status"] == "DENIED"
    count = client.get("/qa/v1/tool-calls/unauthorized-count").json()["count"]
    assert count == 1, count


def test_05_incident_timeline_and_corrective_action_full_flow():
    incident_id, finding_id = uuid4(), uuid4()
    client.post(f"/qa/v1/incidents/{incident_id}/events", json={
        "source": "agent-ops-monitor", "entry_type": "FACT", "summary": "에러율 15% 관측",
        "occurred_at": now.isoformat(), "recorded_by": "svc_audit_collector",
    })
    client.post(f"/qa/v1/incidents/{incident_id}/events", json={
        "source": "incident-postmortem-agent", "entry_type": "INFERENCE",
        "summary": "market-api 지연이 원인으로 추정", "occurred_at": now.isoformat(),
        "recorded_by": "svc_audit_collector",
    })
    tl = client.get(f"/qa/v1/incidents/{incident_id}/timeline").json()
    assert len(tl) == 2 and tl[0]["entry_type"] == "FACT" and tl[1]["entry_type"] == "INFERENCE"

    action = client.post("/qa/v1/corrective-actions", json={
        "owner": "research-department", "action_plan": {"plan": "타임아웃 값 상향"},
        "due_at": (now + timedelta(days=3)).isoformat(), "incident_id": str(incident_id),
    }).json()
    action_id = action["corrective_action_id"]
    client.post(f"/qa/v1/corrective-actions/{action_id}/start")
    client.post(f"/qa/v1/corrective-actions/{action_id}/submit-for-verification")

    mismatched = client.post(
        f"/qa/v1/corrective-actions/{action_id}/verify-and-close",
        json={"verifier": "qa-audit-supervisor", "verification": {}},
        headers={"x-auth-subject": "someone-else"},
    )
    assert mismatched.status_code == 403, mismatched.text

    self_verify = client.post(
        f"/qa/v1/corrective-actions/{action_id}/verify-and-close",
        json={"verifier": "research-department", "verification": {}},
    )
    assert self_verify.status_code == 409, self_verify.text

    closed = client.post(
        f"/qa/v1/corrective-actions/{action_id}/verify-and-close",
        json={"verifier": "qa-audit-supervisor", "verification": {"checked": "확인함"}},
        headers={"x-auth-subject": "qa-audit-supervisor"},
    ).json()
    assert closed["status"] == "COMPLETED", closed
