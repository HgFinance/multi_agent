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


class LSTimescaleMarketPriceResolver:
    """Read the latest tick already collected by the shared LS WebSocket.

    ``ls-realtime`` owns the broker WebSocket and writes one canonical tick
    stream to ``market.market_ticks``.  The conditional worker deliberately
    does not open another broker connection: it reads the latest row by the
    authoritative instrument id.  The pool is bounded because rule evaluation
    is parallel, and the statement timeout keeps a market-database problem
    fail-closed instead of blocking the worker cycle.
    """

    def __init__(
        self,
        pool,
        *,
        statement_timeout_ms: int = 1500,
        lookback_days: int = 7,
    ) -> None:
        self._pool = pool
        self._statement_timeout_ms = max(100, min(int(statement_timeout_ms), 10_000))
        self._lookback_days = max(1, min(int(lookback_days), 30))

    @classmethod
    def from_env(cls) -> "LSTimescaleMarketPriceResolver":
        dsn = os.getenv("CONDITIONAL_RULE_MARKET_DATABASE_URL", "").strip()
        if not dsn:
            raise MarketPriceResolverError(
                "MARKET_PRICE_SHARED_DATABASE_REQUIRED",
                "shared market database URL is required for LS realtime prices",
                retryable=False,
            )
        try:
            from psycopg2.pool import ThreadedConnectionPool

            pool = ThreadedConnectionPool(
                1,
                max(
                    1,
                    min(
                        int(os.getenv("CONDITIONAL_RULE_MARKET_MAX_CONNECTIONS", "8")),
                        16,
                    ),
                ),
                dsn,
                connect_timeout=max(
                    1,
                    min(
                        int(
                            os.getenv(
                                "CONDITIONAL_RULE_MARKET_CONNECT_TIMEOUT_SECONDS", "2"
                            )
                        ),
                        10,
                    ),
                ),
            )
            return cls(
                pool,
                statement_timeout_ms=int(
                    os.getenv("CONDITIONAL_RULE_MARKET_STATEMENT_TIMEOUT_MS", "1500")
                ),
                lookback_days=int(
                    os.getenv("CONDITIONAL_RULE_MARKET_LOOKBACK_DAYS", "7")
                ),
            )
        except MarketPriceResolverError:
            raise
        except Exception as exc:
            raise MarketPriceResolverError(
                "MARKET_PRICE_SHARED_DATABASE_UNAVAILABLE",
                "shared LS realtime market database is unavailable",
            ) from exc

    @staticmethod
    def _aware(value: object, *, field: str) -> datetime:
        if not isinstance(value, datetime):
            raise MarketPriceResolverError(
                "MARKET_PRICE_INVALID",
                f"market tick {field} is invalid",
                retryable=False,
            )
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def snapshot_for_instrument(self, symbol: str, instrument_id: object) -> MarketPriceSnapshot:
        normalized = symbol.strip().upper()
        if not normalized or len(normalized) != 6 or not normalized.isalnum():
            raise MarketPriceResolverError(
                "MARKET_PRICE_SYMBOL_INVALID",
                "market-price symbol is invalid",
                retryable=False,
            )
        connection = None
        try:
            connection = self._pool.getconn()
            with connection.cursor() as cursor:
                cursor.execute(
                    "select set_config('statement_timeout', %s, true)",
                    (str(self._statement_timeout_ms),),
                )
                cursor.execute(
                    """
                    select event_time, observed_at, price, market, provider
                      from market.market_ticks
                     where instrument_id=%s
                       and event_time >= now() - (%s * interval '1 day')
                     order by event_time desc, received_at desc
                     limit 1
                    """,
                    (instrument_id, self._lookback_days),
                )
                row = cursor.fetchone()
            connection.commit()
        except Exception as exc:
            if connection is not None:
                try:
                    connection.rollback()
                except Exception:  # noqa: BLE001 - connection may already be closed
                    pass
            if getattr(exc, "pgcode", None) == "57014":
                raise MarketPriceResolverError(
                    "MARKET_PRICE_SHARED_DATABASE_TIMEOUT",
                    "shared LS realtime market tick query timed out",
                ) from exc
            raise MarketPriceResolverError(
                "MARKET_PRICE_SHARED_DATABASE_UNAVAILABLE",
                "shared LS realtime market tick query failed",
            ) from exc
        finally:
            if connection is not None:
                try:
                    self._pool.putconn(connection)
                except Exception:  # noqa: BLE001 - pool cleanup must not mask the result
                    pass

        if not row:
            raise MarketPriceResolverError(
                "MARKET_PRICE_SHARED_DATA_GAP",
                "no LS realtime tick is available for the instrument",
            )
        event_time, observed_at, price, market, provider = row
        # ``observed_at`` is the freshness boundary; event_time is the broker
        # event clock and can legitimately lag receipt during reconnects.
        self._aware(event_time, field="event_time")
        return MarketPriceSnapshot(
            symbol=normalized,
            price=_decimal_price(price, field="market.market_ticks.price"),
            observed_at=self._aware(observed_at, field="observed_at"),
            source=(
                "LS_REALTIME_TICK:"
                f"{str(provider or 'UNKNOWN').strip()[:32]}:"
                f"{str(market or 'UNKNOWN').strip()[:16]}"
            ),
        )

    def snapshot(self, symbol: str) -> MarketPriceSnapshot:
        raise MarketPriceResolverError(
            "MARKET_PRICE_INSTRUMENT_REQUIRED",
            "shared LS realtime prices require the canonical instrument id",
            retryable=False,
        )

    def close(self) -> None:
        closeall = getattr(self._pool, "closeall", None)
        if callable(closeall):
            closeall()


__all__ = [
    "LSPaperMarketPriceResolver",
    "LSTimescaleMarketPriceResolver",
    "MarketPriceResolver",
    "MarketPriceResolverError",
    "MarketPriceSnapshot",
]
