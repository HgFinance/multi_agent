"""Read-only LS OpenAPI to Risk P1 input normalisation.

The broker response is deliberately treated as untrusted.  Missing symbol or
quantity fields are errors, and a broker symbol is usable only when an
explicit canonical instrument mapping exists.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from integrations.ls_openapi import LSOpenAPIClient

from .analytics import InstrumentMapping, MarketPoint, PortfolioPosition, RiskP1Error


@dataclass(frozen=True)
class LSCollectedRiskInputs:
    equity: float
    positions: tuple[PortfolioPosition, ...]
    market: tuple[MarketPoint, ...]


def collect_ls_inputs(
    client: LSOpenAPIClient,
    *,
    mappings: Sequence[InstrumentMapping],
    returns_by_symbol: Mapping[str, Sequence[float]] | None = None,
) -> LSCollectedRiskInputs:
    """Collect account/quote data without ever creating an order."""

    mapping_by_symbol = {
        mapping.broker_symbol.strip().upper(): mapping for mapping in mappings
    }
    if not mapping_by_symbol:
        raise RiskP1Error("instrument mapping is required before LS collection")
    portfolio = client.get_portfolio_snapshot()
    positions: list[PortfolioPosition] = []
    market: list[MarketPoint] = []
    returns_by_symbol = returns_by_symbol or {}
    for raw in portfolio.positions:
        symbol = _required_text(raw, "symbol", "expcode", "shcode", "IsuNo")
        mapping = mapping_by_symbol.get(symbol.upper())
        if mapping is None:
            raise RiskP1Error(f"instrument mapping missing for LS symbol {symbol}")
        quantity = _decimal(raw, "quantity", "janqty", "잔고수량", "qty")
        if quantity == 0:
            continue
        quote = client.get_quote(symbol)
        positions.append(
            PortfolioPosition(
                symbol, float(quantity), instrument_id=mapping.instrument_id
            )
        )
        market.append(
            MarketPoint(
                broker_symbol=symbol,
                price=float(quote.price),
                observed_at=quote.observed_at,
                returns=tuple(
                    float(value) for value in returns_by_symbol.get(symbol, ())
                ),
            )
        )
    if not positions:
        raise RiskP1Error("LS portfolio has no non-zero mapped position")
    return LSCollectedRiskInputs(
        float(portfolio.equity), tuple(positions), tuple(market)
    )


def _required_text(raw: Mapping[str, Any], *names: str) -> str:
    for name in names:
        value = raw.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    raise RiskP1Error(f"LS position missing one of: {', '.join(names)}")


def _decimal(raw: Mapping[str, Any], *names: str) -> Decimal:
    for name in names:
        value = raw.get(name)
        if value is None or value == "":
            continue
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise RiskP1Error(f"LS position field {name} is not numeric") from exc
        if not result.is_finite():
            raise RiskP1Error(f"LS position field {name} is not finite")
        return result
    raise RiskP1Error(f"LS position missing one of: {', '.join(names)}")
