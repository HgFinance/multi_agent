from __future__ import annotations

import inspect
import importlib.util
import hashlib
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
FACTORY = ROOT / "departments" / "01-research" / "factory"
for path in (str(PIPELINE), str(FACTORY)):
    if path not in sys.path:
        sys.path.insert(0, path)

import allocator  # noqa: E402
import backtest_runner  # noqa: E402
import bottleneck_census  # noqa: E402
import cycle_brief  # noqa: E402
import data_resolution  # noqa: E402
import experiment_orchestrator  # noqa: E402
import factory_autopilot  # noqa: E402
import factory_bridge  # noqa: E402
import grid  # noqa: E402
import objective  # noqa: E402
import orphan_finalizer  # noqa: E402
import progress  # noqa: E402
import walk_forward  # noqa: E402
from stock_universe import (  # noqa: E402
    INTRADAY_REPORT_MANIFEST_VERSION,
    build_stock_evaluation_identity,
    governed_stock_dataset_sql,
    governed_stock_evidence_sql,
)

_MCP_SPEC = importlib.util.spec_from_file_location(
    "research_mcp_server_stock_scope",
    ROOT / "departments" / "01-research" / "api" / "mcp_server.py",
)
assert _MCP_SPEC is not None and _MCP_SPEC.loader is not None
research_mcp_server = importlib.util.module_from_spec(_MCP_SPEC)
_MCP_SPEC.loader.exec_module(research_mcp_server)


def test_common_predicate_requires_stock_identity_and_modern_full_60() -> None:
    sql = governed_stock_evidence_sql(
        experiment_alias="experiment", dataset_alias="dataset",
        hypothesis_alias="hypothesis")

    assert "evaluation_identity_complete" in sql
    assert "KRX_ACTIVE_STOCK_ONLY" in sql
    assert "DAILY_WALK_FORWARD" in sql
    assert "quant.universe_members" in sql
    assert "quant.current_krx_stock_instrument_identity" in sql
    assert "coalesce(upper(governed_instrument.instrument_type), '')" in sql
    assert "governed_instrument.metadata->>'is_spac'" not in sql
    assert "governed_instrument.is_spac" in sql
    assert "all-stock-full-replay-v1" in sql
    assert "full_rung.rung = 'FULL_60'" in sql
    assert "quant.intraday_session_exposures" in sql
    assert "count(distinct exposure.session_date)" in sql
    assert "exposure.root_lineage_id = full_rung.root_lineage_id" in sql
    assert "exposure.exposure_purpose = 'ADAPTIVE_SEARCH'" in sql
    assert "EVENT_TIME_HISTORICAL_ONLY" in sql
    assert "exposure.experiment_rung_id" not in sql
    assert ") = 60" in sql
    assert "planned_session.session_date" in sql
    assert "not (evidence_metric.dimensions ? 'screening_candidate')" in sql
    assert "complete_metric.dimensions ? 'screening_candidate'" in sql
    assert "'experiment_rung_id', full_rung.experiment_rung_id::text" in sql
    assert "'instrument_count', full_rung.planned_instrument_count" in sql
    assert "full_rung.instrument_set_fingerprint" in sql
    assert "full_rung.session_set_fingerprint" in sql
    assert "full_rung.rung_plan_fingerprint" in sql
    assert "primary_fold_count', 4" in sql
    assert "count(*) = 4" in sql
    assert "count(distinct" in sql
    assert "jsonb_array_elements" in sql
    assert "expected_fold->>'window'" in sql
    assert "expected_fold->>'start_session'" in sql
    assert "expected_fold->>'end_session'" in sql
    assert f"'{INTRADAY_REPORT_MANIFEST_VERSION}'" in sql
    assert "reproduction_runtime" in sql
    assert "intraday-forward-reproduction-runtime-v1" in sql
    assert "experiment_input_hash" in sql
    assert "runtime_manifest_fingerprint" in sql
    assert "source_fingerprint" in sql
    assert "::integer" not in sql

    import intraday_experiment_runner
    assert intraday_experiment_runner.REPORT_MANIFEST_VERSION == \
        INTRADAY_REPORT_MANIFEST_VERSION

    with pytest.raises(ValueError, match="simple identifiers"):
        governed_stock_evidence_sql(experiment_alias="e; drop table x")


