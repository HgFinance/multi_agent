from __future__ import annotations

from pathlib import Path
import sys

import pytest


COLLECTORS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/collectors"
if str(COLLECTORS_DIR) not in sys.path:
    sys.path.insert(0, str(COLLECTORS_DIR))

from market_cap_universe_collector import (  # noqa: E402
    MarketCapUniverseError,
    ranked_symbols,
    stock_members,
    snapshot_content_hash,
)


def _rows(count: int = 100) -> list[dict[str, object]]:
    return [{"shcode": f"{index:06d}", "total": 100_000 - index} for index in range(1, count + 1)]


def test_t1444_response_becomes_ordered_rank_manifest_without_raw_fields() -> None:
    ranked = ranked_symbols(_rows())
    identities = {
        member["symbol"]: {"instrument_id": f"id-{member['symbol']}", "instrument_type": "STOCK"}
        for member in ranked
    }
    members = stock_members(ranked, identities)

    assert len(members) == 100
    assert members[0]["rank"] == 1 and members[0]["stock_rank"] == 1
    assert members[-1]["rank"] == 100 and members[-1]["stock_rank"] == 100
    assert len(snapshot_content_hash(members)) == 64


@pytest.mark.parametrize(
    "rows",
    [
        _rows(99),
        _rows()[:-1] + [{"shcode": "000001", "total": 1}],
        _rows()[:-1] + [{"shcode": "bad", "total": 1}],
        _rows()[:-1] + [{"shcode": "000100", "total": 0}],
    ],
)
def test_incomplete_or_invalid_ranking_fails_closed(rows: list[dict[str, object]]) -> None:
    with pytest.raises(MarketCapUniverseError):
        ranked_symbols(rows)


def test_manifest_hash_changes_when_ranked_members_change() -> None:
    first_ranked = ranked_symbols(_rows())
    identities = {
        member["symbol"]: {"instrument_id": f"id-{member['symbol']}", "instrument_type": "STOCK"}
        for member in first_ranked
    }
    first = stock_members(first_ranked, identities)
    second_rows = _rows()
    second_rows[0], second_rows[1] = second_rows[1], second_rows[0]
    second_ranked = ranked_symbols(second_rows)
    second = stock_members(second_ranked, identities)

    assert snapshot_content_hash(first) != snapshot_content_hash(second)


def test_etfs_are_excluded_but_provider_rank_is_retained() -> None:
    ranked = ranked_symbols(_rows(102), limit=102)
    identities = {
        member["symbol"]: {
            "instrument_id": f"id-{member['symbol']}",
            "instrument_type": "ETF" if member["symbol"] in {"000002", "000004"} else "STOCK",
        }
        for member in ranked
    }

    members = stock_members(ranked, identities, limit=100)

    assert len(members) == 100
    assert members[0]["rank"] == 1 and members[0]["stock_rank"] == 1
    assert members[1]["rank"] == 3 and members[1]["stock_rank"] == 2
    assert members[-1]["rank"] == 102 and members[-1]["stock_rank"] == 100
