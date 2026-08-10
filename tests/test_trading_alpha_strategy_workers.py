from __future__ import annotations

import importlib.util
import sys
import unittest
from dataclasses import FrozenInstanceError
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Lock, get_ident


ROOT = Path(__file__).resolve().parents[1]
TRADING = ROOT / "departments" / "02-trading"
if str(TRADING) not in sys.path:
    sys.path.insert(0, str(TRADING))


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


workers = _load("trading_employee_workers_test", TRADING / "employee_workers.py")
pipeline = _load("trading_alpha_scripts_test", TRADING / "scripts.py")


def bundle(strategy_id: str, target: str = "0.5") -> dict:
    return {
        "strategy_id": strategy_id,
        "strategy_version": "v1",
        "capital_allocation": "300000",
        "performance_weights": {"return": 4, "drawdown": 3, "cost": 2, "execution": 1},
        "target_weight": target,
    }

def runtime_kwargs() -> dict:
    return {
        "iam_permission_provider": lambda selection: {
            "granted": True,
            "permission_id": "iam-test",
            "worker_id": selection["worker_id"],
            "strategy_id": selection["strategy_id"],
            "strategy_version": selection["strategy_version"],
            "scopes": ["paper.execute"],
        },
        "strategy_executor_version": "test-executor-v1",
    }


