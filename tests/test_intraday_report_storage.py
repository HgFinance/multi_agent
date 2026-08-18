from __future__ import annotations

import copy
from datetime import date, timedelta
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
if str(PIPELINE) not in sys.path:
    sys.path.insert(0, str(PIPELINE))

import intraday_experiment_runner as runner  # noqa: E402


def _full_60_report() -> dict:
    sessions = [
        (date(2026, 1, 1) + timedelta(days=index)).isoformat()
        for index in range(60)
    ]
    folds = [{
        "fold": number + 1,
        "start_session": sessions[number * 15],
        "end_session": sessions[number * 15 + 14],
        "sessions": 15,
        "mean_net_bps": float(number + 1),
    } for number in range(4)]
    exposure = {
        "rung": runner.FULL_60,
        "experiment_rung_id": "1" * 36,
        "session_count": 60,
        "instrument_count": 2,
        "session_set_fingerprint": runner.stable_fingerprint(sessions),
        "instrument_ids_fingerprint": "2" * 64,
        "rung_plan_fingerprint": "3" * 64,
        "sessions": [{
            "session": session,
            "session_content_fingerprint": f"{index:064x}",
        } for index, session in enumerate(sessions, 1)],
    }
    return {
        "summary": {"sessions": 60},
        "folds": folds,
        "trial_lockbox": {"exposures": [exposure]},
    }


def test_full_60_identity_requires_exact_rung_and_four_contiguous_folds():
    report = _full_60_report()
    identity = runner._intraday_evaluation_identity(report)

    assert identity["evaluation_identity_complete"] is True
    assert identity["evaluation_scope"] == runner.FULL_60
    assert identity["primary_fold_count"] == 4
    assert identity["session_set_fingerprint"] == \
        report["trial_lockbox"]["exposures"][0]["session_set_fingerprint"]
    assert identity["instrument_ids_fingerprint"] == "2" * 64
    assert identity["rung_plan_fingerprint"] == "3" * 64

    broken = []
    stale_session_set = copy.deepcopy(report)
    stale_session_set["trial_lockbox"]["exposures"][0][
        "session_set_fingerprint"] = "4" * 64
    broken.append(stale_session_set)
    missing_plan = copy.deepcopy(report)
    missing_plan["trial_lockbox"]["exposures"][0].pop(
        "rung_plan_fingerprint")
    broken.append(missing_plan)
    short = copy.deepcopy(report)
    short["trial_lockbox"]["exposures"][0]["sessions"].pop()
    broken.append(short)
    one_fold = copy.deepcopy(report)
    one_fold["folds"] = [{
        "fold": 1,
        "start_session": report["folds"][0]["start_session"],
        "end_session": report["folds"][-1]["end_session"],
        "sessions": 60,
        "mean_net_bps": 1.0,
    }]
    broken.append(one_fold)
    overlap = copy.deepcopy(report)
    overlap["folds"][1]["start_session"] = \
        overlap["folds"][0]["end_session"]
    broken.append(overlap)
    wrong_size = copy.deepcopy(report)
    wrong_size["folds"][0]["sessions"] = 14
    broken.append(wrong_size)

    assert all(not runner._intraday_evaluation_identity(candidate)[
        "evaluation_identity_complete"] for candidate in broken)


def _large_calibration() -> dict:
    parameters = {
        target: {
            "intercept": 0.125,
            "coefficients": [index / 100.0 for index in range(46)],
            "means": [index / 200.0 for index in range(46)],
            "scales": [1.0 + index / 300.0 for index in range(46)],
        }
        for target in ("markout_bps", "net_bps", "positive_net")
    }
    teacher = {
        "version": "krx-cost-aware-linear-teacher-v2",
        "status": "PASS",
        "observations": 1200,
        "sessions": 2,
        "model_parameters": parameters,
        "model_fingerprint": "a" * 64,
        "oos_fit_forbidden": True,
    }
    return {
        "version": "intraday-score-calibration-v2",
        "status": "PASS",
        "coefficient_policy": "STRUCTURE_ONLY",
        "beta_bps_per_score_unit": 1.25,
        "oos_fit_forbidden": True,
        "supervised_control": teacher,
    }


