"""Best-effort, read-only portfolio context for Risk advisory tasks.

This is deliberately an observability/advisory enrichment boundary, not an
authorization or execution dependency.  A missing book marker, unavailable
accounting API, malformed response, or timeout returns ``None`` so the
existing Risk workflow can continue and state its data limitation.
"""

from __future__ import annotations

import json
import os
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
        return _compact_snapshot(payload)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        urllib.error.URLError,
    ):
        return None


__all__ = ["fetch_risk_advisory_context"]
