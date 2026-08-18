from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import sys

import pytest


FACTORY = (Path(__file__).resolve().parents[1] / "departments" /
           "01-research" / "factory")
if str(FACTORY) not in sys.path:
    sys.path.insert(0, str(FACTORY))

from formula_search_archive import (  # noqa: E402
    EVIDENCE,
    F0,
    F1,
    F2,
    F3,
    HOLD_NO_EVIDENCE,
    INFRA_FAILURE,
    NO_EVIDENCE,
    PROMOTE,
    REJECT,
    RETRY_INFRA,
    SURVIVOR,
    VALID,
    ExposureLedger,
    FidelityScheduler,
    FormulaEvaluation,
    FormulaSearchArchive,
    FormulaSearchState,
    Niche,
    ObjectiveVector,
    SearchKPIAccumulator,
    horizon_bucket,
)


NICHE = Niche.create(
    "queue pressure", 30, ["WALL_TIME", "QUOTE_EVENT", "WALL_TIME"])


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")).hexdigest()


def candidate_identity(name: str) -> str:
    return fingerprint({"durable_candidate": name})


def objectives(
    *,
    net: float = 1.0,
    oos: float = 1.0,
    coverage: float = 0.5,
    robustness: float = 0.7,
    novelty: float = 0.6,
    complexity: int = 12,
) -> ObjectiveVector:
    return ObjectiveVector(
        cost_net_bps=net,
        oos_sharpe=oos,
        coverage_ratio=coverage,
        robustness_score=robustness,
        novelty_score=novelty,
        complexity_nodes=complexity,
    )


def measured(
    candidate: str,
    *,
    fidelity: str = F1,
    niche: Niche = NICHE,
    exposure: str | None = None,
    values: ObjectiveVector | None = None,
    reason: str = "",
    ast_name: str | None = None,
    root_lineage_id: str | None = None,
    source_lead_ids: list[str] | None = None,
) -> FormulaEvaluation:
    identity = candidate_identity(candidate)
    expression = {"op": "field", "field": ast_name or candidate}
    return FormulaEvaluation(
        candidate_identity_fingerprint=identity,
        niche=niche,
        fidelity=fidelity,
        outcome=EVIDENCE,
        objectives=values or objectives(),
        candidate_payload={
            "candidate_identity_fingerprint": identity,
            "expression": expression,
            "candidate_ast_fingerprint": fingerprint(expression),
            "semantic_plan_fingerprint": fingerprint({
                "economic_rationale": candidate}),
            "root_lineage_id": root_lineage_id or f"root-{candidate}",
            "source_lead_ids": source_lead_ids or [f"lead-{candidate}"],
        },
        exposure_fingerprint=exposure or f"data-{candidate}-{fidelity}",
        sessions=6 if fidelity == F1 else 20,
        opportunities=100,
        reason_code=reason,
    )


def no_evidence(candidate: str, *, exposure: str = "empty-data") -> FormulaEvaluation:
    return FormulaEvaluation(
        candidate_identity_fingerprint=candidate_identity(candidate),
        niche=NICHE,
        fidelity=F1,
        outcome=NO_EVIDENCE,
        candidate_payload={"root_lineage_id": f"root-{candidate}"},
        exposure_fingerprint=exposure,
        sessions=6,
        opportunities=0,
        reason_code="NO_EXECUTABLE_OBSERVATIONS",
    )


def test_niche_is_mechanism_horizon_clock_and_is_canonical() -> None:
    assert NICHE.key == "QUEUE_PRESSURE|6_30S|QUOTE_EVENT+WALL_TIME"
    assert Niche.from_payload(NICHE.to_payload()) == NICHE
    assert [horizon_bucket(value) for value in (1, 5, 6, 30, 31, 300, 301, 3600, 3601)] == [
        "1_5S", "1_5S", "6_30S", "6_30S", "31_300S", "31_300S",
        "301_3600S", "301_3600S", "GT_3600S",
    ]
    with pytest.raises(ValueError, match="positive integer"):
        horizon_bucket(0)
    with pytest.raises(ValueError, match="clock_domain"):
        Niche.create("queue", 30, [])


