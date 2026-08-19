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
