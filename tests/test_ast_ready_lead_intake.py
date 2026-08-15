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
    block = {
        "DERIVATION_MODE": "CROSS_DOMAIN_TRANSFER",
        "DERIVATION_TRANSFORMS": ["MARKET_STRUCTURE_TRANSFER"],
        "NOVELTY_RATIONALE": (
            "Transfer a non-financial event-response structure to Korean microstructure."),
        **block,
    }
    return lead_intake.to_lead(
        block, lens="CROSS_DOMAIN", source_type="PAPER", case_id="case-test",
        model_version="test-model", prompt_version="ast-ready-v2")


def test_ast_ready_persists_validated_microstructure_expression():
    lead = _lead({
        "TITLE": "Order-flow reversal", "URL": "https://example.test/paper",
        "MECHANISM": "Order flow imbalance reveals urgent liquidity-demand pressure.",
        "TESTABLE_WITH": "Rank the negative five-day mean of order_flow_imbalance.",
        "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
        "CANDIDATE_SIGNAL_EXPR": {
            "op": "neg", "arg": {
                "op": "ts_mean", "field": "order_flow_imbalance", "n": 5}},
    })

    assert lead["status"] == "COMPLETE"
    assert lead["testability"] == "RULE_EXPRESSIBLE"
    assert lead["ast_contract"]["ast_readiness"] == "AST_READY"
    assert lead["ast_contract"]["primary_data_plane"] == "MICROSTRUCTURE"
    assert lead["ast_contract"]["daily_data_role"] == (
        "EXECUTION_BENCHMARK_REGIME_AUXILIARY")
    assert lead["ast_contract"]["candidate_signal_expr"]["arg"]["n"] == 5
    assert len(lead["ast_contract"]["ast_fingerprint"]) == 16
    assert len(lead["ast_contract"]["ast_shape_fingerprint"]) == 16
    assert lead["ast_contract"]["alpha_candidate_eligible"] is True
    assert MethodologyLeadV1.model_validate(lead).refs[0].url == lead["refs"][0]["url"]


def test_direct_public_replication_is_kept_as_control_not_alpha_candidate():
    baseline = {"op": "ts_mean", "field": "order_flow_imbalance", "n": 5}
    lead = _lead({
        "TITLE": "Published OFI baseline", "URL": "https://example.test/control",
        "MECHANISM": "order_flow_imbalance measures urgent liquidity pressure",
        "TESTABLE_WITH": "five-day order_flow_imbalance baseline",
        "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
        "CANDIDATE_SIGNAL_EXPR": baseline,
        "DERIVATION_MODE": "DIRECT_REPLICATION",
        "SOURCE_BASELINE_EXPR": baseline,
        "DERIVATION_TRANSFORMS": [],
        "NOVELTY_RATIONALE": "",
    })

    contract = lead["ast_contract"]
    assert contract["novelty_classification"] == "PUBLIC_BASELINE_CONTROL"
    assert contract["alpha_candidate_eligible"] is False
    assert contract["candidate_vs_source_similarity"] == 1.0


def test_public_formula_window_tuning_is_not_a_mechanism_mutation():
    with pytest.raises(ValueError, match="tunable parameters"):
        _lead({
            "TITLE": "Window-tuned OFI", "URL": "https://example.test/tuned",
            "MECHANISM": "order_flow_imbalance measures urgent liquidity pressure",
            "TESTABLE_WITH": "ten-day order_flow_imbalance",
            "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
            "SOURCE_BASELINE_EXPR": {
                "op": "ts_mean", "field": "order_flow_imbalance", "n": 5},
            "CANDIDATE_SIGNAL_EXPR": {
                "op": "ts_mean", "field": "order_flow_imbalance", "n": 10},
            "DERIVATION_MODE": "MECHANISM_MUTATION",
            "DERIVATION_TRANSFORMS": ["CLOCK_CHANGE"],
            "NOVELTY_RATIONALE": "Use a slower clock.",
        })


def test_public_mechanism_interaction_is_eligible_when_ast_shape_changes():
    lead = _lead({
        "TITLE": "Spread-conditioned OFI", "URL": "https://example.test/derived",
        "MECHANISM": (
            "order_flow_imbalance pressure is informative when spread_bps shows costly liquidity"),
        "TESTABLE_WITH": "subtract spread_bps rank from order_flow_imbalance rank",
        "READINESS": "AST_READY",
        "OBSERVABLES": ["order_flow_imbalance", "spread_bps"],
        "SOURCE_BASELINE_EXPR": {
            "op": "rank", "arg": {
                "op": "ts_mean", "field": "order_flow_imbalance", "n": 5}},
        "CANDIDATE_SIGNAL_EXPR": {"op": "sub", "args": [
            {"op": "rank", "arg": {
                "op": "ts_mean", "field": "order_flow_imbalance", "n": 5}},
            {"op": "rank", "arg": {
                "op": "ts_mean", "field": "spread_bps", "n": 5}},
        ]},
        "DERIVATION_MODE": "MECHANISM_MUTATION",
        "DERIVATION_TRANSFORMS": ["MECHANISM_INTERACTION"],
        "NOVELTY_RATIONALE": "Use spread to separate costly informed pressure from noise.",
    })

    assert lead["ast_contract"]["alpha_candidate_eligible"] is True
    assert 0 < lead["ast_contract"]["candidate_vs_source_similarity"] < 1