def test_common_predicate_is_balanced_and_dbapi_percent_safe() -> None:
    """Every embedding must parse and parameterized callers must bind safely."""

    sql = governed_stock_evidence_sql(
        experiment_alias="e", dataset_alias="m", hypothesis_alias="h")

    # The predicate is interpolated into many larger statements.  An open
    # parenthesis here makes all of them fail only when PostgreSQL parses the
    # final statement, which previously disabled allocator ordering at runtime.
    assert sql.count("(") == sql.count(")")
    assert objective._SQL_FAMILY_BEST.count("(") == \
        objective._SQL_FAMILY_BEST.count(")")

    # psycopg2 applies percent-style binding to the whole composed statement.
    # A raw LIKE wildcard inside this helper consumes the caller's parameter
    # and raises IndexError before PostgreSQL sees the query.  Python's percent
    # operator exercises the same escaping contract without requiring a live
    # database in the unit suite.
    parameterized = f"select 1 where {sql} limit %s"
    rendered = parameterized % (12,)
    assert "like\n               'INTRADAY_FOLD_%'" in rendered
    assert rendered.endswith("limit 12")

    class PercentBindingCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, statement, params=()) -> None:
            self.rendered = str(statement) % tuple(params)

        def fetchall(self):
            return []

    class Connection:
        def __init__(self) -> None:
            self.bound = PercentBindingCursor()

        def cursor(self):
            return self.bound

    conn = Connection()
    assert cycle_brief.load_lessons(conn, limit=7) == []
    assert conn.bound.rendered.endswith("limit 7\n    ")


def _flat_daily_evidence_sql() -> str:
    return " ".join(governed_stock_evidence_sql(
        experiment_alias="experiment", dataset_alias="dataset",
        hypothesis_alias="hypothesis").split())


def _daily_plan_dates(count: int = 520) -> list[date]:
    days: list[date] = []
    current = date(2024, 1, 2)
    while len(days) < count:
        if current.weekday() < 5:
            days.append(current)
        current += timedelta(days=1)
    return days


def _frozen_daily_plan(*, dates=None, warmup=30, embargo=5) -> dict:
    return walk_forward.freeze_daily_evaluation_plan(
        dataset_content_hash="a" * 64,
        dates=list(dates or _daily_plan_dates()),
        warmup_days=warmup,
        embargo_days=embargo,
        cost_model=backtest_runner.COST_MODEL,
    )


def test_daily_plan_is_frozen_before_insert_and_exactly_decodable(
        monkeypatch) -> None:
    monkeypatch.setattr(walk_forward, "wf_code_version", lambda: "wf-code-a")
    plan = _frozen_daily_plan()
    decoded = walk_forward.windows_from_frozen_daily_plan(plan)

    assert plan["policy"] == "walk-forward-rolling-6m"
    assert plan["plan_version"] == "daily-walk-forward-plan-v1"
    assert plan["evaluation_scope"] == "DAILY_WALK_FORWARD"
    assert plan["dataset_content_hash"] == "a" * 64
    assert plan["warmup_trading_days"] == 30
    assert plan["embargo_days"] == 5
    assert plan["cost_model"] == backtest_runner.COST_MODEL
    assert plan["walk_forward_code_version"] == "wf-code-a"
    assert len(decoded) == len(plan["windows"]) > 0
    assert [{
        "window": window.label,
        "test_start": window.test_start.isoformat(),
        "test_end": window.test_end.isoformat(),
    } for window in decoded] == [{
        "window": window["window"],
        "test_start": window["test_start"],
        "test_end": window["test_end"],
    } for window in plan["windows"]]

    source = inspect.getsource(backtest_runner.register_and_run)
    assert source.index("freeze_daily_evaluation_plan") < source.index(
        "insert into quant.experiments")
    assert 'evaluation_mode: str | None = None' in source
    assert "json.dumps(split_policy, sort_keys=True)" in source
    sql = _flat_daily_evidence_sql()
    assert (
        "experiment.split_policy->'cost_model'->>'version' = "
        "experiment.cost_model_version"
        in sql
    )
    assert "pg_input_is_valid(" in sql
    assert "then (expected_window->>'test_start')::date <=" in sql
    assert "walk_forward_code_version" in sql
    assert "from quant.universe_members daily_member" in sql
    assert "daily_instrument.listed_from >" in sql
    assert "daily_instrument.listed_to <" in sql


