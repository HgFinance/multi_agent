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
        return max(0.1, min(10.0, float(os.getenv("ACCOUNTING_ADVISORY_SNAPSHOT_TIMEOUT_SECONDS", "5"))))
    except ValueError:
        return 5.0


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
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=_timeout_seconds()) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return _compact_snapshot(payload)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None


__all__ = ["fetch_accounting_advisory_context"]