def _large_residual() -> dict:
    cell = {
        "observations": 1000,
        "mean_error_bps": -1.2345,
        "mean_absolute_error_bps": 2.3456,
        "rmse_bps": 3.4567,
        "null_mean_absolute_error_bps": 4.5678,
        "null_rmse_bps": 5.6789,
        "mae_improvement_vs_null_bps": 2.2222,
    }
    return {
        "version": "krx-domain-residual-qd-v1",
        "status": "PASS",
        "target": "LONG_MIDPRICE_MARKOUT_BPS",
        "prediction_unit": "BPS",
        "observations": 4000,
        "median_time_bucket_mae_bps": 2.5,
        "worst_time_bucket": "OPEN",
        "time_buckets": {
            key: dict(cell) for key in
            ("OPEN", "MIDDAY", "CLOSE", "CONTINUOUS")
        },
        "selection_boundary": "OOS_DIAGNOSTIC_SCREENING_ONLY",
        "adaptive_search_memory_only": True,
        "independent_confirmation": False,
        "forward_new_sessions_required": True,
        "promotion_authority": False,
    }


class _StoreCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).split()).lower()
        self.conn.executed.append((normalized, params))
        self.row = ((params[1],)
                    if "insert into quant.intraday_report_manifests" in normalized
                    else None)

    def fetchone(self):
        return self.row


class _StoreConnection:
    def __init__(self):
        self.executed = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _StoreCursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


class _LoadCursor:
    def __init__(self, config, rows, manifest):
        self.config = config
        self.rows = rows
        self.manifest = manifest
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()):
        normalized = " ".join(str(sql).split()).lower()
        if "from quant.intraday_forward_report_revisions" in normalized:
            self.result = None
        elif "select config from quant.experiments" in normalized:
            self.result = (self.config,)
        elif "from quant.experiment_metrics" in normalized:
            self.result = list(self.rows)
        elif "from quant.intraday_report_manifests" in normalized:
            self.result = (self.manifest,)
        elif "from quant.intraday_forward_confirmations" in normalized:
            self.result = None
        else:
            raise AssertionError(f"unexpected load SQL: {normalized}")

    def fetchone(self):
        return self.result

    def fetchall(self):
        return self.result


class _LoadConnection:
    def __init__(self, config, rows, manifest):
        self.args = config, rows, manifest

    def cursor(self):
        return _LoadCursor(*self.args)