def test_multiobjective_score_is_finite_monotone_and_complexity_aware() -> None:
    weak = objectives(
        net=-1, oos=-0.2, coverage=0.2, robustness=0.3,
        novelty=0.2, complexity=30)
    strong = objectives(
        net=2, oos=1.0, coverage=0.6, robustness=0.8,
        novelty=0.7, complexity=10)
    assert strong.dominates(weak)
    assert strong.quality_score() > weak.quality_score()
    assert set(strong.components()) == {
        "cost_net", "oos", "coverage", "robustness", "novelty", "simplicity"}
    assert 0 < strong.quality_score() < 1
    with pytest.raises(ValueError, match="finite"):
        objectives(net=math.nan)
    with pytest.raises(ValueError, match=r"\[0, 1\]"):
        objectives(coverage=1.01)


def test_map_elites_keeps_one_quality_winner_per_niche() -> None:
    archive = FormulaSearchArchive()
    weak = measured("weak", values=objectives(
        net=-1, oos=-0.1, coverage=0.2, robustness=0.3,
        novelty=0.2, complexity=30))
    strong = measured("strong", values=objectives())
    other_niche = Niche.create("tape pressure", 30, "TRADE_VOLUME")

    assert archive.observe(weak, cycle=1).action == "INSERTED"
    update = archive.observe(strong, cycle=2)
    assert update.action == "REPLACED"
    assert update.incumbent_identity_fingerprint == candidate_identity("weak")
    assert archive.get(NICHE).evaluation.candidate_identity_fingerprint == (
        candidate_identity("strong"))
    assert archive.observe(
        measured("other", niche=other_niche), cycle=2).action == "INSERTED"
    assert len(archive.entries) == 2

    # Exact result delivery is idempotent, including outcome counters.
    assert archive.observe(strong, cycle=99).action == "DUPLICATE_RESULT"
    assert archive.outcome_counts[EVIDENCE] == 3


def test_screening_elite_cannot_overwrite_independent_oos_evidence() -> None:
    archive = FormulaSearchArchive()
    excellent_screen = measured("screen", values=objectives(
        net=8, oos=3, coverage=0.9, robustness=0.9,
        novelty=0.9, complexity=5))
    losing_oos = measured(
        "oos", fidelity=F2, reason="NO_COST_FEASIBLE_ENTRY",
        values=objectives(
            net=-0.5, oos=0.1, coverage=0.3, robustness=0.5,
            novelty=0.2, complexity=20))
    archive.observe(excellent_screen, cycle=1)
    assert archive.observe(losing_oos, cycle=2).action == "REPLACED"
    assert archive.get(NICHE).evaluation.independent_evidence is True
    assert archive.observe(
        measured("screen-2", values=objectives(
            net=20, oos=5, coverage=1, robustness=1,
            novelty=1, complexity=1)), cycle=3).action == "RETAINED"
    assert archive.get(NICHE).evaluation.candidate_identity_fingerprint == (
        candidate_identity("oos"))


