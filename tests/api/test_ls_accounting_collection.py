from __future__ import annotations

import asyncio
from datetime import date

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
