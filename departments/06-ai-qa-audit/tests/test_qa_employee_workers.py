"""QA employee Graph contract tests (no network, no audit mutation)."""

from __future__ import annotations

import sys
from pathlib import Path

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))

import qa_employee_workers
from qa_employee_workers import _audit_tool, qa_runner, run_employee_workers

import scripts as qa_scripts


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"evidence checked","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


def test_llm_workers_are_langgraph_qwen_and_conditional_and_qa_runner_always_runs():
    report = run_employee_workers(
        {"assessment": {"decision": "PASS", "claim_checks": []}},
        llm=_llm,
    )
    assert report["runtime"]["topology"] == "async_fan_out_fan_in_independent_graphs"
    assert report["runtime"]["provider"] == "ollama"
    assert report["runtime"]["model"] == "qwen3:1.7b"
    assert report["runtime"]["max_retries"] == 2
    assert report["runtime"]["max_attempts"] == 3
    assert set(report["runtime"]["technology_profiles"]) == {
        spec.worker_id for spec in qa_employee_workers.WORKER_SPECS
    }
    # 두 LLM Worker 모두 조건이 없으니 실행되지 않고, qa-runner는 레지스트리 밖에서
    # 항상 실행돼 executed에 나온다.
    assert report["executed"] == ["qa-runner"]
    assert "hallucination-critic-worker" in report["not_executed"]
    assert "incident-postmortem-worker" in report["not_executed"]


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
    assert report["not_executed"] == []
    # hallucination-critic-worker + incident-postmortem-worker(LLM) + qa-runner(결정론)
    assert len(report["executed"]) == 3
    assert "qa-runner" in report["executed"]


def test_model_and_internal_audit_stage_feeds_qa_runner_with_governed_inputs():
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

    # model-and-internal-audit-worker는 qa-runner로 흡수됐다 - LLM Worker 목록이
    # 아니라 qa-runner의 facts.audit에서 결과를 확인한다.
    report = run_employee_workers(output, llm=_llm)
    assert "qa-runner" in report["executed"]
    runner_report = next(w for w in report["workers"] if w["worker_id"] == "qa-runner")
    assert runner_report["output"]["facts"]["audit"]["model_risk"]["decision"] == "PASS"
    assert runner_report["output"]["facts"]["audit"]["internal_audit"]["decision"] == "PASS"
    assert runner_report["output"]["blockers"] == []


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


def test_each_llm_worker_trace_contains_all_declared_tools():
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

    # qa-runner는 LangGraph Worker가 아니라 skill_results가 없다 - 별도로 다룬다
    llm_workers = [w for w in report["workers"] if w["worker_id"] != "qa-runner"]
    assert llm_workers
    for worker in llm_workers:
        tool_events = [
            event
            for event in worker["skill_results"]
            if event["skill_id"] == "context.internal_api.v1"
        ]
        assert len(tool_events) == 1
        assert tool_events[0]["tool_calls"] == worker["tools"]


def test_incident_postmortem_worker_persists_classified_entries_to_timeline():
    from audit.incident_timeline import IncidentTimeline

    def _incident_llm(_system: str, prompt: str) -> str:
        if "incident-postmortem-worker" not in prompt:
            return _llm(_system, prompt)
        return (
            '{"summary":"outage traced","confidence":0.7,"evidence_refs":["tool"],'
            '"escalate":false,"entries":['
            '{"entry_type":"FACT","summary":"error rate spiked to 15%",'
            '"occurred_at":"2026-08-06T00:00:00+00:00"},'
            '{"entry_type":"INFERENCE","summary":"likely upstream timeout",'
            '"occurred_at":"2026-08-06T00:02:00+00:00"}]}'
        )

    timeline = IncidentTimeline()  # 인메모리 - Postgres 없이 분류/저장 경로만 검증한다
    incident_id = "00000000-0000-0000-0000-0000000000aa"
    report = run_employee_workers(
        {"incident": {"incident_id": incident_id}, "incident_events": []},
        llm=_incident_llm,
        incident_timeline=timeline,
    )

    worker_report = next(
        w for w in report["workers"] if w["worker_id"] == "incident-postmortem-worker"
    )
    assert worker_report["status"] == "COMPLETED"
    assert "incident_persist_errors" not in worker_report

    stored = timeline.timeline_for(incident_id)
    assert [e.entry_type.value for e in stored] == ["FACT", "INFERENCE"]
    assert stored[0].source == "incident-postmortem-worker"


def test_qa_runner_is_deterministic_and_derives_blockers_from_engine_output():
    report = qa_runner(
        {
            "assessment": {"claim_checks": [{"result": "UNSUPPORTED"}]},
            "model_risk_input": None,
            "ops_assessment": {"status": "CRITICAL"},
            "permission_check": {"result": "DENIED"},
        }
    )
    assert report["llm"] is False
    assert report["status"] == "REJECTED"
    assert "summary" not in report["output"]
    assert report["output"]["decided_by"] == "deterministic"
    assert report["output"]["authoritative"] is False
    assert report["output"]["escalate"] is True
def test_raw_model_risk_decision_cannot_bypass_deterministic_validation():
    report = qa_runner(
        {
            "model_risk_input": {"decision": "PASS"},
        }
    )

    assert report["status"] != "COMPLETED"
    assert report["decision"] != "PASS"
    assert "model_risk" in report["output"]["blockers"][0]