def test_measured_failure_no_evidence_and_infra_failure_are_distinct() -> None:
    archive = FormulaSearchArchive()
    ledger = ExposureLedger()
    scheduler = FidelityScheduler()

    measured_loss = measured(
        "cost-loss", reason="NO_COST_FEASIBLE_ENTRY",
        values=objectives(net=-0.2))
    no_rows = no_evidence("empty")
    infra = FormulaEvaluation(
        candidate_identity_fingerprint=candidate_identity("loader-broke"),
        niche=NICHE,
        fidelity=F1,
        outcome=INFRA_FAILURE,
        reason_code="RAW_PARQUET_SCHEMA_MISMATCH",
    )

    assert archive.observe(measured_loss, cycle=1).action == "INSERTED"
    assert ledger.record(measured_loss, cycle=1) is True
    measured_decision = scheduler.decide(measured_loss, ledger)
    assert measured_decision.action == REJECT
    assert "NO_COST_FEASIBLE_ENTRY" in measured_decision.failures

    assert archive.observe(no_rows, cycle=2).action == "NO_EVIDENCE"
    assert ledger.record(no_rows, cycle=2) is True
    assert scheduler.decide(no_rows, ledger).action == HOLD_NO_EVIDENCE

    assert archive.observe(infra, cycle=3).action == "INFRA_FAILURE"
    assert ledger.record(infra, cycle=3) is False
    infra_decision = scheduler.decide(infra, ledger)
    assert infra_decision.action == RETRY_INFRA
    assert infra_decision.retryable is True
    assert ledger.effective_trial_count == 2
    assert ledger.measured_trial_count == 1
    assert ledger.no_evidence_trial_count == 1
    assert archive.outcome_counts == {
        EVIDENCE: 1, INFRA_FAILURE: 1, NO_EVIDENCE: 1, VALID: 0}

    with pytest.raises(ValueError, match="measured EVIDENCE"):
        FormulaEvaluation(
            candidate_identity("wrong-class"), NICHE, F1, NO_EVIDENCE,
            candidate_payload={"root_lineage_id": "root-wrong-class"},
            exposure_fingerprint="x", reason_code="NO_COST_FEASIBLE_ENTRY")
    with pytest.raises(ValueError, match="must use NO_EVIDENCE"):
        measured("wrong-no-rows", reason="NO_EXECUTABLE_OBSERVATIONS")


@pytest.mark.parametrize(
    "reason_code",
    ["NO_COST_FEASIBLE_ENTRY", "NON_POSITIVE_DIRECTIONAL_RELATION"],
)
def test_measured_economic_failures_are_retained_and_cannot_promote(
    reason_code: str,
) -> None:
    # Even deliberately optimistic metrics cannot turn a typed measured-failure
    # verdict into a promotion.  It remains archive memory and an effective trial.
    evaluation = measured(
        reason_code.lower(), reason=reason_code, values=objectives(net=5, oos=2))
    ledger = ExposureLedger()
    ledger.record(evaluation, cycle=1)
    archive = FormulaSearchArchive()
    assert archive.observe(evaluation, cycle=1).action == "INSERTED"
    decision = FidelityScheduler().decide(evaluation, ledger)
    assert decision.action == REJECT
    assert reason_code in decision.failures
    assert ledger.effective_trial_count == 1


def test_screening_ledger_enforces_exact_exposure_and_cooldown() -> None:
    ledger = ExposureLedger()
    first = measured("formula-a", exposure="screen-set-a")
    assert ledger.record(first, cycle=2) is True
    assert ledger.record(first, cycle=8) is False
    assert ledger.can_screen(
        candidate_identity_fingerprint=candidate_identity("formula-a"),
        root_lineage_id="root-formula-a",
        exposure_fingerprint="screen-set-a",
        current_cycle=8, cooldown_cycles=3) == (
            False, "DUPLICATE_SCREENING_EXPOSURE")
    assert ledger.can_screen(
        candidate_identity_fingerprint=candidate_identity("formula-a"),
        root_lineage_id="root-formula-a",
        exposure_fingerprint="screen-set-b",
        current_cycle=4, cooldown_cycles=3) == (False, "SCREENING_COOLDOWN")
    assert ledger.can_screen(
        candidate_identity_fingerprint=candidate_identity("formula-a"),
        root_lineage_id="root-formula-a",
        exposure_fingerprint="screen-set-b",
        current_cycle=5, cooldown_cycles=3) == (True, "ELIGIBLE")

    changed = measured(
        "formula-a", exposure="screen-set-a", values=objectives(net=9))
    with pytest.raises(ValueError, match="changed its durable result"):
        ledger.record(changed, cycle=5)

    payload = json.loads(json.dumps(ledger.to_payload(), sort_keys=True))
    restored = ExposureLedger.from_payload(payload)
    assert restored.to_payload() == ledger.to_payload()
    assert restored.effective_trial_count == 1


