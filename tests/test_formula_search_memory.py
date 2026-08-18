from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (FACTORY, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import intraday_ast_contract as grammar  # noqa: E402
from formula_search_memory import (  # noqa: E402
    SEARCH_OBJECTIVES_VERSION,
    build_formula_search_memory,
)


BOOK_L1 = {
    "op": "rolling_mean",
    "seconds": 10,
    "arg": {"op": "field", "field": "queue_imbalance_l1"},
}
BOOK_L10 = {
    "op": "rolling_mean",
    "seconds": 10,
    "arg": {"op": "field", "field": "queue_imbalance_l10"},
}


def stable_fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def history_row(
    *,
    expression: dict | None = None,
    exposure: str = "a" * 64,
    survivor: bool = True,
    evidence_scope: str = "F1",
    observed_at: str = "2026-08-18T00:00:00Z",
    objectives: dict | None = None,
    candidate_identity: str | None = None,
    semantic_plan_fingerprint: str | None = None,
    root_lineage_id: str = "root-lineage-a",
    source_lead_ids: list[str] | None = None,
    economic_family_id: str = "queue pressure",
) -> dict:
    expr = deepcopy(expression or BOOK_L1)
    ast_fingerprint = stable_fingerprint(expr)
    semantic_fingerprint = semantic_plan_fingerprint or stable_fingerprint({
        "economic_rationale": "queue pressure",
        "horizon_seconds": 10,
    })
    identity = candidate_identity or stable_fingerprint({
        "candidate_ast": ast_fingerprint,
        "semantic_plan": semantic_fingerprint,
        "evaluator_version": "intraday-candidate-evaluator-v11",
        "cost_model_version": "krx-intraday-execution-v3",
    })
    measured = {
        "version": SEARCH_OBJECTIVES_VERSION,
        "complete": True,
        "cost_net_bps": 1.5,
        "oos_sharpe": 0.7,
        "coverage_ratio": 0.6,
        "robustness_score": 0.7,
        "novelty_score": 0.5,
        "complexity_nodes": grammar.count_nodes(expr),
    }
    if objectives:
        measured.update(objectives)
    return {
        "expression": expr,
        "candidate_identity_fingerprint": identity,
        "candidate_ast_fingerprint": ast_fingerprint,
        "semantic_plan_fingerprint": semantic_fingerprint,
        "root_lineage_id": root_lineage_id,
        "source_lead_ids": source_lead_ids or ["lead-book"],
        "evidence_scope": evidence_scope,
        "explicit_survivor": survivor,
        "search_objectives": {
            "version": measured.pop("version"),
            "complete": measured.pop("complete"),
            "values": measured,
            "missing": [],
            "imputation": "NONE",
        },
        "exposure_fingerprint": exposure,
        "economic_family_id": economic_family_id,
        "evaluator_version": "intraday-candidate-evaluator-v11",
        "cost_model_version": "krx-intraday-execution-v3",
        "measurement_scope": "ADAPTIVE_RUNG_MEASURED",
        "horizon_seconds": 10,
        "clock_domains": sorted(grammar.effective_clock_domains_of(expr)),
        "sessions": 8,
        "opportunities": 120,
        "observed_at": observed_at,
    }


def archive_entries(result: dict) -> list[dict]:
    return result["state_snapshot"]["archive"]["entries"]


def test_level_formula_has_explicit_decision_snapshot_coordinate() -> None:
    level = {"op": "field", "field": "queue_imbalance_l1"}
    row = history_row(expression=level)

    result = build_formula_search_memory([row])

    assert row["clock_domains"] == ["DECISION_SNAPSHOT"]
    assert result["audit"]["archive_entries"] == 1


def test_inactive_evaluator_cost_cohort_cannot_become_parent() -> None:
    stale = history_row()
    stale["evaluator_version"] = "intraday-candidate-evaluator-v10"

    result = build_formula_search_memory(
        [stale],
        active_evaluator_version="intraday-candidate-evaluator-v11",
        active_cost_model_version="krx-intraday-execution-v3",
    )

    assert result["audit"]["inactive_contract_skips"] == 1
    assert result["audit"]["archive_entries"] == 0
    assert result["elite_quality_scores"] == {}


def test_calibration_only_resource_stop_never_enters_quality_archive() -> None:
    calibration_only = history_row()
    calibration_only["measurement_scope"] = "CALIBRATION_ONLY_RESOURCE_STOP"

    result = build_formula_search_memory([calibration_only])

    assert result["audit"]["calibration_resource_stop_skips"] == 1
    assert result["audit"]["archive_entries"] == 0
    assert result["elite_quality_scores"] == {}


def test_missing_objective_is_not_confused_with_measured_zero() -> None:
    missing = history_row(exposure="a" * 64)
    del missing["search_objectives"]["values"]["cost_net_bps"]
    measured_zero = history_row(
        exposure="b" * 64,
        objectives={"cost_net_bps": 0.0},
        observed_at="2026-08-18T00:01:00Z",
    )

    result = build_formula_search_memory([missing, measured_zero])

    assert result["audit"]["rejection_counts"] == {
        "INCOMPLETE_SEARCH_OBJECTIVES": 1,
    }
    assert result["audit"]["missing_values_filled_with_zero"] is False
    assert result["audit"]["archive_entries"] == 1
    assert archive_entries(result)[0]["evaluation"]["objectives"][
        "cost_net_bps"] == 0.0
    assert set(result["elite_quality_scores"]) == {
        measured_zero["candidate_identity_fingerprint"],
    }


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_nonfinite_objective_is_rejected_without_persisting_nan(
    value: float,
) -> None:
    result = build_formula_search_memory([
        history_row(objectives={"oos_sharpe": value}),
    ])

    assert result["audit"]["rejection_counts"] == {
        "NONFINITE_SEARCH_OBJECTIVE": 1,
    }
    assert result["audit"]["archive_entries"] == 0
    assert result["audit"]["effective_exposure_trials"] == 0
    json.dumps(result, allow_nan=False)


def test_only_one_best_formula_is_returned_for_a_niche() -> None:
    weak = history_row(
        expression=BOOK_L1,
        exposure="1" * 64,
        objectives={
            "cost_net_bps": -0.5,
            "oos_sharpe": 0.1,
            "coverage_ratio": 0.3,
            "robustness_score": 0.3,
            "novelty_score": 0.2,
        },
    )
    strong = history_row(
        expression=BOOK_L10,
        exposure="2" * 64,
        observed_at="2026-08-18T00:01:00Z",
        objectives={
            "cost_net_bps": 3.0,
            "oos_sharpe": 1.2,
            "coverage_ratio": 0.8,
            "robustness_score": 0.9,
            "novelty_score": 0.7,
        },
    )

    result = build_formula_search_memory([weak, strong])
    expected = strong["candidate_identity_fingerprint"]

    assert result["audit"]["archive_entries"] == 1
    assert result["audit"]["unique_results_accepted"] == 2
    assert set(result["elite_quality_scores"]) == {expected}
    assert archive_entries(result)[0]["evaluation"][
        "candidate_identity_fingerprint"] == expected
    assert result["elite_candidates"][expected] == {
        "candidate_identity_fingerprint": expected,
        "quality_score": result["elite_quality_scores"][expected],
        "ast_fingerprint": strong["candidate_ast_fingerprint"],
        "semantic_plan_fingerprint": strong[
            "semantic_plan_fingerprint"],
        "root_lineage_id": strong["root_lineage_id"],
        "source_lead_ids": strong["source_lead_ids"],
        "explicit_survivor": True,
        "expression": strong["expression"],
        "economic_family_id": strong["economic_family_id"],
        "evaluator_version": strong["evaluator_version"],
        "cost_model_version": strong["cost_model_version"],
        "evidence_scope": strong["evidence_scope"],
        "measurement_scope": strong["measurement_scope"],
        "observed_at": "2026-08-18T00:01:00Z",
        "exposure_fingerprint": strong["exposure_fingerprint"],
    }


def test_archive_elite_without_explicit_survivor_is_not_a_parent() -> None:
    survivor = history_row(
        expression=BOOK_L1,
        exposure="3" * 64,
        objectives={"cost_net_bps": -1.0, "oos_sharpe": -0.5},
    )
    stronger_non_survivor = history_row(
        expression=BOOK_L10,
        exposure="4" * 64,
        survivor=False,
        observed_at="2026-08-18T00:01:00Z",
        objectives={
            "cost_net_bps": 4.0,
            "oos_sharpe": 1.5,
            "coverage_ratio": 0.9,
            "robustness_score": 0.9,
            "novelty_score": 0.9,
        },
    )

    result = build_formula_search_memory([survivor, stronger_non_survivor])

    assert result["audit"]["archive_entries"] == 1
    assert result["audit"]["survivor_elites"] == 0
    assert result["audit"]["non_survivor_elites"] == 1
    assert result["elite_quality_scores"] == {}
    assert archive_entries(result)[0]["evaluation"]["candidate_payload"][
        "explicit_survivor"] is False


@pytest.mark.parametrize(
    ("failure_code", "audit_name"),
    [
        ("NO_COST_FEASIBLE_ENTRY", "calibration_cost_failure_skips"),
        (
            "NON_POSITIVE_DIRECTIONAL_RELATION",
            "calibration_direction_failure_skips",
        ),
    ],
)
def test_calibration_failure_is_audited_but_never_synthesized_as_objectives(
    failure_code: str,
    audit_name: str,
) -> None:
    failed = history_row()
    failed.pop("search_objectives")
    failed["calibration_status"] = failure_code
    failed["sessions"] = 1
    failed["opportunities"] = 0

    result = build_formula_search_memory([failed])

    assert result["audit"]["calibration_failure_skips"] == 1
    assert result["audit"][audit_name] == 1
    assert result["audit"]["invalid_rows"] == 0
    assert result["audit"]["archive_entries"] == 0
    assert result["audit"]["effective_exposure_trials"] == 0
    assert result["elite_quality_scores"] == {}


def test_exact_duplicate_exposure_and_result_is_idempotent() -> None:
    row = history_row(evidence_scope="F2", exposure="d" * 64)

    result = build_formula_search_memory([deepcopy(row), deepcopy(row)])

    assert result["audit"]["valid_rows"] == 2
    assert result["audit"]["unique_results_accepted"] == 1
    assert result["audit"]["duplicate_exposures"] == 1
    assert result["audit"]["duplicate_results"] == 1
    assert result["audit"]["effective_exposure_trials"] == 1
    assert len(result["state_snapshot"]["exposure_ledger"]["records"]) == 1
    assert len(result["state_snapshot"]["archive"]["seen_results"]) == 1
    assert len(result["elite_quality_scores"]) == 1


def test_same_exposure_with_changed_result_is_audited_as_conflict() -> None:
    original = history_row(exposure="e" * 64)
    changed = history_row(
        exposure="e" * 64,
        objectives={"cost_net_bps": 9.0},
    )

    result = build_formula_search_memory([original, changed])

    assert result["audit"]["conflicting_exposure_results"] == 1
    assert result["audit"]["rejection_counts"] == {
        "CONFLICTING_EXPOSURE_RESULT": 1,
    }
    assert result["audit"]["effective_exposure_trials"] == 1
    assert result["audit"]["unique_results_accepted"] == 1


def test_only_f1_and_f2_history_scopes_are_accepted() -> None:
    result = build_formula_search_memory([
        history_row(evidence_scope="F3"),
    ])

    assert result["audit"]["rejection_counts"] == {
        "UNSUPPORTED_EVIDENCE_SCOPE": 1,
    }
    assert result["audit"]["scheduler_used"] is False
    assert result["audit"]["promotion_authority_used"] is False


def test_same_ast_with_distinct_durable_identities_does_not_collide() -> None:
    first = history_row(
        exposure="f" * 64,
        candidate_identity=stable_fingerprint({"candidate": "identity-a"}),
        root_lineage_id="root-lineage-a",
        source_lead_ids=["lead-a"],
        economic_family_id="queue pressure family a",
    )
    second = history_row(
        exposure="f" * 64,
        candidate_identity=stable_fingerprint({"candidate": "identity-b"}),
        root_lineage_id="root-lineage-b",
        source_lead_ids=["lead-b"],
        economic_family_id="queue pressure family b",
        observed_at="2026-08-18T00:01:00Z",
    )
    assert first["candidate_ast_fingerprint"] == second[
        "candidate_ast_fingerprint"]
    assert first["candidate_identity_fingerprint"] != second[
        "candidate_identity_fingerprint"]

    result = build_formula_search_memory([first, second])

    identities = {
        first["candidate_identity_fingerprint"],
        second["candidate_identity_fingerprint"],
    }
    assert set(result["elite_candidates"]) == identities
    assert set(result["elite_quality_scores"]) == identities
    assert result["audit"]["archive_entries"] == 2
    assert result["audit"]["effective_exposure_trials"] == 2
    assert {
        item["root_lineage_id"]
        for item in result["elite_candidates"].values()
    } == {"root-lineage-a", "root-lineage-b"}


def test_same_candidate_independent_roots_do_not_conflict() -> None:
    identity = stable_fingerprint({"candidate": "root-independent"})
    first = history_row(
        candidate_identity=identity,
        exposure="9" * 64,
        root_lineage_id="independent-root-a",
        source_lead_ids=["lead-a"],
        objectives={"cost_net_bps": 0.5, "oos_sharpe": 0.2},
    )
    second = history_row(
        candidate_identity=identity,
        exposure="9" * 64,
        root_lineage_id="independent-root-b",
        source_lead_ids=["lead-b"],
        observed_at="2026-08-18T00:01:00Z",
        objectives={"cost_net_bps": 2.0, "oos_sharpe": 1.0},
    )

    result = build_formula_search_memory([first, second])

    assert result["audit"]["conflicting_exposure_results"] == 0
    assert result["audit"]["effective_exposure_trials"] == 2
    assert result["audit"]["unique_results_accepted"] == 2
    assert result["elite_candidates"][identity]["root_lineage_id"] == \
        "independent-root-b"


def test_legacy_ast_only_history_has_no_parent_authority() -> None:
    legacy = history_row()
    for key in (
        "candidate_identity_fingerprint", "candidate_ast_fingerprint",
        "semantic_plan_fingerprint", "root_lineage_id", "source_lead_ids",
    ):
        legacy.pop(key)
    legacy["candidate_fingerprint"] = grammar.fingerprint(
        legacy["expression"])

    result = build_formula_search_memory([legacy])

    assert result["audit"]["rejection_counts"] == {
        "MISSING_DURABLE_CANDIDATE_IDENTITY": 1,
    }
    assert result["elite_candidates"] == {}
    assert result["elite_quality_scores"] == {}
    assert result["audit"]["promotion_authority_used"] is False


def test_declared_full_ast_fingerprint_must_match_expression() -> None:
    row = history_row()
    row["candidate_ast_fingerprint"] = "f" * 64

    result = build_formula_search_memory([row])

    assert result["audit"]["rejection_counts"] == {
        "CANDIDATE_AST_FINGERPRINT_MISMATCH": 1,
    }
    assert result["elite_candidates"] == {}
