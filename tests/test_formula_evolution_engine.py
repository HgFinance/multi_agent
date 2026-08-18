from __future__ import annotations

from pathlib import Path
import math
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (FACTORY, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_ast_contract as grammar  # noqa: E402
from formula_evolution_engine import (  # noqa: E402
    EvolutionConfig,
    FailedSubtree,
    FormulaEvolutionEngine,
    FormulaOutcome,
    FormulaSeed,
    describe_economic_niche,
    subtree_shape_fingerprint,
    subtree_shape_fingerprints,
)


def field(name: str) -> dict:
    return {"op": "field", "field": name}


BOOK = {
    "op": "rolling_mean", "seconds": 10,
    "arg": field("queue_imbalance_l1"),
}
TAPE = {
    "op": "rolling_mean", "seconds": 30,
    "arg": field("trade_flow_imbalance"),
}
MICROPRICE = {
    "op": "delta", "seconds": 5,
    "arg": field("microprice_offset_bps"),
}

IDENTITY_A = "a" * 64
IDENTITY_B = "b" * 64
SEMANTIC_FAST = "1" * 64
SEMANTIC_SLOW = "2" * 64
EXPOSURE_A = "e" * 64
SOURCE_CONTRACT_BOOK = "3" * 64


def seed(expr: dict, name: str, source: str = "MANUAL") -> FormulaSeed:
    return FormulaSeed(
        expression=expr, seed_id=name, source=source,
        economic_mechanism=f"mechanism:{name}",
    )


def signatures(batch) -> list[tuple]:
    return [
        (row.candidate_id, row.expression, row.arm, row.operation,
         row.parent_fingerprints)
        for row in batch.candidates
    ]


def test_generation_is_deterministic_typed_and_population_sized() -> None:
    config = EvolutionConfig(
        deterministic_seed=71, population_size=20,
        exploration_fraction=0.5,
    )
    engine = FormulaEvolutionEngine(config)
    inputs = [seed(BOOK, "book"), seed(TAPE, "tape"),
              seed(MICROPRICE, "microprice")]

    first = engine.generate_population(seeds=inputs, generation=4)
    second = engine.generate_population(seeds=inputs, generation=4)

    assert signatures(first) == signatures(second)
    assert len(first.candidates) == 20
    assert first.kpi.shortfall == 0
    assert first.kpi.attempted >= first.kpi.emitted
    assert 0 < first.kpi.yield_rate <= 1
    assert first.kpi.candidates_per_second > 0
    assert first.kpi.emitted_by_arm == {
        "EXPLORATION": 20, "EXPLOITATION": 0}
    assert first.audit["exploit_parent_count"] == 0
    assert len({row.fingerprint for row in first.candidates}) == 20
    assert len({row.shape_fingerprint for row in first.candidates}) == 20
    for row in first.candidates:
        assert grammar.parse(row.expression) == row.expression
        assert grammar.validate_completed_second_candidate(
            row.expression, execution="TAKER") == row.expression
        assert row.niche.output_unit == grammar.unit_of(row.expression)
        assert row.promotion_authority is False
        assert row.adaptive_selection is True
        assert row.requires_preregistered_evaluation is True


def test_llm_candidate_is_only_a_seed_and_receives_no_authority() -> None:
    llm = FormulaSeed.from_llm(
        {"intraday_signal_expr": BOOK}, seed_id="hermes-17",
        economic_mechanism="persistent visible-book pressure",
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=1, population_size=4,
        exploration_fraction=1.0,
    )).generate_population(seeds=[llm])

    original = next(row for row in batch.candidates
                    if row.fingerprint == grammar.fingerprint(BOOK))
    assert original.origin == "LLM"
    assert original.operation == "SEED"
    assert original.parent_fingerprints == ()
    assert original.economic_mechanism == \
        "persistent visible-book pressure"
    assert batch.audit["fitness_computed_by_engine"] is False
    assert batch.audit["coefficient_fitting"] is False
    assert batch.audit["promotion_authority"] is False


