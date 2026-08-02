from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from p1.analytics import (
    InstrumentMapping,
    KillSwitchState,
    MarketPoint,
    PortfolioPosition,
    RiskP1Engine,
    RiskP1Error,
    evaluate_p1_gate,
)
from p1.ls_adapter import collect_ls_inputs
from p1.repository import RiskP1PersistenceError, RiskP1Repository


def _engine() -> RiskP1Engine:
    return RiskP1Engine([InstrumentMapping("AAPL", uuid4()), InstrumentMapping("MSFT", uuid4())])


def _snapshot(**kwargs):
    now = datetime.now(timezone.utc)
    return _engine().build_snapshot(
        fund_id=uuid4(),
        book_id=uuid4(),
        strategy_version_id=None,
        as_of=now,
        equity=5000,
        positions=(PortfolioPosition("AAPL", 10), PortfolioPosition("MSFT", 5)),
        market=(
            MarketPoint("AAPL", 100, now, (0.01, -0.02, 0.03, -0.01)),
            MarketPoint("MSFT", 200, now, (0.02, -0.01, 0.01, -0.02)),
        ),
        stress_scenarios={"equity_down": {"AAPL": -0.20, "MSFT": -0.20}},
        **kwargs,
    )


def test_snapshot_maps_instruments_and_is_gate_ready() -> None:
    snapshot = _snapshot()
    assert snapshot.quality_status == "PASS"
    assert snapshot.gross_exposure > 0
    assert snapshot.value_at_risk is not None
    assert snapshot.correlation_shock_loss is not None
    assert evaluate_p1_gate(snapshot).value == "PASS"


def test_missing_mapping_and_stale_market_fail_closed() -> None:
    now = datetime.now(timezone.utc)
    with pytest.raises(RiskP1Error, match="mapping"):
        RiskP1Engine([InstrumentMapping("MSFT", uuid4())]).build_snapshot(
            fund_id=uuid4(), book_id=None, strategy_version_id=None, as_of=now,
            equity=1000, positions=(PortfolioPosition("AAPL", 1),),
            market=(MarketPoint("AAPL", 100, now),), stress_scenarios={},
        )
    with pytest.raises(RiskP1Error, match="stale"):
        _engine().build_snapshot(
            fund_id=uuid4(), book_id=None, strategy_version_id=None, as_of=now,
            equity=1000, positions=(PortfolioPosition("AAPL", 1), PortfolioPosition("MSFT", 1)),
            market=(
                MarketPoint("AAPL", 100, now - timedelta(minutes=6)),
                MarketPoint("MSFT", 100, now),
            ), stress_scenarios={},
        )


def test_kill_switch_blocks_entry_even_with_good_analytics() -> None:
    snapshot = _snapshot(kill_switch_state=KillSwitchState.ENTRY_BLOCKED)
    assert evaluate_p1_gate(snapshot).value == "REJECT"


class _Cursor:
    def __init__(self, *, fail_on_stress: bool = False):
        self.fail_on_stress = fail_on_stress
        self.calls = []

    def execute(self, query, params):
        self.calls.append((query, params))
        if self.fail_on_stress and "risk.stress_results" in query:
            raise RuntimeError("fk")

    def fetchone(self):
        return (uuid4(),)

    def close(self):
        return None


class _Connection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return self._cursor

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_repository_is_atomic_and_requires_governed_stress_scenario() -> None:
    snapshot = _snapshot()
    trace_id = uuid4()
    cursor = _Cursor()
    connection = _Connection(cursor)
    stored_id = RiskP1Repository(connection).save_snapshot(
        snapshot, stress_scenario_ids={"equity_down": uuid4()}, trace_id=trace_id
    )
    assert stored_id
    assert connection.commits == 1
    assert connection.rollbacks == 0

    failing_cursor = _Cursor()
    failing_connection = _Connection(failing_cursor)
    try:
        RiskP1Repository(failing_connection).save_snapshot(
            snapshot, stress_scenario_ids={}, trace_id=trace_id
        )
    except RiskP1PersistenceError:
        pass
    else:
        raise AssertionError("missing scenario must fail closed")
    assert failing_connection.commits == 0
    assert failing_connection.rollbacks == 1


def test_repository_persists_kill_switch_requester_not_release_approver() -> None:
    snapshot = _snapshot()
    cursor = _Cursor()
    connection = _Connection(cursor)

    RiskP1Repository(connection).save_snapshot(
        snapshot,
        stress_scenario_ids={"equity_down": uuid4()},
        trace_id=uuid4(),
        kill_switch_transition={
            "from_state": "ENABLED",
            "to_state": "ENTRY_BLOCKED",
            "trigger_type": "stress_breach",
            "trigger_details": {"loss": 100.0},
            "evidence": {"source": "test"},
            "requested_by": "risk-supervisor",
            "approved_release_by": "qa-audit-supervisor",
        },
    )

    kill_switch_calls = [
        params for query, params in cursor.calls if "risk.kill_switch_events" in query
    ]
    assert len(kill_switch_calls) == 1
    assert kill_switch_calls[0][6] == "risk-supervisor"


class _Quote:
    def __init__(self, symbol):
        self.symbol = symbol
        self.price = 100
        self.observed_at = datetime.now(timezone.utc)


class _Portfolio:
    equity = 1000
    positions = ({"shcode": "AAPL", "janqty": "2"},)


class _LSClient:
    def get_portfolio_snapshot(self):
        return _Portfolio()

    def get_quote(self, symbol):
        return _Quote(symbol)


def test_ls_adapter_requires_explicit_canonical_mapping() -> None:
    mapping = InstrumentMapping("AAPL", uuid4())
    inputs = collect_ls_inputs(_LSClient(), mappings=(mapping,), returns_by_symbol={"AAPL": (0.01, -0.01)})
    assert inputs.equity == 1000
    assert inputs.positions[0].instrument_id == mapping.instrument_id
    with pytest.raises(RiskP1Error, match="mapping"):
        collect_ls_inputs(_LSClient(), mappings=(InstrumentMapping("MSFT", uuid4()),))