def test_full_artifacts_live_only_in_manifest_and_rehydrate_losslessly():
    calibration = _large_calibration()
    residual = _large_residual()
    residual_qd = {
        "status": "ELIGIBLE", "cell": "OPEN/NODES_1_5", "elite": True,
        "competitors": 4, "promotion_authority": False,
    }
    side_calibration = {
        **calibration,
        "beta_bps_per_score_unit": 0.75,
        "supervised_control": {
            **calibration["supervised_control"],
            "model_fingerprint": "b" * 64,
        },
    }
    reproduction_runtime = runner._qa_reproduction_runtime_manifest(
        hypothesis_id="20000000-0000-0000-0000-000000000001",
        config={
            "asset_scope": "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY",
            "threshold": 0.25,
        })
    report = {
        "summary": {"mean_net_bps_per_opportunity": 1.1},
        "score_calibration": calibration,
        "residual_behavior": residual,
        "residual_qd": residual_qd,
        "folds": [{"fold": 0, "start_session": "2026-05-18",
                   "end_session": "2026-05-30", "mean_net_bps": 1.0}],
        "screening_population": [{
            "ast_fingerprint": "c" * 64,
            "summary": {"mean_net_bps_per_opportunity": 0.5},
            "folds": [],
            "lane_manifest": {"score_calibration": side_calibration},
            "screening_gate_decision": "HOLD",
            "failed_criteria": ["COST_NET_EDGE_NOT_POSITIVE"],
            "pareto_rank": 2,
            "pareto_front": False,
            "novelty_vs_primary": 0.4,
            "complexity_nodes": 5,
            "source_lead_ids": [f"lead-{index}" for index in range(200)],
            "candidate_role": "LINKED_CANDIDATE",
            "empirical_influence": {"detail": "x" * 3000},
            "residual_behavior": residual,
            "residual_qd": residual_qd,
        }],
        "decision": "HOLD",
        "failed_criteria": ["INDEPENDENT_FORWARD_CONFIRMATION_PENDING"],
        "evidence_tier": "SEARCH_EXPOSED_HISTORICAL_SUPPORT",
        "supervised_control": {
            "calibration": calibration["supervised_control"]},
        "hybrid_control": {},
        "multiple_testing": {},
        "trial_lockbox": {"universe": "STOCK"},
        "discovery_rungs": [],
        "forward_lockbox": {"required": True},
        "reproduction_runtime": reproduction_runtime,
        "slice": {"product_filter": "STOCK",
                  "product_filter_version": "stock-only-v1"},
    }

    # This is the failure mode being guarded: a real 46-feature teacher makes
    # one calibration projection far larger than PostgreSQL's B-tree tuple cap.
    assert len(json.dumps(calibration).encode("utf-8")) > 2_704

    conn = _StoreConnection()
    runner._store_report(conn, "experiment-1", report)
    assert conn.commits == 1 and conn.rollbacks == 0

    manifest_call = next(
        call for call in conn.executed
        if "insert into quant.intraday_report_manifests" in call[0])
    manifest_params = manifest_call[1]
    assert manifest_params[2] == runner.REPORT_MANIFEST_VERSION
    manifest = json.loads(manifest_params[3])
    assert manifest["score_calibration"] == calibration
    assert manifest["residual_behavior"] == residual
    assert manifest["screening_candidates"]["c" * 64][
        "score_calibration"] == side_calibration
    assert manifest["screening_candidates"]["c" * 64][
        "empirical_influence"] == {"detail": "x" * 3000}
    assert manifest["reproduction_runtime"] == reproduction_runtime

    metric_calls = [call for call in conn.executed
                    if "insert into quant.experiment_metrics" in call[0]]
    assert metric_calls
    metric_rows = []
    for _, params in metric_calls:
        dimensions_json = params[3]
        dimensions = json.loads(dimensions_json)
        assert len(dimensions_json.encode("utf-8")) <= \
            runner.MAX_INDEXED_DIMENSIONS_JSON_BYTES
        assert "model_parameters" not in dimensions_json
        assert "supervised_control" not in dimensions_json
        assert "time_buckets" not in dimensions_json
        assert "source_lead_ids" not in dimensions_json
        assert "empirical_influence" not in dimensions_json
        metric_rows.append((params[1], params[2], dimensions))

    config = {
        "intraday_signal_expr": {
            "op": "field", "field": "microprice_offset_bps"},
        "screening_population": [{
            "ast_fingerprint": "c" * 64,
            "candidate_role": "LINKED_CANDIDATE",
            "source_lead_ids": ["lead-0"],
            "coefficient_policy": "STRUCTURE_ONLY",
        }],
        "slice": report["slice"],
    }
    restored = runner._load_completed_report(
        _LoadConnection(config, metric_rows, manifest), "experiment-1")
    assert restored["score_calibration"] == calibration
    assert restored["residual_behavior"] == residual
    assert restored["residual_qd"] == residual_qd
    assert restored["failed_criteria"] == \
        ["INDEPENDENT_FORWARD_CONFIRMATION_PENDING"]
    assert restored["screening_population"][0]["lane_manifest"][
        "score_calibration"] == side_calibration
    assert restored["screening_population"][0]["residual_behavior"] == residual
    assert restored["screening_population"][0]["empirical_influence"] == \
        {"detail": "x" * 3000}
    assert restored["reproduction_runtime"] == reproduction_runtime


def test_dimensions_budget_rejects_future_uncompacted_artifact():
    with pytest.raises(RuntimeError, match="compact B-tree budget"):
        runner._indexed_dimensions_json({"accidental_artifact": "x" * 2_000})


def test_forward_candidate_query_prefers_manifest_calibration():
    assert "m.report->'score_calibration'" in runner._FORWARD_CANDIDATES_SQL
    assert "calibration.dimensions" in runner._FORWARD_CANDIDATES_SQL
    factory_source = (
        ROOT / "departments" / "01-research" / "factory" /
        "factory_autopilot.py").read_text(encoding="utf-8")
    assert "manifest.report->'score_calibration'" in factory_source
    assert "manifest.report->'residual_behavior'" in factory_source
    assert "manifest.report->'screening_candidates'" in factory_source


