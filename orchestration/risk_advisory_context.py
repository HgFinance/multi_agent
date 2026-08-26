"""Best-effort, read-only portfolio context for Risk advisory tasks.

This is deliberately an observability/advisory enrichment boundary, not an
authorization or execution dependency.  A missing book marker, unavailable
accounting API, malformed response, or timeout returns ``None`` so the
existing Risk workflow can continue and state its data limitation.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Any
from uuid import UUID

from orchestration.ceo_workflow_scope import read_marker


def _timeout_seconds() -> float:
    try:
        return max(0.1, min(2.0, float(os.getenv("RISK_ADVISORY_SNAPSHOT_TIMEOUT_SECONDS", "0.75"))))
    except ValueError:
        return 0.75


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
                    "average_cost",
                    "mark_price",
                    "mark_as_of",
                    "market_value",
                    "weight",
                )
                if key in item
            }
        )
    context = {
        "contract": "hgfinance.risk-advisory-portfolio.v1",
        "source_of_record": payload.get("source_of_record"),
        "authoritative": bool(payload.get("authoritative", False)),
        "as_of": portfolio.get("as_of"),
        "quality_status": portfolio.get("quality_status"),
        "nav": portfolio.get("nav"),
        "cash": portfolio.get("cash"),
        "securities_value": portfolio.get("securities_value"),
        "gross_exposure": portfolio.get("gross_exposure"),
        "net_exposure": portfolio.get("net_exposure"),
        "positions": safe_positions,
        "sector_exposure": payload.get("sector_exposure") or {
            "status": "unavailable"
        },
    }
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def _normalized_text(value: object) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _mentioned_position(root_body: str, positions: list[dict[str, Any]]) -> dict[str, Any] | None:
    # A workflow root embeds the entire Accounting position list before the
    # user request. Searching that whole body would always match whichever
    # holding appears first (for example NAVER) even when the user asked about
    # SK하이닉스. Only the explicit request tail is valid mention evidence.
    request_marker = "## User request"
    query = root_body.split(request_marker, 1)[1] if request_marker in root_body else root_body
    body = _normalized_text(query)
    for item in positions:
        symbol = _normalized_text(item.get("symbol"))
        display_name = _normalized_text(item.get("display_name"))
        aliases = {symbol, display_name, re.sub(r"^[a-z]+", "", display_name)}
        if any(alias and len(alias) >= 3 and alias in body for alias in aliases):
            return item
    return None


def _market_snapshot(root_body: str, positions: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Fetch one exact-symbol canonical quote for the security in the request."""

    position = _mentioned_position(root_body, positions)
    symbol = str(position.get("symbol") or "") if position else ""
    if not symbol:
        return None
    base_url = os.getenv("MARKET_API_URL", "").strip().rstrip("/")
    if not base_url:
        return None
    request = urllib.request.Request(
        f"{base_url}/snapshot/{symbol}", headers={"Accept": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None
    if not isinstance(payload, dict) or str(payload.get("symbol") or "") != symbol:
        return None
    trade = payload.get("last_trade") if isinstance(payload.get("last_trade"), dict) else None
    quote = payload.get("last_quote") if isinstance(payload.get("last_quote"), dict) else None
    if trade is None and quote is None:
        return None
    return {
        "contract": "hgfinance.risk-advisory-market.v1",
        "source_of_record": "market-api",
        "authoritative": True,
        "instrument_id": position.get("instrument_id"),
        "symbol": symbol,
        "display_name": position.get("display_name"),
        "last_trade": trade,
        "last_quote": quote,
    }


def fetch_risk_advisory_context(root_body: str) -> str | None:
    """Fetch one bounded snapshot for a Risk primary, never raising upstream."""

    book_id = read_marker(root_body, "advisory_book_id")
    if not book_id:
        return None
    try:
        book_id = str(UUID(book_id))
    except (ValueError, AttributeError):
        return None

    base_url = os.getenv("ACCOUNTING_API_URL", "").strip().rstrip("/")
    if not base_url:
        return None
    url = f"{base_url}/accounting/v1/ledgers/{book_id}/advisory-snapshot"
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
        compact = _compact_snapshot(payload)
        if compact is None:
            return None
        context = json.loads(compact)
        positions = context.get("positions")
        if isinstance(positions, list):
            market = _market_snapshot(root_body, positions)
            if market is not None:
                context["market_snapshot"] = market
        return json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None


__all__ = ["fetch_risk_advisory_context"]
