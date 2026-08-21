"""Read-only LS indicator resolver.

This module is the only place where the conditional-rule broker provider knows
how to ask LS for an indicator.  It deliberately depends on the existing
read-only ``LSOpenAPIClient._post_tr`` transport and has no order client,
submission method, or PAPER execution dependency.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from ..base import IndicatorProviderError, IndicatorValue
from .ls import LSIndicatorRoute, route_for_indicator


KST = timezone(timedelta(hours=9))
DEFAULT_HISTORICAL_MAX_AGE_SECONDS = 3 * 24 * 60 * 60
DEFAULT_REALTIME_MAX_AGE_SECONDS = 10


class LSReadOnlyTransport(Protocol):
    async def request(
        self,
        *,
        path: str,
        tr_code: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...

    async def realtime_snapshot(
        self,
        *,
        tr_code: str,
        symbol: str,
    ) -> Mapping[str, Any]: ...


class LSReadOnlyTransportError(RuntimeError):
    """Transport failure with an explicit fail-closed provider code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class LSOpenAPIReadOnlyTransport:
    """Async facade over the repository's existing LS read-only REST client."""

    def __init__(self, client: Any) -> None:
        self._client = client

    @classmethod
    def from_env(cls) -> "LSOpenAPIReadOnlyTransport":
        # ``03-risk`` is intentionally not a Python package.  Match the
        # existing risk market-data adapter's import boundary without importing
        # or modifying the LS order broker.
        repo_root = Path(__file__).resolve().parents[4]
        integrations = repo_root / "departments" / "03-risk" / "integrations"
        if str(integrations) not in sys.path:
            sys.path.insert(0, str(integrations))
        from ls_openapi import LSOpenAPIClient  # type: ignore[import-not-found]

        return cls(LSOpenAPIClient.from_env())

    async def request(
        self,
        *,
        path: str,
        tr_code: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        try:
            return await asyncio.to_thread(
                self._client._post_tr,  # existing read-only transport seam
                path,
                tr_code,
                payload,
            )
        except TimeoutError:
            raise
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                raise LSReadOnlyTransportError(
                    "INDICATOR_PROVIDER_RATE_LIMITED",
                    "LS read-only indicator request was rate limited",
                ) from exc
            if status_code in {408, 504}:
                raise LSReadOnlyTransportError(
                    "INDICATOR_PROVIDER_TIMEOUT",
                    "LS read-only indicator request timed out",
                ) from exc
            raise LSReadOnlyTransportError(
                "INDICATOR_PROVIDER_UNAVAILABLE",
                "LS read-only indicator transport is unavailable",
            ) from exc

    def request_sync(
        self,
        *,
        path: str,
        tr_code: str,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        """Synchronous counterpart for the synchronous conditional worker.

        This calls the same existing LS client and REST route as ``request``;
        it is a transport seam, not a second broker client or order path.
        """
        try:
            return self._client._post_tr(path, tr_code, payload)
        except TimeoutError:
            raise
        except Exception as exc:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
            if status_code == 429:
                raise LSReadOnlyTransportError(
                    "MARKET_PRICE_RATE_LIMITED",
                    "LS read-only market-price request was rate limited",
                ) from exc
            if status_code in {408, 504}:
                raise LSReadOnlyTransportError(
                    "MARKET_PRICE_PROVIDER_TIMEOUT",
                    "LS read-only market-price request timed out",
                ) from exc
            raise LSReadOnlyTransportError(
                "MARKET_PRICE_PROVIDER_UNAVAILABLE",
                "LS read-only market-price transport is unavailable",
            ) from exc

    async def realtime_snapshot(
        self,
        *,
        tr_code: str,
        symbol: str,
    ) -> Mapping[str, Any]:
        # Reuse the existing LS OAuth client for the token and the repository's
        # proven WebSocket wire contract: /websocket, tr_type=3 subscription,
        # tr_key=symbol, ack followed by a body-only market event.  This is a
        # short read-only snapshot; it does not register an order/event TR.
        try:
            import websockets

            token = await asyncio.to_thread(self._client._access_token)
            environment = str(self._client.config.environment).upper()
            port = 29443 if environment == "PAPER" else 9443
            url = f"wss://openapi.ls-sec.co.kr:{port}/websocket"
            timeout = float(self._client.config.timeout_seconds)
            async with websockets.connect(
                url,
                open_timeout=timeout,
                ping_interval=30,
            ) as socket:
                await socket.send(
                    json.dumps(
                        {
                            "header": {"token": token, "tr_type": "3"},
                            "body": {"tr_cd": tr_code, "tr_key": symbol},
                        }
                    )
                )
                deadline = asyncio.get_running_loop().time() + timeout
                while True:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        raise TimeoutError("LS realtime snapshot timed out")
                    message = json.loads(
                        await asyncio.wait_for(socket.recv(), timeout=remaining)
                    )
                    if not isinstance(message, Mapping):
                        raise LSReadOnlyTransportError(
                            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                            "LS realtime message is not an object",
                        )
                    header = message.get("header")
                    if isinstance(header, Mapping) and header.get("rsp_cd") is not None:
                        response_code = str(header.get("rsp_cd") or "")
                        if response_code != "00000":
                            response_message = str(header.get("rsp_msg") or "")
                            code = (
                                "INDICATOR_PROVIDER_RATE_LIMITED"
                                if "limit" in response_message.casefold()
                                else "INDICATOR_PROVIDER_UNAVAILABLE"
                            )
                            raise LSReadOnlyTransportError(code, response_message)
                        continue
                    body = message.get("body")
                    if isinstance(body, Mapping):
                        return dict(body)
        except LSReadOnlyTransportError:
            raise
        except (TimeoutError, asyncio.TimeoutError):
            raise
        except json.JSONDecodeError as exc:
            raise LSReadOnlyTransportError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                "LS realtime message is not valid JSON",
            ) from exc
        except Exception as exc:
            raise LSReadOnlyTransportError(
                "INDICATOR_PROVIDER_UNAVAILABLE",
                "LS realtime transport is unavailable",
            ) from exc


def _context_value(context: Any, key: str, default: Any = None) -> Any:
    if isinstance(context, Mapping):
        return context.get(key, default)
    return getattr(context, key, default)


def _symbol(instrument: Any) -> str:
    if isinstance(instrument, str):
        value = instrument.strip()
    elif isinstance(instrument, Mapping):
        value = str(
            instrument.get("symbol")
            or instrument.get("ticker")
            or instrument.get("code")
            or instrument.get("instrument_id")
            or ""
        ).strip()
    else:
        value = str(
            getattr(instrument, "symbol", None)
            or getattr(instrument, "ticker", None)
            or getattr(instrument, "code", None)
            or getattr(instrument, "instrument_id", None)
            or ""
        ).strip()
    if not value:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            "LS indicator instrument symbol is missing",
            retryable=False,
        )
    return value


