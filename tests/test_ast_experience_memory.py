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
import lead_intake  # noqa: E402


OFI_1 = {"op": "rank", "arg": {
    "op": "ts_mean", "field": "order_flow_imbalance", "n": 1}}
OFI_5 = {"op": "rank", "arg": {
    "op": "ts_mean", "field": "order_flow_imbalance", "n": 5}}
DEPTH_10 = {"op": "rank", "arg": {
    "op": "ts_mean", "field": "depth_imbalance_l10", "n": 10}}
SPREAD_3 = {"op": "neg", "arg": {"op": "rank", "arg": {
    "op": "ts_mean", "field": "spread_bps", "n": 3}}}


def test_duplicate_source_cannot_rewrite_pit_ast_lineage():
    sql = " ".join(lead_intake._SQL_UPSERT.lower().split())

    assert "independent_mentions =" in sql
    assert "ast_contract = excluded" not in sql
    assert "as_known_at = excluded" not in sql


def test_same_source_new_ast_gets_deterministic_revision_lineage():
    base = "lead_0123456789abcdef"
    blocked = {"ast_readiness": "DATA_BLOCKED", "missing_data": "absolute depth"}
    ready = {"ast_readiness": "AST_READY", "candidate_signal_expr": {
        "op": "div", "args": [
            {"op": "ts_last", "field": "ofi_close", "n": 1},
            {"op": "ts_mean", "field": "book_depth_notional_l10", "n": 3},
        ]}}

    assert lead_intake.routed_lead_id(base, blocked, dict(blocked)) == base
    revised = lead_intake.routed_lead_id(base, blocked, ready)
    assert revised.startswith(base + "_r")
    assert revised == lead_intake.revision_lead_id(base, ready)
    assert revised != lead_intake.revision_lead_id(base, blocked)


def test_memory_distinguishes_exact_reuse_from_same_tuning_shape():
    memory = ast_experience.build([
        {"signal_expr": OFI_1, "decision": "GATE_HOLD",
         "lesson_codes": ["UNDERPOWERED_DATA"],
         "oos_summary": {"signal_ic": 0.02, "signal_ic_t": 1.1}},
        {"signal_expr": OFI_1, "decision": "GATE_HOLD", "lesson_codes": []},
    ], [
        {"lead_id": "new-window", "signal_expr": OFI_5, "used": False},
        {"lead_id": "same-formula", "signal_expr": OFI_1, "used": False},
        {"lead_id": "public-control", "signal_expr": OFI_1, "used": False,
         "alpha_candidate_eligible": False, "source_baseline_expr": OFI_1},
    ])

    assert memory.duplicate_trials == 1
    assert memory.unused_novel_leads[0]["nearest_tested_similarity"] == 1.0
    assert memory.unused_recycled_leads[0]["lead_ids"] == ["same-formula"]
    assert memory.public_baseline_controls[0]["lead_ids"] == ["public-control"]
    assert "traded_value" in memory.untested_micro_fields


def test_quality_diversity_frontier_prefers_coverage_over_window_only_tuning():
    memory = ast_experience.build([
        {"signal_expr": OFI_1, "decision": "GATE_HOLD",
         "lesson_codes": ["UNDERPOWERED_DATA"]},
    ], [
        {"lead_id": "window-only", "signal_expr": OFI_5, "used": False},
        {"lead_id": "deep-book", "signal_expr": DEPTH_10, "used": False},
        {"lead_id": "spread-state", "signal_expr": SPREAD_3, "used": False},
    ])

    frontier = memory.diverse_frontier
    assert len(frontier) == 3
    assert frontier[0]["lead_ids"] != ["window-only"]
    assert frontier[0]["coverage_gain_fields"]
    assert frontier[0]["nearest_library_similarity"] < 1.0
    assert any("depth_imbalance_l10@10" in row["clocks"] for row in frontier)
    assert "quality-diversity frontier" in ast_experience.render(memory)


def test_exact_ast_negative_history_blocks_edge_name_laundering():
    proposal = factory_bridge._prop(
        edge_type="momentum", suggested_params={"signal_expr": OFI_1})
    history = [{"decision": "GATE_HOLD",
                "lesson_codes": ["UNDERPOWERED_DATA"],
                "match_scope": "AST_EXACT"}]

    gate = factory_bridge.gate0(proposal, trials_used=0, past_outcomes=history)

    assert not gate.ok
    assert "AST_DUPLICATE_UNADDRESSED" in gate.codes
