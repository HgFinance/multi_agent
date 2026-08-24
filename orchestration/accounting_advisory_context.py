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


def _timeout_seconds() -> float:
    try:
        return max(0.1, min(2.0, float(os.getenv("ACCOUNTING_ADVISORY_SNAPSHOT_TIMEOUT_SECONDS", "0.75"))))
    except ValueError:
        return 0.75


def _fund_id(explicit: str | None = None) -> str | None:
    if explicit and str(explicit).strip():
        return str(explicit).strip()
    # Same demo-wide canonical PAPER fund/book used by the LS reconciliation
    # loop (ACCOUNTING_DEFAULT_BOOK_ID) and the dashboard's own snapshot
    # panel. Not a secret - a fixture identifier for this single-tenant demo.
    return os.getenv("ACCOUNTING_ADVISORY_FUND_ID", "").strip() or None


def _user_id() -> str:
    return os.getenv(
        "ACCOUNTING_ADVISORY_USER_ID", "00000000-0000-4000-8000-00000000cec0"
    ).strip()


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
        "source_of_record": "/ui/snapshot",
        "authoritative": False,
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

    fund_id = _fund_id(fund_id)
    if not fund_id:
        return None
    base_url = os.getenv("PORTFOLIO_BFF_INTERNAL_URL", "http://portfolio-bff:8000").strip().rstrip("/")
    if not base_url:
        return None
    url = f"{base_url}/ui/snapshot?fund_id={fund_id}"
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "X-User-Id": _user_id()}
    )
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
