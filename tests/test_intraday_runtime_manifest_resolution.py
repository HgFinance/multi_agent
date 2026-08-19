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
    True,
)

EXTERNAL_ROW = (
    "krx-intraday-completed-second",
    "v1",
    {
        "market_quotes": "trading-bot-completed-second-book-v1",
        "market_ticks": "trading-bot-completed-second-trade-v1",
    },
    True,
)

DAILY_AGGREGATE_ROW = (
    "krx-microstructure-daily",
    "v1",
    {"microstructure_features": "ms-daily-v5"},
)

ORDINARY_RAW_KEY_ROW = (
    "ordinary-stock-panel",
    "v9",
    {
        "market_quotes": "ls-realtime-book-v1",
        "market_ticks": "ls-realtime-trade-v1",
    },
    False,
)

MALFORMED_TYPED_RAW_ROW = (
    "krx-intraday-events",
    "v1",
    {
        "market_quotes": "daily-book-aggregate-v1",
        "market_ticks": "daily-trade-aggregate-v1",
    },
    True,
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


class _ExternalMarketCursor:
    def __init__(self, conn):
        self.conn = conn
        self.row = None

    def execute(self, sql, params=None):
        self.conn.sql.append((sql, params))
        if "to_regclass" in sql:
            self.row = (True,)
        elif "values->>'n_quotes'" in sql:
            self.row = (1_000_000, date(2026, 5, 18),
                        date(2026, 8, 15), 61, 2_500)
        elif "values->>'n_ticks'" in sql:
            self.row = (500_000, date(2026, 5, 18),
                        date(2026, 8, 15), 61, 2_500)
        else:
            raise AssertionError(sql)

    def fetchone(self):
        return self.row


class _ExternalMarketConnection:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return _ExternalMarketCursor(self)


class _DailyOnlyMetaCursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=None):
        self.conn.sql.append(sql)

    def fetchall(self):
        return [DAILY_AGGREGATE_ROW]


class _DailyOnlyMetaConnection:
    def __init__(self):
        self.sql = []

    def cursor(self):
        return _DailyOnlyMetaCursor(self)


class _NoMarketRead:
    def cursor(self):
        raise AssertionError("daily aggregate must fail before market coverage")


class _RowsMetaCursor:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, sql, params=None):
        self.sql = sql

    def fetchall(self):
        return list(self.rows)


class _RowsMetaConnection:
    def __init__(self, rows):
        self.rows = rows

    def cursor(self):
        return _RowsMetaCursor(self.rows)


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
    assert all(value.granularity == "RAW_EVENT"
               for value in result.coverage.values())
    assert any("RAW_EVENT LIVE CHUNK_RANGE" in note for note in result.notes)
    assert any("LIVE_RUNTIME_SOURCE_ONLY" in note for note in result.notes)
    assert len(market.sql) == 2


def test_intraday_auto_resolves_external_manifest_once_and_freezes_clock() -> None:
    market = _ExternalMarketConnection()
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"],
         "min_history_days": 60},
        meta_conn=_RowsMetaConnection([LIVE_ROW, EXTERNAL_ROW]),
        market_conn=market,
        research_lane="INTRADAY_EVENT",
        requested_event_source="AUTO",
    )

    assert result.ok
    assert result.datasets == (dr.EXTERNAL_INTRADAY_DATASET,)
    assert result.execution_contract == {
        "dataset": dr.EXTERNAL_INTRADAY_DATASET,
        "event_source": dr.EXTERNAL_EVENT_SOURCE,
        "timestamp_policy": dr.COMPLETED_SECOND_POLICY,
        "physical_sources": {
            "market_quotes": "ext_src.quotes",
            "market_ticks": "ext_src.ticks",
        },
        "source_versions": EXTERNAL_ROW[2],
        "knowledge_clock": "EVENT_TIME_ONLY_NO_RECEIPT_CLOCK",
        "evidence_scope": "HISTORICAL_SEARCH_ONLY",
        "content_window": {
            "timezone": "Asia/Seoul", "start": "09:00:00",
            "end_exclusive": "15:30:00",
        },
        "maximum_horizon_seconds": 600,
        "execution": "TAKER_ONLY",
    }
    assert all(value.measurement == "EXTERNAL_FEATURE_LEDGER"
               for value in result.coverage.values())
    assert all(value.history_days == 61
               for value in result.coverage.values())
    assert all(value.granularity == "COMPLETED_SECOND_RAW_EVENT"
               for value in result.coverage.values())
    assert not any("timescaledb_information.chunks" in sql
                   for sql, _params in market.sql)


