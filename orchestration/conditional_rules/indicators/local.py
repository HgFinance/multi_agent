"""Deterministic local OHLCV indicator calculators.

The module intentionally depends only on a candle-shaped object.  The
conditional-rule evaluator owns the Pydantic Candle contract, while these
calculators remain reusable by backtests and provider adapters.
"""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Any, Protocol

from .base import IndicatorCalculationError, IndicatorParameters, IndicatorScalar


ZERO = Decimal("0")
ONE = Decimal("1")
HUNDRED = Decimal("100")


class CandleLike(Protocol):
    bucket_time: Any
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    is_final: bool


def _fail(code: str, message: str) -> IndicatorCalculationError:
    return IndicatorCalculationError(code, message)


def _final_candles(candles: list[CandleLike]) -> list[CandleLike]:
    completed = [candle for candle in candles if candle.is_final]
    if completed != sorted(completed, key=lambda item: item.bucket_time):
        raise _fail("CANDLE_ORDER_INVALID", "candles must be oldest to newest")
    if len({item.bucket_time for item in completed}) != len(completed):
        raise _fail("DUPLICATE_CANDLE", "completed candles contain duplicate buckets")
    return completed


def _mean(values: list[Decimal]) -> Decimal:
    if not values:
        raise _fail("INSUFFICIENT_HISTORY", "indicator input is empty")
    return sum(values, ZERO) / Decimal(len(values))


