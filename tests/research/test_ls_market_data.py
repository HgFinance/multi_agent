from __future__ import annotations

from pathlib import Path
import sys

import pytest


AUTONOMOUS_DIR = Path(__file__).resolve().parents[2] / "departments/01-research/autonomous"
if str(AUTONOMOUS_DIR) not in sys.path:
    sys.path.insert(0, str(AUTONOMOUS_DIR))

from ls_market_data import OnDemandMarketDataClient


class FakeLsClient:
    def __init__(self, pages: list[tuple[dict, dict]]) -> None:
        self.pages = pages
        self.calls: list[dict] = []

    def call_tr(self, **kwargs: object):
        self.calls.append(kwargs)
        return self.pages.pop(0)


def _page(tr: str, rows: list[dict], *, more: str = "N", cursor: str = "", key: str = ""):
    return (
        {f"{tr}OutBlock": {"cts_date": cursor}, f"{tr}OutBlock1": rows},
        {"tr_cont": more, "tr_cont_key": key},
    )


def _ranking_page(tr: str, rows: list[dict], *, more: str = "N", idx: int = 0, key: str = ""):
    return (
        {f"{tr}OutBlock": {"idx": idx}, f"{tr}OutBlock1": rows},
        {"tr_cont": more, "tr_cont_key": key},
    )


def test_integrated_daily_uses_t8451_and_header_continuation() -> None:
    fake = FakeLsClient([
        _page("t8451", [{"date": "20240102", "close": 102}, {"date": "20240101", "close": 101}], more="Y", cursor="20231229", key="next-1"),
        _page("t8451", [{"date": "20231229", "close": 99}]),
    ])
    batch = OnDemandMarketDataClient(fake, max_pages=3).fetch_chart(
        "005930", "20231201", "20240131", timeframe="daily"
    )

    assert [row["date"] for row in batch.rows] == ["20231229", "20240101", "20240102"]
    assert batch.receipt.tr_code == "t8451"
    assert batch.receipt.row_count == 3
    assert fake.calls[0]["in_block"]["t8451InBlock"] == {
        "shcode": "005930",
        "qrycnt": 500,
        "sdate": "20231201",
        "edate": "20240131",
        "cts_date": "",
        "comp_yn": "N",
        "gubun": "2",
        "sujung": "Y",
        "exchgubun": "U",
    }
    assert fake.calls[1]["tr_cont"] == "Y"
    assert fake.calls[1]["tr_cont_key"] == "next-1"
    assert fake.calls[1]["in_block"]["t8451InBlock"]["cts_date"] == "20231229"


@pytest.mark.parametrize(
    ("timeframe", "integrated", "expected_tr"),
    [
        ("weekly", False, "t8410"),
        ("monthly", True, "t8451"),
        ("yearly", False, "t8410"),
        ("minute", False, "t8412"),
        ("minute", True, "t8452"),
        ("tick", False, "t8411"),
        ("tick", True, "t8453"),
    ],
)
def test_chart_timeframe_selects_only_the_requested_allowlisted_tr(
    timeframe: str, integrated: bool, expected_tr: str
) -> None:
    fake = FakeLsClient([_page(expected_tr, [{"date": "20240102", "time": "090000"}])])
    OnDemandMarketDataClient(fake).fetch_chart(
        "005930", "20240101", "20240131", timeframe=timeframe,
        integrated=integrated, interval=5,
    )

    assert fake.calls[0]["tr_cd"] == expected_tr
    assert list(fake.calls[0]["in_block"]) == [f"{expected_tr}InBlock"]
    assert expected_tr in {"t1665", "t8410", "t8411", "t8412", "t8451", "t8452", "t8453"}


def test_t1665_uses_explicit_investor_tr_contract() -> None:
    fake = FakeLsClient([_page("t1665", [{"date": "20240102", "sv_08": 10}])])
    batch = OnDemandMarketDataClient(fake).fetch_investor_trend(
        market="1", upcode="001", start_date="20240101", end_date="20240131",
        value_mode="2", unit="1", exchange="K",
    )

    assert batch.receipt.tr_code == "t1665"
    assert fake.calls[0]["path"] == "/stock/chart"
    assert fake.calls[0]["in_block"] == {
        "t1665InBlock": {
            "market": "1", "upcode": "001", "gubun2": "2", "gubun3": "1",
            "from_date": "20240101", "to_date": "20240131", "exchgubun": "K",
        }
    }


def test_ranking_fetch_supports_market_cap_and_idx_continuation() -> None:
    fake = FakeLsClient([
        _ranking_page("t1444", [{"shcode": "005930", "total": 500}], more="Y", idx=7, key="next-1"),
        _ranking_page("t1444", [{"shcode": "000660", "total": 300}], idx=9),
    ])
    client = OnDemandMarketDataClient(fake, max_pages=3)
    batch = client.fetch_ranking(
        "t1444", {"upcode": "001", "idx": 0}, as_of="20260827"
    )

    assert [row["shcode"] for row in batch.rows] == ["005930", "000660"]
    assert batch.receipt.tr_code == "t1444"
    assert batch.receipt.as_of == "20260827"
    assert fake.calls[0]["path"] == "/stock/high-item"
    assert fake.calls[0]["in_block"] == {"t1444InBlock": {"upcode": "001", "idx": 0}}
    assert fake.calls[1]["tr_cont"] == "Y"
    assert fake.calls[1]["tr_cont_key"] == "next-1"
    assert fake.calls[1]["in_block"] == {"t1444InBlock": {"upcode": "001", "idx": 7}}


def test_all_requested_ranking_trs_are_allow_listed() -> None:
    from ls_market_data import RANKING_TR_CODES

    assert RANKING_TR_CODES == {
        "t1441", "t1444", "t1452", "t1463", "t1466", "t1481", "t1482",
        "t1489", "t1492",
    }


def test_invalid_or_unallowlisted_requests_are_blocked() -> None:
    fake = FakeLsClient([])
    client = OnDemandMarketDataClient(fake)
    with pytest.raises(ValueError, match="six-character"):
        client.fetch_chart("5930", "20240101", "20240131")
    with pytest.raises(ValueError, match="calendar date"):
        client.fetch_chart("005930", "20240231", "20240131")
    with pytest.raises(ValueError, match="allow-listed"):
        client._fetch_pages(  # type: ignore[attr-defined]
            tr_code="t9999", request_block={}, start_date="20240101", end_date="20240131",
            symbol=None, has_time=False,
        )