def test_trial_identity_uses_durable_candidate_not_shared_ast() -> None:
    ledger = ExposureLedger()
    first = measured(
        "identity-a", ast_name="same-ast", exposure="same-exposure")
    second = measured(
        "identity-b", ast_name="same-ast", exposure="same-exposure")
    assert first.candidate_payload["candidate_ast_fingerprint"] == (
        second.candidate_payload["candidate_ast_fingerprint"])
    assert first.candidate_identity_fingerprint != (
        second.candidate_identity_fingerprint)

    assert ledger.record(first, cycle=1) is True
    assert ledger.record(second, cycle=1) is True
    assert first.trial_identity != second.trial_identity
    assert ledger.effective_trial_count == 2


def test_same_candidate_and_exposure_are_separate_on_independent_roots() -> None:
    ledger = ExposureLedger()
    first = measured(
        "shared-candidate", exposure="shared-exposure",
        root_lineage_id="independent-root-a", source_lead_ids=["lead-a"])
    second = measured(
        "shared-candidate", exposure="shared-exposure",
        root_lineage_id="independent-root-b", source_lead_ids=["lead-b"])

    assert first.candidate_identity_fingerprint == \
        second.candidate_identity_fingerprint
    assert first.trial_identity != second.trial_identity
    assert first.result_identity != second.result_identity
    assert ledger.record(first, cycle=1) is True
    assert ledger.record(second, cycle=1) is True
    assert ledger.effective_trial_count == 2

    restored = ExposureLedger.from_payload(json.loads(json.dumps(
        ledger.to_payload(), sort_keys=True)))
    assert restored.to_payload() == ledger.to_payload()


def test_selection_visible_result_without_root_fails_closed() -> None:
    with pytest.raises(ValueError, match="require root_lineage_id"):
        FormulaEvaluation(
            candidate_identity_fingerprint=candidate_identity("missing-root"),
            niche=NICHE, fidelity=F1, outcome=NO_EVIDENCE,
            exposure_fingerprint="immutable-exposure", sessions=6,
            opportunities=0, reason_code="NO_EXECUTABLE_OBSERVATIONS")


def test_source_alias_expansion_does_not_change_scientific_result_key() -> None:
    first = measured(
        "alias-candidate", exposure="shared-exposure",
        root_lineage_id="same-root", source_lead_ids=["lead-a"])
    expanded = measured(
        "alias-candidate", exposure="shared-exposure",
        root_lineage_id="same-root", source_lead_ids=["lead-a", "lead-b"])

    assert first.trial_identity == expanded.trial_identity
    assert first.result_identity == expanded.result_identity
    ledger = ExposureLedger()
    assert ledger.record(first, cycle=1) is True
    assert ledger.record(expanded, cycle=2) is False


def test_legacy_short_ast_key_is_rejected_as_candidate_identity() -> None:
    with pytest.raises(ValueError, match="lowercase SHA-256"):
        FormulaEvaluation(
            "legacy-ast16", NICHE, F0, VALID)

    legacy_payload = {
        "candidate_fingerprint": "a" * 16,
        "niche": NICHE.to_payload(),
        "fidelity": F0,
        "outcome": VALID,
    }
    with pytest.raises(ValueError, match="candidate_identity_fingerprint"):
        FormulaEvaluation.from_payload(legacy_payload)