def test_explicit_local_authority_never_switches_to_external_tables() -> None:
    market = _MarketConnection()
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"]},
        meta_conn=_RowsMetaConnection([LIVE_ROW, EXTERNAL_ROW]),
        market_conn=market,
        research_lane="INTRADAY_EVENT",
        requested_event_source=dr.LOCAL_EVENT_SOURCE,
    )

    assert result.ok
    assert result.datasets == (dr.LIVE_INTRADAY_DATASET,)
    assert result.execution_contract["event_source"] == dr.LOCAL_EVENT_SOURCE
    assert all("timescaledb_information.chunks" in sql
               for sql, _params in market.sql)


def test_explicit_source_without_matching_manifest_fails_before_market_read() -> None:
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"]},
        meta_conn=_RowsMetaConnection([LIVE_ROW]),
        market_conn=_NoMarketRead(),
        research_lane="INTRADAY_EVENT",
        requested_event_source=dr.EXTERNAL_EVENT_SOURCE,
    )

    assert result.verdict == dr.SOURCE_AUTHORITY_MISMATCH
    assert not result.execution_contract
    assert any(dr.EXTERNAL_INTRADAY_DATASET in note for note in result.notes)


def test_direct_dataset_and_requested_source_conflict_fails_closed() -> None:
    result = dr.resolve(
        [dr.LIVE_INTRADAY_DATASET],
        meta_conn=_RowsMetaConnection([LIVE_ROW, EXTERNAL_ROW]),
        market_conn=_NoMarketRead(),
        research_lane="INTRADAY_EVENT",
        requested_event_source=dr.EXTERNAL_EVENT_SOURCE,
    )

    assert result.verdict == dr.SOURCE_AUTHORITY_MISMATCH
    assert "conflicts" in result.notes[0]


def test_intraday_lane_never_substitutes_daily_aggregate_for_raw_events() -> None:
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"],
         "min_history_days": 60},
        meta_conn=_DailyOnlyMetaConnection(),
        market_conn=_NoMarketRead(),
        research_lane="INTRADAY_EVENT",
    )
    assert result.verdict == dr.UNMAPPED_SOURCE
    assert set(result.unmapped) == {"market_quotes", "market_ticks"}
    assert any("daily microstructure aggregates cannot" in note
               for note in result.notes)


def test_intraday_direct_daily_product_cannot_authorize_event_replay() -> None:
    result = dr.resolve(
        ["krx-microstructure-daily/v1"],
        meta_conn=_DailyOnlyMetaConnection(),
        market_conn=_NoMarketRead(),
        research_lane="INTRADAY_EVENT",
    )
    assert result.verdict == dr.UNMAPPED_SOURCE
    assert set(result.unmapped) == {"market_quotes", "market_ticks"}


def test_ordinary_manifest_raw_key_names_cannot_authorize_event_replay() -> None:
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"]},
        meta_conn=_RowsMetaConnection([ORDINARY_RAW_KEY_ROW]),
        market_conn=_NoMarketRead(),
        research_lane="INTRADAY_EVENT",
    )
    assert result.verdict == dr.UNMAPPED_SOURCE
    assert set(result.unmapped) == {"market_quotes", "market_ticks"}
    assert any("source key names" in note for note in result.notes)


def test_typed_authority_marker_still_requires_exact_raw_source_versions() -> None:
    result = dr.resolve(
        {"tables": ["market_quotes", "market_ticks"]},
        meta_conn=_RowsMetaConnection([MALFORMED_TYPED_RAW_ROW]),
        market_conn=_NoMarketRead(),
        research_lane="INTRADAY_EVENT",
    )
    assert result.verdict == dr.UNMAPPED_SOURCE
    assert set(result.unmapped) == {"market_quotes", "market_ticks"}


