from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parents[2]
TRADING = ROOT / "departments" / "02-trading"
if str(TRADING) not in sys.path:
    sys.path.insert(0, str(TRADING))
e2e_dir = str(ROOT / "tests" / "e2e")
if e2e_dir not in sys.path:
    sys.path.insert(0, e2e_dir)
for subdir in ("contracts", "oms", "broker", "multileg", "capability"):
    path = str(TRADING / subdir)
    if path not in sys.path:
        sys.path.insert(0, path)



from contracts import StrategySignal
_worker_spec = importlib.util.spec_from_file_location(
    "trading_alpha_worker_contracts",
    ROOT / "tests" / "test_trading_alpha_strategy_workers.py",
)
assert _worker_spec is not None and _worker_spec.loader is not None
_worker_module = importlib.util.module_from_spec(_worker_spec)
sys.modules[_worker_spec.name] = _worker_module
_worker_spec.loader.exec_module(_worker_module)


class SelectedStrategyTraceTest(unittest.TestCase):
    def test_selected_strategy_reaches_paper_ledger(self) -> None:
        selected_id = "6ba7b810-9dad-11d1-80b4-00c04fd430c8"
        rejected_id = "6ba7b811-9dad-11d1-80b4-00c04fd430c8"
        from test_paper_loop import PaperLoopTest

        loop = PaperLoopTest("runTest")
        loop.setUp()
        selected = _worker_module.pipeline.run_alpha_strategy_selection(
            [
                _worker_module.bundle(selected_id, "0.6"),
                _worker_module.bundle(rejected_id, "0.2"),
            ],
            [
                {
                    "as_of": loop.now,
                    "price": "100",
                    "instrument_id": str(loop.instrument),
                },
                {
                    "as_of": loop.now,
                    "price": "110",
                    "instrument_id": str(loop.instrument),
                },
            ],
            strategy_executor=lambda item, _event: {"target_weight": item["target_weight"]},
            risk_metrics_provider=lambda _report: {
                "approved": True,
                "max_drawdown_limit": "0.2",
                "minimum_trades": 1,
                "execution_feasibility": "1",
            },
            initial_cash=Decimal("1000000"),
            max_workers=2,
            **_worker_module.runtime_kwargs(),
        )
        self.assertEqual(selected["status"], "SELECTED")
        chosen = selected["selected_strategy"]
        self.assertEqual(chosen["promotion_state"], "PROMOTED")
        self.assertFalse(chosen["live_order_submission_allowed"])
        self.assertTrue(chosen["risk_gate_required"])
        selected_report = next(
            report for report in selected["reports"]
            if report["worker_id"] == chosen["worker_id"]
        )
        self.assertTrue(selected_report["signals"])
        worker_signal = selected_report["signals"][-1]
        signal = StrategySignal(
            strategy_id=UUID(worker_signal["strategy_id"]),
            strategy_version=worker_signal["strategy_version"],
            fund_id=loop.fund,
            book_id=loop.book,
            instrument_id=UUID(worker_signal["instrument_id"]),
            philosophy="momentum",
            target_weight=Decimal(str(worker_signal["target_weight"])),
            stage="paper",
            as_of=worker_signal["as_of"],
            valid_until=loop.now + timedelta(hours=1),
            trace_id=worker_signal["trace_id"],
        )
        opening = loop.snapshot()
        bridge = _worker_module.pipeline.propose_intents_from_selection(
            selected,
            [signal],
            nav=opening.nav,
            positions={},
            snapshots={loop.instrument: loop.market()},
            trade_case_id=loop.strategy,
            now=loop.now,
            env="paper",
        )
        self.assertEqual(bridge["promoted"]["strategy_id"], chosen["strategy_id"])
        self.assertEqual(len(bridge["intents"]), 1)
        intent = bridge["intents"][0]
        self.assertEqual(intent.trace_id, selected["trace_id"])

        _record, order = loop.route(intent)
        loop.fill_completely(order)
        loop.post_fills_to_ledger(order)
        closing = loop.snapshot()
        self.assertEqual(order.state.value, "FILLED")
        self.assertEqual(closing.quantity_of(loop.instrument), intent.quantity)
        self.assertEqual(sum(loop.ledger.trial_balance().values(), Decimal("0")), Decimal("0"))


if __name__ == "__main__":
    unittest.main()