def test_daily_input_hash_changes_with_window_embargo_and_wf_code(
        monkeypatch) -> None:
    dates = _daily_plan_dates()
    monkeypatch.setattr(walk_forward, "wf_code_version", lambda: "wf-code-a")
    baseline = _frozen_daily_plan(dates=dates, embargo=5)
    changed_window = _frozen_daily_plan(dates=dates[:-1], embargo=5)
    changed_embargo = _frozen_daily_plan(dates=dates, embargo=6)
    monkeypatch.setattr(walk_forward, "wf_code_version", lambda: "wf-code-b")
    changed_code = _frozen_daily_plan(dates=dates, embargo=5)

    def experiment_hash(plan):
        return backtest_runner.input_hash(
            "a" * 64, {"strategy": "TEST"}, "runner-code", 0,
            evaluation_plan=plan)

    base_hash = experiment_hash(baseline)
    assert base_hash != experiment_hash(changed_window)
    assert base_hash != experiment_hash(changed_embargo)
    assert base_hash != experiment_hash(changed_code)

    # No evaluation mode means no new payload key and retains the historical
    # single-window hash exactly, rather than invalidating unrelated callers.
    legacy_facts = {
        "dataset": "a" * 64,
        "config": {"strategy": "TEST"},
        "code": "runner-code",
        "seed": 0,
        "cost": backtest_runner.COST_MODEL,
    }
    expected_legacy = hashlib.sha256(
        json.dumps(legacy_facts, sort_keys=True).encode()).hexdigest()
    assert backtest_runner.input_hash(
        "a" * 64, {"strategy": "TEST"}, "runner-code", 0,
    ) == expected_legacy


def test_orchestrator_uses_only_verified_frozen_windows_and_blocks_drift(
        monkeypatch) -> None:
    dates = _daily_plan_dates()
    monkeypatch.setattr(walk_forward, "wf_code_version", lambda: "wf-code-a")
    frozen = _frozen_daily_plan(dates=dates, embargo=5)

    decoded = experiment_orchestrator._verified_frozen_daily_windows(
        frozen_plan=frozen,
        dataset_content_hash="a" * 64,
        dates=dates,
        warmup_days=30,
        embargo_days=5,
        cost_model=backtest_runner.COST_MODEL,
    )
    assert [window.label for window in decoded] == [
        row["window"] for row in frozen["windows"]]

    other_valid_plan = _frozen_daily_plan(dates=dates, embargo=6)
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        experiment_orchestrator._verified_frozen_daily_windows(
            frozen_plan=other_valid_plan,
            dataset_content_hash="a" * 64,
            dates=dates,
            warmup_days=30,
            embargo_days=5,
            cost_model=backtest_runner.COST_MODEL,
        )

    source = inspect.getsource(experiment_orchestrator._default_chain)
    assert 'evaluation_mode="DAILY_WALK_FORWARD"' in source
    assert "_verified_frozen_daily_windows(" in source
    assert "make_windows(" not in source