class StrategyWorkerContractTest(unittest.TestCase):
    def test_invalid_bundle_rejects_before_worker_creation(self) -> None:
        with self.assertRaisesRegex(workers.StrategyBundleRejected, "missing_required_fields"):
            workers.create_temporary_worker({}, executor=lambda _bundle, _event: {})

    def test_worker_is_one_to_one_immutable_and_non_llm(self) -> None:
        worker = workers.create_temporary_worker(
            bundle("alpha-a"), executor=lambda item, _event: {"target_weight": item["target_weight"]}
        )
        self.assertEqual(worker.spec.strategy_id, "alpha-a")
        self.assertRegex(worker.spec.worker_id, r"^alpha-[0-9a-f]{20}$")
        self.assertTrue(worker.spec.temporary)
        self.assertFalse(worker.spec.llm)
        with self.assertRaises(FrozenInstanceError):
            worker.bundle = bundle("mutated")

    def test_parallel_paper_selection_preserves_attribution(self) -> None:
        thread_ids: set[int] = set()
        lock = Lock()

        def execute(item, _event):
            with lock:
                thread_ids.add(get_ident())
            return {"target_weight": item["target_weight"]}

        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a", "0.6"), bundle("alpha-b", "0.2")],
            [{"as_of": "t1", "price": "100"}, {"as_of": "t2", "price": "110"}],
            strategy_executor=execute,
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: {
                "approved": True,
                "max_drawdown_limit": "0.2",
                "minimum_trades": 1,
                "execution_feasibility": "1",
            },
            initial_cash=Decimal("1000000"),
            max_workers=2,
        )

        self.assertEqual(result["status"], "SELECTED")
        self.assertEqual(result["selected_strategy"]["strategy_id"], "alpha-a")
        self.assertFalse(result["selected_strategy"]["live_order_submission_allowed"])
        self.assertTrue(result["selected_strategy"]["risk_gate_required"])
        self.assertEqual({report["strategy_id"] for report in result["reports"]}, {"alpha-a", "alpha-b"})
        self.assertTrue(all(report["fills"] for report in result["reports"]))
        self.assertTrue(all(report["trade_count"] >= 1 for report in result["reports"]))
        self.assertGreaterEqual(len(thread_ids), 1)

    def test_missing_risk_thresholds_rejects_every_candidate(self) -> None:
        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: {},
        )
        self.assertEqual(result["status"], "REJECT")
        self.assertIsNone(result["selected_strategy"])
        self.assertIn("missing_risk_thresholds", result["reports"][0]["selection_blockers"][0])

    def test_duplicate_strategy_version_is_rejected(self) -> None:
        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a"), bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: {
                "approved": True,
                "max_drawdown_limit": "0.2",
                "minimum_trades": 1,
                "execution_feasibility": "1",
            },
        )
        self.assertEqual(len(result["reports"]), 1)
        self.assertEqual(len(result["rejected"]), 1)
        self.assertIn("duplicate_strategy_version", result["rejected"][0]["reason"])

    def test_malformed_numeric_bundle_has_domain_rejection(self) -> None:
        malformed = bundle("alpha-bad")
        malformed["capital_allocation"] = "not-a-number"
        with self.assertRaisesRegex(workers.StrategyBundleRejected, "bundle_numeric_value_invalid"):
            workers.create_temporary_worker(malformed, executor=lambda _bundle, _event: {})

    def test_bundle_is_immutable_inside_executor(self) -> None:
        mutation_blocked = []

        def execute(item, _event):
            try:
                item["strategy_id"] = "mutated"
            except TypeError:
                mutation_blocked.append(True)
            return {"target_weight": item["target_weight"]}

        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=execute,
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: {
                "approved": True, "max_drawdown_limit": "0.2", "minimum_trades": 1,
                "execution_feasibility": "1",
            },
        )
        self.assertEqual(result["status"], "SELECTED")
        self.assertEqual(mutation_blocked, [True])

    def test_risk_provider_failure_rejects_without_aborting(self) -> None:
        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: (_ for _ in ()).throw(RuntimeError("risk offline")),
        )
        self.assertEqual(result["status"], "REJECT")
        self.assertIn("risk_metrics_provider_failed:RuntimeError",
                      result["reports"][0]["selection_blockers"])

    def test_parallel_workers_overlap_and_fills_self_attribute(self) -> None:
        barrier = Barrier(2)
        thread_ids: set[int] = set()
        lock = Lock()

        def execute(item, _event):
            with lock:
                thread_ids.add(get_ident())
            barrier.wait(timeout=2)
            return {"target_weight": item["target_weight"]}

        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a"), bundle("alpha-b")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=execute,
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: {
                "approved": True, "max_drawdown_limit": "0.2", "minimum_trades": 1,
                "execution_feasibility": "1",
            },
            max_workers=2,
        )
        self.assertEqual(len(thread_ids), 2)
        for report in result["reports"]:
            self.assertTrue(report["fills"])
            self.assertEqual(report["fills"][0]["strategy_id"], report["strategy_id"])
            self.assertEqual(report["fills"][0]["strategy_version"], report["strategy_version"])

    def test_first_fill_cost_counts_toward_drawdown(self) -> None:
        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: {
                "approved": True,
                "max_drawdown_limit": "0",
                "minimum_trades": 1,
                "execution_feasibility": "1",
            },
        )
        self.assertEqual(result["status"], "REJECT")
        self.assertIn("max_drawdown_exceeded", result["reports"][0]["selection_blockers"])

    def test_string_risk_approval_cannot_pass(self) -> None:
        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            **runtime_kwargs(),
            risk_metrics_provider=lambda _report: {
                "approved": "true",
                "max_drawdown_limit": "0.2",
                "minimum_trades": 1,
                "execution_feasibility": "1",
            },
        )
        self.assertEqual(result["status"], "REJECT")
        self.assertIn("risk_rejected", result["reports"][0]["selection_blockers"])

    def test_iam_denial_blocks_promotion(self) -> None:
        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            strategy_executor_version="test-executor-v1",
            iam_permission_provider=lambda _selection: {"granted": False, "reason": "denied"},
            risk_metrics_provider=lambda _report: {
                "approved": True,
                "max_drawdown_limit": "0.2",
                "minimum_trades": 1,
                "execution_feasibility": "1",
            },
        )
        self.assertEqual(result["status"], "REJECT")
        self.assertEqual(result["selected_strategy"]["promotion_state"], "IAM_DENIED")
        self.assertEqual(result["reports"][0]["worker_lifecycle_state"], "IAM_DENIED")

    def test_iam_forbidden_scope_blocks_promotion(self) -> None:
        def excessive_permission(selection):
            return {
                "granted": True,
                "permission_id": "iam-admin",
                "worker_id": selection["worker_id"],
                "strategy_id": selection["strategy_id"],
                "strategy_version": selection["strategy_version"],
                "scopes": ["broker.submit"],
            }

        result = pipeline.run_alpha_strategy_selection(
            [bundle("alpha-a")],
            [{"as_of": "t1", "price": "100"}],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            strategy_executor_version="test-executor-v1",
            iam_permission_provider=excessive_permission,
            risk_metrics_provider=lambda _report: {
                "approved": True,
                "max_drawdown_limit": "0.2",
                "minimum_trades": 1,
                "execution_feasibility": "1",
            },
        )
        self.assertEqual(result["status"], "REJECT")
        self.assertEqual(result["selected_strategy"]["promotion_state"], "IAM_DENIED")


if __name__ == "__main__":
    unittest.main()
