from __future__ import annotations

from datetime import datetime, timezone

from apps.api.ls_accounting_evidence import normalize_ls_accounting_evidence


def _responses() -> dict:
    return {
        "CSPAQ12200": {
            "CSPAQ12200OutBlock1": {"AcntNo": "12345678901", "Pwd": "secret"},
            "CSPAQ12200OutBlock2": {
                "Dps": 1000,
                "D1Dps": 900,
                "D2Dps": 800,
                "MnyOrdAbleAmt": 700,
                "MnyoutAbleAmt": 600,
                "BalEvalAmt": 2500,
                "DpsastTotamt": 3500,
                "RcvblAmt": 0,
            },
        },
        "CSPAQ22200": {
            "CSPAQ22200OutBlock2": {
                "Dps": 1000,
                "D1Dps": 900,
                "D2Dps": 800,
                "MnyOrdAbleAmt": 700,
                "RcvblAmt": 0,
                "CrdtOrdAbleAmt": 400,
            }
        },
        "CSPAQ12300": {
            "CSPAQ12300OutBlock1": {"AcntNo": "12345678901", "Pwd": "secret"},
            "CSPAQ12300OutBlock2": {
                "Dps": 1000,
                "D1Dps": 900,
                "D2Dps": 800,
                "MnyOrdAbleAmt": 700,
                "BalEvalAmt": 2500,
                "PchsAmt": 2000,
                "EvalPnlSum": 500,
                "DpsastTotamt": 3500,
                "RcvblAmt": 0,
            },
            "CSPAQ12300OutBlock3": [
                {
                    "IsuNo": "A005930",
                    "IsuNm": "삼성전자",
                    "BalQty": 10,
                    "SellAbleQty": 9,
                    "AvrUprc": "71000.50",
                    "NowPrc": 75000,
                    "PchsAmt": 710005,
                    "BalEvalAmt": 750000,
                    "EvalPnl": 39995,
                    "PnlRat": "0.0563",
                }
            ],
        },
        "t0424": {
            "t0424OutBlock": {"sunamt": 3500},
            "t0424OutBlock1": [
                {
                    "expcode": "005930",
                    "hname": "삼성전자",
                    "janqty": 10,
                    "mdposqt": 9,
                    "pamt": 70000,
                    "price": 75000,
                    "mamt": 700000,
                    "appamt": 750000,
                    "dtsunik": 50000,
                    "fee": 10,
                    "tax": 20,
                }
            ],
        },
        "CDPCQ04700": {
            "CDPCQ04700OutBlock1": {"AcntNo": "12345678901", "Pwd": "secret"},
            "CDPCQ04700OutBlock3": [
                {
                    "TrdDt": "20260826",
                    "TrdNo": 1,
                    "TpCodeNm": "매매",
                    "SmryNm": "현금매수",
                    "IsuNo": "A005930",
                    "IsuNm": "삼성전자",
                    "TrdQty": 10,
                    "TrdUprc": 71000,
                    "TrdAmt": 710000,
                    "AdjstAmt": -710100,
                    "CmsnAmt": 100,
                    "TaxSumAmt": 0,
                    "DpsBfbalAmt": 2000,
                    "DpsCrbalAmt": 1000,
                }
            ],
            "CDPCQ04700OutBlock4": {
                "PnlSumAmt": 30,
                "CtrctAsm": 710000,
                "CmsnAmtSumAmt": 100,
            },
            "CDPCQ04700OutBlock5": {"BuyAmt": 710000, "BuyCmsn": 100},
        },
        "t0150": {
            "t0150OutBlock": {"tamt": 710000, "tfee": 100, "ttax": 100},
            "t0150OutBlock1": [
                {"medosu": "매수", "expcode": "005930", "qty": 10, "price": 71000}
            ],
        },
        "t0151": {
            "t0151OutBlock": {"tamt": 500000, "tfee": 80, "ttax": 80},
            "t0151OutBlock1": [],
        },
        "CSPAQ13700": {
            "CSPAQ13700OutBlock2": {"BuyExecAmt": 710000, "BuyExecQty": 10},
            "CSPAQ13700OutBlock3": [
                {"OrdNo": 42, "IsuNo": "A005930", "BnsTpNm": "매수", "ExecQty": 10}
            ],
        },
        "t0425": {
            "t0425OutBlock": {"tqty": 10, "tcheqty": 10, "tordrem": 0, "cmss": 100},
            "t0425OutBlock1": [
                {"ordno": 42, "expcode": "005930", "qty": 10, "cheqty": 10, "ordrem": 0}
            ],
        },
        "FOCCQ33600": {
            "FOCCQ33600OutBlock2": {"InvstPlAmt": 500, "InvstErnrat": "0.142857"},
            "FOCCQ33600OutBlock3": [
                {"BaseDt": "20260826", "FdEvalAmt": 3000, "EotEvalAmt": 3500, "TermErnrat": "0.142857"}
            ],
        },
    }


