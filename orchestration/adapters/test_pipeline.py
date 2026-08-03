"""Deterministic full-pipeline fixture for local contract tests.

This adapter is deliberately non-production: it uses a deterministic Worker
response and safe, non-binding Risk/QA/CEO results. It proves that every
cross-department contract can be handed off without credentials, Ollama,
Notion, broker, or database side effects.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paper_pipeline import build_paper_handlers


def _test_worker_llm(_system: str, _prompt: str) -> str:
    """Return schema-valid context for every independent Worker graph."""

    return json.dumps(
        {
            "summary": "deterministic test worker context",
            "confidence": 1.0,
            "evidence_refs": ["test-fixture"],
            "escalate": False,
        }
    )


def _test_research_runner(symbol: str) -> dict[str, Any]:
    return {
        "status": "COMPLETED",
        "summary": f"deterministic research packet for {symbol}",
        "evidence_available": True,
    }


def _test_risk_runner(
    _order_intent: dict[str, Any],
    _risk_context: dict[str, Any],
    _scope: dict[str, Any],
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "verdict": "approve",
        "approved_quantity": _order_intent["quantity"],
        "decision_origin": "DETERMINISTIC_TEST_RISK_ENGINE",
        "decision_status": "FINAL",
        "hermes_runtime": {
            "profile": "risk-management",
            "provider": "test-fixture",
            "model": "deterministic-test",
            "call_status": "succeeded",
        },
    }


def _test_qa_runner(
    _artifact: dict[str, Any],
    _evidence: dict[str, Any],
    _decision_time: str,
    **_kwargs: Any,
) -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "claim_checks": [{"result": "SUPPORTED"}],
        "decision_status": "FINAL",
        "hermes_runtime": {
            "profile": "qa-department",
            "provider": "test-fixture",
            "model": "deterministic-test",
            "call_status": "succeeded",
        },
    }


class DeterministicTestCeoAdapter:
    """Non-binding CEO fixture; never represents an investment approval."""

    def decide(
        self,
        *,
        case_request: dict[str, Any],
        department_reports: dict[str, Any],
    ) -> dict[str, Any]:
        del case_request, department_reports
        return {
            "recommendation": "HOLD",
            "model_recommendation": "HOLD",
            "confidence": 1.0,
            "rationale": "deterministic test fixture; no investment decision",
            "escalate": False,
            "binding_decision": "HOLD / ESCALATE",
            "binding": False,
            "runtime": {
                "profile": "ceo-agent",
                "provider": "test-fixture",
                "model": "deterministic-test",
                "call_status": "succeeded",
            },
        }


def build_test_handlers(repo_root: Path) -> dict[str, Any]:
    """Build the complete seven-step pipeline with zero external side effects."""

    return build_paper_handlers(
        repo_root,
        research_runner=_test_research_runner,
        risk_runner=_test_risk_runner,
        qa_runner=_test_qa_runner,
        ceo_adapter=DeterministicTestCeoAdapter(),
        worker_llm=_test_worker_llm,
    )
