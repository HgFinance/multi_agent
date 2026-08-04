from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "api"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.risk_engine import TradingState  # noqa: E402
from risk_context_repository import PostgresRiskContextRepository  # noqa: E402


class _Cursor:
    def __init__(self) -> None:
        self.result = None
        self.rows = []
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, _params=()):
        self.queries.append(" ".join(query.split()))
        if "current_setting('transaction_read_only')" in query:
            self.result = ("on",)
        elif "FROM risk.policies p" in query and "SELECT p.scope" in query:
            self.result = (
                {},
                {
                    "allowed_instrument_ids": [str(INSTRUMENT_ID)],
                    "min_order_notional": "100",
                    "max_order_notional": "1000000",
                },
            )
        elif "SELECT l.metric" in query:
            self.rows = [
                ("single_issuer_pct", "0.10", "0.20"),
                ("daily_turnover_notional", None, "1000000"),
                ("daily_order_count", None, "50"),
                ("daily_loss", None, "10000"),
                ("drawdown_pct", None, "0.20"),
            ]
        elif "FROM risk.restricted_items" in query:
            self.rows = []
        elif "FROM accounting.portfolio_snapshots" in query:
            self.result = ("1000", "10000")
        elif "FROM accounting.positions" in query:
            self.rows = [(INSTRUMENT_ID, "2", "0", str(ISSUER_ID))]
        elif "FROM accounting.cash_balances" in query:
            self.result = ("5000",)
        elif "FROM accounting.pnl_snapshots" in query:
            self.result = ("25",)
        elif "FROM accounting.valuations" in query:
            self.rows = [(str(ISSUER_ID), "2000")]
        elif "FROM execution.market_snapshots" in query:
            self.result = ("PASS", "100")
        elif "FROM risk.counterparties" in query:
            self.result = ("ACTIVE", {"status": "OK"})
        else:
            self.result = None

    def fetchone(self):
        result = self.result
        self.result = None
        return result

    def fetchall(self):
        rows = self.rows
        self.rows = []
        return rows


class _Connection:
    def __init__(self):
        self.cursor_instance = _Cursor()
        self.autocommit = None
        self.rolled_back = False

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


FUND_ID = uuid4()
BOOK_ID = uuid4()
INSTRUMENT_ID = uuid4()
ISSUER_ID = uuid4()


def test_repository_loads_complete_pit_context_read_only():
    connection = _Connection()
    repository = PostgresRiskContextRepository(lambda: connection)
    context = repository.load(
        fund_id=FUND_ID,
        book_id=BOOK_ID,
        instrument_id=INSTRUMENT_ID,
        broker_adapter="paper",
        as_of=datetime(2026, 8, 4, tzinfo=timezone.utc),
        trading_state=TradingState.ENABLED,
    )

    assert context.mandate.fund_id == FUND_ID
    assert context.portfolio.cash == Decimal("5000")
    assert context.portfolio.issuer_exposure[str(ISSUER_ID)] == Decimal("2000")
    assert context.market_status.tradable is True
    assert context.counterparty.health.name == "OK"
    assert connection.rolled_back is True
    assert all("INSERT" not in query.upper() and "UPDATE" not in query.upper() for query in connection.cursor_instance.queries)
