"""Contract tests for the cross-department orchestration boundary."""

from __future__ import annotations

import unittest
from pathlib import Path

import yaml

from orchestration.adapters import build_paper_e2e_handlers, build_test_handlers
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
        self.assertEqual(
            registry["portfolio_recommendation_cycle"]["manifest"],
            "orchestration/workflows/portfolio-recommendation.yaml",
        )
        self.assertFalse(registry["portfolio_recommendation_cycle"]["external_writes"])

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
                "portfolio-recommendation-full",
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
        # 마지막 quant 단계(quant-outcome-feedback)가 이 흐름을 공장으로 만든다 -
        # 종결 사유를 lesson_codes 로 적재해 리서치 Gate 0 이 다시 읽는다.
        # 그게 없으면 일방통행이라 같은 실험을 다시 산다(2026-08-10 공장 개편).
        self.assertEqual(
            [step.department for step in spec.steps],
            [
                "quant-backtest-department",
                "qa-department",
                "ceo-agent",
                "quant-backtest-department",
            ],
        )
        self.assertNotIn("trading-department", [step.department for step in spec.steps])
        # 환류는 **맨 뒤**여야 한다. CEO 승격 판정 앞에 오면 아직 나오지도 않은
        # 결론을 교훈으로 적재하게 된다.
        self.assertEqual(spec.steps[-1].id, "quant-outcome-feedback")

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


    def test_test_mode_runs_full_pipeline_without_external_dependencies(self) -> None:
        root = Path(__file__).resolve().parents[2]
        run = execute_workflow(
            load_workflow("investment-case"),
            mode="test",
            handlers=build_test_handlers(root),
            context={
                "case_request": {
                    "case_id": "test-contract-001",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "quantity": 100,
                    "order_type": "LIMIT",
                    "limit_price": "200.00",
                    "stage": "test",
                }
            },
            run_id="test-contract-run",
        )

        self.assertEqual(run.status, "COMPLETED")
        self.assertTrue(all(step.status == "DISPATCHED" for step in run.steps))
        self.assertFalse(run.metadata["external_writes"])
        self.assertFalse(run.metadata["orders_submitted"])
        self.assertFalse(run.metadata["ledger_posted"])
        self.assertEqual(run.metadata["ceo_decision"]["binding"], False)

    def test_production_mode_blocks_without_explicit_approved_adapters(self) -> None:
        run = execute_workflow(
            load_workflow("investment-case"),
            mode="production",
            context={
                "case_request": {
                    "case_id": "production-contract-001",
                    "symbol": "AAPL",
                    "side": "BUY",
                    "quantity": 100,
                    "order_type": "LIMIT",
                    "limit_price": "200.00",
                    "stage": "production",
                }
            },
            run_id="production-contract-run",
        )
        self.assertEqual(run.status, "BLOCKED")
        self.assertEqual(run.safe_action, "HOLD")
        self.assertEqual(run.steps[0].status, "BLOCKED")
        self.assertEqual(run.steps[0].detail, "production adapter not registered")


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
