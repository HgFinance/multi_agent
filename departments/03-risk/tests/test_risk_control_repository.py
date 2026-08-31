from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

RISK = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK))

import risk_control_repository as repository_module
from mandate_limit_compiler import compile_mandate_limits
from risk_control_repository import RiskControlRepository


class _Cursor:
    def __init__(self) -> None:
        self.params: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, _query, params=()) -> None:
        self.params.append(tuple(params))

    def fetchone(self):
        return None


class _Connection:
    def __init__(self) -> None:
        self.cursor_value = _Cursor()
        self.committed = False

    def cursor(self):
        return self.cursor_value

    def commit(self) -> None:
        self.committed = True

    def rollback(self) -> None:
        raise AssertionError("activation should not roll back")


class _Pool:
    def __init__(self) -> None:
        self.connection = _Connection()

    def getconn(self):
        return self.connection

    def putconn(self, _connection) -> None:
        pass


class _ExhaustedPool:
    def getconn(self):
        raise RuntimeError("connection pool exhausted")


def test_activate_compilation_passes_uuid_columns_as_driver_safe_strings(monkeypatch) -> None:
    monkeypatch.setattr(repository_module, "_driver", lambda: (lambda value: value, object))
    compilation = compile_mandate_limits(
        {
            "fund_id": str(uuid4()),
            "mandate_id": str(uuid4()),
            "mandate_version_id": str(uuid4()),
            "mandate_version": 1,
            "mandate_status": "ACTIVE",
            "approval_status": "APPROVED",
            "mindset": "BALANCED",
            "experience": "EXPERIENCED",
            "limits": {
                "base_capital": "500000000",
                "max_instrument_weight": "0.15",
                "max_sector_weight": "0.35",
                "max_gross_exposure": "1.5",
                "max_concurrent_positions": 8,
                "max_daily_loss_pct": "0.03",
                "max_drawdown_pct": "0.20",
                "trade_risk_budget_pct": "0.01",
            },
            "effective_from": datetime.now(timezone.utc).isoformat(),
            "trace_id": "test-risk-control-uuid-adaptation",
        }
    )
    pool = _Pool()

    policy_id = RiskControlRepository(pool).activate_compilation(compilation)

    assert policy_id == compilation.policy_id
    assert pool.connection.committed is True
    assert not any(
        isinstance(value, UUID)
        for params in pool.connection.cursor_value.params
        for value in params
    )


def test_pool_exhaustion_is_normalized_to_control_persistence_error() -> None:
    repository = RiskControlRepository(_ExhaustedPool())

    try:
        repository.runtime_observability()
    except repository_module.RiskControlPersistenceError as exc:
        assert str(exc) == "Risk control connection pool is unavailable"
    else:
        raise AssertionError("pool exhaustion must fail closed")
