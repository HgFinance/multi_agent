"""Contract tests for the cross-department orchestration boundary."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from orchestration.adapters import build_paper_e2e_handlers
from orchestration.workflows.contracts import SAFE_FAILURE_ACTIONS
from orchestration.workflows.manifest import load_workflow, load_workflows
from orchestration.workflows.routing import route_event
from orchestration.workflows.runner import execute_workflow


class WorkflowContractTest(unittest.TestCase):
    def test_root_registry_points_to_canonical_manifests(self) -> None:
        root = Path(__file__).resolve().parents[2]
        registry = yaml.safe_load((root / "multi-agent-workflow.yaml").read_text(encoding="utf-8"))
        self.assertEqual(registry["canonical_workflow_index"], "orchestration/workflows/index.yaml")
        self.assertEqual(registry["workflow"]["manifest"], "orchestration/workflows/investment-case.yaml")
        self.assertEqual(
            registry["workflow"]["order"],
            ["research", "trading", "risk", "qa", "oms-fill-gate", "accounting", "ceo"],
        )

    def test_all_declared_workflows_load_and_validate(self) -> None:
        workflows = load_workflows()
        self.assertEqual(
            set(workflows),
            {
                "investment-case",
                "strategy-research",
                "workforce-management",
                "agent-evolution",
                "event-routing",
            },
        )
        for spec in workflows.values():
            with self.subTest(workflow=spec.name):
                spec.validate()
                self.assertTrue(all(step.failure_action in SAFE_FAILURE_ACTIONS for step in spec.steps))

    def test_realtime_order_keeps_quant_separate_and_adds_oms_fill_boundary(self) -> None:
        spec = load_workflow("investment-case")
        self.assertEqual(
            [step.department for step in spec.steps],
            [
                "research-department",
                "trading-department",
                "risk-management",
                "qa-department",
                "trading-department",
                "accounting-portfolio-department",
                "ceo-agent",
            ],
        )
        self.assertEqual(spec.steps[4].id, "oms-fill-gate")
        self.assertNotIn("quant-backtest-department", [step.department for step in spec.steps])
        self.assertEqual(spec.steps[2].output_contract, spec.steps[3].input_contract)
        self.assertEqual(spec.steps[3].output_contract, spec.steps[4].input_contract)

    def test_strategy_research_is_a_separate_chain(self) -> None:
        spec = load_workflow("strategy-research")
        self.assertEqual(
            [step.department for step in spec.steps],
            ["quant-backtest-department", "qa-department", "ceo-agent"],
        )
        self.assertNotIn("trading-department", [step.department for step in spec.steps])

    def test_dry_run_plans_every_boundary_without_claiming_execution(self) -> None:
        run = execute_workflow(load_workflow("investment-case"), mode="dry-run", run_id="test-run")
        self.assertEqual(run.status, "VALIDATED")
        self.assertEqual(run.safe_action, None)
        self.assertEqual(len(run.steps), 7)
        self.assertTrue(all(step.status == "PLANNED" for step in run.steps))
        self.assertFalse(any(step.status == "SUCCEEDED" for step in run.steps))

    def test_live_mode_blocks_without_explicit_department_adapter(self) -> None:
        run = execute_workflow(load_workflow("investment-case"), mode="live", run_id="test-run")
        self.assertEqual(run.status, "BLOCKED")
        self.assertEqual(run.safe_action, "HOLD")
        self.assertEqual(run.steps[-1].step_id, "research")
        self.assertEqual(run.steps[-1].status, "BLOCKED")


class EventRoutingContractTest(unittest.TestCase):
    def test_allow_listed_event_routes_only_to_declared_experts(self) -> None:
        decision = route_event("volatility_regime_change")
        self.assertEqual(
            decision.calls,
            (
                "research-department.sector-regime-analyst",
                "risk-management.market-liquidity-risk-agent",
            ),
        )
        self.assertFalse(decision.deterministic_check)
        self.assertIsNone(decision.action)

    def test_deterministic_and_unknown_events_fail_closed(self) -> None:
        loss_limit = route_event("loss_limit_approach")
        self.assertEqual(loss_limit.calls, ())
        self.assertTrue(loss_limit.deterministic_check)
        self.assertEqual(loss_limit.action, "ENTRY_BLOCKED")

        unknown = route_event("not-registered")
        self.assertEqual(unknown.calls, ())
        self.assertEqual(unknown.action, "ENTRY_BLOCKED")


class PaperE2EAdapterTest(unittest.TestCase):
    def test_every_realtime_boundary_has_a_smoke_handler(self) -> None:
        class FakeSmokeAdapter:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str, str]] = []

            def invoke(self, step_id, input_contract, output_contract, context):
                self.calls.append((step_id, input_contract, output_contract))
                self.assert_paper(context)
                return f"fake_smoke={step_id}"

            @staticmethod
            def assert_paper(context) -> None:
                self_case = context["case_request"]
                if self_case.get("stage") != "paper":
                    raise AssertionError("paper stage required")

        fake = FakeSmokeAdapter()
        handlers = build_paper_e2e_handlers(Path.cwd(), smoke_adapter=fake)
        spec = load_workflow("investment-case")
        run = execute_workflow(
            spec,
            mode="live",
            handlers=handlers,
            context={"case_request": {"symbol": "AAPL", "stage": "paper"}},
            run_id="paper-e2e-test",
        )
        self.assertEqual(run.status, "COMPLETED")
        self.assertEqual(len(fake.calls), 7)
        self.assertTrue(all(step.status == "DISPATCHED" for step in run.steps))



if __name__ == "__main__":
    unittest.main()