def _period(parameters: IndicatorParameters, key: str = "PERIOD") -> int:
    try:
        value = int(parameters[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise _fail("INVALID_INDICATOR_PARAMETER", f"{key} is invalid") from exc
    if value <= 0:
        raise _fail("INVALID_INDICATOR_PARAMETER", f"{key} must be positive")
    return value


def _ema_series(values: list[Decimal], period: int) -> list[Decimal]:
    if len(values) < period:
        raise _fail(
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
        raise _fail(
            "INSUFFICIENT_HISTORY",
            f"Wilder({period}) requires at least {period} observations",
        )
    result = [_mean(values[:period])]
    for value in values[period:]:
        result.append((result[-1] * Decimal(period - 1) + value) / Decimal(period))
    return result


def _rsi(closes: list[Decimal], period: int) -> Decimal:
    if len(closes) < period + 1:
        raise _fail(
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
    required = slow + signal - 1
    if len(closes) < required:
        raise _fail(
            "INSUFFICIENT_HISTORY",
            f"MACD({fast},{slow},{signal}) requires {required} bars",
        )
    fast_series = _ema_series(closes, fast)
    slow_series = _ema_series(closes, slow)
    aligned_fast = fast_series[slow - fast :]
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
        raise _fail(
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


def _envelope(
    closes: list[Decimal], *, period: int, percent: Decimal
) -> dict[str, Decimal]:
    """Envelope bands: a moving average offset by a fixed percentage.

    Unlike Bollinger the band width does not react to volatility, so the two
    are not interchangeable even at the same period.
    """

    if len(closes) < period:
        raise _fail(
            "INSUFFICIENT_HISTORY",
            f"Envelope({period}) requires at least {period} completed bars",
        )
    middle = _mean(closes[-period:])
    ratio = percent / Decimal(100)
    return {
        "UPPER": middle * (Decimal(1) + ratio),
        "MIDDLE": middle,
        "LOWER": middle * (Decimal(1) - ratio),
    }


def _true_ranges(candles: list[CandleLike]) -> list[Decimal]:
    return [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(candles, candles[1:])
    ]


def _atr(candles: list[CandleLike], period: int) -> Decimal:
    if len(candles) < period + 1:
        raise _fail(
            "INSUFFICIENT_HISTORY",
            f"ATR({period}) requires at least {period + 1} completed bars",
        )
    return _wilder_series(_true_ranges(candles), period)[-1]


def _adx(candles: list[CandleLike], period: int) -> Decimal:
    if len(candles) < period * 2 + 1:
        raise _fail(
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
            ZERO
            if denominator == 0
            else HUNDRED * abs(plus_index - minus_index) / denominator
        )
    return _wilder_series(dx_values, period)[-1]


def _stochastic(
    candles: list[CandleLike], *, period: int, smooth: int
) -> dict[str, Decimal]:
    if len(candles) < period + smooth - 1:
        raise _fail(
            "INSUFFICIENT_HISTORY",
            f"STOCHASTIC({period},{smooth}) requires {period + smooth - 1} bars",
        )
    raw: list[Decimal] = []
    for index in range(period - 1, len(candles)):
        window = candles[index - period + 1 : index + 1]
        highest = max(item.high for item in window)
        lowest = min(item.low for item in window)
        raw.append(
            Decimal("50")
            if highest == lowest
            else HUNDRED * (candles[index].close - lowest) / (highest - lowest)
        )
    return {"K": raw[-1], "D": _mean(raw[-smooth:])}


def _cci(candles: list[CandleLike], period: int) -> Decimal:
    if len(candles) < period:
        raise _fail("INSUFFICIENT_HISTORY", f"CCI({period}) requires {period} bars")
    typical = [
        (item.high + item.low + item.close) / Decimal("3") for item in candles
    ]
    window = typical[-period:]
    mean = _mean(window)
    deviation = _mean([abs(value - mean) for value in window])
    return ZERO if deviation == 0 else (window[-1] - mean) / (Decimal("0.015") * deviation)


def _mfi(candles: list[CandleLike], period: int) -> Decimal:
    if len(candles) < period + 1:
        raise _fail("INSUFFICIENT_HISTORY", f"MFI({period}) requires {period + 1} bars")
    typical = [
        (item.high + item.low + item.close) / Decimal("3") for item in candles
    ]
    positive = ZERO
    negative = ZERO
    for index in range(len(candles) - period, len(candles)):
        flow = typical[index] * candles[index].volume
        if typical[index] > typical[index - 1]:
            positive += flow
        elif typical[index] < typical[index - 1]:
            negative += flow
    if negative == 0:
        return HUNDRED if positive > 0 else Decimal("50")
    return HUNDRED - HUNDRED / (ONE + positive / negative)


def _obv(candles: list[CandleLike]) -> Decimal:
    if len(candles) < 2:
        raise _fail("INSUFFICIENT_HISTORY", "OBV requires at least 2 bars")
    total = ZERO
    for previous, current in zip(candles, candles[1:]):
        if current.close > previous.close:
            total += current.volume
        elif current.close < previous.close:
            total -= current.volume
    return total


def _roc(closes: list[Decimal], period: int) -> Decimal:
    if len(closes) < period + 1:
        raise _fail("INSUFFICIENT_HISTORY", f"ROC({period}) requires {period + 1} bars")
    baseline = closes[-period - 1]
    if baseline == 0:
        raise _fail("DIVISION_BY_ZERO", "ROC baseline close is zero")
    return HUNDRED * (closes[-1] - baseline) / baseline


def _vwap(candles: list[CandleLike], period: int) -> Decimal:
    if len(candles) < period:
        raise _fail("INSUFFICIENT_HISTORY", f"VWAP({period}) requires {period} bars")
    window = candles[-period:]
    volume = sum((item.volume for item in window), ZERO)
    if volume == 0:
        raise _fail("ZERO_VOLUME", "VWAP requires positive volume")
    traded_value = sum(
        (((item.high + item.low + item.close) / Decimal("3")) * item.volume)
        for item in window
    )
    return traded_value / volume


def _williams_r(candles: list[CandleLike], period: int) -> Decimal:
    if len(candles) < period:
        raise _fail("INSUFFICIENT_HISTORY", f"WILLIAMS_R({period}) requires {period} bars")
    window = candles[-period:]
    highest = max(item.high for item in window)
    lowest = min(item.low for item in window)
    return (
        Decimal("-50")
        if highest == lowest
        else -HUNDRED * (highest - window[-1].close) / (highest - lowest)
    )


def _donchian(candles: list[CandleLike], period: int) -> dict[str, Decimal]:
    if len(candles) < period:
        raise _fail("INSUFFICIENT_HISTORY", f"DONCHIAN({period}) requires {period} bars")
    window = candles[-period:]
    upper = max(item.high for item in window)
    lower = min(item.low for item in window)
    return {"UPPER": upper, "MIDDLE": (upper + lower) / Decimal("2"), "LOWER": lower}


def _psar(
    candles: list[CandleLike], *, step: Decimal, maximum: Decimal
) -> dict[str, IndicatorScalar]:
    if len(candles) < 2:
        raise _fail("INSUFFICIENT_HISTORY", "PSAR requires at least 2 bars")
    bullish = True
    sar = candles[0].low
    extreme = candles[0].high
    acceleration = step
    for index in range(1, len(candles)):
        current = candles[index]
        sar = sar + acceleration * (extreme - sar)
        if bullish:
            previous_low = candles[index - 1].low
            sar = min(sar, previous_low)
            if index >= 2:
                sar = min(sar, candles[index - 2].low)
            if current.low < sar:
                bullish = False
                sar = extreme
                extreme = current.low
                acceleration = step
            elif current.high > extreme:
                extreme = current.high
                acceleration = min(maximum, acceleration + step)
        else:
            previous_high = candles[index - 1].high
            sar = max(sar, previous_high)
            if index >= 2:
                sar = max(sar, candles[index - 2].high)
            if current.high > sar:
                bullish = True
                sar = extreme
                extreme = current.high
                acceleration = step
            elif current.low < extreme:
                extreme = current.low
                acceleration = min(maximum, acceleration + step)
    return {"VALUE": sar, "TREND": bullish}


def calculate_local_indicator(
    name: str,
    candles: list[Any],
    parameters: IndicatorParameters,
    output: str,
) -> IndicatorScalar:
    """Calculate a canonical local indicator and expose only its declared output."""

    values = _final_candles(candles)
    closes = [item.close for item in values]
    if name == "SMA":
        period = _period(parameters)
        if len(closes) < period:
            raise _fail("INSUFFICIENT_HISTORY", f"SMA({period}) requires {period} bars")
        result: IndicatorScalar = _mean(closes[-period:])
    elif name == "EMA":
        result = _ema_series(closes, _period(parameters))[-1]
    elif name == "RSI":
        result = _rsi(closes, _period(parameters))
    elif name == "MACD":
        result = _macd(
            closes,
            fast=_period(parameters, "FAST"),
            slow=_period(parameters, "SLOW"),
            signal=_period(parameters, "SIGNAL"),
        )[output]
    elif name == "BOLLINGER":
        result = _bollinger(
            closes,
            period=_period(parameters),
            standard_deviations=Decimal(str(parameters["STDDEV"])),
        )[output]
    elif name == "ENVELOPE":
        result = _envelope(
            closes,
            period=_period(parameters),
            percent=Decimal(str(parameters["PERCENT"])),
        )[output]
    elif name in {"VOLUME_AVERAGE", "AVERAGE_VOLUME"}:
        period = _period(parameters)
        if len(values) < period:
            raise _fail(
                "INSUFFICIENT_HISTORY",
                f"VOLUME_AVERAGE({period}) requires {period} bars",
            )
        result = _mean([item.volume for item in values[-period:]])
    elif name == "ATR":
        result = _atr(values, _period(parameters))
    elif name == "ADX":
        result = _adx(values, _period(parameters))
    elif name == "STOCHASTIC":
        result = _stochastic(
            values, period=_period(parameters), smooth=_period(parameters, "SMOOTH")
        )[output]
    elif name == "CCI":
        result = _cci(values, _period(parameters))
    elif name == "MFI":
        result = _mfi(values, _period(parameters))
    elif name == "OBV":
        result = _obv(values)
    elif name == "ROC":
        result = _roc(closes, _period(parameters))
    elif name == "VWAP":
        result = _vwap(values, _period(parameters))
    elif name == "WILLIAMS_R":
        result = _williams_r(values, _period(parameters))
    elif name == "DONCHIAN":
        result = _donchian(values, _period(parameters))[output]
    elif name == "PSAR":
        result = _psar(
            values,
            step=Decimal(str(parameters["STEP"])),
            maximum=Decimal(str(parameters["MAXIMUM"])),
        )[output]
    else:
        raise _fail("UNSUPPORTED_INDICATOR", f"unsupported local indicator {name}")
    if isinstance(result, Decimal) and not result.is_finite():
        raise _fail("NON_FINITE_INDICATOR", f"{name} produced a non-finite value")
    return result
