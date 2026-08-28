"""Best-effort, read-only accounting snapshot context for Accounting/Portfolio tasks.

Mirrors ``orchestration/risk_advisory_context.py``. The accounting-portfolio
department's head Hermes agent has no shell/terminal tool (deliberately -
see departments/05-accounting-portfolio/hermes/config.yaml's
platform_toolsets comment: shell access on this profile would open a path to
edit Posted Journals directly). So it cannot fetch its own evidence; the
confirmed snapshot is fetched here, server-side, and attached to the task
body instead.

Deliberately an observability/advisory enrichment boundary, not an
authorization or execution dependency: an unavailable BFF, malformed
response, or timeout returns ``None`` so the existing workflow can continue
and the department states its own data limitation rather than this helper
fabricating one.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from uuid import UUID


def _timeout_seconds() -> float:
    try:
        return max(0.1, min(10.0, float(os.getenv("ACCOUNTING_ADVISORY_SNAPSHOT_TIMEOUT_SECONDS", "2"))))
    except ValueError:
        return 2.0


def _book_id() -> str | None:
    raw = os.getenv("ACCOUNTING_ADVISORY_BOOK_ID", "").strip()
    if not raw:
        return None
    try:
        return str(UUID(raw))
    except ValueError:
        return None


def _compact_snapshot(payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return None
    portfolio = payload.get("portfolio")
    if not isinstance(portfolio, dict):
        return None
    positions = portfolio.get("positions")
    if not isinstance(positions, list):
        positions = []
    safe_positions = []
    for item in positions[:50]:
        if not isinstance(item, dict):
            continue
        safe_positions.append(
            {
                key: item[key]
                for key in (
                    "instrument_id",
                    "symbol",
                    "display_name",
                    "quantity",
                    "trade_basis_quantity",
                    "market_value",
                    "unrealized_pnl",
                    "weight",
                )
                if key in item
            }
        )
    context = {
        "contract": "hgfinance.accounting-advisory-portfolio.v1",
        "source_of_record": payload.get("source_of_record"),
        "authoritative": bool(payload.get("authoritative", False)),
        "as_of": portfolio.get("as_of"),
        "quality_status": portfolio.get("quality_status"),
        "nav": portfolio.get("nav"),
        "cash": portfolio.get("cash"),
        "securities_value": portfolio.get("securities_value"),
        "realized_pnl": portfolio.get("realized_pnl"),
        "unrealized_pnl": portfolio.get("unrealized_pnl"),
        "fees": portfolio.get("fees"),
        "taxes": portfolio.get("taxes"),
        "positions": safe_positions,
    }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _safe_dict(value: Any, keys: tuple[str, ...]) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    return {key: value[key] for key in keys if key in value}


def _safe_rows(value: Any, keys: tuple[str, ...], limit: int = 25) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [_safe_dict(row, keys) for row in value[:limit] if isinstance(row, dict)]


def _compact_broker(payload: Any) -> dict[str, Any] | None:
    """Whitelist a bounded subset of the already credential-free BFF contract."""

    if not isinstance(payload, dict) or payload.get("schema_version") != "accounting.broker-evidence.v1":
        return None
    coverage = payload.get("coverage") if isinstance(payload.get("coverage"), dict) else {}
    safe_coverage = {
        str(code): _safe_dict(
            status,
            (
                "name",
                "status",
                "pages",
                "complete",
                "truncated",
                "rsp_cd",
                "rsp_msg",
                "error",
                "required_parameters",
            ),
        )
        for code, status in coverage.items()
        if isinstance(status, dict)
    }
    activity = payload.get("activity") if isinstance(payload.get("activity"), dict) else {}
    safe_activity: dict[str, Any] = {}
    activity_row_keys = (
        "trade_date",
        "order_date",
        "trade_no",
        "order_no",
        "original_order_no",
        "category",
        "summary",
        "symbol",
        "name",
        "side",
        "status",
        "order_type",
        "quantity",
        "order_quantity",
        "executed_quantity",
        "unexecuted_quantity",
        "unit_price",
        "price",
        "order_price",
        "executed_price",
        "trade_amount",
        "contract_amount",
        "settlement_amount",
        "commission",
        "tax_total",
        "transaction_tax",
        "agricultural_tax",
        "realized_pnl",
        "dividend",
        "interest_fee",
        "loan_interest",
        "cash_before",
        "cash_after",
        "execution_time",
        "order_time",
        "channel",
        "currency",
    )
    for name in ("settled_period", "today", "previous_day", "order_history", "execution_status"):
        section = activity.get(name)
        if not isinstance(section, dict):
            continue
        safe_activity[name] = {
            "summary": section.get("summary") if isinstance(section.get("summary"), dict) else {},
            "rows": _safe_rows(section.get("rows"), activity_row_keys),
            "source_tr": section.get("source_tr"),
        }

    performance = payload.get("performance") if isinstance(payload.get("performance"), dict) else {}
    position_keys = (
        "symbol",
        "name",
        "market_code",
        "security_balance_type",
        "quantity",
        "sellable_quantity",
        "unit_cost_bep",
        "average_unit_price",
        "current_price",
        "purchase_amount",
        "market_value",
        "unrealized_pnl",
        "pnl_rate",
        "realized_sell_pnl",
        "unexecuted_quantity",
        "unsettled_quantity",
        "credit_amount",
        "loan_date",
        "due_date",
        "fee",
        "tax",
        "credit_interest",
        "source_tr",
        "cost_basis_mode",
    )
    return {
        "schema_version": payload.get("schema_version"),
        "as_of": payload.get("as_of"),
        "environment": payload.get("environment"),
        "source": payload.get("source"),
        "account": _safe_dict(payload.get("account"), ("masked",)),
        "period": _safe_dict(payload.get("period"), ("start", "end", "previous_date")),
        "coverage": safe_coverage,
        "account_summary": payload.get("account_summary"),
        "account_cross_checks": payload.get("account_cross_checks"),
        "positions": _safe_rows(payload.get("positions"), position_keys, limit=50),
        "position_check": _safe_rows(payload.get("position_check"), position_keys, limit=50),
        "position_reconciliation": payload.get("position_reconciliation"),
        # This is intentionally an invariant of the evidence contract.  The
        # Accounting Hermes must not manufacture a second Engine↔broker break
        # by subtracting snapshots with different as-of/settlement bases.
        "position_reconciliation_scope": "broker_internal_only",
        "position_reconciliation_note": (
            "Use only the deterministic broker comparison above: "
            "CSPAQ12300 매매기준 보유수량 vs t0424 체결기준 잔고수량. "
            "Accounting Engine 포지션과 증권사 포지션을 직접 비교해 새 대사 차이를 만들지 말고, "
            "별도 결정론적 차이 자료가 있을 때만 보고한다."
        ),
        "activity": safe_activity,
        "performance": {
            "summary": performance.get("summary") if isinstance(performance.get("summary"), dict) else {},
            "series": _safe_rows(
                performance.get("series"),
                (
                    "date",
                    "opening_value",
                    "closing_value",
                    "average_investment_principal",
                    "contract_amount",
                    "cash_and_securities_in",
                    "cash_and_securities_out",
                    "evaluation_pnl",
                    "return_rate",
                    "index",
                ),
                limit=40,
            ),
            "source_tr": performance.get("source_tr"),
        },
        "credit_limit": payload.get("credit_limit"),
        "margin_capacity": payload.get("margin_capacity"),
        "exceptions": payload.get("exceptions") if isinstance(payload.get("exceptions"), list) else [],
        "reporting_view": payload.get("reporting_view"),
        "evidence_refs": payload.get("evidence_refs"),
        "authoritative": False,
        "is_official": False,
        "usage": "reconciliation_and_reporting_evidence_only",
        "official_nav_source": payload.get("official_nav_source"),
    }


def _fetch_json(url: str) -> Any:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_accounting_advisory_context(fund_id: str | None = None) -> str | None:
    """Fetch one bounded snapshot for an Accounting/Portfolio primary."""

    # Keep ``fund_id`` only for caller compatibility. Accounting authority is
    # the pinned PAPER book, never the scripted dashboard fixture.
    del fund_id
    book_id = _book_id()
    if not book_id:
        return None
    base_url = os.getenv("ACCOUNTING_API_URL", "http://accounting-api:8000").strip().rstrip("/")
    if not base_url:
        return None
    url = f"{base_url}/accounting/v1/ledgers/{book_id}/advisory-snapshot"
    try:
        compact = _compact_snapshot(_fetch_json(url))
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None
    if compact is None:
        return None

    context = json.loads(compact)
    portfolio_bff_url = os.getenv(
        "PORTFOLIO_BFF_INTERNAL_URL", "http://portfolio-bff:8000"
    ).strip().rstrip("/")
    if portfolio_bff_url:
        try:
            broker = _compact_broker(
                _fetch_json(f"{portfolio_bff_url}/internal/accounting/broker-evidence")
            )
        except (
            OSError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
            urllib.error.URLError,
        ):
            broker = None
        if broker is not None:
            context["broker_evidence"] = broker
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


__all__ = ["fetch_accounting_advisory_context"]