def test_normalizes_all_accounting_axes_without_leaking_credentials() -> None:
    evidence = normalize_ls_accounting_evidence(
        _responses(),
        period_start="2026-07-28",
        period_end="2026-08-26",
        previous_date="2026-08-25",
        environment="paper",
        as_of=datetime(2026, 8, 26, 8, 0, tzinfo=timezone.utc),
    )

    assert evidence["schema_version"] == "accounting.broker-evidence.v1"
    assert evidence["account"] == {"masked": "****8901"}
    assert evidence["account_summary"]["cash"]["d2_deposit"] == "800"
    assert evidence["positions"][0]["unit_cost_bep"] == "71000.5"
    assert evidence["position_check"][0]["average_unit_price"] == "70000"
    assert evidence["position_reconciliation"]["status"] == "MATCH"
    assert evidence["activity"]["today"]["summary"]["total_commission"] == "100"
    assert evidence["activity"]["settled_period"]["rows"][0]["trade_date"] == "2026-08-26"
    assert evidence["performance"]["summary"]["return_rate"] == "0.142857"
    assert evidence["credit_limit"]["status"] == "NEEDS_PARAMETERS"
    assert evidence["margin_capacity"]["status"] == "NEEDS_PARAMETERS"
    assert evidence["authoritative"] is False
    assert evidence["is_official"] is False
    rendered = repr(evidence)
    assert "12345678901" not in rendered
    assert "secret" not in rendered


def test_surfaces_cross_tr_breaks_and_partial_coverage() -> None:
    responses = _responses()
    responses["CSPAQ22200"]["CSPAQ22200OutBlock2"]["D2Dps"] = 799
    responses["t0424"]["t0424OutBlock1"][0]["janqty"] = 9
    responses["FOCCQ33600"] = {"error": "rate limited", "meta": {"pages": 0}}

    evidence = normalize_ls_accounting_evidence(
        responses,
        period_start="2026-07-28",
        period_end="2026-08-26",
        previous_date="2026-08-25",
        environment="PAPER",
    )

    assert evidence["position_reconciliation"]["status"] == "BREAK"
    assert any(not check["match"] for check in evidence["account_cross_checks"])
    kinds = {item["kind"] for item in evidence["exceptions"]}
    assert kinds == {
        "BROKER_POSITION_TR_MISMATCH",
        "BROKER_CASH_TR_MISMATCH",
        "BROKER_EVIDENCE_INCOMPLETE",
    }
    assert evidence["coverage"]["FOCCQ33600"]["status"] == "ERROR"


def test_parameterized_credit_and_margin_trs_are_consumed_when_present() -> None:
    responses = _responses()
    responses["CSPAQ00600"] = {
        "CSPAQ00600OutBlock1": {"IsuNo": "A005930", "InptPwd": "secret"},
        "CSPAQ00600OutBlock2": {
            "MktcplMloanLmtAmt": 1000000,
            "MktcplMloanAmtSum": 200000,
            "PldgMaintRat": "1.40",
            "OrdAbleQty": 5,
        },
    }
    responses["CSPBQ00200"] = {
        "CSPBQ00200OutBlock1": {"IsuNo": "A005930", "InptPwd": "secret"},
        "CSPBQ00200OutBlock2": {
            "IsuNm": "삼성전자",
            "IsuMgnRat": "0.40",
            "OrdAbleAmt": 375000,
            "OrdAbleQty": 5,
        },
    }

    evidence = normalize_ls_accounting_evidence(
        responses,
        period_start="2026-07-28",
        period_end="2026-08-26",
        previous_date="2026-08-25",
        environment="PAPER",
    )

    assert evidence["credit_limit"]["status"] == "OK"
    assert evidence["credit_limit"]["loan_limit"] == "1000000"
    assert evidence["margin_capacity"]["status"] == "OK"
    assert evidence["margin_capacity"]["symbol"] == "005930"
    assert evidence["margin_capacity"]["orderable_quantity"] == "5"
    assert "secret" not in repr(evidence)


def test_paper_completion_code_with_output_is_not_misclassified_as_error() -> None:
    responses = _responses()
    responses["CSPAQ12200"]["rsp_cd"] = "00136"
    responses["CSPAQ12200"]["rsp_msg"] = "모의투자 조회가 완료되었습니다."
    responses["FOCCQ33600"] = {
        "rsp_cd": "01900",
        "rsp_msg": "모의투자에서는 해당업무가 제공되지 않습니다.",
    }

    evidence = normalize_ls_accounting_evidence(
        responses,
        period_start="2026-07-28",
        period_end="2026-08-26",
        previous_date="2026-08-25",
        environment="PAPER",
    )

    assert evidence["coverage"]["CSPAQ12200"]["status"] == "OK"
    assert evidence["coverage"]["FOCCQ33600"]["status"] == "ERROR"
