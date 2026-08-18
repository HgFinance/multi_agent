from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = ROOT / "departments" / "01-research" / "repository"
if str(REPOSITORY) not in sys.path:
    sys.path.insert(0, str(REPOSITORY))

import reference_repository


AS_OF = datetime(2026, 8, 18, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("flags", "expected_type"),
    [
        ({}, "STOCK"),
        ({"etf": True}, "ETF"),
        ({"etn": True}, "ETN"),
        ({"spac": True}, "SPAC"),
    ],
)
def test_stock_master_product_type_mapping(flags, expected_type) -> None:
    row = reference_repository._row(
        "123456", "KR7123456789", reference_repository.Venue.KOSDAQ,
        **flags,
    )

    record = reference_repository.master_row_to_record(row, as_of=AS_OF)

    assert record.instrument_type == expected_type
    assert record.asset_class == "EQUITY"
    assert record.market == "KRX"


def test_spac_marker_is_preserved_for_audit() -> None:
    row = reference_repository._row(
        "123456", "KR7123456789", reference_repository.Venue.KOSDAQ,
        spac=True,
    )

    record = reference_repository.master_row_to_record(row, as_of=AS_OF)

    assert record.instrument_type == "SPAC"
    assert record.metadata["is_spac"] is True


def test_spac_backfill_is_narrow_and_fail_closed() -> None:
    migration = (
        ROOT
        / "supabase"
        / "migrations"
        / "20260818000900_reference_spac_instrument_type.sql"
    ).read_text(encoding="utf-8").lower()

    assert "set instrument_type = 'spac'" in migration
    assert "upper(instrument_type) = 'stock'" in migration
    assert "upper(asset_class) = 'equity'" in migration
    assert "upper(market) = 'krx'" in migration
    assert "upper(status) = 'active'" in migration
    assert "metadata->>'is_spac'" in migration
    assert "upper(instrument_type) not in ('stock', 'spac')" in migration
    assert "raise exception" in migration
