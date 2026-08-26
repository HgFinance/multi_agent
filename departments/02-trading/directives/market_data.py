"""Trusted quote providers for the authenticated PAPER directive lane."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .repository import InstrumentRef


class MarketDataError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 503) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


@dataclass(frozen=True)
class TrustedQuote:
    instrument_id: str
    symbol: str
    observed_at: datetime
    bid: Decimal
    ask: Decimal
    bid_size: Decimal
    ask_size: Decimal
    source: str


class MarketDataProvider(Protocol):
    def quote(self, instrument: InstrumentRef, *, now: datetime, max_age_seconds: float | None = None) -> TrustedQuote: ...


def _aware(value: Any) -> datetime:
    if not isinstance(value, str):
        raise MarketDataError("TRADING_MARKET_QUOTE_INVALID", "quote event_time is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MarketDataError("TRADING_MARKET_QUOTE_INVALID", "quote event_time is invalid") from exc
    if parsed.tzinfo is None:
        raise MarketDataError("TRADING_MARKET_QUOTE_INVALID", "quote event_time must include timezone")
    return parsed.astimezone(timezone.utc)


def _decimal(value: Any, name: str, *, positive: bool) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise MarketDataError("TRADING_MARKET_QUOTE_INVALID", f"quote {name} is invalid") from exc
    if not parsed.is_finite() or (positive and parsed <= 0) or (not positive and parsed < 0):
        raise MarketDataError("TRADING_MARKET_QUOTE_INVALID", f"quote {name} is outside policy")
    return parsed


def validate_quote(
    quote_value: TrustedQuote,
    instrument: InstrumentRef,
    *,
    now: datetime,
    max_age_seconds: float | None = None,
) -> TrustedQuote:
    if now.tzinfo is None:
        raise MarketDataError("TRADING_MARKET_TIME_INVALID", "timezone-aware market time required", 500)
    if quote_value.instrument_id != str(instrument.instrument_id) or quote_value.symbol != instrument.symbol:
        raise MarketDataError(
            "TRADING_MARKET_QUOTE_BINDING_DENIED",
            "trusted quote does not match the canonical instrument",
            409,
        )
    try:
        max_age = float(max_age_seconds) if max_age_seconds is not None else float(os.environ.get("TRADING_MARKET_QUOTE_MAX_AGE_SECONDS", "10"))
        future_skew = float(os.environ.get("TRADING_MARKET_QUOTE_FUTURE_SKEW_SECONDS", "2"))
    except ValueError as exc:
        raise MarketDataError("TRADING_MARKET_POLICY_INVALID", "quote freshness policy is invalid", 503) from exc
    if not (0 < max_age <= 600) or not (0 <= future_skew <= 30):
        raise MarketDataError("TRADING_MARKET_POLICY_INVALID", "quote freshness policy is outside bounds", 503)
    age = (now.astimezone(timezone.utc) - quote_value.observed_at.astimezone(timezone.utc)).total_seconds()
    if age > max_age or age < -future_skew:
        raise MarketDataError("TRADING_MARKET_QUOTE_STALE", "trusted quote is outside the freshness window", 409)
    if quote_value.ask < quote_value.bid:
        raise MarketDataError("TRADING_MARKET_QUOTE_CROSSED", "trusted quote ask is below bid", 409)
    if quote_value.bid_size <= 0 or quote_value.ask_size <= 0:
        raise MarketDataError("TRADING_MARKET_QUOTE_EMPTY", "trusted quote has no executable size", 409)
    return quote_value


class FixtureMarketDataProvider:
    """Explicit deterministic fixture; never selected implicitly in production."""

    def __init__(self, quotes: dict[str, TrustedQuote] | None = None) -> None:
        self.quotes = quotes or {}

    def set_quote(self, quote_value: TrustedQuote) -> None:
        self.quotes[quote_value.symbol] = quote_value

    def quote(self, instrument: InstrumentRef, *, now: datetime, max_age_seconds: float | None = None) -> TrustedQuote:
        value = self.quotes.get(instrument.symbol)
        if value is None:
            raise MarketDataError("TRADING_MARKET_QUOTE_UNAVAILABLE", "fixture quote is unavailable", 503)
        return validate_quote(value, instrument, now=now, max_age_seconds=max_age_seconds)


class HttpMarketDataProvider:
    """Read a bounded-fresh L1 quote from the internal read-only market API."""

    def __init__(self, base_url: str) -> None:
        self.base_url = str(base_url or "").strip().rstrip("/")
        parsed = urlsplit(self.base_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise MarketDataError("TRADING_MARKET_API_NOT_CONFIGURED", "MARKET_API_URL is required")

    @classmethod
    def from_env(cls) -> "HttpMarketDataProvider":
        return cls(os.environ.get("MARKET_API_URL", ""))

    def quote(self, instrument: InstrumentRef, *, now: datetime, max_age_seconds: float | None = None) -> TrustedQuote:
        url = f"{self.base_url}/snapshot/{quote(instrument.symbol, safe='')}"
        request = Request(
            url,
            method="GET",
            headers={"Accept": "application/json", "User-Agent": "trading-api/paper-directive-v1"},
        )
        try:
            with urlopen(request, timeout=3.0) as response:  # noqa: S310 - configured internal service URL
                if response.status != 200:
                    raise MarketDataError(
                        "TRADING_MARKET_QUOTE_UNAVAILABLE",
                        f"market API returned HTTP {response.status}",
                    )
                body = json.loads(response.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise MarketDataError("TRADING_MARKET_QUOTE_UNAVAILABLE", "market API quote read failed") from exc
        if not isinstance(body, dict) or body.get("symbol") != instrument.symbol:
            raise MarketDataError("TRADING_MARKET_QUOTE_INVALID", "market API symbol binding failed")
        level = body.get("last_quote")
        if not isinstance(level, dict):
            raise MarketDataError("TRADING_MARKET_QUOTE_UNAVAILABLE", "market API has no L1 quote")
        value = TrustedQuote(
            instrument_id=str(instrument.instrument_id),
            symbol=instrument.symbol,
            observed_at=_aware(level.get("event_time")),
            bid=_decimal(level.get("best_bid"), "best_bid", positive=True),
            ask=_decimal(level.get("best_ask"), "best_ask", positive=True),
            bid_size=_decimal(level.get("total_bid_size"), "total_bid_size", positive=False),
            ask_size=_decimal(level.get("total_ask_size"), "total_ask_size", positive=False),
            source="market-api:last_quote",
        )
        return validate_quote(value, instrument, now=now, max_age_seconds=max_age_seconds)


class LsPaperFallbackMarketDataProvider:
    """Use an authenticated read-only LS quote when the TSDB projection is stale."""

    _FALLBACK_CODES = {
        "TRADING_MARKET_QUOTE_STALE",
        "TRADING_MARKET_QUOTE_UNAVAILABLE",
    }

    def __init__(self, primary: MarketDataProvider, broker: Any) -> None:
        self.primary = primary
        self.broker = broker

    def quote(
        self,
        instrument: InstrumentRef,
        *,
        now: datetime,
        max_age_seconds: float | None = None,
    ) -> TrustedQuote:
        try:
            return self.primary.quote(
                instrument, now=now, max_age_seconds=max_age_seconds
            )
        except MarketDataError as exc:
            if exc.code not in self._FALLBACK_CODES:
                raise
        try:
            level = self.broker.get_quote(instrument.symbol)
            value = TrustedQuote(
                instrument_id=str(instrument.instrument_id),
                symbol=str(level["symbol"]),
                observed_at=level["observed_at"],
                bid=_decimal(level["bid"], "bid", positive=True),
                ask=_decimal(level["ask"], "ask", positive=True),
                bid_size=_decimal(level["bid_size"], "bid_size", positive=False),
                ask_size=_decimal(level["ask_size"], "ask_size", positive=False),
                source="ls-paper-rest:t1101",
            )
        except Exception as exc:
            raise MarketDataError(
                "TRADING_MARKET_QUOTE_UNAVAILABLE",
                "LS PAPER REST quote fallback failed",
            ) from exc
        return validate_quote(
            value, instrument, now=now, max_age_seconds=max_age_seconds
        )


__all__ = [
    "FixtureMarketDataProvider",
    "HttpMarketDataProvider",
    "LsPaperFallbackMarketDataProvider",
    "MarketDataError",
    "MarketDataProvider",
    "TrustedQuote",
    "validate_quote",
]