def test_intraday_manifest_index_removes_raw_claim_from_ordinary_mixed_panel() -> None:
    ordinary_mixed = (
        "ordinary-stock-panel",
        "v10",
        {
            "market_bars": "ls-chart/1D",
            "market_quotes": "ls-realtime-book-v1",
            "market_ticks": "ls-realtime-trade-v1",
        },
        False,
    )
    index = dr.manifest_index(
        _RowsMetaConnection([ordinary_mixed, LIVE_ROW]),
        research_lane="INTRADAY_EVENT",
    )
    by_dataset = {f"{name}/{version}": sources
                  for name, version, sources in index}
    assert by_dataset["ordinary-stock-panel/v10"] == {
        "market_bars": "ls-chart/1D"}
    assert set(by_dataset[dr.LIVE_INTRADAY_DATASET]) == {
        "market_quotes", "market_ticks"}


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
        "ls-realtime-book-v1",
        "ls-realtime-trade-v1",
        "missing_received_at",
        "available_at=max(received_at,observed_at)",
        "event_time<=decision_time and available_at<=decision_time",
        "entry_time+horizon",
        "instrument_isolation",
        "received_at",
        "observed_at",
        "instrument_id",
        "bid_prices",
        "ask_prices",
        "price",
        "quantity",
        "source_event_id",
    )
    for value in required:
        assert value in sql
    assert "union" not in dr._SQL_MANIFESTS.lower()
    assert "manifest.name = 'krx-intraday-events'" in dr._SQL_MANIFESTS
    assert "manifest.name = 'krx-intraday-completed-second'" in \
        dr._SQL_MANIFESTS
    assert "not (" in dr._SQL_MANIFESTS
    assert "union" in dr._SQL_INTRADAY_MANIFESTS.lower()

    external = dr._SQL_EXTERNAL_INTRADAY_MANIFEST
    for value in (
            "manifest.name = 'krx-intraday-completed-second'",
            "manifest.version = 'v1'",
            "postgresql+fdw://ext_src/{quotes,ticks}",
            "trading-bot-completed-second-book-v1",
            "trading-bot-completed-second-trade-v1",
            "HISTORICAL_COMPLETED_SECOND_REQUIRES_PER_EXPERIMENT_AUDIT",
            "event_time_only_no_receipt_clock",
            "completed_source_second<=decision_time",
            "HISTORICAL_SEARCH_ONLY",
            "[09:00:00,15:30:00) Asia/Seoul",
            "maximum_horizon_seconds",
            "ext_src.quotes", "ext_src.ticks", "TAKER_ONLY"):
        assert value in external


def test_completed_second_manifest_migration_matches_runtime_authority() -> None:
    migration = (ROOT / "supabase" / "migrations" /
                 "20260818001400_intraday_completed_second_dataset.sql") \
        .read_text(encoding="utf-8")
    for value in (
            "krx-intraday-completed-second", "v1",
            "trading-bot-completed-second-book-v1",
            "trading-bot-completed-second-trade-v1",
            "postgresql+fdw://ext_src/{quotes,ticks}",
            "event_time_only_no_receipt_clock",
            "completed_source_second<=decision_time",
            "HISTORICAL_SEARCH_ONLY",
            "[09:00:00,15:30:00) Asia/Seoul",
            "maximum_horizon_seconds", "TAKER_ONLY",
            "ext_src.quotes", "ext_src.ticks"):
        assert value in migration


def test_autopilot_and_orchestrator_pass_the_same_lane_to_resolution() -> None:
    autopilot = (FACTORY / "factory_autopilot.py").read_text(encoding="utf-8")
    orchestrator = (PIPELINE / "experiment_orchestrator.py").read_text(
        encoding="utf-8")
    assert 'manifest_index(\n            conn, research_lane="INTRADAY_EVENT")' in autopilot
    assert "lane == \"INTRADAY_EVENT\"" in autopilot
    assert "research_lane = normalized_research_lane(execution_edge)" in orchestrator
    assert "research_lane=research_lane," in orchestrator
    assert "requested_event_source=str((hyp.get(\"expected_edge\") or {}).get(" \
        in orchestrator
    assert 'resolved_edge["data_source"] = res.execution_contract[' in orchestrator
    assert 'resolved_edge["resolved_data_contract"]' in orchestrator