def test_exact_and_parameter_insensitive_shape_archives_are_not_retested() -> None:
    tuned_clock = {**BOOK, "seconds": 60}
    engine = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=9, population_size=10,
    ))
    batch = engine.generate_population(
        seeds=[seed(BOOK, "book"), seed(TAPE, "tape")],
        known_expressions=[tuned_clock],
    )

    archived_shape = grammar.shape_fingerprint(BOOK)
    assert all(row.fingerprint != grammar.fingerprint(tuned_clock)
               for row in batch.candidates)
    assert all(row.shape_fingerprint != archived_shape
               for row in batch.candidates)
    assert batch.kpi.rejection_counts["SHAPE_DUPLICATE"] >= 1


def test_repeated_failed_subtree_is_avoided_without_altering_scores() -> None:
    losing_term = {
        "op": "rolling_mean", "seconds": 30,
        "arg": field("queue_imbalance_l10"),
    }
    blocked = FailedSubtree(
        subtree_fingerprint=subtree_shape_fingerprint(losing_term), support=3,
        reason="three independent adaptive losses",
    )
    seeds = [seed(losing_term, "loser"), seed(TAPE, "tape"),
             seed(MICROPRICE, "microprice")]
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=5, population_size=12,
        min_failed_subtree_support=2,
    )).generate_population(seeds=seeds, failed_subtrees=[blocked])

    assert len(batch.candidates) == 12
    assert all(blocked.subtree_fingerprint not in
               subtree_shape_fingerprints(row.expression)
               for row in batch.candidates)
    assert batch.kpi.rejection_counts["FAILED_SUBTREE"] >= 1
    assert batch.audit["blocked_subtree_shapes"] == 1


def test_failure_record_below_support_is_a_soft_memory_item_not_a_veto() -> None:
    weak = FailedSubtree(
        subtree_fingerprint=subtree_shape_fingerprint(BOOK), support=1)
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=13, population_size=3,
        exploration_fraction=1.0, min_failed_subtree_support=2,
    )).generate_population(seeds=[seed(BOOK, "book")],
                           failed_subtrees=[weak])

    assert any(row.fingerprint == grammar.fingerprint(BOOK)
               for row in batch.candidates)
    assert batch.audit["blocked_subtree_shapes"] == 0


def test_survivor_results_drive_exploitation_with_parent_lineage_and_crossover() -> None:
    outcomes = [
        FormulaOutcome(
            expression=BOOK, outcome="SURVIVED", observation_id="screen-book",
            search_score=2.0, evidence_scope="ADAPTIVE_SCREENING",
            economic_mechanism="book persistence",
            candidate_identity_fingerprint=IDENTITY_A,
            source_lead_ids=("lead-book",)),
        FormulaOutcome(
            expression=TAPE, outcome="SURVIVED", observation_id="screen-tape",
            search_score=1.0, evidence_scope="ADAPTIVE_SCREENING",
            economic_mechanism="tape persistence",
            candidate_identity_fingerprint=IDENTITY_B,
            source_lead_ids=("lead-tape",)),
    ]
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=44, population_size=18,
        exploration_fraction=0.0,
    )).generate_population(seeds=[], outcomes=outcomes)

    parent_fps = {grammar.fingerprint(BOOK), grammar.fingerprint(TAPE)}
    assert batch.kpi.emitted_by_arm == {
        "EXPLORATION": 0, "EXPLOITATION": 18}
    assert all(row.parent_fingerprints for row in batch.candidates)
    assert all(set(row.parent_fingerprints) <= parent_fps
               for row in batch.candidates)
    assert any(len(row.parent_fingerprints) == 2
               and row.operation.startswith("CROSSOVER_")
               for row in batch.candidates)
    assert all(set(row.parent_seed_ids) <= {"lead-book", "lead-tape"}
               for row in batch.candidates)
    assert all("screen-book" not in row.parent_seed_ids
               and "screen-tape" not in row.parent_seed_ids
               for row in batch.candidates)
    assert all(row.shape_fingerprint not in {
        grammar.shape_fingerprint(BOOK), grammar.shape_fingerprint(TAPE)}
        for row in batch.candidates)


