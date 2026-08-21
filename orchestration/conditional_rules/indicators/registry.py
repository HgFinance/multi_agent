"""Indicator capability registry and default local/broker definitions."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
from decimal import Decimal
from inspect import Parameter, signature
from typing import Any

from ..contracts import ValueUnit
from .base import (
    IndicatorDefinition,
    IndicatorProvider,
    IndicatorProviderError,
    IndicatorValue,
)
from .local import calculate_local_indicator
from .providers import LSBrokerIndicatorProvider, LocalIndicatorProvider
from .broker.ls_readonly import LSReadOnlyIndicatorResolver


_ALL_TIMEFRAMES = frozenset({"1M", "5M", "15M", "1H", "1D"})
_NUMERIC = frozenset({"PERIOD"})


class IndicatorRegistry:
    def __init__(self, definitions: Iterable[IndicatorDefinition] = ()) -> None:
        self._definitions: dict[str, IndicatorDefinition] = {}
        self._aliases: dict[str, str] = {}
        self._providers: dict[str, IndicatorProvider] = {}
        for definition in definitions:
            self.register(definition)

    def register(self, definition: IndicatorDefinition) -> None:
        name = definition.name.strip().upper()
        if not name or name != definition.name:
            raise ValueError("indicator names must be canonical uppercase tokens")
        if definition.source == "LOCAL":
            if definition.calculator is None:
                raise ValueError(f"local indicator requires a calculator: {name}")
            try:
                parameters = tuple(signature(definition.calculator).parameters.values())
            except (TypeError, ValueError) as exc:
                raise ValueError(f"calculator signature is unavailable: {name}") from exc
            positional = tuple(
                item
                for item in parameters
                if item.kind
                in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
            )
            if (
                len(parameters) != 4
                or len(positional) != 4
                or any(
                    item.kind
                    not in (Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD)
                    for item in parameters
                )
                or tuple(item.name for item in parameters)
                != ("name", "candles", "parameters", "output")
            ):
                raise ValueError(
                    f"local calculator must have signature "
                    f"(name, candles, parameters, output): {name}"
                )
        elif definition.calculator is not None:
            raise ValueError(f"provider-backed indicator cannot have a calculator: {name}")
        if name in self._definitions:
            raise ValueError(f"indicator already registered: {name}")
        self._definitions[name] = definition
        for alias in definition.aliases:
            canonical = alias.strip().upper()
            if not canonical or canonical in self._definitions or canonical in self._aliases:
                raise ValueError(f"indicator alias already registered: {alias}")
            self._aliases[canonical] = name

    def register_provider(self, provider: IndicatorProvider) -> None:
        name = str(provider.name).strip().upper()
        if not name:
            raise ValueError("provider name is required")
        if name in self._providers:
            raise ValueError(f"provider already registered: {name}")
        self._providers[name] = provider

    def bind_provider(self, provider: IndicatorProvider) -> None:
        """Bind an adapter at application startup without changing rule ASTs.

        This is the explicit LS connection point.  The default registry keeps
        an unconfigured provider, and an application may replace that seam
        with ``LSBrokerIndicatorProvider(resolver=...)`` once the real market
        data adapter is ready.
        """

        name = str(provider.name).strip().upper()
        if not name:
            raise ValueError("provider name is required")
        self._providers[name] = provider

    def canonical_name(self, name: str | None) -> str:
        token = str(name or "").strip().upper()
        return self._aliases.get(token, token)

    def get(self, name: str | None) -> IndicatorDefinition | None:
        return self._definitions.get(self.canonical_name(name))

    @property
    def definitions(self) -> dict[str, IndicatorDefinition]:
        return dict(self._definitions)

    def list(self, *, source: str | None = None) -> tuple[IndicatorDefinition, ...]:
        values = tuple(self._definitions.values())
        if source is None:
            return values
        source_token = source.strip().upper()
        return tuple(value for value in values if value.source == source_token)

    def provider_for(
        self, definition: IndicatorDefinition, requested: str | None = None
    ) -> IndicatorProvider | None:
        provider_name = (
            requested or definition.provider or definition.source or ""
        ).strip().upper()
        return self._providers.get(provider_name) if provider_name else None

    @staticmethod
    def _context_value(context: Any, key: str) -> Any:
        if isinstance(context, dict):
            return context.get(key)
        return getattr(context, key, None)

    @staticmethod
    def _parameter_signature(parameters: Any) -> dict[str, str]:
        if not isinstance(parameters, dict) and not hasattr(parameters, "items"):
            return {}
        return {
            str(key).upper(): str(value)
            for key, value in sorted(parameters.items(), key=lambda item: str(item[0]))
        }

    def normalize_value(
        self,
        definition: IndicatorDefinition,
        indicator_spec: Any,
        value: Any,
        evaluation_context: Any,
    ) -> IndicatorValue:
        if not isinstance(value, IndicatorValue):
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"{definition.name} provider must return IndicatorValue",
                retryable=False,
            )
        if value.value is None:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_PARTIAL_DATA",
                f"{definition.name} provider returned no value",
                retryable=False,
            )
        if isinstance(value.value, bool):
            pass
        elif isinstance(value.value, Decimal) and value.value.is_finite():
            pass
        else:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"{definition.name} provider returned a non-normalized scalar",
                retryable=False,
            )
        if not isinstance(value.observed_at, datetime) or value.observed_at.tzinfo is None:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"{definition.name} provider timestamp is not timezone-aware",
                retryable=False,
            )
        if value.data_timestamp is not None and (
            not isinstance(value.data_timestamp, datetime)
            or value.data_timestamp.tzinfo is None
        ):
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"{definition.name} provider data timestamp is invalid",
                retryable=False,
            )
        if not isinstance(value.parameters, Mapping):
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_INVALID_PAYLOAD",
                f"{definition.name} provider parameters are invalid",
                retryable=False,
            )

        expected_output = str(getattr(indicator_spec, "output", None) or "VALUE").upper()
        expected_timeframe = getattr(
            getattr(indicator_spec, "timeframe", None), "value", None
        ) or getattr(indicator_spec, "timeframe", None)
        from ..semantic import normalized_indicator_parameters

        expected_parameters = normalized_indicator_parameters(indicator_spec)
        expected_provider = (definition.provider or definition.source).upper()
        expected_market_data_source = self._context_value(
            evaluation_context, "market_data_source_id"
        )
        actual_provider = str(value.provider or "").strip().upper()
        actual_source = str(value.source or "").strip().upper()
        mismatches = (
            actual_source != definition.source,
            str(value.indicator or "").strip().upper() != definition.name,
            actual_provider != expected_provider,
            value.output not in (None, "")
            and str(value.output).upper() != expected_output,
            value.timeframe not in (None, "")
            and str(value.timeframe).upper() != str(expected_timeframe).upper(),
            bool(value.parameters)
            and self._parameter_signature(value.parameters)
            != self._parameter_signature(expected_parameters),
            value.calculation_version not in (None, "")
            and value.calculation_version != definition.calculation_version,
            value.market_data_source_id not in (None, "")
            and expected_market_data_source not in (None, "")
            and value.market_data_source_id != expected_market_data_source,
        )
        if any(mismatches):
            raise IndicatorProviderError(
                "INDICATOR_VALUE_PROVENANCE_MISMATCH",
                f"{definition.name} provider value provenance does not match the registry",
                retryable=False,
            )
        if value.observed_at is None:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_PARTIAL_DATA",
                f"{definition.name} provider value has no observed timestamp",
                retryable=False,
            )
        return value.with_identity(
            indicator=definition.name,
            source=definition.source,
            provider=expected_provider,
            output=expected_output,
            timeframe=expected_timeframe,
            parameters=expected_parameters,
            calculation_version=definition.calculation_version,
            market_data_source_id=expected_market_data_source,
        )

    async def resolve(
        self, instrument: Any, indicator_spec: Any, evaluation_context: Any
    ) -> IndicatorValue:
        definition = self.get(getattr(indicator_spec, "name", None))
        if definition is None:
            raise IndicatorProviderError(
                "UNSUPPORTED_INDICATOR",
                f"unsupported indicator {getattr(indicator_spec, 'name', None)!r}",
                retryable=False,
            )
        requested_source = getattr(indicator_spec, "source", None)
        requested_source = getattr(requested_source, "value", requested_source)
        if requested_source is not None and str(requested_source).upper() != definition.source:
            raise IndicatorProviderError(
                "INDICATOR_SOURCE_MISMATCH",
                f"{definition.name} belongs to source {definition.source}",
                retryable=False,
            )
        requested_provider = getattr(indicator_spec, "provider", None)
        if requested_provider is not None:
            expected_provider = (definition.provider or definition.source).upper()
            if str(requested_provider).upper() != expected_provider:
                raise IndicatorProviderError(
                    "INDICATOR_PROVIDER_MISMATCH",
                    f"{definition.name} belongs to provider {expected_provider}",
                    retryable=False,
                )
        provider = self.provider_for(definition, requested_provider)
        if provider is None:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_UNAVAILABLE",
                f"provider is unavailable for {definition.name}",
            )
        if not provider.supports(indicator_spec):
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_UNSUPPORTED",
                f"provider does not support {definition.name}",
                retryable=False,
            )
        try:
            value = await provider.resolve(instrument, indicator_spec, evaluation_context)
        except IndicatorProviderError:
            raise
        except TimeoutError as exc:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_TIMEOUT",
                f"provider timed out while resolving {definition.name}",
            ) from exc
        except Exception as exc:
            raise IndicatorProviderError(
                "INDICATOR_PROVIDER_RESOLUTION_FAILED",
                f"provider failed while resolving {definition.name}",
            ) from exc
        return self.normalize_value(
            definition, indicator_spec, value, evaluation_context
        )


def _local(
    name: str,
    *,
    aliases: tuple[str, ...] = (),
    category: str,
    outputs: dict[str, ValueUnit],
    defaults: dict[str, int | Decimal | str],
    integer_parameters: frozenset[str] = frozenset(),
    warmup_bars: int | Any = 1,
) -> IndicatorDefinition:
    return IndicatorDefinition(
        name=name,
        aliases=aliases,
        category=category,
        source="LOCAL",
        outputs=outputs,
        defaults=defaults,
        integer_parameters=integer_parameters,
        supported_timeframes=_ALL_TIMEFRAMES,
        warmup_bars=warmup_bars,
        calculator=calculate_local_indicator,
    )


def _broker(
    name: str,
    *,
    category: str,
    unit: ValueUnit,
    aliases: tuple[str, ...] = (),
    output: str = "VALUE",
    realtime: bool = False,
    historical: bool = True,
    required_parameters: frozenset[str] = frozenset(),
    string_parameters: frozenset[str] = frozenset(),
) -> IndicatorDefinition:
    return IndicatorDefinition(
        name=name,
        aliases=aliases,
        category=category,
        source="BROKER",
        provider="LS",
        outputs={output: unit},
        string_parameters=string_parameters,
        required_parameters=required_parameters,
        supported_timeframes=_ALL_TIMEFRAMES,
        warmup_bars=0,
        update_mode="REALTIME" if realtime else "POLLING",
        cache_policy="OBSERVED_TIMESTAMP",
        historical_supported=historical,
        realtime_supported=realtime,
        calculator=None,
    )


def build_default_registry() -> IndicatorRegistry:
    period = frozenset({"PERIOD"})
    definitions = [
        _local("SMA", aliases=("이평", "이동평균", "이동평균선"), category="TREND", outputs={"VALUE": ValueUnit.PRICE}, defaults={"PERIOD": 20}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("EMA", category="TREND", outputs={"VALUE": ValueUnit.PRICE}, defaults={"PERIOD": 20}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("RSI", aliases=("알에스아이", "상대강도지수", "상대강도"), category="MOMENTUM", outputs={"VALUE": ValueUnit.NUMBER}, defaults={"PERIOD": 14}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"]) + 1),
        _local("MACD", category="MOMENTUM", outputs={"MACD": ValueUnit.PRICE, "SIGNAL": ValueUnit.PRICE, "HISTOGRAM": ValueUnit.PRICE}, defaults={"FAST": 12, "SLOW": 26, "SIGNAL": 9}, integer_parameters=frozenset({"FAST", "SLOW", "SIGNAL"}), warmup_bars=lambda p: int(p["SLOW"]) + int(p["SIGNAL"]) - 1),
        _local("BOLLINGER", aliases=("볼린저", "볼린저밴드"), category="VOLATILITY", outputs={"UPPER": ValueUnit.PRICE, "MIDDLE": ValueUnit.PRICE, "LOWER": ValueUnit.PRICE}, defaults={"PERIOD": 20, "STDDEV": Decimal("2")}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("VOLUME_AVERAGE", aliases=("AVERAGE_VOLUME", "거래량평균"), category="VOLUME", outputs={"VALUE": ValueUnit.VOLUME}, defaults={"PERIOD": 20}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("ATR", category="VOLATILITY", outputs={"VALUE": ValueUnit.PRICE}, defaults={"PERIOD": 14}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"]) + 1),
        _local("ADX", aliases=("평균방향성지수",), category="TREND", outputs={"VALUE": ValueUnit.NUMBER}, defaults={"PERIOD": 14}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"]) * 2 + 1),
        _local("STOCHASTIC", aliases=("STOCH", "스토캐스틱"), category="MOMENTUM", outputs={"K": ValueUnit.NUMBER, "D": ValueUnit.NUMBER}, defaults={"PERIOD": 14, "SMOOTH": 3}, integer_parameters=frozenset({"PERIOD", "SMOOTH"}), warmup_bars=lambda p: int(p["PERIOD"]) + int(p["SMOOTH"]) - 1),
        _local("CCI", aliases=("상품채널지수",), category="MOMENTUM", outputs={"VALUE": ValueUnit.NUMBER}, defaults={"PERIOD": 20}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("MFI", aliases=("자금흐름지수",), category="VOLUME", outputs={"VALUE": ValueUnit.NUMBER}, defaults={"PERIOD": 14}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"]) + 1),
        _local("OBV", category="VOLUME", outputs={"VALUE": ValueUnit.VOLUME}, defaults={}, warmup_bars=2),
        _local("ROC", aliases=("RATE_OF_CHANGE", "변화율"), category="MOMENTUM", outputs={"VALUE": ValueUnit.NUMBER}, defaults={"PERIOD": 12}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"]) + 1),
        _local("VWAP", category="VOLUME", outputs={"VALUE": ValueUnit.PRICE}, defaults={"PERIOD": 20}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("WILLIAMS_R", aliases=("WILLIAMS", "윌리엄스R"), category="MOMENTUM", outputs={"VALUE": ValueUnit.NUMBER}, defaults={"PERIOD": 14}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("DONCHIAN", aliases=("돈치안",), category="VOLATILITY", outputs={"UPPER": ValueUnit.PRICE, "MIDDLE": ValueUnit.PRICE, "LOWER": ValueUnit.PRICE}, defaults={"PERIOD": 20}, integer_parameters=period, warmup_bars=lambda p: int(p["PERIOD"])),
        _local("PSAR", aliases=("PARABOLIC_SAR", "파라볼릭SAR"), category="TREND", outputs={"VALUE": ValueUnit.PRICE, "TREND": ValueUnit.BOOL}, defaults={"STEP": Decimal("0.02"), "MAXIMUM": Decimal("0.2")}, warmup_bars=2),
        _broker("FOREIGN_NET_BUY_VOLUME", aliases=("외국인순매수량", "외인순매수량"), category="INVESTOR_FLOW", unit=ValueUnit.VOLUME),
        _broker("FOREIGN_NET_BUY_AMOUNT", aliases=("외국인순매수대금", "외인순매수대금"), category="INVESTOR_FLOW", unit=ValueUnit.KRW),
        _broker("INSTITUTION_NET_BUY_VOLUME", aliases=("기관순매수량",), category="INVESTOR_FLOW", unit=ValueUnit.VOLUME),
        _broker("INSTITUTION_NET_BUY_AMOUNT", aliases=("기관순매수대금",), category="INVESTOR_FLOW", unit=ValueUnit.KRW),
        _broker("PROGRAM_NET_BUY_VOLUME", aliases=("프로그램순매수량",), category="INVESTOR_FLOW", unit=ValueUnit.VOLUME),
        _broker("PROGRAM_NET_BUY_AMOUNT", aliases=("프로그램순매수대금",), category="INVESTOR_FLOW", unit=ValueUnit.KRW),
        _broker("SHORT_SELL_VOLUME", category="FUNDAMENTAL", unit=ValueUnit.VOLUME),
        _broker("SHORT_SELL_RATIO", category="FUNDAMENTAL", unit=ValueUnit.RATIO),
        _broker("VI_STATUS", category="BROKER_SIGNAL", unit=ValueUnit.BOOL, realtime=True, historical=False),
        _broker("MARKET_WARNING_STATUS", category="BROKER_SIGNAL", unit=ValueUnit.BOOL, realtime=False),
        _broker("BROKER_SEARCH_MATCH", aliases=("LS_SIGNAL", "LS_ITEM_SEARCH_MATCH"), category="BROKER_SIGNAL", unit=ValueUnit.BOOL, required_parameters=frozenset({"SEARCH_ID"}), string_parameters=frozenset({"SEARCH_ID"})),
    ]
    registry = IndicatorRegistry(definitions)
    # Bind the read-only LS resolver at the provider boundary.  Construction is
    # side-effect free; credentials/transport are acquired only on resolve.
    # Therefore an unavailable adapter still fails closed and can never become
    # a LOCAL/OHLCV fallback.
    registry.register_provider(LocalIndicatorProvider())
    registry.register_provider(
        LSBrokerIndicatorProvider(resolver=LSReadOnlyIndicatorResolver())
    )
    return registry


DEFAULT_REGISTRY = build_default_registry()