def _aware_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is not None:
            return parsed
    return None


def _observed_at(context: Any) -> datetime:
    return _aware_datetime(_context_value(context, "observed_at")) or datetime.now(
        timezone.utc
    )


def _query_date(context: Any, observed_at: datetime) -> str:
    explicit = _context_value(context, "data_date") or _context_value(
        context, "as_of_date"
    )
    if isinstance(explicit, str) and len(explicit.replace("-", "")) == 8:
        return explicit.replace("-", "")
    return observed_at.astimezone(KST).strftime("%Y%m%d")


def _clock(context: Any) -> str:
    value = _context_value(context, "clock")
    return str(getattr(value, "value", value) or "").upper()


def _decimal(value: Any, *, field: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_PARTIAL_DATA",
            f"LS response field {field!r} is missing",
            retryable=False,
        )
    try:
        parsed = Decimal(str(value).strip())
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            f"LS response field {field!r} is not numeric",
            retryable=False,
        ) from exc
    if not parsed.is_finite():
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            f"LS response field {field!r} is not finite",
            retryable=False,
        )
    return parsed


def _timestamp_from_row(
    row: Mapping[str, Any],
    *,
    observed_at: datetime,
    realtime: bool,
) -> datetime | None:
    raw_date = str(row.get("date") or row.get("datects") or "").strip()
    raw_time = str(row.get("time") or "").strip()
    if realtime and not raw_date:
        raw_date = observed_at.astimezone(KST).strftime("%Y%m%d")
    if len(raw_date) != 8 or not raw_date.isdigit():
        return None
    if raw_time:
        raw_time = raw_time.zfill(6)
        if len(raw_time) != 6 or not raw_time.isdigit():
            return None
    else:
        # Daily TRs have no close time in their row.  End-of-day KST is the
        # conservative timestamp for freshness checks.
        raw_time = "235959"
    try:
        parsed = datetime.strptime(raw_date + raw_time, "%Y%m%d%H%M%S")
    except ValueError:
        return None
    return parsed.replace(tzinfo=KST).astimezone(timezone.utc)