def test_forward_lockbox_result_cannot_be_used_for_parent_selection() -> None:
    lockbox = FormulaOutcome(
        expression=BOOK, outcome="SURVIVED", observation_id="secret-forward",
        search_score=999_999.0, evidence_scope="FORWARD_LOCKBOX",
    )
    adaptive = FormulaOutcome(
        expression=TAPE, outcome="SURVIVED", observation_id="adaptive-screen",
        search_score=-1.0, evidence_scope="ADAPTIVE_SCREENING",
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=19, population_size=8,
        exploration_fraction=0.0,
    )).generate_population(seeds=[], outcomes=[lockbox, adaptive])

    tape_fp = grammar.fingerprint(TAPE)
    lockbox_fp = grammar.fingerprint(BOOK)
    assert all(tape_fp in row.parent_fingerprints for row in batch.candidates)
    assert all(lockbox_fp not in row.parent_fingerprints
               for row in batch.candidates)
    assert batch.audit["lockbox_survivors_ignored_for_selection"] == 1


def test_nonfinite_search_score_is_rejected_not_ranked_as_a_winner() -> None:
    bad = FormulaOutcome(
        expression=BOOK, outcome="SURVIVED", observation_id="bad",
        search_score=math.inf,
    )
    engine = FormulaEvolutionEngine(EvolutionConfig(population_size=3))
    with pytest.raises(ValueError, match="no contract-valid"):
        engine.generate_population(seeds=[], outcomes=[bad])


def test_completed_second_lane_rejects_sequence_dependent_llm_seed() -> None:
    unavailable = seed(field("normalized_quote_ofi"), "sequence-only", "LLM")
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=12, population_size=4,
        completed_second_only=True,
    )).generate_population(seeds=[unavailable, seed(BOOK, "book")])

    assert batch.kpi.rejection_counts["INVALID_SEED"] == 1
    assert all("normalized_quote_ofi" not in grammar.fields_of(row.expression)
               for row in batch.candidates)


@pytest.mark.parametrize(
    ("expression", "pressure", "mechanism", "regime", "clock"),
    [
        (BOOK, "BOOK_PRESSURE", "PERSISTENCE", "UNCONDITIONED", "6_30S"),
        ({"op": "sub", "args": [
            {"op": "rolling_mean", "seconds": 5,
             "arg": field("trade_flow_imbalance")},
            {"op": "rolling_mean", "seconds": 60,
             "arg": field("trade_flow_imbalance")},
        ]}, "TRADE_FLOW", "CROSS_SCALE", "UNCONDITIONED", "31_300S"),
        ({"op": "where", "condition": {"op": "lt", "args": [
            field("spread_bps"), {"const": 5, "unit": "BPS"}]},
          "then": BOOK, "else": {"const": 0, "unit": "RATIO"}},
         "BOOK_PRESSURE", "STATE_CONDITIONAL", "LIQUIDITY", "6_30S"),
    ],
)
def test_economic_niche_descriptor_is_behavioral_not_a_fitness_claim(
        expression, pressure, mechanism, regime, clock) -> None:
    niche = describe_economic_niche(expression)
    assert (niche.pressure_source, niche.mechanism, niche.regime,
            niche.clock_bucket) == (pressure, mechanism, regime, clock)
    assert niche.output_unit == grammar.unit_of(expression)
    assert niche.key.count("/") == 4


def test_failed_subtree_record_accepts_intraday_memory_projection() -> None:
    expected = subtree_shape_fingerprint(BOOK)
    record = FailedSubtree.from_record({
        "subtree_fingerprint": expected,
        "losing_support": 4,
        "reason": "cost-net loss",
    })
    assert record == FailedSubtree(
        subtree_fingerprint=expected, support=4, reason="cost-net loss")


