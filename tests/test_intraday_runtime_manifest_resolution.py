from datetime import date
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
FACTORY = ROOT / "departments" / "01-research" / "factory"
sys.path.insert(0, str(PIPELINE))

import data_resolution as dr  # noqa: E402


LIVE_ROW = (
    "krx-intraday-events",
    "v1",
    {
        "market_quotes": "ls-realtime-book-v1",
        "market_ticks": "ls-realtime-trade-v1",
    },
)


class _MetaCursor:
    def __init__(self, conn):
        self.conn = conn
        self.rows = []

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)
        self.rows = ([LIVE_ROW]
                     if "LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT" in sql
                     else [])

    def fetchall(self):
        return list(self.rows)


class _MetaConnection:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return _MetaCursor(self)


class _MarketCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        assert "timescaledb_information.chunks" in sql
        self.row = (date(2026, 5, 18), date(2026, 8, 15), 61)

    def fetchone(self):
        return self.row


class _MarketConnection:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return _MarketCursor(self)


def test_live_manifest_is_not_visible_to_generic_or_daily_resolution() -> None:
    meta = _MetaConnection()
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"],
         "min_history_days": 10},
        meta_conn=meta,
        market_conn=_MarketConnection(),
    )
    assert result.verdict == dr.UNMAPPED_SOURCE
    assert "LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT" not in meta.sql[0]


def test_intraday_lane_accepts_only_typed_runtime_source_then_defers_exact_scope() -> None:
    meta = _MetaConnection()
    market = _MarketConnection()
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"],
         "min_history_days": 10},
        meta_conn=meta,
        market_conn=market,
        research_lane="INTRADAY_EVENT",
    )
    assert result.ok
    assert result.datasets == ("krx-intraday-events/v1",)
    assert set(result.coverage) == {"market_quotes", "market_ticks"}
    assert all(value.measurement == "CHUNK_RANGE"
               for value in result.coverage.values())
    assert any("LIVE_RUNTIME_SOURCE_ONLY" in note for note in result.notes)
    assert len(market.sql) == 2


def test_runtime_exception_is_exact_and_cannot_authorize_an_arbitrary_null_manifest() -> None:
    sql = dr._SQL_LIVE_INTRADAY_MANIFEST
    required = (
        "manifest.name = 'krx-intraday-events'",
        "manifest.version = 'v1'",
        "manifest.universe_version_id is null",
        "manifest.row_count is null",
        "manifest.partitions = '[]'::jsonb",
        "market_quotes",
        "market_ticks",
        "LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT",
        "available_at=max(received_at,observed_at)",
        "event_time<=decision_time and available_at<=decision_time",
        "entry_time+horizon",
        "instrument_isolation",
        "received_at",
        "observed_at",
        "instrument_id",
    )
    for value in required:
        assert value in sql
    assert "union" not in dr._SQL_MANIFESTS.lower()
    assert "manifest.name = 'krx-intraday-events'" in dr._SQL_MANIFESTS
    assert "not (" in dr._SQL_MANIFESTS
    assert "union" in dr._SQL_INTRADAY_MANIFESTS.lower()


def test_autopilot_and_orchestrator_pass_the_same_lane_to_resolution() -> None:
    autopilot = (FACTORY / "factory_autopilot.py").read_text(encoding="utf-8")
    orchestrator = (PIPELINE / "experiment_orchestrator.py").read_text(
        encoding="utf-8")
    assert 'manifest_index(\n            conn, research_lane="INTRADAY_EVENT")' in autopilot
    assert "lane == \"INTRADAY_EVENT\"" in autopilot
    assert "research_lane=str((hyp.get(\"expected_edge\") or {}).get(" in orchestrator
