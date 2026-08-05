"""QA employee Graph contract tests (no network, no audit mutation)."""

from __future__ import annotations

import sys
from pathlib import Path

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))

from qa_employee_workers import _audit_tool, run_employee_workers

import scripts as qa_scripts


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"evidence checked","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


def test_evidence_worker_is_langgraph_qwen_and_conditional_roles_sleep():
    report = run_employee_workers(
        {"assessment": {"decision": "PASS", "claim_checks": []}},
        llm=_llm,
    )
    assert report["runtime"] == {
        "executor": "LangGraph",
        "topology": "async_fan_out_fan_in_independent_graphs",
        "provider": "ollama",
        "model": "qwen3:1.7b",
        "max_retries": 2,
        "max_attempts": 3,
    }
    assert report["executed"] == ["evidence-qa-worker"]
    assert "model-and-internal-audit-worker" in report["not_executed"]


def test_all_conditional_qa_workers_require_real_signals():
    report = run_employee_workers(
        {
            "assessment": {
                "decision": "FAIL",
                "claim_checks": [{"result": "UNSUPPORTED"}],
            },
            "model_risk": {"decision": "WARN"},
            "ops_assessment": {"status": "DEGRADED"},
            "incident": {"incident_id": "i1"},
        },
        llm=_llm,
    )
    assert report["failed"] == []
    assert len(report["executed"]) == 5


def test_model_and_internal_audit_graph_stage_runs_with_governed_inputs():
    model_risk_input = {
        "model_id": "00000000-0000-0000-0000-000000000001",
        "model_version": "model-v1",
        "prompt_version": "prompt-v1",
        "dataset_version": "dataset-v1",
        "evaluation_count": 500,
        "accuracy": 0.9,
        "calibration_error": 0.02,
        "drift_score": 0.04,
        "protected_failure_rate": 0.01,
    }
    audit_events = [
        {
            "action": "qa.evidence.check",
            "department": "qa",
            "trace_id": "trace-qa-1",
            "profile_status": "ACTIVE",
            "authorized": True,
        }
    ]

    output = qa_scripts.model_and_internal_audit(
        {
            "model_risk_input": model_risk_input,
            "internal_audit_events": audit_events,
        }
    )

    assert output["model_risk"]["decision"] == "PASS"
    assert output["internal_audit"]["decision"] == "PASS"
    assert output["audit_escalate"] is False

    report = run_employee_workers(output, llm=_llm)
    assert "model-and-internal-audit-worker" in report["executed"]
    assert "model-and-internal-audit-worker" not in report["not_executed"]


def test_audit_worker_tool_runs_deterministic_engines_for_explicit_inputs():
    output = _audit_tool(
        {
            "model_risk_input": {
                "model_id": "00000000-0000-0000-0000-000000000001",
                "model_version": "model-v1",
                "prompt_version": "prompt-v1",
                "dataset_version": "dataset-v1",
                "evaluation_count": 500,
                "accuracy": 0.9,
                "calibration_error": 0.02,
                "drift_score": 0.04,
                "protected_failure_rate": 0.01,
            },
            "internal_audit_events": [
                {
                    "action": "qa.evidence.check",
                    "department": "qa",
                    "trace_id": "trace-qa-tool-1",
                    "profile_status": "ACTIVE",
                    "authorized": True,
                }
            ],
        }
    )

    assert output["model_risk"]["decision"] == "PASS"
    assert output["internal_audit"]["decision"] == "PASS"


def test_each_worker_trace_contains_all_declared_tools():
    report = run_employee_workers(
        {
            "assessment": {"claim_checks": [{"result": "UNSUPPORTED"}]},
            "model_risk": {"status": "TESTING"},
            "internal_audit": {"status": "TESTING"},
            "ops_assessment": {"status": "HEALTHY"},
            "permission_check": {"result": "ALLOWED"},
            "incident": {"incident_id": "incident-test"},
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
