from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (FACTORY, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_ast_contract as grammar  # noqa: E402
from formula_evolution_engine import (  # noqa: E402
    EvolutionConfig,
    FormulaEvolutionEngine,
    FormulaOutcome,
    FormulaSeed,
)


INVARIANT_TERM = {
    "op": "rolling_mean",
    "seconds": 30,
    "arg": {"op": "field", "field": "queue_imbalance_l1"},
}
LEGACY_FAILURE = {
    "op": "add",
    "args": [
        INVARIANT_TERM,
        {"op": "field", "field": "trade_flow_imbalance"},
    ],
}


def _engine() -> FormulaEvolutionEngine:
    return FormulaEvolutionEngine(EvolutionConfig(
        deterministic_seed=20260819,
        population_size=4,
        exploration_fraction=1.0,
        enable_crossover=False,
        min_failed_subtree_support=2,
        feature_window_contract_version=
            grammar.EXPLICIT_FEATURE_WINDOW_CONTRACT,
    ))


def _fresh_seed() -> FormulaSeed:
    return FormulaSeed(
        expression=INVARIANT_TERM,
        seed_id="independent-state-pressure-seed",
        economic_mechanism="persistent visible queue pressure",
    )


def test_one_legacy_failure_is_one_evidence_unit_not_seven_vetoes() -> None:
    failed = FormulaOutcome(
        expression=LEGACY_FAILURE,
        outcome="FAILED",
        observation_id="one-v1-failure",
        candidate_identity_fingerprint="a" * 64,
        root_lineage_id="root-one-v1-failure",
        evidence_scope="ADAPTIVE_SCREENING",
    )

    batch = _engine().generate_population(
        seeds=[_fresh_seed()], outcomes=[failed])

    assert batch.audit["legacy_parents_upgraded"] == 1
    assert batch.audit["explicit_parent_variants"] == len(
        grammar.PRIMITIVE_WINDOWS_SECONDS)
    assert batch.audit["terminal_evidence_count"] == 1
    assert batch.audit["upgrade_translation_evidence_collapsed"] == \
        len(grammar.PRIMITIVE_WINDOWS_SECONDS) - 1
    assert batch.audit["max_observed_failed_subtree_support"] == 1
    assert batch.audit["blocked_subtree_shapes"] == 0
    assert any(
        row.expression == grammar.parse(INVARIANT_TERM)
        for row in batch.candidates)


def test_two_distinct_native_v2_failures_still_reach_veto_support() -> None:
    explicit_failure = {
        "op": "add",
        "args": [
            INVARIANT_TERM,
            {"op": "field", "field": "trade_flow_imbalance",
             "seconds": 30},
        ],
    }
    failures = [FormulaOutcome(
        expression=explicit_failure,
        outcome="FAILED",
        observation_id=f"native-v2-failure-{index}",
        candidate_identity_fingerprint=identity * 64,
        root_lineage_id=f"root-native-v2-{index}",
        evidence_scope="ADAPTIVE_SCREENING",
    ) for index, identity in enumerate(("b", "c"), start=1)]

    batch = _engine().generate_population(
        seeds=[_fresh_seed()], outcomes=failures)

    assert batch.audit["legacy_parents_upgraded"] == 0
    assert batch.audit["terminal_evidence_count"] == 2
    assert batch.audit["upgrade_translation_evidence_collapsed"] == 0
    assert batch.audit["max_observed_failed_subtree_support"] == 2
    assert batch.audit["blocked_subtree_shapes"] > 0
    assert batch.audit["exploration_parent_count"] == 0
    assert batch.kpi.rejection_counts["FAILED_SUBTREE"] >= 1
