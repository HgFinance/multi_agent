"""Fail-closed market-price input for conditional PAPER rules.

The conditional worker uses this read-only adapter only for the current-price
dependency. Historical bars continue to come from the existing Market API.
There is intentionally no local-value or Market API fallback here: a missing
PAPER LS quote must prevent a trigger from becoming an order.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Protocol

from .indicators.broker.ls_readonly import (
    LSOpenAPIReadOnlyTransport,
    LSReadOnlyTransportError,
)


class MarketPriceResolverError(RuntimeError):
    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class MarketPriceSnapshot:
    symbol: str
    price: Decimal
    observed_at: datetime
    source: str


class MarketPriceResolver(Protocol):
    def snapshot(self, symbol: str) -> MarketPriceSnapshot: ...


def _decimal_price(value: object, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None or str(value).strip() == "":
        raise MarketPriceResolverError(
            "MARKET_PRICE_INVALID", f"{field} is missing", retryable=False
        )
    try:
        price = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketPriceResolverError(
            "MARKET_PRICE_INVALID", f"{field} is not numeric", retryable=False
        ) from exc
    if not price.is_finite() or price <= 0:
        raise MarketPriceResolverError(
            "MARKET_PRICE_INVALID",
            f"{field} is not a positive finite price",
            retryable=False,
        )
    if price != price.to_integral_value():
        raise MarketPriceResolverError(
            "MARKET_PRICE_INVALID",
            f"{field} is not a KRW integer price",
            retryable=False,
        )
    return price


class LSPaperMarketPriceResolver:
    """Read one KRX quote through the existing LS REST read-only transport."""

    TR_CODE = "t1102"
    PATH = "/stock/market-data"

    def __init__(self, transport: LSOpenAPIReadOnlyTransport) -> None:
        self._transport = transport

    @classmethod
    def from_env(cls) -> "LSPaperMarketPriceResolver":
        # The conditional worker is PAPER-only. Refuse the default LIVE
        # environment even though the shared LS client supports both modes.
        if os.getenv("LS_ENV", "").strip().upper() != "PAPER":
            raise MarketPriceResolverError(
                "MARKET_PRICE_PAPER_ENV_REQUIRED",
                "conditional market-price resolver requires LS_ENV=PAPER",
                retryable=False,
            )
        try:
            return cls(LSOpenAPIReadOnlyTransport.from_env())
        except Exception as exc:
            raise MarketPriceResolverError(
                "MARKET_PRICE_PROVIDER_UNAVAILABLE",
                "LS PAPER market-price provider is unavailable",
            ) from exc

    def snapshot(self, symbol: str) -> MarketPriceSnapshot:
        normalized = symbol.strip().upper()
        if not normalized or len(normalized) != 6 or not normalized.isalnum():
            raise MarketPriceResolverError(
                "MARKET_PRICE_SYMBOL_INVALID",
                "market-price symbol is invalid",
                retryable=False,
            )
        try:
            payload = self._transport.request_sync(
                path=self.PATH,
                tr_code=self.TR_CODE,
                payload={
                    "t1102InBlock": {"shcode": normalized, "exchgubun": "U"}
                },
            )
        except LSReadOnlyTransportError as exc:
            raise MarketPriceResolverError(exc.code, str(exc)) from exc
        except TimeoutError as exc:
            raise MarketPriceResolverError(
                "MARKET_PRICE_PROVIDER_TIMEOUT",
                "LS PAPER market-price request timed out",
            ) from exc
        except Exception as exc:
            raise MarketPriceResolverError(
                "MARKET_PRICE_PROVIDER_UNAVAILABLE",
                "LS PAPER market-price request failed",
            ) from exc

        if not isinstance(payload, Mapping):
            raise MarketPriceResolverError(
                "MARKET_PRICE_INVALID",
                "LS market-price response is not an object",
                retryable=False,
            )
        response_code = str(payload.get("rsp_cd") or payload.get("rspCode") or "").strip()
        if response_code and response_code not in {"0000", "00000", "0"}:
            raise MarketPriceResolverError(
                "MARKET_PRICE_PROVIDER_UNAVAILABLE",
                "LS rejected the market-price request",
            )
        block = payload.get("t1102OutBlock")
        if not isinstance(block, Mapping):
            raise MarketPriceResolverError(
                "MARKET_PRICE_INVALID",
                "LS t1102 response block is missing",
                retryable=False,
            )
        response_symbol = str(block.get("shcode") or "").strip().upper()
        if response_symbol and response_symbol != normalized:
            raise MarketPriceResolverError(
                "MARKET_PRICE_SYMBOL_MISMATCH",
                "LS returned a different symbol",
                retryable=False,
            )
        # t1102 is a current-price REST response and has no trade event
        # timestamp in the reviewed schema. Receipt time is the explicit
        # observation boundary; no provider timestamp is invented.
        observed_at = datetime.now(timezone.utc)
        return MarketPriceSnapshot(
            symbol=normalized,
            price=_decimal_price(block.get("price"), field="t1102OutBlock.price"),
            observed_at=observed_at,
            source="LS_T1102_READONLY_RECEIPT",
        )


__all__ = [
    "LSPaperMarketPriceResolver",
    "MarketPriceResolver",
    "MarketPriceResolverError",
    "MarketPriceSnapshot",
]