def test_scheduler_runs_f0_f1_f2_f3_and_adjusts_for_effective_trials() -> None:
    scheduler = FidelityScheduler()
    ledger = ExposureLedger()
    static = FormulaEvaluation(candidate_identity("new-ast"), NICHE, F0, VALID)
    assert scheduler.decide(static, ledger).next_fidelity == F1

    screen = measured("new-ast", fidelity=F1)
    assert scheduler.decide(screen, ledger).action == PROMOTE
    assert scheduler.decide(screen, ledger).next_fidelity == F2

    # A marginal F2 result passes with one adaptive look.
    marginal = measured(
        "marginal", fidelity=F2,
        values=objectives(oos=0.30, coverage=0.3, robustness=0.5))
    clean_decision = scheduler.decide(marginal, ExposureLedger())
    assert clean_decision.action == PROMOTE
    assert clean_decision.next_fidelity == F3

    # The same nominal statistic is not enough after a broad adaptive search.
    crowded = ExposureLedger()
    for index in range(100):
        crowded.record(measured(
            f"prior-{index}", exposure=f"prior-data-{index}"), cycle=index)
    adjusted = scheduler.decide(marginal, crowded)
    assert adjusted.action == REJECT
    assert "TRIAL_ADJUSTED_OOS_FLOOR" in adjusted.failures
    assert adjusted.effective_trial_count == 101
    assert (adjusted.applied_thresholds["trial_adjusted_min_oos_sharpe"]
            > clean_decision.applied_thresholds["trial_adjusted_min_oos_sharpe"])

    forward = measured(
        "winner", fidelity=F3,
        values=objectives(oos=1.2, coverage=0.5, robustness=0.8,
                          novelty=0.1, complexity=20))
    final = scheduler.decide(forward, ExposureLedger())
    assert final.action == SURVIVOR
    assert final.production_promotion_authority is False

    restored = FidelityScheduler.from_payload(json.loads(json.dumps(
        scheduler.to_payload())))
    assert restored.to_payload() == scheduler.to_payload()


def test_kpis_are_unique_idempotent_and_compute_normalized() -> None:
    kpis = SearchKPIAccumulator()
    second_niche = Niche.create("tape pressure", 60, "TRADE_VOLUME")
    assert kpis.record_generation(
        generation_id="g-1", candidate_fingerprint="ast-1", valid=True,
        compute_seconds=30, niche=NICHE)
    assert kpis.record_generation(
        generation_id="g-2", candidate_fingerprint="invalid", valid=False,
        compute_seconds=30)
    assert kpis.record_generation(
        generation_id="g-3", candidate_fingerprint="ast-2", valid=True,
        compute_seconds=60, niche=second_niche)
    assert not kpis.record_generation(
        generation_id="g-1", candidate_fingerprint="ast-1", valid=True,
        compute_seconds=999, niche=NICHE)

    evaluation = measured("ast-1")
    decision = FidelityScheduler().decide(evaluation, ExposureLedger())
    assert kpis.record_evaluation(evaluation, decision, compute_seconds=120)
    assert not kpis.record_evaluation(evaluation, decision, compute_seconds=999)

    snapshot = kpis.snapshot()
    assert snapshot["valid_ast_per_minute"] == pytest.approx(1.0)
    assert snapshot["unique_niche_per_hour"] == pytest.approx(30.0)
    assert snapshot["survivor_per_compute_hour"] == pytest.approx(15.0)
    assert snapshot["survivor_unique"] == 1
    restored = SearchKPIAccumulator.from_payload(json.loads(json.dumps(
        kpis.to_payload())))
    assert restored.snapshot() == snapshot


def test_composed_state_is_idempotent_and_json_round_trippable() -> None:
    state = FormulaSearchState()
    scheduler = FidelityScheduler()
    evaluation = measured("state-candidate")
    first = state.process_result(
        evaluation, cycle=1, compute_seconds=15, scheduler=scheduler)
    retry = state.process_result(
        evaluation, cycle=9, compute_seconds=999, scheduler=scheduler)
    assert first.new_result is True
    assert first.decision.action == PROMOTE
    assert retry.new_result is False
    assert state.exposure_ledger.effective_trial_count == 1
    assert state.kpis.snapshot()["evaluation_compute_seconds"] == 15

    encoded = json.dumps(state.to_payload(), allow_nan=False, sort_keys=True)
    restored = FormulaSearchState.from_payload(json.loads(encoded))
    assert restored.to_payload() == state.to_payload()
    assert restored.archive.get(
        NICHE).evaluation.candidate_identity_fingerprint == candidate_identity(
            "state-candidate")