def test_cross_domain_transfer_cannot_launder_an_academic_replication():
    with pytest.raises(ValueError, match="only valid.*CROSS_DOMAIN"):
        lead_intake.to_lead({
            "TITLE": "Academic relabel", "URL": "https://example.test/laundered",
            "MECHANISM": "order_flow_imbalance measures liquidity pressure",
            "TESTABLE_WITH": "one-day order_flow_imbalance",
            "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
            "CANDIDATE_SIGNAL_EXPR": {
                "op": "ts_mean", "field": "order_flow_imbalance", "n": 1},
            "DERIVATION_MODE": "CROSS_DOMAIN_TRANSFER",
            "DERIVATION_TRANSFORMS": ["MARKET_STRUCTURE_TRANSFER"],
            "NOVELTY_RATIONALE": "Relabel an academic source as cross-domain.",
        }, lens="ACADEMIC", source_type="PAPER", case_id="case-test",
            model_version="test-model", prompt_version="ast-ready-v2")


def test_ast_identity_distinguishes_exact_formula_from_tuning_shape():
    a = _lead({
        "TITLE": "OFI one day", "URL": "https://example.test/ofi-1",
        "MECHANISM": "order_flow_imbalance measures urgent liquidity pressure",
        "TESTABLE_WITH": "rank one-day order_flow_imbalance",
        "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
        "CANDIDATE_SIGNAL_EXPR": {"op": "rank", "arg": {
            "op": "ts_mean", "field": "order_flow_imbalance", "n": 1}},
    })
    b = _lead({
        "TITLE": "OFI five day", "URL": "https://example.test/ofi-5",
        "MECHANISM": "order_flow_imbalance measures persistent liquidity pressure",
        "TESTABLE_WITH": "rank five-day order_flow_imbalance",
        "READINESS": "AST_READY", "OBSERVABLES": ["order_flow_imbalance"],
        "CANDIDATE_SIGNAL_EXPR": {"op": "rank", "arg": {
            "op": "ts_mean", "field": "order_flow_imbalance", "n": 5}},
    })

    assert a["ast_contract"]["ast_fingerprint"] != b["ast_contract"]["ast_fingerprint"]
    assert (a["ast_contract"]["ast_shape_fingerprint"] ==
            b["ast_contract"]["ast_shape_fingerprint"])


def test_ast_ready_rejects_observable_expression_disagreement():
    with pytest.raises(ValueError, match="do not match AST fields"):
        _lead({
            "TITLE": "Bad mapping", "URL": "https://example.test/bad",
            "MECHANISM": "returns reverse", "TESTABLE_WITH": "returns",
            "READINESS": "AST_READY", "OBSERVABLES": "close",
            "CANDIDATE_SIGNAL_EXPR": {"op": "ts_mean", "field": "returns", "n": 5},
        })


def test_ast_ready_rejects_unknown_window_key_instead_of_silently_using_one_day():
    with pytest.raises(ValueError, match="unknown ts_mean key.*window"):
        _lead({
            "TITLE": "Misspelled window", "URL": "https://example.test/window",
            "MECHANISM": "realized_volatility predicts a subsequent return premium",
            "TESTABLE_WITH": "five-day realized_volatility predicts forward returns",
            "READINESS": "AST_READY", "OBSERVABLES": "realized_volatility",
            "CANDIDATE_SIGNAL_EXPR": {
                "op": "ts_mean", "field": "realized_volatility", "window": 5},
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


def test_ast_ready_rejects_daily_only_expression_even_for_liquidity_story():
    with pytest.raises(ValueError, match="MICROSTRUCTURE_PRIMARY_REQUIRED"):
        _lead({
            "TITLE": "Return-only liquidity proxy",
            "URL": "https://example.test/daily-proxy",
            "MECHANISM": "Liquidity demand creates short-term return reversal.",
            "TESTABLE_WITH": "Rank negative lagged returns.",
            "READINESS": "AST_READY", "OBSERVABLES": "returns",
            "CANDIDATE_SIGNAL_EXPR": {
                "op": "neg", "arg": {
                    "op": "ts_mean", "field": "returns", "n": 5}},
        })


def test_ast_ready_allows_daily_fields_only_as_microstructure_auxiliaries():
    lead = _lead({
        "TITLE": "Order flow conditioned reversal",
        "URL": "https://example.test/mixed",
        "MECHANISM": "Order flow imbalance and returns identify urgent liquidity demand.",
        "TESTABLE_WITH": "Subtract ranked returns from ranked order_flow_imbalance.",
        "READINESS": "AST_READY",
        "OBSERVABLES": ["order_flow_imbalance", "returns"],
        "CANDIDATE_SIGNAL_EXPR": {"op": "sub", "args": [
            {"op": "rank", "arg": {
                "op": "ts_mean", "field": "order_flow_imbalance", "n": 3}},
            {"op": "rank", "arg": {
                "op": "ts_mean", "field": "returns", "n": 3}},
        ]},
    })

    assert lead["ast_contract"]["primary_data_plane"] == "MICROSTRUCTURE"


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
