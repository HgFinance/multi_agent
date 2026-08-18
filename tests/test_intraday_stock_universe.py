from __future__ import annotations

from datetime import date
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (PIPELINE, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from intraday_experiment_runner import (
    _assert_stock_selection_evidence,
    _profiled_panel,
    _stock_only_slice,
    prepare,
    record_data_feasibility,
)
from intraday_microstructure import EXTERNAL_EVENT_SOURCE


class _Cursor:
    def __init__(self, rows, symbol_rows=None):
        self.rows = rows
        self.symbol_rows = symbol_rows or []
        self.active_rows = rows
        self.sql = ""
        self.params = None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=None):
        self.sql = sql
        self.params = params
        self.active_rows = (self.symbol_rows
                            if "reference.instrument_symbols" in sql
                            else self.rows)

    def fetchall(self):
        return list(self.active_rows)


class _Conn:
    def __init__(self, rows, symbol_rows=None):
        self.rows = rows
        self.symbol_rows = symbol_rows

    def cursor(self):
        return _Cursor(self.rows, self.symbol_rows)


def test_external_slice_excludes_nonstock_and_unknown_identity_fail_closed() -> None:
    market = _Conn([
        ("005930", "id-stock"),
        ("069500", "id-etf"),
        ("999999", "id-missing-reference"),
    ])
    meta = _Conn([
        ("id-stock", "STOCK", "KRX", "KOSPI", "ACTIVE", None, None,
         "EQUITY", False),
        ("id-etf", "ETF", "KRX", "KOSPI", "ACTIVE", None, None,
         "EQUITY", False),
    ], symbol_rows=[
        ("id-stock", "005930"),
        ("id-etf", "069500"),
    ])
    selected = {
        "status": "PASS",
        "event_source": EXTERNAL_EVENT_SOURCE,
        "sessions": ["2026-08-14"],
        "instruments": ["005930", "069500", "999999", "no-symbol-map"],
        "instrument_profiles": {
            "005930": {"quote_events": 10},
            "069500": {"quote_events": 20},
        },
    }

    out = _stock_only_slice(meta, market, selected)

    assert out["status"] == "INSUFFICIENT_INSTRUMENTS"
    assert out["instruments"] == ["005930"]
    assert out["reference_instrument_ids"] == ["id-stock"]
    assert out["instrument_profiles"] == {
        "005930": {"quote_events": 10}}
    assert out["product_filter"] == "REFERENCE_INSTRUMENT_TYPE_STOCK_ONLY"
    assert out["asset_scope"] == "KRX_ACTIVE_STOCK_ONLY"
    assert out["product_filter_excluded"] == {
        "NON_STOCK": 1,
        "NON_EQUITY": 0,
        "NON_KRX": 0,
        "INACTIVE": 0,
        "SPAC": 0,
        "OUTSIDE_LISTING_INTERVAL": 0,
        "INVALID_TRADING_SYMBOL_FORMAT": 0,
        "AMBIGUOUS_VALID_SYMBOL_IDENTITY": 0,
        "MISSING_VALID_SYMBOL_IDENTITY": 0,
        "MISSING_SYMBOL_MAP": 1,
        "MISSING_REFERENCE_METADATA": 1,
    }
    assert out["symbol_valid_time_required"] is True
    assert out["historical_listing_interval_verified"] is False


def test_local_slice_uses_governed_uuid_identity_without_symbol_comparison() -> None:
    ids = [
        "00000000-0000-0000-0000-000000000001",
        "00000000-0000-0000-0000-000000000002",
    ]
    meta = _Conn([
        (instrument_id, "STOCK", "KRX", "KOSPI", "ACTIVE", None,
         None, "EQUITY", False)
        for instrument_id in ids
    ])
    selected = {
        "status": "PASS",
        "event_source": "LOCAL_RECEIPT_CLOCK",
        "sessions": ["2026-08-14"],
        "instruments": ids,
    }

    out = _stock_only_slice(meta, _Conn([]), selected)

    assert out["status"] == "PASS"
    assert out["instruments"] == ids
    assert out["reference_instrument_ids"] == ids
    assert out["product_filter_version"] == "krx-stock-only-v3"
    assert out["stock_universe_contract_version"] == \
        "krx-active-stock-only-v1"
    assert out["symbol_valid_time_required"] is False
    assert not any(out["product_filter_excluded"].values())


