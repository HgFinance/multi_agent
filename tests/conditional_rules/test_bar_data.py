from __future__ import annotations

from datetime import datetime, timedelta, timezone

from orchestration.conditional_rules import Timeframe
from orchestration.conditional_rules.bar_data import (
    KST,
    LSChartBarResolver,
    timeframe_close_at,
)


def test_timeframe_close_at_supports_new_intraday_cadences() -> None:
    start = datetime(2026, 8, 20, 9, 0, tzinfo=KST)

    assert timeframe_close_at(start, Timeframe.M3) == datetime(
        2026, 8, 20, 0, 3, tzinfo=timezone.utc
    )
    assert timeframe_close_at(start, Timeframe.M10) == datetime(
        2026, 8, 20, 0, 10, tzinfo=timezone.utc
    )
    assert timeframe_close_at(start, Timeframe.M30) == datetime(
        2026, 8, 20, 0, 30, tzinfo=timezone.utc
    )


def test_daily_chart_path_excludes_today_partial_candle() -> None:
    yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y%m%d")

    class Transport:
        def __init__(self) -> None:
            self.calls = []

        def request_sync(self, *, path, tr_code, payload):
            self.calls.append((path, tr_code, payload))
            return {
                "rsp_cd": "00000",
                "t8451OutBlock1": [
                    {
                        "date": yesterday,
                        "open": "100",
                        "high": "110",
                        "low": "90",
                        "close": "105",
                        "jdiff_vol": "1000",
                    }
                ],
                "t8451OutBlock": {},
            }

    transport = Transport()
    resolver = LSChartBarResolver(transport)

    bars = resolver.bars("005930", Timeframe.D1, 1)

    assert len(bars) == 1
    assert bars[0].bucket_time.strftime("%Y%m%d") == yesterday
    _path, tr_code, payload = transport.calls[0]
    assert tr_code == "t8451"
    assert payload["t8451InBlock"]["gubun"] == "2"
    assert payload["t8451InBlock"]["sujung"] == "Y"
