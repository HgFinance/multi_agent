from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
for path in (FACTORY, CONTRACTS, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import evolution_candidate_intake as evolution_intake  # noqa: E402
import formula_breeder as breeder  # noqa: E402
from factory_bridge import expected_edge_for  # noqa: E402
from factory_contracts import (  # noqa: E402
    CompetingExplanation,
    DataRequirement,
    ExperimentProposalV1,
    MethodologyLeadV1,
)
from intraday_experiment_runner import config_from_edge  # noqa: E402
import intraday_ast_contract as grammar  # noqa: E402
import proposal_intake  # noqa: E402


NOW = datetime(2026, 8, 18, tzinfo=timezone.utc)
LEGACY_EXPR = {"op": "field", "field": "trade_flow_imbalance"}
PLAN = {
    "event": "ORDER_FLOW",
    "context": ["ALL"],
    "qualities": ["PERSISTENCE"],
    "direction": "FOLLOW",
    "output": "TAKER_NET_PNL",
    "execution": "TAKER",
    "horizon_seconds": 30,
}
THESIS = {
    "target": "TAKER_NET_PNL",
    "functional_form": "MONOTONE",
    "expected_sign": "POSITIVE",
    "coefficient_policy": "STRUCTURE_ONLY",
    "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
    "terms": {"trade_flow_imbalance": "PRESSURE"},
    "identification": (
        "Signed flow pressure must predict positive stock net markout after "
        "the preregistered execution-cost hurdle."),
}


def _legacy_contract() -> dict:
    return {
        "ast_readiness": "AST_READY",
        "research_lane": "INTRADAY_EVENT",
        "formula_discovery_version": "formula-discovery-v5",
        "formula_contract_complete": True,
        "alpha_candidate_eligible": True,
        "candidate_signal_expr": LEGACY_EXPR,
        "source_baseline_expr": LEGACY_EXPR,
        "derivation_mode": "MECHANISM_MUTATION",
        "derivation_transforms": ["MECHANISM_INTERACTION"],
        "semantic_plan": PLAN,
        "formula_thesis": THESIS,
        "feature_window_contract_version":
            grammar.LEGACY_FEATURE_WINDOW_CONTRACT,
    }


def _breeder_lead(*, explicit: bool = False) -> dict:
    expression = (
        {**LEGACY_EXPR, "seconds": 30} if explicit else LEGACY_EXPR)
    contract = _legacy_contract()
    contract.update({
        "candidate_signal_expr": expression,
        "feature_window_contract_version": (
            grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT if explicit else
            grammar.LEGACY_FEATURE_WINDOW_CONTRACT),
    })
    return {
        "lead_id": "lead-explicit" if explicit else "lead-legacy",
        "title": "Signed tape persistence",
        "expression": expression,
        "fingerprint": grammar.fingerprint(expression),
        "used": False,
        "economic_mechanism": (
            "Signed stock trade pressure predicts short-horizon net markout."),
        "contract": contract,
    }


def _parent_mapping() -> dict:
    return {
        "lead_id": "source-parent-placeholder",
        "case_id": "v2-roundtrip",
        "scout_lens": "ACADEMIC",
        "source_type": "PAPER",
        "refs": [{
            "url": "https://example.test/signed-flow",
            "title": "Signed flow pressure",
            "accessed_at": NOW.isoformat(),
            "excerpt": "Signed order flow predicts short-horizon returns.",
        }],
        "ast_contract": _legacy_contract(),
        "claimed_edge": "Signed tape persistence",
        "stated_mechanism": (
            "Signed stock trade pressure predicts short-horizon net markout."),
        "market_context": "KRX stocks",
        "stated_failure_mode": "Crossing costs can dominate gross markout.",
    }


def _upgrade_draft() -> dict:
    batch = breeder.generate_from_records(
        leads=[_breeder_lead()], outcome_rows=[], population_size=8,
        generation=1,
        feature_window_contract_version=
            grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT)
    return next(
        row for row in batch["candidates"]
        if row["operation"] == "FEATURE_WINDOW_UPGRADE_30S")


def _enriched_candidate(draft: dict) -> dict:
    return {
        "title": "Explicit 30-second signed-flow child",
        "candidate_signal_expr": draft["expression"],
        "feature_window_contract_version":
            draft["feature_window_contract_version"],
        "semantic_plan": draft["semantic_plan_hint"],
        "formula_thesis": {
            **draft["formula_thesis_skeleton"],
            "identification": (
                "Signed pressure aggregated at a preregistered raw-event "
                "clock must retain positive net markout after costs."),
        },
        "evolution_operators": draft["suggested_evolution_operators"],
        "derivation_transforms": draft["suggested_evolution_operators"],
        "expected_increment": (
            "The explicit raw-event window tests whether pressure persistence "
            "survives executable costs."),
        "ablations": [
            "compare the legacy cohort lookback",
            "swap the primitive aggregation window",
        ],
        "economic_mechanism": (
            "Signed stock trade pressure predicts short-horizon net markout."),
        "novelty_rationale": (
            "This is an auditable search coordinate, not a claim of a new "
            "economic mechanism."),
    }


def test_production_breeder_targets_explicit_v2_and_v12(monkeypatch) -> None:
    monkeypatch.setattr(
        breeder, "load_records", lambda _conn: ([_breeder_lead()], []))

    batch = breeder.generate(
        object(), population_size=8, generation=1)

    assert batch["feature_window_contract_version"] == \
        grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
    assert batch["evaluator_version"] == breeder.EXPLICIT_EVALUATOR_VERSION
    assert batch["audit"]["target_evaluator_version"] == \
        breeder.EXPLICIT_EVALUATOR_VERSION
    assert batch["candidates"]
    assert all(
        row["feature_window_contract_version"] ==
        grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
        for row in batch["candidates"])
    assert all(
        grammar.validate_feature_window_contract(
            row["expression"],
            contract_version=grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT)
        for row in batch["candidates"])


def test_legacy_source_contract_identity_is_byte_stable_for_memory() -> None:
    # Frozen before explicit primitive-window contracts existed.  Changing it
    # would silently disconnect all accumulated v11 outcome/failure memory.
    assert breeder._source_contract_fingerprint(_breeder_lead()) == (
        "462d401493d508300a0439741a09c5f4e49f21ad6bc2ce2340e85d5f4ef58683")
    assert breeder._source_contract_fingerprint(
        _breeder_lead(explicit=True)) != \
        breeder._source_contract_fingerprint(_breeder_lead())


def test_v12_memory_feeds_v2_generation_without_mixing_v11() -> None:
    expression = _breeder_lead(explicit=True)["expression"]
    outcomes = [{
        "expression": expression,
        "decision": "FAILED",
        "observation_id": evaluator,
        "evaluator_version": evaluator,
        "cost_model_version": breeder.ACTIVE_COST_MODEL_VERSION,
        "evidence_scope": "ADAPTIVE_SCREENING",
        "lesson_codes": (
            ["LEGACY_FAILURE_MUST_NOT_MIX"]
            if evaluator == breeder.LEGACY_EVALUATOR_VERSION else []),
    } for evaluator in (
        breeder.LEGACY_EVALUATOR_VERSION,
        breeder.EXPLICIT_EVALUATOR_VERSION,
    )] + [{
        "expression": expression,
        "decision": "FAILED",
        "observation_id": "unversioned-memory",
        "evidence_scope": "ADAPTIVE_SCREENING",
    }, {
        # Even a forged v12 label cannot turn a legacy AST into measured V2
        # evidence; it must be migrated as a new, unmeasured seed coordinate.
        "expression": LEGACY_EXPR,
        "decision": "FAILED",
        "observation_id": "v12-label-on-v1-ast",
        "evaluator_version": breeder.EXPLICIT_EVALUATOR_VERSION,
        "cost_model_version": breeder.ACTIVE_COST_MODEL_VERSION,
        "evidence_scope": "ADAPTIVE_SCREENING",
    }]

    batch = breeder.generate_from_records(
        leads=[_breeder_lead(explicit=True)], outcome_rows=outcomes,
        population_size=8, generation=2,
        feature_window_contract_version=
            grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT)

    assert batch["outcome_memory_rows"] == 1
    assert batch["failure_memory_rows"] == 0
    assert batch["evaluator_version"] == breeder.EXPLICIT_EVALUATOR_VERSION


def test_legacy_upgrade_requires_migration_and_is_not_economic_novelty() -> None:
    draft = _upgrade_draft()
    assert draft["suggested_evolution_operators"] == [
        "PRIMITIVE_WINDOW_MIGRATION", "CLOCK_CHANGE"]
    candidate = _enriched_candidate(draft)

    missing_provenance = {
        **candidate,
        "evolution_operators": ["CLOCK_CHANGE"],
        "derivation_transforms": ["CLOCK_CHANGE"],
    }
    with pytest.raises(ValueError, match="PRIMITIVE_WINDOW_MIGRATION"):
        evolution_intake.build_evolved_lead(
            _parent_mapping(), missing_provenance,
            model_version="hermes-test", prompt_version="breeder-v2",
            as_known_at=NOW)

    evolved = evolution_intake.build_evolved_lead(
        _parent_mapping(), candidate,
        model_version="hermes-test", prompt_version="breeder-v2",
        as_known_at=NOW)
    contract = evolved["ast_contract"]
    assert contract["feature_window_contract_version"] == \
        grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
    assert contract["evolution_role"] == "WINDOW_SEARCH_CHILD"
    assert contract["economic_novelty"] is False
    assert contract["parent_feature_window_contract_version"] == \
        grammar.LEGACY_FEATURE_WINDOW_CONTRACT
    assert "PRIMITIVE_WINDOW_MIGRATION" in contract["evolution_operators"]
    assert "PRIMITIVE_WINDOW_MIGRATION" in contract["derivation_transforms"]


def test_v2_contract_survives_lead_proposal_bridge_and_runner_config() -> None:
    draft = _upgrade_draft()
    evolved = evolution_intake.build_evolved_lead(
        _parent_mapping(), _enriched_candidate(draft),
        model_version="hermes-test", prompt_version="breeder-v2",
        as_known_at=NOW)
    lead = MethodologyLeadV1.model_validate(evolved)
    proposal = ExperimentProposalV1(
        proposal_id="before-v2-cohort",
        case_id="v2-roundtrip",
        as_known_at=NOW,
        lead_ids=(lead.lead_id,),
        economic_rationale=(
            "Signed stock tape imbalance may persist over an explicit event "
            "window after costs."),
        counterparty="Urgent stock liquidity takers",
        competing_explanation="Data mining",
        competing_explanation_codes=(CompetingExplanation.DATA_MINING,),
        skeptic_sign="independent-v2-review",
        edge_type="order_flow_imbalance",
        universe_key="krx_all",
        falsification_tests=("Net markout is nonpositive",),
        data_requirements=DataRequirement(
            tables=("market_quotes", "market_ticks"),
            min_history_days=60),
        suggested_params={
            "intraday_signal_expr": draft["expression"],
            "feature_window_contract_version":
                grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT,
            "horizon_seconds": 30,
            "execution": "TAKER",
            "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
            "coefficient_policy": "STRUCTURE_ONLY",
        },
        research_lane="INTRADAY_EVENT",
        semantic_plan=draft["semantic_plan_hint"],
    )

    attached = proposal_intake._attach_intraday_screening_cohort(
        proposal, {lead.lead_id: lead})
    params = attached.suggested_params
    assert params["feature_window_contract_version"] == \
        grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
    assert params["migration_parent_ast_fingerprint"] == \
        grammar.fingerprint(LEGACY_EXPR)
    assert params["migration_parent_feature_window_contract_version"] == \
        grammar.LEGACY_FEATURE_WINDOW_CONTRACT
    # An incompatible v11 parent is research provenance, not a false v12
    # same-evaluator runtime edge.
    assert params["parent_ast_fingerprint"] is None
    assert params["source_baseline_expr"] == LEGACY_EXPR
    assert all(
        row["feature_window_contract_version"] ==
        grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
        for row in params["screening_population"])

    edge, dropped = expected_edge_for(attached.model_dump(mode="json"))
    assert "feature_window_contract_version" not in dropped
    assert "migration_parent_ast_fingerprint" not in dropped
    assert edge["feature_window_contract_version"] == \
        grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
    assert edge["migration_parent_ast_fingerprint"] == \
        grammar.fingerprint(LEGACY_EXPR)

    config, _lane_spec = config_from_edge(edge)
    assert config["feature_window_contract_version"] == \
        grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT
    assert config["evaluator_version"] == breeder.EXPLICIT_EVALUATOR_VERSION
    assert config["source_baseline_expr"] == LEGACY_EXPR
    assert not config.get("parent_ast_fingerprint")
    assert config["migration_parent_ast_fingerprint"] == \
        grammar.fingerprint(LEGACY_EXPR)
    assert config[
        "migration_parent_feature_window_contract_version"] == \
        grammar.LEGACY_FEATURE_WINDOW_CONTRACT

    invalid_edge = dict(edge)
    invalid_edge["parent_ast_fingerprint"] = grammar.fingerprint(LEGACY_EXPR)
    with pytest.raises(ValueError, match="cannot be an in-cohort parent"):
        config_from_edge(invalid_edge)