def test_frozen_plan_and_metric_identity_share_exact_join_keys(
        monkeypatch) -> None:
    monkeypatch.setattr(walk_forward, "wf_code_version", lambda: "wf-code-a")
    plan = _frozen_daily_plan()
    decoded = walk_forward.windows_from_frozen_daily_plan(plan)
    boundaries = [{
        "window": window.label,
        "start_session": window.test_start.isoformat(),
        "end_session": window.test_end.isoformat(),
    } for window in decoded]
    identity = build_stock_evaluation_identity(
        dataset_id="00000000-0000-0000-0000-000000000001",
        dataset_content_hash="a" * 64,
        universe_version_id="00000000-0000-0000-0000-000000000002",
        instrument_ids=["00000000-0000-0000-0000-000000000003"],
        windows=boundaries,
        cost_model_version=backtest_runner.COST_MODEL["version"],
        evaluation_scope="DAILY_WALK_FORWARD",
        evaluation_plan_fingerprint=plan["evaluation_plan_fingerprint"],
    )

    assert identity["evaluation_plan_fingerprint"] == \
        plan["evaluation_plan_fingerprint"]
    assert identity["session_boundary_fingerprint"] == \
        plan["session_boundary_fingerprint"]
    sql = _flat_daily_evidence_sql()
    assert (
        "evidence_metric.dimensions->> 'evaluation_plan_fingerprint' = "
        "experiment.split_policy->> 'evaluation_plan_fingerprint'"
        in sql
    )
    assert (
        "evidence_metric.dimensions->> 'session_boundary_fingerprint' = "
        "experiment.split_policy->> 'session_boundary_fingerprint'"
        in sql
    )


def test_daily_evidence_rejects_arbitrary_window_labels_at_the_same_count(
        ) -> None:
    """A matching cardinality cannot substitute unrelated OOS windows."""

    sql = _flat_daily_evidence_sql()

    # Every metric must be claimed as DAILY evidence, there can be no hidden
    # extra label, and the claimed row is joined to the preregistered label.
    assert "from quant.experiment_metrics claimed_daily_metric" in sql
    assert (
        "claimed_daily_metric.dimensions->>'window' <> 'SUMMARY'"
        in sql
    )
    assert (
        "evidence_metric.dimensions->>'window' = "
        "expected_window->>'window'"
        in sql
    )
    assert "count(distinct evidence_metric.experiment_metric_id)" in sql
    # Two rows for expected A plus no row for expected B still has the right
    # total cardinality.  Counting the joined expected labels closes that gap.
    assert sql.count("count(distinct expected_window->>'window')") >= 2


def test_daily_evidence_rejects_swapped_or_substituted_window_bounds() -> None:
    """Labels alone do not identify the test sessions that were evaluated."""

    sql = _flat_daily_evidence_sql()

    assert (
        "evidence_metric.dimensions->>'start_session' = "
        "expected_window->>'test_start'"
        in sql
    )
    assert (
        "evidence_metric.dimensions->>'end_session' = "
        "expected_window->>'test_end'"
        in sql
    )
    assert "count(distinct expected_window->>'window')" in sql


def test_daily_evidence_rejects_missing_or_mixed_evaluation_fingerprints(
        ) -> None:
    """All exact-window rows must share one complete SHA-256 identity."""

    sql = _flat_daily_evidence_sql()
    fingerprints = (
        "evaluation_fingerprint",
        "evaluation_plan_fingerprint",
        "session_boundary_fingerprint",
        "instrument_ids_fingerprint",
        "source_content_fingerprint",
    )
    for fingerprint in fingerprints:
        assert (
            f"count(distinct evidence_metric.dimensions->> "
            f"'{fingerprint}') = 1"
            in sql
        )
        assert (
            f"coalesce(evidence_metric.dimensions->> '{fingerprint}', '') ~ "
            "'^[0-9a-f]{64}$'"
            in sql
        )


