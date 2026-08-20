"""Provider implementations that do not own order authority."""

from __future__ import annotations

from typing import Any

from .base import (
    IndicatorCalculationError,
    IndicatorProviderError,
    IndicatorResolver,
    IndicatorValue,
)
from .broker.ls import route_for_indicator


Resolver = IndicatorResolver


def _context_value(context: Any, key: str) -> Any:
    if isinstance(context, dict):
        return context.get(key)
    return getattr(context, key, None)


async def _invoke_resolver(
    resolver: Resolver | None,
    instrument: Any,
    indicator_spec: Any,
    evaluation_context: Any,
    *,
    provider_name: str,
) -> IndicatorValue:
    if resolver is None:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_UNAVAILABLE",
            f"{provider_name} indicator resolver is not configured",
        )
    try:
        value = await resolver(instrument, indicator_spec, evaluation_context)
    except IndicatorProviderError:
        raise
    except TimeoutError as exc:
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_TIMEOUT",
            f"{provider_name} indicator resolver timed out",
        ) from exc
    if not isinstance(value, IndicatorValue):
        raise IndicatorProviderError(
            "INDICATOR_PROVIDER_INVALID_PAYLOAD",
            f"{provider_name} indicator resolver must return IndicatorValue",
            retryable=False,
        )
    return value


class LSBrokerIndicatorProvider:
    """Adapter seam for LS native signals and investor-flow data.

    The resolver is injected by the LS market-data adapter.  Keeping it out of
    the AST means a TR replacement changes one adapter mapping, not persisted
    rules.  Without an injected resolver this provider deliberately fails
    closed instead of fabricating a value.
    """

    name = "LS"

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver = resolver

    def supports(self, indicator_spec: Any) -> bool:
        provider = getattr(indicator_spec, "provider", None)
        if provider not in (None, "LS"):
            return False
        return route_for_indicator(getattr(indicator_spec, "name", None)) is not None

    async def resolve(
        self, instrument: Any, indicator_spec: Any, evaluation_context: Any
    ) -> IndicatorValue:
        route = route_for_indicator(getattr(indicator_spec, "name", None))
        if route is None:
            raise IndicatorProviderError(
                "INDICATOR_TR_UNSUPPORTED",
                f"LS has no TR route for {getattr(indicator_spec, 'name', None)!r}",
                retryable=False,
            )
        requested_tr = _context_value(evaluation_context, "tr_code")
        if requested_tr is not None and not route.supports_tr(str(requested_tr)):
            raise IndicatorProviderError(
                "INDICATOR_TR_UNSUPPORTED",
                f"TR {requested_tr!r} does not support {route.indicator}",
                retryable=False,
            )
        return await _invoke_resolver(
            self._resolver,
            instrument,
            indicator_spec,
            evaluation_context,
            provider_name="LS",
        )


class LocalIndicatorProvider:
    """Optional async facade over the same deterministic local calculators."""

    name = "LOCAL"

    def supports(self, indicator_spec: Any) -> bool:
        source = getattr(indicator_spec, "source", None)
        return source in (None, "LOCAL")

    async def resolve(
        self, instrument: Any, indicator_spec: Any, evaluation_context: Any
    ) -> IndicatorValue:
        from .registry import DEFAULT_REGISTRY
        from ..semantic import normalized_indicator_parameters

        definition = DEFAULT_REGISTRY.get(getattr(indicator_spec, "name", None))
        if definition is None or definition.calculator is None:
            raise IndicatorProviderError(
                "UNSUPPORTED_INDICATOR",
                f"unsupported local indicator {getattr(indicator_spec, 'name', None)!r}",
                retryable=False,
            )
        candles = (
            evaluation_context.get("candles")
            if isinstance(evaluation_context, dict)
            else getattr(evaluation_context, "candles", None)
        )
        if not isinstance(candles, list):
            raise IndicatorProviderError(
                "INDICATOR_INPUT_UNAVAILABLE",
                f"OHLCV input is unavailable for {definition.name}",
            )
        try:
            value = definition.calculator(
                definition.name,
                candles,
                normalized_indicator_parameters(indicator_spec),
                getattr(indicator_spec, "output", None) or "VALUE",
            )
        except IndicatorCalculationError as exc:
            raise IndicatorProviderError(exc.code, str(exc), retryable=False) from exc
        except Exception as exc:
            raise IndicatorProviderError(
                "INDICATOR_CALCULATION_FAILED", str(exc), retryable=False
            ) from exc
        observed_at = _context_value(evaluation_context, "observed_at")
        return IndicatorValue(
            value=value,
            indicator=definition.name,
            source="LOCAL",
            provider="LOCAL",
            observed_at=observed_at,
            calculation_version=definition.calculation_version,
            output=getattr(indicator_spec, "output", None) or "VALUE",
            timeframe=(
                getattr(getattr(indicator_spec, "timeframe", None), "value", None)
                or getattr(indicator_spec, "timeframe", None)
            ),
            parameters=normalized_indicator_parameters(indicator_spec),
            market_data_source_id=_context_value(
                evaluation_context, "market_data_source_id"
            ),
        )


class _CallbackProvider:
    def __init__(self, name: str, resolver: Resolver | None = None) -> None:
        self.name = name
        self._resolver = resolver

    def supports(self, indicator_spec: Any) -> bool:
        return str(getattr(indicator_spec, "provider", "") or self.name).upper() == self.name

    async def resolve(
        self, instrument: Any, indicator_spec: Any, evaluation_context: Any
    ) -> IndicatorValue:
        return await _invoke_resolver(
            self._resolver,
            instrument,
            indicator_spec,
            evaluation_context,
            provider_name=self.name,
        )


class MarketMicrostructureProvider(_CallbackProvider):
    def __init__(self, resolver: Resolver | None = None) -> None:
        super().__init__("MICROSTRUCTURE", resolver)


class PortfolioIndicatorProvider(_CallbackProvider):
    def __init__(self, resolver: Resolver | None = None) -> None:
        super().__init__("PORTFOLIO", resolver)
