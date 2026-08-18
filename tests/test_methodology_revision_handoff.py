from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys
import math

import pytest
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
RESEARCH = ROOT / "departments" / "01-research"
for path in (RESEARCH / "contracts", RESEARCH / "factory"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from factory_contracts import (  # noqa: E402
    MethodologyLeadV1,
    lead_id_for,
    lead_revision_id,
)
import factory_autopilot  # noqa: E402
import proposal_intake  # noqa: E402


NOW = datetime(2026, 8, 18, 21, 0, tzinfo=timezone.utc)
REFS = [{
    "url": "https://example.test/microstructure-source",
    "title": "Microstructure source",
    "accessed_at": NOW,
    "excerpt": "Signed order flow may reveal urgent liquidity demand.",
}]
BASE_ID = lead_id_for(REFS)
COLS = (
    "lead_id", "case_id", "scout_lens", "source_type", "as_known_at",
    "refs", "ast_contract", "claimed_edge", "stated_mechanism", "inferred",
    "market_context", "stated_failure_mode", "independent_mentions",
    "testability", "status", "model_version", "prompt_version",
)


def _contract(window: int) -> dict:
    return {
        "ast_readiness": "AST_READY",
        "research_lane": "INTRADAY_EVENT",
        "primary_data_plane": "MICROSTRUCTURE",
        "formula_discovery_version": "formula-discovery-v5",
        "feature_window_contract_version":
            proposal_intake.CURRENT_INTRADAY_FEATURE_WINDOW_CONTRACT,
        "formula_contract_complete": True,
        "alpha_candidate_eligible": True,
        "candidate_signal_expr": {
            "op": "field", "field": "trade_flow_imbalance",
            "seconds": window,
        },
    }


def _row(lead_id: str, contract: dict) -> tuple:
    return (
        lead_id, "revision-handoff", "ACADEMIC", "PAPER", NOW, REFS,
        contract, "signed flow", "urgent takers consume liquidity", True,
        "KRX stocks", "cost hurdle", 1, "RULE_EXPRESSIBLE", "COMPLETE",
        "hermes-test", "planner-revision-test",
    )


class _Cursor:
    def __init__(self, rows: dict[str, tuple], revision_id: str):
        self.rows = rows
        self.revision_id = revision_id
        self.result: list[tuple] = []
        self.sql: list[str] = []

    def execute(self, sql: str, params: tuple) -> None:
        self.sql.append(sql)
        if "select l.lead_id" in sql:
            self.result = [(self.revision_id,)]
            return
        requested = params[0]
        self.result = [self.rows[value] for value in requested
                       if value in self.rows]

    def fetchall(self) -> list[tuple]:
        return list(self.result)


class _Connection:
    def __init__(self, rows: dict[str, tuple], revision_id: str):
        self.cur = _Cursor(rows, revision_id)

    def cursor(self) -> _Cursor:
        return self.cur


def test_revision_identity_is_source_plus_exact_ast_contract() -> None:
    contract = _contract(30)
    revision_id = lead_revision_id(BASE_ID, contract)
    payload = dict(zip(COLS, _row(revision_id, contract)))

    lead = MethodologyLeadV1.model_validate(payload)

    assert lead.lead_id == revision_id
    with pytest.raises(ValidationError, match="출처와 맞지 않는다"):
        MethodologyLeadV1.model_validate({
            **payload,
            "lead_id": f"{BASE_ID}_r000000000000",
        })


@pytest.mark.parametrize("non_finite", [math.nan, math.inf, -math.inf])
def test_revision_identity_rejects_non_finite_json_numbers(
        non_finite: float) -> None:
    contract = {**_contract(30), "non_finite": non_finite}

    with pytest.raises(ValueError, match="Out of range float values"):
        lead_revision_id(BASE_ID, contract)


def test_planner_cohort_can_reload_a_persisted_revision_lead() -> None:
    base_contract = _contract(5)
    revision_contract = _contract(30)
    revision_id = lead_revision_id(BASE_ID, revision_contract)
    conn = _Connection({
        BASE_ID: _row(BASE_ID, base_contract),
        revision_id: _row(revision_id, revision_contract),
    }, revision_id)

    loaded = proposal_intake.load_leads(conn, [BASE_ID])

    assert set(loaded) == {BASE_ID, revision_id}
    assert loaded[revision_id].ast_contract["candidate_signal_expr"][
        "seconds"] == 30
    expansion_sql = next(sql for sql in conn.cur.sql
                         if "select l.lead_id" in sql)
    assert "primary_data_plane' = 'MICROSTRUCTURE'" in expansion_sql


def test_non_microstructure_primary_cannot_expand_the_current_cohort() -> None:
    non_micro = {**_contract(5), "primary_data_plane": "DAILY_BARS"}
    revision_contract = _contract(30)
    revision_id = lead_revision_id(BASE_ID, revision_contract)
    conn = _Connection({
        BASE_ID: _row(BASE_ID, non_micro),
        revision_id: _row(revision_id, revision_contract),
    }, revision_id)

    loaded = proposal_intake.load_leads(conn, [BASE_ID])

    assert set(loaded) == {BASE_ID}
    assert not any("select l.lead_id" in sql for sql in conn.cur.sql)


def test_breeder_board_read_failure_defers_planner(monkeypatch) -> None:
    def broken_board(*_args, **_kwargs):
        raise RuntimeError("board unavailable")

    monkeypatch.setattr(factory_autopilot, "_board_rows", broken_board)

    state = factory_autopilot._active_card_by_key_prefix(
        "factory-formula-breeder-", fail_closed=True)

    assert state == "UNKNOWN_BOARD_STATE(RuntimeError)"
    assert not factory_autopilot._should_schedule_planner(
        "fresh brief", 3, 2, breeder_pending=bool(state))


def test_planner_version_bump_does_not_bypass_active_family(
        monkeypatch) -> None:
    monkeypatch.setattr(
        factory_autopilot,
        "_board_rows",
        lambda *_args, **_kwargs: [
            ("t_old", "running", "factory-planner-v8-20260818T21a"),
        ],
    )
    called = []
    monkeypatch.setattr(
        factory_autopilot.subprocess,
        "run",
        lambda *_args, **_kwargs: called.append(True),
    )

    created = factory_autopilot._create_card(
        title="new planner", body="body",
        assignee=factory_autopilot.RESEARCH_ASSIGNEE,
        key="factory-planner-v9-20260818T21a", dry_run=False, priority=1,
        active_family_prefix="factory-planner-v",
    )

    assert created is None
    assert called == []


def test_concurrent_scheduler_cycle_is_fail_closed(monkeypatch) -> None:
    class Cursor:
        def __init__(self, acquired=False):
            self.acquired = acquired

        def execute(self, _sql, _params):
            return None

        def fetchone(self):
            return (self.acquired,)

    class Connection:
        def __init__(self, acquired=False):
            self.acquired = acquired
            self.closed = False
            self.commits = 0
            self.rollbacks = 0

        def cursor(self):
            return Cursor(self.acquired)

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    conn = Connection()
    monkeypatch.setattr(factory_autopilot, "_conn", lambda: conn)
    called = []

    @factory_autopilot._serialized_factory_cycle
    def candidate(*, dry_run=False):
        called.append(dry_run)
        return 7

    assert candidate(dry_run=False) == 0
    assert called == []
    assert conn.closed is True
    assert conn.commits == 0
    assert conn.rollbacks == 1


def test_scheduler_cycle_lock_commit_rollback_and_dry_run(monkeypatch) -> None:
    class Cursor:
        def execute(self, _sql, _params):
            return None

        def fetchone(self):
            return (True,)

    class Connection:
        def __init__(self):
            self.commits = 0
            self.rollbacks = 0
            self.closed = False

        def cursor(self):
            return Cursor()

        def commit(self):
            self.commits += 1

        def rollback(self):
            self.rollbacks += 1

        def close(self):
            self.closed = True

    connections = []

    def connect():
        conn = Connection()
        connections.append(conn)
        return conn

    monkeypatch.setattr(factory_autopilot, "_conn", connect)

    @factory_autopilot._serialized_factory_cycle
    def succeeds(*, dry_run=False):
        return 7 if not dry_run else 3

    @factory_autopilot._serialized_factory_cycle
    def fails(*, dry_run=False):
        raise RuntimeError("cycle failed")

    assert succeeds(dry_run=False) == 7
    assert (connections[0].commits, connections[0].rollbacks,
            connections[0].closed) == (1, 0, True)
    with pytest.raises(RuntimeError, match="cycle failed"):
        fails(dry_run=False)
    assert (connections[1].commits, connections[1].rollbacks,
            connections[1].closed) == (0, 1, True)
    assert succeeds(dry_run=True) == 3
    assert len(connections) == 2