def test_local_slice_rejects_spac_even_when_master_calls_it_stock() -> None:
    stock_id = "00000000-0000-0000-0000-000000000001"
    spac_id = "00000000-0000-0000-0000-000000000002"
    meta = _Conn([
        (stock_id, "STOCK", "KRX", "KOSPI", "ACTIVE", None, None,
         "EQUITY", False),
        (spac_id, "STOCK", "KRX", "KOSPI", "ACTIVE", None, None,
         "EQUITY", True),
    ])
    selected = {
        "status": "PASS",
        "event_source": "LOCAL_RECEIPT_CLOCK",
        "calibration_sessions": ["2026-08-13"],
        "sessions": ["2026-08-14"],
        "instruments": [stock_id, spac_id],
    }

    out = _stock_only_slice(meta, _Conn([]), selected)

    assert out["instruments"] == [stock_id]
    assert out["product_filter_excluded"]["SPAC"] == 1


def test_local_slice_checks_listing_interval_over_calibration_too() -> None:
    valid_id = "00000000-0000-0000-0000-000000000001"
    late_id = "00000000-0000-0000-0000-000000000002"
    meta = _Conn([
        (valid_id, "STOCK", "KRX", "KOSPI", "ACTIVE",
         date(2026, 8, 1), None, "EQUITY", False),
        (late_id, "STOCK", "KRX", "KOSPI", "ACTIVE",
         date(2026, 8, 14), None, "EQUITY", False),
    ])
    selected = {
        "status": "PASS",
        "event_source": "LOCAL_RECEIPT_CLOCK",
        "calibration_sessions": ["2026-08-13"],
        "sessions": ["2026-08-14"],
        "instruments": [valid_id, late_id],
    }

    out = _stock_only_slice(meta, _Conn([]), selected)

    assert out["instruments"] == [valid_id]
    assert out["product_filter_excluded"][
        "OUTSIDE_LISTING_INTERVAL"] == 1


def test_prepare_fails_closed_without_reference_plane() -> None:
    with pytest.raises(RuntimeError, match="reference-plane stock validation"):
        prepare({}, market_conn=_Conn([]))


def test_pass_slice_without_stock_intersection_cannot_be_persisted() -> None:
    forged = {
        "status": "PASS",
        "statistical_readiness": "FULL",
        "sessions": ["2026-08-14"],
        "instruments": ["069500"],
    }

    with pytest.raises(RuntimeError, match="stock-scope evidence"):
        _assert_stock_selection_evidence(forged)
    with pytest.raises(RuntimeError, match="stock-scope evidence"):
        record_data_feasibility(
            _Conn([]), "00000000-0000-0000-0000-000000000099",
            {"selected": forged, "cutoff": "2026-08-15T00:00:00+00:00"},
        )


def test_profiled_panel_contains_information_rich_and_activity_guard() -> None:
    instruments = [f"S{index:02d}" for index in range(20)]
    selected = {
        "instruments": instruments,
        "instrument_profiles": {
            instrument: {
                "quote_events": index + 1,
                "trade_intensity": index / 10,
                "spread_bps": 20 - index / 2,
                "depth_notional_l1": 100 * index,
            }
            for index, instrument in enumerate(instruments)
        },
    }

    panel, manifest = _profiled_panel(selected, 8)

    assert len(panel) == 8
    assert len(set(panel)) == 8
    assert manifest["information_rich"] == ["S19", "S18", "S17", "S16"]
    assert len(manifest["representative_guard"]) == 4
    assert manifest["profile_source"] == \
        "STRICTLY_PRE_EVALUATION_CALIBRATION_SUMMARIES"
    assert manifest["nested_prefix_contract"] is True
    larger, larger_manifest = _profiled_panel(selected, 16)
    assert panel == larger[:8]
    assert set(panel) < set(larger)
    assert (manifest["ordered_universe_fingerprint"] ==
            larger_manifest["ordered_universe_fingerprint"])
    assert manifest["promotion_authority"] is False