def test_daily_reuse_calls_exact_experiment_audit_and_rejects_nonfinite(
        ) -> None:
    """A valid sibling must not authorize a malformed current experiment."""

    sql = governed_stock_evidence_sql(
        experiment_alias="current_experiment", dataset_alias="dataset",
        hypothesis_alias="shared_hypothesis")
    exact_authority = (
        "quant.experiment_has_governed_daily_stock_evidence(\n"
        "        current_experiment.experiment_id)"
    )

    # The authority is bound to the row being reused, never merely to its
    # hypothesis (which may have a different, valid sibling experiment).
    assert sql.count("quant.experiment_has_governed_daily_stock_evidence") == 1
    assert exact_authority in sql
    assert "audit.experiment_has_governed_daily_stock_evidence" not in sql
    assert (
        "audit.hypothesis_has_governed_daily_stock_evidence("
        "shared_hypothesis.hypothesis_id)"
        not in sql
    )
    assert "evidence_metric.value is not null" in sql
    assert (
        "evidence_metric.value::text not in\n"
        "                         ('NaN', 'Infinity', '-Infinity')"
        in sql
    )
    assert "evidence_metric.dimensions->>'asset_class'" in sql
    assert "stock_universe_contract_version" in sql


def test_intraday_reuse_does_not_depend_on_daily_experiment_audit() -> None:
    """The daily-only audit must not reject a valid intraday FULL_60 row."""

    sql = governed_stock_evidence_sql(
        experiment_alias="experiment", dataset_alias="dataset",
        hypothesis_alias="hypothesis")
    intraday_branch, daily_branch = sql.split("\n  or (not ", 1)

    assert "experiment_has_governed_daily_stock_evidence" not in \
        intraday_branch
    assert "experiment_has_governed_daily_stock_evidence" in daily_branch
    assert "full_rung.rung = 'FULL_60'" in intraday_branch


def test_daily_evidence_rejects_missing_or_substituted_source_content_hash(
        ) -> None:
    """The immutable manifest hash, metric identity, and source are one fact."""

    sql = _flat_daily_evidence_sql()

    assert "dataset.content_hash ~ '^[0-9a-f]{64}$'" in sql
    assert (
        "evidence_metric.dimensions->> 'dataset_content_hash' = "
        "dataset.content_hash"
        in sql
    )
    assert (
        "evidence_metric.dimensions->> 'source_content_fingerprint' = "
        "dataset.content_hash"
        in sql
    )

    common = {
        "dataset_id": "00000000-0000-0000-0000-000000000001",
        "universe_version_id": "00000000-0000-0000-0000-000000000002",
        "instrument_ids": ["00000000-0000-0000-0000-000000000003"],
        "windows": [{
            "window": "2026H1",
            "start_session": "2026-01-02",
            "end_session": "2026-06-30",
        }],
        "cost_model_version": "krx-cost-v2",
        "evaluation_scope": "DAILY_WALK_FORWARD",
        "evaluation_plan_fingerprint": "d" * 64,
    }
    for invalid_hash in ("", "a" * 63, "A" * 64, "g" * 64,
                         "sha256:" + "a" * 64):
        with pytest.raises(RuntimeError):
            build_stock_evaluation_identity(
                **common, dataset_content_hash=invalid_hash)


def test_allocator_failure_clears_transaction_before_dispatch_continues(
        monkeypatch) -> None:
    class Connection:
        def __init__(self) -> None:
            self.rollbacks = 0

        def rollback(self) -> None:
            self.rollbacks += 1

    conn = Connection()
    monkeypatch.setattr(
        factory_autopilot, "_widest_price_dataset",
        lambda _conn: ("krx-basket-daily/v3", 2600),
    )
    monkeypatch.setattr(
        allocator, "plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            SyntaxError("generated SQL did not parse")),
    )

    assert factory_autopilot._enact_top_move(conn) == 0
    assert conn.rollbacks == 1


