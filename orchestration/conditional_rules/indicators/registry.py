"""Indicator capability registry and default local/broker definitions."""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal
from typing import Any

from ..contracts import ValueUnit
from .base import IndicatorDefinition, IndicatorProvider, IndicatorProviderError
from .local import calculate_local_indicator


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
        provider_name = (requested or definition.provider or "").strip().upper()
        return self._providers.get(provider_name) if provider_name else None

    async def resolve(
        self, instrument: Any, indicator_spec: Any, evaluation_context: Any
    ) -> Any:
        definition = self.get(getattr(indicator_spec, "name", None))
        if definition is None:
            raise IndicatorProviderError(
                "UNSUPPORTED_INDICATOR", f"unsupported indicator {getattr(indicator_spec, 'name', None)!r}", retryable=False
            )
        provider = self.provider_for(definition, getattr(indicator_spec, "provider", None))
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
        return await provider.resolve(instrument, indicator_spec, evaluation_context)


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
    return IndicatorRegistry(definitions)


DEFAULT_REGISTRY = build_default_registry()
