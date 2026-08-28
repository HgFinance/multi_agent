from __future__ import annotations

from datetime import date
from pathlib import Path
import sys
import types
from uuid import UUID

import pytest


ROOT = Path(__file__).resolve().parents[1]
COLLECTORS = ROOT / "departments" / "01-research" / "collectors"
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

import full_universe_builder
import ls_realtime_service
import ls_realtime_worker


def _row(
    symbol: str,
    value: int,
    *,
    market: str = "KRX",
    asset_class: str = "EQUITY",
    instrument_type: str = "STOCK",
    status: str = "ACTIVE",
    venue: str = "KOSPI",
    is_spac: bool = False,
) -> ls_realtime_service.ReferenceInstrumentRow:
    return ls_realtime_service.ReferenceInstrumentRow(
        symbol=symbol,
        instrument_id=UUID(int=value),
        market=market,
        asset_class=asset_class,
        instrument_type=instrument_type,
        status=status,
        venue=venue,
        is_spac=is_spac,
    )


def test_selection_includes_only_krx_active_equity_stock_and_audits_exclusions():
    rows = [
        _row("005930", 1),
        _row("0004Y0", 2, instrument_type="SPAC", venue="KOSDAQ", is_spac=True),
        _row("069500", 3, instrument_type="ETF"),
        _row("500001", 4, instrument_type="ETN"),
        _row("000660", 5, status="HALTED"),
        _row("AAPL", 6, market="NASDAQ"),
        _row("FUND01", 7, asset_class="FUND"),
        _row("123456", 8, is_spac=True),
        _row("654321", 9, venue="KONEX"),
    ]
    symbols = tuple(row.symbol for row in rows)

    selected = ls_realtime_service.select_stock_capture_universe(symbols, rows)

    assert [row.symbol for row in selected.included] == ["005930"]
    assert selected.excluded_by_reason == {
        "NON_EQUITY:FUND": ("FUND01",),
        "NON_KRX:NASDAQ": ("AAPL",),
        "NON_STOCK:ETF": ("069500",),
        "NON_STOCK:ETN": ("500001",),
        "NON_STOCK:SPAC": ("0004Y0",),
        "NOT_ACTIVE:HALTED": ("000660",),
        "SPAC_METADATA_CONFLICT": ("123456",),
        "UNSUPPORTED_VENUE:KONEX": ("654321",),
    }
    assert len(selected.fingerprint) == 64
    assert selected.fingerprint == (
        ls_realtime_service.select_stock_capture_universe(
            tuple(reversed(symbols)), list(reversed(rows))
        ).fingerprint
    )


@pytest.mark.parametrize(
    ("symbols", "rows", "message"),
    [
        (("005930", "000660"), [_row("005930", 1)], "매핑이 없는"),
        (("005930", "005930"), [_row("005930", 1)], "중복 심볼"),
        (
            ("005930",),
            [_row("005930", 1), _row("005930", 2)],
            "매핑이 중복",
        ),
    ],
    ids=["unresolved", "duplicate-request", "ambiguous-current-mapping"],
)
def test_selection_fails_closed_on_unresolved_or_ambiguous_identity(
    symbols, rows, message
):
    with pytest.raises(ls_realtime_service.LsRealtimeError, match=message):
        ls_realtime_service.select_stock_capture_universe(symbols, rows)


def test_large_snapshot_quarantines_a_tiny_stale_unmapped_tail_with_evidence():
    symbols = tuple(f"{value:06d}" for value in range(1, 1_001))
    rows = [_row(symbol, value) for value, symbol in enumerate(symbols[:-1], 1)]

    selected = ls_realtime_service.select_stock_capture_universe(symbols, rows)

    assert len(selected.included) == 999
    assert selected.excluded_by_reason == {
        ls_realtime_service.STALE_UNMAPPED_REASON: ("001000",),
    }
    assert len(selected.fingerprint) == 64