def test_large_cost_gap_abandons_failed_family_instead_of_cosmetic_inversion() -> None:
    failed = FormulaOutcome.from_result_row({
        "experiment_id": "exp-cost-gap",
        "intraday_signal_expr": BOOK,
        "decision": "GATE_HOLD",
        "failed_criteria": (
            "NO_COST_FEASIBLE_ENTRY|NON_POSITIVE_DIRECTIONAL_RELATION"),
        "calibration_observations": 262_042,
        "min_cost_hurdle_bps": 23.0,
        "max_calibrated_markout_bps": 3.98,
    })
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=88, population_size=8,
        exploration_fraction=0.5,
    )).generate_population(seeds=[seed(TAPE, "new-mechanism")],
                           outcomes=[failed])

    failed_fp = grammar.fingerprint(BOOK)
    assert batch.audit["cost_infeasible_families_abandoned"] == 1
    assert batch.audit["direction_inversion_eligible_failures"] == 0
    assert all(failed_fp not in row.parent_fingerprints
               for row in batch.candidates)
    assert all(row.operation != "FAILURE_MODE_DIRECTION_INVERSION"
               for row in batch.candidates)
    # Diagnostics are used only for an abandon decision; the engine does not
    # weaken the cost hurdle or invent a replacement fitness value.
    assert batch.audit["fitness_computed_by_engine"] is False
    assert batch.audit["coefficient_fitting"] is False


def test_cost_feasible_direction_failure_gets_one_auditable_inversion() -> None:
    failed = FormulaOutcome(
        expression=BOOK, outcome="FAILED", observation_id="wrong-sign",
        search_score=-2.0, evidence_scope="ADAPTIVE_SCREENING",
        economic_mechanism="book pressure continuation",
        lesson_codes=("NON_POSITIVE_DIRECTIONAL_RELATION",),
        diagnostics={
            "min_cost_hurdle_bps": 5.0,
            "max_calibrated_markout_bps": 8.0,
        },
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=91, population_size=6,
        exploration_fraction=0.5,
    )).generate_population(seeds=[seed(TAPE, "tape")], outcomes=[failed])

    inversions = [row for row in batch.candidates
                  if row.operation == "FAILURE_MODE_DIRECTION_INVERSION"]
    assert len(inversions) == 1
    assert inversions[0].expression == {
        "op": "neg", "arg": grammar.parse(BOOK)}
    assert inversions[0].parent_fingerprints == (grammar.fingerprint(BOOK),)
    assert inversions[0].origin == "FAILURE_MEMORY"
    assert inversions[0].promotion_authority is False


def test_newer_survivor_supersedes_older_exact_family_cost_failure() -> None:
    failed = FormulaOutcome(
        expression=BOOK, outcome="FAILED", observation_id="old-cost",
        evidence_scope="ADAPTIVE_SCREENING", observed_at="2026-08-01T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="root-book",
        lesson_codes=("NO_COST_FEASIBLE_ENTRY",), diagnostics={
            "min_cost_hurdle_bps": 23.0,
            "max_calibrated_markout_bps": 3.0,
        })
    recovered = FormulaOutcome(
        expression=BOOK, outcome="SURVIVED", observation_id="new-screen",
        search_score=0.7, evidence_scope="ADAPTIVE_SCREENING",
        observed_at="2026-08-02T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="root-book")
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=92, population_size=6,
        exploration_fraction=0.0)).generate_population(
            seeds=[], outcomes=[recovered, failed])

    assert batch.audit["cost_infeasible_families_abandoned"] == 0
    assert batch.audit["exploit_parent_count"] == 1
    assert all(row.arm == "EXPLOITATION" for row in batch.candidates)


