"""QA employee Graph contract tests (no network, no audit mutation)."""

from __future__ import annotations

import sys
from pathlib import Path

QA_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(QA_DIR))

from qa_employee_workers import run_employee_workers


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"evidence checked","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


def test_evidence_worker_is_langgraph_qwen_and_conditional_roles_sleep():
    report = run_employee_workers(
        {"assessment": {"decision": "PASS", "claim_checks": []}},
        llm=_llm,
    )
    assert report["runtime"] == {
        "executor": "LangGraph",
        "provider": "ollama",
        "model": "qwen3:8b",
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
