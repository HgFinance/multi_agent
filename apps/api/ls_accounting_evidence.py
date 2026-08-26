"""LS stock-account TR responses -> bounded accounting broker evidence.

The complete field catalogue lives in
``docs/06-integrations/ls-openapi/03-stock/14-37d22d4d.md``.  This module is
the smaller consumption contract used by Accounting/Portfolio: it keeps the
fields that explain cash, settlement, holdings, costs, returns, orders and
credit/margin capacity, while removing account numbers, passwords and names.

Broker observations are never the official ledger or an official NAV.  They
are independent read-only evidence used for reconciliation and reporting.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


TR_CODES = (
    "CDPCQ04700",
    "CSPAQ00600",
    "CSPAQ12200",
    "CSPAQ12300",
    "CSPAQ13700",
    "CSPAQ22200",
    "CSPBQ00200",
    "FOCCQ33600",
    "t0150",
    "t0151",
    "t0424",
    "t0425",
)

ACCOUNT_LEVEL_TR_CODES = tuple(
    code for code in TR_CODES if code not in {"CSPAQ00600", "CSPBQ00200"}
)
PARAMETERIZED_TR_CODES = {
    "CSPAQ00600": ("loan_detail_class", "symbol", "order_price"),
    "CSPBQ00200": ("side", "symbol", "order_price"),
}

TR_NAMES = {
    "CDPCQ04700": "계좌 거래내역",
    "CSPAQ00600": "계좌별신용한도조회",
    "CSPAQ12200": "현물계좌예수금 주문가능금액 총평가 조회",
    "CSPAQ12300": "BEP단가조회",
    "CSPAQ13700": "현물계좌 주문체결내역 조회(API)",
    "CSPAQ22200": "현물계좌예수금 주문가능금액 총평가2",
    "CSPBQ00200": "현물계좌증거금률별주문가능수량조회",
    "FOCCQ33600": "주식계좌 기간별수익률 상세",
    "t0150": "주식당일매매일지/수수료",
    "t0151": "주식당일매매일지/수수료(전일)",
    "t0424": "주식잔고2",
    "t0425": "주식체결/미체결",
}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _number(value: Any) -> str | None:
    """Return an exact, non-exponent decimal string; never coerce bad data to 0."""

    text = _text(value)
    if text is None:
        return None
    try:
        parsed = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    rendered = format(parsed, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered or "0"


def _decimal(value: Any) -> Decimal | None:
    number = _number(value)
    return Decimal(number) if number is not None else None


def _date(value: Any) -> str | None:
    text = _text(value)
    if text and len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return text


def _symbol(value: Any) -> str | None:
    text = _text(value)
    if text and len(text) > 6 and text[0] in {"A", "J"}:
        return text[1:]
    return text


def _mask_account(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    return "****" + text[-4:] if len(text) > 4 else "****"


def _rows(body: Mapping[str, Any], block: str) -> list[Mapping[str, Any]]:
    value = body.get(block)
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _block(body: Mapping[str, Any], block: str) -> Mapping[str, Any]:
    value = body.get(block)
    return value if isinstance(value, Mapping) else {}


def _entry(
    responses: Mapping[str, Any], tr_code: str
) -> tuple[Mapping[str, Any], dict[str, Any]]:
    raw = responses.get(tr_code)
    if not isinstance(raw, Mapping):
        required = PARAMETERIZED_TR_CODES.get(tr_code)
        status = "NEEDS_PARAMETERS" if required else "UNAVAILABLE"
        return {}, {
            "status": status,
            "pages": 0,
            "complete": False,
            "error": None,
            "required_parameters": list(required or ()),
        }

    body = raw.get("body") if isinstance(raw.get("body"), Mapping) else raw
    error = _text(raw.get("error"))
    meta = raw.get("meta") if isinstance(raw.get("meta"), Mapping) else {}
    rsp_cd = _text(body.get("rsp_cd"))
    output_suffixes = (
        "OutBlock",
        "OutBlock1",
        "OutBlock2",
        "OutBlock3",
        "OutBlock4",
        "OutBlock5",
    )
    has_output = any(str(key).endswith(output_suffixes) for key in body)
    # PAPER의 정상 CSPAQ/CDPCQ 응답은 실측상 rsp_cd=00136("조회 완료")다.
    # 성공 코드를 하드코딩하지 않고 출력 블록 존재 여부로 성공을 판별한다.
    if not error and not has_output and rsp_cd not in {None, "0000", "00000"}:
        error = _text(body.get("rsp_msg")) or f"LS rsp_cd={rsp_cd}"
    status = "ERROR" if error else ("OK" if has_output else "EMPTY")
    return body, {
        "status": status,
        "pages": int(meta.get("pages") or (1 if body else 0)),
        "complete": bool(meta.get("complete", True if body else False)),
        "truncated": bool(meta.get("truncated", False)),
        "rsp_cd": rsp_cd,
        "rsp_msg": _text(body.get("rsp_msg")),
        "error": error,
        "required_parameters": list(PARAMETERIZED_TR_CODES.get(tr_code, ())),
    }


def _first(blocks: list[Mapping[str, Any]], field: str) -> str | None:
    for block in blocks:
        value = _number(block.get(field))
        if value is not None:
            return value
    return None


def _account_summary(bodies: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    b123 = _block(bodies.get("CSPAQ12300", {}), "CSPAQ12300OutBlock2")
    b122 = _block(bodies.get("CSPAQ12200", {}), "CSPAQ12200OutBlock2")
    b222 = _block(bodies.get("CSPAQ22200", {}), "CSPAQ22200OutBlock2")
    preferred = [b123, b122, b222]
    return {
        "cash": {
            "deposit": _first(preferred, "Dps"),
            "d1_deposit": _first(preferred, "D1Dps"),
            "d2_deposit": _first(preferred, "D2Dps"),
            "withdrawable": _first(preferred, "MnyoutAbleAmt"),
            "cash_orderable": _first(preferred, "MnyOrdAbleAmt"),
            "substitute_orderable": _first(preferred, "SubstOrdAbleAmt"),
        },
        "valuation": {
            "deposit_assets_total": _first(preferred, "DpsastTotamt"),
            "balance_value": _first(preferred, "BalEvalAmt"),
            "purchase_amount": _first(preferred, "PchsAmt"),
            "evaluation_pnl": _first(preferred, "EvalPnlSum"),
            "investment_principal": _first(preferred, "InvstOrgAmt"),
            "investment_pnl": _first(preferred, "InvstPlAmt"),
            "pnl_rate": _first(preferred, "PnlRat"),
        },
        "settlement": {
            "previous_sell_adjustment": _first(preferred, "PrdaySellAdjstAmt"),
            "previous_buy_adjustment": _first(preferred, "PrdayBuyAdjstAmt"),
            "current_sell_adjustment": _first(preferred, "CrdaySellAdjstAmt"),
            "current_buy_adjustment": _first(preferred, "CrdayBuyAdjstAmt"),
            "d1_expected_settlement": _first(preferred, "D1SettPrergAmt"),
            "d2_expected_settlement": _first(preferred, "D2SettPrergAmt"),
            "d1_commission": _first(preferred, "D1CmsnAmt"),
            "d2_commission": _first(preferred, "D2CmsnAmt"),
            "d1_tax": _first(preferred, "D1EvrTax"),
            "d2_tax": _first(preferred, "D2EvrTax"),
        },
        "margin_credit": {
            "receivable": _first(preferred, "RcvblAmt"),
            "loan_amount": _first(preferred, "MloanAmt") or _first(preferred, "LoanAmt"),
            "credit_orderable": _first(preferred, "CrdtOrdAbleAmt"),
            "credit_collateral_order": _first(preferred, "CrdtPldgOrdAmt"),
            "required_collateral": _first(preferred, "RqrdPldgAmt"),
            "collateral_shortfall": _first(preferred, "PdlckAmt"),
            "post_change_collateral_ratio": _first(preferred, "ChgAfPldgRat"),
            "no_receivable_orderable": _first(preferred, "RcvblUablOrdAbleAmt"),
        },
        "source_trs": ["CSPAQ12300", "CSPAQ12200", "CSPAQ22200"],
    }


def _cash_cross_checks(bodies: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    named = {
        "CSPAQ12200": _block(bodies.get("CSPAQ12200", {}), "CSPAQ12200OutBlock2"),
        "CSPAQ22200": _block(bodies.get("CSPAQ22200", {}), "CSPAQ22200OutBlock2"),
        "CSPAQ12300": _block(bodies.get("CSPAQ12300", {}), "CSPAQ12300OutBlock2"),
    }
    checks: list[dict[str, Any]] = []
    for field, label in (
        ("Dps", "deposit"),
        ("D1Dps", "d1_deposit"),
        ("D2Dps", "d2_deposit"),
        ("MnyOrdAbleAmt", "cash_orderable"),
        ("RcvblAmt", "receivable"),
    ):
        values = {
            tr: number
            for tr, block in named.items()
            if (number := _number(block.get(field))) is not None
        }
        if len(values) < 2:
            continue
        decimals = {tr: Decimal(value) for tr, value in values.items()}
        first = next(iter(decimals.values()))
        checks.append(
            {
                "field": label,
                "values": values,
                "match": all(value == first for value in decimals.values()),
                "max_difference": _number(max(decimals.values()) - min(decimals.values())),
            }
        )
    return checks


def _positions(body: Mapping[str, Any], row_limit: int) -> list[dict[str, Any]]:
    result = []
    for row in _rows(body, "CSPAQ12300OutBlock3")[:row_limit]:
        result.append(
            {
                "symbol": _symbol(row.get("IsuNo")),
                "name": _text(row.get("IsuNm")),
                "market_code": _text(row.get("RegMktCode")),
                "security_balance_type": _text(row.get("SecBalPtnNm")),
                "quantity": _number(row.get("BalQty")),
                "sellable_quantity": _number(row.get("SellAbleQty")),
                "unit_cost_bep": _number(row.get("AvrUprc")),
                "current_price": _number(row.get("NowPrc")),
                "purchase_amount": _number(row.get("PchsAmt")),
                "market_value": _number(row.get("BalEvalAmt")),
                "unrealized_pnl": _number(row.get("EvalPnl")),
                "pnl_rate": _number(row.get("PnlRat")),
                "realized_sell_pnl": _number(row.get("SellPnlAmt")),
                "unexecuted_quantity": _number(row.get("UnercQty")),
                "unsettled_quantity": _number(row.get("UnsttQty")),
                "credit_amount": _number(row.get("CrdtAmt")),
                "loan_date": _date(row.get("LoanDt")),
                "due_date": _date(row.get("DueDt")),
                "source_tr": "CSPAQ12300",
                "cost_basis_mode": "BEP",
            }
        )
    return result


def _position_check(body: Mapping[str, Any], row_limit: int) -> list[dict[str, Any]]:
    result = []
    for row in _rows(body, "t0424OutBlock1")[:row_limit]:
        result.append(
            {
                "symbol": _symbol(row.get("expcode")),
                "name": _text(row.get("hname")),
                "quantity": _number(row.get("janqty")),
                "sellable_quantity": _number(row.get("mdposqt")),
                "average_unit_price": _number(row.get("pamt")),
                "current_price": _number(row.get("price")),
                "purchase_amount": _number(row.get("mamt")),
                "market_value": _number(row.get("appamt")),
                "unrealized_pnl": _number(row.get("dtsunik")),
                "pnl_rate": _number(row.get("sunikrt")),
                "fee": _number(row.get("fee")),
                "tax": _number(row.get("tax")),
                "credit_interest": _number(row.get("sininter")),
                "source_tr": "t0424",
                "cost_basis_mode": "AVERAGE",
            }
        )
    return result


def _position_reconciliation(
    positions: list[dict[str, Any]], checks: list[dict[str, Any]]
) -> dict[str, Any]:
    primary = {row.get("symbol"): row for row in positions if row.get("symbol")}
    secondary = {row.get("symbol"): row for row in checks if row.get("symbol")}
    discrepancies = []
    for symbol in sorted(set(primary) | set(secondary)):
        left = _decimal((primary.get(symbol) or {}).get("quantity"))
        right = _decimal((secondary.get(symbol) or {}).get("quantity"))
        if left is None or right is None or left != right:
            discrepancies.append(
                {
                    "symbol": symbol,
                    "cspaq12300_quantity": _number(left),
                    "t0424_quantity": _number(right),
                    "difference": _number(left - right) if left is not None and right is not None else None,
                }
            )
    return {
        "status": "MATCH" if not discrepancies else "BREAK",
        "discrepancies": discrepancies,
        "compared_symbols": len(set(primary) | set(secondary)),
        "source_trs": ["CSPAQ12300", "t0424"],
    }


def _trade_journal(body: Mapping[str, Any], prefix: str, row_limit: int) -> dict[str, Any]:
    summary = _block(body, f"{prefix}OutBlock")
    rows = []
    for row in _rows(body, f"{prefix}OutBlock1")[:row_limit]:
        rows.append(
            {
                "side": _text(row.get("medosu")),
                "symbol": _symbol(row.get("expcode")),
                "quantity": _number(row.get("qty")),
                "price": _number(row.get("price")),
                "contract_amount": _number(row.get("amt")),
                "commission": _number(row.get("fee")),
                "transaction_tax": _number(row.get("tax")),
                "agricultural_tax": _number(row.get("argtax")),
                "settlement_amount": _number(row.get("adjamt")),
                "channel": _text(row.get("middiv")),
            }
        )
    return {
        "summary": {
            "sell_quantity": _number(summary.get("mdqty")),
            "sell_contract_amount": _number(summary.get("mdamt")),
            "sell_commission": _number(summary.get("mdfee")),
            "sell_total_cost": _number(summary.get("tmdtax")),
            "sell_settlement": _number(summary.get("mdadjamt")),
            "buy_quantity": _number(summary.get("msqty")),
            "buy_contract_amount": _number(summary.get("msamt")),
            "buy_commission": _number(summary.get("msfee")),
            "buy_total_cost": _number(summary.get("tmstax")),
            "buy_settlement": _number(summary.get("msadjamt")),
            "total_quantity": _number(summary.get("tqty")),
            "total_contract_amount": _number(summary.get("tamt")),
            "total_commission": _number(summary.get("tfee")),
            "total_transaction_tax": _number(summary.get("tottax")),
            "total_agricultural_tax": _number(summary.get("targtax")),
            "total_cost": _number(summary.get("ttax")),
            "total_settlement": _number(summary.get("tadjamt")),
        },
        "rows": rows,
        "source_tr": prefix,
    }


def _settled_transactions(body: Mapping[str, Any], row_limit: int) -> dict[str, Any]:
    aggregate = _block(body, "CDPCQ04700OutBlock4")
    flows = _block(body, "CDPCQ04700OutBlock5")
    rows = []
    for row in _rows(body, "CDPCQ04700OutBlock3")[:row_limit]:
        rows.append(
            {
                "trade_date": _date(row.get("TrdDt")),
                "trade_no": _number(row.get("TrdNo")),
                "category": _text(row.get("TpCodeNm")),
                "summary": _text(row.get("SmryNm")),
                "symbol": _symbol(row.get("IsuNo")),
                "name": _text(row.get("IsuNm")),
                "quantity": _number(row.get("TrdQty")),
                "unit_price": _number(row.get("TrdUprc")),
                "trade_amount": _number(row.get("TrdAmt")),
                "settlement_amount": _number(row.get("AdjstAmt")),
                "commission": _number(row.get("CmsnAmt")),
                "tax_total": _number(row.get("TaxSumAmt")),
                "realized_pnl": _number(row.get("BnsplAmt")),
                "dividend": _number(row.get("MnyDvdAmt")),
                "interest_fee": _number(row.get("IntrstUtlfee")),
                "loan_interest": _number(row.get("LoanIntrstAmt")),
                "cash_before": _number(row.get("DpsBfbalAmt")),
                "cash_after": _number(row.get("DpsCrbalAmt")),
                "currency": _text(row.get("CrcyCode")),
            }
        )
    return {
        "summary": {
            "pnl": _number(aggregate.get("PnlSumAmt")),
            "contract_total": _number(aggregate.get("CtrctAsm")),
            "commission_total": _number(aggregate.get("CmsnAmtSumAmt")),
            "cash_in": _number(flows.get("MnyinAmt")),
            "cash_out": _number(flows.get("MnyoutAmt")),
            "securities_in": _number(flows.get("SecinAmt")),
            "securities_out": _number(flows.get("SecoutAmt")),
            "sell_amount": _number(flows.get("SellAmt")),
            "buy_amount": _number(flows.get("BuyAmt")),
            "sell_commission": _number(flows.get("SellCmsn")),
            "buy_commission": _number(flows.get("BuyCmsn")),
            "tax": _number(flows.get("EvrTax")) or _number(flows.get("ExecTax")),
        },
        "rows": rows,
        "source_tr": "CDPCQ04700",
    }


def _order_history(body: Mapping[str, Any], row_limit: int) -> dict[str, Any]:
    summary = _block(body, "CSPAQ13700OutBlock2")
    rows = []
    for row in _rows(body, "CSPAQ13700OutBlock3")[:row_limit]:
        rows.append(
            {
                "order_date": _date(row.get("OrdDt")),
                "order_no": _number(row.get("OrdNo")),
                "original_order_no": _number(row.get("OrgOrdNo")),
                "symbol": _symbol(row.get("IsuNo")),
                "name": _text(row.get("IsuNm")),
                "side": _text(row.get("BnsTpNm")) or _text(row.get("BnsTpCode")),
                "order_type": _text(row.get("OrdPtnNm")),
                "amend_cancel_type": _text(row.get("MrcTpNm")),
                "order_quantity": _number(row.get("OrdQty")),
                "order_price": _number(row.get("OrdPrc")),
                "executed_quantity": _number(row.get("ExecQty")),
                "executed_price": _number(row.get("ExecPrc")),
                "execution_time": _text(row.get("ExecTrxTime")),
                "last_execution_time": _text(row.get("LastExecTime")),
                "order_time": _text(row.get("OrdTime")),
                "channel": _text(row.get("CommdaNm")),
            }
        )
    return {
        "summary": {
            "sell_executed_amount": _number(summary.get("SellExecAmt")),
            "buy_executed_amount": _number(summary.get("BuyExecAmt")),
            "sell_executed_quantity": _number(summary.get("SellExecQty")),
            "buy_executed_quantity": _number(summary.get("BuyExecQty")),
            "sell_order_quantity": _number(summary.get("SellOrdQty")),
            "buy_order_quantity": _number(summary.get("BuyOrdQty")),
        },
        "rows": rows,
        "source_tr": "CSPAQ13700",
    }


def _execution_status(body: Mapping[str, Any], row_limit: int) -> dict[str, Any]:
    summary = _block(body, "t0425OutBlock")
    rows = []
    for row in _rows(body, "t0425OutBlock1")[:row_limit]:
        rows.append(
            {
                "order_no": _number(row.get("ordno")),
                "original_order_no": _number(row.get("orgordno")),
                "symbol": _symbol(row.get("expcode")),
                "side": _text(row.get("medosu")),
                "order_quantity": _number(row.get("qty")),
                "order_price": _number(row.get("price")),
                "executed_quantity": _number(row.get("cheqty")),
                "executed_price": _number(row.get("cheprice")),
                "unexecuted_quantity": _number(row.get("ordrem")),
                "status": _text(row.get("status")),
                "order_type": _text(row.get("ordgb")),
                "order_time": _text(row.get("ordtime")),
                "channel": _text(row.get("ordermtd")),
                "exchange": _text(row.get("exchname")),
            }
        )
    return {
        "summary": {
            "total_order_quantity": _number(summary.get("tqty")),
            "total_executed_quantity": _number(summary.get("tcheqty")),
            "total_unexecuted_quantity": _number(summary.get("tordrem")),
            "estimated_commission": _number(summary.get("cmss")),
            "total_order_amount": _number(summary.get("tamt")),
            "total_sell_executed_amount": _number(summary.get("tmdamt")),
            "total_buy_executed_amount": _number(summary.get("tmsamt")),
            "estimated_tax": _number(summary.get("tax")),
        },
        "rows": rows,
        "source_tr": "t0425",
    }


def _performance(body: Mapping[str, Any], row_limit: int) -> dict[str, Any]:
    summary = _block(body, "FOCCQ33600OutBlock2")
    series = []
    for row in _rows(body, "FOCCQ33600OutBlock3")[:row_limit]:
        series.append(
            {
                "date": _date(row.get("BaseDt")),
                "opening_value": _number(row.get("FdEvalAmt")),
                "closing_value": _number(row.get("EotEvalAmt")),
                "average_investment_principal": _number(row.get("InvstAvrbalPramt")),
                "contract_amount": _number(row.get("BnsctrAmt")),
                "cash_and_securities_in": _number(row.get("MnyinSecinAmt")),
                "cash_and_securities_out": _number(row.get("MnyoutSecoutAmt")),
                "evaluation_pnl": _number(row.get("EvalPnlAmt")),
                "return_rate": _number(row.get("TermErnrat")),
                "index": _number(row.get("Idx")),
            }
        )
    return {
        "summary": {
            "contract_amount": _number(summary.get("BnsctrAmt")),
            "cash_in": _number(summary.get("MnyinAmt")),
            "cash_out": _number(summary.get("MnyoutAmt")),
            "average_investment_principal": _number(summary.get("InvstAvrbalPramt")),
            "investment_pnl": _number(summary.get("InvstPlAmt")),
            "return_rate": _number(summary.get("InvstErnrat")),
        },
        "series": series,
        "source_tr": "FOCCQ33600",
    }


def _credit_limit(body: Mapping[str, Any]) -> dict[str, Any]:
    if not body:
        return {
            "status": "NEEDS_PARAMETERS",
            "required_parameters": list(PARAMETERIZED_TR_CODES["CSPAQ00600"]),
            "source_tr": "CSPAQ00600",
        }
    block = _block(body, "CSPAQ00600OutBlock2")
    return {
        "status": "OK" if block else "EMPTY",
        "loan_limit": _number(block.get("MktcplMloanLmtAmt")),
        "loan_used": _number(block.get("MktcplMloanAmtSum")),
        "short_sale_limit": _number(block.get("SloanLmtAmt")),
        "short_sale_used": _number(block.get("SloanAmtSum")),
        "collateral_maintenance_ratio": _number(block.get("PldgMaintRat")),
        "collateral_ratio": _number(block.get("PldgRat")),
        "deposit_assets": _number(block.get("DpsastSum")),
        "orderable_amount": _number(block.get("OrdAbleAmt")),
        "orderable_quantity": _number(block.get("OrdAbleQty")),
        "no_receivable_orderable_quantity": _number(block.get("RcvblUablOrdAbleQty")),
        "source_tr": "CSPAQ00600",
    }


def _margin_capacity(body: Mapping[str, Any]) -> dict[str, Any]:
    if not body:
        return {
            "status": "NEEDS_PARAMETERS",
            "required_parameters": list(PARAMETERIZED_TR_CODES["CSPBQ00200"]),
            "source_tr": "CSPBQ00200",
        }
    block = _block(body, "CSPBQ00200OutBlock2")
    return {
        "status": "OK" if block else "EMPTY",
        "symbol": _symbol(_block(body, "CSPBQ00200OutBlock1").get("IsuNo")),
        "name": _text(block.get("IsuNm")),
        "cash_orderable_amount": _number(block.get("MnyOrdAbleAmt")),
        "withdrawable_amount": _number(block.get("MnyoutAbleAmt")),
        "commission_rate": _number(block.get("CmsnRat")),
        "commission": _number(block.get("Cmsn")),
        "firm_margin_rate": _number(block.get("FirmMgnRat")),
        "instrument_margin_rate": _number(block.get("IsuMgnRat")),
        "account_margin_rate": _number(block.get("AcntMgnRat")),
        "trade_margin_rate": _number(block.get("TrdMgnrt")),
        "orderable_amount": _number(block.get("OrdAbleAmt")),
        "orderable_quantity": _number(block.get("OrdAbleQty")),
        "margin_20_orderable_quantity": _number(block.get("MgnRat20OrdAbleQty")),
        "margin_30_orderable_quantity": _number(block.get("MgnRat30OrdAbleQty")),
        "margin_40_orderable_quantity": _number(block.get("MgnRat40OrdAbleQty")),
        "margin_100_orderable_quantity": _number(block.get("MgnRat100OrdAbleQty")),
        "source_tr": "CSPBQ00200",
    }


def normalize_ls_accounting_evidence(
    responses: Mapping[str, Any],
    *,
    period_start: date | str,
    period_end: date | str,
    previous_date: date | str,
    environment: str,
    as_of: datetime | None = None,
    row_limit: int = 100,
) -> dict[str, Any]:
    """Build the only LS broker payload that may be attached to Accounting tasks."""

    bodies: dict[str, Mapping[str, Any]] = {}
    coverage: dict[str, Any] = {}
    for tr_code in TR_CODES:
        body, metadata = _entry(responses, tr_code)
        bodies[tr_code] = body
        coverage[tr_code] = {"name": TR_NAMES[tr_code], **metadata}

    account_candidates = (
        _block(bodies["CSPAQ12300"], "CSPAQ12300OutBlock1").get("AcntNo"),
        _block(bodies["CSPAQ12200"], "CSPAQ12200OutBlock1").get("AcntNo"),
        _block(bodies["CDPCQ04700"], "CDPCQ04700OutBlock1").get("AcntNo"),
    )
    masked_account = next(
        (masked for value in account_candidates if (masked := _mask_account(value))), None
    )

    positions = _positions(bodies["CSPAQ12300"], row_limit)
    position_check = _position_check(bodies["t0424"], row_limit)
    position_reconciliation = _position_reconciliation(positions, position_check)
    cash_checks = _cash_cross_checks(bodies)

    exceptions: list[dict[str, Any]] = []
    if position_reconciliation["status"] == "BREAK":
        exceptions.append(
            {
                "kind": "BROKER_POSITION_TR_MISMATCH",
                "severity": "REVIEW",
                "detail": position_reconciliation["discrepancies"],
                "source_trs": ["CSPAQ12300", "t0424"],
            }
        )
    mismatched_cash = [check for check in cash_checks if not check["match"]]
    if mismatched_cash:
        exceptions.append(
            {
                "kind": "BROKER_CASH_TR_MISMATCH",
                "severity": "REVIEW",
                "detail": mismatched_cash,
                "source_trs": ["CSPAQ12200", "CSPAQ12300", "CSPAQ22200"],
            }
        )
    failed = [
        code
        for code, status in coverage.items()
        if status["status"] in {"ERROR", "UNAVAILABLE", "EMPTY"}
        and code in ACCOUNT_LEVEL_TR_CODES
    ]
    if failed:
        exceptions.append(
            {
                "kind": "BROKER_EVIDENCE_INCOMPLETE",
                "severity": "DATA_QUALITY",
                "missing_or_failed_trs": failed,
            }
        )

    account_summary = _account_summary(bodies)
    activity = {
        "settled_period": _settled_transactions(bodies["CDPCQ04700"], row_limit),
        "today": _trade_journal(bodies["t0150"], "t0150", row_limit),
        "previous_day": _trade_journal(bodies["t0151"], "t0151", row_limit),
        "order_history": _order_history(bodies["CSPAQ13700"], row_limit),
        "execution_status": _execution_status(bodies["t0425"], row_limit),
    }
    performance = _performance(bodies["FOCCQ33600"], row_limit)
    observed_at = as_of or datetime.now(timezone.utc)
    evidence_refs = [f"ls-tr:{code}" for code, status in coverage.items() if status["status"] == "OK"]

    return {
        "schema_version": "accounting.broker-evidence.v1",
        "as_of": observed_at.astimezone(timezone.utc).isoformat(),
        "environment": str(environment).strip().upper(),
        "source": "LS OPEN API /stock/accno",
        "account": {"masked": masked_account},
        "period": {
            "start": str(period_start),
            "end": str(period_end),
            "previous_date": str(previous_date),
        },
        "coverage": coverage,
        "account_summary": account_summary,
        "account_cross_checks": cash_checks,
        "positions": positions,
        "position_check": position_check,
        "position_reconciliation": position_reconciliation,
        "activity": activity,
        "performance": performance,
        "credit_limit": _credit_limit(bodies["CSPAQ00600"]),
        "margin_capacity": _margin_capacity(bodies["CSPBQ00200"]),
        "exceptions": exceptions,
        "reporting_view": {
            "liquidity": account_summary["cash"],
            "valuation": account_summary["valuation"],
            "settlement": account_summary["settlement"],
            "margin_credit": account_summary["margin_credit"],
            "today_costs": activity["today"]["summary"],
            "previous_day_costs": activity["previous_day"]["summary"],
            "period_performance": performance["summary"],
            "open_order_summary": activity["execution_status"]["summary"],
            "exception_count": len(exceptions),
        },
        "evidence_refs": evidence_refs,
        "authoritative": False,
        "is_official": False,
        "usage": "reconciliation_and_reporting_evidence_only",
        "official_nav_source": "/accounting/v1/ledgers/{ledger_id}/advisory-snapshot",
        "field_reference": "docs/06-integrations/ls-openapi/03-stock/14-37d22d4d.md",
    }


__all__ = [
    "ACCOUNT_LEVEL_TR_CODES",
    "PARAMETERIZED_TR_CODES",
    "TR_CODES",
    "TR_NAMES",
    "normalize_ls_accounting_evidence",
]
