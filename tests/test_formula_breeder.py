from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (FACTORY, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import formula_breeder as breeder  # noqa: E402
import intraday_ast_contract as grammar  # noqa: E402
from intraday_candidate_identity import (  # noqa: E402
    candidate_identity_fingerprint,
)


FAILED_EXPR = {"op": "sub", "args": [
    {"op": "field", "field": "queue_imbalance_l1"},
    {"op": "field", "field": "queue_imbalance_l10"},
]}
FRESH_EXPR = {"op": "rolling_mean", "seconds": 30, "arg": {
    "op": "field", "field": "trade_flow_imbalance"}}
FAILED_IDENTITY = hashlib.sha256(b"failed-candidate-v1").hexdigest()
FAILED_ROOT = "40000000-0000-0000-0000-000000000001"


def lead(lead_id: str, expression: dict, mechanism: str) -> dict:
    return {
        "lead_id": lead_id,
        "title": mechanism,
        "expression": expression,
        "fingerprint": grammar.fingerprint(expression),
        "used": False,
        "economic_mechanism": mechanism,
        "contract": {
            "candidate_signal_expr": expression,
            "semantic_plan": {
                "event": "ORDER_FLOW", "context": ["ALL"],
                "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
                "output": "TAKER_NET_PNL", "execution": "TAKER",
                "horizon_seconds": 30,
            },
            "formula_thesis": {
                "coefficient_policy": "STRUCTURE_ONLY",
                "expected_sign": "POSITIVE",
            },
        },
    }


def cost_failure() -> dict:
    source = lead("lead_failed", FAILED_EXPR, "depth divergence")
    stable = lambda value: hashlib.sha256(json.dumps(  # noqa: E731
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    return {
        "expression": FAILED_EXPR,
        "decision": "GATE_HOLD",
        "observation_id": "experiment-cost-failure:PRIMARY",
        "lesson_codes": ["NO_COST_FEASIBLE_ENTRY"],
        "diagnostics": {
            "calibration_observations": 262_042,
            "min_cost_hurdle_bps": 23.0,
            "max_calibrated_markout_bps": 3.9757,
        },
        "evidence_scope": "PRIMARY_DISCOVERY",
        "title": "cost-infeasible depth divergence",
        "candidate_identity_fingerprint": FAILED_IDENTITY,
        "candidate_ast_fingerprint": stable(FAILED_EXPR),
        "semantic_plan_fingerprint": stable(
            source["contract"]["semantic_plan"]),
        "economic_family_id": "failed-family",
        "source_lead_ids": ["lead_failed"],
        "root_lineage_id": FAILED_ROOT,
    }


def test_primitive_window_does_not_rewrite_prediction_horizon() -> None:
    parent_contract = {
        "semantic_plan": {
            "event": "ORDER_FLOW", "context": ["ALL"],
            "qualities": ["LEVEL"], "direction": "FOLLOW",
            "output": "TAKER_NET_PNL", "execution": "TAKER",
            "horizon_seconds": 5,
        },
        "formula_thesis": {
            "coefficient_policy": "STRUCTURE_ONLY",
            "expected_sign": "POSITIVE",
        },
    }
    primitive_only = {
        "op": "field", "field": "trade_flow_imbalance", "seconds": 600,
    }
    temporal = {
        "op": "rolling_mean", "seconds": 300,
        "arg": {"op": "field", "field": "trade_flow_imbalance",
                "seconds": 5},
    }

    primitive_plan, _ = breeder._semantic_hint(
        primitive_only, parent_contract, "PRIMITIVE_WINDOW_SWAP")
    temporal_plan, _ = breeder._semantic_hint(
        temporal, parent_contract, "ROLLING_MEAN")

    assert primitive_plan["horizon_seconds"] == 5
    assert temporal_plan["horizon_seconds"] == 300


def test_cost_infeasible_family_is_not_reseeded_or_inverted():
    result = breeder.generate_from_records(
        leads=[lead("lead_failed", FAILED_EXPR, "depth divergence")],
        outcome_rows=[cost_failure()], population_size=16, generation=2)

    assert result["ok"] is False
    assert result["emitted_population"] == 0
    assert result["kpi"]["rejection_counts"]["FAILED_FORMULA_RESEED"] == 1
    assert result["audit"]["cost_infeasible_families_abandoned"] == 1


def test_latest_cost_capacity_failure_retires_stale_survivor_parent():
    old_survivor = {
        "expression": FAILED_EXPR,
        "decision": "SURVIVED",
        "observation_id": "old-screen-survivor",
        "lesson_codes": [],
        "diagnostics": {},
        "evidence_scope": "ADAPTIVE_SCREENING",
        "title": "old gross screen",
        "candidate_identity_fingerprint": FAILED_IDENTITY,
        "candidate_ast_fingerprint": cost_failure()[
            "candidate_ast_fingerprint"],
        "semantic_plan_fingerprint": cost_failure()[
            "semantic_plan_fingerprint"],
        "economic_family_id": "failed-family",
        "source_lead_ids": ["lead_failed"],
        "root_lineage_id": FAILED_ROOT,
    }
    result = breeder.generate_from_records(
        leads=[lead("lead_failed", FAILED_EXPR, "depth divergence")],
        outcome_rows=[old_survivor, cost_failure()],
        population_size=16, generation=4)

    assert result["ok"] is False
    assert result["emitted_population"] == 0
    assert result["audit"]["exploit_parent_count"] == 0


def test_breeder_emits_submission_drafts_from_other_economic_seed():
    result = breeder.generate_from_records(
        leads=[
            lead("lead_failed", FAILED_EXPR, "depth divergence"),
            lead("lead_fresh", FRESH_EXPR, "signed tape persistence"),
        ],
        outcome_rows=[cost_failure()], population_size=32, generation=3)

    assert result["ok"] is True
    assert result["submission_ready_count"] > 0
    assert result["submission_ready_niches"] >= 2
    assert result["audit"]["forward_lockbox_used_for_generation"] is False
    assert result["audit"]["cost_hurdle_modified_by_breeder"] is False
    ready = [row for row in result["candidates"] if row["submission_ready"]]
    assert all(row["parent_lead_ids"] == ["lead_fresh"] for row in ready)
    assert all(row["adaptive_selection"] is True for row in ready)
    assert all(row["promotion_authority"] is False for row in ready)
    assert all(
        row["formula_thesis_skeleton"]["identification"].startswith(
            "REQUIRES_HERMES") for row in ready)


def test_duplicate_source_formula_uses_one_canonical_unused_parent():
    old = lead("lead-old", FRESH_EXPR, "signed tape persistence")
    old["used"] = True
    fresh = lead("lead-unused", FRESH_EXPR, "signed tape persistence")

    result = breeder.generate_from_records(
        leads=[old, fresh], outcome_rows=[],
        population_size=16, generation=3)

    ready = [row for row in result["candidates"] if row["submission_ready"]]
    assert result["unique_source_formulas"] == 1
    assert ready
    assert all(row["parent_lead_ids"] == ["lead-unused"] for row in ready)
    assert all(row["source_lead_ids"] == ["lead-old", "lead-unused"]
               for row in ready)
    assert all(row["parent_contract_count"] == 1 for row in ready)
    assert all(row["submission_blocker"] == "" for row in ready)


def test_failure_of_source_alias_retires_whole_exact_contract_group():
    alias_a = lead("lead-a", FAILED_EXPR, "depth divergence")
    alias_b = lead("lead-b", FAILED_EXPR, "depth divergence")
    failed = cost_failure()
    failed["source_lead_ids"] = ["lead-b"]

    result = breeder.generate_from_records(
        leads=[alias_a, alias_b], outcome_rows=[failed],
        population_size=16, generation=5)

    assert result["unique_source_formulas"] == 1
    assert result["ok"] is False
    assert result["emitted_population"] == 0
    assert result["kpi"]["rejection_counts"][
        "FAILED_SOURCE_CONTRACT_RESEED"] == 1


def test_baseline_ast_divides_exact_contract_failure_memory():
    baseline_a = {"op": "field", "field": "queue_imbalance_l1"}
    baseline_b = {"op": "field", "field": "queue_imbalance_l10"}
    parent_a = lead("lead-a", FRESH_EXPR, "signed tape persistence")
    parent_b = lead("lead-b", FRESH_EXPR, "signed tape persistence")
    parent_a["contract"]["source_baseline_expr"] = baseline_a
    parent_b["contract"]["source_baseline_expr"] = baseline_b
    stable = lambda value: hashlib.sha256(json.dumps(  # noqa: E731
        value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    failed_b = {
        "expression": FRESH_EXPR,
        "decision": "FAILED",
        "observation_id": "failed-baseline-b",
        "candidate_identity_fingerprint": hashlib.sha256(
            b"failed-baseline-b").hexdigest(),
        "candidate_ast_fingerprint": stable(FRESH_EXPR),
        "semantic_plan_fingerprint": stable(
            parent_b["contract"]["semantic_plan"]),
        "baseline_ast_fingerprint": stable(baseline_b),
        "source_lead_ids": ["lead-b"],
        "root_lineage_id": "root-baseline-b",
        "evidence_scope": "ADAPTIVE_SCREENING",
    }

    result = breeder.generate_from_records(
        leads=[parent_a, parent_b], outcome_rows=[failed_b],
        population_size=16, generation=6)

    assert result["unique_source_formulas"] == 2
    assert result["ok"] is True
    ready = [row for row in result["candidates"] if row["submission_ready"]]
    assert ready
    assert all(row["parent_lead_ids"] == ["lead-a"] for row in ready)
    assert all(row["source_lead_ids"] == ["lead-a"] for row in ready)


def test_distinct_contract_crossover_requires_review_not_aliases():
    parent_a = lead("lead-a", FAILED_EXPR, "book pressure")
    parent_b = lead("lead-b", FRESH_EXPR, "tape pressure")
    contract_a = breeder._source_contract_fingerprint(parent_a)
    contract_b = breeder._source_contract_fingerprint(parent_b)
    by_id = {"lead-a": parent_a, "lead-b": parent_b}
    canonical = {contract_a: parent_a, contract_b: parent_b}
    candidate = SimpleNamespace(
        parent_seed_ids=("lead-a", "lead-b"),
        parent_source_contract_fingerprints=(contract_a, contract_b),
    )

    provenance = breeder._parent_provenance(
        candidate, lead_by_id=by_id,
        canonical_lead_by_contract=canonical)

    assert provenance["provenance_mismatch"] is False
    assert provenance["parent_contract_count"] == 2
    assert breeder._submission_blocker(
        "CROSSOVER_TYPED", provenance) == \
        "MULTI_PARENT_PROVENANCE_REQUIRES_REVIEW"


def test_semantic_contract_changes_candidate_and_batch_identity():
    follow = lead("lead-follow", FRESH_EXPR, "follow tape")
    revert = lead("lead-revert", FRESH_EXPR, "revert tape")
    revert["contract"]["semantic_plan"] = {
        **revert["contract"]["semantic_plan"],
        "direction": "REVERT",
        "horizon_seconds": 600,
    }

    follow_batch = breeder.generate_from_records(
        leads=[follow], outcome_rows=[], population_size=16, generation=9)
    revert_batch = breeder.generate_from_records(
        leads=[revert], outcome_rows=[], population_size=16, generation=9)

    assert follow_batch["batch_fingerprint"] != \
        revert_batch["batch_fingerprint"]
    assert {row["candidate_id"] for row in follow_batch["candidates"]}.isdisjoint(
        {row["candidate_id"] for row in revert_batch["candidates"]})


def test_generation_is_invariant_to_source_record_order():
    book = lead("lead-book", FAILED_EXPR, "book pressure")
    tape = lead("lead-tape", FRESH_EXPR, "tape pressure")

    first = breeder.generate_from_records(
        leads=[book, tape], outcome_rows=[], population_size=32, generation=10)
    reversed_order = breeder.generate_from_records(
        leads=[tape, book], outcome_rows=[],
        population_size=32, generation=10)

    assert first["batch_fingerprint"] == reversed_order["batch_fingerprint"]
    assert [row["candidate_id"] for row in first["candidates"]] == [
        row["candidate_id"] for row in reversed_order["candidates"]]


def test_archive_score_does_not_leak_across_independent_roots(monkeypatch):
    source = lead("lead-shared", FRESH_EXPR, "signed tape persistence")
    identity = hashlib.sha256(b"root-independent-candidate").hexdigest()
    ast_fp = hashlib.sha256(json.dumps(
        FRESH_EXPR, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    semantic_fp = hashlib.sha256(json.dumps(
        source["contract"]["semantic_plan"], sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    elite = {
        "candidate_identity_fingerprint": identity,
        "quality_score": 0.91,
        "ast_fingerprint": ast_fp,
        "semantic_plan_fingerprint": semantic_fp,
        "root_lineage_id": "independent-root-a",
        "source_lead_ids": ["lead-shared"],
        "explicit_survivor": True,
        "expression": FRESH_EXPR,
        "economic_family_id": "tape-family",
        "evaluator_version": breeder.ACTIVE_EVALUATOR_VERSION,
        "cost_model_version": breeder.ACTIVE_COST_MODEL_VERSION,
        "evidence_scope": "F1",
        "measurement_scope": "ADAPTIVE_RUNG_MEASURED",
        "observed_at": "2026-08-18T00:00:00Z",
        "exposure_fingerprint": "8" * 64,
    }
    monkeypatch.setattr(breeder, "build_formula_search_memory", lambda *a, **k: {
        "elite_candidates": {identity: elite},
        "audit": {}, "rejected_rows": [], "state_snapshot": {},
    })
    actual_engine = breeder.FormulaEvolutionEngine
    captured = {}

    class CapturingEngine:
        def __init__(self, config):
            self.engine = actual_engine(config)

        def generate_population(self, **kwargs):
            captured["outcomes"] = list(kwargs["outcomes"])
            return self.engine.generate_population(**kwargs)

    monkeypatch.setattr(breeder, "FormulaEvolutionEngine", CapturingEngine)
    common = {
        "archive_history": True,
        "expression": FRESH_EXPR,
        "decision": "SCREENING_ONLY",
        "explicit_survivor": True,
        "candidate_identity_fingerprint": identity,
        "candidate_ast_fingerprint": ast_fp,
        "semantic_plan_fingerprint": semantic_fp,
        "economic_family_id": "tape-family",
        "source_lead_ids": ["lead-shared"],
        "exposure_fingerprint": "8" * 64,
        "evidence_scope": "F1",
    }
    breeder.generate_from_records(
        leads=[source], outcome_rows=[
            {**common, "observation_id": "root-a-result",
             "root_lineage_id": "independent-root-a"},
            {**common, "observation_id": "root-b-result",
             "root_lineage_id": "independent-root-b"},
        ], population_size=16, generation=11)

    decisions = {row.root_lineage_id: row.outcome
                 for row in captured["outcomes"]}
    assert decisions == {
        "independent-root-a": "SURVIVED",
        "independent-root-b": "SCREENING_ONLY",
    }


def test_population_bounds_fail_before_generation():
    try:
        breeder.generate_from_records(
            leads=[lead("lead_fresh", FRESH_EXPR, "signed tape")],
            outcome_rows=[], population_size=breeder.MAX_POPULATION + 1)
    except ValueError as exc:
        assert "population_size" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("unbounded breeder population passed")


def test_governed_stock_predicate_wraps_every_result_memory_query():
    for query in (breeder._PRIMARY_OUTCOMES_SQL, breeder._SCREEN_OUTCOMES_SQL):
        assert "join quant.dataset_manifests m" in query
        assert "intraday-governance-report-v7" in query
    assert "KRX_ACTIVE_STOCK_ONLY" in breeder._PRIMARY_OUTCOMES_SQL
    assert "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY" in \
        breeder._SCREEN_OUTCOMES_SQL
    assert "FULL_60" in breeder._PRIMARY_OUTCOMES_SQL
    assert "DISCOVERY_6" in breeder._SCREEN_OUTCOMES_SQL
    assert "VALIDATION_20" in breeder._SCREEN_OUTCOMES_SQL
    assert "ADAPTIVE_SEARCH" in breeder._SCREEN_OUTCOMES_SQL
    assert "rung_plan_fingerprint" in breeder._SCREEN_OUTCOMES_SQL
    assert "intraday_candidate_lineages" in breeder._SCREEN_OUTCOMES_SQL
    assert "candidate_ast_fingerprint" in breeder._SCREEN_OUTCOMES_SQL
    assert "economic_family_id" in breeder._SCREEN_OUTCOMES_SQL
    assert "manifest.created_at" in breeder._SCREEN_OUTCOMES_SQL
    assert "quote_row_count > 0" in breeder._SCREEN_OUTCOMES_SQL
    assert "trade_row_count > 0" in breeder._SCREEN_OUTCOMES_SQL
    assert "v_current_experiment_outcomes" not in breeder._PRIMARY_OUTCOMES_SQL
    assert "research.experiment_outcomes" in breeder._PRIMARY_OUTCOMES_SQL
    assert "quote_row_count > 0" in breeder._CALIBRATION_FAILURES_SQL
    assert "trade_row_count > 0" in breeder._CALIBRATION_FAILURES_SQL
    assert "EVENT_TIME_HISTORICAL_ONLY" in breeder._CALIBRATION_FAILURES_SQL


def test_adaptive_memory_uses_raw_manifest_plus_per_rung_stock_evidence():
    for query in (breeder._CALIBRATION_FAILURES_SQL,
                  breeder._SCREEN_OUTCOMES_SQL):
        assert "m.name = 'krx-intraday-events'" in query
        assert "m.version = 'v1'" in query
        assert "m.universe_version_id is null" in query
        assert "LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT" in query
        assert "timescaledb://market/{market_quotes,market_ticks}" in query
        assert "m.content_hash ~ '^[0-9a-f]{64}$'" in query
        assert "source_lineage" in query
        assert "content_fingerprint" in query
        assert "reference_identity_revalidated" in query
        assert "reference_identity_fingerprint" in query
        assert "reference_instrument_ids" in query
        assert "post_product_filter_instruments" in query
        assert "krx-stock-only-v3" in query
        assert "current_krx_stock_instrument_identity" in query
        assert "listed_from" in query
        assert "listed_to" in query
        assert "planned_instrument_ids" in query
        assert "session_content_fingerprint" in query
        assert "exposure_evidence_fingerprint" in query

    # The FULL_60 result-memory lane intentionally keeps the much stronger
    # performance-evidence predicate and is not relaxed by this raw-source
    # exception.
    assert "FULL_60" in breeder._PRIMARY_OUTCOMES_SQL
    assert "primary_fold_count" in breeder._PRIMARY_OUTCOMES_SQL


def test_calibration_exposure_is_reusable_only_with_exact_root_slice():
    query = breeder._CALIBRATION_FAILURES_SQL
    # A descendant/retry gets a new rung UUID, while the append-only exposure
    # is uniquely owned by (root_lineage_id, session_date).  Requiring the new
    # rung UUID made every valid reuse return zero rows.
    assert "exposure.experiment_rung_id =" not in query
    assert "exposure.root_lineage_id =" in query
    assert "exposure.dataset_id = calibration_rung.dataset_id" in query
    assert "exposure.session_date = any(" in query
    assert "calibration_rung.planned_session_dates" in query
    assert "exposure.exposure_purpose = 'CALIBRATION'" in query
    assert "exposure.knowledge_cutoff <=" in query
    assert "calibration_rung.dataset_cutoff" in query
    assert "manifest.report#>>'{trial_lockbox,rungs,calibration}'" in query


def test_screen_exposure_requires_exact_adaptive_purpose_and_report_rung():
    query = breeder._SCREEN_OUTCOMES_SQL
    assert "exposure.exposure_purpose = 'ADAPTIVE_SEARCH'" in query
    assert "('CALIBRATION','ADAPTIVE_SEARCH')" not in query
    assert "manifest.report#>>'{trial_lockbox,rungs,discovery}'" in query
    assert "manifest.report#>>'{trial_lockbox,rungs,validation}'" in query
    assert "search_rung.root_lineage_id = primary_lineage.root_lineage_id" \
        in query


def test_calibration_failure_is_failed_memory_when_decision_is_blank():
    primary = [("experiment", FAILED_EXPR, "", [], {}, {
        "status": "NO_COST_FEASIBLE_ENTRY",
        "observations": 262_042,
        "minimum_observed_entry_hurdle_bps": 23.0,
        "maximum_calibrated_predicted_markout_bps": 3.9757,
    }, "depth divergence", "2026-08-18")]
    rows = breeder._outcome_records(primary, [])

    assert rows[0]["decision"] == "FAILED"
    assert rows[0]["diagnostics"]["calibration_observations"] == 262_042


def _search_objectives(expression: dict, *, net: float = 1.0,
                       sharpe: float = 0.5) -> dict:
    return {
        "version": "intraday-search-objectives-v1",
        "complete": True,
        "values": {
            "cost_net_bps": net,
            "oos_sharpe": sharpe,
            "coverage_ratio": 0.75,
            "robustness_score": 0.6,
            "novelty_score": 0.8,
            "complexity_nodes": grammar.count_nodes(expression),
        },
        "sessions": 6,
        "opportunities": 120,
        "missing": [],
        "imputation": "NONE",
    }


def _screen_row(expression: dict, rungs: list[dict]) -> tuple:
    fingerprint = grammar.fingerprint(expression)
    plan = {
        "event": "ORDER_FLOW", "context": ["ALL"],
        "qualities": ["PERSISTENCE"], "direction": "FOLLOW",
        "output": "TAKER_NET_PNL", "execution": "TAKER",
        "horizon_seconds": 30,
    }
    candidate = {
        "intraday_signal_expr": expression,
        "ast_fingerprint": fingerprint,
        "semantic_plan": plan,
        "horizon_seconds": 30,
        "execution": "TAKER",
        "source_lead_ids": ["lead-fresh"],
    }
    config = {
        "intraday_signal_expr": FAILED_EXPR,
        "semantic_plan": plan,
        "horizon_seconds": 30,
        "execution": "TAKER",
        "screening_population": [candidate],
    }
    exposures = [{
        "rung": name,
        "rung_plan_fingerprint": value * 64,
    } for name, value in (("DISCOVERY_6", "1"), ("VALIDATION_20", "2"))]
    primary_fp = grammar.fingerprint(FAILED_EXPR)
    root_id = "10000000-0000-0000-0000-000000000001"
    primary_id = "10000000-0000-0000-0000-000000000002"
    linked_id = "10000000-0000-0000-0000-000000000003"
    registered = {primary_fp: primary_id, fingerprint: linked_id}
    lockbox = {
        "exposures": exposures,
        "root_lineage_id": root_id,
        "primary_candidate_lineage_id": primary_id,
        "registered_candidate_lineages": registered,
    }

    def stable(value) -> str:
        return hashlib.sha256(json.dumps(
            value, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    def candidate_contract(key: str, expr: dict) -> dict:
        feature = {"candidate": key, "contract": "feature-v1"}
        label = {"candidate": key, "contract": "label-v1"}
        model = {"candidate": key, "contract": "model-v1"}
        return {
            "candidate": key,
            "ast_fingerprint": grammar.fingerprint(expr),
            "semantic_plan_fingerprint": stable(plan),
            "horizon_seconds": 30,
            "clock_domains": sorted(
                grammar.effective_clock_domains_of(expr)),
            "execution": "TAKER",
            "feature_contract": feature,
            "label_contract": label,
            "model_contract": model,
        }

    contracts = [
        candidate_contract("PRIMARY", FAILED_EXPR),
        candidate_contract(fingerprint, expression),
    ]

    def lineage(lineage_id: str, expr: dict, contract: dict) -> dict:
        row = {
            "candidate_lineage_id": lineage_id,
            "root_lineage_id": root_id,
            "candidate_ast_fingerprint": stable(expr),
            "semantic_plan_fingerprint": stable(plan),
            "baseline_ast_fingerprint": None,
            "feature_spec_fingerprint": stable(
                contract["feature_contract"]),
            "label_spec_fingerprint": stable(contract["label_contract"]),
            "model_spec_fingerprint": stable(contract["model_contract"]),
            "economic_family_id": "intraday-economic-family-v1:test",
            "evaluator_version": "intraday-candidate-evaluator-v11",
            "cost_model_version": "krx-intraday-execution-v3",
        }
        row["candidate_identity_fingerprint"] = \
            candidate_identity_fingerprint(
                candidate_ast_fingerprint=row[
                    "candidate_ast_fingerprint"],
                semantic_plan_fingerprint=row[
                    "semantic_plan_fingerprint"],
                baseline_ast_fingerprint=None,
                feature_spec_fingerprint=row["feature_spec_fingerprint"],
                label_spec_fingerprint=row["label_spec_fingerprint"],
                model_spec_fingerprint=row["model_spec_fingerprint"],
                evaluator_version=row["evaluator_version"],
                cost_model_version=row["cost_model_version"],
            )
        return row
    lineages = {
        primary_fp: lineage(primary_id, FAILED_EXPR, contracts[0]),
        fingerprint: lineage(linked_id, expression, contracts[1]),
    }
    bound_rungs = []
    governed_rungs = []
    for index, raw_rung in enumerate(rungs):
        rung = dict(raw_rung)
        completion = f"2026-08-18T0{3 + index}:00:00+00:00"
        session_count = 6 if rung["rung"] == "DISCOVERY_6" else 20
        sessions = [f"2026-05-{day:02d}" for day in range(1, session_count + 1)]
        instruments = [
            "20000000-0000-0000-0000-000000000001",
            "20000000-0000-0000-0000-000000000002",
        ]
        panel_keys = ["005930", "000660"]
        content_rows = [{
            "session": session,
            "session_content_fingerprint": stable({"session": session}),
            "quote_rows": 1000,
            "trade_rows": 500,
            "source_watermark": {"session": session, "sealed": True},
        } for session in sessions]
        source_lineage = [{"source": "EXTERNAL_PARQUET",
                           "version": "raw-v1"}]
        panel_manifest = {
            "mode": "NESTED_PANEL",
            "information_rich": panel_keys,
            "representative_guard": [],
            "promotion_authority": False,
        }
        core = {
            "version": "intraday-adaptive-search-exposure-v1",
            "fingerprint_contract": "canonical-json-sha256-v1",
            "identifier_exclusions": [
                "experiment_id", "experiment_rung_id",
                "rung_plan_fingerprint", "candidate_lineage_id",
                "root_lineage_id", "session_access_id",
                "session_exposure_id", "completion_timestamp",
            ],
            "evidence_purpose": "ADAPTIVE_SEARCH",
            "adaptive_search_only": True,
            "promotion_authority": False,
            "dataset": {
                "name": "krx-stock-microstructure",
                "version": "v1",
                "dataset_id": "30000000-0000-0000-0000-000000000001",
                "dataset_cutoff": "2026-08-18T00:00:00+00:00",
                "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
                "stock_universe_contract_version": "stock-universe-v1",
                "reference_identity_fingerprint": stable(instruments),
            },
            "rung": rung["rung"],
            "evaluation": {
                "status": "EVALUATED",
                "measurement_scope": "ADAPTIVE_RUNG_MEASURED",
                "planned_sessions": sessions,
                "planned_session_count": session_count,
                "evaluated_sessions": sessions,
                "evaluated_session_count": session_count,
                "session_set_fingerprint": stable(sessions),
                "panel_replay_keys": panel_keys,
                "panel_reference_instrument_ids": instruments,
                "panel_instrument_count": len(panel_keys),
                "panel_order_fingerprint": stable(panel_keys),
                "panel_reference_set_fingerprint": stable(sorted(instruments)),
                "panel_manifest": panel_manifest,
                "full_universe_instrument_count": len(instruments),
                "full_universe_reference_set_fingerprint": stable(instruments),
            },
            "content_evidence": {
                "scope": "FULL_FROZEN_STOCK_UNIVERSE_PER_SESSION",
                "per_session": content_rows,
                "panel_only_content_fingerprints_available": True,
                "conservative_full_universe_content_limitation": None,
            },
            "source_contract": {
                "event_source": "EXTERNAL_PARQUET",
                "source_lineage": source_lineage,
                "source_lineage_fingerprint": stable(source_lineage),
                "knowledge_clock_mode": "EVENT_TIME_HISTORICAL_ONLY",
                "timestamp_policy": "COMPLETED_SECOND_ONLY",
            },
            "lane_contract": {"version": "lane-v1"},
            "execution_contract": {
                "population_execution_model": "TAKER_SHARED_REPLAY",
                "position_mode": "LONG_ONLY",
                "order_latency_ms": 10,
                "max_quote_age_seconds": 2.0,
                "minimum_predicted_edge_bps": 1.0,
            },
            "evaluator_contract": {
                "runner_version": "intraday-experiment-runner-v16",
                "evaluator_version": "intraday-candidate-evaluator-v11",
                "fast_screen_version": "intraday-fast-discovery-screen-v3",
                "candidate_contracts": contracts,
                "candidate_set_fingerprint": stable(contracts),
            },
            "cost_contract": {
                "cost_model_version": "krx-intraday-execution-v3",
                "fee_bps_per_side": 1.5,
                "maker_fee_bps_per_side": 0.2,
            },
            "cross_checks": {
                "ledger_session_set_fingerprint_verified": True,
                "ledger_full_universe_fingerprint_verified": True,
                "screen_panel_fingerprint_verified": True,
                "screen_panel_manifest_verified": True,
                "per_session_content_evidence_verified": True,
            },
        }
        exposure_fp = stable(core)
        rung["search_exposure"] = {
            **core,
            "search_exposure_fingerprint": exposure_fp,
        }
        rung["search_exposure_fingerprint"] = exposure_fp
        rung["completed_at"] = completion
        rung["candidate_evidence"] = [{
            **row,
            "observed_at": completion,
            "search_exposure_fingerprint": exposure_fp,
            "evidence_scope": (
                "F1" if rung["rung"] == "DISCOVERY_6" else "F2"),
            "adaptive_search_only": True,
            "promotion_authority": False,
            "measurement_scope": "ADAPTIVE_RUNG_MEASURED",
        } for row in rung["candidate_evidence"]]
        bound_rungs.append(rung)
        governed_rungs.append({
            "rung": rung["rung"],
            "rung_plan_fingerprint": next(
                row["rung_plan_fingerprint"] for row in exposures
                if row["rung"] == rung["rung"]),
            "root_lineage_id": root_id,
            "dataset_id": core["dataset"]["dataset_id"],
            "dataset_cutoff": core["dataset"]["dataset_cutoff"],
            "planned_session_dates": sessions,
            "planned_session_count": session_count,
            "planned_instrument_ids": instruments,
            "planned_instrument_count": len(instruments),
            "session_set_fingerprint": stable(sessions),
            "instrument_set_fingerprint": stable(instruments),
            "source_watermark": {
                "event_source": core["source_contract"]["event_source"],
                "source_lineage": source_lineage,
            },
            "session_evidence": [{
                **row,
                "instrument_count": len(instruments),
                "instrument_set_fingerprint": stable(instruments),
            } for row in content_rows],
        })
    return (
        "experiment-search", config, [candidate], {}, bound_rungs,
        lockbox, "adaptive search",
        "2026-08-18T12:00:00+09:00",
        governed_rungs,
        lineages,
    )


def _rung(name: str, expression: dict, *, survivor: bool,
          complete: bool = True) -> dict:
    fingerprint = grammar.fingerprint(expression)
    expected_sessions = 6 if name == "DISCOVERY_6" else 20
    objectives = (_search_objectives(expression)
                  if complete else {
                      "version": "intraday-search-objectives-v1",
                      "complete": False, "values": {},
                      "sessions": 0, "opportunities": 0,
                      "missing": ["sessions", "opportunities"],
                      "imputation": "NONE",
                  })
    if complete:
        objectives["sessions"] = expected_sessions
    linked = {
        "candidate": fingerprint,
        "summary": {"sessions": objectives["sessions"],
                    "opportunities": objectives["opportunities"]},
        "failed_criteria": ([] if complete else
                            ["NO_EXECUTABLE_OBSERVATIONS"]),
        "search_objectives": objectives,
    }
    primary_objectives = _search_objectives(FAILED_EXPR)
    primary_objectives["sessions"] = expected_sessions
    selected = [fingerprint] if survivor and name == "DISCOVERY_6" else []
    return {
        "rung": name,
        "primary_pass": True,
        "survivors": ["PRIMARY", *([fingerprint] if survivor else [])],
        "candidate_count": 2,
        "candidate_evidence": [{
            "candidate": "PRIMARY",
            "summary": {"sessions": expected_sessions,
                        "opportunities": 120},
            "failed_criteria": [],
            "search_objectives": primary_objectives,
        }, linked],
        "next_rung_selection": {
            "selected_linked_ast_fingerprints": selected,
            "eliminated_linked_ast_fingerprints": (
                [] if selected else [fingerprint]),
        },
    }


def test_real_screen_row_hydrates_explicit_survivor_archive_parent():
    rows = breeder._outcome_records([], [
        _screen_row(FRESH_EXPR, [
            _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)])])

    linked = next(row for row in rows if row["expression"] == FRESH_EXPR)
    assert len(rows) == 2
    assert linked["archive_history"] is True
    assert linked["evidence_scope"] == "F1"
    assert linked["explicit_survivor"] is True
    assert linked["futility_gate_status"] == "SURVIVED"
    assert linked["resource_allocation_status"] == "SELECTED_NEXT_RUNG"
    assert len(linked["exposure_fingerprint"]) == 64
    assert linked["exposure_fingerprint"] != "1" * 64

    result = breeder.generate_from_records(
        leads=[lead("lead-fresh", FRESH_EXPR, "signed tape persistence")],
        outcome_rows=rows, population_size=16, generation=7)

    assert result["ok"] is True
    assert result["audit"]["source_backed_search_elites"] == 1
    assert result["audit"]["exploit_parent_count"] == 1
    assert result["audit"]["search_memory"]["missing_values_filled_with_zero"] is False


def test_later_non_survivor_f2_retires_old_f1_survivor():
    rows = breeder._outcome_records([], [
        _screen_row(FRESH_EXPR, [
            _rung("DISCOVERY_6", FRESH_EXPR, survivor=True),
            _rung("VALIDATION_20", FRESH_EXPR, survivor=False),
        ])])
    result = breeder.generate_from_records(
        leads=[lead("lead-fresh", FRESH_EXPR, "signed tape persistence")],
        outcome_rows=rows, population_size=16, generation=8)

    # MAP-Elites retains one incumbent per behavioural niche.  The later F2
    # observation replaces the F1 observation for this candidate/niche.
    assert result["audit"]["search_memory"]["archive_entries"] == 1
    assert result["audit"]["source_backed_search_elites"] == 0
    assert result["audit"]["exploit_parent_count"] == 0
    assert result["ok"] is False


def test_f2_budget_nonselection_does_not_erase_futility_survivor():
    rows = breeder._outcome_records([], [
        _screen_row(FRESH_EXPR, [
            _rung("DISCOVERY_6", FRESH_EXPR, survivor=True),
            _rung("VALIDATION_20", FRESH_EXPR, survivor=True),
        ])])
    linked_f2 = next(row for row in rows
                     if row["expression"] == FRESH_EXPR
                     and row["evidence_scope"] == "F2")
    result = breeder.generate_from_records(
        leads=[lead("lead-fresh", FRESH_EXPR, "signed tape persistence")],
        outcome_rows=rows, population_size=16, generation=8)

    assert linked_f2["explicit_survivor"] is True
    assert linked_f2["resource_allocation_status"] == "NOT_SELECTED_BUDGET"
    assert result["audit"]["source_backed_search_elites"] == 1
    assert result["audit"]["exploit_parent_count"] == 1


def test_no_observations_are_retryable_memory_not_formula_failure():
    rows = breeder._outcome_records([], [
        _screen_row(FRESH_EXPR, [
            _rung("DISCOVERY_6", FRESH_EXPR, survivor=False,
                  complete=False)])])
    result = breeder.generate_from_records(
        leads=[lead("lead-fresh", FRESH_EXPR, "signed tape persistence")],
        outcome_rows=rows, population_size=16, generation=9)

    linked = next(row for row in rows if row["expression"] == FRESH_EXPR)
    assert linked["decision"] == "NO_EVIDENCE"
    assert result["ok"] is True
    assert result["audit"]["source_backed_search_elites"] == 0
    assert result["audit"]["exploration_parent_count"] == 1


def test_report_rung_must_match_authoritative_database_fingerprint():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    row[-2] = [{"rung": "DISCOVERY_6",
                "rung_plan_fingerprint": "f" * 64}]

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_search_exposure_contract_declaration_is_part_of_fingerprint():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    exposure = row[4][0]["search_exposure"]
    exposure["fingerprint_contract"] = "forged-contract"

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_search_exposure_identifier_exclusions_must_match_producer():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    exposure = row[4][0]["search_exposure"]
    exposure["identifier_exclusions"] = ["experiment_id"]

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_lineage_ast_identity_mismatch_fails_closed():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    linked_fp = grammar.fingerprint(FRESH_EXPR)
    row[-1][linked_fp]["candidate_ast_fingerprint"] = "f" * 64

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_lineage_candidate_identity_mismatch_fails_closed():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    linked_fp = grammar.fingerprint(FRESH_EXPR)
    row[-1][linked_fp]["candidate_identity_fingerprint"] = "f" * 64

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_self_hashed_but_incomplete_search_exposure_fails_closed():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    rung = row[4][0]
    exposure = rung["search_exposure"]
    exposure.pop("dataset")
    core = {key: value for key, value in exposure.items()
            if key != "search_exposure_fingerprint"}
    forged = hashlib.sha256(json.dumps(
        core, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    exposure["search_exposure_fingerprint"] = forged
    rung["search_exposure_fingerprint"] = forged
    for evidence in rung["candidate_evidence"]:
        evidence["search_exposure_fingerprint"] = forged

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_report_content_must_match_durable_exposure_ledger():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    row[-2][0]["session_evidence"][0]["quote_rows"] += 1

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_report_source_lineage_must_match_durable_rung_ledger():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    row[-2][0]["source_watermark"]["source_lineage"][0]["version"] = \
        "different-raw-content"

    assert breeder._outcome_records([], [tuple(row)]) == []


def test_neutral_final_screen_does_not_erase_measured_survivor():
    row = list(_screen_row(FRESH_EXPR, [
        _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)]))
    linked_fp = grammar.fingerprint(FRESH_EXPR)
    row[3] = {linked_fp: {
        "failed_criteria": [],
        "summary": {"sessions": 60, "opportunities": 1000},
        "score_calibration": {},
    }}
    rows = breeder._outcome_records([], [tuple(row)])
    result = breeder.generate_from_records(
        leads=[lead("lead-fresh", FRESH_EXPR, "signed tape persistence")],
        outcome_rows=rows, population_size=16, generation=10)

    assert result["audit"]["source_backed_search_elites"] == 1
    assert result["audit"]["exploit_parent_count"] == 1


def test_old_evaluator_failure_cannot_retire_current_source_seed():
    failure = cost_failure()
    failure["evaluator_version"] = "intraday-candidate-evaluator-v10"
    failure["cost_model_version"] = "krx-intraday-execution-v2"
    result = breeder.generate_from_records(
        leads=[lead("lead_failed", FAILED_EXPR, "depth divergence")],
        outcome_rows=[failure], population_size=16, generation=11)

    assert result["ok"] is True
    assert result["kpi"]["rejection_counts"].get(
        "FAILED_FORMULA_RESEED", 0) == 0


def test_same_ast_different_semantic_plan_cannot_borrow_elite_score():
    rows = breeder._outcome_records([], [
        _screen_row(FRESH_EXPR, [
            _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)])])
    incompatible = lead(
        "lead-fresh", FRESH_EXPR, "different economic candidate")
    incompatible["contract"]["semantic_plan"]["direction"] = "REVERT"
    result = breeder.generate_from_records(
        leads=[incompatible], outcome_rows=rows,
        population_size=16, generation=12)

    assert result["audit"]["source_backed_search_elites"] == 0
    assert result["audit"]["exploit_parent_count"] == 0


def test_level_ast_uses_explicit_decision_snapshot_clock():
    rows = breeder._outcome_records([], [
        _screen_row(FRESH_EXPR, [
            _rung("DISCOVERY_6", FRESH_EXPR, survivor=True)])])
    primary = next(row for row in rows if row["expression"] == FAILED_EXPR)

    assert primary["clock_domains"] == ["DECISION_SNAPSHOT"]
