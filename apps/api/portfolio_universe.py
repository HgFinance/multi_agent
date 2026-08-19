"""Portfolio recommendation universe projection.

The BFF exposes a small, read-only instrument catalog for the operator UI. It
does not rank securities or invent return forecasts. A live market/research
adapter can replace the TEST catalog later while keeping this response shape.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import ROUND_DOWN, Decimal
from pathlib import Path
from typing import Any

UNIVERSE_PATH = Path(__file__).with_name("portfolio_universes.json")
# Current product scope is domestic equities. Legacy mixed/bond catalogs are
# intentionally not accepted by the BFF projection.
DEFAULT_UNIVERSE_ID = "KOREA_EQUITY_WATCHLIST"
STOCK_ASSET_CLASSES = frozenset({"KOREA_EQUITY"})
DERIVATIVE_ASSET_CLASSES = frozenset({"LEVERAGED_ETF", "SHORT_EXPOSURE", "DERIVATIVES_HEDGE"})


def load_universes() -> list[dict[str, Any]]:
    try:
        value = json.loads(UNIVERSE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def get_universe(universe_id: str | None) -> dict[str, Any] | None:
    requested = universe_id or DEFAULT_UNIVERSE_ID
    return next((item for item in load_universes() if item.get("universe_id") == requested), None)


def universe_options() -> list[dict[str, Any]]:
    """Return UI-safe metadata; instrument rows are returned with no secrets."""
    return [
        {
            "universe_id": item.get("universe_id"),
            "name": item.get("name"),
            "description": item.get("description"),
            "status": item.get("status", "TEST"),
            "source": item.get("source"),
            "instrument_count": len(item.get("instruments", [])),
        }
        for item in load_universes()
    ]


def _split_amount(total: Decimal, count: int) -> list[Decimal]:
    if count <= 0:
        return []
    unit = (total / count).quantize(Decimal("0.0001"), rounding=ROUND_DOWN)
    values = [unit for _ in range(count)]
    values[-1] = total - sum(values[:-1], Decimal(0))
    return values


def _instrument_rows(
    recommendation: Mapping[str, Any],
    universe: Mapping[str, Any],
    currency: str,
    allowed_asset_classes: frozenset[str] | None = None,
    instruments: Sequence[Mapping[str, Any]] | None = None,
    data_status: str | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    source_instruments = list(instruments) if instruments is not None else [
        item
        for item in universe.get("instruments", [])
        if isinstance(item, Mapping)
    ]
    rows: list[dict[str, Any]] = []
    unresolved: list[str] = []
    allocations = recommendation.get("target_allocations", {})
    amounts = recommendation.get("target_amounts", {})
    for asset_class, raw_weight in allocations.items():
        # Bonds and cash-like assets are intentionally outside this UI
        # projection. An explicit toggle controls the remaining two groups.
        if allowed_asset_classes is not None and asset_class not in allowed_asset_classes:
            continue
        matches = [
            item for item in source_instruments if item.get("asset_class") == asset_class
        ]
        if not matches:
            unresolved.append(str(asset_class))
            continue
        weight = Decimal(str(raw_weight))
        amount = Decimal(str(amounts.get(asset_class, "0")))
        weights = _split_amount(weight, len(matches))
        amounts_by_instrument = _split_amount(amount, len(matches))
        for item, instrument_weight, instrument_amount in zip(
            matches, weights, amounts_by_instrument, strict=True
        ):
            rows.append(
                {
                    "symbol": str(item.get("symbol", "")),
                    "exchange": str(item.get("exchange", "")),
                    "name": str(item.get("name", item.get("symbol", ""))),
                    "asset_class": str(asset_class),
                    "currency": str(item.get("currency", currency)),
                    "target_weight": str(instrument_weight),
                    "target_amount": str(instrument_amount),
                    # Never show an invented forecast. This is populated only
                    # when a PIT-checked research/market adapter supplies it.
                    "expected_return": item.get("expected_return"),
                    "expected_return_status": item.get("expected_return_status", "UNAVAILABLE"),
                    "expected_return_basis": item.get(
                        "expected_return_basis",
                        "실시간 시장·리서치 데이터 연결 후 산출",
                    ),
                "data_status": item.get("data_status", data_status or universe.get("status", "TEST")),
                    "evidence_refs": list(item.get("evidence_refs", [])),
                }
            )
    return rows, unresolved


def enrich_suitability_result(
    result: Mapping[str, Any],
    universe_id: str | None,
    *,
    include_stock: bool = True,
    include_derivatives: bool = False,
    live_instruments: Sequence[Mapping[str, Any]] | None = None,
    live_universe_status: str | None = None,
) -> dict[str, Any]:
    """Attach instrument-level advisory rows to the deterministic result."""
    output = dict(result)
    universe = get_universe(universe_id)
    if universe is None:
        output["universe_id"] = universe_id or DEFAULT_UNIVERSE_ID
        output["universe"] = None
        output["instrument_recommendations"] = []
        output["unresolved_asset_classes"] = []
        output["instrument_recommendations_status"] = "UNAVAILABLE"
        output["safe_action"] = "HOLD"
        output["forecast_notice"] = "선택한 투자 유니버스를 찾지 못해 종목 추천을 확정하지 않았습니다."
        return output

    currency = str(output.get("currency", "KRW"))
    source_instruments = (
        list(live_instruments) if live_instruments is not None else None
    )
    instrument_status = (
        live_universe_status
        if live_universe_status is not None
        else "LIVE"
        if live_instruments is not None
        else universe.get("status", "TEST")
    )
    allowed_asset_classes = frozenset()
    if include_stock:
        allowed_asset_classes |= STOCK_ASSET_CLASSES
    if include_derivatives:
        allowed_asset_classes |= DERIVATIVE_ASSET_CLASSES
    if universe.get("universe_id") == "KOREA_EQUITY_WATCHLIST":
        allowed_asset_classes &= {"KOREA_EQUITY"}
    instrument_recommendations: list[dict[str, Any]] = []
    unresolved: set[str] = set()
    for recommendation in output.get("suitability", {}).get("recommendations", []):
        rows, missing = _instrument_rows(
            recommendation,
            universe,
            currency,
            allowed_asset_classes,
            source_instruments,
            instrument_status,
        )
        portfolio_id = recommendation.get("portfolio_id")
        for row in rows:
            row["portfolio_id"] = portfolio_id
        # Keep the rows both at the result root and beside each portfolio
        # candidate. The nested copy makes the UI resilient to older BFF
        # projections while the root copy remains the canonical collection.
        recommendation["instrument_recommendations"] = rows
        instrument_recommendations.extend(rows)
        unresolved.update(missing)

    output["universe_id"] = universe["universe_id"]
    output["universe"] = {
        "universe_id": universe["universe_id"],
        "name": universe["name"],
        "description": universe.get("description"),
        "status": instrument_status,
        "source": (
            "control-db.reference.instruments"
            if live_instruments is not None
            else universe.get("source")
        ),
    }
    output["instrument_recommendations"] = instrument_recommendations
    output["asset_visibility"] = {
        "include_stock": include_stock,
        "include_derivatives": include_derivatives,
        "bond_data_excluded": True,
    }
    output["unresolved_asset_classes"] = sorted(unresolved)
    if not instrument_recommendations:
        output["instrument_recommendations_status"] = "UNAVAILABLE"
        output["safe_action"] = "HOLD"
    else:
        output["instrument_recommendations_status"] = "INCOMPLETE" if unresolved else "COMPLETE"
    if unresolved or not instrument_recommendations:
        output["safe_action"] = "HOLD"
    output["forecast_notice"] = (
        "예상 수익률은 보장값이 아니며, PIT 검증된 시장·리서치 데이터가 없는 경우 "
        "산출하지 않습니다."
    )
    return output


__all__ = [
    "DEFAULT_UNIVERSE_ID",
    "DERIVATIVE_ASSET_CLASSES",
    "STOCK_ASSET_CLASSES",
    "enrich_suitability_result",
    "get_universe",
    "load_universes",
    "universe_options",
]
