from __future__ import annotations

from datetime import datetime, timezone
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (FACTORY, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import evolution_candidate_intake as intake  # noqa: E402
import intraday_ast_contract as grammar  # noqa: E402


NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)
PARENT_EXPR = {
    "op": "mul",
    "args": [
        {"op": "rolling_mean", "seconds": 30,
         "arg": {"op": "field", "field": "queue_imbalance_l1"}},
        {"op": "field", "field": "realized_volatility_bps"},
    ],
}
CHILD_EXPR = {
    "op": "where",
    "condition": {"op": "lt", "args": [
        {"op": "field", "field": "spread_bps"},
        {"const": 5, "unit": "BPS"},
    ]},
    "then": PARENT_EXPR,
    "else": {"const": 0, "unit": "BPS"},
}


def parent() -> dict:
    return {
        "lead_id": "lead_parent_revision",
        "case_id": "old-case",
        "scout_lens": "ACADEMIC",
        "source_type": "PAPER",
        "refs": [{
            "url": "https://example.com/queue-pressure",
            "title": "Queue pressure",
            "accessed_at": NOW.isoformat(),
            "excerpt": "Queue pressure predicts short-horizon price changes.",
        }],
        "ast_contract": {
            "ast_readiness": "AST_READY",
            "research_lane": "INTRADAY_EVENT",
            "formula_discovery_version": "formula-discovery-v5",
            "formula_contract_complete": True,
            "alpha_candidate_eligible": True,
            "candidate_signal_expr": PARENT_EXPR,
            "source_baseline_expr": PARENT_EXPR,
            "derivation_mode": "MECHANISM_MUTATION",
            "derivation_transforms": ["MECHANISM_INTERACTION"],
        },
        "claimed_edge": "Queue pressure scaled by volatility",
        "stated_mechanism": "Visible depth pressure precedes urgent taker flow.",
        "market_context": "KRX stocks",
        "stated_failure_mode": "Wide spreads dominate the markout.",
    }


def candidate(expr: dict = CHILD_EXPR) -> dict:
    return {
        "title": "Tight-spread queue-pressure child",
        "candidate_signal_expr": expr,
        "semantic_plan": {
            "event": "QUOTE_IMBALANCE",
            "context": ["TIGHT_SPREAD"],
            "qualities": ["PERSISTENCE", "STATE_CONDITIONAL"],
            "direction": "FOLLOW",
            "output": "TAKER_NET_PNL",
            "execution": "TAKER",
            "horizon_seconds": 30,
        },
        "formula_thesis": {
            "target": "TAKER_NET_PNL",
            "functional_form": "STATE_CONDITIONAL",
            "expected_sign": "POSITIVE",
            "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
            "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
            "terms": {
                "queue_imbalance_l1": "PRESSURE",
                "realized_volatility_bps": "VOLATILITY",
                "spread_bps": "LIQUIDITY",
            },
            "identification": (
                "Persistent queue pressure must clear the full taker cost only "
                "when the observed spread is below five basis points."
            ),
        },
        "evolution_operators": ["STATE_CONDITION"],
        "derivation_transforms": [
            "MECHANISM_INTERACTION", "STATE_CONDITION"],
        "expected_increment": (
            "The spread gate removes states where the parent's gross markout "
            "cannot pay the executable hurdle."
        ),
        "ablations": ["remove spread gate", "remove volatility scale"],
        "economic_mechanism": (
            "Queue pressure is informative only while taker spread drag is bounded."
        ),
        "lessons_addressed": "CALIBRATION_COST_INFEASIBLE=exclude wide spreads",
    }


def test_build_evolved_lead_reuses_source_and_records_lineage():
    lead = intake.build_evolved_lead(
        parent(), candidate(), model_version="hermes-test",
        prompt_version="breeder-v1", as_known_at=NOW)

    contract = lead["ast_contract"]
    assert lead["inferred"] is True
    assert lead["refs"][0]["url"] == parent()["refs"][0]["url"]
    assert lead["refs"][0]["title"] == parent()["refs"][0]["title"]
    assert datetime.fromisoformat(
        lead["refs"][0]["accessed_at"].replace("Z", "+00:00")) == NOW
    assert contract["formula_contract_complete"] is True
    assert contract["evolution_role"] == "CHILD"
    assert contract["parent_ast_fingerprint"] == grammar.fingerprint(PARENT_EXPR)
    assert contract["ast_fingerprint"] == grammar.fingerprint(CHILD_EXPR)
    assert contract["lessons_addressed"].startswith(
        "CALIBRATION_COST_INFEASIBLE")


def test_build_evolved_lead_rejects_exact_parent_reuse():
    with pytest.raises(ValueError, match="exactly reuses"):
        intake.build_evolved_lead(
            parent(), candidate(PARENT_EXPR), model_version="hermes-test",
            prompt_version="breeder-v1", as_known_at=NOW)


def test_build_evolved_lead_rejects_unenriched_generator_placeholder():
    draft = candidate()
    draft["formula_thesis"] = dict(draft["formula_thesis"])
    draft["formula_thesis"]["identification"] = (
        "REQUIRES_HERMES_FALSIFIABLE_ECONOMIC_IDENTIFICATION")
    with pytest.raises(ValueError, match="substantive economic statement"):
        intake.build_evolved_lead(
            parent(), draft, model_version="hermes-test",
            prompt_version="breeder-v1", as_known_at=NOW)


def test_build_evolved_lead_rejects_unreplayable_completed_second_formula():
    draft = candidate({"op": "field", "field": "quote_event_ofi"})
    with pytest.raises(grammar.IntradayExprError, match="blocked fields"):
        intake.build_evolved_lead(
            parent(), draft, model_version="hermes-test",
            prompt_version="breeder-v1", as_known_at=NOW)


class _Cursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, *_):
        return None

    def fetchone(self):
        return self.row


class _Conn:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return _Cursor(self.row)


def test_submit_is_bounded_deduplicated_and_revision_aware(monkeypatch):
    p = parent()
    row = tuple(p[key] for key in (
        "lead_id", "case_id", "scout_lens", "source_type", "refs",
        "ast_contract", "claimed_edge", "stated_mechanism", "market_context",
        "stated_failure_mode"))
    persisted = []

    def fake_persist(_conn, leads, *, return_ids=False):
        persisted.extend(leads)
        result = (len(leads), 0, ["lead_revision_child"])
        return result if return_ids else result[:2]

    monkeypatch.setattr(intake.lead_intake, "persist", fake_persist)
    result = intake.submit(
        _Conn(row), parent_lead_id=p["lead_id"],
        candidates=[candidate(), candidate()], model_version="hermes-test",
        prompt_version="breeder-v1")

    assert result["accepted"] == 1
    assert result["new"] == 1
    assert len(result["rejected"]) == 1
    assert "duplicate AST" in result["rejected"][0]["reason"]
    assert len(persisted) == 1


def test_submit_rejects_unbounded_population_before_db_access():
    with pytest.raises(ValueError, match="exceeds"):
        intake.submit(
            _Conn(None), parent_lead_id="unused",
            candidates=[candidate()] * (intake.MAX_EVOLUTION_BATCH + 1),
            model_version="hermes", prompt_version="breeder-v1")
