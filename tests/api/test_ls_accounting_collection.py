from __future__ import annotations

import asyncio
from datetime import date

import pytest

from apps.api import ls_account_stream


def test_accounting_request_catalog_covers_ten_account_level_trs() -> None:
    requests = ls_account_stream._accounting_tr_requests(
        date(2026, 8, 1), date(2026, 8, 26), date(2026, 8, 25)
    )

    assert set(requests) == {
        "CDPCQ04700",
        "CSPAQ12200",
        "CSPAQ12300",
        "CSPAQ13700",
        "CSPAQ22200",
        "FOCCQ33600",
        "t0150",
        "t0151",
        "t0424",
        "t0425",
    }
    assert requests["CSPAQ12300"]["payload"]["CSPAQ12300InBlock1"]["UprcTpCode"] == "1"
    assert requests["t0424"]["payload"]["t0424InBlock"]["prcgb"] == "1"
    assert requests["t0151"]["payload"]["t0151InBlock"]["date"] == "20260825"


def test_parameterized_trs_are_added_only_with_required_inputs() -> None:
    requests = ls_account_stream._accounting_tr_requests(
        date(2026, 8, 1),
        date(2026, 8, 26),
        date(2026, 8, 25),
        symbol="005930",
        order_price="75,000",
        side="2",
        loan_detail_class="01",
    )

    assert requests["CSPAQ00600"]["payload"]["CSPAQ00600InBlock1"] == {
        "LoanDtlClssCode": "01",
        "IsuNo": "005930",
        "OrdPrc": "75000",
        "CommdaCode": "41",
    }
    assert requests["CSPBQ00200"]["payload"]["CSPBQ00200InBlock1"]["BnsTpCode"] == "2"


def test_realtime_fill_is_visible_before_account_history_refresh() -> None:
    realtime_fill = {
        "kind": "FILLED",
        "source": "LS_REALTIME",
        "order_no": "1001",
        "broker_order_id": "1001",
        "broker_order_ids": ["1001"],
        "symbol": "005930",
        "side": "매수",
        "quantity": "1",
        "price": "70000",
        "event_time": "09:01:02",
        "received_at": "2026-08-31T00:01:02+00:00",
        "seq": 1,
    }

    merged = ls_account_stream.merge_order_events([], [realtime_fill], 50)

    assert len(merged) == 1
    assert merged[0]["kind"] == "FILLED"
    assert merged[0]["source"] == "LS_REALTIME"


def test_realtime_fill_is_deduplicated_when_account_history_arrives() -> None:
    realtime_fill = {
        "kind": "FILLED",
        "source": "LS_REALTIME",
        "order_no": "1001",
        "broker_order_id": "1001",
        "broker_order_ids": ["1001"],
        "symbol": "005930",
        "side": "매수",
        "quantity": "1",
        "price": "70000",
        "event_time": "09:01:02",
        "received_at": "2026-08-31T00:01:02+00:00",
        "seq": 1,
    }
    history_fill = {
        **realtime_fill,
        "source": "LS_ORDER_HISTORY",
        "received_at": "2026-08-31T09:01:02",
    }

    merged = ls_account_stream.merge_order_events(
        [], [realtime_fill, history_fill], 50
    )

    assert len(merged) == 1
    assert merged[0]["source"] == "LS_ORDER_HISTORY"


def test_today_activity_reuses_trade_rows_when_summary_is_omitted() -> None:
    activity = ls_account_stream.normalize_today_activity(
        {
            "t0150OutBlock1": [
                {
                    "medosu": "매수",
                    "expcode": "005930",
                    "qty": "2",
                    "price": "70000",
                    "amt": "140000",
                },
                {
                    "medosu": "종목소계",
                    "qty": "2",
                    "price": "70000",
                    "amt": "140000",
                    "fee": "100",
                    "tax": "0",
                    "argtax": "0",
                    "adjamt": "140100",
                },
            ]
        }
    )

    assert activity == {
        "trade_count": 1,
        "summary": {
            "buy_quantity": "2",
            "sell_quantity": "0",
            "buy_amount": "140000",
            "sell_amount": "0",
            "total_amount": "140000",
            "total_fee": "100",
            "total_tax": "0",
            "total_settlement": "-140100",
        },
    }


def test_today_activity_without_summary_or_rows_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="요약과 거래 행이 없습니다"):
        ls_account_stream.normalize_today_activity({"rsp_cd": "ERROR"})


def test_fetch_today_activity_uses_one_t0150_request(monkeypatch) -> None:
    calls: list[str] = []

    async def post_tr(config, token, tr_cd, payload, path="/stock/accno"):
        del config, token, payload, path
        calls.append(tr_cd)
        return {"rsp_cd": "00000", "rsp_msg": "조회가 완료되었습니다."}

    monkeypatch.setattr(ls_account_stream, "_post_tr", post_tr)

    activity = asyncio.run(
        ls_account_stream._fetch_today_activity(object(), "paper-token")
    )

    assert calls == ["t0150"]
    assert activity["trade_count"] == 0