def test_same_ast_different_identity_failure_does_not_retire_survivor_or_seed(
        ) -> None:
    """AST equality cannot collapse distinct horizon/evaluation identities."""
    survivor = FormulaOutcome(
        expression=BOOK, outcome="SURVIVED", observation_id="fast-f2",
        search_score=1.2, evidence_scope="ADAPTIVE_SCREENING",
        observed_at="2026-08-01T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_A,
        semantic_plan_fingerprint=SEMANTIC_FAST,
        economic_family_id="book-pressure",
        source_lead_ids=("lead-survivor",), root_lineage_id="root-fast",
        exposure_fingerprint=EXPOSURE_A,
    )
    failed_other_horizon = FormulaOutcome(
        expression=BOOK, outcome="FAILED", observation_id="slow-f2",
        evidence_scope="ADAPTIVE_SCREENING",
        observed_at="2026-08-02T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_B,
        semantic_plan_fingerprint=SEMANTIC_SLOW,
        economic_family_id="book-pressure",
        source_lead_ids=("lead-failed",), root_lineage_id="root-slow",
        lesson_codes=("NO_COST_FEASIBLE_ENTRY",), diagnostics={
            "min_cost_hurdle_bps": 23.0,
            "max_calibrated_markout_bps": 3.0,
        },
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=93, population_size=12,
        exploration_fraction=0.5, enable_crossover=False,
    )).generate_population(
        seeds=[seed(BOOK, "lead-fresh")],
        outcomes=[survivor, failed_other_horizon])

    assert batch.audit["cost_infeasible_families_abandoned"] == 1
    assert batch.audit["exploit_parent_count"] == 1
    assert batch.audit["exploration_parent_count"] == 1
    assert any(row.arm == "EXPLOITATION"
               and row.parent_seed_ids == ("lead-survivor",)
               for row in batch.candidates)
    assert any(row.arm == "EXPLORATION"
               and row.parent_seed_ids == ("lead-fresh",)
               for row in batch.candidates)
    assert all("lead-failed" not in row.parent_seed_ids
               for row in batch.candidates)


def test_same_identity_on_independent_root_does_not_retire_survivor() -> None:
    survivor = FormulaOutcome(
        expression=BOOK, outcome="SURVIVED", observation_id="root-a-f2",
        search_score=0.9, observed_at="2026-08-01T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="independent-root-a",
        source_lead_ids=("lead-root-a",),
    )
    independent_failure = FormulaOutcome(
        expression=BOOK, outcome="FAILED", observation_id="root-b-f2",
        observed_at="2026-08-02T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="independent-root-b",
        source_lead_ids=("lead-root-b",),
        lesson_codes=("NO_COST_FEASIBLE_ENTRY",), diagnostics={
            "min_cost_hurdle_bps": 23.0,
            "max_calibrated_markout_bps": 3.0,
        },
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=94, population_size=6,
        exploration_fraction=0.0, enable_crossover=False,
    )).generate_population(
        seeds=[], outcomes=[survivor, independent_failure])

    assert batch.audit["exploit_parent_count"] == 1
    assert all(row.parent_seed_ids == ("lead-root-a",)
               for row in batch.candidates)


def test_later_neutral_row_cannot_erase_measured_survivor() -> None:
    survivor = FormulaOutcome(
        expression=BOOK, outcome="SURVIVED", observation_id="f2-survivor",
        search_score=0.5, observed_at="2026-08-01T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="same-root", source_lead_ids=("lead-book",),
    )
    neutral = FormulaOutcome(
        expression=BOOK, outcome="SCREENING_ONLY",
        observation_id="final-unresolved",
        observed_at="2026-08-02T00:00:00Z",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="same-root", source_lead_ids=("lead-book",),
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=95, population_size=5,
        exploration_fraction=0.0, enable_crossover=False,
    )).generate_population(seeds=[], outcomes=[survivor, neutral])

    assert batch.audit["exploit_parent_count"] == 1
    assert all(row.parent_seed_ids == ("lead-book",)
               for row in batch.candidates)