def test_dataset_selector_rejects_a_larger_mixed_universe() -> None:
    sql = governed_stock_dataset_sql(dataset_alias="manifest")

    assert "quant.universe_members" in sql
    assert "quant.current_krx_stock_instrument_identity" in sql
    assert "instrument_type" in sql and "'STOCK'" in sql
    assert "asset_class" in sql and "'EQUITY'" in sql
    assert "market" in sql and "'KRX'" in sql
    assert "status" in sql and "'ACTIVE'" in sql

    source = inspect.getsource(factory_autopilot._widest_price_dataset)
    assert "_GOVERNED_STOCK_DATASET" in source
    assert "m.row_count desc, m.version desc" in source

    resolver_sql = data_resolution._SQL_MANIFESTS
    assert "coalesce(upper(instrument.instrument_type), '') <> 'STOCK'" in resolver_sql
    assert "coalesce(upper(instrument.asset_class), '') <> 'EQUITY'" in resolver_sql
    assert "coalesce(upper(instrument.market), '') <> 'KRX'" in resolver_sql
    assert "coalesce(upper(instrument.status), '') <> 'ACTIVE'" in resolver_sql


def test_planner_prior_memory_uses_the_same_governed_boundary() -> None:
    import proposal_intake

    source = inspect.getsource(proposal_intake.load_past_outcomes)
    assert "_GOVERNED_PAST_OUTCOME" in source
    assert "v_current_experiment_outcomes" in source


def test_trials_stay_in_pressure_while_performance_selection_is_filtered() -> None:
    # Gate 0 trial pressure deliberately has no evidence predicate.  A mixed
    # legacy run still consumed a test and must continue to deflate DSR.
    assert "evaluation_identity_complete" not in factory_bridge._SQL_FAMILY_TRIALS
    assert "evaluation_identity_complete" not in \
        factory_bridge._SQL_FAMILY_OR_EXACT_TRIALS

    sql = objective._SQL_FAMILY_BEST
    pressure, eligible = sql.split("), eligible as (", 1)
    assert "max(trial_number) n_trials" in pressure
    assert "evaluation_identity_complete" not in pressure
    assert "evaluation_identity_complete" in eligible
    assert "join pressure using (fam)" in eligible

    grid_pressure, grid_eligible = grid._SQL.split(
        "), eligible_performance as (", 1
    )
    assert "evaluation_identity_complete" not in grid_pressure
    assert "evaluation_identity_complete" in grid_eligible
    assert "eligible_evidence" in grid_eligible
    assert "if not eligible_evidence" in inspect.getsource(grid.build)


def test_all_directional_outcome_and_allocator_reads_use_common_boundary() -> None:
    evidence_queries = (
        factory_bridge._SQL_FAMILY_OUTCOMES,
        factory_bridge._SQL_EXACT_AST_OUTCOMES,
        objective._SQL_FAMILY_BEST,
        allocator._SQL_PARENT,
        allocator._SQL_VARIANT_EXISTS,
        allocator._SQL_SETTLED_BY_FINGERPRINT,
        orphan_finalizer._SQL_EXPERIMENT,
    )
    for sql in evidence_queries:
        assert "evaluation_identity_complete" in sql
        assert "FULL_60" in sql
        assert "quant.current_krx_stock_instrument_identity" in sql

    plan_source = inspect.getsource(allocator.plan)
    assert "st.evidence_experiment_id" in plan_source
    assert "order by trial_number desc limit 1" not in plan_source

    finalizer_source = inspect.getsource(orphan_finalizer.finalize_one)
    assert "if not governed_evidence" in finalizer_source
    assert 'decision="GATE_HOLD"' in finalizer_source
    assert '"INELIGIBLE_EVIDENCE"' in finalizer_source


class _CaptureCursor:
    def __init__(self, batches=None) -> None:
        self.sql: list[str] = []
        self.batches = list(batches or [])

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, _params=()) -> None:
        self.sql.append(str(sql))

    def fetchall(self):
        return self.batches.pop(0) if self.batches else []


class _CaptureConnection:
    def __init__(self, batches=None) -> None:
        self.cursor_instance = _CaptureCursor(batches)
        self.rollbacks = 0

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollbacks += 1


