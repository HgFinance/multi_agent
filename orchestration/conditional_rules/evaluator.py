"""Pure, deterministic indicator and expression evaluation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext
from typing import Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .contracts import (
    ConditionalRuleSpec,
    ExpressionNode,
    ExpressionType,
    Timeframe,
)
from .indicators import (
    DEFAULT_REGISTRY,
    IndicatorCalculationError,
    IndicatorProviderError,
    IndicatorValue,
)
from .semantic import (
    RuleSemanticError,
    indicator_definition,
    indicator_source,
    normalized_indicator_parameters,
)


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


class EvaluationError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Candle(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    bucket_time: datetime
    open: Decimal = Field(gt=0)
    high: Decimal = Field(gt=0)
    low: Decimal = Field(gt=0)
    close: Decimal = Field(gt=0)
    volume: Decimal = Field(ge=0)
    is_final: bool = True

    @field_validator("bucket_time")
    @classmethod
    def _aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("candle bucket_time must include timezone")
        return value

    @field_validator("open", "high", "low", "close", "volume")
    @classmethod
    def _market_value_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("candle values must be finite")
        return value

    @model_validator(mode="after")
    def _valid_range(self) -> "Candle":
        if self.low > self.high or not self.low <= self.open <= self.high:
            raise ValueError("candle open must be within low/high")
        if not self.low <= self.close <= self.high:
            raise ValueError("candle close must be within low/high")
        return self


@dataclass(frozen=True)
class EvaluationFrame:
    market: Mapping[str, Decimal]
    portfolio: Mapping[str, Decimal]
    indicators: Mapping[str, Decimal | bool]
    observed_at: datetime
    market_data_source_id: str | None = None


@dataclass(frozen=True)
class EvaluationContext:
    current: EvaluationFrame
    previous: EvaluationFrame | None = None
    market_data_source_id: str | None = None
    calculation_profile: str = "DEFAULT"


def indicator_key(
    node: ExpressionNode, *, market_data_source_id: str | None = None
) -> str:
    definition = DEFAULT_REGISTRY.get(node.name)
    payload = {
        "name": DEFAULT_REGISTRY.canonical_name(node.name),
        "output": node.output or "VALUE",
        "timeframe": node.timeframe.value if node.timeframe else None,
        "parameters": {
            key: str(value)
            for key, value in sorted(normalized_indicator_parameters(node).items())
        },
        "source": definition.source if definition is not None else None,
        "provider": (
            (definition.provider or definition.source) if definition is not None else None
        ),
        "calculation_version": (
            definition.calculation_version if definition is not None else None
        ),
        "market_data_source_id": market_data_source_id,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise EvaluationError("INSUFFICIENT_HISTORY", "indicator input is empty")
    return sum(values, ZERO) / Decimal(len(values))


def _ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        raise EvaluationError(
            "INSUFFICIENT_HISTORY",
            f"EMA({period}) requires at least {period} completed bars",
        )
    seed = _mean(values[:period])
    multiplier = Decimal(2) / Decimal(period + 1)
    result = [seed]
    for value in values[period:]:
        result.append((value - result[-1]) * multiplier + result[-1])
    return result


def _wilder_series(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        raise EvaluationError(
            "INSUFFICIENT_HISTORY",
            f"Wilder({period}) requires at least {period} observations",
        )
    result = [_mean(values[:period])]
    for value in values[period:]:
        result.append((result[-1] * Decimal(period - 1) + value) / Decimal(period))
    return result


def _rsi(closes: list[Decimal], period: int) -> Decimal:
    if len(closes) < period + 1:
        raise EvaluationError(
            "INSUFFICIENT_HISTORY",
            f"RSI({period}) requires at least {period + 1} completed bars",
        )
    changes = [right - left for left, right in zip(closes, closes[1:])]
    gains = [max(value, ZERO) for value in changes]
    losses = [max(-value, ZERO) for value in changes]
    average_gain = _wilder_series(gains, period)[-1]
    average_loss = _wilder_series(losses, period)[-1]
    if average_loss == 0:
        return HUNDRED if average_gain > 0 else Decimal("50")
    strength = average_gain / average_loss
    return HUNDRED - HUNDRED / (ONE + strength)


def _macd(
    closes: list[Decimal], *, fast: int, slow: int, signal: int
) -> dict[str, Decimal]:
    if len(closes) < slow + signal - 1:
        raise EvaluationError(
            "INSUFFICIENT_HISTORY",
            f"MACD({fast},{slow},{signal}) requires {slow + signal - 1} bars",
        )
    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    fast_offset = slow - fast
    aligned_fast = fast_series[fast_offset:]
    macd_series = [a - b for a, b in zip(aligned_fast, slow_series)]
    signal_series = _ema_series(macd_series, signal)
    macd_value = macd_series[-1]
    signal_value = signal_series[-1]
    return {
        "MACD": macd_value,
        "SIGNAL": signal_value,
        "HISTOGRAM": macd_value - signal_value,
    }


def _bollinger(
    closes: list[Decimal], *, period: int, standard_deviations: Decimal
) -> dict[str, Decimal]:
    if len(closes) < period:
        raise EvaluationError(
            "INSUFFICIENT_HISTORY",
            f"Bollinger({period}) requires at least {period} completed bars",
        )
    window = closes[-period:]
    middle = _mean(window)
    variance = _mean([(value - middle) ** 2 for value in window])
    with localcontext() as context:
        context.prec = 34
        deviation = context.sqrt(variance)
    width = deviation * standard_deviations
    return {"UPPER": middle + width, "MIDDLE": middle, "LOWER": middle - width}


def _true_ranges(candles: list[Candle]) -> list[Decimal]:
    values: list[Decimal] = []
    for previous, current in zip(candles, candles[1:]):
        values.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )
    return values


def _atr(candles: list[Candle], period: int) -> Decimal:
    if len(candles) < period + 1:
        raise EvaluationError(
            "INSUFFICIENT_HISTORY",
            f"ATR({period}) requires at least {period + 1} completed bars",
        )
    return _wilder_series(_true_ranges(candles), period)[-1]


def _adx(candles: list[Candle], period: int) -> Decimal:
    if len(candles) < period * 2 + 1:
        raise EvaluationError(
            "INSUFFICIENT_HISTORY",
            f"ADX({period}) requires at least {period * 2 + 1} completed bars",
        )
    true_ranges = _true_ranges(candles)
    plus_dm: list[Decimal] = []
    minus_dm: list[Decimal] = []
    for previous, current in zip(candles, candles[1:]):
        up = current.high - previous.high
        down = previous.low - current.low
        plus_dm.append(up if up > down and up > 0 else ZERO)
        minus_dm.append(down if down > up and down > 0 else ZERO)
    atr_values = _wilder_series(true_ranges, period)
    plus_values = _wilder_series(plus_dm, period)
    minus_values = _wilder_series(minus_dm, period)
    dx_values: list[Decimal] = []
    for atr_value, plus_value, minus_value in zip(
        atr_values, plus_values, minus_values
    ):
        if atr_value == 0:
            dx_values.append(ZERO)
            continue
        plus_index = HUNDRED * plus_value / atr_value
        minus_index = HUNDRED * minus_value / atr_value
        denominator = plus_index + minus_index
        dx_values.append(
            ZERO if denominator == 0 else HUNDRED * abs(plus_index - minus_index) / denominator
        )
    return _wilder_series(dx_values, period)[-1]


def _final_candles(candles: list[Candle]) -> list[Candle]:
    completed = [candle for candle in candles if candle.is_final]
    if completed != sorted(completed, key=lambda item: item.bucket_time):
        raise EvaluationError("CANDLE_ORDER_INVALID", "candles must be oldest to newest")
    if len({item.bucket_time for item in completed}) != len(completed):
        raise EvaluationError("DUPLICATE_CANDLE", "completed candles contain duplicate buckets")
    return completed


class IndicatorEngine:
    def compute(self, node: ExpressionNode, candles: list[Candle]) -> Decimal | bool:
        if node.type is not ExpressionType.INDICATOR:
            raise EvaluationError("NOT_AN_INDICATOR", "indicator node required")
        try:
            definition = indicator_definition(node)
        except RuleSemanticError as exc:
            raise EvaluationError(exc.code, str(exc)) from exc
        if definition.source != "LOCAL" or definition.calculator is None:
            raise EvaluationError(
                "INDICATOR_PROVIDER_UNAVAILABLE",
                f"{definition.name} requires provider data",
            )
        parameters = normalized_indicator_parameters(node)
        try:
            return definition.calculator(definition.name, candles, parameters, node.output or "VALUE")
        except IndicatorCalculationError as exc:
            raise EvaluationError(exc.code, str(exc)) from exc

    def build_context(
        self,
        rule: ConditionalRuleSpec,
        *,
        bars: Mapping[Timeframe, list[Candle]],
        portfolio: Mapping[str, Decimal],
        current_market: Mapping[str, Decimal] | None = None,
        external_indicators: Mapping[str, IndicatorValue] | None = None,
        previous_external_indicators: Mapping[str, IndicatorValue] | None = None,
        market_data_source_id: str | None = None,
        calculation_profile: str = "DEFAULT",
    ) -> EvaluationContext:
        indicator_nodes = _collect_indicators(
            rule.condition, market_data_source_id=market_data_source_id
        )
        needs_previous = _contains_cross(rule.condition)
        current_indicators: dict[str, Decimal | bool] = {}
        previous_indicators: dict[str, Decimal | bool] = {}
        external_indicators = external_indicators or {}
        previous_external_indicators = previous_external_indicators or {}
        normalized_external: dict[str, IndicatorValue] = {}
        normalized_previous_external: dict[str, IndicatorValue] = {}
        for node in indicator_nodes:
            series = list(bars.get(node.timeframe, []))
            definition = DEFAULT_REGISTRY.get(node.name)
            if definition is None:
                raise EvaluationError("UNSUPPORTED_INDICATOR", f"unsupported indicator {node.name}")
            key = indicator_key(node, market_data_source_id=market_data_source_id)
            if indicator_source(node) == "LOCAL":
                current_indicators[key] = self.compute(node, series)
            elif key in external_indicators:
                try:
                    normalized = DEFAULT_REGISTRY.normalize_value(
                        definition,
                        node,
                        external_indicators[key],
                        {"market_data_source_id": market_data_source_id},
                    )
                except IndicatorProviderError as exc:
                    raise EvaluationError(exc.code, str(exc)) from exc
                normalized_external[key] = normalized
                current_indicators[key] = normalized.value
            else:
                raise EvaluationError(
                    "INDICATOR_PROVIDER_UNAVAILABLE",
                    f"no normalized value supplied for {definition.name}",
                )
            if needs_previous:
                if indicator_source(node) == "LOCAL":
                    previous_indicators[key] = self.compute(node, series[:-1])
                elif key in previous_external_indicators:
                    try:
                        previous_normalized = DEFAULT_REGISTRY.normalize_value(
                            definition,
                            node,
                            previous_external_indicators[key],
                            {"market_data_source_id": market_data_source_id},
                        )
                    except IndicatorProviderError as exc:
                        raise EvaluationError(exc.code, str(exc)) from exc
                    normalized_previous_external[key] = previous_normalized
                    previous_indicators[key] = previous_normalized.value
                else:
                    raise EvaluationError(
                        "INDICATOR_PROVIDER_UNAVAILABLE",
                        f"no previous normalized value supplied for {definition.name}",
                    )

        primary = rule.evaluation.primary_timeframe
        primary_bars = _final_candles(list(bars.get(primary, []))) if primary else []
        current_fields = dict(current_market or {})
        previous_fields: dict[str, Decimal] = {}
        if primary_bars:
            current_fields.update(_candle_fields(primary_bars[-1]))
        if len(primary_bars) >= 2:
            previous_fields.update(_candle_fields(primary_bars[-2]))
        elif needs_previous and _contains_bar_market_field(rule.condition):
            raise EvaluationError(
                "PREVIOUS_FRAME_REQUIRED",
                "bar-close indicator evaluation requires a previous completed bar",
            )
        external_times = [
            _indicator_timestamp(value)
            for value in normalized_external.values()
            if _indicator_timestamp(value) is not None
        ]
        previous_external_times = [
            _indicator_timestamp(value)
            for value in normalized_previous_external.values()
            if _indicator_timestamp(value) is not None
        ]
        observed_at = (
            primary_bars[-1].bucket_time
            if primary_bars
            else max(external_times) if external_times else
            datetime.fromtimestamp(0, tz=timezone.utc)
        )
        previous_at = (
            primary_bars[-2].bucket_time
            if len(primary_bars) >= 2
            else max(previous_external_times) if previous_external_times else observed_at
        )
        current = EvaluationFrame(
            market=current_fields,
            portfolio=dict(portfolio),
            indicators=current_indicators,
            observed_at=observed_at,
            market_data_source_id=market_data_source_id,
        )
        previous = (
            EvaluationFrame(
                market=previous_fields,
                portfolio={},
                indicators=previous_indicators,
                observed_at=previous_at,
                market_data_source_id=market_data_source_id,
            )
            if needs_previous
            else None
        )
        return EvaluationContext(
            current=current,
            previous=previous,
            market_data_source_id=market_data_source_id,
            calculation_profile=calculation_profile,
        )


def _candle_fields(candle: Candle) -> dict[str, Decimal]:
    return {
        "OPEN": candle.open,
        "HIGH": candle.high,
        "LOW": candle.low,
        "CLOSE": candle.close,
        "LAST_PRICE": candle.close,
        "VOLUME": candle.volume,
    }


def _collect_indicators(
    node: ExpressionNode, *, market_data_source_id: str | None = None
) -> tuple[ExpressionNode, ...]:
    found: dict[str, ExpressionNode] = {}

    def visit(value: ExpressionNode | None) -> None:
        if value is None:
            return
        if value.type is ExpressionType.INDICATOR:
            found[indicator_key(value, market_data_source_id=market_data_source_id)] = value
        visit(value.left)
        visit(value.right)
        visit(value.operand)
        for child in value.children or ():
            visit(child)

    visit(node)
    return tuple(found[key] for key in sorted(found))


def _contains_cross(node: ExpressionNode | None) -> bool:
    if node is None:
        return False
    if node.type is ExpressionType.CROSS:
        return True
    return any(
        _contains_cross(child)
        for child in (node.left, node.right, node.operand, *(node.children or ()))
    )


def _contains_bar_market_field(node: ExpressionNode | None) -> bool:
    if node is None:
        return False
    if node.type is ExpressionType.MARKET and node.field != "LAST_PRICE":
        return True
    return any(
        _contains_bar_market_field(child)
        for child in (node.left, node.right, node.operand, *(node.children or ()))
    )


def _indicator_timestamp(value: IndicatorValue) -> datetime | None:
    candidate = value.data_timestamp or value.observed_at
    if not isinstance(candidate, datetime) or candidate.tzinfo is None:
        return None
    return candidate.astimezone(timezone.utc)


def _numeric(value: Decimal | bool, *, code: str) -> Decimal:
    if isinstance(value, bool):
        raise EvaluationError(code, "numeric expression produced boolean")
    return value


def _evaluate(node: ExpressionNode, frame: EvaluationFrame) -> Decimal | bool:
    if node.type is ExpressionType.LITERAL:
        return node.value  # type: ignore[return-value]
    if node.type is ExpressionType.MARKET:
        try:
            return frame.market[node.field or ""]
        except KeyError as exc:
            raise EvaluationError("MARKET_FIELD_MISSING", f"missing {node.field}") from exc
    if node.type is ExpressionType.PORTFOLIO:
        try:
            return frame.portfolio[node.field or ""]
        except KeyError as exc:
            raise EvaluationError("PORTFOLIO_FIELD_MISSING", f"missing {node.field}") from exc
    if node.type is ExpressionType.INDICATOR:
        try:
            return frame.indicators[
                indicator_key(node, market_data_source_id=frame.market_data_source_id)
            ]
        except KeyError as exc:
            raise EvaluationError("INDICATOR_VALUE_MISSING", f"missing {node.name}") from exc
    if node.type is ExpressionType.ARITHMETIC:
        left = _numeric(_evaluate(node.left, frame), code="ARITHMETIC_TYPE_ERROR")  # type: ignore[arg-type]
        right = _numeric(_evaluate(node.right, frame), code="ARITHMETIC_TYPE_ERROR")  # type: ignore[arg-type]
        try:
            return {
                "ADD": lambda: left + right,
                "SUB": lambda: left - right,
                "MUL": lambda: left * right,
                "DIV": lambda: left / right,
            }[node.operator or ""]()
        except KeyError as exc:
            raise EvaluationError("ARITHMETIC_OPERATOR_INVALID", str(node.operator)) from exc
        except (DivisionByZero, InvalidOperation, ZeroDivisionError) as exc:
            raise EvaluationError("DIVISION_BY_ZERO", "runtime divisor is zero") from exc
    if node.type is ExpressionType.COMPARISON:
        left_value = _evaluate(node.left, frame)  # type: ignore[arg-type]
        right_value = _evaluate(node.right, frame)  # type: ignore[arg-type]
        if isinstance(left_value, bool) or isinstance(right_value, bool):
            if not (isinstance(left_value, bool) and isinstance(right_value, bool) and node.operator == "EQ"):
                raise EvaluationError("COMPARISON_TYPE_ERROR", "boolean comparison requires EQ")
            return left_value == right_value
        left = _numeric(left_value, code="COMPARISON_TYPE_ERROR")
        right = _numeric(right_value, code="COMPARISON_TYPE_ERROR")
        operations = {
            "GT": left > right,
            "GTE": left >= right,
            "LT": left < right,
            "LTE": left <= right,
            "EQ": left == right,
        }
        try:
            return operations[node.operator or ""]
        except KeyError as exc:
            raise EvaluationError("COMPARISON_OPERATOR_INVALID", str(node.operator)) from exc
    if node.type is ExpressionType.LOGICAL:
        values = [_evaluate(child, frame) for child in node.children or ()]
        if not all(isinstance(value, bool) for value in values):
            raise EvaluationError("LOGICAL_TYPE_ERROR", "logical operand is not boolean")
        if node.operator == "AND":
            return all(values)
        if node.operator == "OR":
            return any(values)
        raise EvaluationError("LOGICAL_OPERATOR_INVALID", str(node.operator))
    if node.type is ExpressionType.NOT:
        value = _evaluate(node.operand, frame)  # type: ignore[arg-type]
        if not isinstance(value, bool):
            raise EvaluationError("LOGICAL_TYPE_ERROR", "NOT operand is not boolean")
        return not value
    if node.type is ExpressionType.CROSS:
        raise EvaluationError("CROSS_CONTEXT_REQUIRED", "cross requires previous frame")
    raise EvaluationError("EXPRESSION_TYPE_INVALID", str(node.type))


def _evaluate_with_cross(node: ExpressionNode, context: EvaluationContext) -> Decimal | bool:
    if node.type is ExpressionType.CROSS:
        if context.previous is None:
            raise EvaluationError("PREVIOUS_FRAME_REQUIRED", "cross requires previous frame")
        current_left = _numeric(_evaluate(node.left, context.current), code="CROSS_TYPE_ERROR")  # type: ignore[arg-type]
        current_right = _numeric(_evaluate(node.right, context.current), code="CROSS_TYPE_ERROR")  # type: ignore[arg-type]
        previous_left = _numeric(_evaluate(node.left, context.previous), code="CROSS_TYPE_ERROR")  # type: ignore[arg-type]
        previous_right = _numeric(_evaluate(node.right, context.previous), code="CROSS_TYPE_ERROR")  # type: ignore[arg-type]
        if node.operator == "ABOVE":
            return previous_left <= previous_right and current_left > current_right
        if node.operator == "BELOW":
            return previous_left >= previous_right and current_left < current_right
        raise EvaluationError("CROSS_OPERATOR_INVALID", str(node.operator))
    if node.type is ExpressionType.LOGICAL:
        values = [_evaluate_with_cross(child, context) for child in node.children or ()]
        if not all(isinstance(value, bool) for value in values):
            raise EvaluationError("LOGICAL_TYPE_ERROR", "logical operand is not boolean")
        return all(values) if node.operator == "AND" else any(values)
    if node.type is ExpressionType.NOT:
        value = _evaluate_with_cross(node.operand, context)  # type: ignore[arg-type]
        if not isinstance(value, bool):
            raise EvaluationError("LOGICAL_TYPE_ERROR", "NOT operand is not boolean")
        return not value
    return _evaluate(node, context.current)


def evaluate_condition(rule: ConditionalRuleSpec, context: EvaluationContext) -> bool:
    result = _evaluate_with_cross(rule.condition, context)
    if not isinstance(result, bool):
        raise EvaluationError("CONDITION_NOT_BOOLEAN", "condition did not produce boolean")
    return result


__all__ = [
    "Candle",
    "EvaluationContext",
    "EvaluationError",
    "EvaluationFrame",
    "IndicatorEngine",
    "evaluate_condition",
    "indicator_key",
]