def test_account_tr_request_sends_normalized_mac_header(monkeypatch) -> None:
    import ls_http
    from types import SimpleNamespace

    seen: dict[str, object] = {}

    class FakeResponse:
        text = "{\"rsp_cd\":\"00000\"}"
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def post(self, url, *, json, headers):
            seen.update(url=url, json=json, headers=headers)
            return FakeResponse()

    monkeypatch.setattr(ls_http, "ls_async_client", lambda **_kwargs: FakeClient())

    body, metadata = asyncio.run(
        ls_account_stream._post_tr_page(
            SimpleNamespace(
                base_url="https://broker.example",
                timeout_seconds=5,
                mac_address="0a:c5:e7-48-30-0f",
            ),
            "paper-token",
            "t0150",
            {"t0150InBlock": {}},
        )
    )

    assert body == {"rsp_cd": "00000"}
    assert metadata == {"tr_cont": "", "tr_cont_key": ""}
    assert seen["headers"]["mac_address"] == "0AC5E748300F"


def test_continuation_pages_merge_rows_and_forward_cts(monkeypatch) -> None:
    calls = []

    async def no_wait() -> None:
        return None

    async def post_page(config, token, tr_cd, payload, path="/stock/accno", **headers):
        del config, token, tr_cd, path
        calls.append((dict(payload["t0424InBlock"]), headers))
        if len(calls) == 1:
            return (
                {
                    "t0424OutBlock": {"cts_expcode": "005930", "sunamt": 100},
                    "t0424OutBlock1": [{"expcode": "000660"}],
                },
                {"tr_cont": "Y", "tr_cont_key": "next-key"},
            )
        return (
            {
                "t0424OutBlock": {"cts_expcode": "", "sunamt": 100},
                "t0424OutBlock1": [{"expcode": "005930"}],
            },
            {"tr_cont": "N", "tr_cont_key": ""},
        )

    monkeypatch.setattr(ls_account_stream, "_tr_slot", no_wait)
    monkeypatch.setattr(ls_account_stream, "_post_tr_page", post_page)
    definition = ls_account_stream._accounting_tr_requests(
        date(2026, 8, 1), date(2026, 8, 26), date(2026, 8, 25)
    )["t0424"]

    result = asyncio.run(
        ls_account_stream._fetch_tr_pages(object(), "token", "t0424", definition)
    )

    assert [row["expcode"] for row in result["body"]["t0424OutBlock1"]] == [
        "000660",
        "005930",
    ]
    assert calls[1][0]["cts_expcode"] == "005930"
    assert calls[1][1] == {"tr_cont": "Y", "tr_cont_key": "next-key"}
    assert result["meta"] == {"pages": 2, "complete": True, "truncated": False}


def test_order_events_sort_newest_first_across_mixed_timestamp_formats() -> None:
    """The 2026-09-01 screenshot: a 15:28 order sat at the bottom of the list.

    `received_at` arrives in three shapes - KST naive from the account history,
    a UTC offset from the realtime feed, and a bare date from the settled
    ledger. Comparing them as strings puts `...T06:28:19+00:00` below
    `...T13:24:09` because `'0' < '1'` at the eleventh character, even though
    06:28 UTC *is* 15:28 KST and therefore the newest event on the page.
    """

    history = [
        {"kind": "FILLED", "received_at": "2026-09-01T13:24:09",
         "event_time": "13:24:09", "order_no": "27456"},
        {"kind": "FILLED", "received_at": "2026-09-01T11:37:31",
         "event_time": "11:37:31", "order_no": "21316"},
        {"kind": "FILLED", "received_at": "2026-08-31",
         "event_time": None, "order_no": None},
    ]
    realtime = [
        {"kind": "ACCEPTED", "received_at": "2026-09-01T06:28:19.632013+00:00",
         "event_time": "152819479", "order_no": "39736"},
    ]

    merged = ls_account_stream.merge_order_events(history, realtime, 50)

    assert [event.get("order_no") for event in merged] == [
        "39736",  # 15:28:19 KST - newest, and it must lead
        "27456",
        "21316",
        None,      # settled ledger day roll-up is oldest
    ]


def test_order_event_instant_reads_naive_timestamps_as_kst() -> None:
    naive = ls_account_stream.order_event_instant("2026-09-01T15:28:19")
    aware = ls_account_stream.order_event_instant("2026-09-01T06:28:19+00:00")
    assert naive == aware

    # An unusable value must not sort ahead of a real one.
    assert ls_account_stream.order_event_instant("") < aware
    assert ls_account_stream.order_event_instant("not-a-time") < aware
