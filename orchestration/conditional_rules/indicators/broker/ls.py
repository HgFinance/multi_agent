"""LS OpenAPI capability routes for the normalized indicator names.

The route table is adapter metadata only. Persisted rules use canonical names
like ``FOREIGN_NET_BUY_AMOUNT`` and never these TR identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from datetime import datetime
from types import MappingProxyType
from typing import Any, Mapping

from ..base import IndicatorProviderError, IndicatorValue


@dataclass(frozen=True)
class LSIndicatorRoute:
    indicator: str
    documentation: str
    tr_codes: tuple[str, ...]
    default_tr_code: str | None = None
    path: str | None = None
    response_block: str | None = None
    value_field: str | None = None
    realtime: bool = False
    boolean_match: bool = False

    def supports_tr(self, tr_code: str) -> bool:
        token = tr_code.strip().upper()
        return bool(token) and token in {code.upper() for code in self.tr_codes}


LS_INDICATOR_ROUTES = MappingProxyType(
    {
        "FOREIGN_NET_BUY_VOLUME": LSIndicatorRoute(
            "FOREIGN_NET_BUY_VOLUME", "03-stock/06-90378c39.md", ("t1702", "t1716", "t1717"),
            default_tr_code="t1717", path="/stock/frgr-itt", response_block="t1717OutBlock",
            value_field="tjj0016_vol",
        ),
        "FOREIGN_NET_BUY_AMOUNT": LSIndicatorRoute(
            "FOREIGN_NET_BUY_AMOUNT", "03-stock/06-90378c39.md", ("t1702", "t1716", "t1717"),
            default_tr_code="t1702", path="/stock/frgr-itt", response_block="t1702OutBlock1",
            value_field="tjj0016",
        ),
        "INSTITUTION_NET_BUY_VOLUME": LSIndicatorRoute(
            "INSTITUTION_NET_BUY_VOLUME", "03-stock/06-90378c39.md", ("t1702", "t1716", "t1717"),
            default_tr_code="t1717", path="/stock/frgr-itt", response_block="t1717OutBlock",
            value_field="tjj0018_vol",
        ),
        "INSTITUTION_NET_BUY_AMOUNT": LSIndicatorRoute(
            "INSTITUTION_NET_BUY_AMOUNT", "03-stock/06-90378c39.md", ("t1702", "t1716", "t1717"),
            default_tr_code="t1702", path="/stock/frgr-itt", response_block="t1702OutBlock1",
            value_field="tjj0018",
        ),
        "PROGRAM_NET_BUY_VOLUME": LSIndicatorRoute(
            "PROGRAM_NET_BUY_VOLUME", "03-stock/04-6b554636.md", ("t1631", "t1632", "t1633", "t1636", "t1637", "t1640", "t1662"),
            default_tr_code="t1637", path="/stock/program", response_block="t1637OutBlock1",
            value_field="svolume",
        ),
        "PROGRAM_NET_BUY_AMOUNT": LSIndicatorRoute(
            "PROGRAM_NET_BUY_AMOUNT", "03-stock/04-6b554636.md", ("t1631", "t1632", "t1633", "t1636", "t1640", "t1662", "t1637"),
            default_tr_code="t1637", path="/stock/program", response_block="t1637OutBlock1",
            value_field="svalue",
        ),
        "SHORT_SELL_VOLUME": LSIndicatorRoute(
            "SHORT_SELL_VOLUME", "03-stock/13-316495d3.md", ("t1927", "t1941"),
            default_tr_code="t1927", path="/stock/etc", response_block="t1927OutBlock1",
            value_field="gm_vo",
        ),
        "SHORT_SELL_RATIO": LSIndicatorRoute(
            "SHORT_SELL_RATIO", "03-stock/13-316495d3.md", ("t1927",),
            default_tr_code="t1927", path="/stock/etc", response_block="t1927OutBlock1",
            value_field="gm_per",
        ),
        "VI_STATUS": LSIndicatorRoute(
            "VI_STATUS", "03-stock/16-9a2800c3.md", ("VI_", "UVI"),
            default_tr_code="VI_", path="/websocket", value_field="vi_gubun",
            realtime=True,
        ),
        "MARKET_WARNING_STATUS": LSIndicatorRoute(
            "MARKET_WARNING_STATUS", "03-stock/01-54a99b02.md", ("t1405",),
            default_tr_code="t1405", path="/stock/market-data", response_block="t1405OutBlock1",
            boolean_match=True,
        ),
        "BROKER_SEARCH_MATCH": LSIndicatorRoute(
            "BROKER_SEARCH_MATCH", "03-stock/10-6b67369a.md", ("t1809", "t1825", "t1826", "t1852", "t1856", "t1866", "t1859", "t1860")
        ),
    }
)


def route_for_indicator(indicator: str | None) -> LSIndicatorRoute | None:
    """Resolve a canonical indicator to capability metadata only."""

    token = str(indicator or "").strip().upper()
    return LS_INDICATOR_ROUTES.get(token)


def normalize_ls_payload(
    raw_payload: Mapping[str, Any],
    *,
    indicator: str,
    value_field: str,
    source: str = "BROKER",
    provider: str = "LS",
    output: str = "VALUE",
    timeframe: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    observed_at: datetime | None = None,
    data_timestamp: datetime | None = None,
    calculation_version: str = "v1",
    market_data_source_id: str | None = None,
) -> IndicatorValue:
    """Convert one LS raw TR response inside the broker adapter boundary.

    The evaluator never calls this function and never receives the raw TR
    object.  A future HTTP/websocket adapter should perform field-specific
    parsing here (or in a sibling module under ``broker``), then return only
    ``IndicatorValue`` through ``LSBrokerIndicatorProvider``.
    """

    if route_for_indicator(indicator) is None:
        raise IndicatorProviderError(
            "INDICATOR_TR_UNSUPPORTED",
            f"LS has no route for {indicator!r}",
            retryable=False,
        )
    if not isinstance(raw_payload, Mapping):
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            "LS response must be an object",
            retryable=False,
        )
    if value_field not in raw_payload:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_PARTIAL_DATA",
            f"LS response has no {value_field!r} field",
            retryable=False,
        )
    raw_value = raw_payload[value_field]
    if raw_value is None:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_PARTIAL_DATA",
            f"LS response field {value_field!r} is null",
            retryable=False,
        )
    if observed_at is None:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_PARTIAL_DATA",
            "LS response has no observed timestamp",
            retryable=False,
        )
    if not isinstance(raw_value, bool):
        try:
            raw_value = Decimal(str(raw_value))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"LS response field {value_field!r} is not a scalar",
                retryable=False,
            ) from exc
        if not raw_value.is_finite():
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"LS response field {value_field!r} is not finite",
                retryable=False,
            )
    return IndicatorValue(
        value=raw_value,
        indicator=indicator,
        source=source,
        provider=provider,
        observed_at=observed_at,
        data_timestamp=data_timestamp,
        calculation_version=calculation_version,
        output=output,
        timeframe=timeframe,
        parameters=dict(parameters or {}),
        market_data_source_id=market_data_source_id,
    )
