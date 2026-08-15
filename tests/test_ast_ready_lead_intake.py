from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "departments" / "01-research"
sys.path.insert(0, str(RESEARCH))
sys.path.insert(0, str(RESEARCH / "factory"))

import lead_intake  # noqa: E402
from factory_contracts import MethodologyLeadV1  # noqa: E402


def _lead(block: dict) -> dict:
    return lead_intake.to_lead(
        block, lens="ACADEMIC", source_type="PAPER", case_id="case-test",
        model_version="test-model", prompt_version="ast-ready-v2")


def test_ast_ready_persists_validated_expression_in_source_ref():
    lead = _lead({
        "TITLE": "Return reversal", "URL": "https://example.test/paper",
        "MECHANISM": "Lagged returns reverse after liquidity-demand pressure.",
        "TESTABLE_WITH": "Rank the negative five-day mean of returns.",
        "READINESS": "AST_READY", "OBSERVABLES": ["returns"],
        "CANDIDATE_SIGNAL_EXPR": {
            "op": "neg", "arg": {"op": "ts_mean", "field": "returns", "n": 5}},
    })

    assert lead["status"] == "COMPLETE"
    assert lead["testability"] == "RULE_EXPRESSIBLE"
    assert lead["ast_contract"]["ast_readiness"] == "AST_READY"
    assert lead["ast_contract"]["candidate_signal_expr"]["arg"]["n"] == 5
    assert MethodologyLeadV1.model_validate(lead).refs[0].url == lead["refs"][0]["url"]


def test_ast_ready_rejects_observable_expression_disagreement():
    with pytest.raises(ValueError, match="do not match AST fields"):
        _lead({
            "TITLE": "Bad mapping", "URL": "https://example.test/bad",
            "MECHANISM": "returns reverse", "TESTABLE_WITH": "returns",
            "READINESS": "AST_READY", "OBSERVABLES": "close",
            "CANDIDATE_SIGNAL_EXPR": {"op": "ts_mean", "field": "returns", "n": 5},
        })


def test_ast_ready_rejects_semantic_proxy_substitution():
    with pytest.raises(ValueError, match="SEMANTIC_MISMATCH"):
        _lead({
            "TITLE": "Sentiment", "URL": "https://example.test/sentiment",
            "MECHANISM": "News sentiment predicts returns.",
            "TESTABLE_WITH": "Use an available liquidity proxy.",
            "READINESS": "AST_READY", "OBSERVABLES": "spread_bps",
            "CANDIDATE_SIGNAL_EXPR": {"op": "ts_mean", "field": "spread_bps", "n": 5},
        })


@pytest.mark.parametrize(
    ("readiness", "detail_field", "status"),
    [("DATA_BLOCKED", "MISSING_DATA", "BLOCKED"),
     ("SEMANTIC_MISMATCH", "MAPPING_LOSS", "UNUSABLE")],
)
def test_non_ready_leads_are_preserved_but_not_planner_ready(
        readiness: str, detail_field: str, status: str):
    lead = _lead({
        "TITLE": readiness, "URL": f"https://example.test/{readiness.lower()}",
        "MECHANISM": "A sourced economic mechanism.", "READINESS": readiness,
        detail_field: "required evidence is unavailable or not representable",
    })

    assert lead["status"] == status
    assert lead["ast_contract"]["ast_readiness"] == readiness
