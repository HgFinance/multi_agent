"""Risk employee Graph contract tests (no network, no order side effects)."""

from __future__ import annotations

import sys
from pathlib import Path

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

from risk_employee_workers import WORKER_SPECS, run_employee_workers


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"evidence checked","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


def test_active_workers_are_independent_and_use_allowlisted_tools():
    report = run_employee_workers(
        {"trading_state": "ENABLED", "assessment": {"verdict": "approve"}},
        llm=_llm,
    )
    assert report["runtime"]["executor"] == "LangGraph"
    assert report["runtime"]["model"] == "qwen3:1.7b"
    assert report["failed"] == []
    assert report["executed"] == ["market-liquidity-worker", "pre-trade-risk-worker"]
    assert "compliance-policy-worker" in report["not_executed"]
    assert all(item["tools"] for item in report["workers"])


def test_conditional_workers_run_only_when_their_signal_exists():
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
    }


def test_each_worker_trace_contains_all_declared_tools():
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

    for worker in report["workers"]:
        tool_events = [
            event
            for event in worker["skill_results"]
            if event["skill_id"] == "context.internal_api.v1"
        ]
        assert len(tool_events) == 1
        assert tool_events[0]["tool_calls"] == worker["tools"]

    market_event = next(
        event
        for event in next(
            worker for worker in report["workers"] if worker["worker_id"] == "market-liquidity-worker"
        )["skill_results"]
        if event["skill_id"] == "context.internal_api.v1"
    )
    assert market_event["output"]["p1_snapshot"] == {"status": "PASS"}
