"""Risk employee Graph contract tests (no network, no order side effects)."""

from __future__ import annotations

import sys
from pathlib import Path

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

from risk_employee_workers import WORKER_SPECS, risk_runner, run_employee_workers


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"evidence checked","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


def test_llm_worker_is_langgraph_qwen_and_conditional():
    report = run_employee_workers(
        {"trading_state": "ENABLED", "assessment": {"verdict": "approve"}},
        llm=_llm,
    )
    assert report["runtime"]["executor"] == "LangGraph"
    assert report["runtime"]["model"] == "qwen3:1.7b"
    assert report["failed"] == []
    assert "compliance-policy-worker" in report["not_executed"]
    # risk-runner는 레지스트리 밖이지만 항상 실행되고 workers/executed에 나온다
    assert report["executed"] == ["risk-runner"]
    assert any(item["worker_id"] == "risk-runner" for item in report["workers"])
    assert all(item["tools"] for item in report["workers"])


def test_compliance_worker_runs_only_when_its_signal_exists():
    report = run_employee_workers(
        {
            "trading_state": "ENABLED",
            "assessment": {"verdict": "approve"},
            "compliance": {"grounded": True},
            "counterparty": {"status": "DEGRADED"},
        },
        llm=_llm,
    )
    assert report["failed"] == []
    assert {item["worker_id"] for item in report["workers"]} == {
        spec.worker_id for spec in WORKER_SPECS
    } | {"risk-runner"}


def test_compliance_worker_trace_contains_all_declared_tools():
    report = run_employee_workers(
        {
            "trading_state": "ENABLED",
            "p1_snapshot": {"status": "PASS"},
            "assessment": {"verdict": "approve"},
            "compliance": {"grounded": True},
            "counterparty": {"status": "DEGRADED"},
        },
        llm=_llm,
    )

    # risk-runner는 LangGraph Worker가 아니라 skill_results가 없다 - 별도로 다룬다
    llm_workers = [w for w in report["workers"] if w["worker_id"] != "risk-runner"]
    assert llm_workers
    for worker in llm_workers:
        tool_events = [
            event
            for event in worker["skill_results"]
            if event["skill_id"] == "context.internal_api.v1"
        ]
        assert len(tool_events) == 1
        assert tool_events[0]["tool_calls"] == worker["tools"]


def test_risk_runner_is_deterministic_and_derives_blockers_from_engine_output():
    report = risk_runner(
        {
            "trading_state": "ENABLED",
            "assessment": {
                "verdict": "reject",
                "check_results": [{"name": "concentration", "passed": False}],
            },
            "counterparty": {"status": "DEGRADED"},
        }
    )
    assert report["llm"] is False
    assert report["status"] == "COMPLETED"
    assert "summary" not in report["output"]
    assert report["output"]["decided_by"] == "deterministic"
    assert report["output"]["authoritative"] is False
    assert "risk_verdict_reject" in report["output"]["blockers"]
    assert "check_failed:concentration" in report["output"]["blockers"]
    assert report["output"]["escalate"] is True


def test_risk_runner_has_no_blockers_when_engine_approves():
    report = risk_runner({"assessment": {"verdict": "approve", "check_results": []}})
    assert report["output"]["blockers"] == []
    assert report["output"]["escalate"] is False
