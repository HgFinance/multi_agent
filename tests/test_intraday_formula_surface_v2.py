"""The v2 microstructure observables reach Scout, intake, and evolution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
FACTORY = ROOT / "departments" / "01-research" / "factory"
for path in (PIPELINE, CONTRACTS, FACTORY):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import alpha_evolution
import alpha_semantics as semantics
import factory_autopilot
import formula_discovery
import intraday_ast_contract as grammar
import lead_intake
import literature_derivation
import proposal_intake

from factory_contracts import (  # noqa: E402
    CompetingExplanation,
    DataRequirement,
    ExperimentProposalV1,
    MethodologyLeadV1,
    SourceRef,
    lead_id_for,
)


L1_L10_CONVERGENCE = {
    "op": "where",
    "condition": {
        "op": "lt",
        "args": [
            {
                "op": "abs",
                "arg": {
                    "op": "sub",
                    "args": [
                        {"op": "field", "field": "normalized_quote_ofi"},
                        {
                            "op": "field",
                            "field": "normalized_multi_level_quote_ofi_l10",
                        },
                    ],
                },
            },
            {"const": 0.25, "unit": "RATIO"},
        ],
    },
    "then": {
        "op": "mul",
        "args": [
            {
                "op": "add",
                "args": [
                    {"op": "field", "field": "normalized_quote_ofi"},
                    {
                        "op": "field",
                        "field": "normalized_multi_level_quote_ofi_l10",
                    },
                ],
            },
            {"const": 0.5, "unit": "RATIO"},
        ],
    },
    "else": {"const": 0, "unit": "RATIO"},
}

QUOTE_TAPE_CONFIRMATION = {
    "op": "where",
    "condition": {
        "op": "gt",
        "args": [
            {
                "op": "mul",
                "args": [
                    {"op": "field", "field": "normalized_quote_ofi"},
                    {"op": "field", "field": "trade_flow_imbalance"},
                ],
            },
            {"const": 0, "unit": "RATIO"},
        ],
    },
    "then": {
        "op": "mul",
        "args": [
            {
                "op": "add",
                "args": [
                    {"op": "field", "field": "normalized_quote_ofi"},
                    {"op": "field", "field": "trade_flow_imbalance"},
                ],
            },
            {"const": 0.5, "unit": "RATIO"},
        ],
    },
    "else": {"const": 0, "unit": "RATIO"},
}


def test_v2_fields_keep_units_and_physical_clocks_explicit() -> None:
    assert grammar.AST_VERSION == "intraday-alpha-ast-v2"
    assert (
        semantics.QUOTE_PRESSURE_FIELDS
        | semantics.TAPE_PRESSURE_FIELDS
        | semantics.EVENT_NORMALIZED_FIELDS
        | semantics.VOLUME_NORMALIZED_FIELDS
    ) <= set(grammar.FIELDS)
    expected_units = {
        "multi_level_quote_ofi_l10": "SHARES",
        "normalized_multi_level_quote_ofi_l10": "RATIO",
        "depth_imbalance_slope": "RATIO",
        "quote_ofi_depth_divergence": "RATIO",
        "quote_event_transition_count": "COUNT",
        "normalized_quote_ofi_per_event": "RATIO",
        "signed_trade_volume": "SHARES",
        "trade_volume": "SHARES",
        "trade_side_known_ratio": "RATIO",
        "quote_ofi_per_trade_volume": "RATIO",
    }
    for field, unit in expected_units.items():
        assert grammar.unit_of({"op": "field", "field": field}) == unit

    cross_clock = {
        "op": "rolling_mean",
        "seconds": 30,
        "arg": {
            "op": "add",
            "args": [
                {"op": "field", "field": "normalized_quote_ofi_per_event"},
                {"op": "field", "field": "quote_ofi_per_trade_volume"},
            ],
        },
    }
    assert grammar.clock_domains_of(cross_clock) == {
        grammar.WALL_TIME_CLOCK,
        grammar.QUOTE_EVENT_CLOCK,
        grammar.TRADE_VOLUME_CLOCK,
    }


@pytest.mark.parametrize(
    "field", sorted(grammar.COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS))
def test_completed_second_gate_preserves_local_grammar_but_blocks_sequence_fields(
        field: str) -> None:
    expr = {"op": "field", "field": field}

    # The strict grammar remains available for a future sequenced local feed.
    assert grammar.parse(expr) == expr
    with pytest.raises(
            grammar.IntradayExprError,
            match=rf"no deterministic within-second quote sequence.*{field}"):
        grammar.validate_completed_second_candidate(expr, execution="TAKER")


def test_completed_second_gate_is_taker_only_and_exposes_safe_surface() -> None:
    safe = {
        "op": "add",
        "args": [
            {"op": "field", "field": "queue_imbalance_l1"},
            {"op": "field", "field": "trade_flow_imbalance"},
        ],
    }
    assert grammar.validate_completed_second_candidate(
        safe, execution="TAKER") == grammar.parse(safe)
    with pytest.raises(grammar.IntradayExprError, match="TAKER only"):
        grammar.validate_completed_second_candidate(
            safe, execution="PASSIVE_FIFO_LOWER_BOUND")
    assert grammar.COMPLETED_SECOND_REPLAYABLE_FIELDS == (
        set(grammar.FIELDS) - grammar.COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS)
    assert grammar.COMPLETED_SECOND_RECOMMENDED_FIELDS == (
        grammar.COMPLETED_SECOND_REPLAYABLE_FIELDS)


def test_semantic_quality_labels_require_their_observable_paths() -> None:
    plan = {
        "event": "ORDER_FLOW",
        "context": ["ALL"],
        "qualities": [
            "L1_L10_CONVERGENCE",
            "QUOTE_TAPE_CONFIRMATION",
            "EVENT_NORMALIZED",
            "VOLUME_NORMALIZED",
        ],
        "direction": "FOLLOW",
        "output": "TAKER_NET_PNL",
        "execution": "TAKER",
        "horizon_seconds": 30,
    }
    fields = {
        "normalized_quote_ofi",
        "normalized_multi_level_quote_ofi_l10",
        "trade_flow_imbalance",
        "normalized_quote_ofi_per_event",
        "quote_ofi_per_trade_volume",
    }
    assert semantics.check_observables(
        plan,
        fields,
        operators={"where", "mul", "add", "sub", "abs"},
    )["ok"]

    missing = semantics.check_observables(
        plan,
        {"normalized_quote_ofi", "normalized_multi_level_quote_ofi_l10"},
        operators={"where", "add", "sub", "abs"},
    )
    assert not missing["ok"]
    assert any("QUOTE_TAPE_CONFIRMATION" in item for item in missing["missing"])
    assert any("EVENT_NORMALIZED" in item for item in missing["missing"])
    assert any("VOLUME_NORMALIZED" in item for item in missing["missing"])


def test_formula_contract_accepts_depth_and_quote_tape_economic_forms() -> None:
    confirmation = formula_discovery.assess(
        {
            "target": "TAKER_NET_PNL",
            "functional_form": "QUOTE_TAPE_CONFIRMATION",
            "expected_sign": "POSITIVE",
            "coefficient_policy": "STRUCTURE_ONLY",
            "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
            "terms": {
                "normalized_quote_ofi": "PRESSURE",
                "trade_flow_imbalance": "CONFIRMATION",
            },
            "identification": (
                "Quote pressure earns positive net markout only when signed tape agrees."
            ),
        },
        candidate=QUOTE_TAPE_CONFIRMATION,
        semantic_plan={"output": "TAKER_NET_PNL", "execution": "TAKER"},
        grammar=grammar,
    )
    assert set(confirmation["formula_math_profile"]["clock_domains"]) == {
        grammar.QUOTE_EVENT_CLOCK,
        grammar.TRADE_VOLUME_CLOCK,
    }

    convergence = formula_discovery.assess(
        {
            "target": "TAKER_NET_PNL",
            "functional_form": "L1_L10_CONFIRMATION",
            "expected_sign": "POSITIVE",
            "coefficient_policy": "STRUCTURE_ONLY",
            "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
            "terms": {
                "normalized_quote_ofi": "PRESSURE",
                "normalized_multi_level_quote_ofi_l10": "CONFIRMATION",
            },
            "identification": (
                "L1 pressure earns net markout only while deeper visible-book OFI agrees."
            ),
        },
        candidate=L1_L10_CONVERGENCE,
        semantic_plan={"output": "TAKER_NET_PNL", "execution": "TAKER"},
        grammar=grammar,
    )
    assert convergence["formula_contract_complete"] is True


def test_evolution_labels_cannot_rename_an_unrelated_child() -> None:
    parent = {"op": "field", "field": "normalized_quote_ofi"}
    derived = literature_derivation.assess(
        candidate=QUOTE_TAPE_CONFIRMATION,
        mode="MECHANISM_MUTATION",
        source_baseline=parent,
        transforms=["QUOTE_TAPE_CONFIRMATION"],
        novelty_rationale=(
            "Tape agreement separates replenishing displayed quotes from executed demand."
        ),
        ast_module=grammar,
    )
    assert derived["alpha_candidate_eligible"] is True

    child = alpha_evolution.assess_lineage(
        candidate=QUOTE_TAPE_CONFIRMATION,
        parent=parent,
        operators=["QUOTE_TAPE_CONFIRMATION"],
        expected_increment="Agreement should improve net markout after crossing cost.",
        ablations=["quote only", "tape only"],
        grammar=grammar,
    )
    assert child["evolution_role"] == "CHILD"

    unrelated = {
        "op": "where",
        "condition": {
            "op": "lt",
            "args": [
                {"op": "field", "field": "spread_bps"},
                {"const": 5, "unit": "BPS"},
            ],
        },
        "then": parent,
        "else": {"const": 0, "unit": "RATIO"},
    }
    with pytest.raises(ValueError, match="signed quote and tape pressure"):
        alpha_evolution.assess_lineage(
            candidate=unrelated,
            parent=parent,
            operators=["QUOTE_TAPE_CONFIRMATION"],
            expected_increment="A renamed spread state should not pass as confirmation.",
            ablations=["remove spread"],
            grammar=grammar,
        )


def test_lead_intake_persists_new_clock_and_confirmation_contract() -> None:
    lead = lead_intake.to_lead(
        {
            "TITLE": "Quote pressure confirmed by executed flow",
            "URL": "https://example.test/public-ofi-baseline",
            "MECHANISM": (
                "Displayed pressure is more credible when signed prints confirm demand."
            ),
            "COUNTERPARTY": "Urgent liquidity demand crosses against replenishing makers.",
            "READINESS": "AST_READY",
            "OBSERVABLES": ["normalized_quote_ofi", "trade_flow_imbalance"],
            "CANDIDATE_SIGNAL_EXPR": QUOTE_TAPE_CONFIRMATION,
            "RESEARCH_LANE": "INTRADAY_EVENT",
            "SEMANTIC_PLAN": {
                "event": "ORDER_FLOW",
                "context": ["ALL"],
                "qualities": ["QUOTE_TAPE_CONFIRMATION"],
                "direction": "FOLLOW",
                "output": "TAKER_NET_PNL",
                "execution": "TAKER",
                "horizon_seconds": 30,
            },
            "DERIVATION_MODE": "MECHANISM_MUTATION",
            "SOURCE_BASELINE_EXPR": {
                "op": "field",
                "field": "normalized_quote_ofi",
            },
            "DERIVATION_TRANSFORMS": ["QUOTE_TAPE_CONFIRMATION"],
            "NOVELTY_RATIONALE": (
                "Signed prints distinguish executed demand from displayed-book churn."
            ),
            "FORMULA_THESIS": {
                "target": "TAKER_NET_PNL",
                "functional_form": "QUOTE_TAPE_CONFIRMATION",
                "expected_sign": "POSITIVE",
                "coefficient_policy": "STRUCTURE_ONLY",
                "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
                "terms": {
                    "normalized_quote_ofi": "PRESSURE",
                    "trade_flow_imbalance": "CONFIRMATION",
                },
                "identification": (
                    "Quote pressure earns positive net markout only when tape agrees."
                ),
            },
        },
        lens="ACADEMIC",
        source_type="PAPER",
        case_id="microstructure-v2",
        model_version="test-model",
        prompt_version="factory-scout-v9",
    )
    contract = lead["ast_contract"]
    assert contract["alpha_candidate_eligible"] is True
    assert contract["derivation_transforms"] == ["QUOTE_TAPE_CONFIRMATION"]
    assert set(contract["formula_math_profile"]["clock_domains"]) == {
        grammar.QUOTE_EVENT_CLOCK,
        grammar.TRADE_VOLUME_CLOCK,
    }
    assert contract["formula_math_profile"]["grammar_version"] == grammar.AST_VERSION


def test_live_scout_and_planner_prompts_expose_only_executable_examples() -> None:
    scout = factory_autopilot._ast_scout_contract()
    planner = factory_autopilot.INTRADAY_PLANNER_NOTE
    for token in (
        "multi_level_quote_ofi_l10",
        "normalized_quote_ofi_per_event",
        "quote_ofi_per_trade_volume",
        "L1_L10_CONVERGENCE",
        "QUOTE_TAPE_CONFIRMATION",
        "EVENT_NORMALIZATION",
        "VOLUME_NORMALIZATION",
        grammar.WALL_TIME_CLOCK,
        grammar.QUOTE_EVENT_CLOCK,
        grammar.TRADE_VOLUME_CLOCK,
    ):
        assert token in scout
        assert token in planner or token in {
            grammar.QUOTE_EVENT_CLOCK,
            grammar.TRADE_VOLUME_CLOCK,
        }
    assert "mechanism seeds, not current replayable" in scout
    assert "MBO queue ids" in scout
    assert "TAKER only" in scout and "no passive quota" in scout
    assert "unordered completed-second" in scout
    assert "instrument_type=STOCK" in planner
    for field in grammar.COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS:
        assert field in scout
        assert field in planner

    examples = [
        line.strip().split("=", 1)[1]
        for line in scout.splitlines()
        if line.strip().startswith("SCORE_")
    ]
    assert len(examples) >= 6
    assert all(grammar.unit_of(json.loads(example)) == "RATIO"
               for example in examples)
    bps_examples = [
        line.strip().split("=", 1)[1]
        for line in scout.splitlines()
        if line.strip().startswith("BPS_")
    ]
    assert len(bps_examples) == 4
    assert all(grammar.unit_of(json.loads(example)) == "BPS"
               for example in bps_examples)
    for example in examples + bps_examples:
        fields = grammar.fields_of(json.loads(example))
        assert not fields & grammar.COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS


def test_proposal_intake_filters_unsafe_completed_second_sidecars() -> None:
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    taker_plan = {
        "event": "MICROPRICE_DISLOCATION",
        "context": ["ALL"],
        "qualities": ["LEVEL"],
        "direction": "REVERT",
        "output": "TAKER_NET_PNL",
        "execution": "TAKER",
        "horizon_seconds": 5,
    }
    passive_plan = {
        **taker_plan,
        "output": "PASSIVE_FILL_ADJUSTED_PNL",
        "execution": "PASSIVE_FIFO_LOWER_BOUND",
    }
    raw = {"op": "field", "field": "microprice_offset_bps"}
    primary_expr = {"op": "rolling_mean", "arg": raw, "seconds": 10}
    safe_sidecar = {"op": "rolling_mean", "arg": raw, "seconds": 30}
    blocked_sidecar = {"op": "field", "field": "normalized_quote_ofi"}
    passive_sidecar = {"op": "rolling_mean", "arg": raw, "seconds": 60}

    def make_lead(label: str, expr: dict, plan: dict) -> MethodologyLeadV1:
        ref = SourceRef(
            url=f"https://example.test/{label}",
            title=label,
            accessed_at=now,
            excerpt="bounded microstructure evidence",
        )
        fields = grammar.fields_of(expr)
        structure_only = grammar.unit_of(expr) != "BPS"
        return MethodologyLeadV1(
            lead_id=lead_id_for([ref]),
            case_id="completed-second-v4",
            scout_lens="ACADEMIC",
            source_type="PAPER",
            as_known_at=now,
            refs=(ref,),
            claimed_edge=label,
            stated_mechanism="signed stock microstructure pressure",
            ast_contract={
                "formula_discovery_version": "formula-discovery-v5",
                "formula_contract_complete": True,
                "alpha_candidate_eligible": True,
                "research_lane": "INTRADAY_EVENT",
                "candidate_signal_expr": expr,
                "source_baseline_expr": raw,
                "semantic_plan": plan,
                "formula_thesis": {
                    "target": plan["output"],
                    "functional_form": "MONOTONE",
                    "expected_sign": "STATE_DEPENDENT",
                    "coefficient_policy": (
                        "STRUCTURE_ONLY" if structure_only
                        else "PREREGISTERED_NO_OOS_FIT"),
                    "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
                    "terms": {field: "PRESSURE" for field in fields},
                    "identification": (
                        "Signed pressure must predict positive stock net markout "
                        "after the preregistered crossing-cost hurdle."),
                },
                "evolution_role": "CHILD" if label == "primary" else "SEED",
                "parent_signal_expr": raw if label == "primary" else None,
                "parent_ast_fingerprint": (
                    grammar.fingerprint(raw) if label == "primary" else ""),
            },
        )

    primary = make_lead("primary", primary_expr, taker_plan)
    safe = make_lead("safe", safe_sidecar, taker_plan)
    blocked = make_lead("blocked", blocked_sidecar, taker_plan)
    passive = make_lead("passive", passive_sidecar, passive_plan)
    leads = {lead.lead_id: lead for lead in (primary, safe, blocked, passive)}
    proposal = ExperimentProposalV1(
        proposal_id="before",
        case_id="completed-second-v4",
        as_known_at=now,
        lead_ids=(primary.lead_id,),
        economic_rationale="stock microprice pressure meets urgent liquidity demand",
        counterparty="urgent KRX stock liquidity taker",
        competing_explanation="data mining",
        competing_explanation_codes=(CompetingExplanation.DATA_MINING,),
        skeptic_sign="independent-worker",
        edge_type="order_flow_imbalance",
        universe_key="krx_all",
        falsification_tests=("net <= 0",),
        data_requirements=DataRequirement(
            tables=("market_quotes", "market_ticks"), min_history_days=60),
        suggested_params={
            "intraday_signal_expr": primary_expr,
            "horizon_seconds": 5,
            "execution": "TAKER",
            "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
        },
        research_lane="INTRADAY_EVENT",
        semantic_plan=taker_plan,
    )

    attached = proposal_intake._attach_intraday_screening_cohort(proposal, leads)
    population = attached.suggested_params["screening_population"]
    linked_ids = {
        lead_id
        for row in population
        for lead_id in row.get("source_lead_ids", [])
        if row.get("candidate_role") == "LINKED_CANDIDATE"
    }
    assert safe.lead_id in linked_ids
    assert blocked.lead_id not in linked_ids
    assert passive.lead_id not in linked_ids
    assert all(
        not grammar.fields_of(row["intraday_signal_expr"])
        & grammar.COMPLETED_SECOND_SEQUENCE_DEPENDENT_FIELDS
        for row in population
    )
    assert all(
        row["semantic_plan"]["execution"] == "TAKER"
        for row in population
    )

    assert attached.suggested_params["source_baseline_expr"] == raw
    assert all(row["source_baseline_expr"] == raw for row in population)
    assert attached.suggested_params["parent_ast_fingerprint"] == \
        grammar.fingerprint(raw)
    assert any(
        row["ast_fingerprint"] == grammar.fingerprint(raw)
        and row["candidate_role"] == "LINEAGE_PARENT"
        for row in population)

    blocked_primary = proposal.model_copy(update={
        "suggested_params": {
            **proposal.suggested_params,
            "intraday_signal_expr": blocked_sidecar,
        },
    })
    with pytest.raises(
            grammar.IntradayExprError, match="blocked fields: normalized_quote_ofi"):
        proposal_intake._attach_intraday_screening_cohort(
            blocked_primary, leads)


def test_same_ast_conflicting_candidate_contracts_do_not_merge_provenance() -> None:
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    expr = {
        "op": "rolling_mean",
        "seconds": 30,
        "arg": {"op": "field", "field": "microprice_offset_bps"},
    }
    follow_30 = {
        "event": "MICROPRICE_DISLOCATION",
        "context": ["ALL"],
        "qualities": ["PERSISTENCE"],
        "direction": "FOLLOW",
        "output": "TAKER_NET_PNL",
        "execution": "TAKER",
        "horizon_seconds": 30,
    }
    revert_600 = {
        **follow_30,
        "direction": "REVERT",
        "horizon_seconds": 600,
    }

    def make_lead(label: str, plan: dict, coefficient_policy: str) \
            -> MethodologyLeadV1:
        ref = SourceRef(
            url=f"https://example.test/contract/{label}", title=label,
            accessed_at=now, excerpt="bounded exact-contract evidence")
        return MethodologyLeadV1(
            lead_id=lead_id_for([ref]), case_id="exact-contract-cohort",
            scout_lens="ACADEMIC", source_type="PAPER", as_known_at=now,
            refs=(ref,), claimed_edge=label,
            stated_mechanism="microprice displacement persistence",
            ast_contract={
                "formula_discovery_version": "formula-discovery-v5",
                "formula_contract_complete": True,
                "alpha_candidate_eligible": True,
                "research_lane": "INTRADAY_EVENT",
                "candidate_signal_expr": expr,
                "semantic_plan": plan,
                "formula_thesis": {
                    "target": "TAKER_NET_PNL",
                    "functional_form": "MONOTONE",
                    "expected_sign": "STATE_DEPENDENT",
                    "coefficient_policy": coefficient_policy,
                    "decision_rule": "PREDICTED_MARKOUT_CLEARS_COST",
                    "terms": {"microprice_offset_bps": "PRESSURE"},
                    "identification": (
                        "Microprice displacement must predict positive net "
                        "markout after the preregistered cost hurdle."),
                },
                "evolution_role": "SEED",
            })

    exact_a = make_lead(
        "follow-30-a", follow_30, "PREREGISTERED_NO_OOS_FIT")
    exact_b = make_lead(
        "follow-30-b", follow_30, "PREREGISTERED_NO_OOS_FIT")
    wrong_semantics = make_lead(
        "revert-600", revert_600, "PREREGISTERED_NO_OOS_FIT")
    wrong_coefficient = make_lead(
        "follow-30-structure-only", follow_30, "STRUCTURE_ONLY")
    all_leads = (wrong_semantics, exact_b, wrong_coefficient, exact_a)
    proposal = ExperimentProposalV1(
        proposal_id="before", case_id="exact-contract-cohort",
        as_known_at=now, lead_ids=tuple(row.lead_id for row in all_leads),
        economic_rationale="microprice pressure meets urgent liquidity demand",
        counterparty="urgent KRX stock liquidity taker",
        competing_explanation="data mining",
        competing_explanation_codes=(CompetingExplanation.DATA_MINING,),
        skeptic_sign="independent-worker", edge_type="order_flow_imbalance",
        universe_key="krx_all", falsification_tests=("net <= 0",),
        data_requirements=DataRequirement(
            tables=("market_quotes", "market_ticks"), min_history_days=60),
        suggested_params={
            "intraday_signal_expr": expr,
            "horizon_seconds": 30,
            "execution": "TAKER",
            "entry_policy": "PREDICTED_MARKOUT_CLEARS_COST",
            "coefficient_policy": "PREREGISTERED_NO_OOS_FIT",
        },
        research_lane="INTRADAY_EVENT", semantic_plan=follow_30)

    expected_sources = tuple(sorted((exact_a.lead_id, exact_b.lead_id)))
    forward = proposal_intake._attach_intraday_screening_cohort(
        proposal, {row.lead_id: row for row in all_leads})
    reverse = proposal_intake._attach_intraday_screening_cohort(
        proposal, {row.lead_id: row for row in reversed(all_leads)})

    assert forward.lead_ids == expected_sources
    assert reverse.lead_ids == expected_sources
    assert reverse.proposal_id == forward.proposal_id
    cited = set(forward.lead_ids)
    for row in forward.suggested_params["screening_population"]:
        cited.update(row.get("source_lead_ids") or [])
    assert wrong_semantics.lead_id not in cited
    assert wrong_coefficient.lead_id not in cited
    assert proposal_intake._exact_semantic_plan_fingerprint(follow_30) != \
        proposal_intake._exact_semantic_plan_fingerprint(revert_600)

    mismatched_config = proposal.model_copy(update={
        "suggested_params": {**proposal.suggested_params,
                             "horizon_seconds": 600},
    })
    with pytest.raises(ValueError, match="horizon_seconds does not match"):
        proposal_intake._attach_intraday_screening_cohort(
            mismatched_config, {row.lead_id: row for row in all_leads})


def test_proposal_identity_binds_cohort_contract_not_sidecar_membership() -> None:
    base = {
        "research_lane": "INTRADAY_EVENT",
        "suggested_params": {
            "intraday_signal_expr": {
                "op": "field", "field": "microprice_offset_bps"},
            "execution": "TAKER",
            "screening_cohort_version": "intraday-screening-cohort-v3",
            "screening_population": [{"ast_fingerprint": "legacy-sidecar"}],
        },
    }
    same_contract_replay = {
        **base,
        "suggested_params": {
            **base["suggested_params"],
            "screening_population": [{"ast_fingerprint": "fresh-sidecar"}],
        },
    }
    repaired_v4 = {
        **same_contract_replay,
        "suggested_params": {
            **same_contract_replay["suggested_params"],
            "screening_cohort_version": "intraday-screening-cohort-v4",
        },
    }
    legacy_persisted = {
        **base,
        "suggested_params": {
            key: value for key, value in base["suggested_params"].items()
            if key not in {"screening_cohort_version", "screening_population"}
        },
    }
    same_v4_replay = {
        **repaired_v4,
        "suggested_params": {
            **repaired_v4["suggested_params"],
            "screening_population": [{"ast_fingerprint": "another-sidecar"}],
        },
    }

    def identity(material: dict) -> str:
        return proposal_intake.proposal_id_for(
            ["lead-primary"], "order_flow_imbalance", "krx_all",
            material=material)

    assert identity(base) == identity(same_contract_replay)
    assert identity(base) != identity(repaired_v4)
    assert identity(legacy_persisted) != identity(repaired_v4)
    assert identity(repaired_v4) == identity(same_v4_replay)


def _sidecar_novelty_fixture() -> tuple[dict, list[dict]]:
    base_plan = {
        "event": "MICROPRICE_DISLOCATION",
        "context": ["ALL"],
        "qualities": ["LEVEL"],
        "direction": "REVERT",
        "output": "TAKER_NET_PNL",
        "execution": "TAKER",
        "horizon_seconds": 5,
    }

    def field(name: str) -> dict:
        return {"op": "field", "field": name}

    def row(name: str, expr: dict, plan: dict) -> dict:
        return {
            "name": name,
            "ast_fingerprint": grammar.fingerprint(expr),
            "intraday_signal_expr": expr,
            "semantic_plan": plan,
            # Deliberately UUID-like metadata: selection must never read it.
            "source_lead_ids": [f"uuid-{name}"],
        }

    primary = row(
        "primary",
        {"op": "rolling_mean", "arg": field("microprice_offset_bps"),
         "seconds": 10},
        base_plan,
    )
    candidates = [
        row(
            f"near-clone-{seconds}",
            {"op": "rolling_mean", "arg": field("microprice_offset_bps"),
             "seconds": seconds},
            base_plan,
        )
        for seconds in (20, 30, 40)
    ]
    candidates.extend([
        row(
            "spread-acceleration",
            {"op": "delta", "arg": field("spread_bps"), "seconds": 30},
            {**base_plan, "event": "SPREAD_CHANGE",
             "qualities": ["ACCELERATION"], "direction": "FOLLOW"},
        ),
        row(
            "quote-depth-interaction",
            {"op": "mul", "args": [field("queue_imbalance_l10"),
                                      field("spread_bps")]},
            {**base_plan, "event": "QUOTE_IMBALANCE",
             "qualities": ["CROSS_SIGNAL_INTERACTION"],
             "direction": "CONDITIONAL"},
        ),
        row(
            "signed-tape-state",
            {
                "op": "where",
                "condition": {
                    "op": "gt",
                    "args": [field("trade_flow_imbalance"),
                             {"const": 0, "unit": "RATIO"}],
                },
                "then": field("microprice_offset_bps"),
                "else": {"op": "neg", "arg": field(
                    "microprice_offset_bps")},
            },
            {**base_plan, "event": "ORDER_FLOW",
             "qualities": ["STATE_CONDITIONAL"], "direction": "FOLLOW"},
        ),
        row(
            "volatility-persistence",
            {"op": "rolling_std", "arg": field("realized_volatility_bps"),
             "seconds": 60},
            {**base_plan, "event": "VOLATILITY_BURST",
             "qualities": ["PERSISTENCE"]},
        ),
    ])
    return primary, candidates


def test_sidecar_max_min_frontier_is_permutation_and_uuid_invariant() -> None:
    primary, candidates = _sidecar_novelty_fixture()

    def selected(rows: list[dict]) -> list[str]:
        return [row["ast_fingerprint"] for row in
                proposal_intake._select_novel_intraday_sidecars(
                    primary, rows, limit=4, grammar=grammar)]

    expected = selected(candidates)
    permutations = [
        list(reversed(candidates)),
        candidates[3:] + candidates[:3],
        sorted(candidates, key=lambda row: row["source_lead_ids"], reverse=True),
    ]
    for index, rows in enumerate(permutations):
        # Changing all source UUIDs as well as order cannot alter the frontier.
        changed_ids = [
            {**row, "source_lead_ids": [f"replacement-{index}-{position}"]}
            for position, row in enumerate(rows)
        ]
        assert selected(changed_ids) == expected


def test_sidecar_max_min_frontier_prevents_near_clone_crowding() -> None:
    primary, candidates = _sidecar_novelty_fixture()
    selected = proposal_intake._select_novel_intraday_sidecars(
        primary, candidates, limit=4, grammar=grammar)

    assert {row["name"] for row in selected} == {
        "spread-acceleration",
        "quote-depth-interaction",
        "signed-tape-state",
        "volatility-persistence",
    }
    assert not any(row["name"].startswith("near-clone-") for row in selected)