def test_failed_seed_retirement_requires_exact_source_lead_provenance() -> None:
    failed = FormulaOutcome(
        expression=BOOK, outcome="FAILED", observation_id="failed-book",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="root-book", source_lead_ids=("lead-book",),
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=96, population_size=6,
        exploration_fraction=1.0, enable_crossover=False,
    )).generate_population(
        seeds=[seed(BOOK, "lead-book"), seed(TAPE, "lead-tape")],
        outcomes=[failed])

    assert batch.kpi.rejection_counts["FAILED_FORMULA_RESEED"] == 1
    assert batch.audit["exploration_parent_count"] == 1
    assert all("lead-book" not in row.parent_seed_ids
               for row in batch.candidates)


def test_failed_alias_retires_seed_by_exact_source_contract() -> None:
    failed_alias = FormulaOutcome(
        expression=BOOK, outcome="FAILED", observation_id="failed-alias-b",
        candidate_identity_fingerprint=IDENTITY_A,
        root_lineage_id="root-book", source_lead_ids=("lead-b",),
        source_contract_fingerprints=(SOURCE_CONTRACT_BOOK,),
    )
    canonical_alias = FormulaSeed(
        expression=BOOK, seed_id="lead-a", source="PERSISTED_LEAD",
        source_lead_ids=("lead-a", "lead-b"),
        source_contract_fingerprint=SOURCE_CONTRACT_BOOK,
        semantic_plan_fingerprint=SEMANTIC_FAST,
    )
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=97, population_size=6,
        exploration_fraction=1.0, enable_crossover=False,
    )).generate_population(seeds=[canonical_alias], outcomes=[failed_alias])

    assert batch.candidates == ()
    assert batch.kpi.rejection_counts[
        "FAILED_SOURCE_CONTRACT_RESEED"] == 1
    assert batch.audit["failed_source_contracts"] == 1


def test_result_row_preserves_durable_identity_and_provenance_fields() -> None:
    outcome = FormulaOutcome.from_result_row({
        "expression": BOOK,
        "decision": "SURVIVED",
        "observation_id": "screen-book",
        "candidate_identity_fingerprint": IDENTITY_A.upper(),
        "semantic_plan_fingerprint": SEMANTIC_FAST,
        "economic_family_id": "family-book",
        "source_lead_ids": ["lead-a", "lead-a", "lead-b"],
        "source_contract_fingerprints": [SOURCE_CONTRACT_BOOK],
        "root_lineage_id": "root-a",
        "exposure_fingerprint": EXPOSURE_A,
    })
    parent = FormulaEvolutionEngine._parse_outcome(outcome)

    assert parent.candidate_identity_fingerprint == IDENTITY_A
    assert parent.semantic_plan_fingerprint == SEMANTIC_FAST
    assert parent.economic_family_id == "family-book"
    assert parent.source_lead_ids == ("lead-a", "lead-b")
    assert parent.source_contract_fingerprints == (SOURCE_CONTRACT_BOOK,)
    assert parent.root_lineage_id == "root-a"
    assert parent.exposure_fingerprint == EXPOSURE_A


def test_large_batch_has_throughput_and_behavioral_diversity_kpis() -> None:
    fields = (
        "queue_imbalance_l1", "queue_imbalance_l10",
        "trade_flow_imbalance", "microprice_offset_bps",
        "depth_imbalance_slope", "signed_trade_volume",
    )
    seeds = [
        seed({"op": "rolling_mean", "seconds": 5 + index,
              "arg": field(name)}, f"bulk-{index}")
        for index, name in enumerate(fields)
    ]
    batch = FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=2026, population_size=128,
        exploration_fraction=0.5,
    )).generate_population(seeds=seeds, generation=7)

    assert batch.kpi.emitted == 128
    assert batch.kpi.shortfall == 0
    assert batch.kpi.candidates_per_second > 0
    assert batch.kpi.attempted < 128 * 10
    assert len({row.niche.key for row in batch.candidates}) >= 20
    assert len({row.fingerprint for row in batch.candidates}) == 128
    assert len({row.shape_fingerprint for row in batch.candidates}) == 128