def test_large_snapshot_still_fails_closed_on_broad_reference_loss():
    symbols = tuple(f"{value:06d}" for value in range(1, 1_001))
    rows = [_row(symbol, value) for value, symbol in enumerate(symbols[:-10], 1)]

    with pytest.raises(ls_realtime_service.LsRealtimeError, match="coverage="):
        ls_realtime_service.select_stock_capture_universe(symbols, rows)


def test_selection_rejects_an_all_non_stock_capture():
    with pytest.raises(ls_realtime_service.LsRealtimeError, match="0개"):
        ls_realtime_service.select_stock_capture_universe(
            ("0004Y0",),
            [_row("0004Y0", 1, instrument_type="SPAC", is_spac=True)],
        )


class _Cursor:
    def __init__(self, owner):
        self.owner = owner

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        self.owner.sql = " ".join(sql.lower().split())
        self.owner.params = params

    def fetchall(self):
        return list(self.owner.rows)


class _Repository:
    instances = []
    rows = [
        ("0004Y0", UUID(int=2), "KRX", "EQUITY", "SPAC", "ACTIVE", "KOSDAQ", True),
        ("005930", UUID(int=1), "KRX", "EQUITY", "STOCK", "ACTIVE", "KOSPI", False),
    ]

    def __init__(self):
        self.sql = ""
        self.params = None
        self.closed = False
        self._conn = types.SimpleNamespace(cursor=lambda: _Cursor(self))
        self.__class__.instances.append(self)

    def recent_trading_sessions(self, *, limit):
        assert limit == 40
        return [(date(2026, 8, 18), None, None)]

    def market_session(self, trade_date):
        return (True, None, None)

    def close(self):
        self.closed = True


def test_fetch_reference_classifies_before_filtering_and_logs_audit(monkeypatch, capsys):
    _Repository.instances.clear()
    module = types.ModuleType("reference_repository")
    module.SupabaseReferenceRepository = _Repository
    monkeypatch.setitem(sys.modules, "reference_repository", module)

    ids, venues, days, session = ls_realtime_service._fetch_reference(
        ("005930", "0004Y0")
    )

    assert ids == {"005930": UUID(int=1)}
    assert venues == [("005930", "KOSPI")]
    assert days == {date(2026, 8, 18)}
    assert session == (True, None, None)
    repository = _Repository.instances[0]
    assert repository.closed is True
    assert "s.provider = 'ls'" in repository.sql
    assert "s.market = 'krx'" in repository.sql
    assert "s.symbol_type = 'trading'" in repository.sql
    assert "s.valid_from <= %s" in repository.sql
    assert "s.valid_to is null or s.valid_to > %s" in repository.sql
    assert "i.instrument_type = 'stock'" not in repository.sql
    output = capsys.readouterr().out
    assert "requested=2 included=1 excluded=1" in output
    assert "reason=NON_STOCK:SPAC count=1 symbols=['0004Y0']" in output
    assert "sha256=" in output


def test_full_universe_builder_query_is_current_ls_krx_stock_only():
    source = Path(full_universe_builder.__file__).read_text(encoding="utf-8").lower()

    assert "isym.provider = 'ls'" in source
    assert "isym.market = 'krx'" in source
    assert "isym.symbol_type = 'trading'" in source
    assert "isym.valid_from <= now()" in source
    assert "isym.valid_to is null or isym.valid_to > now()" in source
    assert "upper(i.market) = 'krx'" in source
    assert "upper(i.asset_class) = 'equity'" in source
    assert "upper(i.instrument_type) = 'stock'" in source
    assert "upper(i.status) = 'active'" in source
    assert "upper(i.venue) in ('kospi', 'kosdaq')" in source
    assert "metadata->>'is_spac'" in source


def test_market_workers_stagger_connection_handshakes(monkeypatch):
    sleeps = []
    run_max_seconds = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    class Worker:
        async def run(self, *, max_seconds):
            run_max_seconds.append(max_seconds)
            return "done"

    monkeypatch.setattr(ls_realtime_service.asyncio, "sleep", fake_sleep)

    result = ls_realtime_service.asyncio.run(
        ls_realtime_service._run_worker_after_delay(
            Worker(), max_seconds=10.0, delay_seconds=1.5
        )
    )

    assert result == "done"
    assert sleeps == [1.5]
    assert run_max_seconds == [8.5]


