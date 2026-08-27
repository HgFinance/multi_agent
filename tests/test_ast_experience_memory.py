from __future__ import annotations

import sys
from pathlib import Path

import pytest


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


class _PersistCursor:
    def __init__(self, contracts=None):
        self.contracts = dict(contracts or {})
        self.lookup_id = ""
        self.phase = ""

    def execute(self, sql, params):
        if sql == lead_intake._SQL_LOCK_SOURCE:
            self.phase = "lock"
        elif sql == lead_intake._SQL_EXISTING_CONTRACT:
            self.phase = "existing"
            self.lookup_id = str(params[0])
        else:
            self.phase = "upsert"

    def fetchone(self):
        if self.phase == "existing":
            contract = self.contracts.get(self.lookup_id)
            return ((contract,) if contract is not None else None)
        if self.phase == "upsert":
            return (True,)
        return None


class _PersistConn:
    def __init__(self, contracts=None):
        self.cur = _PersistCursor(contracts)
        self.commits = 0

    def cursor(self):
        return self.cur

    def commit(self):
        self.commits += 1


def test_persist_can_return_actual_routed_ids_without_breaking_old_callers():
    source_id = "lead_0123456789abcdef"
    original = {"ast_readiness": "DATA_BLOCKED", "missing_data": "depth"}
    revised = {"ast_readiness": "AST_READY", "candidate_signal_expr": OFI_1}
    lead = {"lead_id": source_id, "refs": [{"url": "https://example.org/p"}],
            "ast_contract": revised}

    old_conn = _PersistConn()
    assert lead_intake.persist(old_conn, [lead]) == (1, 0)
    assert old_conn.commits == 1

    api_conn = _PersistConn(contracts={source_id: original})
    new, dup, lead_ids = lead_intake.persist(
        api_conn, [lead], return_ids=True)
    assert (new, dup) == (1, 0)
    assert lead_ids == [lead_intake.revision_lead_id(source_id, revised)]
    assert api_conn.commits == 1

    # The retired factory MCP surface must not be the handoff boundary anymore.
    # Outcome-conditioned evolution owns the validated revision IDs now.
    evolution_source = (ROOT / "departments" / "01-research" / "factory" /
                        "evolution_candidate_intake.py").read_text(encoding="utf-8")
    assert '"lead_ids": lead_ids' in evolution_source
    assert "return_ids=True" in evolution_source


def test_persist_rejects_a_truncated_revision_digest_collision(monkeypatch):
    source_id = "lead_0123456789abcdef"
    original = {"ast_readiness": "DATA_BLOCKED", "missing_data": "depth"}
    candidate = {"ast_readiness": "AST_READY", "candidate_signal_expr": OFI_1}
    colliding = {"ast_readiness": "AST_READY", "candidate_signal_expr": OFI_5}
    lead = {"lead_id": source_id, "refs": [{"url": "https://example.org/p"}],
            "ast_contract": candidate}
    monkeypatch.setattr(
        lead_intake, "revision_lead_id",
        lambda source, _contract: f"{source}_r000000000000")
    conn = _PersistConn(contracts={
        source_id: original,
        f"{source_id}_r000000000000": colliding,
    })

    with pytest.raises(RuntimeError, match="revision digest collision"):
        lead_intake.persist(conn, [lead])


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
