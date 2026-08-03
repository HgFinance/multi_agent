"""Tests for the non-binding CEO paper report projection."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from orchestration.reports.paper_case import (
    PaperCaseInput,
    build_paper_case_report,
    write_paper_case_report,
)
from orchestration.workflows.contracts import StepRun, WorkflowRun


class PaperCaseReportTest(unittest.TestCase):
    def test_completed_smoke_is_not_treated_as_investment_approval(self) -> None:
        case = PaperCaseInput.from_mapping(
            {
                "case_id": "paper-aapl-001",
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 100,
                "order_type": "LIMIT",
                "limit_price": "200.00",
                "stage": "paper",
            }
        )
        steps = tuple(
            StepRun(
                step_id=step_id,
                sequence=index,
                status="DISPATCHED",
                input_contract=input_contract,
                output_contract=output_contract,
                failure_action=failure_action,
                attempts=1,
                detail="hermes_smoke=PASS paper_no_side_effects=true",
            )
            for index, (step_id, _owner, input_contract, output_contract, failure_action)
            in enumerate(
                (
                    ("research", "research-department", "case_request", "research_packet", "HOLD"),
                    ("trading", "trading-department", "research_packet", "order_intent", "HOLD"),
                    ("risk", "risk-management", "order_intent", "risk_decision", "REJECT"),
                    ("qa", "qa-department", "risk_decision", "qa_assessment", "ESCALATE"),
                    ("oms-fill-gate", "trading-department", "qa_assessment", "execution_result", "HOLD"),
                    ("accounting", "accounting-portfolio-department", "execution_result", "accounting_snapshot", "BREAK"),
                    ("ceo", "ceo-agent", "accounting_snapshot", "ceo_case_summary", "ESCALATE"),
                ),
                start=1,
            )
        )
        run = WorkflowRun(
            run_id="wf-paper-001",
            workflow="investment-case",
            mode="paper-e2e",
            status="COMPLETED",
            safe_action=None,
            steps=steps,
        )

        report = build_paper_case_report(case, workflow_run=run)

        self.assertEqual(report.report_status, "PAPER_CONNECTED")
        self.assertEqual(report.final_decision, "HOLD / ESCALATE")
        self.assertEqual(len(report.stages), 7)
        self.assertIn("SIMULATION_ONLY", report.to_markdown())
        self.assertIn("Broker order, Paper Broker fill, Ledger posting", report.to_markdown())

    def test_failed_or_missing_run_skips_downstream_safely(self) -> None:
        case = PaperCaseInput.from_mapping(
            {
                "case_id": "paper-aapl-002",
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 100,
                "order_type": "LIMIT",
                "limit_price": "200.00",
            }
        )
        report = build_paper_case_report(
            case,
            run_id="wf-paper-failed",
            runtime_error="hermes_profile_filesystem_permission",
        )

        self.assertEqual(report.report_status, "INCONCLUSIVE")
        self.assertEqual(report.stages[0].status, "NOT_EXECUTED")
        self.assertEqual(report.stages[-1].status, "SKIPPED_SAFE")
        self.assertIn("HOLD / ESCALATE", report.to_markdown())

    def test_completed_status_with_missing_stage_is_inconclusive(self) -> None:
        case = PaperCaseInput.from_mapping(
            {
                "case_id": "paper-aapl-004",
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 100,
                "order_type": "LIMIT",
                "limit_price": "200.00",
            }
        )
        run = WorkflowRun(
            run_id="wf-paper-partial",
            workflow="investment-case",
            mode="paper-e2e",
            status="COMPLETED",
            safe_action=None,
            steps=(
                StepRun(
                    step_id="research",
                    sequence=1,
                    status="DISPATCHED",
                    input_contract="case_request",
                    output_contract="research_packet",
                    failure_action="HOLD",
                    attempts=1,
                    detail="smoke pass",
                ),
            ),
        )

        report = build_paper_case_report(case, workflow_run=run)

        self.assertEqual(report.report_status, "INCONCLUSIVE")

    def test_writer_creates_local_markdown_only(self) -> None:
        case = PaperCaseInput.from_mapping(
            {
                "case_id": "paper-aapl-003",
                "symbol": "AAPL",
                "side": "BUY",
                "quantity": 1,
                "order_type": "LIMIT",
                "limit_price": "200.00",
            }
        )
        report = build_paper_case_report(case)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = write_paper_case_report(report, Path(temp_dir) / "report.md")
            self.assertTrue(path.is_file())
            self.assertTrue(path.read_text(encoding="utf-8").startswith("# Paper Investment Case Report"))


if __name__ == "__main__":
    unittest.main()