def test_market_worker_failure_restarts_only_that_shard():
    calls = []
    lines = []

    class Worker:
        stats = object()

        async def run(self, *, max_seconds):
            calls.append(max_seconds)
            if len(calls) == 1:
                raise ls_realtime_service.LsRealtimeError("handshake failed")
            return "recovered"

    result = ls_realtime_service.asyncio.run(
        ls_realtime_service._run_worker_resilient(
            Worker(),
            max_seconds=10.0,
            delay_seconds=0.0,
            stop=ls_realtime_service.asyncio.Event(),
            shard_index=7,
            retry_backoff=0.0,
            log=lines.append,
        )
    )

    assert result == "recovered"
    assert len(calls) == 2
    assert any("소켓7" in line and "해당 소켓만 재시작" in line for line in lines)


def test_market_worker_rebuilds_shared_capture_after_bounded_failures():
    class Worker:
        stats = object()

        async def run(self, *, max_seconds):
            del max_seconds
            raise ls_realtime_service.LsRealtimeError("database connection closed")

    with pytest.raises(ls_realtime_service.LsRealtimeError, match="공유 수집 경계를 재구축"):
        ls_realtime_service.asyncio.run(
            ls_realtime_service._run_worker_resilient(
                Worker(),
                max_seconds=10.0,
                delay_seconds=0.0,
                stop=ls_realtime_service.asyncio.Event(),
                shard_index=2,
                retry_backoff=0.0,
            )
        )


def test_market_heartbeat_restarts_after_no_progress():
    class Sink:
        class Stats:
            messages = written_ticks = written_quotes = 0

        stats = Stats()

    lines = []
    with pytest.raises(ls_realtime_service.LsRealtimeError, match="무진전"):
        ls_realtime_service.asyncio.run(
            ls_realtime_service._heartbeat(
                [Sink()],
                ls_realtime_service.asyncio.Event(),
                interval_seconds=0.01,
                restart_after_seconds=0.03,
                log=lines.append,
            )
        )
    assert any("전체 소켓 무진전" in line for line in lines)


def test_market_stream_disables_protocol_ping(monkeypatch):
    calls = []
    sentinel = object()
    module = types.ModuleType("websockets")

    def connect(url, **kwargs):
        calls.append((url, kwargs))
        return sentinel

    module.connect = connect
    monkeypatch.setitem(sys.modules, "websockets", module)

    result = ls_realtime_worker._connect_market_stream("wss://example.test/websocket")

    assert result is sentinel
    assert calls == [
        (
            "wss://example.test/websocket",
            {"open_timeout": 20, "ping_interval": None},
        )
    ]


def test_realtime_shards_share_one_timescale_repository_connection():
    source = Path(ls_realtime_service.__file__).read_text(encoding="utf-8")

    assert "repository = TimescaleMarketRepository(dsn)" in source
    assert "repos = [repository]" in source
    assert "MarketSink(repository) for _ in shards" in source
    assert "TimescaleMarketRepository(dsn) for _ in shards" not in source


def test_capture_scope_merges_active_rules_without_duplicates():
    assert ls_realtime_service.merge_capture_symbols(
        ("005930", "000660"), ("487400", "005930")
    ) == ("000660", "005930", "487400")


def test_symbol_change_waiter_reloads_only_when_authority_changes():
    calls = []

    def loader():
        calls.append(True)
        return ("005930", "487400")

    result = ls_realtime_service.asyncio.run(
        ls_realtime_service._wait_for_symbol_change(
            ("005930",),
            ls_realtime_service.asyncio.Event(),
            loader=loader,
            interval_seconds=0.001,
        )
    )

    assert result == ("005930", "487400")
    assert len(calls) == 1