def _freshness_limit(context: Any, *, realtime: bool) -> float:
    raw = _context_value(context, "max_data_age_seconds")
    if raw is None:
        return (
            DEFAULT_REALTIME_MAX_AGE_SECONDS
            if realtime
            else DEFAULT_HISTORICAL_MAX_AGE_SECONDS
        )
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            "max_data_age_seconds must be numeric",
            retryable=False,
        ) from exc
    if value <= 0:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            "max_data_age_seconds must be positive",
            retryable=False,
        )
    return value


def _route_for_tr(route: LSIndicatorRoute, tr_code: str) -> LSIndicatorRoute:
    token = tr_code.upper()
    if not route.supports_tr(token):
        raise IndicatorProviderError(
            "INDICATOR_TR_UNSUPPORTED",
            f"TR {tr_code!r} does not support {route.indicator}",
            retryable=False,
        )
    if route.indicator in {
        "FOREIGN_NET_BUY_AMOUNT",
        "INSTITUTION_NET_BUY_AMOUNT",
    } and token != "T1702":
        raise IndicatorProviderError(
            "INDICATOR_TR_UNSUPPORTED",
            f"TR {tr_code!r} has no amount mapping for {route.indicator}",
            retryable=False,
        )
    if route.indicator in {
        "FOREIGN_NET_BUY_VOLUME",
        "INSTITUTION_NET_BUY_VOLUME",
    }:
        if token == "T1716":
            field = "krx_0009" if route.indicator.startswith("FOREIGN") else "krx_0018"
            return replace(
                route,
                response_block="t1716OutBlock",
                value_field=field,
            )
        if token != "T1717":
            raise IndicatorProviderError(
                "INDICATOR_TR_UNSUPPORTED",
                f"TR {tr_code!r} has no volume mapping for {route.indicator}",
                retryable=False,
            )
    if route.indicator.startswith("SHORT_SELL") and token != "T1927":
        raise IndicatorProviderError(
            "INDICATOR_TR_UNSUPPORTED",
            f"TR {tr_code!r} has no short-sale mapping for {route.indicator}",
            retryable=False,
        )
    if route.indicator == "VI_STATUS" and token == "UVI":
        return replace(route, value_field="krx_vi_gubun")
    if route.default_tr_code is None or route.path is None:
        raise IndicatorProviderError(
            "INDICATOR_TR_UNSUPPORTED",
            f"LS read-only request mapping is not bound for {route.indicator}",
            retryable=False,
        )
    return route


def _request_payload(
    route: LSIndicatorRoute,
    *,
    symbol: str,
    query_date: str,
) -> dict[str, Any]:
    tr_code = str(route.default_tr_code or "").upper()
    if tr_code == "T1702":
        return {
            "t1702InBlock": {
                "fromdt": query_date,
                "shcode": symbol,
                "todt": query_date,
                "volvalgb": "0" if route.indicator.endswith("AMOUNT") else "1",
                "msmdgb": "0",
                "gubun": "0",
                "exchgubun": "U",
            }
        }
    if tr_code == "T1717":
        return {
            "t1717InBlock": {
                "shcode": symbol,
                "gubun": "0",
                "fromdt": "",
                "todt": query_date,
                "exchgubun": "U",
            }
        }
    if tr_code == "T1637":
        return {
            "t1637InBlock": {
                "gubun1": "1" if route.indicator.endswith("AMOUNT") else "0",
                "gubun2": "1",
                "shcode": symbol,
                "date": query_date,
                "time": "",
                "cts_idx": "9999",
                "exchgubun": "U",
            }
        }
    if tr_code == "T1927":
        return {
            "t1927InBlock": {
                "shcode": symbol,
                "date": "",
                "sdate": query_date,
                "edate": query_date,
            }
        }
    if tr_code == "T1405":
        return {
            "t1405InBlock": {
                "gubun": "0",
                "jongchk": "1",
                "cts_shcode": "",
            }
        }
    raise IndicatorProviderError(
        "INDICATOR_TR_UNSUPPORTED",
        f"no read-only request payload builder for {route.default_tr_code}",
        retryable=False,
    )


