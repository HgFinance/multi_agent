"""Provider implementations that do not own order authority."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .base import IndicatorProviderError, IndicatorValue


Resolver = Callable[[Any, Any, Any], Awaitable[IndicatorValue]]


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
        return provider in (None, "LS")

    async def resolve(
        self, instrument: Any, indicator_spec: Any, evaluation_context: Any
    ) -> IndicatorValue:
        if self._resolver is None:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_UNAVAILABLE",
                "LS indicator resolver is not configured",
            )
        return await self._resolver(instrument, indicator_spec, evaluation_context)
