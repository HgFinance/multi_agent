"""Contract tests for the read-only full paper adapter."""

from __future__ import annotations

import unittest
from pathlib import Path

from orchestration.adapters.ceo import _parse_decision
from orchestration.adapters.paper_pipeline import (
    PaperPipelineAdapter,
    _default_qa_runner,
    _default_risk_runner,
    _department_summary,
)
from orchestration.workflows.manifest import load_workflow
from orchestration.workflows.runner import execute_workflow


class FakeCeoAdapter:
    def __init__(self) -> None:
        self.received: dict[str, object] | None = None

    def decide(self, *, case_request, department_reports):
        self.received = {
            "case_request": dict(case_request),
            "department_reports": dict(department_reports),
        }
        return {
            "recommendation": "BUY",
            "binding_decision": "HOLD / ESCALATE",
            "binding": False,
            "escalate": True,
            "rationale": "paper-only fake CEO result",
            "runtime": {"profile": "ceo-agent", "call_status": "succeeded"},
        }


class PaperPipelineAdapterTest(unittest.TestCase):
    def test_full_handoff_reaches_ceo_without_external_writes(self) -> None:
        ceo = FakeCeoAdapter()
        adapter = PaperPipelineAdapter(
            Path.cwd(),
            research_runner=lambda symbol: {
                "status": "COMPLETED",
                "summary": f"research packet for {symbol}",
            },
            risk_runner=lambda order, context, scope, **kwargs: {
                "verdict": "approve",
                "approved_quantity": order["quantity"],
                "decision_origin": "DETERMINISTIC_RISK_ENGINE",
                "hermes_runtime": {"model": "gpt-5.6-luna"},
            },
            qa_runner=lambda artifact, evidence, decision_time, **kwargs: {
                "verdict": "PASS",
                "claim_checks": [{"result": "SUPPORTED"}],
                "hermes_runtime": {"model": "gpt-5.6-luna"},
            },
            ceo_adapter=ceo,
        )
        context = {
            "workflow_run_id": "wf-paper-test",
            "case_request": {
                "case_id": "paper-test-001",
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 100,
                "order_type": "LIMIT",
                "limit_price": "200.00",
                "stage": "paper",
            },
        }

        run = execute_workflow(
            load_workflow("investment-case"),
            mode="paper",
            handlers=adapter.handlers(),
            context=context,
            run_id="wf-paper-test",
        )

        self.assertEqual(run.status, "COMPLETED")
        self.assertEqual(len(run.steps), 7)
        self.assertEqual(run.steps[-1].status, "DISPATCHED")
        self.assertEqual(run.metadata["ceo_decision"]["binding"], False)
        self.assertEqual(run.metadata["ceo_decision"]["binding_decision"], "HOLD / ESCALATE")
        self.assertIsNotNone(ceo.received)
        self.assertEqual(
            set(ceo.received["department_reports"]),
            {"research", "trading", "risk", "qa", "oms-fill-gate", "accounting"},
        )
        self.assertFalse(run.metadata["external_writes"])
        self.assertFalse(run.metadata["orders_submitted"])
        self.assertFalse(run.metadata["ledger_posted"])

    def test_ceo_json_contract_is_allow_listed(self) -> None:
        result = _parse_decision(
            'noise {"recommendation":"BUY","confidence":0.7,'
            '"rationale":"paper review","escalate":false}'
        )
        self.assertEqual(result["recommendation"], "BUY")
        self.assertEqual(result["confidence"], 0.7)
        with self.assertRaises(ValueError):
            _parse_decision(
                '{"recommendation":"ALL_IN","confidence":1,'
                '"rationale":"unsafe","escalate":false}'
            )


    def test_risk_and_qa_runners_keep_local_modules_isolated(self) -> None:
        risk = _default_risk_runner(Path.cwd())
        qa = _default_qa_runner(Path.cwd())

        risk_file = risk.__paper_module__.__dict__["md_cell"].__globals__["__file__"]
        qa_file = qa.__paper_module__.__dict__["md_cell"].__globals__["__file__"]
        self.assertIn("departments/03-risk/reporting.py", risk_file)
        self.assertIn("departments/06-ai-qa-audit/reporting.py", qa_file)
        self.assertIsNot(
            risk.__paper_module__.__dict__["md_cell"],
            qa.__paper_module__.__dict__["md_cell"],
        )

    def test_degraded_execution_evidence_cannot_be_promoted(self) -> None:
        report = _department_summary(
            "risk",
            {
                "status": "COMPLETED",
                "verdict": "reject",
                "execution_evidence": {
                    "pipeline_status": "DEGRADED",
                    "safe_action": "HOLD",
                    "trace_id": "trace-test",
                },
            },
        )
        self.assertEqual(report["status"], "DEGRADED")
        self.assertEqual(report["safe_action"], "HOLD")
        self.assertTrue(report["langgraph"]["used"])


if __name__ == "__main__":
    unittest.main()
