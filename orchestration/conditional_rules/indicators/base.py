"""Stable contracts shared by local calculators and broker providers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Any, Awaitable, Callable, Mapping, Protocol

from ..contracts import ValueUnit


IndicatorScalar = Decimal | bool
IndicatorParameters = Mapping[str, Decimal | int | str]
IndicatorCalculator = Callable[
    [str, list[Any], IndicatorParameters, str], IndicatorScalar
]
WarmupCalculator = Callable[[IndicatorParameters], int]
IndicatorResolver = Callable[[Any, Any, Any], Awaitable["IndicatorValue"]]


class IndicatorCalculationError(RuntimeError):
    """A deterministic local calculation failure that must fail closed."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class IndicatorProviderError(RuntimeError):
    """A provider failure; callers must not turn it into an order."""

    def __init__(self, code: str, message: str, *, retryable: bool = True) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass(frozen=True)
class IndicatorValue:
    """A normalized provider value plus the provenance needed for audit."""

    value: IndicatorScalar
    indicator: str
    source: str
    provider: str | None
    observed_at: datetime
    data_timestamp: datetime | None = None
    calculation_version: str | None = None
    output: str = "VALUE"
    timeframe: str | None = None
    parameters: IndicatorParameters = field(default_factory=dict)
    market_data_source_id: str | None = None

    def with_identity(
        self,
        *,
        indicator: str,
        source: str,
        provider: str | None,
        output: str,
        timeframe: str | None,
        parameters: IndicatorParameters,
        calculation_version: str,
        market_data_source_id: str | None = None,
    ) -> "IndicatorValue":
        """Fill omitted identity fields after the provider contract is checked."""

        return replace(
            self,
            indicator=indicator,
            source=source,
            provider=provider,
            output=output,
            timeframe=timeframe,
            parameters=dict(parameters),
            calculation_version=calculation_version,
            market_data_source_id=(
                self.market_data_source_id
                if self.market_data_source_id is not None
                else market_data_source_id
            ),
        )


class IndicatorProvider(Protocol):
    """Provider boundary used by broker/native and realtime indicators."""

    name: str

    def supports(self, indicator_spec: Any) -> bool: ...

    async def resolve(
        self,
        instrument: Any,
        indicator_spec: Any,
        evaluation_context: Any,
    ) -> IndicatorValue: ...


@dataclass(frozen=True)
class IndicatorDefinition:
    """The source of truth for one canonical indicator capability."""

    name: str
    aliases: tuple[str, ...] = ()
    category: str = "PRICE"
    source: str = "LOCAL"
    provider: str | None = None
    outputs: Mapping[str, ValueUnit] = field(default_factory=dict)
    defaults: Mapping[str, int | Decimal | str] = field(default_factory=dict)
    integer_parameters: frozenset[str] = frozenset()
    string_parameters: frozenset[str] = frozenset()
    required_parameters: frozenset[str] = frozenset()
    supported_markets: frozenset[str] = frozenset({"KRX"})
    supported_timeframes: frozenset[str] = frozenset(
        {"1M", "5M", "15M", "1H", "1D"}
    )
    warmup_bars: int | WarmupCalculator = 1
    update_mode: str = "BAR_CLOSE"
    cache_policy: str = "BAR_TIMESTAMP"
    nullable: bool = False
    historical_supported: bool = True
    realtime_supported: bool = False
    calculation_version: str = "v1"
    calculator: IndicatorCalculator | None = field(default=None, repr=False)

    def required_history(self, parameters: IndicatorParameters) -> int:
        # Provider-backed values arrive through their provider contract.  They
        # must never cause the worker to manufacture an OHLCV warm-up request.
        if self.source != "LOCAL":
            return 0
        value = (
            self.warmup_bars(parameters)
            if callable(self.warmup_bars)
            else self.warmup_bars
        )
        return max(1, int(value))

    def metadata(self) -> dict[str, Any]:
        """Return JSON-safe catalog metadata without exposing implementation code."""

        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "category": self.category,
            "source": self.source,
            "provider": self.provider,
            "outputs": {key: value.value for key, value in self.outputs.items()},
            "defaults": {key: str(value) for key, value in self.defaults.items()},
            "supported_markets": sorted(self.supported_markets),
            "supported_timeframes": sorted(self.supported_timeframes),
            "warmup_bars": (
                "dynamic" if callable(self.warmup_bars) else self.warmup_bars
            ),
            "update_mode": self.update_mode,
            "cache_policy": self.cache_policy,
            "nullable": self.nullable,
            "historical_supported": self.historical_supported,
            "realtime_supported": self.realtime_supported,
            "calculation_version": self.calculation_version,
        }