def test_autopilot_memory_queries_exclude_legacy_mixed_evidence() -> None:
    experiment_id = "00000000-0000-0000-0000-000000000001"
    near = _CaptureConnection(batches=[
        [("family", "관문 2/8 통과", [], experiment_id)],
        [(experiment_id,)],
    ])
    assert factory_autopilot._near_miss(near) == ""
    near_evidence = [sql for sql in near.cursor_instance.sql
                     if "from quant.experiments e" in sql]
    assert len(near_evidence) == 1
    assert "evaluation_identity_complete" in near_evidence[0]
    assert any("statement_timeout" in sql for sql in near.cursor_instance.sql)
    assert any("rollback to savepoint factory_near_miss_budget" in sql
               for sql in near.cursor_instance.sql)
    assert not any("statement_timeout = 0" in sql
                   for sql in near.cursor_instance.sql)

    empty = _CaptureConnection()
    assert factory_autopilot._near_miss(empty) == ""
    assert not any("from quant.experiments e" in sql
                   for sql in empty.cursor_instance.sql)

    class _TimeoutCursor(_CaptureCursor):
        def execute(self, sql, params=()) -> None:
            super().execute(sql, params)
            if "from quant.experiments e" in str(sql):
                raise RuntimeError("statement timeout")

    timed_out = _CaptureConnection()
    timed_out.cursor_instance = _TimeoutCursor(batches=[
        [("family", "관문 4/8 통과", [], experiment_id)],
    ])
    assert factory_autopilot._near_miss(timed_out) == ""
    assert any("rollback to savepoint factory_near_miss_budget" in sql
               for sql in timed_out.cursor_instance.sql)
    assert any("release savepoint factory_near_miss_budget" in sql
               for sql in timed_out.cursor_instance.sql)
    assert timed_out.rollbacks == 0

    pareto = _CaptureConnection()
    assert factory_autopilot._pareto_line(pareto) == ""
    assert len(pareto.cursor_instance.sql) == 2
    assert all("evaluation_identity_complete" in sql
               for sql in pareto.cursor_instance.sql)

    memory = _CaptureConnection()
    factory_autopilot._ast_experience_block(memory)
    performance_reads = [
        sql for sql in memory.cursor_instance.sql
        if "from quant.experiments e" in sql
    ]
    assert len(performance_reads) == 3
    assert all("evaluation_identity_complete" in sql
               for sql in performance_reads)


def test_brief_progress_and_bottleneck_feedback_are_stock_governed() -> None:
    predicates = (
        cycle_brief._GOVERNED_LESSON_EVIDENCE,
        progress._GOVERNED_PROGRESS_EVIDENCE,
        bottleneck_census._GOVERNED_CENSUS_EVIDENCE,
    )
    assert all("evaluation_identity_complete" in sql for sql in predicates)
    assert all("FULL_60" in sql for sql in predicates)
    assert "quant.current_krx_stock_instrument_identity" in \
        bottleneck_census._GOVERNED_CENSUS_DATASET

    for function in (
        cycle_brief.load_lessons,
        progress.measure,
        bottleneck_census._gate_blockers,
        bottleneck_census._repeat_lessons,
    ):
        source = inspect.getsource(function)
        assert "_GOVERNED_" in source
        assert "join quant.experiments" in source

    assert "_GOVERNED_CENSUS_DATASET" in inspect.getsource(
        bottleneck_census._unused_datasets
    )
    assert "_GOVERNED_CENSUS_DATASET" in inspect.getsource(
        bottleneck_census._feedback_gap
    )


def test_hermes_library_tools_hide_legacy_mixed_performance() -> None:
    queries = (
        research_mcp_server._SQL_FACTORY_OUTCOMES,
        research_mcp_server._SQL_LIBRARY_SIGNAL_SHELF,
        research_mcp_server._SQL_LIBRARY_FAMILIES,
        research_mcp_server._SQL_LIBRARY_SCORECARD,
    )
    for sql in queries:
        assert "evaluation_identity_complete" in sql
        assert "FULL_60" in sql
        assert "join quant.experiments" in sql
        assert "join quant.dataset_manifests" in sql
