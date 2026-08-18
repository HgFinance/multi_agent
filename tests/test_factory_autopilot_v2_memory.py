"""Focused guards for V2 factory memory and queue recovery boundaries."""

from __future__ import annotations

import inspect
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
for path in (FACTORY, PIPELINE):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

import factory_autopilot as autopilot  # noqa: E402


def test_current_v2_memory_excludes_legacy_execution_evidence() -> None:
    evidence = autopilot._CURRENT_V2_INTRADAY_MEMORY_EVIDENCE
    assert autopilot.CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT in evidence
    assert "intraday_candidate_lineages current_lineage" in evidence
    assert autopilot.CURRENT_INTRADAY_EVALUATOR_VERSION in evidence

    source = inspect.getsource(autopilot._ast_experience_block)
    # Primary outcomes and screening metrics both cross the same exact gate.
    assert source.count("_CURRENT_V2_INTRADAY_MEMORY_EVIDENCE") == 2
    assert "not (e.config ? 'intraday_signal_expr')" in source
    assert "current_candidate->>" in source
    assert "feature_window_contract_version' is distinct from" in source
    # Lead and review memory retain daily evidence while excluding legacy
    # intraday rows from the current Scout/Planner prompt.
    assert "reviewed_lead.ast_contract->>'research_lane'" in source
    assert "explicit-primitive-window-v2" in source


def test_same_ast_retirement_requires_matching_execution_identity() -> None:
    sql = autopilot._LIVE_INTRADAY_EVOLUTION_PARENTS_SQL
    assert autopilot.LEGACY_INTRADAY_FEATURE_WINDOW_CONTRACT in sql
    assert autopilot.CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT in sql
    assert "primary_lineage.evaluator_version" in sql
    assert autopilot.LEGACY_INTRADAY_EVALUATOR_VERSION in sql
    assert autopilot.CURRENT_INTRADAY_EVALUATOR_VERSION in sql
    assert ("e.config->>'feature_window_contract_version'" in sql
            and "l.ast_contract->>'feature_window_contract_version'" in sql)
    assert " = coalesce(" in sql


def test_active_discovery_card_requires_fresh_queue_or_heartbeat(
        monkeypatch) -> None:
    captured = {}

    def rows(sql, params=()):
        captured["sql"] = sql
        captured["params"] = params
        return [("t_live", "running", "factory-scout-v9-x")]

    monkeypatch.setattr(autopilot, "_board_rows", rows)
    assert autopilot._active_card_by_key_prefix("factory-scout-") == \
        "t_live(running)"
    assert "last_heartbeat_at" in captured["sql"]
    assert "strftime('%s','now')" in captured["sql"]
    assert captured["params"] == (
        "factory-scout-%", autopilot.ACTIVE_DISCOVERY_CARD_TTL_SECONDS)


def test_failed_health_is_not_treated_as_a_healthy_queue() -> None:
    class Broken:
        def __init__(self):
            self.rollbacks = 0

        def cursor(self):
            raise RuntimeError("read failed")

        def rollback(self):
            self.rollbacks += 1

    conn = Broken()
    health = autopilot._lead_health(conn)
    assert "QUEUE HEALTH UNKNOWN - FAIL CLOSED" in health
    assert "Scout recovery card" in health
    assert conn.rollbacks == 1

    assert autopilot._safe_queue_measurement(
        conn, lambda _conn: (_ for _ in ()).throw(RuntimeError("boom")),
        label="test") is None
    assert conn.rollbacks == 2


def test_empty_v2_queue_schedules_scout_even_with_migration_parent() -> None:
    assert autopilot._should_schedule_formula_breeder("starving", 0, 1)
    assert autopilot._should_schedule_scout("starving", True, 0)
    assert autopilot._should_schedule_scout("starving", True, None)
    assert not autopilot._should_schedule_scout("starving", True, 1)
    assert autopilot._should_schedule_scout("starving", False, 1)


def test_superseded_proposed_rows_get_typed_terminal_audit_job() -> None:
    class Cursor:
        def __init__(self, owner):
            self.owner = owner

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, params=()):
            self.owner.calls.append((sql, params))

        def fetchone(self):
            return (2,)

    class Conn:
        def __init__(self):
            self.calls = []
            self.commits = 0

        def cursor(self):
            return Cursor(self)

        def commit(self):
            self.commits += 1

    conn = Conn()
    assert autopilot._retire_superseded_intraday_hypotheses(conn) == 2
    sql, params = conn.calls[0]
    assert "update quant.hypotheses h" in sql
    assert "status = 'REJECTED'" in sql
    assert "status_changed_at = now()" in sql
    assert "active_job.status in ('QUEUED','LEASED')" in sql
    assert "insert into quant.experiment_jobs" in sql
    assert "'CANCELLED'" in sql
    assert autopilot.SUPERSEDED_INTRADAY_CONTRACT_REASON in params
    assert conn.commits == 1

    preview = Conn()
    assert autopilot._retire_superseded_intraday_hypotheses(
        preview, dry_run=True) == 2
    assert "select count(*)" in preview.calls[0][0]
    assert preview.commits == 0