def test_authoritative_forward_revision_supersedes_without_mutating_manifest():
    revision = {
        "decision": "SUBMIT_TO_QA",
        "evidence_tier": "INDEPENDENT_FORWARD_CONFIRMATION",
        "authoritative_revision": {"revision_number": 1},
    }

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params=()):
            assert "quant.intraday_forward_report_revisions" in str(sql)

        def fetchone(self):
            return (revision,)

    class Connection:
        def cursor(self):
            return Cursor()

    assert runner._load_completed_report(Connection(), "experiment-1") == \
        revision

    source = (PIPELINE / "intraday_experiment_runner.py").read_text(
        encoding="utf-8")
    assert "insert into research.experiment_outcomes (" not in source
    assert "update quant.intraday_report_manifests" not in source


def test_forward_publication_migration_versions_outcomes_and_is_append_only():
    migration = (ROOT / "supabase" / "migrations" /
                 "20260818000100_intraday_forward_publication_queue.sql")
    sql = migration.read_text(encoding="utf-8").lower()
    assert "create table research.experiment_outcome_revisions" in sql
    assert "create view research.v_current_experiment_outcomes" in sql
    assert "select distinct on (base.experiment_id) base.*" in sql
    assert "outcome_revision_id, forward_confirmation_id, experiment_id" in sql
    assert "experiment_outcome_revisions_append_only" in sql
    assert "intraday_forward_report_revisions_append_only" in sql
    assert "intraday_forward_qa_handoffs_append_only" in sql
    assert "for select to svc_quant" in sql
    assert "hypotheses_svc_quant_update" in sql
    assert "'failed'" in sql and "max_error_count" in sql
    assert sql.count("research.v_current_experiment_outcomes") >= 4
    assert "create or replace view research.v_trial_family_status" in sql
    assert "create or replace view research.v_experiment_scorecard" in sql
    for column in ("max_drawdown_stop", "vol_target_annual",
                   "max_exposure", "min_adv_krw", "risk_controlled"):
        assert column in sql


def test_forward_semantic_guard_migration_is_fail_closed_and_keeps_empty_lessons():
    migration = (ROOT / "supabase" / "migrations" /
                 "20260818000200_intraday_forward_semantic_guards.sql")
    sql = migration.read_text(encoding="utf-8").lower()

    assert "intraday_forward_report_revision_semantic_guard" in sql
    assert "new.decision is distinct from confirmation_decision" in sql
    assert "outcome_decision is distinct from expected_outcome_decision" in sql
    assert "new.hypothesis_status is distinct from expected_hypothesis_status" in sql
    assert "new.report->>'decision' is distinct from expected_report_decision" in sql
    assert "intraday_forward_qa_handoff_pass_guard" in sql
    assert "report.decision = 'pass'" in sql
    assert "report.hypothesis_status = 'supported'" in sql
    assert "do $semantic_audit$" in sql
    assert "existing forward publication violates semantic decision mapping" in sql
    assert "existing qa handoff is not backed by a pass forward report" in sql

    # LEFT JOIN emits one null lesson row for an empty array. DISTINCT outcome
    # counting also prevents multi-code outcomes from inflating family totals.
    assert "left join lateral unnest" in sql
    assert "coalesce(outcome.lesson_codes, '{}'::text[])" in sql
    assert "count(distinct outcome.outcome_id) as outcomes" in sql


def test_semantic_outcome_consumers_read_the_current_projection():
    relative_paths = (
        "departments/01-research/factory/proposal_intake.py",
        "departments/01-research/factory/factory_autopilot.py",
        "departments/01-research/factory/cycle_brief.py",
        "departments/01-research/factory/bottleneck_census.py",
        "departments/01-research/factory/progress.py",
        "departments/01-research/api/mcp_server.py",
        "departments/04-quant-backtest/pipeline/allocator.py",
        "departments/04-quant-backtest/pipeline/factory_bridge.py",
    )
    for relative_path in relative_paths:
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "research.v_current_experiment_outcomes" in source, \
            relative_path

    # Storage/deduplication still targets revision 0. A second legacy row would
    # resurrect the contradictory all-row-consumer failure this view avoids.
    bridge = (ROOT / relative_paths[-1]).read_text(encoding="utf-8")
    assert "insert into research.experiment_outcomes" in bridge
    assert "select 1 from research.experiment_outcomes" in bridge
