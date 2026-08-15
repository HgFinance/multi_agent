from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "departments" / "01-research"
QUANT = ROOT / "departments" / "04-quant-backtest" / "pipeline"
for path in (RESEARCH / "contracts", RESEARCH / "factory", RESEARCH, QUANT):
    sys.path.insert(0, str(path))

import ast_experience  # noqa: E402
import factory_bridge  # noqa: E402


OFI_1 = {"op": "rank", "arg": {
    "op": "ts_mean", "field": "order_flow_imbalance", "n": 1}}
OFI_5 = {"op": "rank", "arg": {
    "op": "ts_mean", "field": "order_flow_imbalance", "n": 5}}


def test_memory_distinguishes_exact_reuse_from_same_tuning_shape():
    memory = ast_experience.build([
        {"signal_expr": OFI_1, "decision": "GATE_HOLD",
         "lesson_codes": ["UNDERPOWERED_DATA"],
         "oos_summary": {"signal_ic": 0.02, "signal_ic_t": 1.1}},
        {"signal_expr": OFI_1, "decision": "GATE_HOLD", "lesson_codes": []},
    ], [
        {"lead_id": "new-window", "signal_expr": OFI_5, "used": False},
        {"lead_id": "same-formula", "signal_expr": OFI_1, "used": False},
    ])

    assert memory.duplicate_trials == 1
    assert memory.unused_novel_leads[0]["nearest_tested_similarity"] == 1.0
    assert memory.unused_recycled_leads[0]["lead_ids"] == ["same-formula"]
    assert "traded_value" in memory.untested_micro_fields


def test_exact_ast_negative_history_blocks_edge_name_laundering():
    proposal = factory_bridge._prop(
        edge_type="momentum", suggested_params={"signal_expr": OFI_1})
    history = [{"decision": "GATE_HOLD",
                "lesson_codes": ["UNDERPOWERED_DATA"],
                "match_scope": "AST_EXACT"}]

    gate = factory_bridge.gate0(proposal, trials_used=0, past_outcomes=history)

    assert not gate.ok
    assert "AST_DUPLICATE_UNADDRESSED" in gate.codes