def parse_ls_indicator_response(
    raw_payload: Mapping[str, Any],
    *,
    route: LSIndicatorRoute,
    indicator_spec: Any,
    symbol: str,
    observed_at: datetime,
    context: Any,
) -> IndicatorValue:
    """Parse one raw REST/WebSocket response wholly inside the broker adapter."""

    if not isinstance(raw_payload, Mapping):
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            "LS indicator response must be an object",
            retryable=False,
        )
    response_code = str(raw_payload.get("rsp_cd") or raw_payload.get("rspCode") or "").strip()
    if response_code and response_code not in {"0000", "00000", "0"}:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_UNAVAILABLE",
            f"LS returned non-success response code {response_code}",
        )

    selected: Mapping[str, Any] | None = None
    data_timestamp: datetime | None = None
    if route.realtime:
        selected = raw_payload
    else:
        if not route.response_block:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"LS response block is not configured for {route.indicator}",
                retryable=False,
            )
        raw_rows = raw_payload.get(route.response_block)
        if not isinstance(raw_rows, list):
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_PARTIAL_DATA",
                f"LS response block {route.response_block!r} is missing or invalid",
                retryable=False,
            )
        rows = [row for row in raw_rows if isinstance(row, Mapping)]
        if len(rows) != len(raw_rows):
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"LS response block {route.response_block!r} contains invalid rows",
                retryable=False,
            )
        if route.boolean_match and not rows:
            # A present, valid empty warning block is a negative match.  The
            # query observation time is the freshness boundary; this is not
            # treated as a historical OHLCV value.
            selected = {"__boolean_match__": False}
            data_timestamp = observed_at
        else:
            matching = [
                row
                for row in rows
                if not row.get("shcode") or str(row.get("shcode")) == symbol
            ]
            if not matching:
                raise IndicatorProviderError(
                    "INDICATOR_PROVIDER_PARTIAL_DATA",
                    f"LS response has no row for {symbol}",
                    retryable=False,
                )
            selected = matching[-1]

    if selected is None:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_PARTIAL_DATA",
            "LS response did not contain a usable row",
            retryable=False,
        )

    if route.boolean_match:
        raw_value: Any = selected.get("__boolean_match__")
        if "__boolean_match__" not in selected:
            raw_value = True
    else:
        if not route.value_field or route.value_field not in selected:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_PARTIAL_DATA",
                f"LS response has no {route.value_field!r} field",
                retryable=False,
            )
        raw_value = selected.get(route.value_field)
        if raw_value is None or str(raw_value).strip() == "":
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_PARTIAL_DATA",
                f"LS response field {route.value_field!r} is empty",
                retryable=False,
            )

    if not isinstance(raw_value, bool):
        raw_value = _decimal(raw_value, field=route.value_field or "value")
    if route.indicator == "VI_STATUS" and not isinstance(raw_value, bool):
        try:
            raw_value = int(raw_value) != 0
        except (TypeError, ValueError) as exc:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                "LS VI status is not a valid status code",
                retryable=False,
            ) from exc

    if data_timestamp is None:
        data_timestamp = _timestamp_from_row(
            selected,
            observed_at=observed_at,
            realtime=route.realtime,
        )
    if data_timestamp is None:
        data_timestamp = _aware_datetime(_context_value(context, "data_timestamp"))
    if data_timestamp is None:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_PARTIAL_DATA",
            "LS indicator response has no usable data timestamp",
            retryable=False,
        )
    age = (observed_at - data_timestamp).total_seconds()
    if age < 0 or age > _freshness_limit(context, realtime=route.realtime):
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_STALE",
            f"LS {route.indicator} data is stale or from the future",
            retryable=False,
        )

    output = str(getattr(indicator_spec, "output", None) or "VALUE").upper()
    timeframe = getattr(getattr(indicator_spec, "timeframe", None), "value", None)
    timeframe = timeframe or getattr(indicator_spec, "timeframe", None)
    # Import lazily: semantic validation exposes DEFAULT_REGISTRY and importing
    # it at module load would create a registry/provider cycle.
    from ...semantic import normalized_indicator_parameters

    parameters = normalized_indicator_parameters(indicator_spec)
    return IndicatorValue(
        value=raw_value,
        indicator=route.indicator,
        source="BROKER",
        provider="LS",
        observed_at=observed_at,
        data_timestamp=data_timestamp,
        calculation_version="v1",
        output=output,
        timeframe=timeframe,
        parameters=parameters,
        market_data_source_id=_context_value(context, "market_data_source_id"),
    )


class LSReadOnlyIndicatorResolver:
    """Resolve LS indicators into normalized values and nothing else."""

    def __init__(
        self,
        transport: LSReadOnlyTransport | None = None,
        *,
        timeout_seconds: float = 5.0,
    ) -> None:
        self._transport = transport
        self.timeout_seconds = timeout_seconds

    async def __call__(
        self,
        instrument: Any,
        indicator_spec: Any,
        evaluation_context: Any,
    ) -> IndicatorValue:
        indicator = str(getattr(indicator_spec, "name", "") or "").upper()
        route = route_for_indicator(indicator)
        if route is None:
            raise IndicatorProviderError(
                "INDICATOR_TR_UNSUPPORTED",
                f"LS has no read-only route for {indicator!r}",
                retryable=False,
            )
        expected_clock = "QUOTE" if route.realtime else "BAR_CLOSE"
        requested_clock = _clock(evaluation_context)
        if requested_clock and requested_clock != expected_clock:
            raise IndicatorProviderError(
                "INDICATOR_CLOCK_MISMATCH",
                f"{indicator} requires {expected_clock}",
                retryable=False,
            )
        symbol = _symbol(instrument)
        observed_at = _observed_at(evaluation_context)
        requested_tr = str(
            _context_value(evaluation_context, "tr_code") or route.default_tr_code or ""
        ).strip()
        resolved_route = _route_for_tr(route, requested_tr)
        transport = self._transport
        if transport is None:
            try:
                transport = LSOpenAPIReadOnlyTransport.from_env()
            except Exception as exc:
                raise IndicatorProviderError(
                    "INDICATOR_PROVIDER_UNAVAILABLE",
                    "LS read-only indicator transport is not configured",
                ) from exc

        try:
            if resolved_route.realtime:
                raw_payload = await asyncio.wait_for(
                    transport.realtime_snapshot(
                        tr_code=requested_tr,
                        symbol=symbol,
                    ),
                    timeout=self.timeout_seconds,
                )
            else:
                raw_payload = await asyncio.wait_for(
                    transport.request(
                        path=str(resolved_route.path),
                        tr_code=requested_tr,
                        payload=_request_payload(
                            resolved_route,
                            symbol=symbol,
                            query_date=_query_date(evaluation_context, observed_at),
                        ),
                    ),
                    timeout=self.timeout_seconds,
                )
        except IndicatorProviderError:
            raise
        except LSReadOnlyTransportError as exc:
            raise IndicatorProviderError(exc.code, str(exc)) from exc
        except (TimeoutError, asyncio.TimeoutError) as exc:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_TIMEOUT",
                "LS read-only indicator request timed out",
            ) from exc
        except Exception as exc:
            response = getattr(exc, "response", None)
            if getattr(response, "status_code", None) == 429:
                code = "INDICATOR_PROVIDER_RATE_LIMITED"
            else:
                code = "INDICATOR_PROVIDER_UNAVAILABLE"
            raise IndicatorProviderError(
                code,
                "LS read-only indicator request failed",
            ) from exc

        return parse_ls_indicator_response(
            raw_payload,
            route=resolved_route,
            indicator_spec=indicator_spec,
            symbol=symbol,
            observed_at=observed_at,
            context=evaluation_context,
        )


__all__ = [
    "LSOpenAPIReadOnlyTransport",
    "LSReadOnlyIndicatorResolver",
    "LSReadOnlyTransport",
    "LSReadOnlyTransportError",
    "parse_ls_indicator_response",
]
